#!/usr/bin/env python3
"""Rebuild settlement-time truth observations and isolated reference evidence.

This is an isolated historical-research step.  It never writes either
production database, canonical live facts, decisions, or orders.  AMSC owns
the stations declared ``amsc`` by the target registry for formal truth; AWC
owns the stations declared ``awc`` for formal truth and may be queried for
separate reference evidence on AMSC stations.  Existing aggregate truth files
are not read.
Only compact, content-addressed raw report objects, a bounded formal
observation evidence file, and a separate bounded reference evidence file are
retained.  For AMSC-lane stations, whole-hour AMSC METARs are the formal
primary truth; AMSC half-hour METARs and AWC hourly METARs queried for China
stations are reference-only and can never become formal truth by fallback.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import math
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_raw_history_archive import raw_text_sha256  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.et_history import assert_event_day_started, event_day_window  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    MAX_TARGET_RESEARCH_RAW_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.settlement_time import (  # noqa: E402
    historical_registry_scope_settlement_timezone,
)
from bayes_basket_target.source_policy import (  # noqa: E402
    AMSC_HALF_HOUR_REFERENCE,
    AWC_HOUR_REFERENCE_FOR_AMSC,
    reference_only_reason,
)
from official_nwp_https_transport import official_nwp_proxy_url  # noqa: E402
from scripts.capture_amsc_historical_backfill import _curl_tls_verified_payload  # noqa: E402
from scripts.fetch_zuuu_metar_taf import ENDPOINT as AMSC_ENDPOINT, token_candidates  # noqa: E402


SCHEMA = "bayes_basket_target_historical_truth_rebuild_evidence_v1"
REFERENCE_SCHEMA = "bayes_basket_target_truth_reference_evidence_v1"
# Emit the same bounded observation contract consumed by settlement history and
# calibration readers.  The enclosing rebuild receipt still marks the packet
# as historical-only; no historical row is thereby treated as a live arrival.
OBSERVATION_SCHEMA = "bayes_basket_target_canonical_observation_evidence_v1"
AMSC_LOCAL = ZoneInfo("Asia/Shanghai")
AMSC_UTC = timezone.utc
DEFAULT_LANE_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_historical_truth_rebuild_evidence.json"
DEFAULT_OBSERVATIONS = ROOT / "output/audits/bayes_basket_target_historical_truth_observations.json"
DEFAULT_REFERENCE_OUTPUT = ROOT / "output/audits/bayes_basket_target_truth_reference_evidence.json"
DEFAULT_RAW_ROOT = ROOT / "data/research/bayes_basket_target/historical_truth_raw"
DEFAULT_PROXY_CONFIG = ROOT / "config/trading/bayes_basket_target_research_network_v1.json"
DEFAULT_AWC_IDS = ""
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DOH_RESPONSE_BYTES = 64 * 1024
AWC_HOST = "aviationweather.gov"
# Shadowrocket/MacPacket can return a synthetic 198.18.0.0/15 address for
# aviationweather.gov.  The proxy CONNECT succeeds, but TLS then fails before
# the official AWC service sees the request.  Resolve only the address here;
# the request itself still uses the official hostname, SNI, certificate and
# API response.  Do not use the resolver as a data source.
AWC_DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query?name=aviationweather.gov&type=A",
    "https://dns.google/resolve?name=aviationweather.gov&type=A",
)
_AWC_CONNECT_TO: tuple[str, ...] = ()
# Historical repair is an offline, bounded job.  Keep transport retries
# short; a dead proxy must produce a durable blocker receipt instead of
# holding the process for hours across every station/day chunk.
DEFAULT_AWC_FETCH_ATTEMPTS = 1
DEFAULT_AWC_FETCH_TIMEOUT_SEC = 15
DEFAULT_AWC_BATCH_BUDGET_SEC = 60.0
MAX_DATES = 32
MAX_STATIONS = 64
AWC_QUERY_CHUNK = 8
AWC_MIN_INTERVAL_SEC = 0.75
METAR_RE = re.compile(r"\b(?:METAR|SPECI)\s+([A-Z0-9]{4})\s+(\d{2})(\d{2})(\d{2})Z\b")
TEMP_RE = re.compile(r"(?<!TX)(?<!TN)\b(M?\d{2}|//)/(M?\d{2}|//)\b")
_RESEARCH_DIRECT = False


def _active_proxy_url() -> str:
    return "" if _RESEARCH_DIRECT else official_nwp_proxy_url()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _research_proxy_url(path: Path, explicit: str = "") -> str:
    """Select a reachable isolated research proxy, never a shared fallback."""
    if explicit:
        candidates = [str(explicit).strip()]
    else:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("enabled") is not True:
            return ""
        candidates = [str(item).strip() for item in payload.get("proxy_candidates", []) if str(item).strip()]
        candidates.extend([
            str(payload.get("proxy_url") or "").strip(),
            str(payload.get("fallback_proxy_url") or "").strip(),
        ])
    seen: set[str] = set()
    for value in candidates:
        if not value or value in seen:
            continue
        seen.add(value)
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
            raise ValueError("bayes_basket_target_research_proxy_invalid")
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
                return value
        except OSError:
            continue
    raise RuntimeError("bayes_basket_target_research_proxy_unreachable")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _parse_utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _settlement_window(event_date: str, target_timezone: str) -> tuple[datetime, datetime]:
    day = datetime.fromisoformat(event_date).date()
    start = datetime(
        day.year,
        day.month,
        day.day,
        tzinfo=ZoneInfo(target_timezone),
    ).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _stations(path: Path) -> dict[str, str]:
    value = load_bounded_json(path.resolve())
    value = value.get("stations") if isinstance(value, Mapping) else None
    if not isinstance(value, Mapping):
        raise ValueError("station_lane_map_invalid")
    result = {str(k).strip().upper(): str(v).strip().lower() for k, v in value.items() if str(k).strip()}
    if not result or len(result) > MAX_STATIONS or set(result.values()) - {"amsc", "awc"}:
        raise ValueError("station_lane_map_invalid")
    return result


def _registry_timezone(
    path: Path,
    *,
    station_ids: list[str],
    event_dates: list[str],
) -> str:
    value = load_bounded_json(path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES)
    rows = value.get("rows") if isinstance(value, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("historical_truth_timezone_registry_rows_missing")
    # Historical weather dates commonly predate the retained market row for
    # that station.  Bind them to the registry's station-invariant settlement
    # timezone instead of requiring a synthetic historical market row.
    return historical_registry_scope_settlement_timezone(
        (row for row in rows if isinstance(row, Mapping)),
        station_ids=station_ids,
        event_dates=event_dates,
    )


def _dates(values: list[str]) -> list[str]:
    result = sorted({datetime.fromisoformat(str(v)).date().isoformat() for v in values})
    if not result or len(result) > MAX_DATES:
        raise ValueError("event_dates_invalid")
    return result


def _atomic_gzip_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = gzip.compress(_canonical_bytes(value), mtime=0)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _store_raw(
    root: Path,
    raw_sha: str,
    value: Mapping[str, Any],
    *,
    projected_bytes: list[int] | None = None,
) -> str:
    path = root / "objects" / raw_sha[:2] / f"{raw_sha}.json.gz"
    if path.exists():
        return str(path.resolve())
    # A rebuild can contain tens of thousands of content-addressed objects.
    # The caller supplies a batch-local running total so this guard remains
    # linear in the number of new objects instead of recursively stat'ing the
    # entire archive for every row.
    projected = (
        int(projected_bytes[0])
        if projected_bytes is not None
        else sum(p.stat().st_size for p in (root / "objects").rglob("*.json.gz"))
        if (root / "objects").exists()
        else 0
    )
    encoded = gzip.compress(_canonical_bytes(value), mtime=0)
    if projected + len(encoded) > MAX_TARGET_RESEARCH_RAW_BYTES:
        raise RuntimeError("historical_truth_raw_budget_exceeded")
    if shutil_free(root) < MIN_TARGET_FREE_BYTES + len(encoded):
        raise RuntimeError("historical_truth_min_free_space_guard")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if projected_bytes is not None:
        projected_bytes[0] = projected + len(encoded)
    return str(path.resolve())


def shutil_free(path: Path) -> int:
    import shutil
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return int(shutil.disk_usage(candidate).free)


def _signed_temp(token: str) -> float | None:
    if not token or token == "//":
        return None
    negative = token.startswith("M")
    number = token[1:] if negative else token
    try:
        value = float(number)
    except ValueError:
        return None
    return -value if negative else value


def _parse_metar(station: str, raw_text: str, valid_at: datetime | None = None) -> tuple[str, datetime, float] | None:
    match = METAR_RE.search(raw_text.replace("\n", " "))
    if not match or match.group(1).upper() != station:
        return None
    if valid_at is None:
        # The API's oDateTime is authoritative and is passed by callers.  The
        # DDHHMMZ token is retained in raw_text for audit only.
        return None
    temp_match = TEMP_RE.search(raw_text.replace("\n", " "))
    if not temp_match:
        return None
    temperature = _signed_temp(temp_match.group(1))
    if temperature is None:
        return None
    report_type = raw_text.split(None, 1)[0].upper()
    return report_type, valid_at, temperature


def _as_reference(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        **dict(row),
        "evidence_role": "REFERENCE_ONLY",
        "reference_reason": reason,
    }


def _parse_doh_ipv4_addresses(payload: Any, *, host: str = AWC_HOST) -> tuple[str, ...]:
    """Extract bounded A records for transport routing, never weather data."""

    if not isinstance(payload, Mapping):
        return ()
    answers = payload.get("Answer")
    if not isinstance(answers, list):
        return ()
    expected = host.rstrip(".").lower() + "."
    result: list[str] = []
    for answer in answers:
        if not isinstance(answer, Mapping) or int(answer.get("type") or 0) != 1:
            continue
        name = str(answer.get("name") or "").strip().lower()
        if name and name.rstrip(".") + "." != expected:
            continue
        address = str(answer.get("data") or "").strip()
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.version != 4 or address in result:
            continue
        # Avoid accidentally turning a proxy-reserved synthetic address back
        # into the target.  The provider's current public A records are not in
        # this range; a resolver returning it is treated as unusable.
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved:
            continue
        result.append(address)
        if len(result) >= 4:
            break
    return tuple(result)


def _resolve_awc_connect_to(proxy_url: str, *, timeout_sec: int) -> tuple[str, ...]:
    """Resolve AWC's public address without trusting synthetic local DNS.

    The desktop VPN can synthesize a ``198.18.0.0/15`` answer even when its
    HTTP proxy is unavailable.  Therefore the resolver must run in both
    transport modes: through the explicit research proxy when reachable, or
    directly with ``noproxy=*`` otherwise.  The result is only a bounded
    routing hint; the request still uses the official hostname for Host/SNI
    and certificate verification.
    """

    def quoted(value: str) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    for resolver_url in AWC_DOH_ENDPOINTS:
        config = "\n".join([
            f"url = {quoted(resolver_url)}",
            f"proxy = {quoted(proxy_url)}" if proxy_url else 'noproxy = "*"',
            "location",
            "fail",
            "silent",
            "show-error",
            'header = "accept: application/dns-json"',
            "connect-timeout = 3",
            f"max-time = {max(5, min(12, int(timeout_sec)))}",
            f"max-filesize = {MAX_DOH_RESPONSE_BYTES}",
            "",
        ])
        try:
            result = subprocess.run(
                ["/usr/bin/curl", "--config", "-"],
                input=config,
                text=True,
                capture_output=True,
                timeout=max(8, min(20, int(timeout_sec) + 5)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        raw = result.stdout.encode("utf-8")
        if len(raw) > MAX_DOH_RESPONSE_BYTES:
            continue
        try:
            addresses = _parse_doh_ipv4_addresses(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if addresses:
            return tuple(f"{AWC_HOST}:443:{address}:443" for address in addresses)
    return ()


def _curl_json(url: str, *, timeout: int = 30) -> Any:
    # URL only; the bounded response is parsed in memory and never retained.
    proxy = _active_proxy_url()

    def quoted(value: str) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    config_lines = [
        f"url = {quoted(url)}",
        f"proxy = {quoted(proxy)}" if proxy else 'noproxy = "*"',
        "location",
        "compressed",
        "fail",
        "silent",
        "show-error",
        "connect-timeout = 5",
        f"max-time = {max(5, int(timeout))}",
        f"max-filesize = {MAX_RESPONSE_BYTES}",
    ]
    config_lines.extend(f"connect-to = {quoted(value)}" for value in _AWC_CONNECT_TO)
    config_lines.append("")
    config = "\n".join(config_lines)
    try:
        max_attempts = max(
            1,
            min(3, int(os.environ.get("PM_TARGET_AWC_FETCH_MAX_ATTEMPTS", str(DEFAULT_AWC_FETCH_ATTEMPTS)))),
        )
    except (TypeError, ValueError):
        max_attempts = DEFAULT_AWC_FETCH_ATTEMPTS
    last_error = "awc_official_fetch_failed"
    for attempt in range(max_attempts):
        result = subprocess.run(
            ["/usr/bin/curl", "--config", "-"],
            input=config,
            text=True,
            capture_output=True,
            timeout=max(8, timeout + 5),
            check=False,
        )
        if result.returncode == 0:
            raw = result.stdout.encode("utf-8")
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("awc_official_response_too_large")
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError("awc_official_response_invalid_json") from exc
        last_error = f"awc_official_fetch_failed_rc_{result.returncode}"
        if attempt + 1 < max_attempts:
            time.sleep(min(10.0, float(2**attempt)))
    raise RuntimeError(last_error)


def _amsc_fetch(
    station: str,
    event_date: str,
    token: str,
    received_at: str,
    *,
    target_timezone: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    start, end = _settlement_window(event_date, target_timezone)
    # AMSC's message-retrieval endpoint is aligned to complete station-local
    # days and accepts the corresponding UTC wall-clock values.  The target
    # Query every physical station-local day touched by the settlement window,
    # then filter returned reports back to that exact interval.
    first_local_day = start.astimezone(AMSC_LOCAL).date()
    last_local_day = (end - timedelta(seconds=1)).astimezone(AMSC_LOCAL).date()
    physical_days: list[Any] = []
    current_day = first_local_day
    while current_day <= last_local_day:
        physical_days.append(current_day)
        current_day += timedelta(days=1)

    payload_rows: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    query_receipts: list[dict[str, Any]] = []
    for physical_day in physical_days:
        local_midnight = datetime.combine(physical_day, datetime.min.time(), tzinfo=AMSC_LOCAL)
        query_start = local_midnight.astimezone(AMSC_UTC)
        query_end = query_start + timedelta(days=1) - timedelta(seconds=1)
        params = {
            "isCCCC": "cccc",
            "cccc": station,
            "starDate": query_start.strftime("%Y-%m-%d %H:%M:%S"),
            "endDate": query_end.strftime("%Y-%m-%d %H:%M:%S"),
            "tt": "SA,SP,",
            "token": token,
        }
        url = AMSC_ENDPOINT + "?" + urlencode(params)
        payload = _curl_tls_verified_payload(url=url, token=token, timeout=30)
        sanitized_payload = {k: v for k, v in payload.items() if k != "token"}
        response_sha = _sha_bytes(_canonical_bytes(sanitized_payload))
        raw_data = payload.get("data")
        response_code = payload.get("code")
        if response_code is None:
            response_code = payload.get("errCode")
        response_message = payload.get("msg")
        if response_message is None:
            response_message = payload.get("errMsg")
        query_receipts.append({
            "physical_station_date": physical_day.isoformat(),
            "response_sha256": response_sha,
            "url": url.split("&token=", 1)[0],
            "response_code": response_code,
            "response_message": str(response_message or "")[:160],
            "response_data_count": len(raw_data) if isinstance(raw_data, list) else None,
            "response_data_type": type(raw_data).__name__,
        })
        payload_rows.append((payload, response_sha, query_receipts[-1]))

    response_sha = _sha_bytes(_canonical_bytes([item[1] for item in payload_rows]))
    records: list[dict[str, Any]] = []
    reference_records: list[dict[str, Any]] = []
    for payload, payload_sha, _query_receipt in payload_rows:
        for row in payload.get("data") or []:
            if not isinstance(row, Mapping):
                continue
            raw_text = str(row.get("content") or "").strip()
            try:
                valid_at = _parse_utc(str(row.get("oDateTime") or "").replace(" ", "T") + "+00:00")
            except ValueError:
                continue
            parsed = _parse_metar(station, raw_text, valid_at)
            if parsed is None or not start <= valid_at < end:
                continue
            report_type, valid_at, temperature = parsed
            raw_sha = raw_text_sha256(raw_text)
            record = {
                "station_id": station,
                "source_lane": "amsc",
                "report_type": report_type,
                "valid_at_utc": _iso(valid_at),
                "reported_temperature_c": temperature,
                "raw_sha256": raw_sha,
                "raw_text": raw_text,
                "source_poll_id": f"amsc-historical-{payload_sha}",
                "first_seen_at_utc": received_at,
                "canonical_committed_at_utc": received_at,
                "source_url": AMSC_ENDPOINT,
                "historical_data_class": "H",
            }
            reference_reason = reference_only_reason(
                source_lane="amsc",
                report_type=report_type,
                valid_at=valid_at,
                expected_lane="amsc",
            )
            if reference_reason:
                reference_records.append(_as_reference(record, reference_reason))
            else:
                records.append(record)
    response_codes = [item.get("response_code") for item in query_receipts]
    response_messages = [str(item.get("response_message") or "") for item in query_receipts]
    response_data_count = sum(int(item.get("response_data_count") or 0) for item in query_receipts)
    return records, reference_records, {
        "station_id": station,
        "event_date": event_date,
        "response_sha256": response_sha,
        "query_physical_station_days": [item["physical_station_date"] for item in query_receipts],
        "query_receipts": query_receipts,
        "url": query_receipts[0]["url"] if query_receipts else "",
        "returned": len(records) + len(reference_records),
        "formal_returned": len(records),
        "reference_returned": len(reference_records),
        "response_code": response_codes[0] if len(set(map(str, response_codes))) == 1 and response_codes else None,
        "response_codes": response_codes,
        "response_message": response_messages[0] if len(set(response_messages)) == 1 and response_messages else "",
        "response_data_count": response_data_count,
        "response_data_type": "list",
    }


def _awc_fetch(
    stations: list[str],
    event_dates: list[str],
    fetched_at: str,
    *,
    reference_only: bool = False,
    target_timezone: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    query_blockers: list[dict[str, str]] = []
    wanted = set(stations)
    windows = {
        day: _settlement_window(day, target_timezone)
        for day in event_dates
    }
    last_query_at = 0.0
    try:
        timeout_sec = max(
            5,
            min(60, int(os.environ.get("PM_TARGET_AWC_FETCH_TIMEOUT_SEC", str(DEFAULT_AWC_FETCH_TIMEOUT_SEC)))),
        )
    except (TypeError, ValueError):
        timeout_sec = DEFAULT_AWC_FETCH_TIMEOUT_SEC
    try:
        max_attempts = max(
            1,
            min(3, int(os.environ.get("PM_TARGET_AWC_FETCH_MAX_ATTEMPTS", str(DEFAULT_AWC_FETCH_ATTEMPTS)))),
        )
    except (TypeError, ValueError):
        max_attempts = DEFAULT_AWC_FETCH_ATTEMPTS
    try:
        batch_budget_sec = max(
            5.0,
            min(900.0, float(os.environ.get("PM_TARGET_AWC_BATCH_BUDGET_SEC", str(DEFAULT_AWC_BATCH_BUDGET_SEC)))),
        )
    except (TypeError, ValueError):
        batch_budget_sec = DEFAULT_AWC_BATCH_BUDGET_SEC
    batch_deadline = time.monotonic() + batch_budget_sec
    budget_exhausted = False
    global _AWC_CONNECT_TO
    _AWC_CONNECT_TO = _resolve_awc_connect_to(
        _active_proxy_url(),
        timeout_sec=timeout_sec,
    )
    station_chunks = [stations[index:index + AWC_QUERY_CHUNK] for index in range(0, len(stations), AWC_QUERY_CHUNK)]
    for event_date in event_dates:
        _start, end = windows[event_date]
        query_date = end.strftime("%Y%m%d_%H%M")
        for chunk in station_chunks:
            if time.monotonic() >= batch_deadline:
                query_blockers.append({
                    "event_date": event_date,
                    "stations": ",".join(chunk),
                    "reason": "batch_budget_exceeded",
                })
                budget_exhausted = True
                break
            wait = AWC_MIN_INTERVAL_SEC - (time.monotonic() - last_query_at)
            if wait > 0:
                time.sleep(wait)
            url = "https://aviationweather.gov/api/data/metar?" + urlencode({
                "ids": ",".join(chunk),
                "format": "json",
                "date": query_date,
                "hours": "24",
            })
            last_query_at = time.monotonic()
            try:
                payload = _curl_json(url, timeout=timeout_sec)
            except Exception as exc:
                failure = f"{type(exc).__name__}:{str(exc)[:160]}"
                query_blockers.append({
                    "event_date": event_date,
                    "stations": ",".join(chunk),
                    "reason": f"fetch:{failure}",
                })
                receipts.append({
                    "event_date": event_date,
                    "stations": chunk,
                    "source_url": url,
                    "blocked": True,
                    "reason": f"fetch:{failure}",
                })
                continue
            payload_bytes = _canonical_bytes(payload)
            response_sha = _sha_bytes(payload_bytes)
            receipts.append({
                "event_date": event_date,
                "stations": chunk,
                "response_sha256": response_sha,
                "source_url": url,
                "returned_rows": len(payload) if isinstance(payload, list) else 0,
            })
            for row in payload if isinstance(payload, list) else []:
                if not isinstance(row, Mapping):
                    continue
                station = str(row.get("icaoId") or "").strip().upper()
                if station not in wanted or row.get("temp") is None:
                    continue
                raw_text = str(row.get("rawOb") or "").strip()
                try:
                    valid_at = datetime.fromtimestamp(float(row.get("obsTime")), tz=timezone.utc) if row.get("obsTime") is not None else _parse_utc(row.get("reportTime"))
                    temperature = float(row.get("temp"))
                except (TypeError, ValueError, OverflowError):
                    continue
                if not raw_text or temperature != temperature or not windows[event_date][0] <= valid_at < windows[event_date][1]:
                    continue
                report_type = str(row.get("metarType") or "METAR").strip().upper()
                if report_type not in {"METAR", "SPECI"}:
                    continue
                raw_sha = _sha_bytes(raw_text.encode("utf-8"))
                record = {
                    "station_id": station,
                    "source_lane": "awc",
                    "report_type": report_type,
                    "valid_at_utc": _iso(valid_at),
                    "reported_temperature_c": temperature,
                    "raw_sha256": raw_sha,
                    "raw_text": raw_text,
                    "source_poll_id": f"awc-historical-{response_sha}",
                    "first_seen_at_utc": fetched_at,
                    "canonical_committed_at_utc": fetched_at,
                    "source_url": url,
                    "historical_data_class": "H",
                }
                if reference_only:
                    reference_reason = reference_only_reason(
                        source_lane="awc",
                        report_type=report_type,
                        valid_at=valid_at,
                        expected_lane="amsc",
                    )
                    if not reference_reason:
                        continue
                    records.append(_as_reference(record, reference_reason))
                else:
                    records.append(record)
        if budget_exhausted:
            break
    return records, {
        "query_count": len(receipts),
        "queries": receipts,
        "query_blockers": query_blockers,
        "returned": len(records),
        "reference_only": reference_only,
        "transport_bounds": {
            "timeout_sec": timeout_sec,
            "max_attempts": max_attempts,
            "batch_budget_sec": batch_budget_sec,
            "budget_exhausted": budget_exhausted,
            "connect_to": list(_AWC_CONNECT_TO),
            "address_resolution": "bounded_doh_connect_to" if _AWC_CONNECT_TO else "proxy_or_direct_dns",
        },
    }


def _load_existing_amsc(
    stations: set[str],
    start_date: str,
    end_date: str,
    *,
    target_timezone: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # The target research archive is the smallest retained source of truth for
    # historical reconstruction.  It contains both AMSC and AWC objects, so
    # its explicit source_lane must be honored; the older AMSC-only archives
    # predate that field and remain trusted by their scope.
    roots = [
        (ROOT / "data/research/bayes_basket_target/historical_truth_raw/objects", True),
        (ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/raw/amsc_china_station_history/objects", False),
        (ROOT / "data/amsc_raw_history/objects", False),
    ]
    result: list[dict[str, Any]] = []
    reference_result: list[dict[str, Any]] = []
    for root, require_explicit_amsc_lane in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.json.gz"):
            assert_process_memory_budget()
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    value = json.load(handle)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            station = str(value.get("station_id") or "").strip().upper()
            if station not in stations:
                continue
            source_lane = str(value.get("source_lane") or "").strip().lower()
            if require_explicit_amsc_lane and source_lane != "amsc":
                # Never reinterpret an AWC object from the mixed research
                # archive as formal AMSC truth.
                continue
            if source_lane and source_lane != "amsc":
                continue
            raw_text = str(value.get("raw_text") or "").strip()
            raw_sha = str(value.get("raw_sha256") or "").strip().lower()
            if len(raw_sha) != 64 or not raw_text:
                continue
            try:
                valid_at = _parse_utc(value.get("verified_valid_time_utc") or value.get("valid_at_utc"))
            except (TypeError, ValueError):
                continue
            if not (
                start_date
                <= valid_at.astimezone(ZoneInfo(target_timezone)).date().isoformat()
                <= end_date
            ):
                continue
            temp = value.get("parsed_observation", {}).get("temperature_c") if isinstance(value.get("parsed_observation"), Mapping) else None
            if temp is None:
                match = TEMP_RE.search(raw_text.replace("\n", " "))
                temp = _signed_temp(match.group(1)) if match else None
            if temp is None:
                continue
            report_type = str(value.get("report_type") or raw_text.split(None, 1)[0] or "METAR").strip().upper()
            record = {
                "station_id": station,
                "source_lane": "amsc",
                "report_type": report_type,
                "valid_at_utc": _iso(valid_at),
                "reported_temperature_c": float(temp),
                "raw_sha256": raw_sha,
                "raw_text": raw_text,
                "source_poll_id": str(value.get("first_historical_query_id") or value.get("source_id") or path.name),
                "first_seen_at_utc": str(value.get("first_historical_query_received_at") or value.get("first_seen_at") or value.get("query_received_at") or _now()),
                "canonical_committed_at_utc": str(value.get("first_historical_query_received_at") or value.get("first_seen_at") or _now()),
                "source_url": str(value.get("source_url") or "AMSC historical raw archive"),
                "historical_data_class": "H",
            }
            reference_reason = reference_only_reason(
                source_lane="amsc",
                report_type=report_type,
                valid_at=valid_at,
                expected_lane="amsc",
            )
            if reference_reason:
                reference_result.append(_as_reference(record, reference_reason))
            else:
                result.append(record)
    return result, reference_result


def _load_existing_awc(
    stations: set[str],
    start_date: str,
    end_date: str,
    *,
    reference_only: bool = False,
    target_timezone: str,
) -> list[dict[str, Any]]:
    """Load retained formal AWC objects for non-China historical truth.

    The research raw archive is mixed-source by design.  Only explicit AWC
    objects are eligible here.  Formal mode loads non-reference objects for
    AWC-owned stations; reference mode loads only AWC objects explicitly
    marked ``REFERENCE_ONLY`` for AMSC-owned stations.
    """

    root = ROOT / "data/research/bayes_basket_target/historical_truth_raw/objects"
    result: list[dict[str, Any]] = []
    if not root.exists():
        return result
    for path in root.rglob("*.json.gz"):
        assert_process_memory_budget()
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if str(value.get("source_lane") or "").strip().lower() != "awc":
            continue
        is_reference = value.get("evidence_role") == "REFERENCE_ONLY"
        if is_reference != reference_only:
            continue
        station = str(value.get("station_id") or "").strip().upper()
        if station not in stations:
            continue
        raw_text = str(value.get("raw_text") or "").strip()
        raw_sha = str(value.get("raw_sha256") or "").strip().lower()
        if len(raw_sha) != 64 or not raw_text:
            continue
        valid_at = _parse_utc(value.get("verified_valid_time_utc") or value.get("valid_at_utc"))
        if valid_at is None or not (
            start_date
            <= valid_at.astimezone(ZoneInfo(target_timezone)).date().isoformat()
            <= end_date
        ):
            continue
        # Retained AWC objects already carry the provider's parsed precision
        # (including tenths from the RMK T-group).  Prefer that value over
        # reparsing the rounded ``TT/DD`` body group; otherwise a local
        # rebuild silently changes official truth such as 31.1C to 31.0C.
        temp = value.get("reported_temperature_c")
        if temp is None:
            temp = value.get("parsed_observation", {}).get("temperature_c") if isinstance(value.get("parsed_observation"), Mapping) else None
        if temp is None:
            match = TEMP_RE.search(raw_text.replace("\n", " "))
            temp = _signed_temp(match.group(1)) if match else None
        try:
            temp = float(temp)
        except (TypeError, ValueError):
            temp = None
        if temp is None or not math.isfinite(temp):
            continue
        report_type = str(value.get("report_type") or raw_text.split(None, 1)[0] or "METAR").strip().upper()
        record = {
            "station_id": station,
            "source_lane": "awc",
            "report_type": report_type,
            "valid_at_utc": _iso(valid_at),
            "reported_temperature_c": temp,
            "raw_sha256": raw_sha,
            "raw_text": raw_text,
            "source_poll_id": str(value.get("source_poll_id") or value.get("source_id") or path.name),
            "first_seen_at_utc": str(value.get("first_seen_at_utc") or value.get("first_historical_query_received_at") or _now()),
            "canonical_committed_at_utc": str(value.get("canonical_committed_at_utc") or value.get("first_seen_at_utc") or _now()),
            "source_url": str(value.get("source_url") or "AWC historical raw archive"),
            "historical_data_class": "H",
        }
        if reference_only:
            # Re-derive the policy label from the source lane and timestamp;
            # retained research packets may carry a pre-SSOT reason string.
            reason = reference_only_reason(
                source_lane="awc",
                report_type=report_type,
                valid_at=valid_at,
                expected_lane="amsc",
            )
            if not reason:
                continue
            result.append(_as_reference(record, reason))
        else:
            result.append(record)
    return result


def rebuild(
    *,
    lane_map_path: Path,
    event_dates: list[str],
    output_path: Path,
    observations_path: Path | None,
    reference_output_path: Path,
    raw_root: Path,
    target_timezone: str,
    fetch_amsc: bool = True,
    fetch_awc: bool = True,
    fetch_formal_awc: bool = True,
    awc_reference_stations: list[str] | None = None,
    station_ids: list[str] | None = None,
) -> dict[str, Any]:
    lane_map = _stations(lane_map_path)
    event_dates = _dates(event_dates)
    generated_at = _now()
    # Historical backfill may collect the current settlement day before its
    # boundary closes. The packet is explicitly marked pending and the history
    # builder will exclude that day from exact joins/calibration until the
    # boundary closes.  Future days remain rejected at the started-day gate.
    event_day_windows = [
        assert_event_day_started(
            event_date,
            as_of_utc=generated_at,
            target_timezone=target_timezone,
        )
        for event_date in event_dates
    ]
    pending_event_dates = sorted(
        item["event_date"] for item in event_day_windows if item["state"] != "closed"
    )
    requested_stations = sorted({str(item).strip().upper() for item in (station_ids or []) if str(item).strip()})
    if set(requested_stations) - set(lane_map):
        raise ValueError("station_ids_not_in_target_registry")
    scope = set(requested_stations) if requested_stations else set(lane_map)
    amsc_stations = sorted(station for station, lane in lane_map.items() if lane == "amsc" and station in scope)
    awc_stations = sorted(station for station, lane in lane_map.items() if lane == "awc" and station in scope)
    reference_stations = sorted(set(awc_reference_stations or amsc_stations))
    if set(reference_stations) - set(amsc_stations):
        raise ValueError("awc_reference_station_must_be_amsc_station")
    rows: list[dict[str, Any]] = []
    reference_rows: list[dict[str, Any]] = []
    fetch_receipts: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []

    existing_rows, existing_reference_rows = _load_existing_amsc(
        set(amsc_stations),
        event_dates[0],
        event_dates[-1],
        target_timezone=target_timezone,
    )
    rows.extend(existing_rows)
    reference_rows.extend(existing_reference_rows)
    rows.extend(_load_existing_awc(
        set(awc_stations),
        event_dates[0],
        event_dates[-1],
        target_timezone=target_timezone,
    ))
    reference_rows.extend(_load_existing_awc(
        set(amsc_stations),
        event_dates[0],
        event_dates[-1],
        reference_only=True,
        target_timezone=target_timezone,
    ))
    if fetch_amsc:
        tokens = token_candidates()
        if not tokens:
            blockers.append({"source_lane": "amsc", "reason": "amsc_session_unavailable"})
        else:
            for event_date in event_dates:
                for station in amsc_stations:
                    try:
                        fetched, reference_fetched, receipt = _amsc_fetch(
                            station,
                            event_date,
                            tokens[0],
                            generated_at,
                            target_timezone=target_timezone,
                        )
                        rows.extend(fetched)
                        reference_rows.extend(reference_fetched)
                        fetch_receipts.append({"source_lane": "amsc", **receipt})
                    except Exception as exc:
                        blockers.append({"source_lane": "amsc", "station_id": station, "event_date": event_date, "reason": f"fetch:{type(exc).__name__}"})
    if fetch_awc:
        if fetch_formal_awc and awc_stations:
            try:
                fetched, receipt = _awc_fetch(
                    awc_stations,
                    event_dates,
                    generated_at,
                    target_timezone=target_timezone,
                )
                rows.extend(fetched)
                fetch_receipts.append({"source_lane": "awc", "purpose": "formal_truth", **receipt})
                blockers.extend({"source_lane": "awc", **item} for item in receipt.get("query_blockers") or [])
            except Exception as exc:
                blockers.append({"source_lane": "awc", "purpose": "formal_truth", "reason": f"fetch:{type(exc).__name__}"})
        if reference_stations:
            try:
                fetched, receipt = _awc_fetch(
                    reference_stations,
                    event_dates,
                    generated_at,
                    reference_only=True,
                    target_timezone=target_timezone,
                )
                reference_rows.extend(fetched)
                fetch_receipts.append({"source_lane": "awc", "purpose": "reference_only", **receipt})
                blockers.extend({"source_lane": "awc", "purpose": "reference_only", **item} for item in receipt.get("query_blockers") or [])
            except Exception as exc:
                blockers.append({"source_lane": "awc", "purpose": "reference_only", "reason": f"fetch:{type(exc).__name__}"})

    # Deduplicate exact raw reports; a same-slot conflicting hash remains in
    # the evidence and is quarantined by the settlement-history builder instead of silently
    # choosing one revision.
    by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in [*rows, *reference_rows]:
        key = (row["station_id"], row["source_lane"], row["report_type"], row["raw_sha256"])
        by_key.setdefault(key, row)
    all_rows = sorted(by_key.values(), key=lambda x: (x["station_id"], x["valid_at_utc"], x["report_type"], x["raw_sha256"]))
    rows = [row for row in all_rows if row.get("evidence_role") != "REFERENCE_ONLY"]
    reference_rows = [row for row in all_rows if row.get("evidence_role") == "REFERENCE_ONLY"]
    existing_raw_bytes = (
        sum(path.stat().st_size for path in (raw_root / "objects").rglob("*.json.gz"))
        if (raw_root / "objects").exists()
        else 0
    )
    projected_raw_bytes = [existing_raw_bytes]
    for row in all_rows:
        _store_raw(raw_root, row["raw_sha256"], row, projected_bytes=projected_raw_bytes)

    observations = [{
        "station_id": row["station_id"],
        "source_lane": row["source_lane"],
        "report_type": row["report_type"],
        "valid_at_utc": row["valid_at_utc"],
        "reported_temperature_c": row["reported_temperature_c"],
        "raw_sha256": row["raw_sha256"],
        "source_poll_id": row["source_poll_id"],
        "first_seen_at_utc": row["first_seen_at_utc"],
        "canonical_committed_at_utc": row["canonical_committed_at_utc"],
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    } for row in rows]
    observation_evidence = {
        "schema": OBSERVATION_SCHEMA,
        "target_timezone": target_timezone,
        "historical_only": True,
        "canonical_ingress": True,
        "generated_at_utc": generated_at,
        "observations": observations,
        "observation_count": len(observations),
        "boundary": {"writes_production_db": False, "calls_order_gateway": False, "places_orders": False, "live_arrival_evidence": False},
    }
    observation_evidence["evidence_sha256"] = content_sha256(observation_evidence)
    if observations_path is not None:
        write_bounded_json(observations_path, observation_evidence, limit=MAX_JSON_OUTPUT_BYTES)

    reference_observations = [{
        "station_id": row["station_id"],
        "source_lane": row["source_lane"],
        "report_type": row["report_type"],
        "valid_at_utc": row["valid_at_utc"],
        "reported_temperature_c": row["reported_temperature_c"],
        "raw_sha256": row["raw_sha256"],
        "source_poll_id": row["source_poll_id"],
        "first_seen_at_utc": row["first_seen_at_utc"],
        "canonical_committed_at_utc": row["canonical_committed_at_utc"],
        "evidence_role": "REFERENCE_ONLY",
        "reference_reason": row["reference_reason"],
    } for row in reference_rows]
    reference_evidence = {
        "schema": REFERENCE_SCHEMA,
        "target_timezone": target_timezone,
        "historical_only": True,
        "calibration_eligible": False,
        "formal_truth_fallback_allowed": False,
        "formal_truth_priority": {
            "amsc_hourly_metar_for_amsc_station": "primary",
            "amsc_half_hour_metar": "reference_only",
            "awc_hourly_for_amsc_station": "reference_only",
            "awc_hourly_precedence_over_amsc_hourly": False,
        },
        "generated_at_utc": generated_at,
        "reference_observations": reference_observations,
        "reference_observation_count": len(reference_observations),
        "source_counts": {
            "amsc_half_hour": sum(1 for row in reference_rows if row["reference_reason"] == AMSC_HALF_HOUR_REFERENCE),
            "awc_hourly": sum(1 for row in reference_rows if row["reference_reason"] == AWC_HOUR_REFERENCE_FOR_AMSC),
        },
        "boundary": {
            "reference_only": True,
            "writes_formal_observation_evidence": False,
            "enters_settlement_join": False,
            "enters_calibration": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    reference_evidence["evidence_sha256"] = content_sha256(reference_evidence)
    write_bounded_json(reference_output_path, reference_evidence, limit=MAX_JSON_OUTPUT_BYTES)

    # Station-day coverage follows the registry settlement timezone, not a
    # UTC date or the ET operations/reporting boundary. A local day can cross
    # UTC midnight, so a raw UTC slice can falsely split one truth day
    # into two and make the completeness receipt look better or worse than
    # the exact join actually sees.
    station_day = {
        (
            row["station_id"],
            _parse_utc(row["valid_at_utc"])
            .astimezone(ZoneInfo(target_timezone))
            .date()
            .isoformat(),
        )
        for row in rows
    }
    identity = {
        "schema": SCHEMA,
        "target_timezone": target_timezone,
        "event_dates": event_dates,
        "event_day_windows": event_day_windows,
        "pending_event_dates": pending_event_dates,
        "state": "PARTIAL_PENDING_SETTLEMENT_CLOSE" if pending_event_dates else "CLOSED_HISTORICAL",
        "station_lane_map": lane_map,
        "generated_at_utc": generated_at,
        "observation_count": len(observations),
        "station_day_count": len(station_day),
        "station_day_keys": [f"{s}|{d}" for s, d in sorted(station_day)],
        "source_counts": {"amsc": sum(1 for row in rows if row["source_lane"] == "amsc"), "awc": sum(1 for row in rows if row["source_lane"] == "awc")},
        "reference_observation_count": len(reference_rows),
        "reference_source_counts": {
            "amsc_half_hour": sum(1 for row in reference_rows if row["reference_reason"] == AMSC_HALF_HOUR_REFERENCE),
            "awc_hourly": sum(1 for row in reference_rows if row["reference_reason"] == AWC_HOUR_REFERENCE_FOR_AMSC),
        },
        "fetch_receipts": fetch_receipts,
        "blockers": blockers,
        "observation_evidence_path": str(observations_path.resolve()) if observations_path is not None else None,
        "reference_evidence_path": str(reference_output_path.resolve()),
        "raw_root": str(raw_root.resolve()),
        "boundary": {
            "historical_only": True,
            "open_event_days_may_be_collected": True,
            "open_event_days_final_truth_frozen": False,
            "open_event_days_calibration_eligible": False,
            "live_arrival_evidence": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    result = {**identity, "evidence_sha256": content_sha256(identity)}
    result["receipt_sha256"] = content_sha256(result)
    write_bounded_json(output_path, result, limit=MAX_JSON_OUTPUT_BYTES)
    return result


def _result_exit_code(result: Mapping[str, Any], *, reference_only: bool) -> int:
    """Return success for the evidence class explicitly requested by the CLI."""

    count_key = "reference_observation_count" if reference_only else "observation_count"
    return 0 if int(result.get(count_key) or 0) > 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument("--timezone-registry", type=Path, default=DEFAULT_TIMEZONE_REGISTRY)
    parser.add_argument("--event-date", action="append", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--observations-output", type=Path, default=DEFAULT_OBSERVATIONS)
    parser.add_argument("--reference-output", type=Path, default=DEFAULT_REFERENCE_OUTPUT)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--proxy-config", type=Path, default=DEFAULT_PROXY_CONFIG)
    parser.add_argument("--proxy-url", default="", help="Explicit isolated research proxy; does not modify shared production env.")
    parser.add_argument(
        "--transport",
        choices=("configured_proxy", "direct"),
        default="configured_proxy",
        help="Use the isolated proxy config or an explicit direct connection.",
    )
    parser.add_argument("--reference-only", action="store_true", help="Write only reference evidence and its rebuild receipt; do not retain a duplicate formal observations file.")
    parser.add_argument("--awc-reference-station", action="append", default=[], help="Limit AWC hourly reference fetch to this AMSC-lane station; repeatable.")
    parser.add_argument("--station-id", action="append", default=[], help="Limit this isolated rebuild to registered stations; repeatable.")
    parser.add_argument("--skip-amsc-fetch", action="store_true", help="Reuse existing bounded AMSC raw objects and fetch only AWC.")
    parser.add_argument("--skip-awc-fetch", action="store_true", help="Fetch only AMSC and reuse the isolated AWC packets separately.")
    parser.add_argument("--skip-formal-awc-fetch", action="store_true", help="Do not fetch formal AWC-lane truth; reference fetch remains enabled.")
    args = parser.parse_args()
    import os
    global _RESEARCH_DIRECT
    _RESEARCH_DIRECT = args.transport == "direct"
    # A local rebuild from retained, content-addressed raw objects must remain
    # runnable when the research proxy is offline.  Resolve a proxy only when
    # at least one source fetch is requested; otherwise network availability
    # is unrelated to the operation and must not block it.
    needs_network = (not args.skip_amsc_fetch) or (not args.skip_awc_fetch)
    research_proxy = (
        ""
        if _RESEARCH_DIRECT
        else _research_proxy_url(args.proxy_config, args.proxy_url)
        if needs_network
        else ""
    )
    if research_proxy:
        os.environ["PM_OFFICIAL_NWP_PROXY"] = research_proxy
    event_dates = _dates(args.event_date)
    lane_map = _stations(args.station_lane_map)
    requested_stations = sorted({
        str(item).strip().upper()
        for item in args.station_id
        if str(item).strip()
    })
    station_ids = requested_stations or sorted(lane_map)
    result = rebuild(
        lane_map_path=args.station_lane_map,
        event_dates=event_dates,
        output_path=args.output,
        observations_path=None if args.reference_only else args.observations_output,
        reference_output_path=args.reference_output,
        raw_root=args.raw_root,
        target_timezone=_registry_timezone(
            args.timezone_registry,
            station_ids=station_ids,
            event_dates=event_dates,
        ),
        fetch_amsc=not args.skip_amsc_fetch,
        fetch_awc=not args.skip_awc_fetch,
        fetch_formal_awc=not args.skip_formal_awc_fetch,
        awc_reference_stations=[str(item).strip().upper() for item in args.awc_reference_station if str(item).strip()],
        station_ids=station_ids,
    )
    print(json.dumps({"schema": result["schema"], "research_proxy_url": research_proxy or "direct", "observation_count": result["observation_count"], "reference_observation_count": result["reference_observation_count"], "station_day_count": result["station_day_count"], "source_counts": result["source_counts"], "reference_source_counts": result["reference_source_counts"], "blocker_count": len(result["blockers"]), "output": str(args.output.resolve()), "observations_output": str(args.observations_output.resolve()), "reference_output": str(args.reference_output.resolve())}, ensure_ascii=False, sort_keys=True))
    return _result_exit_code(result, reference_only=args.reference_only)


if __name__ == "__main__":
    raise SystemExit(main())
