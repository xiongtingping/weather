from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Mapping, Optional, Tuple


SIGNAL_TO_ORDER_TIMING_TABLE = "signal_to_order_timing"

SIGNAL_TO_ORDER_TIMING_COLUMNS: Tuple[str, ...] = (
    "signal_id",
    "decision_id",
    "order_intent_id",
    "awc_hard_source_signal_at",
    "fact_snapshot_written_at",
    "posterior_written_at",
    "auto_trade_decision_written_at",
    "execution_consumer_seen_decision_at",
    "order_job_enqueued_at",
    "order_submitted_at",
    "signal_to_order_slo_violation_reason",
    "timing_json",
    "created_at",
)

SIGNAL_TO_ORDER_TIMING_UPSERT_SQL = """
INSERT INTO signal_to_order_timing(
    signal_id,
    decision_id,
    order_intent_id,
    awc_hard_source_signal_at,
    fact_snapshot_written_at,
    posterior_written_at,
    auto_trade_decision_written_at,
    execution_consumer_seen_decision_at,
    order_job_enqueued_at,
    order_submitted_at,
    signal_to_order_slo_violation_reason,
    timing_json,
    created_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(signal_id) DO UPDATE SET
    decision_id=CASE
      WHEN COALESCE(excluded.decision_id, '') <> '' THEN excluded.decision_id
      ELSE signal_to_order_timing.decision_id
    END,
    order_intent_id=CASE
      WHEN COALESCE(excluded.order_intent_id, '') <> '' THEN excluded.order_intent_id
      ELSE signal_to_order_timing.order_intent_id
    END,
    awc_hard_source_signal_at=CASE
      WHEN COALESCE(excluded.awc_hard_source_signal_at, '') <> '' THEN excluded.awc_hard_source_signal_at
      ELSE signal_to_order_timing.awc_hard_source_signal_at
    END,
    fact_snapshot_written_at=CASE
      WHEN COALESCE(excluded.fact_snapshot_written_at, '') <> '' THEN excluded.fact_snapshot_written_at
      ELSE signal_to_order_timing.fact_snapshot_written_at
    END,
    posterior_written_at=CASE
      WHEN COALESCE(excluded.posterior_written_at, '') <> '' THEN excluded.posterior_written_at
      ELSE signal_to_order_timing.posterior_written_at
    END,
    auto_trade_decision_written_at=CASE
      WHEN COALESCE(excluded.auto_trade_decision_written_at, '') <> '' THEN excluded.auto_trade_decision_written_at
      ELSE signal_to_order_timing.auto_trade_decision_written_at
    END,
    execution_consumer_seen_decision_at=CASE
      WHEN COALESCE(excluded.execution_consumer_seen_decision_at, '') <> '' THEN excluded.execution_consumer_seen_decision_at
      ELSE signal_to_order_timing.execution_consumer_seen_decision_at
    END,
    order_job_enqueued_at=CASE
      WHEN COALESCE(excluded.order_job_enqueued_at, '') <> '' THEN excluded.order_job_enqueued_at
      ELSE signal_to_order_timing.order_job_enqueued_at
    END,
    order_submitted_at=CASE
      WHEN COALESCE(signal_to_order_timing.order_submitted_at, '') = ''
       AND COALESCE(excluded.order_submitted_at, '') <> '' THEN excluded.order_submitted_at
      ELSE signal_to_order_timing.order_submitted_at
    END,
    signal_to_order_slo_violation_reason=CASE
      WHEN COALESCE(excluded.signal_to_order_slo_violation_reason, '') <> '' THEN excluded.signal_to_order_slo_violation_reason
      ELSE signal_to_order_timing.signal_to_order_slo_violation_reason
    END,
    timing_json=excluded.timing_json
"""


SIGNAL_TO_ORDER_TERMINAL_RANK = {
    "": 0,
    "pending": 10,
    "not_applicable": 10,
    "fact_written": 20,
    "decision_written": 30,
    "job_enqueued": 40,
    "worker_started": 50,
    "blocked": 90,
    "stale": 90,
    "skipped": 90,
    "no_action": 90,
    "error": 90,
    "submitted": 100,
}


def _terminal_rank(timing: Mapping[str, Any]) -> int:
    if not isinstance(timing, Mapping):
        return 0
    state = str(timing.get("terminal_state") or "").strip()
    return int(SIGNAL_TO_ORDER_TERMINAL_RANK.get(state, 0))


def _timing_value_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _merge_timing_json(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Merge-fill timing evidence without erasing earlier chain segments."""

    merged = dict(existing or {})
    incoming_map = dict(incoming or {})
    existing_rank = _terminal_rank(merged)
    incoming_rank = _terminal_rank(incoming_map)
    terminal_progressed = incoming_rank >= existing_rank
    terminal_fields = {
        "terminal_state",
        "terminal_reason",
        "terminal_at",
        "signal_to_order_terminal_state",
        "signal_to_order_terminal_reason",
        "signal_to_order_terminal_at",
        "signal_to_order_slo_met",
        "signal_to_order_slo_violation_reason",
        "awc_signal_to_order_submit_ms",
        "signal_to_terminal_ms",
    }
    for key, value in incoming_map.items():
        if _timing_value_missing(value):
            continue
        if _timing_value_missing(merged.get(key)):
            merged[key] = value
            continue
        if key in terminal_fields and terminal_progressed:
            merged[key] = value
    return merged


def _fetch_existing_timing(target: Any, signal_id: str) -> dict[str, Any]:
    try:
        row = target.execute(
            "SELECT timing_json FROM signal_to_order_timing WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    try:
        raw = row["timing_json"]
    except Exception:
        try:
            raw = row[0]
        except Exception:
            raw = ""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _sqlite_is_locked_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    return "locked" in text or "busy" in text


def _execute_with_retry(
    target: Any,
    sql: str,
    params: Tuple[Any, ...] = (),
    *,
    attempts: int = 5,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
) -> Any:
    execute_fn = target.execute if hasattr(target, "execute") else target
    if not callable(execute_fn):
        raise TypeError("target must be a sqlite cursor/connection or execute callable")
    max_attempts = max(1, int(attempts))
    base_sleep = max(0.0, float(sleep_base_sec))
    cap_sleep = max(0.0, float(sleep_cap_sec))
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return execute_fn(sql, params)
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if (not _sqlite_is_locked_error(exc)) or attempt >= (max_attempts - 1):
                raise
            if base_sleep <= 0:
                continue
            sleep_for = base_sleep * float(attempt + 1)
            if cap_sleep > 0:
                sleep_for = min(sleep_for, cap_sleep)
            time.sleep(sleep_for)
    if last_exc is not None:
        raise last_exc
    return execute_fn(sql, params)


def _table_columns(target: Any, table: str) -> set[str]:
    rows = target.execute(f"PRAGMA table_info({table})").fetchall()
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


def signal_to_order_timing_schema_status(target: Any) -> dict[str, Any]:
    columns = _table_columns(target, SIGNAL_TO_ORDER_TIMING_TABLE)
    required = set(SIGNAL_TO_ORDER_TIMING_COLUMNS)
    missing = sorted(required - columns)
    return {
        "ok": bool(columns and not missing),
        "table": SIGNAL_TO_ORDER_TIMING_TABLE,
        "exists": bool(columns),
        "missing_columns": missing,
        "required_columns": list(SIGNAL_TO_ORDER_TIMING_COLUMNS),
    }


def build_signal_to_order_timing_row(
    payload: Mapping[str, Any],
    *,
    created_at: str = "",
    ensure_ascii: bool = False,
) -> Tuple[Any, ...]:
    timing = (
        dict(payload.get("signal_to_order_timing"))
        if isinstance(payload.get("signal_to_order_timing"), Mapping)
        else dict(payload)
    )
    signal_id = str(payload.get("signal_id") or timing.get("signal_id") or "").strip()
    return (
        signal_id,
        str(payload.get("decision_id") or timing.get("decision_id") or ""),
        str(payload.get("order_intent_id") or timing.get("order_intent_id") or ""),
        str(payload.get("awc_hard_source_signal_at") or timing.get("awc_hard_source_signal_at") or ""),
        str(payload.get("fact_snapshot_written_at") or timing.get("fact_snapshot_written_at") or timing.get("fact_written_at") or ""),
        str(payload.get("posterior_written_at") or timing.get("posterior_written_at") or ""),
        str(
            payload.get("auto_trade_decision_written_at")
            or timing.get("auto_trade_decision_written_at")
            or timing.get("decision_written_at")
            or ""
        ),
        str(
            payload.get("execution_consumer_seen_decision_at")
            or timing.get("execution_consumer_seen_decision_at")
            or timing.get("worker_started_at")
            or ""
        ),
        str(payload.get("order_job_enqueued_at") or timing.get("order_job_enqueued_at") or timing.get("job_enqueued_at") or ""),
        str(payload.get("order_submitted_at") or timing.get("order_submitted_at") or ""),
        str(
            payload.get("signal_to_order_slo_violation_reason")
            or timing.get("signal_to_order_slo_violation_reason")
            or ""
        ),
        json.dumps(timing, ensure_ascii=ensure_ascii, separators=(",", ":"), default=str),
        str(created_at or timing.get("created_at") or payload.get("created_at") or ""),
    )


def upsert_signal_to_order_timing(
    target: Any,
    payload: Mapping[str, Any],
    *,
    created_at: str = "",
    attempts: int = 5,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
    ensure_ascii: bool = False,
) -> dict[str, Any]:
    status = signal_to_order_timing_schema_status(target)
    signal_id = str(payload.get("signal_id") or "").strip()
    if not signal_id and isinstance(payload.get("signal_to_order_timing"), Mapping):
        signal_id = str(payload["signal_to_order_timing"].get("signal_id") or "").strip()
    if not signal_id:
        return {
            "ok": False,
            "status": "signal_id_missing",
            "table": SIGNAL_TO_ORDER_TIMING_TABLE,
            "writes_archive_db": False,
        }
    if not status.get("ok"):
        return {
            "ok": True,
            "status": "schema_missing_noop",
            "table": SIGNAL_TO_ORDER_TIMING_TABLE,
            "signal_id": signal_id,
            "schema_status": status,
            "writes_archive_db": False,
        }
    new_timing = (
        dict(payload.get("signal_to_order_timing"))
        if isinstance(payload.get("signal_to_order_timing"), Mapping)
        else dict(payload)
    )
    existing_timing = _fetch_existing_timing(target, signal_id)
    if existing_timing and _terminal_rank(existing_timing) > _terminal_rank(new_timing):
        return {
            "ok": True,
            "status": "preserved_existing_terminal",
            "table": SIGNAL_TO_ORDER_TIMING_TABLE,
            "signal_id": signal_id,
            "existing_terminal_state": str(existing_timing.get("terminal_state") or ""),
            "incoming_terminal_state": str(new_timing.get("terminal_state") or ""),
            "writes_archive_db": False,
        }
    merged_timing = _merge_timing_json(existing_timing, new_timing)
    row_payload = dict(payload)
    row_payload["signal_to_order_timing"] = merged_timing
    row = build_signal_to_order_timing_row(row_payload, created_at=created_at, ensure_ascii=ensure_ascii)
    _execute_with_retry(
        target,
        SIGNAL_TO_ORDER_TIMING_UPSERT_SQL,
        row,
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return {
        "ok": True,
        "status": "upserted",
        "table": SIGNAL_TO_ORDER_TIMING_TABLE,
        "signal_id": signal_id,
        "writes_archive_db": True,
    }
