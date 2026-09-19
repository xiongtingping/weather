"""Read-only export of canonical AWC observations for the target lane.

The resident ``archive.db`` is an owner-controlled live database.  This
module is the only target-side boundary that may read it: it opens SQLite in
read-only/query-only mode and emits a content-addressed evidence object.  The
target runtime consumes that object, never the shared database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, content_sha256
from .resource_bounds import (
    assert_process_memory_budget,
    write_bounded_json_if_changed,
)
from .source_policy import (
    AWC_METAR_TIMING_INVALID,
    SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN,
    normalize_station_lane_map,
    report_seen_within_early_transmission_tolerance,
    source_policy_reason,
)


SCHEMA = "bayes_basket_target_canonical_observation_evidence_v1"
TABLE = "reported_trajectory_observations"
POLL_TABLE = "reported_trajectory_source_polls"
REQUIRED_COLUMNS = (
    "observation_id",
    "source_lane",
    "station_id",
    "report_type",
    "valid_at",
    "reported_temperature_c",
    "raw_sha256",
    "first_seen_at",
    "parsed_at",
    "canonical_committed_at",
    "fetch_started_at",
    "fetch_completed_at",
    "source_poll_id",
    "correction_suspected",
)
MAX_CANONICAL_SOURCE_ROWS = 20_000
CANONICAL_FETCH_BATCH_ROWS = 256
MAX_CANONICAL_EVIDENCE_NODES = 1_000_000
MAX_CANONICAL_EVIDENCE_BYTES = 64 * 1024 * 1024


def _reference_only_reason(
    *,
    source_lane: str,
    report_type: str,
    valid_at: datetime,
    expected_lane: str = "",
) -> str:
    """Return the source-policy reason for a non-formal observation.

    AWC is the formal global lane after the 2026-08-01 SSOT revision.  Its
    native valid-time cadence is preserved; retired AMSC rows remain audit
    evidence and cannot be mixed into a live station scope.
    """

    reason = source_policy_reason(
        source_lane=source_lane,
        report_type=report_type,
        valid_at=valid_at,
        expected_lane=expected_lane,
    )
    return reason if reason.endswith("reference_only") or "reference_only" in reason else ""


def _timestamp(name: str, value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ContractError(f"{name}_missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _station_day(station_id: str, valid_at: datetime, station_timezones: Mapping[str, str]) -> tuple[str, str]:
    timezone_name = str(station_timezones.get(station_id) or "").strip()
    if not timezone_name:
        raise ContractError(f"station_timezone_missing:{station_id}")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ContractError(f"station_timezone_invalid:{station_id}") from exc
    return valid_at.astimezone(zone).date().isoformat(), timezone_name


def _canonical_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": str(row["observation_id"]),
        "source_lane": str(row["source_lane"]).upper(),
        "station_id": str(row["station_id"]),
        "report_type": str(row["report_type"]).upper(),
        "valid_at_utc": str(row["valid_at_utc"]),
        "reported_temperature_c": int(row["reported_temperature_c"]),
        "raw_sha256": str(row["raw_sha256"]),
        "first_seen_at_utc": str(row["first_seen_at_utc"]),
        "parsed_at_utc": str(row["parsed_at_utc"]),
        "canonical_committed_at_utc": str(row["canonical_committed_at_utc"]),
        "fetch_started_at_utc": str(row["fetch_started_at_utc"]),
        "fetch_completed_at_utc": str(row["fetch_completed_at_utc"]),
        "source_poll_id": str(row["source_poll_id"]),
        "awc_raw_object_sha256": str(row["awc_raw_object_sha256"]),
        "awc_source_receipt_sha256": str(row["awc_source_receipt_sha256"]),
        "correction_suspected": int(row["correction_suspected"]),
    }


_POLL_RECEIPT_MEMO: dict[tuple[Any, ...], str] = {}
_POLL_RECEIPT_MEMO_LIMIT = 50_000


def _source_poll_receipt(raw: Mapping[str, Any], *, expected_lane: str) -> str:
    """The poll's receipt hash, once per distinct poll row in a process.

    A pure function of the poll fields below, and the same poll arrives once
    per report it carried: 5,590 reports against 2,829 polls in a three-day
    window (2026-09-19), each paying a canonical-JSON hash of the whole
    provider payload.  The memo is keyed on every field the receipt reads
    (the two JSON columns by digest), so a changed row is a new key.
    """

    key = (
        expected_lane,
        raw.get("source_poll_id"), raw.get("poll_source_lane"),
        raw.get("poll_request_started_at"), raw.get("poll_response_started_at"),
        raw.get("poll_response_completed_at"), raw.get("poll_response_status"),
        raw.get("poll_duration_ms"), raw.get("poll_report_count"),
        raw.get("poll_new_report_count"), raw.get("poll_error_code"),
        hashlib.sha256(str(raw.get("poll_station_ids_json") or "").encode()).hexdigest(),
        hashlib.sha256(str(raw.get("poll_json") or "").encode()).hexdigest(),
    )
    cached = _POLL_RECEIPT_MEMO.get(key)
    if cached is not None:
        return cached
    receipt = _source_poll_receipt_uncached(raw, expected_lane=expected_lane)
    if len(_POLL_RECEIPT_MEMO) >= _POLL_RECEIPT_MEMO_LIMIT:
        _POLL_RECEIPT_MEMO.clear()
    _POLL_RECEIPT_MEMO[key] = receipt
    return receipt


def _source_poll_receipt_uncached(raw: Mapping[str, Any], *, expected_lane: str) -> str:
    try:
        station_ids = json.loads(str(raw.get("poll_station_ids_json") or ""))
        poll_json = json.loads(str(raw.get("poll_json") or ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError("canonical_source_poll_receipt_json_invalid") from exc
    if not isinstance(station_ids, list) or not isinstance(poll_json, dict):
        raise ContractError("canonical_source_poll_receipt_shape_invalid")
    lane = str(raw.get("poll_source_lane") or "").strip().lower()
    if lane != expected_lane:
        raise ContractError("canonical_source_poll_lane_mismatch")
    identity = {
        "poll_id": str(raw.get("source_poll_id") or ""),
        "source_lane": lane,
        "station_ids": sorted(str(item).strip().upper() for item in station_ids),
        "request_started_at_utc": _timestamp_text(
            _timestamp("poll_request_started_at", raw.get("poll_request_started_at"))
        ),
        "response_started_at_utc": (
            _timestamp_text(
                _timestamp("poll_response_started_at", raw.get("poll_response_started_at"))
            )
            if raw.get("poll_response_started_at") else None
        ),
        "response_completed_at_utc": _timestamp_text(
            _timestamp("poll_response_completed_at", raw.get("poll_response_completed_at"))
        ),
        "response_status": raw.get("poll_response_status"),
        "duration_ms": float(raw.get("poll_duration_ms")),
        "report_count": int(raw.get("poll_report_count")),
        "new_report_count": int(raw.get("poll_new_report_count")),
        "error_code": raw.get("poll_error_code"),
        "poll_payload": poll_json,
    }
    if not identity["poll_id"] or identity["response_status"] not in {200, 206}:
        raise ContractError("canonical_source_poll_receipt_not_successful")
    return content_sha256(identity)


def _read_source_rows(
    source_db: Path,
    *,
    station_ids: Sequence[str] = (),
    source_lanes: Sequence[str] = (),
    as_of_utc: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = Path(source_db).expanduser().resolve()
    if not source.is_file():
        raise ContractError("canonical_source_database_missing")
    clauses: list[str] = []
    params: list[Any] = []
    normalized_stations = sorted({str(item).strip() for item in station_ids if str(item).strip()})
    normalized_lanes = sorted({str(item).strip().lower() for item in source_lanes if str(item).strip()})
    if normalized_stations:
        clauses.append("o.station_id IN (" + ",".join("?" for _ in normalized_stations) + ")")
        params.extend(normalized_stations)
    if normalized_lanes:
        invalid = set(normalized_lanes) - {"amsc", "awc"}
        if invalid:
            raise ContractError("canonical_source_lane_filter_invalid")
        clauses.append("o.source_lane IN (" + ",".join("?" for _ in normalized_lanes) + ")")
        params.extend(normalized_lanes)
    if as_of_utc:
        as_of = _timestamp("as_of_utc", as_of_utc)
        clauses.append("o.canonical_committed_at <= ?")
        params.append(_timestamp_text(as_of))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = (
        "SELECT o.observation_id,o.source_lane,o.station_id,o.report_type,o.valid_at,"
        "o.reported_temperature_c,o.raw_sha256,o.first_seen_at,o.parsed_at,o.canonical_committed_at,"
        "o.fetch_started_at,o.fetch_completed_at,o.source_poll_id,o.correction_suspected "
        f"FROM {TABLE} o{where} "
        "ORDER BY o.source_lane,o.station_id,o.valid_at,o.report_type,o.observation_id "
        "LIMIT ?"
    )
    query_params = (*params, MAX_CANONICAL_SOURCE_ROWS + 1)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        table_columns = {
            str(item[1])
            for item in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
        }
        missing = sorted(set(REQUIRED_COLUMNS) - table_columns)
        if missing:
            raise ContractError("canonical_source_schema_missing:" + ",".join(missing))
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (POLL_TABLE,),
        ).fetchone() is None:
            raise ContractError("canonical_source_poll_table_missing")
        cursor = conn.execute(sql, query_params)
        rows: list[dict[str, Any]] = []
        while True:
            batch = cursor.fetchmany(CANONICAL_FETCH_BATCH_ROWS)
            if not batch:
                break
            rows.extend(dict(row) for row in batch)
            if len(rows) > MAX_CANONICAL_SOURCE_ROWS:
                raise ContractError("canonical_source_row_limit_exceeded")
            assert_process_memory_budget()
        poll_ids = sorted({str(row["source_poll_id"]) for row in rows})
        poll_hashes: dict[tuple[str, str], tuple[str, str]] = {}
        for offset in range(0, len(poll_ids), 400):
            batch_ids = poll_ids[offset : offset + 400]
            poll_sql = (
                "SELECT poll_id AS source_poll_id,source_lane AS poll_source_lane,"
                "station_ids_json AS poll_station_ids_json,"
                "request_started_at AS poll_request_started_at,"
                "response_started_at AS poll_response_started_at,"
                "response_completed_at AS poll_response_completed_at,"
                "response_status AS poll_response_status,duration_ms AS poll_duration_ms,"
                "report_count AS poll_report_count,new_report_count AS poll_new_report_count,"
                "error_code AS poll_error_code,poll_json FROM reported_trajectory_source_polls "
                "WHERE poll_id IN (" + ",".join("?" for _ in batch_ids) + ")"
            )
            for poll in conn.execute(poll_sql, batch_ids).fetchall():
                mapped = dict(poll)
                lane = str(mapped["poll_source_lane"]).lower()
                poll_payload = json.loads(str(mapped["poll_json"]))
                poll_hashes[(str(mapped["source_poll_id"]), lane)] = (
                    _source_poll_receipt(mapped, expected_lane=lane),
                    content_sha256(poll_payload),
                )
        for row in rows:
            key = (str(row["source_poll_id"]), str(row["source_lane"]).lower())
            binding = poll_hashes.get(key)
            if binding is None:
                raise ContractError("canonical_source_poll_receipt_missing")
            row["awc_source_receipt_sha256"] = binding[0]
            row["awc_raw_object_sha256"] = binding[1]
    return rows, {
        "source_database_path": str(source),
        "source_rows_read": len(rows),
        "source_read_only": True,
        "source_query_only": True,
        "filters": {
            "station_ids": normalized_stations,
            "source_lanes": normalized_lanes,
            "as_of_utc": as_of_utc,
        },
        "resource_bounds": {
            "max_source_rows": MAX_CANONICAL_SOURCE_ROWS,
            "fetch_batch_rows": CANONICAL_FETCH_BATCH_ROWS,
        },
    }


def export_canonical_observation_evidence(
    *,
    source_db: Path,
    output_path: Path | None = None,
    station_ids: Sequence[str] = (),
    source_lanes: Sequence[str] = (),
    station_timezones: Mapping[str, str] | None = None,
    station_lane_map: Mapping[str, Any] | None = None,
    as_of_utc: str = "",
) -> dict[str, Any]:
    """Export canonical observations and station-day truth without writes.

    A correction or same-slot revision is retained as quarantined evidence;
    it never contributes to an accepted station-day truth row.  Structural
    violations (timestamp rewind, lane mixing, duplicate identity) fail the
    whole receipt closed.
    """

    timezone_map = {str(key).strip(): str(value).strip() for key, value in (station_timezones or {}).items()}
    lane_map = normalize_station_lane_map(station_lane_map)
    raw_rows, source_meta = _read_source_rows(
        source_db,
        station_ids=station_ids,
        source_lanes=source_lanes,
        as_of_utc=as_of_utc,
    )
    observations: list[dict[str, Any]] = []
    structural_blockers: list[str] = []
    seen_identity: set[tuple[str, str, str, str, str]] = set()
    slot_to_hashes: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    station_lanes: dict[str, set[str]] = defaultdict(set)
    for raw in raw_rows:
        try:
            lane = str(raw.get("source_lane") or "").strip().lower()
            station = str(raw.get("station_id") or "").strip().upper()
            report_type = str(raw.get("report_type") or "").strip().upper()
            observation_id = str(raw.get("observation_id") or "").strip()
            source_poll_id = str(raw.get("source_poll_id") or "").strip()
            if lane not in {"amsc", "awc"}:
                raise ContractError("source_lane_invalid")
            if not station or not observation_id or not source_poll_id:
                raise ContractError("canonical_observation_identity_missing")
            expected_lane = lane_map.get(station, "")
            if lane_map and not expected_lane:
                raise ContractError("canonical_station_not_in_target_registry")
            if report_type not in {"METAR", "SPECI"}:
                raise ContractError("report_type_invalid")
            valid_at = _timestamp("valid_at", raw.get("valid_at"))
            first_seen = _timestamp("first_seen_at", raw.get("first_seen_at"))
            parsed_at = _timestamp("parsed_at", raw.get("parsed_at"))
            committed_at = _timestamp("canonical_committed_at", raw.get("canonical_committed_at"))
            fetch_started = _timestamp("fetch_started_at", raw.get("fetch_started_at"))
            fetch_completed = _timestamp("fetch_completed_at", raw.get("fetch_completed_at"))
            temperature = int(raw.get("reported_temperature_c"))
            raw_sha = _sha("raw_sha256", raw.get("raw_sha256"))
            source_receipt_sha = _sha(
                "awc_source_receipt_sha256",
                raw.get("awc_source_receipt_sha256"),
            )
            raw_object_sha = _sha(
                "awc_raw_object_sha256", raw.get("awc_raw_object_sha256")
            )
            correction = int(raw.get("correction_suspected") or 0)
            if correction not in {0, 1}:
                raise ContractError("correction_suspected_invalid")
            if not (
                report_seen_within_early_transmission_tolerance(valid_at, first_seen)
                and first_seen <= parsed_at <= committed_at
            ):
                raise ContractError("canonical_observation_timestamp_rewind")
            if not (fetch_started <= first_seen <= fetch_completed <= committed_at):
                raise ContractError("canonical_observation_fetch_timestamp_rewind")
            policy_reason = source_policy_reason(
                source_lane=lane,
                report_type=report_type,
                valid_at=valid_at,
                expected_lane=expected_lane,
            )
            if policy_reason == SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN:
                raise ContractError("canonical_source_lane_cross_fallback_forbidden")
            # Historical receipts can still carry the retired timing-exclusion
            # vocabulary; the live policy no longer produces it.
            timing_exclusion = policy_reason == AWC_METAR_TIMING_INVALID
            if policy_reason and "reference_only" not in policy_reason and not timing_exclusion:
                raise ContractError(policy_reason)
            reference_reason = policy_reason if "reference_only" in policy_reason else ""
            identity_key = (lane, station, report_type, _timestamp_text(valid_at), raw_sha)
            slot_key = (lane, station, report_type, _timestamp_text(valid_at))
            if identity_key in seen_identity:
                structural_blockers.append("duplicate_canonical_observation:" + observation_id)
            seen_identity.add(identity_key)
            slot_to_hashes[slot_key].add(raw_sha)
            station_lanes[station].add(lane)
            observations.append({
                "observation_id": observation_id,
                "source_lane": lane.upper(),
                "station_id": station,
                "report_type": report_type,
                "valid_at_utc": _timestamp_text(valid_at),
                "reported_temperature_c": temperature,
                "raw_sha256": raw_sha,
                "first_seen_at_utc": _timestamp_text(first_seen),
                "parsed_at_utc": _timestamp_text(parsed_at),
                "canonical_committed_at_utc": _timestamp_text(committed_at),
                "fetch_started_at_utc": _timestamp_text(fetch_started),
                "fetch_completed_at_utc": _timestamp_text(fetch_completed),
                "source_poll_id": source_poll_id,
                "awc_raw_object_sha256": raw_object_sha,
                "awc_source_receipt_sha256": source_receipt_sha,
                "correction_suspected": correction,
                "evidence_role": (
                    "AUDIT_ONLY" if timing_exclusion
                    else "REFERENCE_ONLY" if reference_reason
                    else "FORMAL"
                ),
                "reference_reason": reference_reason,
                "policy_exclusion_reason": policy_reason if timing_exclusion else "",
                "qc_status": (
                    "EXCLUDED_POLICY" if timing_exclusion
                    else "REFERENCE_ONLY" if reference_reason
                    else "QUARANTINED" if correction
                    else "ACCEPTED"
                ),
                "quarantine_reasons": ["correction_suspected"] if correction else [],
            })
        except (ContractError, TypeError, ValueError) as exc:
            structural_blockers.append(f"invalid_canonical_observation:{raw.get('observation_id')}:{exc}")

    for station, lanes in sorted(station_lanes.items()):
        expected_lane = lane_map.get(station, "")
        allowed_reference_mix = expected_lane == "amsc" and lanes <= {"amsc", "awc"}
        if len(lanes) > 1 and not allowed_reference_mix:
            structural_blockers.append("station_source_lane_mixed:" + station)
    for slot, hashes in sorted(slot_to_hashes.items()):
        if len(hashes) > 1:
            lane, station, report_type, valid_at = slot
            for item in observations:
                if (
                    item["source_lane"].lower(), item["station_id"], item["report_type"], item["valid_at_utc"]
                ) == (lane, station, report_type, valid_at) and item.get(
                    "qc_status"
                ) != "EXCLUDED_POLICY":
                    item["qc_status"] = "QUARANTINED"
                    item["quarantine_reasons"] = sorted(set(item["quarantine_reasons"]) | {"same_slot_revision"})

    accepted_by_day: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    quarantined_by_day: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    timezone_errors: list[str] = []
    for item in observations:
        try:
            local_date, timezone_name = _station_day(
                item["station_id"], datetime.fromisoformat(item["valid_at_utc"].replace("Z", "+00:00")), timezone_map
            )
            item["event_date_local"] = local_date
            item["station_timezone"] = timezone_name
            if item["qc_status"] in {"REFERENCE_ONLY", "EXCLUDED_POLICY"}:
                continue
            target = accepted_by_day if item["qc_status"] == "ACCEPTED" else quarantined_by_day
            target[(item["source_lane"], item["station_id"], local_date)].append(item)
        except ContractError as exc:
            timezone_errors.append(str(exc))
    structural_blockers.extend(timezone_errors)

    # A correction/revision is a station-day halt.  Keeping earlier reports
    # from that day in an accepted max would silently turn a known revision
    # into calibration truth before manual review.
    quarantined_day_keys = {
        (item["source_lane"], item["station_id"], item["event_date_local"])
        for item in observations
        if item.get("qc_status") == "QUARANTINED" and item.get("event_date_local")
    }
    for item in observations:
        key = (item.get("source_lane"), item.get("station_id"), item.get("event_date_local"))
        if (
            key in quarantined_day_keys
            and item.get("qc_status") not in {"REFERENCE_ONLY", "EXCLUDED_POLICY"}
        ):
            item["qc_status"] = "QUARANTINED"
            item["quarantine_reasons"] = sorted(
                set(item.get("quarantine_reasons") or []) | {"station_day_revision_halt"}
            )
    accepted_by_day.clear()
    quarantined_by_day.clear()
    for item in observations:
        if not item.get("event_date_local"):
            continue
        if item["qc_status"] in {"REFERENCE_ONLY", "EXCLUDED_POLICY"}:
            continue
        target = accepted_by_day if item["qc_status"] == "ACCEPTED" else quarantined_by_day
        target[(item["source_lane"], item["station_id"], item["event_date_local"])].append(item)

    def _truth_rows(grouped: Mapping[tuple[str, str, str], Sequence[dict[str, Any]]], qc_status: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for (lane, station, local_date), items in sorted(grouped.items()):
            items = sorted(items, key=lambda item: (item["valid_at_utc"], item["observation_id"]))
            identity = {
                "station_id": station,
                "local_date": local_date,
                "source_lane": lane,
                "report_ids": [item["observation_id"] for item in items],
                "raw_sha256": [item["raw_sha256"] for item in items],
                "qc_status": qc_status,
            }
            result.append({
                "truth_id": "canonical-truth-" + content_sha256(identity)[:32],
                "station_id": station,
                "event_date": local_date,
                "source_lane": lane,
                "station_timezone": items[0]["station_timezone"],
                "official_tmax_c": max(item["reported_temperature_c"] for item in items),
                "report_count": len(items),
                "observation_ids": [item["observation_id"] for item in items],
                "source_poll_ids": sorted({item["source_poll_id"] for item in items}),
                "raw_sha256": sorted({item["raw_sha256"] for item in items}),
                "source_payload_sha256": content_sha256(
                    {"raw_sha256": sorted({item["raw_sha256"] for item in items})}
                ),
                "published_at_utc": None,
                "received_at_utc": max(item["first_seen_at_utc"] for item in items),
                "canonical_committed_at_utc": max(item["canonical_committed_at_utc"] for item in items),
                "qc_status": qc_status,
                "content_sha256": content_sha256(identity),
            })
        return result

    truth_rows = _truth_rows(accepted_by_day, "ACCEPTED")
    quarantined_truth_rows = _truth_rows(quarantined_by_day, "QUARANTINED")
    identity = {
        "schema": SCHEMA,
        "source": source_meta,
        "source_policy": {
            "station_lane_map_bound": bool(lane_map),
            "awc_global_primary": "FORMAL_PRIMARY_AT_SOURCE_MINUTE",
            "amsc_production_lane": "RETIRED_HISTORY_ONLY",
            "awc_precedence_over_amsc": True,
        },
        "observations": observations,
        "truth_rows": truth_rows,
        "quarantined_truth_rows": quarantined_truth_rows,
        "checks": {
            "strict_timestamp_ordering": not any("timestamp_rewind" in item for item in structural_blockers),
            "fetch_timestamp_ordering": not any("fetch_timestamp_rewind" in item for item in structural_blockers),
            "source_lane_disjoint": not any("station_source_lane_mixed" in item for item in structural_blockers),
            "duplicate_free": not any("duplicate_canonical_observation" in item for item in structural_blockers),
            "revisions_quarantined": all(
                item["qc_status"] == "QUARANTINED"
                for item in observations
                if "same_slot_revision" in item["quarantine_reasons"] or item["correction_suspected"]
            ),
            "canonical_truth_present": bool(truth_rows),
            "promotion_allowed": False,
            "writes_archive_db": False,
            "writes_target_db": False,
            "calls_order_gateway": False,
        },
        "blockers": sorted(set(structural_blockers)),
        "boundary": {
            "reads_shared_live_database": True,
            "shared_live_read_mode": "sqlite_mode_ro_query_only",
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "target_runtime_may_read_archive_db": False,
        },
    }
    result = {
        **identity,
        "state": "EXPORTED" if not structural_blockers else "BLOCKED_FAIL_CLOSED",
        "observation_count": len(observations),
        "accepted_truth_count": len(truth_rows),
        "quarantined_truth_count": len(quarantined_truth_rows),
        "source_lane_counts": {
            lane: sum(1 for item in observations if item["source_lane"] == lane)
            for lane in ("AMSC", "AWC")
        },
        "reference_observation_count": sum(
            1 for item in observations if item.get("evidence_role") == "REFERENCE_ONLY"
        ),
        "excluded_policy_observation_count": sum(
            1 for item in observations if item.get("qc_status") == "EXCLUDED_POLICY"
        ),
        "policy_exclusion_reason_counts": {
            reason: sum(
                1 for item in observations
                if item.get("policy_exclusion_reason") == reason
            )
            for reason in sorted({
                str(item.get("policy_exclusion_reason") or "")
                for item in observations
                if item.get("policy_exclusion_reason")
            })
        },
        "reference_reason_counts": {
            reason: sum(1 for item in observations if item.get("reference_reason") == reason)
            for reason in sorted({
                str(item.get("reference_reason") or "")
                for item in observations
                if item.get("reference_reason")
            })
        },
    }
    result["receipt_sha256"] = content_sha256(result)
    if output_path is not None:
        destination = Path(output_path).expanduser().resolve()
        write_bounded_json_if_changed(
            destination,
            result,
            limit=MAX_CANONICAL_EVIDENCE_BYTES,
            max_nodes=MAX_CANONICAL_EVIDENCE_NODES,
        )
        result["output"] = str(destination)
    return result


__all__ = [
    "CANONICAL_FETCH_BATCH_ROWS",
    "MAX_CANONICAL_SOURCE_ROWS",
    "SCHEMA",
    "export_canonical_observation_evidence",
]
