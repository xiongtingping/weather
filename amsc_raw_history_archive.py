"""Append-only, content-addressed AMSC raw report archive.

This module is deliberately outside the fact writer.  It preserves the raw
AMSC reports and each poll's sanitized availability manifest so later
trajectory replay can prove what was observed without creating another
posterior or execution data path.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from zuuu_metar_reporting_schedule import (
    EXPECTED_REGULAR_SLOT_COUNT,
    SCHEDULE_ID,
    SCHEDULE_SCHEMA,
    build_zuuu_primary_metar_schedule_contract,
    validate_regular_metar_slot_coverage,
)


RAW_OBJECT_SCHEMA = "amsc_raw_report_object_v1"
POLL_MANIFEST_SCHEMA = "amsc_raw_poll_manifest_v1"
ARCHIVE_RESULT_SCHEMA = "amsc_raw_history_archive_result_v1"
SENSITIVE_KEY_RE = re.compile(r"(?:token|session|cookie|authorization|password|secret|credential)", re.IGNORECASE)
STATION_TZ = ZoneInfo("Asia/Shanghai")
DAILY_COVERAGE_SCHEMA = "amsc_daily_raw_coverage_manifest_v2"
DAILY_COVERAGE_DIRECTORY = "coverage_v2"
DAILY_COVERAGE_MATERIALIZATION_SCHEMA = "amsc_daily_coverage_v2_materialization_result_v1"
DAILY_COVERAGE_MATERIALIZATION_WRITER = "amsc_daily_coverage_v2_materializer_v1"


def raw_text_sha256(value: Any) -> str:
    """Return the identity hash for one raw provider report, not a poll payload."""

    return hashlib.sha256(str(value or "").strip().encode("utf-8")).hexdigest()


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _safe_timestamp(value: str) -> str:
    text = str(value or "").strip() or _iso_now()
    return re.sub(r"[^0-9A-Za-z]+", "", text)[:32] or "unknown"


def _date_parts(value: str) -> tuple[str, str, str]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return f"{parsed.year:04d}", f"{parsed.month:02d}", f"{parsed.day:02d}"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _read_json_gz(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _station_day(value: datetime | None) -> str:
    return value.astimezone(STATION_TZ).date().isoformat() if value is not None else ""


def _daily_coverage_core(coverage: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": DAILY_COVERAGE_SCHEMA,
        "event_date": str(coverage.get("event_date") or ""),
        "station_id": str(coverage.get("station_id") or ""),
        "raw_object_sha256_list": list(coverage.get("raw_object_sha256_list") or []),
        "distinct_valid_time_count": coverage.get("distinct_valid_time_count"),
        "first_valid_time_utc": str(coverage.get("first_valid_time_utc") or ""),
        "last_valid_time_utc": str(coverage.get("last_valid_time_utc") or ""),
        "max_gap_seconds": coverage.get("max_gap_seconds"),
        "correction_count": coverage.get("correction_count"),
        "late_arrival_count": coverage.get("late_arrival_count"),
        "missing_first_seen_count": coverage.get("missing_first_seen_count"),
        "day_state": str(coverage.get("day_state") or ""),
        "reporting_schedule_schema": str(coverage.get("reporting_schedule_schema") or ""),
        "reporting_schedule_id": str(coverage.get("reporting_schedule_id") or ""),
        "expected_regular_metar_slot_count": coverage.get("expected_regular_metar_slot_count"),
        "expected_regular_metar_slots_utc": list(coverage.get("expected_regular_metar_slots_utc") or []),
        "missing_regular_metar_slots_utc": list(coverage.get("missing_regular_metar_slots_utc") or []),
        "unexpected_regular_metar_slots_utc": list(coverage.get("unexpected_regular_metar_slots_utc") or []),
    }


def validate_amsc_daily_coverage_snapshot(coverage: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the signed content identity and frozen ZUUU schedule contract."""

    value = dict(coverage) if isinstance(coverage, Mapping) else {}
    issues: list[str] = []
    if value.get("schema") != DAILY_COVERAGE_SCHEMA:
        issues.append("coverage_schema_invalid")
    core = _daily_coverage_core(value)
    expected_sha256 = hashlib.sha256(_canonical_json_bytes(core)).hexdigest()
    if str(value.get("coverage_sha256") or "") != expected_sha256:
        issues.append("coverage_sha256_mismatch")
    try:
        expected_schedule = build_zuuu_primary_metar_schedule_contract(core["event_date"])
    except Exception:
        expected_schedule = {}
        issues.append("coverage_event_date_invalid")
    if (
        core["reporting_schedule_schema"] != SCHEDULE_SCHEMA
        or core["reporting_schedule_id"] != SCHEDULE_ID
        or core["expected_regular_metar_slot_count"] != EXPECTED_REGULAR_SLOT_COUNT
        or core["expected_regular_metar_slots_utc"] != expected_schedule.get("expected_regular_metar_slots_utc")
    ):
        issues.append("coverage_reporting_schedule_invalid")
    if "snapshot_generated_at" in value and _parse_iso(value.get("snapshot_generated_at")) is None:
        issues.append("coverage_snapshot_generated_at_invalid")
    return {
        "schema": "amsc_daily_coverage_snapshot_contract_v1",
        "ok": not issues,
        "issues": sorted(set(issues)),
        "coverage_sha256": str(value.get("coverage_sha256") or ""),
        "expected_coverage_sha256": expected_sha256,
    }


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * float(percentile))))
    return round(ordered[position], 3)


def sanitize_metadata(value: Any) -> Any:
    """Recursively drop credential-bearing keys before anything reaches disk."""

    if isinstance(value, Mapping):
        return {
            str(key): sanitize_metadata(item)
            for key, item in value.items()
            if not SENSITIVE_KEY_RE.search(str(key))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_metadata(item) for item in value]
    return value


def _poll_manifest_core(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the signed poll-manifest payload, including v1 optional timing.

    ``response_started_at`` was added without changing the manifest schema so
    earlier append-only manifests remain verifiable.  Its absence means the
    poll is still useful as a raw archive record but cannot prove the complete
    request/start/complete transport lineage required by strict Tier-A TAF.
    """

    core = {
        "schema": POLL_MANIFEST_SCHEMA,
        "poll_started_at": str(manifest.get("poll_started_at") or ""),
        "response_completed_at": str(manifest.get("response_completed_at") or ""),
        "endpoint_id": str(manifest.get("endpoint_id") or ""),
        "sanitized_query": sanitize_metadata(manifest.get("sanitized_query") or {}),
        "response_status": manifest.get("response_status"),
        "returned_raw_sha256_list": list(manifest.get("returned_raw_sha256_list") or []),
        "returned_raw_count": manifest.get("returned_raw_count"),
        "fetcher_version": str(manifest.get("fetcher_version") or ""),
    }
    if "response_started_at" in manifest:
        core["response_started_at"] = str(manifest.get("response_started_at") or "")
    if "response_headers_observed" in manifest:
        core["response_headers_observed"] = manifest.get("response_headers_observed") is True
    return core


def validate_amsc_raw_poll_manifest(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate immutable manifest identity and observed transport chronology."""

    value = dict(manifest) if isinstance(manifest, Mapping) else {}
    issues: list[str] = []
    if value.get("schema") != POLL_MANIFEST_SCHEMA:
        issues.append("amsc_poll_manifest_schema_invalid")
    expected_sha256 = hashlib.sha256(_canonical_json_bytes(_poll_manifest_core(value))).hexdigest()
    if str(value.get("manifest_sha256") or "") != expected_sha256:
        issues.append("amsc_poll_manifest_sha256_mismatch")
    raw_list = [str(item or "") for item in value.get("returned_raw_sha256_list") or []]
    if len(raw_list) != len(set(raw_list)) or any(not item for item in raw_list):
        issues.append("amsc_poll_manifest_raw_sha_list_invalid")
    if value.get("returned_raw_count") != len(raw_list):
        issues.append("amsc_poll_manifest_raw_count_mismatch")
    request_started = _parse_iso(value.get("poll_started_at"))
    response_started = _parse_iso(value.get("response_started_at")) if "response_started_at" in value else None
    response_completed = _parse_iso(value.get("response_completed_at"))
    if request_started is None:
        issues.append("amsc_poll_manifest_poll_started_at_invalid")
    if response_completed is None:
        issues.append("amsc_poll_manifest_response_completed_at_invalid")
    if "response_started_at" in value and response_started is None:
        issues.append("amsc_poll_manifest_response_started_at_invalid")
    if "response_headers_observed" in value and not isinstance(value.get("response_headers_observed"), bool):
        issues.append("amsc_poll_manifest_response_headers_observed_invalid")
    if request_started and response_started and response_started < request_started:
        issues.append("amsc_poll_manifest_response_started_before_request")
    if response_started and response_completed and response_completed < response_started:
        issues.append("amsc_poll_manifest_response_completed_before_start")
    if SENSITIVE_KEY_RE.search(" ".join(str(key) for key in value)):
        issues.append("amsc_poll_manifest_sensitive_key_present")
    return {
        "schema": "amsc_raw_poll_manifest_contract_v1",
        "ok": not issues,
        "issues": sorted(set(issues)),
        "manifest_sha256": str(value.get("manifest_sha256") or ""),
        "expected_manifest_sha256": expected_sha256,
        "request_started_at": str(value.get("poll_started_at") or ""),
        "response_started_at": str(value.get("response_started_at") or ""),
        "response_completed_at": str(value.get("response_completed_at") or ""),
        "response_headers_observed": value.get("response_headers_observed") is True,
        "strict_transport_observed": response_started is not None and request_started is not None and response_completed is not None,
    }


def _source_record(source: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_text = str(source.get("raw_text") or source.get("raw_ref") or "").strip()
    if not raw_text:
        return None
    raw_sha256 = raw_text_sha256(raw_text)
    return {
        "source_id": str(source.get("source_id") or source.get("source") or "").strip(),
        "station_id": str(source.get("station_id") or "").strip(),
        "report_type": str(source.get("report_type") or "").strip().upper(),
        "raw_text": raw_text,
        "raw_sha256": raw_sha256,
        "verified_valid_time_utc": str(source.get("verified_metar_valid_time") or source.get("valid_time_utc") or source.get("valid_time") or "").strip(),
        "verified_valid_time_local": str(source.get("valid_time_local") or "").strip(),
    }


def archive_source_records(source_payloads: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize source payloads to immutable raw-object records, deduped by content."""

    records: dict[str, dict[str, Any]] = {}
    for source in source_payloads:
        record = _source_record(source)
        if record is None:
            continue
        # A correction at the same valid time has different content and therefore
        # a different hash. Identical repeated reports collapse only here.
        records.setdefault(str(record["raw_sha256"]), record)
    return [records[key] for key in sorted(records)]


def _object_path(root: Path, raw_sha256: str) -> Path:
    return root / "objects" / raw_sha256[:2] / f"{raw_sha256}.json.gz"


def _object_reference(raw_sha256: str) -> str:
    return f"objects/{raw_sha256[:2]}/{raw_sha256}.json.gz"


def _assert_existing_object(path: Path, raw_sha256: str) -> dict[str, Any]:
    stored = _read_json_gz(path)
    raw_text = str(stored.get("raw_text") or "")
    if stored.get("schema") != RAW_OBJECT_SCHEMA or raw_text_sha256(raw_text) != raw_sha256:
        raise ValueError(f"raw_archive_object_integrity_failure:{path}")
    return stored


def archive_amsc_poll(
    *,
    archive_dir: str | Path,
    source_payloads: Sequence[Mapping[str, Any]],
    poll_started_at: str,
    response_completed_at: str,
    response_started_at: str = "",
    response_headers_observed: bool = False,
    response_status: int | str,
    request_metadata: Mapping[str, Any] | None,
    fetcher_version: str,
    endpoint_id: str = "amsc_packet_retrieval_message_retrieval_sort",
    materialize_daily_coverage: bool = True,
) -> dict[str, Any]:
    """Persist one sanitized poll manifest and any previously unseen report objects.

    This is forward archival only. It never writes archive.db, fact tables,
    posterior data, or execution records.
    """

    root = Path(archive_dir).expanduser()
    records = archive_source_records(source_payloads)
    completed_at = str(response_completed_at or _iso_now()).strip()
    started_at = str(poll_started_at or completed_at).strip()
    objects_written = 0
    objects_reused = 0
    object_index: dict[str, dict[str, Any]] = {}

    for record in records:
        raw_sha256 = str(record["raw_sha256"])
        path = _object_path(root, raw_sha256)
        if path.exists():
            stored = _assert_existing_object(path, raw_sha256)
            objects_reused += 1
            first_seen_at = str(stored.get("first_seen_at") or completed_at)
        else:
            first_seen_at = completed_at
            object_payload = {
                "schema": RAW_OBJECT_SCHEMA,
                "source_id": record["source_id"],
                "station_id": record["station_id"],
                "report_type": record["report_type"],
                "raw_text": record["raw_text"],
                "raw_sha256": raw_sha256,
                "verified_valid_time_utc": record["verified_valid_time_utc"],
                "verified_valid_time_local": record["verified_valid_time_local"],
                "first_seen_at": first_seen_at,
                "arrival_time_quality": "observed_first_seen",
                "archive_writer": "amsc_raw_history_archive_v1",
            }
            _atomic_write_bytes(path, gzip.compress(_canonical_json_bytes(object_payload), mtime=0))
            objects_written += 1
        object_index[raw_sha256] = {
            "raw_sha256": raw_sha256,
            "raw_object_ref": _object_reference(raw_sha256),
            "first_seen_at": first_seen_at,
            "arrival_time_quality": "observed_first_seen",
            "verified_valid_time_utc": record["verified_valid_time_utc"],
            "verified_valid_time_local": record["verified_valid_time_local"],
        }

    manifest_input = {
        "schema": POLL_MANIFEST_SCHEMA,
        "poll_started_at": started_at,
        "response_completed_at": completed_at,
        "endpoint_id": str(endpoint_id or "").strip(),
        "sanitized_query": sanitize_metadata(dict(request_metadata or {})),
        "response_status": response_status,
        "returned_raw_sha256_list": sorted(object_index),
        "returned_raw_count": len(object_index),
        "fetcher_version": str(fetcher_version or "").strip(),
    }
    if str(response_started_at or "").strip():
        manifest_input["response_started_at"] = str(response_started_at).strip()
    manifest_input["response_headers_observed"] = response_headers_observed is True
    manifest_core = _poll_manifest_core(manifest_input)
    manifest_sha256 = hashlib.sha256(_canonical_json_bytes(manifest_core)).hexdigest()
    year, month, day = _date_parts(completed_at)
    manifest_path = root / "manifests" / year / month / day / f"{_safe_timestamp(completed_at)}_{manifest_sha256[:16]}.json"
    manifest_payload = {
        **manifest_core,
        "manifest_sha256": manifest_sha256,
        "raw_objects": [object_index[key] for key in sorted(object_index)],
        "archive_writer": "amsc_raw_history_archive_v1",
    }
    if not manifest_path.exists():
        _atomic_write_bytes(manifest_path, _canonical_json_bytes(manifest_payload) + b"\n")

    # Daily coverage is an audit/training derivative. Production observation
    # ingestion may defer its full-archive scan until after the T1 -> T3
    # probability transaction; raw objects and the signed poll manifest are
    # already durable and sufficient for the strict live reader.
    coverage_manifest_refs = (
        write_amsc_daily_coverage_snapshots(
            root,
            source_records=records,
            snapshot_generated_at=completed_at,
        )
        if materialize_daily_coverage
        else []
    )

    return {
        "schema": ARCHIVE_RESULT_SCHEMA,
        "archive_dir": str(root),
        "manifest_ref": str(manifest_path.relative_to(root)),
        "manifest_sha256": manifest_sha256,
        "objects_written": objects_written,
        "objects_reused": objects_reused,
        "raw_object_count": len(object_index),
        "raw_objects_by_sha256": object_index,
        "coverage_manifest_refs": coverage_manifest_refs,
        "daily_coverage_state": "materialized" if materialize_daily_coverage else "deferred_after_probability_t3",
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "historical_backfill": False,
            "credentials_persisted": False,
        },
    }


def attach_archive_metadata(
    source_payloads: Sequence[Mapping[str, Any]],
    archive_result: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Attach immutable archive lineage to normalized source payloads."""

    by_sha = dict((archive_result or {}).get("raw_objects_by_sha256") or {})
    manifest_ref = str((archive_result or {}).get("manifest_ref") or "")
    manifest_sha256 = str((archive_result or {}).get("manifest_sha256") or "")
    attached: list[dict[str, Any]] = []
    for source in source_payloads:
        row = dict(source)
        raw_text = str(row.get("raw_text") or row.get("raw_ref") or "").strip()
        raw_sha256 = raw_text_sha256(raw_text) if raw_text else ""
        metadata = by_sha.get(raw_sha256)
        if metadata:
            row["raw_sha256"] = raw_sha256
            row["raw_object_ref"] = str(metadata.get("raw_object_ref") or "")
            row["first_seen_at"] = str(metadata.get("first_seen_at") or "")
            row["arrival_time_quality"] = str(metadata.get("arrival_time_quality") or "")
            # A repeated poll may discover an already archived physical
            # report long after its immutable T1.  Once raw archive metadata
            # is attached, the strict-as-of eligibility clock must be that
            # original receipt as well; retaining this poll's fetched time
            # would create a false late observation or an inconsistent
            # first_seen/as_of pair downstream.
            row["as_of_eligible_at"] = row["first_seen_at"]
            row["verified_metar_valid_time"] = str(metadata.get("verified_valid_time_utc") or row.get("valid_time_utc") or "")
            transport = dict(row.get("transport") or {})
            transport.update(
                {
                    "raw_object_ref": row["raw_object_ref"],
                    "raw_archive_manifest_ref": manifest_ref,
                    "raw_archive_manifest_sha256": manifest_sha256,
                }
            )
            row["transport"] = transport
        attached.append(row)
    return attached


def audit_amsc_raw_history_archive(archive_dir: str | Path) -> dict[str, Any]:
    """Read-only integrity and credential-leak audit for a raw archive."""

    root = Path(archive_dir).expanduser()
    issues: list[str] = []
    manifests = sorted((root / "manifests").glob("**/*.json")) if (root / "manifests").exists() else []
    object_paths = sorted((root / "objects").glob("**/*.json.gz")) if (root / "objects").exists() else []
    # v1 `coverage/` assumed a false 48-slot grid.  It is intentionally not
    # read as a compatibility path; only v2 schedule-bound snapshots may
    # affect any final-source decision.
    coverage_root = root / DAILY_COVERAGE_DIRECTORY
    coverage_paths = sorted(coverage_root.glob("**/*.json")) if coverage_root.exists() else []
    object_hashes: set[str] = set()
    for path in object_paths:
        try:
            stored = _read_json_gz(path)
            raw_sha256 = str(stored.get("raw_sha256") or "")
            if not raw_sha256 or raw_text_sha256(stored.get("raw_text")) != raw_sha256:
                issues.append(f"object_hash_invalid:{path.relative_to(root)}")
                continue
            object_hashes.add(raw_sha256)
            if SENSITIVE_KEY_RE.search(" ".join(str(key) for key in stored)):
                issues.append(f"object_sensitive_key:{path.relative_to(root)}")
        except Exception:
            issues.append(f"object_unreadable:{path.relative_to(root)}")
    for path in manifests:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, Mapping):
                raise ValueError("not_mapping")
            manifest_contract = validate_amsc_raw_poll_manifest(manifest)
            for reason in manifest_contract.get("issues") or []:
                issues.append(f"manifest_contract_invalid:{path.relative_to(root)}:{reason}")
            for raw_sha256 in manifest.get("returned_raw_sha256_list") or []:
                if str(raw_sha256) not in object_hashes:
                    issues.append(f"manifest_object_missing:{path.relative_to(root)}:{raw_sha256}")
        except Exception:
            issues.append(f"manifest_unreadable:{path.relative_to(root)}")
    for path in coverage_paths:
        try:
            coverage = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(coverage, Mapping):
                raise ValueError("not_mapping")
            if coverage.get("schema") != DAILY_COVERAGE_SCHEMA:
                issues.append(f"coverage_schema_invalid:{path.relative_to(root)}")
                continue
            if SENSITIVE_KEY_RE.search(" ".join(str(key) for key in coverage)):
                issues.append(f"coverage_sensitive_key:{path.relative_to(root)}")
            core = _daily_coverage_core(coverage)
            coverage_contract = validate_amsc_daily_coverage_snapshot(coverage)
            for reason in coverage_contract.get("issues") or []:
                label = "coverage_hash_invalid" if reason == "coverage_sha256_mismatch" else reason
                issues.append(f"{label}:{path.relative_to(root)}")
            for raw_sha256 in core["raw_object_sha256_list"]:
                if str(raw_sha256) not in object_hashes:
                    issues.append(f"coverage_object_missing:{path.relative_to(root)}:{raw_sha256}")
        except Exception:
            issues.append(f"coverage_unreadable:{path.relative_to(root)}")
    return {
        "schema": "amsc_raw_history_archive_audit_v1",
        "ok": not issues,
        "archive_dir": str(root),
        "object_count": len(object_paths),
        "manifest_count": len(manifests),
        "coverage_manifest_count": len(coverage_paths),
        "issues": issues,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "places_orders": False,
            "historical_backfill": False,
        },
    }


def build_amsc_daily_coverage_manifest(
    archive_dir: str | Path,
    *,
    event_date: str,
    station_id: str = "ZUUU",
    late_arrival_tolerance_seconds: int = 5 * 60,
) -> dict[str, Any]:
    """Build a read-only raw-METAR coverage manifest for one station-local day.

    The result intentionally describes availability rather than smoothing it.
    A provider-window report first seen after its valid time remains visible as
    late; it must not be promoted to an A-class as-of replay point merely
    because the provider still returned it later.
    """

    root = Path(archive_dir).expanduser()
    normalized_date = str(event_date or "").strip()
    target_station = str(station_id or "ZUUU").strip().upper()
    issues: list[str] = []
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized_date):
        issues.append("event_date_invalid")
    if not target_station:
        issues.append("station_id_missing")

    object_paths = sorted((root / "objects").glob("**/*.json.gz")) if (root / "objects").exists() else []
    target_rows: list[dict[str, Any]] = []
    malformed_object_count = 0
    non_target_station_count = 0
    non_metar_count = 0
    local_utc_date_mismatch_count = 0
    for path in object_paths:
        try:
            value = _read_json_gz(path)
        except Exception:
            malformed_object_count += 1
            continue
        if str(value.get("station_id") or "").strip().upper() != target_station:
            non_target_station_count += 1
            continue
        if str(value.get("report_type") or "").strip().upper() != "METAR":
            non_metar_count += 1
            continue
        valid_at = _parse_iso(value.get("verified_valid_time_utc"))
        valid_local_text = _parse_iso(value.get("verified_valid_time_local"))
        if valid_at is None:
            malformed_object_count += 1
            continue
        valid_day = _station_day(valid_at)
        if valid_local_text is not None and _station_day(valid_local_text) != valid_day:
            local_utc_date_mismatch_count += 1
        if valid_day != normalized_date:
            continue
        first_seen_at = _parse_iso(value.get("first_seen_at"))
        target_rows.append(
            {
                "raw_sha256": str(value.get("raw_sha256") or ""),
                "raw_object_ref": str(path.relative_to(root)),
                "valid_at": valid_at,
                "first_seen_at": first_seen_at,
                "arrival_time_quality": str(value.get("arrival_time_quality") or ""),
            }
        )

    target_rows.sort(key=lambda row: (row["valid_at"], row["raw_sha256"]))
    by_valid_time: dict[str, list[dict[str, Any]]] = {}
    for row in target_rows:
        by_valid_time.setdefault(_iso_utc(row["valid_at"]), []).append(row)
    valid_times = sorted(_parse_iso(value) for value in by_valid_time)
    valid_times = [value for value in valid_times if value is not None]
    gaps = [
        (right - left).total_seconds()
        for left, right in zip(valid_times, valid_times[1:])
        if right >= left
    ]
    correction_groups = [
        {
            "verified_valid_time_utc": valid_time,
            "raw_sha256_list": sorted({str(row["raw_sha256"]) for row in rows}),
        }
        for valid_time, rows in sorted(by_valid_time.items())
        if len({str(row["raw_sha256"]) for row in rows}) > 1
    ]
    correction_count = sum(max(0, len(row["raw_sha256_list"]) - 1) for row in correction_groups)
    arrival_lags = [
        (row["first_seen_at"] - row["valid_at"]).total_seconds()
        for row in target_rows
        if row["first_seen_at"] is not None
    ]
    late_count = sum(1 for value in arrival_lags if value > max(0, int(late_arrival_tolerance_seconds)))
    missing_first_seen_count = sum(1 for row in target_rows if row["first_seen_at"] is None)
    quality_counts: dict[str, int] = {}
    for row in target_rows:
        quality = str(row["arrival_time_quality"] or "missing")
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
    now_day = datetime.now(timezone.utc).astimezone(STATION_TZ).date().isoformat()
    slot_coverage = validate_regular_metar_slot_coverage(
        event_date=normalized_date,
        observed_valid_times_utc=[_iso_utc(value) for value in valid_times],
    )
    missing_regular_slots = list(slot_coverage.get("missing_regular_metar_slots_utc") or [])
    unexpected_regular_slots = list(slot_coverage.get("unexpected_regular_metar_slots_utc") or [])
    if normalized_date >= now_day:
        day_state = "in_progress"
    elif not target_rows:
        day_state = "no_raw_metar"
    elif late_count or missing_first_seen_count or missing_regular_slots or unexpected_regular_slots:
        day_state = "historical_window_or_late_arrival_only"
    else:
        day_state = "forward_arrival_complete_pending_label"

    archive_integrity = audit_amsc_raw_history_archive(root)
    core = {
        "schema": DAILY_COVERAGE_SCHEMA,
        "event_date": normalized_date,
        "station_id": target_station,
        "raw_object_sha256_list": sorted({str(row["raw_sha256"]) for row in target_rows}),
        "distinct_valid_time_count": len(by_valid_time),
        "first_valid_time_utc": _iso_utc(valid_times[0]) if valid_times else "",
        "last_valid_time_utc": _iso_utc(valid_times[-1]) if valid_times else "",
        "max_gap_seconds": int(max(gaps)) if gaps else None,
        "correction_count": correction_count,
        "late_arrival_count": late_count,
        "missing_first_seen_count": missing_first_seen_count,
        "day_state": day_state,
        "reporting_schedule_schema": SCHEDULE_SCHEMA,
        "reporting_schedule_id": SCHEDULE_ID,
        "expected_regular_metar_slot_count": slot_coverage.get("expected_regular_metar_slot_count"),
        "expected_regular_metar_slots_utc": list(slot_coverage.get("expected_regular_metar_slots_utc") or []),
        "missing_regular_metar_slots_utc": missing_regular_slots,
        "unexpected_regular_metar_slots_utc": unexpected_regular_slots,
    }
    coverage_sha256 = hashlib.sha256(_canonical_json_bytes(core)).hexdigest()
    return {
        **core,
        "coverage_sha256": coverage_sha256,
        "event_raw_object_count": len(target_rows),
        "arrival_time_quality_counts": dict(sorted(quality_counts.items())),
        "arrival_lag_seconds": {
            "p50": _percentile(arrival_lags, 0.50),
            "p90": _percentile(arrival_lags, 0.90),
            "max": _percentile(arrival_lags, 1.0),
            "late_tolerance_seconds": max(0, int(late_arrival_tolerance_seconds)),
        },
        "correction_groups": correction_groups,
        "date_quality": {
            "local_utc_date_mismatch_count": local_utc_date_mismatch_count,
            "non_target_station_object_count": non_target_station_count,
            "non_metar_object_count": non_metar_count,
            "malformed_object_count": malformed_object_count,
        },
        "raw_archive_integrity": {
            "ok": bool(archive_integrity.get("ok")),
            "issues": list(archive_integrity.get("issues") or []),
        },
        "replay_eligibility": {
            "a_class_as_of_replay_eligible": bool(
                target_rows
                and not late_count
                and not missing_first_seen_count
                and not local_utc_date_mismatch_count
                and not missing_regular_slots
                and not unexpected_regular_slots
                and bool(archive_integrity.get("ok"))
            ),
            "reason": (
                "all_scheduled_regular_metar_points_arrived_within_tolerance"
                if target_rows and not late_count and not missing_first_seen_count and not local_utc_date_mismatch_count and not missing_regular_slots and not unexpected_regular_slots and archive_integrity.get("ok")
                else "late_or_missing_arrival_or_schedule_or_date_or_integrity_constraint"
            ),
        },
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "historical_backfill": False,
        },
        "issues": sorted(set(issues)),
    }


def write_amsc_daily_coverage_snapshots(
    archive_dir: str | Path,
    *,
    source_records: Sequence[Mapping[str, Any]],
    snapshot_generated_at: str = "",
) -> list[str]:
    """Persist immutable, content-addressed coverage snapshots for touched METAR days.

    This is an archive-side derivative only: it does not alter raw objects,
    source observations, facts, posterior state, or any execution record.
    """

    root = Path(archive_dir).expanduser()
    station_event_dates: set[tuple[str, str]] = set()
    for record in source_records:
        if str(record.get("report_type") or "").strip().upper() != "METAR":
            continue
        valid_at = _parse_iso(record.get("verified_valid_time_utc"))
        station_id = str(record.get("station_id") or "").strip().upper()
        if valid_at is not None and station_id:
            station_event_dates.add((station_id, _station_day(valid_at)))
    refs: list[str] = []
    for station_id, event_date in sorted(station_event_dates):
        coverage = build_amsc_daily_coverage_manifest(
            root,
            event_date=event_date,
            station_id=station_id,
        )
        year, month, day = event_date.split("-")
        digest = str(coverage.get("coverage_sha256") or "")
        if not digest:
            raise ValueError("coverage_manifest_hash_missing")
        path = root / DAILY_COVERAGE_DIRECTORY / year / month / day / f"{digest}.json"
        if not path.exists():
            snapshot = {
                **coverage,
                "snapshot_generated_at": str(snapshot_generated_at or _iso_now()),
                "snapshot_writer": "amsc_raw_history_archive_v1",
            }
            _atomic_write_bytes(path, _canonical_json_bytes(snapshot) + b"\n")
        refs.append(str(path.relative_to(root)))
    return refs


def persist_amsc_daily_coverage_snapshot(
    archive_dir: str | Path,
    *,
    event_date: str,
    station_id: str = "ZUUU",
    late_arrival_tolerance_seconds: int = 5 * 60,
    snapshot_generated_at: str = "",
) -> dict[str, Any]:
    """Materialize one immutable coverage-v2 derivative from archived raw objects.

    This controlled path exists for station days whose immutable AMSC raw
    objects were archived before the v2 coverage writer existed.  It never
    reads the retired ``coverage/`` tree and never changes raw objects,
    poll manifests, facts, posterior state, decisions, or orders.  Historical
    late-arrival state is retained exactly as reconstructed; materialization
    does not promote a station day to strict-as-of eligibility.
    """

    root = Path(archive_dir).expanduser()
    normalized_date = str(event_date or "").strip()
    normalized_station = str(station_id or "").strip().upper()
    try:
        parsed_date = datetime.strptime(normalized_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("coverage_materialization_event_date_invalid") from exc
    if parsed_date.strftime("%Y-%m-%d") != normalized_date:
        raise ValueError("coverage_materialization_event_date_invalid")
    if normalized_station != "ZUUU":
        raise ValueError("coverage_materialization_station_not_supported_by_schedule")
    generated_at = str(snapshot_generated_at or _iso_now()).strip()
    if _parse_iso(generated_at) is None:
        raise ValueError("coverage_materialization_snapshot_generated_at_invalid")

    coverage = build_amsc_daily_coverage_manifest(
        root,
        event_date=normalized_date,
        station_id=normalized_station,
        late_arrival_tolerance_seconds=late_arrival_tolerance_seconds,
    )
    if coverage.get("issues"):
        raise ValueError(f"coverage_materialization_manifest_invalid:{','.join(coverage['issues'])}")
    if int(coverage.get("event_raw_object_count") or 0) <= 0:
        raise ValueError("coverage_materialization_no_raw_metar")
    if (coverage.get("raw_archive_integrity") or {}).get("ok") is not True:
        raise ValueError("coverage_materialization_raw_archive_integrity_failed")
    if (
        coverage.get("schema") != DAILY_COVERAGE_SCHEMA
        or coverage.get("reporting_schedule_schema") != SCHEDULE_SCHEMA
        or coverage.get("reporting_schedule_id") != SCHEDULE_ID
        or coverage.get("expected_regular_metar_slot_count") != EXPECTED_REGULAR_SLOT_COUNT
    ):
        raise ValueError("coverage_materialization_reporting_schedule_invalid")
    expected_schedule = build_zuuu_primary_metar_schedule_contract(normalized_date)
    if list(coverage.get("expected_regular_metar_slots_utc") or []) != list(
        expected_schedule.get("expected_regular_metar_slots_utc") or []
    ):
        raise ValueError("coverage_materialization_reporting_schedule_slots_invalid")

    digest = str(coverage.get("coverage_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("coverage_materialization_hash_invalid")
    year, month, day = normalized_date.split("-")
    path = root / DAILY_COVERAGE_DIRECTORY / year / month / day / f"{digest}.json"
    snapshot = {
        **coverage,
        "snapshot_generated_at": generated_at,
        "snapshot_writer": DAILY_COVERAGE_MATERIALIZATION_WRITER,
    }
    written = False
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("coverage_materialization_existing_snapshot_unreadable") from exc
        if not isinstance(existing, Mapping) or str(existing.get("coverage_sha256") or "") != digest:
            raise ValueError("coverage_materialization_existing_snapshot_identity_mismatch")
        if validate_amsc_daily_coverage_snapshot(existing).get("ok") is not True:
            raise ValueError("coverage_materialization_existing_snapshot_contract_invalid")
        if (
            str(existing.get("event_date") or "") != normalized_date
            or str(existing.get("station_id") or "").upper() != normalized_station
            or list(existing.get("raw_object_sha256_list") or [])
            != list(coverage.get("raw_object_sha256_list") or [])
        ):
            raise ValueError("coverage_materialization_existing_snapshot_content_mismatch")
        snapshot = dict(existing)
    else:
        _atomic_write_bytes(path, _canonical_json_bytes(snapshot) + b"\n")
        written = True

    return {
        "schema": DAILY_COVERAGE_MATERIALIZATION_SCHEMA,
        "ok": True,
        "state": "materialized" if written else "idempotent_reuse",
        "archive_dir": str(root),
        "coverage_manifest_ref": str(path.relative_to(root)),
        "coverage_sha256": digest,
        "snapshot_written": written,
        "snapshot_reused": not written,
        "coverage": snapshot,
        "guards": {
            "reads_legacy_coverage": False,
            "writes_raw_objects": False,
            "writes_poll_manifests": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "promotes_strict_asof": False,
        },
    }


def load_amsc_daily_coverage_snapshot(
    archive_dir: str | Path,
    *,
    event_date: str,
    station_id: str = "ZUUU",
) -> dict[str, Any] | None:
    """Load the deterministic latest persisted coverage snapshot for one day.

    Selection is by snapshot generation time, then retained raw-object coverage
    and latest valid time.  The chosen hash is carried into every sample manifest,
    so a later archive expansion cannot silently rewrite an earlier sample.
    """

    root = Path(archive_dir).expanduser()
    normalized_date = str(event_date or "").strip()
    normalized_station = str(station_id or "").strip().upper()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized_date) or not normalized_station:
        return None
    year, month, day = normalized_date.split("-")
    paths = sorted((root / DAILY_COVERAGE_DIRECTORY / year / month / day).glob("*.json"))
    candidates: list[dict[str, Any]] = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(value, Mapping):
            continue
        if value.get("schema") != DAILY_COVERAGE_SCHEMA:
            continue
        if str(value.get("event_date") or "") != normalized_date:
            continue
        if str(value.get("station_id") or "").upper() != normalized_station:
            continue
        candidates.append(dict(value))
    if not candidates:
        return None
    selected = max(
        candidates,
        key=lambda value: (
            _parse_iso(value.get("snapshot_generated_at")) or datetime.min.replace(tzinfo=timezone.utc),
            len(value.get("raw_object_sha256_list") or []),
            str(value.get("last_valid_time_utc") or ""),
            str(value.get("coverage_sha256") or ""),
        ),
    )
    return selected
