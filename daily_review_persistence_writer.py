from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "daily_review_persistence_writer_v1"
PLAN_SCHEMA = "daily_review_persistence_plan_v1"

DAILY_WEATHER_REVIEWS_TABLE = "daily_weather_reviews"
DAILY_CALIBRATION_RECORDS_TABLE = "daily_calibration_records"
BIAS_CORRECTION_CANDIDATES_TABLE = "bias_correction_candidates"
PARAMETER_CHANGE_EVENTS_TABLE = "parameter_change_events"

DAILY_WEATHER_REVIEW_COLUMNS: tuple[str, ...] = (
    "review_id",
    "run_date",
    "slug",
    "event_date",
    "fact_snapshot_id",
    "first_bad_link",
    "review_json",
    "created_at",
)

DAILY_CALIBRATION_RECORD_COLUMNS: tuple[str, ...] = (
    "daily_calibration_record_id",
    "review_id",
    "run_date",
    "slug",
    "event_date",
    "fact_snapshot_id",
    "state",
    "calibration_metrics_json",
    "error_attribution_json",
    "strata_json",
    "record_json",
    "created_at",
    "updated_at",
)

BIAS_CORRECTION_CANDIDATE_COLUMNS: tuple[str, ...] = (
    "calibration_candidate_id",
    "review_id",
    "candidate_type",
    "state",
    "proposal_json",
    "rollback_plan_json",
    "created_at",
    "updated_at",
)

PARAMETER_CHANGE_EVENT_COLUMNS: tuple[str, ...] = (
    "id",
    "calibration_candidate_id",
    "state",
    "changed_by",
    "change_json",
    "created_at",
)

REQUIRED_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    DAILY_WEATHER_REVIEWS_TABLE: DAILY_WEATHER_REVIEW_COLUMNS,
    DAILY_CALIBRATION_RECORDS_TABLE: DAILY_CALIBRATION_RECORD_COLUMNS,
    BIAS_CORRECTION_CANDIDATES_TABLE: BIAS_CORRECTION_CANDIDATE_COLUMNS,
    PARAMETER_CHANGE_EVENTS_TABLE: PARAMETER_CHANGE_EVENT_COLUMNS,
}

DAILY_WEATHER_REVIEW_UPSERT_SQL = """
INSERT INTO daily_weather_reviews(
    review_id, run_date, slug, event_date,
    fact_snapshot_id, first_bad_link, review_json, created_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(review_id) DO UPDATE SET
    run_date=excluded.run_date,
    slug=excluded.slug,
    event_date=excluded.event_date,
    fact_snapshot_id=excluded.fact_snapshot_id,
    first_bad_link=excluded.first_bad_link,
    review_json=excluded.review_json
"""

DAILY_CALIBRATION_RECORD_UPSERT_SQL = """
INSERT INTO daily_calibration_records(
    daily_calibration_record_id, review_id, run_date, slug, event_date,
    fact_snapshot_id, state, calibration_metrics_json, error_attribution_json,
    strata_json, record_json, created_at, updated_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(daily_calibration_record_id) DO UPDATE SET
    review_id=excluded.review_id,
    run_date=excluded.run_date,
    slug=excluded.slug,
    event_date=excluded.event_date,
    fact_snapshot_id=excluded.fact_snapshot_id,
    state=excluded.state,
    calibration_metrics_json=excluded.calibration_metrics_json,
    error_attribution_json=excluded.error_attribution_json,
    strata_json=excluded.strata_json,
    record_json=excluded.record_json,
    updated_at=excluded.updated_at
"""

BIAS_CORRECTION_CANDIDATE_UPSERT_SQL = """
INSERT INTO bias_correction_candidates(
    calibration_candidate_id, review_id, candidate_type, state,
    proposal_json, rollback_plan_json, created_at, updated_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(calibration_candidate_id) DO UPDATE SET
    review_id=excluded.review_id,
    candidate_type=excluded.candidate_type,
    state=excluded.state,
    proposal_json=excluded.proposal_json,
    rollback_plan_json=excluded.rollback_plan_json,
    updated_at=excluded.updated_at
"""

PARAMETER_CHANGE_EVENT_INSERT_SQL = """
INSERT INTO parameter_change_events(
    calibration_candidate_id, state, changed_by, change_json, created_at
)
VALUES (?, ?, ?, ?, ?)
"""


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _table_columns(target: Any, table: str) -> set[str]:
    try:
        rows = target.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    columns: set[str] = set()
    for row in rows:
        try:
            columns.add(str(row["name"] or ""))
        except Exception:
            try:
                columns.add(str(row[1] or ""))
            except Exception:
                continue
    columns.discard("")
    return columns


def daily_review_persistence_schema_status(target: Any) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    ok = True
    for table, required in REQUIRED_TABLE_COLUMNS.items():
        columns = _table_columns(target, table)
        missing = sorted(set(required) - columns)
        table_ok = bool(columns and not missing)
        ok = ok and table_ok
        tables[table] = {
            "ok": table_ok,
            "exists": bool(columns),
            "missing_columns": missing,
            "required_columns": list(required),
        }
    return {
        "schema": f"{SCHEMA_VERSION}_schema_status",
        "ok": ok,
        "tables": tables,
    }


def _review_payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = _as_mapping(raw)
    nested = _as_mapping(payload.get("review"))
    return nested or payload


def _candidate_rows(review: Mapping[str, Any], *, created_at: str) -> list[dict[str, Any]]:
    candidates = [
        dict(_as_mapping(candidate))
        for candidate in _as_sequence(review.get("bias_correction_candidates") or review.get("calibration_candidates"))
        if isinstance(candidate, Mapping)
    ]
    requests = [
        dict(_as_mapping(request))
        for request in _as_sequence(_as_mapping(review.get("parameter_change_state")).get("requests"))
        if isinstance(request, Mapping)
    ]
    request_by_candidate = {
        _first_text(request.get("calibration_candidate_id")): request
        for request in requests
        if _first_text(request.get("calibration_candidate_id"))
    }
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        calibration_candidate_id = _first_text(candidate.get("calibration_candidate_id"))
        if not calibration_candidate_id:
            continue
        request = request_by_candidate.get(calibration_candidate_id, {})
        rollback_plan = _as_mapping(request).get("rollback_plan") if request else {}
        rows.append(
            {
                "calibration_candidate_id": calibration_candidate_id,
                "review_id": _first_text(candidate.get("review_id"), review.get("review_id")),
                "candidate_type": _first_text(candidate.get("candidate_type")),
                "state": _first_text(candidate.get("state"), "proposed"),
                "proposal_json": _json_dumps(candidate),
                "rollback_plan_json": _json_dumps(rollback_plan or {}),
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
    return rows


def _daily_calibration_record_row(review: Mapping[str, Any], *, created_at: str) -> dict[str, Any] | None:
    record = _as_mapping(review.get("daily_calibration_record")) or _as_mapping(
        _as_mapping(review.get("review_json")).get("daily_calibration_record")
    )
    record_id = _first_text(record.get("daily_calibration_record_id"))
    if not record_id:
        return None
    return {
        "daily_calibration_record_id": record_id,
        "review_id": _first_text(record.get("review_id"), review.get("review_id")),
        "run_date": _first_text(record.get("run_date"), review.get("run_date")),
        "slug": _first_text(record.get("slug"), review.get("slug")),
        "event_date": _first_text(record.get("event_date"), review.get("event_date")),
        "fact_snapshot_id": _first_text(record.get("fact_snapshot_id"), review.get("fact_snapshot_id")),
        "state": _first_text(record.get("state"), "no_apply"),
        "calibration_metrics_json": _json_dumps(_as_mapping(record.get("calibration_metrics"))),
        "error_attribution_json": _json_dumps(_as_mapping(record.get("error_attribution"))),
        "strata_json": _json_dumps(_as_mapping(record.get("strata"))),
        "record_json": _json_dumps(record),
        "created_at": created_at,
        "updated_at": created_at,
    }


def _parameter_event_rows(review: Mapping[str, Any], *, created_at: str) -> list[dict[str, Any]]:
    requests = [
        dict(_as_mapping(request))
        for request in _as_sequence(_as_mapping(review.get("parameter_change_state")).get("requests"))
        if isinstance(request, Mapping)
    ]
    rows: list[dict[str, Any]] = []
    for request in requests:
        calibration_candidate_id = _first_text(request.get("calibration_candidate_id"))
        state = _first_text(request.get("state"), "proposed")
        if not calibration_candidate_id or not state:
            continue
        rows.append(
            {
                "id": None,
                "calibration_candidate_id": calibration_candidate_id,
                "state": state,
                "changed_by": "daily_review_persistence_writer",
                "change_json": _json_dumps(request),
                "created_at": created_at,
            }
        )
    return rows


def build_daily_review_persistence_plan(
    review_or_report: Mapping[str, Any],
    *,
    created_at: str = "",
) -> dict[str, Any]:
    review = _review_payload(review_or_report)
    created = str(created_at or _now_iso())
    review_json = dict(_as_mapping(review.get("review_json")))
    review_id = _first_text(review.get("review_id"))
    blockers: list[str] = []
    if not review_id:
        blockers.append("review_id_missing")
    if not review_json:
        blockers.append("review_json_missing")
    review_row = {
        "review_id": review_id,
        "run_date": _first_text(review.get("run_date"), review_json.get("run_date")),
        "slug": _first_text(review.get("slug"), review_json.get("slug")),
        "event_date": _first_text(review.get("event_date"), review_json.get("event_date")),
        "fact_snapshot_id": _first_text(review.get("fact_snapshot_id"), review_json.get("fact_snapshot_id")),
        "first_bad_link": _first_text(review.get("first_bad_link"), review_json.get("first_bad_link")),
        "review_json": _json_dumps(review_json),
        "created_at": created,
    }
    calibration_record_row = _daily_calibration_record_row(review, created_at=created)
    if not calibration_record_row:
        blockers.append("daily_calibration_record_missing")
    candidate_rows = _candidate_rows(review, created_at=created)
    parameter_rows = _parameter_event_rows(review, created_at=created)
    return {
        "schema": PLAN_SCHEMA,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "no_apply_plan",
        "review_id": review_id,
        "fact_snapshot_id": review_row["fact_snapshot_id"],
        "blockers": blockers,
        "rows": {
            DAILY_WEATHER_REVIEWS_TABLE: [review_row] if review_id else [],
            DAILY_CALIBRATION_RECORDS_TABLE: [calibration_record_row] if calibration_record_row else [],
            BIAS_CORRECTION_CANDIDATES_TABLE: candidate_rows,
            PARAMETER_CHANGE_EVENTS_TABLE: parameter_rows,
        },
        "row_counts": {
            DAILY_WEATHER_REVIEWS_TABLE: 1 if review_id else 0,
            DAILY_CALIBRATION_RECORDS_TABLE: 1 if calibration_record_row else 0,
            BIAS_CORRECTION_CANDIDATES_TABLE: len(candidate_rows),
            PARAMETER_CHANGE_EVENTS_TABLE: len(parameter_rows),
        },
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_daily_calibration_records": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "changes_parameters": False,
            "historical_backfill": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "next_boundary": "apply_requires_schema_present_and_explicit_allow_apply",
    }


def compact_daily_review_persistence_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    rows = _as_mapping(plan.get("rows"))
    preview: dict[str, list[dict[str, Any]]] = {}
    for table, table_rows_raw in rows.items():
        table_rows = [dict(_as_mapping(row)) for row in _as_sequence(table_rows_raw) if isinstance(row, Mapping)]
        compact_rows: list[dict[str, Any]] = []
        for row in table_rows:
            row_json = _json_dumps(row)
            compact_rows.append(
                {
                    "primary_id": _first_text(
                        row.get("daily_calibration_record_id"),
                        row.get("review_id"),
                        row.get("calibration_candidate_id"),
                        row.get("id"),
                    ),
                    "state": row.get("state"),
                    "json_sha256": _sha256_text(row_json),
                }
            )
        preview[str(table)] = compact_rows
    return {
        "schema": f"{PLAN_SCHEMA}_compact",
        "ok": bool(plan.get("ok")),
        "read_only": True,
        "execution_mode": plan.get("execution_mode"),
        "review_id": plan.get("review_id"),
        "fact_snapshot_id": plan.get("fact_snapshot_id"),
        "blockers": list(_as_sequence(plan.get("blockers"))),
        "row_counts": dict(_as_mapping(plan.get("row_counts"))),
        "row_preview": preview,
        "guards": dict(_as_mapping(plan.get("guards"))),
        "next_boundary": plan.get("next_boundary"),
    }


def _dict_to_row(row: Mapping[str, Any], columns: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row.get(column) for column in columns)


def _insert_parameter_event_if_absent(target: Any, row: Mapping[str, Any]) -> bool:
    existing = target.execute(
        """
        SELECT id FROM parameter_change_events
        WHERE calibration_candidate_id = ? AND state = ? AND change_json = ?
        LIMIT 1
        """,
        (
            row.get("calibration_candidate_id"),
            row.get("state"),
            row.get("change_json"),
        ),
    ).fetchone()
    if existing:
        return False
    target.execute(
        PARAMETER_CHANGE_EVENT_INSERT_SQL,
        (
            row.get("calibration_candidate_id"),
            row.get("state"),
            row.get("changed_by"),
            row.get("change_json"),
            row.get("created_at"),
        ),
    )
    return True


def apply_daily_review_persistence_plan(
    target: Any,
    plan: Mapping[str, Any],
    *,
    allow_apply: bool = False,
    allow_parameter_event_apply: bool = False,
) -> dict[str, Any]:
    if not allow_apply:
        return {
            "ok": True,
            "status": "apply_blocked_noop",
            "writes_archive_db": False,
            "guards": {
                "requires_allow_apply": True,
                "changes_parameters": False,
                "historical_backfill": False,
            },
        }
    schema_status = daily_review_persistence_schema_status(target)
    if not schema_status.get("ok"):
        return {
            "ok": True,
            "status": "schema_missing_noop",
            "writes_archive_db": False,
            "schema_status": schema_status,
        }
    if not plan.get("ok"):
        return {
            "ok": False,
            "status": "plan_blocked",
            "writes_archive_db": False,
            "blockers": list(_as_sequence(plan.get("blockers"))),
        }
    rows = _as_mapping(plan.get("rows"))
    review_rows = [
        dict(_as_mapping(row))
        for row in _as_sequence(rows.get(DAILY_WEATHER_REVIEWS_TABLE))
        if isinstance(row, Mapping)
    ]
    calibration_record_rows = [
        dict(_as_mapping(row))
        for row in _as_sequence(rows.get(DAILY_CALIBRATION_RECORDS_TABLE))
        if isinstance(row, Mapping)
    ]
    candidate_rows = [
        dict(_as_mapping(row))
        for row in _as_sequence(rows.get(BIAS_CORRECTION_CANDIDATES_TABLE))
        if isinstance(row, Mapping)
    ]
    parameter_rows = [
        dict(_as_mapping(row))
        for row in _as_sequence(rows.get(PARAMETER_CHANGE_EVENTS_TABLE))
        if isinstance(row, Mapping)
    ]
    for row in review_rows:
        target.execute(
            DAILY_WEATHER_REVIEW_UPSERT_SQL,
            _dict_to_row(row, DAILY_WEATHER_REVIEW_COLUMNS),
        )
    for row in calibration_record_rows:
        target.execute(
            DAILY_CALIBRATION_RECORD_UPSERT_SQL,
            _dict_to_row(row, DAILY_CALIBRATION_RECORD_COLUMNS),
        )
    for row in candidate_rows:
        target.execute(
            BIAS_CORRECTION_CANDIDATE_UPSERT_SQL,
            _dict_to_row(row, BIAS_CORRECTION_CANDIDATE_COLUMNS),
        )
    parameter_events_inserted = 0
    if allow_parameter_event_apply:
        for row in parameter_rows:
            if _insert_parameter_event_if_absent(target, row):
                parameter_events_inserted += 1
    return {
        "ok": True,
        "status": "applied",
        "writes_archive_db": bool(review_rows or calibration_record_rows or candidate_rows or parameter_events_inserted),
        "row_counts": {
            DAILY_WEATHER_REVIEWS_TABLE: len(review_rows),
            DAILY_CALIBRATION_RECORDS_TABLE: len(calibration_record_rows),
            BIAS_CORRECTION_CANDIDATES_TABLE: len(candidate_rows),
            PARAMETER_CHANGE_EVENTS_TABLE: parameter_events_inserted,
        },
        "parameter_events_skipped": 0 if allow_parameter_event_apply else len(parameter_rows),
        "guards": {
            "allow_apply": True,
            "allow_parameter_event_apply": bool(allow_parameter_event_apply),
            "changes_parameters": False,
            "historical_backfill": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }
