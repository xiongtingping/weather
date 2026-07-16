from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Mapping, Optional, Tuple


AWC_HARD_SOURCE_SIGNALS_TABLE = "awc_hard_source_signals"

AWC_HARD_SOURCE_SIGNAL_COLUMNS: Tuple[str, ...] = (
    "signal_id",
    "source_event_id",
    "slug",
    "event_date",
    "awc_valid_time",
    "awc_hard_source_signal_at",
    "acceptance_state",
    "terminal_state",
    "state_reason",
    "created_at",
)

AWC_HARD_SOURCE_SIGNAL_TERMINAL_RANK = {
    "": 0,
    "pending": 10,
    "accepted_pending_execution": 15,
    "fact_written": 20,
    "posterior_written": 25,
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


AWC_HARD_SOURCE_SIGNAL_UPSERT_SQL = """
INSERT INTO awc_hard_source_signals(
    signal_id,
    source_event_id,
    slug,
    event_date,
    awc_valid_time,
    awc_hard_source_signal_at,
    acceptance_state,
    terminal_state,
    state_reason,
    created_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(signal_id) DO UPDATE SET
    source_event_id=CASE
      WHEN COALESCE(excluded.source_event_id, '') <> '' THEN excluded.source_event_id
      ELSE awc_hard_source_signals.source_event_id
    END,
    slug=CASE
      WHEN COALESCE(excluded.slug, '') <> '' THEN excluded.slug
      ELSE awc_hard_source_signals.slug
    END,
    event_date=CASE
      WHEN COALESCE(excluded.event_date, '') <> '' THEN excluded.event_date
      ELSE awc_hard_source_signals.event_date
    END,
    awc_valid_time=CASE
      WHEN COALESCE(excluded.awc_valid_time, '') <> '' THEN excluded.awc_valid_time
      ELSE awc_hard_source_signals.awc_valid_time
    END,
    awc_hard_source_signal_at=CASE
      WHEN COALESCE(excluded.awc_hard_source_signal_at, '') <> '' THEN excluded.awc_hard_source_signal_at
      ELSE awc_hard_source_signals.awc_hard_source_signal_at
    END,
    acceptance_state=CASE
      WHEN COALESCE(excluded.acceptance_state, '') <> '' THEN excluded.acceptance_state
      ELSE awc_hard_source_signals.acceptance_state
    END,
    terminal_state=CASE
      WHEN COALESCE(excluded.terminal_state, '') <> '' THEN excluded.terminal_state
      ELSE awc_hard_source_signals.terminal_state
    END,
    state_reason=CASE
      WHEN COALESCE(excluded.state_reason, '') <> '' THEN excluded.state_reason
      ELSE awc_hard_source_signals.state_reason
    END,
    created_at=CASE
      WHEN COALESCE(awc_hard_source_signals.created_at, '') <> '' THEN awc_hard_source_signals.created_at
      ELSE excluded.created_at
    END
"""


def _terminal_rank_value(state: Any) -> int:
    return int(AWC_HARD_SOURCE_SIGNAL_TERMINAL_RANK.get(str(state or "").strip(), 0))


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


def awc_hard_source_signals_schema_status(target: Any) -> dict[str, Any]:
    columns = _table_columns(target, AWC_HARD_SOURCE_SIGNALS_TABLE)
    required = set(AWC_HARD_SOURCE_SIGNAL_COLUMNS)
    missing = sorted(required - columns)
    return {
        "ok": bool(columns and not missing),
        "table": AWC_HARD_SOURCE_SIGNALS_TABLE,
        "exists": bool(columns),
        "missing_columns": missing,
        "required_columns": list(AWC_HARD_SOURCE_SIGNAL_COLUMNS),
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def build_awc_hard_source_signal_row(
    payload: Mapping[str, Any],
    *,
    created_at: str = "",
) -> Tuple[Any, ...]:
    acceptance = _mapping(payload.get("hard_source_acceptance_state"))
    timing = _mapping(payload.get("signal_to_order_timing"))
    candidate = _mapping(payload.get("awc_hard_source_signal_candidate"))
    signal_id = str(
        payload.get("signal_id")
        or acceptance.get("signal_id")
        or timing.get("signal_id")
        or candidate.get("signal_id")
        or ""
    ).strip()
    terminal_state = str(
        payload.get("terminal_state")
        or payload.get("signal_to_order_terminal_state")
        or timing.get("terminal_state")
        or candidate.get("terminal_state")
        or ""
    ).strip()
    state_reason = str(
        payload.get("state_reason")
        or payload.get("terminal_reason")
        or payload.get("signal_to_order_terminal_reason")
        or timing.get("terminal_reason")
        or timing.get("signal_to_order_slo_violation_reason")
        or acceptance.get("acceptance_reason")
        or candidate.get("acceptance_reason")
        or ""
    ).strip()
    return (
        signal_id,
        str(
            payload.get("source_event_id")
            or acceptance.get("source_event_id")
            or timing.get("source_event_id")
            or candidate.get("source_event_id")
            or ""
        ).strip(),
        str(payload.get("slug") or acceptance.get("slug") or timing.get("slug") or candidate.get("slug") or "").strip(),
        str(payload.get("event_date") or acceptance.get("event_date") or timing.get("event_date") or candidate.get("event_date") or "").strip(),
        str(
            payload.get("awc_valid_time")
            or acceptance.get("awc_valid_time")
            or timing.get("awc_valid_time")
            or candidate.get("awc_valid_time")
            or ""
        ).strip(),
        str(
            payload.get("awc_hard_source_signal_at")
            or acceptance.get("awc_hard_source_signal_at")
            or timing.get("awc_hard_source_signal_at")
            or candidate.get("awc_hard_source_signal_at")
            or ""
        ).strip(),
        str(
            payload.get("acceptance_state")
            or acceptance.get("acceptance_state")
            or timing.get("acceptance_state")
            or candidate.get("acceptance_state")
            or ""
        ).strip(),
        terminal_state,
        state_reason,
        str(created_at or payload.get("created_at") or timing.get("created_at") or "").strip(),
    )


def _fetch_existing_signal(target: Any, signal_id: str) -> dict[str, Any]:
    try:
        row = target.execute(
            "SELECT terminal_state, state_reason FROM awc_hard_source_signals WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    try:
        return {key: row[key] for key in row.keys()}
    except Exception:
        return {"terminal_state": row[0], "state_reason": row[1] if len(row) > 1 else ""}


def upsert_awc_hard_source_signal(
    target: Any,
    payload: Mapping[str, Any],
    *,
    created_at: str = "",
    attempts: int = 5,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
) -> dict[str, Any]:
    status = awc_hard_source_signals_schema_status(target)
    row = build_awc_hard_source_signal_row(payload, created_at=created_at)
    signal_id = str(row[0] or "").strip()
    if not signal_id:
        return {
            "ok": False,
            "status": "signal_id_missing",
            "table": AWC_HARD_SOURCE_SIGNALS_TABLE,
            "writes_archive_db": False,
        }
    if not status.get("ok"):
        return {
            "ok": True,
            "status": "schema_missing_noop",
            "table": AWC_HARD_SOURCE_SIGNALS_TABLE,
            "signal_id": signal_id,
            "schema_status": status,
            "writes_archive_db": False,
        }
    existing = _fetch_existing_signal(target, signal_id)
    incoming_state = str(row[7] or "").strip()
    existing_state = str(existing.get("terminal_state") or "").strip()
    if existing_state and _terminal_rank_value(existing_state) > _terminal_rank_value(incoming_state):
        return {
            "ok": True,
            "status": "preserved_existing_terminal",
            "table": AWC_HARD_SOURCE_SIGNALS_TABLE,
            "signal_id": signal_id,
            "existing_terminal_state": existing_state,
            "incoming_terminal_state": incoming_state,
            "writes_archive_db": False,
        }
    _execute_with_retry(
        target,
        AWC_HARD_SOURCE_SIGNAL_UPSERT_SQL,
        row,
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return {
        "ok": True,
        "status": "upserted",
        "table": AWC_HARD_SOURCE_SIGNALS_TABLE,
        "signal_id": signal_id,
        "terminal_state": incoming_state,
        "writes_archive_db": True,
    }
