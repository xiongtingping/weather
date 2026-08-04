#!/usr/bin/env python3
"""Capture direct native-provider model arrival evidence prospectively.

The command is target-only and research-only.  It records the provider's
HTTP ``Last-Modified`` metadata as the publication/issue timestamp and the
completed local response time as receipt time.  If the provider does not
expose that metadata, the capture is BLOCKED; no filename, mtime, run time,
or retrospective request time is substituted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_RESEARCH_RAW_BYTES,
    assert_target_raw_retention_budget,
    load_bounded_json,
)
from bayes_basket_target.model_arrival import (  # noqa: E402
    ALLOWED_PROVIDERS,
    build_blocked_receipt,
    validate_model_arrival_capture,
    validate_prospective_capture_window,
)
from bayes_basket_target.model_run_contract import validate_capture_request  # noqa: E402
from official_nwp_https_transport import open_official_https, response_read_deadline  # noqa: E402


ALLOWED_HOSTS = {
    # ECMWF publishes the same native Open Data object through its origin and
    # official cloud replicas.  Keep all accepted hosts explicit: this is
    # transport redundancy for the same provider, never a republisher or an
    # aggregator fallback.
    "ecmwf_ifs025_official": {
        "data.ecmwf.int",
        "ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
        "storage.googleapis.com",
        "ai4edataeuwest.blob.core.windows.net",
    },
    "ecmwf_aifs025_official": {
        "data.ecmwf.int",
        "ecmwf-forecasts.s3.amazonaws.com",
        "ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
        "storage.googleapis.com",
        "ai4edataeuwest.blob.core.windows.net",
    },
    # NOMADS serves the bounded filtered payload.  NOAA's public S3 PDS is
    # accepted only so --publication-url can HEAD the same native object and
    # bind its Last-Modified timestamp; both hosts remain official NOAA
    # endpoints and credentials/query secrets are rejected below.
    "gfs_0p25_official": {
        "nomads.ncep.noaa.gov",
        "noaa-gfs-bdp-pds.s3.amazonaws.com",
    },
    "icon_global_official": {"opendata.dwd.de"},
}
DEFAULT_ROOT = ROOT / "data" / "research" / "bayes_basket_target" / "model_arrivals"
MIB = 1024 * 1024
GIB = 1024 * MIB
# The target collector must remain bounded even when a provider advertises a
# very large object or a proxy drips bytes indefinitely.  ECMWF's current
# single-object capture is well below this ceiling.
DEFAULT_MAX_RESPONSE_BYTES = 256 * MIB
DEFAULT_MAX_CAPTURE_BYTES = 512 * MIB
DEFAULT_MIN_FREE_BYTES = 512 * MIB
MAX_ALLOWED_RESPONSE_BYTES = 512 * MIB
MAX_ALLOWED_CAPTURE_BYTES = 1 * GIB
MAX_RETAINED_RAW_BYTES = min(512 * MIB, MAX_TARGET_RESEARCH_RAW_BYTES // 2)
# A provider-local limit is not enough: the target lane has one aggregate
# research budget so three providers cannot fill the host independently.
MAX_AGGREGATE_RETAINED_RAW_BYTES = MAX_TARGET_RESEARCH_RAW_BYTES
MAX_URLS_PER_CAPTURE = 32
STREAM_CHUNK_BYTES = 1024 * 1024
STALE_TEMP_GRACE_SEC = 60.0
UNREFERENCED_RAW_GRACE_SEC = 3600.0
# DWD's rolling ICON tree can return a slow/empty GET for a file that is not
# published yet.  A bounded HEAD is enough to distinguish that state before
# opening a multi-megabyte response body.  Keep this lower than the payload
# timeout so one unavailable forecast hour cannot stall a whole run.
ICON_AVAILABILITY_PREFLIGHT_TIMEOUT_SEC = 10.0


def _temp_pid(name: str) -> int | None:
    """Extract a PID only from the collector's known temp-name forms."""

    patterns = (
        r"\.tmp\.(\d+)$",          # object.json.tmp.<pid>
        r"\.(\d+)\.tmp$",          # .object.json.<pid>.tmp
        r"(?:^|[.\-])(?:partial|capture)[.\-](\d+)(?:[.\-]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, name)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                return None
    return None


def _cleanup_stale_capture_temps(
    output_root: Path,
    provider_id: str,
    *,
    now: float | None = None,
    grace_sec: float = STALE_TEMP_GRACE_SEC,
) -> int:
    """Remove only old, PID-identifiable crash remnants in target paths.

    The cleanup is deliberately scoped to this collector's raw/receipt
    directories.  A committed content-addressed object, an unrecognised
    filename, a recent file, or a file owned by a live/indeterminate PID is
    retained.  This keeps a killed capture from accumulating disk residue
    without touching production data or another process's work.
    """

    root = Path(output_root) / provider_id
    directories = (root / "raw", root / "receipts")
    current_pid = os.getpid()
    cutoff = float(time.time() if now is None else now) - max(0.0, float(grace_sec))
    removed = 0
    for directory in directories:
        if not directory.is_dir():
            continue
        try:
            paths = tuple(directory.iterdir())
        except OSError:
            continue
        for path in paths:
            if not path.is_file() or path.is_symlink():
                continue
            pid = _temp_pid(path.name)
            if pid is None or pid == current_pid:
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                try:
                    path.unlink()
                except OSError:
                    continue
                removed += 1
            except PermissionError:
                continue
            except OSError:
                # An indeterminate PID probe is fail-closed.
                continue
    return removed


def _receipt_raw_references(receipts_dir: Path) -> tuple[set[str], set[Path]]:
    """Read only local receipt lineage used to protect raw objects."""

    hashes: set[str] = set()
    paths: set[Path] = set()
    if not receipts_dir.is_dir():
        return hashes, paths
    for receipt_path in receipts_dir.glob("*.json"):
        try:
            value = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        stack: list[Any] = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in {"raw_sha256", "raw_payload_sha256"} and isinstance(child, str):
                        candidate = child.strip().lower()
                        if re.fullmatch(r"[0-9a-f]{64}", candidate):
                            hashes.add(candidate)
                    elif key == "raw_object_ref" and isinstance(child, str) and child.strip():
                        try:
                            paths.add(Path(child).expanduser().resolve())
                        except OSError:
                            continue
                    else:
                        stack.append(child)
            elif isinstance(item, list):
                stack.extend(item)
    return hashes, paths


def _prune_unreferenced_raw(
    output_root: Path,
    provider_id: str,
    *,
    now: float | None = None,
    grace_sec: float = UNREFERENCED_RAW_GRACE_SEC,
) -> dict[str, int]:
    """Remove only old content-addressed raw objects absent from receipts."""

    provider_root = Path(output_root) / provider_id
    raw_dir = provider_root / "raw"
    hashes, paths = _receipt_raw_references(provider_root / "receipts")
    cutoff = float(time.time() if now is None else now) - max(0.0, float(grace_sec))
    removed_count = 0
    removed_bytes = 0
    if not raw_dir.is_dir():
        return {"removed_count": 0, "removed_bytes": 0}
    for path in tuple(raw_dir.iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})(?:\.[A-Za-z0-9._-]+)?", path.name)
        if not match:
            continue
        digest = match.group(1).lower()
        try:
            stat = path.stat()
            resolved = path.resolve()
        except OSError:
            continue
        if digest in hashes or resolved in paths or stat.st_mtime > cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed_count += 1
        removed_bytes += int(stat.st_size)
    return {"removed_count": removed_count, "removed_bytes": removed_bytes}


def _ensure_raw_retention_budget(
    output_root: Path,
    provider_id: str,
    *,
    bytes_needed: int,
) -> None:
    """Fail closed before a provider's referenced raw corpus grows unbounded."""

    raw_dir = Path(output_root) / provider_id / "raw"
    retained = 0
    if raw_dir.is_dir():
        for path in raw_dir.iterdir():
            if not path.is_file() or path.is_symlink():
                continue
            try:
                retained += int(path.stat().st_size)
            except OSError:
                continue
    if retained + max(0, int(bytes_needed)) > MAX_RETAINED_RAW_BYTES:
        raise ContractError("model_arrival_raw_retention_budget_exceeded")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_from_last_modified(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError("provider_last_modified_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("provider_last_modified_timezone_missing")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_url(value: str, provider_id: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS[provider_id]:
        raise ContractError("model_arrival_source_url_not_allowlisted")
    sensitive = {"api_key", "apikey", "key", "token", "access_token", "signature", "password", "secret"}
    if any(key.lower() in sensitive for key in (part.split("=", 1)[0] for part in parsed.query.split("&") if part)):
        raise ContractError("model_arrival_source_url_contains_credential")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _bounded_limit(name: str, value: int, *, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result <= 0 or result > maximum:
        raise ContractError(f"{name}_invalid")
    return result


def _icon_availability_preflight(url: str, *, timeout_sec: float) -> dict[str, Any]:
    """Check one official DWD ICON object before attempting a payload GET.

    DWD exposes only a rolling current-run directory.  Requesting a not-yet-
    published cycle or an old date with GET used to spend the full payload
    timeout and produced a long sequence of opaque failed captures.  HEAD is
    metadata-only, has no raw-file side effect, and gives the scheduler a
    fast, explicit availability reason.
    """

    request = Request(
        url,
        method="HEAD",
        headers={"User-Agent": "bayes-basket-target-icon-availability-preflight/1"},
    )
    try:
        with open_official_https(
            request,
            timeout_sec=min(
                ICON_AVAILABILITY_PREFLIGHT_TIMEOUT_SEC,
                max(1.0, float(timeout_sec)),
            ),
        ) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            headers = {
                str(key).lower(): str(value)
                for key, value in (getattr(response, "headers", {}) or {}).items()
            }
    except Exception as exc:
        provider_status = getattr(exc, "code", None)
        if isinstance(provider_status, int):
            raise ContractError(
                f"model_arrival_icon_preflight_http_{provider_status}"
            ) from exc
        raise ContractError(
            f"model_arrival_icon_preflight_unavailable:{type(exc).__name__}"
        ) from exc
    if status_code < 200 or status_code >= 300:
        raise ContractError(f"model_arrival_icon_preflight_http_{status_code}")
    return {
        "method": "HEAD",
        "status_code": status_code,
        "provider_last_modified": headers.get("last-modified", ""),
        "content_length": headers.get("content-length", ""),
    }


def _resource_limits(
    *,
    max_response_bytes: int,
    max_capture_bytes: int,
    min_free_bytes: int,
) -> dict[str, int]:
    response_limit = _bounded_limit(
        "max_response_bytes", max_response_bytes, maximum=MAX_ALLOWED_RESPONSE_BYTES
    )
    capture_limit = _bounded_limit(
        "max_capture_bytes", max_capture_bytes, maximum=MAX_ALLOWED_CAPTURE_BYTES
    )
    free_limit = _bounded_limit("min_free_bytes", min_free_bytes, maximum=MAX_ALLOWED_CAPTURE_BYTES)
    if capture_limit < response_limit:
        raise ContractError("max_capture_bytes_below_max_response_bytes")
    return {
        "max_response_bytes": response_limit,
        "max_capture_bytes": capture_limit,
        "min_free_bytes": free_limit,
    }


def _filesystem_free_bytes(path: Path) -> int:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return int(shutil.disk_usage(candidate).free)


def _ensure_free_space(root: Path, *, bytes_needed: int, min_free_bytes: int) -> None:
    if _filesystem_free_bytes(root) < int(bytes_needed) + int(min_free_bytes):
        raise ContractError("model_arrival_min_free_space_guard")


def _content_length(headers: dict[str, str], *, max_response_bytes: int) -> int | None:
    raw = headers.get("content-length")
    if raw in (None, ""):
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ContractError("provider_content_length_invalid") from exc
    if value < 0:
        raise ContractError("provider_content_length_invalid")
    if value > max_response_bytes:
        raise ContractError("model_arrival_response_too_large")
    return value


def _bounded_byte_range(
    range_start: int | None,
    range_end: int | None,
    *,
    max_response_bytes: int,
) -> tuple[int, int] | None:
    """Validate an optional inclusive HTTP byte range before any request."""

    if range_start is None and range_end is None:
        return None
    if range_start is None or range_end is None:
        raise ContractError("model_arrival_byte_range_invalid")
    try:
        start = int(range_start)
        end = int(range_end)
    except (TypeError, ValueError) as exc:
        raise ContractError("model_arrival_byte_range_invalid") from exc
    if start < 0 or end < start or end - start + 1 > int(max_response_bytes):
        raise ContractError("model_arrival_byte_range_invalid")
    return start, end


def _validate_byte_range_response(
    headers: dict[str, str],
    *,
    status_code: int,
    expected_range: tuple[int, int],
) -> None:
    """Refuse a provider response that does not prove the requested slice."""

    if status_code != 206:
        raise ContractError("model_arrival_byte_range_not_honored")
    raw = str(headers.get("content-range") or "").strip()
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", raw)
    if match is None:
        raise ContractError("provider_content_range_invalid")
    returned_start, returned_end = int(match.group(1)), int(match.group(2))
    if (returned_start, returned_end) != expected_range:
        raise ContractError("provider_content_range_mismatch")
    content_length = headers.get("content-length")
    if content_length not in (None, ""):
        try:
            if int(content_length) != returned_end - returned_start + 1:
                raise ContractError("provider_content_range_length_mismatch")
        except ValueError as exc:
            raise ContractError("provider_content_length_invalid") from exc


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(STREAM_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _stream_response(
    response: Any,
    *,
    destination: Path | None,
    output_root: Path,
    timeout_sec: float,
    max_response_bytes: int,
    max_capture_bytes: int,
    min_free_bytes: int,
    captured_response_bytes: int,
) -> tuple[str, int]:
    """Hash and optionally persist a response without retaining its body.

    The stream is bounded by both wall time and bytes.  A destination is only
    used when the content-addressed raw object is not already present; a
    duplicate is still drained and hashed, but never duplicated on disk.
    """

    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    temporary: Path | None = None
    handle = None
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file():
            _ensure_free_space(
                output_root,
                bytes_needed=max_response_bytes,
                min_free_bytes=min_free_bytes,
            )
            temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
            handle = temporary.open("wb")
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise TimeoutError("official_https_response_wall_clock_timeout")
            raw_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if raw_socket is not None:
                try:
                    raw_socket.settimeout(max(0.1, min(float(timeout_sec), remaining)))
                except (AttributeError, OSError, TypeError, ValueError):
                    pass
            try:
                with response_read_deadline(max(0.01, remaining)):
                    block = response.read(STREAM_CHUNK_BYTES)
            except TypeError:
                with response_read_deadline(max(0.01, remaining)):
                    block = response.read()
            block = bytes(block or b"")
            if not block:
                break
            total += len(block)
            if total > max_response_bytes:
                raise ContractError("model_arrival_response_too_large")
            if captured_response_bytes + total > max_capture_bytes:
                raise ContractError("model_arrival_capture_budget_exceeded")
            digest.update(block)
            if handle is not None:
                # Check the free-space floor at every write boundary so a
                # concurrent writer cannot silently consume the safety reserve.
                _ensure_free_space(output_root, bytes_needed=len(block), min_free_bytes=min_free_bytes)
                handle.write(block)
        if total <= 0:
            raise ContractError("model_arrival_provider_response_invalid")
        if handle is not None and temporary is not None and destination is not None:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            if destination.exists():
                if _hash_file(destination) != digest.hexdigest():
                    raise ContractError("model_arrival_raw_sha256_collision")
                temporary.unlink(missing_ok=True)
            else:
                os.replace(temporary, destination)
        return digest.hexdigest(), total
    finally:
        if handle is not None:
            handle.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def capture(
    *,
    provider_id: str,
    urls: list[str],
    publication_url: str | None,
    station_id: str,
    target_local_date: str,
    run_initialization_utc: str,
    valid_time_utc: str,
    decision_at_utc: str | None,
    target_timezone: str,
    output_root: Path,
    timeout_sec: float,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    range_start: int | None = None,
    range_end: int | None = None,
    run_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = _now()
    created_raw_paths: set[Path] = set()
    created_receipt_path: Path | None = None
    try:
        limits = _resource_limits(
            max_response_bytes=max_response_bytes,
            max_capture_bytes=max_capture_bytes,
            min_free_bytes=min_free_bytes,
        )
        requested_range = _bounded_byte_range(
            range_start,
            range_end,
            max_response_bytes=limits["max_response_bytes"],
        )
        if provider_id not in ALLOWED_PROVIDERS:
            raise ContractError("model_arrival_provider_not_allowed")
        if run_contract is not None:
            validate_capture_request(
                run_contract,
                provider_id=provider_id,
                station_id=station_id,
                target_local_date=target_local_date,
                run_initialization_utc=run_initialization_utc,
                target_timezone=target_timezone,
            )
        # Validate the bounded d2/d1 raw-capture horizon before network access.
        # This may be the target's pre-event phase, but it never writes the
        # target SSOT or authorizes formal consumption.
        capture_guard_at_utc = decision_at_utc or _now()
        validate_prospective_capture_window(
            target_local_date=target_local_date,
            target_timezone=target_timezone,
            run_initialization_utc=run_initialization_utc,
            decision_at_utc=capture_guard_at_utc,
        )
        _cleanup_stale_capture_temps(output_root, provider_id)
        _prune_unreferenced_raw(output_root, provider_id)
        safe_urls = [_safe_url(url, provider_id) for url in urls]
        if not safe_urls:
            raise ContractError("model_arrival_source_urls_missing")
        if len(safe_urls) > MAX_URLS_PER_CAPTURE:
            raise ContractError("model_arrival_url_count_exceeded")
        safe_publication_url = _safe_url(publication_url, provider_id) if publication_url else ""
        publication_from_metadata_url = ""
        if safe_publication_url:
            request = Request(safe_publication_url, method="HEAD", headers={"User-Agent": "bayes-basket-target-arrival-capture/1"})
            with open_official_https(request, timeout_sec=timeout_sec) as response:
                metadata_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
                last_modified = metadata_headers.get("last-modified")
            if not last_modified:
                raise ContractError("provider_publication_timestamp_missing")
            publication_from_metadata_url = _timestamp_from_last_modified(last_modified)
        captures: list[dict[str, Any]] = []
        captured_response_bytes = 0
        capture_nonce = f"{os.getpid()}.{time.time_ns()}"
        for sequence, url in enumerate(safe_urls):
            requested_at = _now()
            preflight: dict[str, Any] | None = None
            # ICON is the only provider whose target capture always requests
            # the native object directly from a rolling file tree.  Probe it
            # before GET so missing/not-yet-published DWD files fail in
            # bounded metadata time and never open a body stream.
            if provider_id == "icon_global_official":
                preflight = _icon_availability_preflight(
                    url,
                    timeout_sec=timeout_sec,
                )
            request_headers = {"User-Agent": "bayes-basket-target-arrival-capture/1"}
            if requested_range is not None:
                request_headers["Range"] = f"bytes={requested_range[0]}-{requested_range[1]}"
            request = Request(url, headers=request_headers)
            with open_official_https(request, timeout_sec=timeout_sec) as response:
                response_started_at = _now()
                headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
                status_code = int(getattr(response, "status", 200) or 200)
                if status_code < 200 or status_code >= 300:
                    raise ContractError("model_arrival_provider_response_invalid")
                if requested_range is not None:
                    _validate_byte_range_response(
                        headers,
                        status_code=status_code,
                        expected_range=requested_range,
                    )
                # Do not stream a payload when neither the payload response
                # nor the explicit metadata HEAD supplied a native issue
                # timestamp.  Besides fail-closing the evidence contract,
                # this prevents a rejected response body from becoming an
                # unreferenced raw/temp object on disk.
                last_modified = headers.get("last-modified") or str(
                    (preflight or {}).get("provider_last_modified") or ""
                )
                if not last_modified and not publication_from_metadata_url:
                    raise ContractError("provider_publication_timestamp_missing")
                advertised_bytes = _content_length(
                    headers,
                    max_response_bytes=limits["max_response_bytes"],
                )
                if advertised_bytes is not None:
                    if captured_response_bytes + advertised_bytes > limits["max_capture_bytes"]:
                        raise ContractError("model_arrival_capture_budget_exceeded")
                    _ensure_raw_retention_budget(
                        output_root,
                        provider_id,
                        bytes_needed=advertised_bytes,
                    )
                    assert_target_raw_retention_budget(
                        output_root,
                        bytes_needed=advertised_bytes,
                    )
                    _ensure_free_space(
                        output_root,
                        bytes_needed=advertised_bytes,
                        min_free_bytes=limits["min_free_bytes"],
                    )
                else:
                    _ensure_raw_retention_budget(
                        output_root,
                        provider_id,
                        bytes_needed=min(
                            limits["max_response_bytes"],
                            limits["max_capture_bytes"] - captured_response_bytes,
                        ),
                    )
                    assert_target_raw_retention_budget(
                        output_root,
                        bytes_needed=min(
                            limits["max_response_bytes"],
                            limits["max_capture_bytes"] - captured_response_bytes,
                        ),
                    )
                suffix = Path(urlsplit(url).path).suffix or ".bin"
                # The object is content-addressed, so the final path is not
                # known until hashing completes.  Stream to a bounded temp
                # file and rename it only after the digest is complete.
                temporary_target = output_root / provider_id / "raw" / f".capture.{capture_nonce}.{sequence}{suffix}"
                if temporary_target.exists():
                    raise ContractError("model_arrival_capture_temp_collision")
                created_raw_paths.add(temporary_target)
                payload_sha, response_bytes = _stream_response(
                    response,
                    destination=temporary_target,
                    output_root=output_root,
                    timeout_sec=timeout_sec,
                    max_response_bytes=limits["max_response_bytes"],
                    max_capture_bytes=limits["max_capture_bytes"],
                    min_free_bytes=limits["min_free_bytes"],
                    captured_response_bytes=captured_response_bytes,
                )
                if (
                    advertised_bytes is not None
                    and response_bytes != advertised_bytes
                ):
                    raise ContractError(
                        "model_arrival_content_length_mismatch"
                    )
                captured_response_bytes += response_bytes
                response_completed_at = _now()
            published_at = publication_from_metadata_url or _timestamp_from_last_modified(last_modified)
            raw_path = output_root / provider_id / "raw" / f"{payload_sha}{suffix}"
            temporary_target = output_root / provider_id / "raw" / f".capture.{capture_nonce}.{sequence}{suffix}"
            if temporary_target.is_file():
                if raw_path.is_file():
                    if _hash_file(raw_path) != payload_sha:
                        raise ContractError("model_arrival_raw_sha256_collision")
                    temporary_target.unlink(missing_ok=True)
                else:
                    os.replace(temporary_target, raw_path)
                    created_raw_paths.add(raw_path)
            captures.append({
                "sequence": sequence,
                "raw_request_url": url,
                "raw_object_ref": str(raw_path.resolve()),
                "raw_sha256": payload_sha,
                "response_bytes": response_bytes,
                "requested_at": requested_at,
                "response_started_at": response_started_at,
                "response_completed_at": response_completed_at,
                "provider_published_at_utc": published_at,
                "availability_preflight": preflight,
                "response_headers": {
                    key: headers[key]
                    for key in ("last-modified", "etag", "content-length", "content-range", "content-type", "date")
                    if key in headers
                },
                "request_byte_range": (
                    {"start": requested_range[0], "end": requested_range[1]}
                    if requested_range is not None
                    else None
                ),
                "publication_metadata_url": safe_publication_url or None,
            })
        # Keep the receipt envelope self-sufficient as well as preserving the
        # per-request raw receipt.  Downstream readers are allowed to consume
        # either level, but both must describe the same observed native
        # publication and completed-receive times.  These are never derived
        # from a filename, mtime, request start, or retrospective clock.
        receipt_published_at_utc = max(
            item["provider_published_at_utc"] for item in captures
        )
        receipt_received_at_utc = max(
            item["response_completed_at"] for item in captures
        )
        receipt_identity = {
            "schema": "bayes_basket_target_native_model_transport_receipt_v1",
            "provider_id": provider_id,
            "station_id": station_id,
            "target_local_date": target_local_date,
            "run_initialization_utc": run_initialization_utc,
            "valid_time_utc": valid_time_utc,
            "capture_mode": "prospective",
            "historical_arrival_time_observed": True,
            "publication_time_source": "provider_http_last_modified",
            "provider_published_at_utc": receipt_published_at_utc,
            "response_completed_at": receipt_received_at_utc,
            "aggregator_or_republisher": False,
            "open_meteo": False,
            "capture_started_at_utc": started_at,
            "raw_receipts": captures,
            "request_byte_range": (
                {"start": requested_range[0], "end": requested_range[1]}
                if requested_range is not None
                else None
            ),
            "publication_metadata_url": safe_publication_url or None,
            "resource_budget": {**limits, "captured_response_bytes": captured_response_bytes},
        }
        receipt_digest = content_sha256(receipt_identity)
        receipt_path = output_root / provider_id / "receipts" / f"{receipt_digest}.json"
        _atomic_json(receipt_path, {**receipt_identity, "receipt_content_sha256": receipt_digest})
        created_receipt_path = receipt_path
        published_at = receipt_published_at_utc
        received_at = receipt_received_at_utc
        # With no explicit cutoff, bind the decision clock to the observed
        # completion of this response.  This preserves the invariant
        # published_at <= received_at <= decision_at without rejecting every
        # live forward capture merely because the download necessarily occurs
        # after the request was initiated.
        arrival_decision_at_utc = decision_at_utc or received_at
        arrival = validate_model_arrival_capture(
            {
                "arrival_id": "arrival-" + receipt_digest[:24],
                "provider_id": provider_id,
                "model_id": ALLOWED_PROVIDERS[provider_id],
                "station_id": station_id,
                "target_local_date": target_local_date,
                "target_timezone": target_timezone,
                "run_initialization_utc": run_initialization_utc,
                "valid_time_utc": valid_time_utc,
                "published_at_utc": published_at,
                "received_at_utc": received_at,
                "decision_at_utc": arrival_decision_at_utc,
                "publication_time_source": "provider_http_last_modified",
                "source_url": safe_urls[0],
                "raw_payload_sha256s": [item["raw_sha256"] for item in captures],
                "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                "capture_mode": "prospective",
                "historical_arrival_time_observed": True,
                "aggregator_or_republisher": False,
                "open_meteo": False,
            },
            decision_at_utc=arrival_decision_at_utc,
        )
        return {
            "status": "CAPTURED",
            "arrival": arrival,
            "transport_receipt_path": str(receipt_path.resolve()),
            "boundary": {
                "target_only": True,
                "raw_capture_only": True,
                "capture_phase": arrival["capture_phase"],
                "p0_consumption_authorized": True,
                "p1_consumption_authorized": arrival["p1_consumption_eligible_at_capture"],
                "writes_production_db": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "resource_budget": {**limits, "captured_response_bytes": captured_response_bytes},
                "request_byte_range": (
                    {"start": requested_range[0], "end": requested_range[1]}
                    if requested_range is not None
                    else None
                ),
            },
        }
    except (OSError, ContractError, TimeoutError, ValueError) as exc:
        # A rejected prospective capture is not a lineage object.  In
        # particular, received-after-decision and missing native metadata must
        # not leave raw bodies or receipts that later retention treats as
        # protected evidence.  Only remove paths created by this invocation;
        # pre-existing content-addressed bodies remain untouched.
        if created_receipt_path is not None:
            created_receipt_path.unlink(missing_ok=True)
        for raw_path in created_raw_paths:
            raw_path.unlink(missing_ok=True)
        return {
            "status": "BLOCKED",
            "receipt": build_blocked_receipt(reason_codes=[str(exc)], received_at_utc=_now()),
            "boundary": {
                "target_only": True,
                "writes_production_db": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "resource_budget": locals().get("limits", {}),
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=tuple(ALLOWED_PROVIDERS), required=True)
    parser.add_argument("--url", action="append", required=True)
    parser.add_argument("--publication-url", help="direct native-provider metadata URL used only for HEAD Last-Modified")
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--target-local-date", required=True)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument("--valid-time-utc", required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument("--target-timezone", default="America/New_York")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--timeout-sec", type=float, default=60.0)
    parser.add_argument("--max-response-mib", type=int, default=DEFAULT_MAX_RESPONSE_BYTES // MIB)
    parser.add_argument("--max-capture-mib", type=int, default=DEFAULT_MAX_CAPTURE_BYTES // MIB)
    parser.add_argument("--min-free-mib", type=int, default=DEFAULT_MIN_FREE_BYTES // MIB)
    parser.add_argument("--range-start", type=int)
    parser.add_argument("--range-end", type=int)
    parser.add_argument("--run-contract", type=Path, help="target-only manifest binding all three providers to one run")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    run_contract: Mapping[str, Any] | None = None
    if args.run_contract:
        try:
            loaded = load_bounded_json(args.run_contract.expanduser().resolve())
            run_contract = loaded if isinstance(loaded, Mapping) else {}
        except (OSError, UnicodeDecodeError, ValueError):
            run_contract = {}
    result = capture(
        provider_id=args.provider,
        urls=list(args.url),
        publication_url=args.publication_url,
        station_id=args.station_id,
        target_local_date=args.target_local_date,
        run_initialization_utc=args.run_initialization_utc,
        valid_time_utc=args.valid_time_utc,
        decision_at_utc=args.decision_at_utc,
        target_timezone=args.target_timezone,
        output_root=args.output_root.expanduser().resolve(),
        timeout_sec=args.timeout_sec,
        max_response_bytes=args.max_response_mib * MIB,
        max_capture_bytes=args.max_capture_mib * MIB,
        min_free_bytes=args.min_free_mib * MIB,
        range_start=args.range_start,
        range_end=args.range_end,
        run_contract=run_contract,
    )
    if args.output:
        _atomic_json(args.output.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") == "CAPTURED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
