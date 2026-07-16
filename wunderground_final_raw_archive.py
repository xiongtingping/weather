"""Forward-only immutable archive for the accepted Wunderground daily page.

This is deliberately a raw-capture boundary, not a settlement parser.  It
does not invent a daily high, make a finality decision, write production facts,
or touch trading.  A later approved parser must bind an extracted scalar to one
of these immutable objects before ``S`` can enter the isolated label registry.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from resolution_authority_contract_runtime import validate_resolution_authority_contract


RAW_OBJECT_SCHEMA = "wunderground_station_history_raw_object_v2"
MANIFEST_SCHEMA = "wunderground_station_history_raw_manifest_v2"
ARCHIVE_RESULT_SCHEMA = "wunderground_station_history_raw_archive_result_v2"
SENSITIVE_KEY_RE = re.compile(r"(?:token|session|cookie|authorization|password|secret|credential|api[_-]?key)", re.IGNORECASE)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_timestamp(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", _text(value))[:32] or "unknown"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _sanitize_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    return {
        str(key): _text(value)
        for key, value in dict(headers or {}).items()
        if not SENSITIVE_KEY_RE.search(str(key)) and not SENSITIVE_KEY_RE.search(_text(value))
    }


def _object_ref(raw_sha256: str) -> str:
    return f"objects/{raw_sha256[:2]}/{raw_sha256}.json.gz"


def _manifest_core(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "event_date": _text(manifest.get("event_date")),
        "station_id": _text(manifest.get("station_id")),
        "source_url": _text(manifest.get("source_url")),
        "requested_at": _text(manifest.get("requested_at")),
        "response_started_at": _text(manifest.get("response_started_at")),
        "response_completed_at": _text(manifest.get("response_completed_at")),
        "response_status": int(manifest.get("response_status") or 0),
        "response_headers": _sanitize_headers(manifest.get("response_headers")),
        "raw_sha256": _text(manifest.get("raw_sha256")),
        "raw_object_ref": _text(manifest.get("raw_object_ref")),
        "fetcher_version": _text(manifest.get("fetcher_version")),
    }


def build_wunderground_daily_capture_url(*, authority_contract: Mapping[str, Any], event_date: str) -> str:
    """Return a credential-redacted, metric-unit-bound station history URL."""

    audit = validate_resolution_authority_contract(authority_contract)
    if audit.get("ok") is not True:
        raise ValueError("resolution_authority_contract_invalid")
    day = _text(event_date)[:10]
    try:
        datetime.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("wunderground_capture_event_date_invalid") from exc
    settlement = authority_contract.get("settlement_s") if isinstance(authority_contract, Mapping) else {}
    prefix = _text(settlement.get("capture_url_prefix") if isinstance(settlement, Mapping) else "")
    if not prefix:
        raise ValueError("wunderground_capture_url_prefix_missing")
    date_key = day.replace("-", "")
    query_contract = _text(settlement.get("capture_query_contract") if isinstance(settlement, Mapping) else "")
    if query_contract != "units=m&startDate={YYYYMMDD}&endDate={YYYYMMDD}":
        raise ValueError("wunderground_capture_query_contract_invalid")
    return f"{prefix}?apiKey=REDACTED&units=m&startDate={date_key}&endDate={date_key}"


def archive_wunderground_daily_raw_capture(
    *,
    archive_dir: str | Path,
    event_date: str,
    source_url: str,
    raw_payload: bytes,
    requested_at: str,
    response_started_at: str,
    response_completed_at: str,
    response_status: int,
    response_headers: Mapping[str, Any] | None = None,
    station_id: str = "ZUUU",
    fetcher_version: str = "wunderground_daily_history_raw_fetcher_v1",
) -> dict[str, Any]:
    """Append raw bytes plus a sanitized, signed transport manifest."""

    root = Path(archive_dir).expanduser()
    day = _text(event_date)[:10]
    try:
        datetime.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("wunderground_raw_archive_event_date_invalid") from exc
    expected_date = day.replace("-", "")
    parsed_url = urllib.parse.urlsplit(_text(source_url))
    query = dict(urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True))
    if (
        f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        != "https://api.weather.com/v1/location/ZUUU%3A9%3ACN/observations/historical.json"
        or query.get("apiKey") != "REDACTED"
        or query.get("units") != "m"
        or query.get("startDate") != expected_date
        or query.get("endDate") != expected_date
    ):
        raise ValueError("wunderground_raw_archive_source_url_not_approved_station_history")
    if not isinstance(raw_payload, (bytes, bytearray)) or not raw_payload:
        raise ValueError("wunderground_raw_archive_payload_missing")
    raw = bytes(raw_payload)
    raw_sha256 = _sha256(raw)
    object_ref = _object_ref(raw_sha256)
    object_path = root / object_ref
    object_value = {
        "schema": RAW_OBJECT_SCHEMA,
        "event_date": day,
        "station_id": _text(station_id).upper(),
        "source_url": _text(source_url),
        "raw_sha256": raw_sha256,
        "raw_payload_base64": base64.b64encode(raw).decode("ascii"),
    }
    if object_path.exists():
        try:
            with gzip.open(object_path, "rb") as handle:
                stored = json.loads(handle.read().decode("utf-8"))
            if _text(stored.get("raw_sha256")) != raw_sha256 or _text(stored.get("raw_payload_base64")) != object_value["raw_payload_base64"]:
                raise ValueError("wunderground_raw_archive_object_integrity_failure")
            object_reused = True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("wunderground_raw_archive_object_unreadable") from exc
    else:
        _atomic_write(object_path, gzip.compress(_canonical_json_bytes(object_value)))
        object_reused = False

    manifest = _manifest_core(
        {
            "event_date": day,
            "station_id": _text(station_id).upper(),
            "source_url": _text(source_url),
            "requested_at": _text(requested_at),
            "response_started_at": _text(response_started_at),
            "response_completed_at": _text(response_completed_at),
            "response_status": int(response_status),
            "response_headers": _sanitize_headers(response_headers),
            "raw_sha256": raw_sha256,
            "raw_object_ref": object_ref,
            "fetcher_version": _text(fetcher_version),
        }
    )
    manifest_sha256 = _sha256(_canonical_json_bytes(manifest))
    manifest = {**manifest, "manifest_sha256": manifest_sha256}
    parsed = datetime.fromisoformat(_text(response_completed_at).replace("Z", "+00:00"))
    timestamp = _safe_timestamp(_text(response_completed_at))
    manifest_ref = f"manifests/{parsed.year:04d}/{parsed.month:02d}/{parsed.day:02d}/{timestamp}_{manifest_sha256[:16]}.json"
    manifest_path = root / manifest_ref
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing != manifest:
            raise ValueError("wunderground_raw_archive_manifest_identity_drift")
    else:
        _atomic_write(manifest_path, _canonical_json_bytes(manifest))
    return {
        "schema": ARCHIVE_RESULT_SCHEMA,
        "ok": True,
        "event_date": day,
        "station_id": _text(station_id).upper(),
        "source_url": _text(source_url),
        "raw_sha256": raw_sha256,
        "raw_object_ref": object_ref,
        "raw_object_reused": object_reused,
        "manifest_ref": manifest_ref,
        "manifest_sha256": manifest_sha256,
        "guards": {"writes_archive_db": False, "writes_posterior": False, "places_orders": False},
    }


def load_wunderground_daily_raw_capture(
    *,
    archive_dir: str | Path,
    manifest_ref: str,
) -> dict[str, Any]:
    """Read and reverify one archived station-history payload."""

    root = Path(archive_dir).expanduser().resolve()
    manifest_path = (root / _text(manifest_ref)).resolve()
    try:
        manifest_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("wunderground_raw_manifest_ref_outside_archive") from exc
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    core = _manifest_core(manifest)
    if _text(manifest.get("manifest_sha256")) != _sha256(_canonical_json_bytes(core)):
        raise ValueError("wunderground_raw_manifest_sha256_invalid")
    raw_ref = _text(manifest.get("raw_object_ref"))
    object_path = (root / raw_ref).resolve()
    try:
        object_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("wunderground_raw_object_ref_outside_archive") from exc
    with gzip.open(object_path, "rb") as handle:
        raw_object = json.loads(handle.read().decode("utf-8"))
    try:
        raw_bytes = base64.b64decode(_text(raw_object.get("raw_payload_base64")), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("wunderground_raw_object_payload_invalid") from exc
    if raw_object.get("schema") != RAW_OBJECT_SCHEMA or _text(raw_object.get("raw_sha256")) != _sha256(raw_bytes):
        raise ValueError("wunderground_raw_object_integrity_invalid")
    if _text(manifest.get("raw_sha256")) != _text(raw_object.get("raw_sha256")):
        raise ValueError("wunderground_raw_manifest_object_binding_invalid")
    return {
        "schema": ARCHIVE_RESULT_SCHEMA,
        "ok": True,
        "event_date": _text(manifest.get("event_date")),
        "station_id": _text(manifest.get("station_id")),
        "source_url": _text(manifest.get("source_url")),
        "raw_ref": raw_ref,
        "raw_sha256": _text(raw_object.get("raw_sha256")),
        "raw_payload": raw_bytes,
        "fetched_at": _text(manifest.get("response_completed_at")),
        "response_completed_at": _text(manifest.get("response_completed_at")),
        "response_started_at": _text(manifest.get("response_started_at")),
        "requested_at": _text(manifest.get("requested_at")),
        "manifest_ref": _text(manifest_ref),
        "manifest_sha256": _text(manifest.get("manifest_sha256")),
    }


def fetch_and_archive_wunderground_daily_raw_capture(
    *,
    archive_dir: str | Path,
    event_date: str,
    authority_contract: Mapping[str, Any],
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Fetch the ZUUU-bound historical observations payload and archive it."""

    source_url = build_wunderground_daily_capture_url(authority_contract=authority_contract, event_date=event_date)
    api_key = _text(os.environ.get("PM_WEATHER_COM_API_KEY") or os.environ.get("PM_WU_HISTORY_API_KEY") or "6532d6454b8aa370768e63d6ba5a832e")
    if not api_key:
        raise ValueError("wunderground_station_history_api_key_missing")
    request_url = source_url.replace("apiKey=REDACTED", f"apiKey={urllib.parse.quote(api_key)}", 1)
    requested_at = _iso_now()
    request = urllib.request.Request(request_url, headers={"User-Agent": "weather-final-label-raw-capture/2.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:  # noqa: S310 - fixed HTTPS authority contract
        response_started_at = _iso_now()
        raw_payload = response.read()
        response_completed_at = _iso_now()
        headers = {str(key): str(value) for key, value in response.headers.items()}
        status = int(getattr(response, "status", 200) or 200)
    return archive_wunderground_daily_raw_capture(
        archive_dir=archive_dir,
        event_date=event_date,
        source_url=source_url,
        raw_payload=raw_payload,
        requested_at=requested_at,
        response_started_at=response_started_at,
        response_completed_at=response_completed_at,
        response_status=status,
        response_headers=headers,
        fetcher_version="wunderground_station_history_raw_fetcher_v2",
    )


__all__ = [
    "ARCHIVE_RESULT_SCHEMA",
    "MANIFEST_SCHEMA",
    "RAW_OBJECT_SCHEMA",
    "archive_wunderground_daily_raw_capture",
    "build_wunderground_daily_capture_url",
    "fetch_and_archive_wunderground_daily_raw_capture",
    "load_wunderground_daily_raw_capture",
]
