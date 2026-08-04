#!/usr/bin/env python3
"""Revalidate retained official native forward archives without range fetches.

The ordinary forward collector downloads the GEFS/ECMWF member ranges.  This
operator path is intentionally different: it reuses an already-retained
20260803 archive, HEADs each unique source object, downloads only each unique
provider index (bounded and in-memory), and then builds a fresh inbox whose
archive query receipt is the revalidation time.  No member range is fetched,
no SQLite table is opened, and no old archive/inbox is overwritten.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    bounded_json_bytes,
    load_bounded_json,
)


DEFAULT_ROOT = ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
DEFAULT_REGISTRY = DEFAULT_ROOT / "market_registry_forward.json"
DEFAULT_PROXY = "http://127.0.0.1:1082"
MAX_STATIONS = 45
MAX_WORKERS = 4
MAX_REQUESTS = 1_000
MAX_INDEX_BYTES = 3 * 1024 * 1024
MAX_TOTAL_INDEX_BYTES = 128 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 120.0
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
ALLOWED_HOSTS = frozenset({
    "noaa-gefs-pds.s3.amazonaws.com",
    "ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
    "ecmwf-forecasts.s3.amazonaws.com",
})
SCHEMA_BY_PROVIDER = {
    "NOAA_GEFS": "noaa_gefs_official_historical_multistation_members_v2",
    "ECMWF": "ecmwf_ifs_open_pf50_historical_multistation_members_v1",
}
OBJECT_SUFFIX_BY_PROVIDER = {"NOAA_GEFS": ".idx", "ECMWF": ".index"}
_HTTP_CLIENT_LOCAL = threading.local()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _parse_provider_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _proxy(proxy: str) -> str:
    value = str(proxy or "").strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 1082
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError("official_forward_revalidation_proxy_invalid")
    return value


def _safe_url(value: object) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ContractError("official_forward_revalidation_url_invalid")
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def _alternate_ecmwf_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.hostname == "ecmwf-forecasts.s3.eu-central-1.amazonaws.com":
        host = "ecmwf-forecasts.s3.amazonaws.com"
    elif parsed.hostname == "ecmwf-forecasts.s3.amazonaws.com":
        host = "ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
    else:
        return None
    return urlunsplit(("https", host, parsed.path, "", ""))


def _object_url(capture: Mapping[str, Any], receipt: Mapping[str, Any]) -> str:
    prefix = _safe_url(capture.get("source_prefix"))
    parsed_prefix = urlsplit(prefix)
    key = str(receipt.get("object_key") or "").strip().lstrip("/")
    if not key or ".." in key.split("/"):
        raise ContractError("official_forward_revalidation_object_key_invalid")
    # Both retained formats store a full provider key in object_key.  Using
    # the source-prefix host preserves the exact official regional endpoint
    # used by the original capture without concatenating the prefix twice.
    return _safe_url(urlunsplit((
        "https",
        parsed_prefix.netloc,
        "/" + key,
        "",
        "",
    )))


def _archive_identity(archive: Mapping[str, Any], *, provider: str) -> dict[str, Any]:
    if not isinstance(archive, Mapping):
        raise ContractError("official_forward_revalidation_archive_invalid")
    payload = dict(archive)
    identity = {
        key: value for key, value in payload.items() if key != "content_sha256"
    }
    if (
        payload.get("schema") != SCHEMA_BY_PROVIDER[provider]
        or payload.get("content_sha256") != content_sha256(identity)
        or payload.get("event_date") != "2026-08-03"
        or payload.get("training_only") is not True
        or payload.get("holdout_eligible") is not False
        or payload.get("station_count") != MAX_STATIONS
        or not isinstance(payload.get("station_ids"), list)
        or len(payload["station_ids"]) != MAX_STATIONS
        or len(set(str(item).upper() for item in payload["station_ids"]))
        != MAX_STATIONS
        or not isinstance(payload.get("captures"), list)
        or len(payload["captures"]) != MAX_STATIONS
    ):
        raise ContractError(
            "official_forward_revalidation_archive_identity_invalid:" + provider
        )
    stations = {str(item).strip().upper() for item in payload["station_ids"]}
    captures = {
        str(row.get("station_id") or "").strip().upper()
        for row in payload["captures"]
        if isinstance(row, Mapping)
    }
    if stations != captures or "" in captures:
        raise ContractError(
            "official_forward_revalidation_archive_station_scope_invalid:" + provider
        )
    for capture in payload["captures"]:
        if not isinstance(capture, Mapping):
            raise ContractError("official_forward_revalidation_capture_invalid")
        cap_identity = {
            key: value
            for key, value in capture.items()
            if key != "content_sha256"
        }
        if capture.get("content_sha256") != content_sha256(cap_identity):
            raise ContractError(
                "official_forward_revalidation_capture_hash_invalid:" + provider
            )
        if not isinstance(capture.get("field_receipts"), list):
            raise ContractError("official_forward_revalidation_receipts_missing")
    return payload


def _collect_receipts(
    archive: Mapping[str, Any],
    *,
    provider: str,
) -> list[dict[str, Any]]:
    by_object: dict[str, dict[str, Any]] = {}
    for capture in archive["captures"]:
        for raw in capture["field_receipts"]:
            if not isinstance(raw, Mapping):
                raise ContractError("official_forward_revalidation_receipt_invalid")
            receipt = dict(raw)
            object_url = _object_url(capture, receipt)
            object_key = str(receipt.get("object_key") or "")
            if provider == "ECMWF":
                if not object_url.endswith(".grib2"):
                    raise ContractError(
                        "official_forward_revalidation_ecmwf_object_suffix_invalid"
                    )
                index_url = object_url[:-len(".grib2")] + ".index"
            else:
                index_url = object_url + OBJECT_SUFFIX_BY_PROVIDER[provider]
            expected_index = str(receipt.get("index_sha256") or "").lower()
            expected_message = str(receipt.get("message_sha256") or "").lower()
            if (
                not re.fullmatch(r"[0-9a-f]{64}", expected_index)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_message)
            ):
                raise ContractError("official_forward_revalidation_receipt_hash_invalid")
            existing = by_object.get(object_url)
            if existing is None:
                by_object[object_url] = {
                    "provider": provider,
                    "object_key": object_key,
                    "object_url": object_url,
                    "index_url": index_url,
                    "expected_index_sha256": expected_index,
                    "expected_message_sha256": expected_message,
                    "expected_message_sha256s": [expected_message],
                    "expected_last_modified": str(
                        receipt.get("last_modified") or ""
                    ),
                    "range_end_max": int(receipt.get("range_end") or -1),
                }
            else:
                if (
                    existing["expected_index_sha256"] != expected_index
                    or existing["index_url"] != index_url
                ):
                    raise ContractError(
                        "official_forward_revalidation_duplicate_receipt_conflict"
                    )
                # A single ECMWF GRIB2 object contains many ensemble-member
                # messages.  Its retained field receipts therefore share one
                # index hash but legitimately carry a distinct message hash
                # for each byte range.  This path deliberately does not
                # redownload member ranges, so retain the complete set for
                # audit context while validating only the object/index
                # identity here.
                message_hashes = set(existing.get("expected_message_sha256s") or ())
                message_hashes.add(expected_message)
                existing["expected_message_sha256s"] = sorted(message_hashes)
                existing["range_end_max"] = max(
                    int(existing["range_end_max"]),
                    int(receipt.get("range_end") or -1),
                )
    result = [by_object[key] for key in sorted(by_object)]
    if not result or len(result) > MAX_REQUESTS:
        raise ContractError("official_forward_revalidation_request_bound_invalid")
    return result


def _response_header(response: httpx.Response, name: str) -> str:
    return str(response.headers.get(name) or response.headers.get(name.lower()) or "").strip()


def _http_client(proxy: str) -> httpx.Client:
    """Reuse one bounded TLS connection per revalidation worker thread."""

    client = getattr(_HTTP_CLIENT_LOCAL, "client", None)
    if client is None:
        client = httpx.Client(
            proxy=proxy,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, pool=30.0),
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
                keepalive_expiry=30.0,
            ),
            headers={"User-Agent": "weather-official-forward-revalidation/1"},
        )
        _HTTP_CLIENT_LOCAL.client = client
    return client


def _discard_http_client() -> None:
    client = getattr(_HTTP_CLIENT_LOCAL, "client", None)
    if client is None:
        return
    try:
        client.close()
    finally:
        try:
            delattr(_HTTP_CLIENT_LOCAL, "client")
        except AttributeError:
            pass


def _one_request(
    item: Mapping[str, Any],
    *,
    proxy: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    provider = str(item["provider"])
    object_url = str(item["object_url"])
    index_url = str(item["index_url"])
    last_error = ""
    for attempt in range(3):
        try:
            client = _http_client(proxy)
            head = client.head(object_url, timeout=timeout_seconds)
            if head.status_code in TRANSIENT_STATUS and attempt < 2:
                _discard_http_client()
                time.sleep(min(8.0, 2.0 ** attempt))
                continue
            if head.status_code != 200:
                raise ContractError(
                    f"official_forward_revalidation_object_http_{head.status_code}"
                )
            length_text = _response_header(head, "Content-Length")
            content_length = int(length_text) if length_text.isdigit() else None
            if content_length is None or content_length <= int(item["range_end_max"]):
                raise ContractError("official_forward_revalidation_object_length_invalid")
            last_modified = _response_header(head, "Last-Modified")
            if not last_modified or _parse_provider_time(last_modified) is None:
                raise ContractError("official_forward_revalidation_object_last_modified_missing")
            expected_modified = str(item.get("expected_last_modified") or "")
            expected_time = _parse_provider_time(expected_modified)
            if expected_time is None or _parse_provider_time(last_modified) != expected_time:
                raise ContractError("official_forward_revalidation_object_last_modified_changed")
            etag = _response_header(head, "ETag")
            response_bytes = 0
            digest = hashlib.sha256()
            index_urls = [index_url]
            if provider == "ECMWF":
                alternate = _alternate_ecmwf_url(index_url)
                if alternate:
                    index_urls.append(alternate)
            index_url_used = index_url
            transient_index_status: int | None = None
            for candidate_index_url in index_urls:
                response_bytes = 0
                digest = hashlib.sha256()
                with client.stream("GET", candidate_index_url, timeout=timeout_seconds) as index_response:
                    if index_response.status_code in TRANSIENT_STATUS:
                        transient_index_status = int(index_response.status_code)
                        continue
                    if index_response.status_code != 200:
                        raise ContractError(
                            f"official_forward_revalidation_index_http_{index_response.status_code}"
                        )
                    for chunk in index_response.iter_bytes(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        response_bytes += len(chunk)
                        if response_bytes > MAX_INDEX_BYTES:
                            raise ContractError("official_forward_revalidation_index_too_large")
                        digest.update(chunk)
                index_url_used = candidate_index_url
                break
            else:
                _discard_http_client()
                if attempt < 2:
                    time.sleep(min(8.0, 2.0 ** attempt))
                    continue
                raise ContractError(
                    "official_forward_revalidation_index_http_"
                    + str(transient_index_status or "transient")
                )
            observed_index = digest.hexdigest()
            expected_index = str(item["expected_index_sha256"])
            if observed_index != expected_index:
                raise ContractError("official_forward_revalidation_index_hash_changed")
            return {
                **dict(item),
                "state": "PASS",
                "head_status": int(head.status_code),
                "head_content_length": content_length,
                "head_etag": etag,
                "head_last_modified": last_modified,
                "index_status": 200,
                "index_url_used": index_url_used,
                "index_bytes": response_bytes,
                "observed_index_sha256": observed_index,
                # A HEAD cannot prove a range's SHA-256.  Re-downloading
                # the member range is explicitly forbidden on this path;
                # the retained message hash is therefore carried with a
                # precise, non-cryptographic revalidation status.
                "message_sha256_recomputed": False,
                "message_sha256_revalidation": "HEAD_LENGTH_LAST_MODIFIED_ONLY",
            }
        except (httpx.HTTPError, OSError, ContractError) as exc:
            last_error = f"{type(exc).__name__}:{str(exc)[:240]}"
            _discard_http_client()
            if isinstance(exc, ContractError) and "http_" not in str(exc):
                break
    return {
        **dict(item),
        "state": "BLOCKED",
        "error": last_error or "official_forward_revalidation_unknown_error",
    }


def _revalidate(
    items: Sequence[Mapping[str, Any]],
    *,
    proxy: str,
    timeout_seconds: float,
    workers: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    worker_count = max(1, min(int(workers), MAX_WORKERS, len(items)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="native-reval") as pool:
        futures = {
            pool.submit(
                _one_request,
                item,
                proxy=proxy,
                timeout_seconds=timeout_seconds,
            ): item
            for item in items
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # defensive boundary; fail closed below
                item = dict(futures[future])
                results.append({
                    **item,
                    "state": "BLOCKED",
                    "error": f"{type(exc).__name__}:{str(exc)[:240]}",
                })
    return sorted(results, key=lambda row: str(row.get("object_url") or ""))


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = path.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise ContractError("official_forward_revalidation_output_exists")
    raw = bounded_json_bytes(value, limit=MAX_JSON_OUTPUT_BYTES)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.tmp.{os.getpid()}"
    )
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _fresh_archive(
    archive: Mapping[str, Any],
    *,
    provider: str,
    captured_at_utc: str,
    validation_audit_sha256: str,
) -> dict[str, Any]:
    old_query = str(archive.get("archive_query_at_utc") or "")
    captures: list[dict[str, Any]] = []
    for raw_capture in archive.get("captures") or ():
        capture = dict(raw_capture)
        family_curve = capture.get("family_temperature_curve")
        if isinstance(family_curve, Mapping):
            curve_identity = {
                key: value
                for key, value in family_curve.items()
                if key != "content_sha256"
            }
            curve_identity["available_at_utc"] = captured_at_utc
            capture["family_temperature_curve"] = {
                **curve_identity,
                "content_sha256": content_sha256(curve_identity),
            }
        capture["archive_query_at_utc"] = captured_at_utc
        capture_identity = {
            key: value for key, value in capture.items()
            if key != "content_sha256"
        }
        capture["content_sha256"] = content_sha256(capture_identity)
        captures.append(capture)
    identity = {
        key: value for key, value in archive.items() if key != "content_sha256"
    }
    identity.update({
        "archive_query_at_utc": captured_at_utc,
        "captures": captures,
        "revalidation": {
            "schema": "official_native_forward_archive_revalidation_v1",
            "mode": "REUSED_RAW_HEAD_AND_INDEX",
            "provider": provider,
            "validated_at_utc": captured_at_utc,
            "prior_archive_content_sha256": str(archive["content_sha256"]),
            "prior_archive_query_at_utc": old_query,
            "validation_audit_sha256": validation_audit_sha256,
            "member_range_downloaded": False,
            "member_message_sha256_recomputed": False,
            "index_hashes_recomputed": True,
            "source_object_head_revalidated": True,
        },
    })
    return {**identity, "content_sha256": content_sha256(identity)}


def _verify_inbox(path: Path, *, event_date: str) -> dict[str, Any]:
    raw = load_bounded_json(path)
    if not isinstance(raw, Mapping):
        raise ContractError("official_forward_revalidation_inbox_invalid")
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    packets = raw.get("packets")
    if (
        raw.get("schema") != "native_ensemble_forward_inbox_v1"
        or raw.get("content_sha256") != content_sha256(identity)
        or not isinstance(packets, list)
        or len(packets) != MAX_STATIONS
    ):
        raise ContractError("official_forward_revalidation_inbox_invalid")
    scopes = {
        (str(packet.get("station_id") or "").upper(), str(packet.get("event_date") or ""))
        for packet in packets
        if isinstance(packet, Mapping)
    }
    if len(scopes) != MAX_STATIONS or any(day != event_date for _station, day in scopes):
        raise ContractError("official_forward_revalidation_inbox_scope_invalid")
    families = {
        str(system.get("independence_family_id") or "")
        for packet in packets
        if isinstance(packet, Mapping)
        for system in packet.get("systems") or ()
        if isinstance(system, Mapping)
    }
    if not {"ECMWF", "NOAA_GEFS"}.issubset(families):
        raise ContractError("official_forward_revalidation_inbox_family_missing")
    return dict(raw)


def revalidate_and_build(
    *,
    gefs_archive: Path,
    ecmwf_archive: Path,
    market_registry: Path,
    gefs_output: Path,
    ecmwf_output: Path,
    inbox_output: Path,
    audit_output: Path,
    proxy: str,
    timeout_seconds: float,
    workers: int,
    prior_audit: Path | None = None,
) -> dict[str, Any]:
    proxy = _proxy(proxy)
    if not 5.0 <= float(timeout_seconds) <= MAX_TIMEOUT_SECONDS:
        raise ContractError("official_forward_revalidation_timeout_invalid")
    source_paths = {
        gefs_archive.expanduser().resolve(strict=True),
        ecmwf_archive.expanduser().resolve(strict=True),
    }
    output_paths = {
        gefs_output.expanduser().resolve(),
        ecmwf_output.expanduser().resolve(),
        inbox_output.expanduser().resolve(),
        audit_output.expanduser().resolve(),
    }
    if source_paths & output_paths or len(output_paths) != 4:
        raise ContractError("official_forward_revalidation_output_path_conflict")
    if any(path.exists() or path.is_symlink() for path in output_paths):
        raise ContractError("official_forward_revalidation_output_exists")
    gefs = _archive_identity(
        load_bounded_json(gefs_archive.expanduser().resolve()),
        provider="NOAA_GEFS",
    )
    ecmwf = _archive_identity(
        load_bounded_json(ecmwf_archive.expanduser().resolve()),
        provider="ECMWF",
    )
    event_date = str(gefs["event_date"])
    if str(ecmwf["event_date"]) != event_date:
        raise ContractError("official_forward_revalidation_event_date_mismatch")
    all_items = [
        *_collect_receipts(gefs, provider="NOAA_GEFS"),
        *_collect_receipts(ecmwf, provider="ECMWF"),
    ]
    if len(all_items) > MAX_REQUESTS:
        raise ContractError("official_forward_revalidation_request_bound_invalid")
    captured_at = _utc_now()
    reused_results: dict[tuple[str, str], dict[str, Any]] = {}
    if prior_audit is not None:
        prior = load_bounded_json(prior_audit.expanduser().resolve(strict=True))
        prior_identity = {
            key: value for key, value in prior.items()
            if key != "content_sha256"
        }
        if (
            prior.get("schema") != "official_native_forward_archive_revalidation_audit_v1"
            or prior.get("content_sha256") != content_sha256(prior_identity)
            or prior.get("state") != "BLOCKED_FAIL_CLOSED"
            or str(prior.get("proxy") or "") != proxy
        ):
            raise ContractError("official_forward_revalidation_prior_audit_invalid")
        prior_sources = prior.get("source_archives")
        if not isinstance(prior_sources, Mapping):
            raise ContractError("official_forward_revalidation_prior_sources_invalid")
        if (
            str((prior_sources.get("NOAA_GEFS") or {}).get("content_sha256") or "")
            != str(gefs["content_sha256"])
            or str((prior_sources.get("ECMWF") or {}).get("content_sha256") or "")
            != str(ecmwf["content_sha256"])
        ):
            raise ContractError("official_forward_revalidation_prior_source_changed")
        for row in prior.get("results") or ():
            if not isinstance(row, Mapping) or row.get("state") != "PASS":
                continue
            key = (str(row.get("provider") or ""), str(row.get("object_url") or ""))
            if key[0] and key[1]:
                reused_results[key] = dict(row)
    pending_items = [
        item for item in all_items
        if (str(item.get("provider") or ""), str(item.get("object_url") or ""))
        not in reused_results
    ]
    fresh_results = _revalidate(
        pending_items,
        proxy=proxy,
        timeout_seconds=float(timeout_seconds),
        workers=workers,
    ) if pending_items else []
    results = sorted(
        [*reused_results.values(), *fresh_results],
        key=lambda row: str(row.get("object_url") or ""),
    )
    total_index_bytes = sum(int(row.get("index_bytes") or 0) for row in results)
    blocked = [row for row in results if row.get("state") != "PASS"]
    if total_index_bytes > MAX_TOTAL_INDEX_BYTES:
        blocked.append({"state": "BLOCKED", "error": "official_forward_revalidation_total_index_bytes_exceeded"})
    audit_identity = {
        "schema": "official_native_forward_archive_revalidation_audit_v1",
        "state": "PASS" if not blocked else "BLOCKED_FAIL_CLOSED",
        "validated_at_utc": captured_at,
        "event_date": event_date,
        "proxy": proxy,
        "timeout_seconds": float(timeout_seconds),
        "worker_count": max(1, min(int(workers), MAX_WORKERS, len(all_items))),
        "source_archives": {
            "NOAA_GEFS": {
                "path": str(gefs_archive.expanduser().resolve()),
                "content_sha256": str(gefs["content_sha256"]),
                "archive_query_at_utc": str(gefs["archive_query_at_utc"]),
                "station_count": int(gefs["station_count"]),
            },
            "ECMWF": {
                "path": str(ecmwf_archive.expanduser().resolve()),
                "content_sha256": str(ecmwf["content_sha256"]),
                "archive_query_at_utc": str(ecmwf["archive_query_at_utc"]),
                "station_count": int(ecmwf["station_count"]),
            },
        },
        "request_count": len(all_items),
        "reused_pass_count": len(reused_results),
        "network_request_count": len(pending_items),
        "unique_source_object_count": len(all_items),
        "unique_index_count": len(all_items),
        "head_object_pass_count": sum(row.get("head_status") == 200 for row in results),
        "index_hash_pass_count": sum(row.get("observed_index_sha256") == row.get("expected_index_sha256") for row in results),
        "message_sha256_recomputed_count": 0,
        "member_range_downloaded": False,
        "total_index_bytes": total_index_bytes,
        "blocked_count": len(blocked),
        "results": results,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    audit = {**audit_identity, "content_sha256": content_sha256(audit_identity)}
    if blocked:
        _write_new_json(audit_output, audit)
        raise ContractError(
            "official_forward_revalidation_blocked:" + str(blocked[0].get("error") or "unknown")
        )
    audit_sha = str(audit["content_sha256"])
    new_gefs = _fresh_archive(
        gefs,
        provider="NOAA_GEFS",
        captured_at_utc=captured_at,
        validation_audit_sha256=audit_sha,
    )
    new_ecmwf = _fresh_archive(
        ecmwf,
        provider="ECMWF",
        captured_at_utc=captured_at,
        validation_audit_sha256=audit_sha,
    )
    with tempfile.TemporaryDirectory(prefix="official-forward-revalidation-") as temporary:
        temp_root = Path(temporary)
        temp_gefs = temp_root / "gefs.json"
        temp_ecmwf = temp_root / "ecmwf.json"
        temp_inbox = temp_root / "inbox.json"
        _write_new_json(temp_gefs, new_gefs)
        _write_new_json(temp_ecmwf, new_ecmwf)
        command = [
            sys.executable,
            str(ROOT / "scripts" / "build_official_native_forward_inbox.py"),
            "--archive", str(temp_gefs),
            "--archive", str(temp_ecmwf),
            "--market-registry", str(market_registry.expanduser().resolve(strict=True)),
            "--output", str(temp_inbox),
        ]
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        inbox = _verify_inbox(temp_inbox, event_date=event_date)
        _write_new_json(gefs_output, new_gefs)
        _write_new_json(ecmwf_output, new_ecmwf)
        _write_new_json(inbox_output, inbox)
    audit_identity["new_outputs"] = {
        "NOAA_GEFS": {"path": str(gefs_output.expanduser().resolve()), "content_sha256": str(new_gefs["content_sha256"])},
        "ECMWF": {"path": str(ecmwf_output.expanduser().resolve()), "content_sha256": str(new_ecmwf["content_sha256"])},
        "inbox": {"path": str(inbox_output.expanduser().resolve()), "content_sha256": str(inbox["content_sha256"]), "packet_count": len(inbox["packets"])},
    }
    audit_identity["inbox_builder_stdout"] = completed.stdout.strip()
    audit = {**audit_identity, "content_sha256": content_sha256(audit_identity)}
    _write_new_json(audit_output, audit)
    return {
        "schema": audit["schema"],
        "state": audit["state"],
        "validated_at_utc": captured_at,
        "request_count": len(all_items),
        "member_range_downloaded": False,
        "new_archive_paths": [str(gefs_output), str(ecmwf_output)],
        "new_inbox_path": str(inbox_output),
        "audit_path": str(audit_output),
        "audit_content_sha256": str(audit["content_sha256"]),
        "packet_count": len(inbox["packets"]),
        "orders_submitted": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gefs-archive", type=Path, required=True)
    parser.add_argument("--ecmwf-archive", type=Path, required=True)
    parser.add_argument("--market-registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--gefs-output", type=Path, required=True)
    parser.add_argument("--ecmwf-output", type=Path, required=True)
    parser.add_argument("--inbox-output", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--proxy", default=DEFAULT_PROXY)
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--prior-audit", type=Path, default=None,
        help="Reuse PASS rows from a prior blocked audit and retry only its blockers.",
    )
    args = parser.parse_args(argv)
    try:
        result = revalidate_and_build(
            gefs_archive=args.gefs_archive,
            ecmwf_archive=args.ecmwf_archive,
            market_registry=args.market_registry,
            gefs_output=args.gefs_output,
            ecmwf_output=args.ecmwf_output,
            inbox_output=args.inbox_output,
            audit_output=args.audit_output,
            proxy=str(args.proxy),
            timeout_seconds=float(args.timeout_seconds),
            workers=int(args.workers),
            prior_audit=args.prior_audit,
        )
    except (ContractError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({
            "schema": "official_native_forward_archive_revalidation_audit_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "member_range_downloaded": False,
            "writes_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
