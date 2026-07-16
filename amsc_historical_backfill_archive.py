"""Isolated, content-addressed AMSC historical-report backfill archive.

Historical AMSC searches can provide official report *content*, but a report
downloaded today cannot prove when it reached the live system.  This module
therefore has a deliberately separate H-data contract: it preserves query
scope, raw response and parsed METAR/SPECI records for offline research while
making it structurally impossible to manufacture canonical T1 or P10 arrival
evidence.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from amsc_raw_history_archive import SENSITIVE_KEY_RE, raw_text_sha256, sanitize_metadata
from scripts.fetch_zuuu_metar_taf import PARSER_VERSION, source_payloads_from_amsc_payload
from zuuu_metar_reporting_schedule import SCHEDULE_ID, SCHEDULE_SCHEMA, expected_regular_metar_slots_utc, validate_regular_metar_slot_coverage


HISTORICAL_QUERY_SCHEMA = "amsc_historical_query_contract_v1"
HISTORICAL_RESPONSE_SCHEMA = "amsc_historical_query_response_v1"
HISTORICAL_OBJECT_SCHEMA = "amsc_historical_report_object_v1"
HISTORICAL_MANIFEST_SCHEMA = "amsc_historical_query_manifest_v1"
HISTORICAL_PREPARED_SCHEMA = "amsc_historical_backfill_prepared_v1"
HISTORICAL_ARCHIVE_RESULT_SCHEMA = "amsc_historical_backfill_archive_result_v1"
HISTORICAL_ARCHIVE_AUDIT_SCHEMA = "amsc_historical_backfill_archive_audit_v1"
HISTORICAL_COVERAGE_SCHEMA = "amsc_historical_daily_coverage_v1"

HISTORICAL_DATA_CLASS = "H"
PRIMARY_STATION = "ZUUU"
STATION_TZ = ZoneInfo("Asia/Shanghai")
_METAR_REPORT_TYPES = {"METAR", "SPECI"}
_PHYSICAL_FIELDS = (
    "temperature_c",
    "dewpoint_c",
    "visibility_m",
    "cloud_text",
    "clouds",
    "cloud_cover_pct",
    "low_cloud_cover_pct",
    "weather_text",
    "metar_current_no_significant_weather",
    "metar_trend_text",
    "metar_trend_cloud_text",
    "metar_trend_weather_text",
    "metar_trend_nosig",
    "wind_dir_deg",
    "wind_speed_mps",
    "wind_gust_mps",
    "wind_variable",
    "wind_variable_from_deg",
    "wind_variable_to_deg",
    "pressure_hpa",
)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_timestamp(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", _text(value))[:32] or "unknown"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_gzip_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError("historical_archive_payload_not_mapping")
    return dict(value)


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(SENSITIVE_KEY_RE.search(str(key)) or _contains_sensitive_key(child) for key, child in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_sensitive_key(child) for child in value)
    return False


def _object_path(root: Path, raw_sha256: str) -> Path:
    return root / "objects" / raw_sha256[:2] / f"{raw_sha256}.json.gz"


def _response_path(root: Path, response_sha256: str) -> Path:
    return root / "responses" / response_sha256[:2] / f"{response_sha256}.json.gz"


def _object_ref(raw_sha256: str) -> str:
    return f"objects/{raw_sha256[:2]}/{raw_sha256}.json.gz"


def _response_ref(response_sha256: str) -> str:
    return f"responses/{response_sha256[:2]}/{response_sha256}.json.gz"


def _station_day_utc_bounds(event_date: str) -> tuple[datetime, datetime] | None:
    try:
        local_day = date.fromisoformat(_text(event_date))
    except ValueError:
        return None
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=STATION_TZ).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def historical_station_day_query_contract(*, event_date: str, station_id: str = PRIMARY_STATION) -> dict[str, Any]:
    """Build the exact UTC query window for one Asia/Shanghai station day."""

    bounds = _station_day_utc_bounds(event_date)
    station = _text(station_id).upper()
    if bounds is None or not station:
        return {
            "schema": HISTORICAL_QUERY_SCHEMA,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["historical_query_event_date_or_station_invalid"],
            "read_only": True,
        }
    start, end_exclusive = bounds
    return {
        "schema": HISTORICAL_QUERY_SCHEMA,
        "ok": True,
        "state": "ready",
        "station_id": station,
        "utc_start": _iso_utc(start),
        "utc_end": _iso_utc(end_exclusive - timedelta(seconds=1)),
        "report_type_codes": ["SA", "SP"],
        "report_types": ["METAR", "SPECI"],
        "endpoint_id": "amsc_packet_retrieval_message_retrieval_sort",
        "time_basis": "query_utc_for_physical_station_local_day",
        "event_date": _text(event_date),
        "historical_data_class": HISTORICAL_DATA_CLASS,
        "live_arrival_evidence": False,
        "canonical_t1_eligible": False,
        "p10_latency_eligible": False,
        "read_only": True,
    }


def _normalize_query(query: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    value = dict(query or {})
    issues: list[str] = []
    if _contains_sensitive_key(value):
        issues.append("historical_query_sensitive_key_forbidden")
    start = _parse_utc(value.get("utc_start"))
    end = _parse_utc(value.get("utc_end"))
    station = _text(value.get("station_id")).upper()
    codes = sorted({_text(item).upper() for item in value.get("report_type_codes") or [] if _text(item)})
    endpoint = _text(value.get("endpoint_id"))
    if start is None or end is None or start > end:
        issues.append("historical_query_utc_window_invalid")
    elif end - start > timedelta(hours=49):
        issues.append("historical_query_window_exceeds_one_station_day")
    if station != PRIMARY_STATION:
        issues.append("historical_query_station_not_primary_zuuu")
    if not {"SA", "SP"}.issubset(set(codes)):
        issues.append("historical_query_sa_sp_report_types_required")
    if endpoint != "amsc_packet_retrieval_message_retrieval_sort":
        issues.append("historical_query_endpoint_invalid")
    normalized = {
        "station_id": station,
        "utc_start": _iso_utc(start),
        "utc_end": _iso_utc(end),
        "report_type_codes": codes,
        "endpoint_id": endpoint,
        "time_basis": "utc_interval",
        "historical_data_class": HISTORICAL_DATA_CLASS,
        "live_arrival_evidence": False,
        "canonical_t1_eligible": False,
        "p10_latency_eligible": False,
    }
    return normalized, sorted(set(issues))


def _physical_record(source: Mapping[str, Any], *, query_received_at: str, query_id: str) -> dict[str, Any] | None:
    raw_text = _text(source.get("raw_text") or source.get("raw_ref"))
    report_type = _text(source.get("report_type")).upper()
    valid_at = _parse_utc(source.get("valid_time_utc"))
    station = _text(source.get("station_id")).upper()
    if not raw_text or report_type not in _METAR_REPORT_TYPES or valid_at is None or station != PRIMARY_STATION:
        return None
    raw_sha256 = raw_text_sha256(raw_text)
    parsed = {key: source.get(key) for key in _PHYSICAL_FIELDS if key in source}
    record_core = {
        "schema": HISTORICAL_OBJECT_SCHEMA,
        "historical_data_class": HISTORICAL_DATA_CLASS,
        "source_id": "zuuu_metar_raw_amsc",
        "provider": "AMSC PacketRetrieval historical query",
        "station_id": station,
        "report_type": report_type,
        "raw_text": raw_text,
        "raw_sha256": raw_sha256,
        "verified_valid_time_utc": _iso_utc(valid_at),
        "verified_valid_time_local": valid_at.astimezone(STATION_TZ).replace(microsecond=0).isoformat(),
        "parsed_observation": parsed,
        "first_historical_query_received_at": query_received_at,
        "first_historical_query_id": query_id,
        "arrival_time_quality": "historical_query_retrieved_after_event",
        "live_arrival_evidence": False,
        "canonical_t1_eligible": False,
        "p10_latency_eligible": False,
        "archive_writer": "amsc_historical_backfill_archive_v1",
    }
    record_core["historical_report_id"] = f"amsc_h_{raw_sha256[:24]}"
    return record_core


def prepare_amsc_historical_backfill(
    *,
    payload: Mapping[str, Any] | None,
    query: Mapping[str, Any] | None,
    query_received_at: str,
    query_transport: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one historical response before any archive file is written."""

    normalized_query, issues = _normalize_query(query)
    received_at = _parse_utc(query_received_at)
    raw_payload = dict(payload or {})
    if received_at is None:
        issues.append("historical_query_received_at_invalid")
    if _contains_sensitive_key(raw_payload):
        issues.append("historical_response_sensitive_key_forbidden")
    transport = dict(query_transport or {"mode": "urllib_default_tls", "tls_verification": "verified"})
    if _contains_sensitive_key(transport):
        issues.append("historical_query_transport_sensitive_key_forbidden")
    transport_mode = _text(transport.get("mode"))
    transport_tls = _text(transport.get("tls_verification"))
    if transport_mode not in {"urllib_default_tls", "curl_system_trust_tls"}:
        issues.append("historical_query_transport_mode_invalid")
    if transport_tls != "verified":
        issues.append("historical_query_transport_tls_not_verified")
    if raw_payload.get("code") not in {200, "200"}:
        issues.append("historical_response_not_successful")
    sanitized_payload = sanitize_metadata(raw_payload)
    response_sha256 = _sha256(sanitized_payload)
    start = _parse_utc(normalized_query.get("utc_start"))
    end = _parse_utc(normalized_query.get("utc_end"))
    reference_at = _iso_utc(start + (end - start) / 2) if start is not None and end is not None else ""
    parsed_sources = source_payloads_from_amsc_payload(
        sanitized_payload,
        fetched_at=_iso_utc(received_at),
        # This is only a calendar reference for the DDHHMMZ token; it is not
        # arrival evidence and never appears in a T1/T10 field.
        valid_time_reference_at=reference_at,
    ) if received_at is not None else []
    query_id_core = {
        "schema": HISTORICAL_MANIFEST_SCHEMA,
        "query": normalized_query,
        "query_received_at": _iso_utc(received_at),
        "response_sha256": response_sha256,
        "parser_version": PARSER_VERSION,
        "query_transport": {"mode": transport_mode, "tls_verification": transport_tls},
    }
    query_id = _sha256(query_id_core)
    records_by_sha: dict[str, dict[str, Any]] = {}
    outside_query_count = 0
    malformed_report_count = 0
    for source in parsed_sources:
        record = _physical_record(source, query_received_at=_iso_utc(received_at), query_id=query_id)
        if record is None:
            malformed_report_count += 1
            continue
        valid_at = _parse_utc(record.get("verified_valid_time_utc"))
        if valid_at is None or start is None or end is None or valid_at < start or valid_at > end:
            outside_query_count += 1
            continue
        records_by_sha.setdefault(str(record["raw_sha256"]), record)
    if not records_by_sha:
        issues.append("historical_response_has_no_usable_metar_or_speci")
    records = [records_by_sha[key] for key in sorted(records_by_sha)]
    manifest_core = {
        **query_id_core,
        "returned_raw_sha256_list": [str(item["raw_sha256"]) for item in records],
        "returned_raw_count": len(records),
        "outside_query_count": outside_query_count,
        "malformed_report_count": malformed_report_count,
    }
    manifest_sha256 = _sha256(manifest_core)
    return {
        "schema": HISTORICAL_PREPARED_SCHEMA,
        "ok": not issues,
        "state": "ready" if not issues else "blocked",
        "block_reasons": sorted(set(issues)),
        "query": normalized_query,
        "query_received_at": _iso_utc(received_at),
        "query_id": query_id,
        "response_sha256": response_sha256,
        "sanitized_response_payload": sanitized_payload,
        "records": records,
        "returned_raw_count": len(records),
        "outside_query_count": outside_query_count,
        "malformed_report_count": malformed_report_count,
        "manifest_core": manifest_core,
        "manifest_sha256": manifest_sha256,
        "boundary": {
            "historical_data_class": HISTORICAL_DATA_CLASS,
            "creates_canonical_t1": False,
            "creates_p10_arrival_evidence": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


def _assert_existing_object(path: Path, expected_raw_sha256: str) -> dict[str, Any]:
    value = _read_gzip_json(path)
    if value.get("schema") != HISTORICAL_OBJECT_SCHEMA:
        raise ValueError("historical_archive_object_schema_drift")
    if _text(value.get("raw_sha256")) != expected_raw_sha256 or raw_text_sha256(value.get("raw_text")) != expected_raw_sha256:
        raise ValueError("historical_archive_object_hash_drift")
    if value.get("historical_data_class") != HISTORICAL_DATA_CLASS or value.get("canonical_t1_eligible") is not False:
        raise ValueError("historical_archive_object_boundary_drift")
    return value


def persist_amsc_historical_backfill(*, archive_dir: str | Path, prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Append a validated H-query response without touching runtime stores."""

    value = dict(prepared or {})
    if value.get("schema") != HISTORICAL_PREPARED_SCHEMA or value.get("ok") is not True:
        raise ValueError("historical_backfill_prepared_contract_not_ready")
    if _contains_sensitive_key(value):
        raise ValueError("historical_backfill_prepared_sensitive_key_forbidden")
    manifest_core = _mapping(value.get("manifest_core"))
    if _sha256(manifest_core) != _text(value.get("manifest_sha256")):
        raise ValueError("historical_backfill_manifest_sha_drift")
    response_payload = _mapping(value.get("sanitized_response_payload"))
    response_sha256 = _text(value.get("response_sha256"))
    if _sha256(response_payload) != response_sha256:
        raise ValueError("historical_backfill_response_sha_drift")
    root = Path(archive_dir).expanduser()
    response_path = _response_path(root, response_sha256)
    response_payload_to_store = {
        "schema": HISTORICAL_RESPONSE_SCHEMA,
        "response_sha256": response_sha256,
        "payload": response_payload,
        "archive_writer": "amsc_historical_backfill_archive_v1",
    }
    if response_path.exists():
        existing_response = _read_gzip_json(response_path)
        if existing_response != response_payload_to_store:
            raise ValueError("historical_backfill_response_content_drift")
        response_reused = True
    else:
        _atomic_write(response_path, gzip.compress(_canonical_json_bytes(response_payload_to_store), mtime=0))
        response_reused = False

    objects_written = 0
    objects_reused = 0
    object_index: list[dict[str, str]] = []
    for record in value.get("records") or []:
        item = _mapping(record)
        raw_sha256 = _text(item.get("raw_sha256"))
        if not raw_sha256 or raw_text_sha256(item.get("raw_text")) != raw_sha256:
            raise ValueError("historical_backfill_record_raw_hash_invalid")
        object_path = _object_path(root, raw_sha256)
        if object_path.exists():
            stored = _assert_existing_object(object_path, raw_sha256)
            if _text(stored.get("verified_valid_time_utc")) != _text(item.get("verified_valid_time_utc")):
                raise ValueError("historical_backfill_record_valid_time_drift")
            objects_reused += 1
        else:
            _atomic_write(object_path, gzip.compress(_canonical_json_bytes(item), mtime=0))
            objects_written += 1
        object_index.append({
            "raw_sha256": raw_sha256,
            "historical_report_id": _text(item.get("historical_report_id")),
            "raw_object_ref": _object_ref(raw_sha256),
            "verified_valid_time_utc": _text(item.get("verified_valid_time_utc")),
            "report_type": _text(item.get("report_type")),
        })

    received_at = _parse_utc(value.get("query_received_at"))
    if received_at is None:
        raise ValueError("historical_backfill_query_received_at_invalid")
    manifest_sha256 = _text(value.get("manifest_sha256"))
    manifest_path = (
        root / "manifests" / f"{received_at.year:04d}" / f"{received_at.month:02d}" / f"{received_at.day:02d}"
        / f"{_safe_timestamp(_iso_utc(received_at))}_{manifest_sha256[:16]}.json"
    )
    manifest_payload = {
        **manifest_core,
        "manifest_sha256": manifest_sha256,
        "query_id": _text(value.get("query_id")),
        "response_ref": _response_ref(response_sha256),
        "raw_objects": sorted(object_index, key=lambda item: item["raw_sha256"]),
        "historical_data_class": HISTORICAL_DATA_CLASS,
        "live_arrival_evidence": False,
        "canonical_t1_eligible": False,
        "p10_latency_eligible": False,
        "archive_writer": "amsc_historical_backfill_archive_v1",
    }
    if manifest_path.exists():
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing_manifest != manifest_payload:
            raise ValueError("historical_backfill_manifest_content_drift")
        manifest_reused = True
    else:
        _atomic_write(manifest_path, _canonical_json_bytes(manifest_payload) + b"\n")
        manifest_reused = False
    return {
        "schema": HISTORICAL_ARCHIVE_RESULT_SCHEMA,
        "ok": True,
        "state": "appended",
        "archive_dir": str(root),
        "manifest_ref": str(manifest_path.relative_to(root)),
        "manifest_sha256": manifest_sha256,
        "response_ref": _response_ref(response_sha256),
        "objects_written": objects_written,
        "objects_reused": objects_reused,
        "response_reused": response_reused,
        "manifest_reused": manifest_reused,
        "raw_object_count": len(object_index),
        "boundary": {
            "historical_data_class": HISTORICAL_DATA_CLASS,
            "creates_canonical_t1": False,
            "creates_p10_arrival_evidence": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def _historical_object_paths(root: Path) -> list[Path]:
    return sorted((root / "objects").glob("**/*.json.gz")) if (root / "objects").exists() else []


def audit_amsc_historical_backfill_archive(archive_dir: str | Path) -> dict[str, Any]:
    """Read-only integrity and runtime-boundary audit for the H archive."""

    root = Path(archive_dir).expanduser()
    issues: list[str] = []
    objects = _historical_object_paths(root)
    responses = sorted((root / "responses").glob("**/*.json.gz")) if (root / "responses").exists() else []
    manifests = sorted((root / "manifests").glob("**/*.json")) if (root / "manifests").exists() else []
    object_hashes: set[str] = set()
    response_hashes: set[str] = set()
    prohibited_runtime_keys = {
        "first_seen_at",
        "as_of_eligible_at",
        "first_actionable_seen_at",
        "source_event_id",
        "canonical_observation_event_id",
    }
    for path in objects:
        try:
            item = _read_gzip_json(path)
            raw_sha256 = _text(item.get("raw_sha256"))
            if item.get("schema") != HISTORICAL_OBJECT_SCHEMA:
                issues.append(f"object_schema_invalid:{path.relative_to(root)}")
            if not raw_sha256 or raw_text_sha256(item.get("raw_text")) != raw_sha256:
                issues.append(f"object_hash_invalid:{path.relative_to(root)}")
            if _parse_utc(item.get("verified_valid_time_utc")) is None:
                issues.append(f"object_valid_time_invalid:{path.relative_to(root)}")
            if _parse_utc(item.get("first_historical_query_received_at")) is None:
                issues.append(f"object_query_received_at_invalid:{path.relative_to(root)}")
            if item.get("historical_data_class") != HISTORICAL_DATA_CLASS or item.get("live_arrival_evidence") is not False or item.get("canonical_t1_eligible") is not False or item.get("p10_latency_eligible") is not False:
                issues.append(f"object_runtime_boundary_invalid:{path.relative_to(root)}")
            if prohibited_runtime_keys.intersection(item):
                issues.append(f"object_runtime_identity_leak:{path.relative_to(root)}")
            if _contains_sensitive_key(item):
                issues.append(f"object_sensitive_key:{path.relative_to(root)}")
            object_hashes.add(raw_sha256)
        except Exception:
            issues.append(f"object_unreadable:{path.relative_to(root)}")
    for path in responses:
        try:
            item = _read_gzip_json(path)
            response_sha256 = _text(item.get("response_sha256"))
            if item.get("schema") != HISTORICAL_RESPONSE_SCHEMA or not response_sha256 or _sha256(_mapping(item.get("payload"))) != response_sha256:
                issues.append(f"response_hash_or_schema_invalid:{path.relative_to(root)}")
            if _contains_sensitive_key(item):
                issues.append(f"response_sensitive_key:{path.relative_to(root)}")
            response_hashes.add(response_sha256)
        except Exception:
            issues.append(f"response_unreadable:{path.relative_to(root)}")
    for path in manifests:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(item, Mapping):
                raise ValueError("not_mapping")
            core = {
                "schema": item.get("schema"),
                "query": item.get("query"),
                "query_received_at": item.get("query_received_at"),
                "response_sha256": item.get("response_sha256"),
                "parser_version": item.get("parser_version"),
                "query_transport": item.get("query_transport"),
                "returned_raw_sha256_list": item.get("returned_raw_sha256_list"),
                "returned_raw_count": item.get("returned_raw_count"),
                "outside_query_count": item.get("outside_query_count"),
                "malformed_report_count": item.get("malformed_report_count"),
            }
            if item.get("schema") != HISTORICAL_MANIFEST_SCHEMA or _sha256(core) != _text(item.get("manifest_sha256")):
                issues.append(f"manifest_hash_or_schema_invalid:{path.relative_to(root)}")
            normalized_query, query_issues = _normalize_query(_mapping(item.get("query")))
            if query_issues or normalized_query != item.get("query"):
                issues.append(f"manifest_query_invalid:{path.relative_to(root)}")
            if _parse_utc(item.get("query_received_at")) is None:
                issues.append(f"manifest_query_received_at_invalid:{path.relative_to(root)}")
            if item.get("historical_data_class") != HISTORICAL_DATA_CLASS or item.get("live_arrival_evidence") is not False or item.get("canonical_t1_eligible") is not False or item.get("p10_latency_eligible") is not False:
                issues.append(f"manifest_runtime_boundary_invalid:{path.relative_to(root)}")
            if _text(item.get("response_sha256")) not in response_hashes:
                issues.append(f"manifest_response_missing:{path.relative_to(root)}")
            for raw_sha256 in item.get("returned_raw_sha256_list") or []:
                if _text(raw_sha256) not in object_hashes:
                    issues.append(f"manifest_object_missing:{path.relative_to(root)}:{raw_sha256}")
            if _contains_sensitive_key(item):
                issues.append(f"manifest_sensitive_key:{path.relative_to(root)}")
        except Exception:
            issues.append(f"manifest_unreadable:{path.relative_to(root)}")
    return {
        "schema": HISTORICAL_ARCHIVE_AUDIT_SCHEMA,
        "ok": not issues,
        "archive_dir": str(root),
        "object_count": len(objects),
        "response_count": len(responses),
        "manifest_count": len(manifests),
        "issues": sorted(set(issues)),
        "boundary": {
            "historical_data_class": HISTORICAL_DATA_CLASS,
            "creates_canonical_t1": False,
            "creates_p10_arrival_evidence": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


def build_amsc_historical_daily_coverage(
    archive_dir: str | Path,
    *,
    event_date: str,
    station_id: str = PRIMARY_STATION,
) -> dict[str, Any]:
    """Build a read-only schedule-bound primary-METAR assessment for H."""

    root = Path(archive_dir).expanduser()
    bounds = _station_day_utc_bounds(event_date)
    station = _text(station_id).upper()
    issues: list[str] = []
    if bounds is None or station != PRIMARY_STATION:
        issues.append("historical_coverage_event_date_or_station_invalid")
    if bounds is None:
        start = end = None
        expected_slots: list[datetime] = []
    else:
        start, end = bounds
        expected_slots = [_parse_utc(value) for value in expected_regular_metar_slots_utc(event_date)]
        expected_slots = [value for value in expected_slots if value is not None]
    by_slot: dict[str, set[str]] = {}
    off_grid: list[str] = []
    for path in _historical_object_paths(root):
        try:
            item = _read_gzip_json(path)
        except Exception:
            continue
        if _text(item.get("station_id")).upper() != station or _text(item.get("report_type")).upper() != "METAR":
            continue
        valid_at = _parse_utc(item.get("verified_valid_time_utc"))
        if valid_at is None or start is None or end is None or not (start <= valid_at < end):
            continue
        valid_text = _iso_utc(valid_at)
        raw_sha256 = _text(item.get("raw_sha256"))
        by_slot.setdefault(valid_text, set()).add(raw_sha256)
        if valid_at not in expected_slots:
            off_grid.append(valid_text)
    slot_contract = validate_regular_metar_slot_coverage(
        event_date=event_date,
        observed_valid_times_utc=by_slot,
    )
    expected_text = list(slot_contract.get("expected_regular_metar_slots_utc") or [])
    missing_slots = list(slot_contract.get("missing_regular_metar_slots_utc") or [])
    unexpected_slots = list(slot_contract.get("unexpected_regular_metar_slots_utc") or [])
    correction_slots = [slot for slot, raw_values in sorted(by_slot.items()) if len(raw_values) > 1]
    archive_audit = audit_amsc_historical_backfill_archive(root)
    complete = bool(expected_slots) and not missing_slots and not unexpected_slots and not correction_slots and not off_grid and bool(archive_audit.get("ok"))
    return {
        "schema": HISTORICAL_COVERAGE_SCHEMA,
        "ok": not issues,
        "state": "complete" if complete else "incomplete",
        "event_date": _text(event_date),
        "station_id": station,
        "reporting_schedule_schema": SCHEDULE_SCHEMA,
        "reporting_schedule_id": SCHEDULE_ID,
        "expected_regular_metar_slot_count": len(expected_slots),
        "expected_regular_metar_slots_utc": expected_text,
        "returned_regular_metar_slot_count": len(by_slot),
        "missing_regular_metar_slots_utc": missing_slots,
        "unexpected_regular_metar_slots_utc": unexpected_slots,
        "correction_slots_utc": correction_slots,
        "off_grid_primary_metar_slots_utc": sorted(set(off_grid)),
        "offline_physical_sequence_eligible": complete,
        "strict_asof_or_live_replay_eligible": False,
        "strict_asof_or_live_replay_na_reason": "historical_query_retrieval_has_no_forward_arrival_evidence",
        "archive_integrity": {
            "ok": bool(archive_audit.get("ok")),
            "issues": list(archive_audit.get("issues") or []),
        },
        "issues": sorted(set(issues)),
        "boundary": {
            "historical_data_class": HISTORICAL_DATA_CLASS,
            "creates_canonical_t1": False,
            "creates_p10_arrival_evidence": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


def load_amsc_historical_complete_primary_metar_day(
    archive_dir: str | Path,
    *,
    event_date: str,
    station_id: str = PRIMARY_STATION,
) -> dict[str, Any]:
    """Read one complete H physical sequence without constructing T1/T10 facts."""

    root = Path(archive_dir).expanduser()
    coverage = build_amsc_historical_daily_coverage(root, event_date=event_date, station_id=station_id)
    if coverage.get("offline_physical_sequence_eligible") is not True:
        return {
            "schema": "amsc_historical_complete_primary_metar_day_v1",
            "ok": False,
            "state": "blocked",
            "event_date": _text(event_date),
            "block_reasons": ["historical_primary_metar_schedule_coverage_not_complete"],
            "coverage": coverage,
            "read_only": True,
        }
    expected_slots = list(coverage.get("expected_regular_metar_slots_utc") or [])
    reports_by_slot: dict[str, dict[str, Any]] = {}
    for path in _historical_object_paths(root):
        try:
            item = _read_gzip_json(path)
        except Exception:
            continue
        if (
            _text(item.get("station_id")).upper() != _text(station_id).upper()
            or _text(item.get("report_type")).upper() != "METAR"
        ):
            continue
        valid_at = _parse_utc(item.get("verified_valid_time_utc"))
        valid_text = _iso_utc(valid_at)
        if valid_text not in expected_slots:
            continue
        if valid_text in reports_by_slot:
            return {
                "schema": "amsc_historical_complete_primary_metar_day_v1",
                "ok": False,
                "state": "blocked",
                "event_date": _text(event_date),
                "block_reasons": ["historical_primary_metar_slot_duplicate_after_coverage"],
                "coverage": coverage,
                "read_only": True,
            }
        reports_by_slot[valid_text] = {
            "historical_report_id": _text(item.get("historical_report_id")),
            "raw_sha256": _text(item.get("raw_sha256")),
            "raw_object_ref": str(path.relative_to(root)),
            "valid_time_utc": valid_text,
            "valid_time_local": _text(item.get("verified_valid_time_local")),
            "parsed_observation": dict(_mapping(item.get("parsed_observation"))),
            "historical_data_class": HISTORICAL_DATA_CLASS,
            "arrival_time_quality": _text(item.get("arrival_time_quality")),
            "live_arrival_evidence": False,
        }
    reports = [reports_by_slot[slot] for slot in expected_slots if slot in reports_by_slot]
    if len(reports) != len(expected_slots):
        return {
            "schema": "amsc_historical_complete_primary_metar_day_v1",
            "ok": False,
            "state": "blocked",
            "event_date": _text(event_date),
            "block_reasons": ["historical_primary_metar_report_load_coverage_drift"],
            "coverage": coverage,
            "read_only": True,
        }
    return {
        "schema": "amsc_historical_complete_primary_metar_day_v1",
        "ok": True,
        "state": "available",
        "event_date": _text(event_date),
        "station_id": _text(station_id).upper(),
        "reporting_schedule_id": _text(coverage.get("reporting_schedule_id")),
        "reports": reports,
        "coverage": coverage,
        "boundary": {
            "historical_data_class": HISTORICAL_DATA_CLASS,
            "creates_canonical_t1": False,
            "creates_p10_arrival_evidence": False,
            "strict_asof_training_eligible": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


__all__ = [
    "HISTORICAL_ARCHIVE_AUDIT_SCHEMA",
    "HISTORICAL_ARCHIVE_RESULT_SCHEMA",
    "HISTORICAL_COVERAGE_SCHEMA",
    "HISTORICAL_DATA_CLASS",
    "HISTORICAL_OBJECT_SCHEMA",
    "HISTORICAL_PREPARED_SCHEMA",
    "audit_amsc_historical_backfill_archive",
    "build_amsc_historical_daily_coverage",
    "historical_station_day_query_contract",
    "load_amsc_historical_complete_primary_metar_day",
    "persist_amsc_historical_backfill",
    "prepare_amsc_historical_backfill",
]
