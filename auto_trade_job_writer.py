from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from archive_db_write_queue import connect_archive_db_rw
from lineage_identity_runtime import lineage_id_from_parts


AUTO_TRADE_JOB_INSERT_SQL = """
INSERT INTO auto_trade_jobs(
    run_date, source, state, priority, payload_json,
    lease_until, retry_count, max_retry, next_retry_at, last_error,
    started_at, ended_at, run_id, created_at, updated_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


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


def _payload_json(payload: Any = None, payload_json: Any = None) -> str:
    if payload_json is not None:
        return str(payload_json)
    if isinstance(payload, Mapping):
        return json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"))
    return ""


def _parse_json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _table_columns(target: Any, table: str) -> set[str]:
    try:
        result = _execute_with_retry(target, f"PRAGMA table_info({table})")
        rows = result.fetchall()
    except Exception:
        return set()
    columns: set[str] = set()
    for row in rows:
        try:
            columns.add(str(row[1] or ""))
        except Exception:
            try:
                columns.add(str(row["name"] or ""))
            except Exception:
                pass
    return {column for column in columns if column}


def _merge_fill_inserted_job_identity(
    target: Any,
    *,
    row_id: int,
    fields: Mapping[str, Any],
    attempts: int,
    sleep_base_sec: float,
    sleep_cap_sec: float,
) -> None:
    if row_id <= 0:
        return
    columns = _table_columns(target, "auto_trade_jobs")
    identity_columns = ("correlation_id", "decision_id", "signal_id", "order_intent_id")
    writable_columns = [column for column in identity_columns if column in columns]
    if not writable_columns:
        return
    payload = _parse_json_mapping(fields.get("payload"))
    if not payload:
        payload = _parse_json_mapping(fields.get("payload_json"))
    timing = payload.get("signal_to_order_timing")
    if not isinstance(timing, Mapping):
        timing = {}
    natural_key = {
        "table": "auto_trade_jobs",
        "id": row_id,
        "run_id": fields.get("run_id"),
        "run_date": fields.get("run_date"),
        "source": fields.get("source"),
        "created_at": fields.get("created_at"),
    }
    values = {
        "correlation_id": _first_text(
            fields.get("correlation_id"),
            payload.get("correlation_id"),
            timing.get("correlation_id"),
            lineage_id_from_parts("correlation_id", natural_key),
        ),
        "order_intent_id": _first_text(
            fields.get("order_intent_id"),
            payload.get("order_intent_id"),
            timing.get("order_intent_id"),
            payload.get("idempotency_key"),
            timing.get("idempotency_key"),
            lineage_id_from_parts("order_intent_id", natural_key),
        ),
        "decision_id": _first_text(fields.get("decision_id"), payload.get("decision_id"), timing.get("decision_id")),
        "signal_id": _first_text(fields.get("signal_id"), payload.get("signal_id"), timing.get("signal_id")),
    }
    set_columns = [column for column in writable_columns if values.get(column)]
    if not set_columns:
        return
    assignments = ", ".join(
        f"{column} = CASE WHEN COALESCE({column}, '') = '' THEN ? ELSE {column} END"
        for column in set_columns
    )
    _execute_with_retry(
        target,
        f"UPDATE auto_trade_jobs SET {assignments} WHERE id = ?",
        tuple(values[column] for column in set_columns) + (row_id,),
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )


def insert_auto_trade_job(
    target: Any,
    *,
    run_date: Any,
    source: Any,
    priority: Any = 0,
    payload: Any = None,
    payload_json: Any = None,
    state: Any = "queued",
    lease_until: Any = "",
    retry_count: Any = 0,
    max_retry: Any = 0,
    next_retry_at: Any = "",
    last_error: Any = "",
    started_at: Any = "",
    ended_at: Any = "",
    run_id: Any = None,
    created_at: Any,
    updated_at: Any,
    attempts: int = 5,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
) -> int:
    result = _execute_with_retry(
        target,
        AUTO_TRADE_JOB_INSERT_SQL,
        (
            str(run_date or ""),
            str(source or ""),
            str(state or "queued"),
            int(priority or 0),
            _payload_json(payload, payload_json),
            str(lease_until or ""),
            int(retry_count or 0),
            int(max_retry or 0),
            str(next_retry_at or ""),
            str(last_error or ""),
            str(started_at or ""),
            str(ended_at or ""),
            run_id,
            str(created_at or ""),
            str(updated_at or ""),
        ),
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    cursor = result if hasattr(result, "lastrowid") else target
    try:
        row_id = int(getattr(cursor, "lastrowid", 0) or 0)
    except Exception:
        return 0
    _merge_fill_inserted_job_identity(
        target,
        row_id=row_id,
        fields={
            "run_date": run_date,
            "source": source,
            "payload": payload,
            "payload_json": payload_json,
            "run_id": run_id,
            "created_at": created_at,
            "correlation_id": None,
            "decision_id": None,
            "signal_id": None,
            "order_intent_id": None,
        },
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return row_id


def mark_superseded_terminal_jobs_with_newer_active(
    target: Any,
    *,
    now_iso: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE auto_trade_jobs
        SET state = 'superseded',
            updated_at = ?,
            ended_at = CASE WHEN COALESCE(ended_at, '') = '' THEN ? ELSE ended_at END
        WHERE state IN ('failed', 'stuck')
          AND EXISTS (
                SELECT 1
                FROM auto_trade_jobs newer
                WHERE newer.run_date = auto_trade_jobs.run_date
                  AND newer.id > auto_trade_jobs.id
                  AND newer.state IN ('queued', 'running', 'retrying', 'success')
          )
        """,
        (now_iso, now_iso),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def delete_terminal_auto_trade_jobs_before(
    target: Any,
    *,
    cutoff_iso: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        DELETE FROM auto_trade_jobs
        WHERE state IN ('success', 'failed', 'stuck', 'superseded')
          AND COALESCE(updated_at, created_at, '') <> ''
          AND COALESCE(updated_at, created_at) < ?
        """,
        (cutoff_iso,),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def claim_auto_trade_job_running(
    target: Any,
    *,
    job_id: Any,
    lease_until: str,
    now_iso: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE auto_trade_jobs
        SET state = 'running',
            lease_until = ?,
            started_at = CASE WHEN COALESCE(started_at, '') = '' THEN ? ELSE started_at END,
            ended_at = '',
            updated_at = ?,
            last_error = ''
        WHERE id = ?
        """,
        (lease_until, now_iso, now_iso, int(job_id)),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def touch_running_auto_trade_job(
    target: Any,
    *,
    job_id: Any,
    lease_until: str,
    now_iso: str,
    run_id: Any = None,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE auto_trade_jobs
        SET lease_until = ?,
            updated_at = ?,
            run_id = COALESCE(run_id, ?),
            started_at = CASE
                WHEN COALESCE(started_at, '') = '' THEN ?
                ELSE started_at
            END
        WHERE id = ?
          AND state = 'running'
        """,
        (lease_until, now_iso, int(run_id) if run_id else None, now_iso, int(job_id)),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def link_running_auto_trade_job_run_id(
    target: Any,
    *,
    job_id: Any,
    run_id: Any,
    now_iso: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE auto_trade_jobs
        SET run_id = ?, updated_at = ?
        WHERE id = ? AND state = 'running'
        """,
        (int(run_id), now_iso, int(job_id)),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def write_auto_trade_job_final_state(
    target: Any,
    *,
    job_id: Any,
    state: str,
    ended_at: str,
    run_id: Any = None,
    retry_count: Any = None,
    next_retry_at: str = "",
    last_error: str = "",
    attempts: int = 5,
) -> int:
    state_now = str(state or "").strip().lower()
    if state_now == "success":
        sql = """
        UPDATE auto_trade_jobs
        SET state = 'success',
            lease_until = '',
            ended_at = ?,
            updated_at = ?,
            run_id = ?,
            last_error = ''
        WHERE id = ?
        """
        params = (ended_at, ended_at, run_id, int(job_id))
    elif state_now == "retrying":
        sql = """
        UPDATE auto_trade_jobs
        SET state = 'retrying',
            lease_until = '',
            retry_count = ?,
            next_retry_at = ?,
            ended_at = ?,
            updated_at = ?,
            run_id = ?,
            last_error = ?
        WHERE id = ?
        """
        params = (
            int(retry_count or 0),
            str(next_retry_at or ""),
            ended_at,
            ended_at,
            run_id,
            str(last_error or "auto_trade_failed"),
            int(job_id),
        )
    elif state_now == "failed":
        sql = """
        UPDATE auto_trade_jobs
        SET state = 'failed',
            lease_until = '',
            retry_count = ?,
            ended_at = ?,
            updated_at = ?,
            run_id = ?,
            last_error = ?
        WHERE id = ?
        """
        params = (
            int(retry_count or 0),
            ended_at,
            ended_at,
            run_id,
            str(last_error or "auto_trade_failed"),
            int(job_id),
        )
    elif state_now == "superseded":
        sql = """
        UPDATE auto_trade_jobs
        SET state = 'superseded',
            lease_until = '',
            ended_at = ?,
            updated_at = ?,
            run_id = ?,
            last_error = ?
        WHERE id = ?
        """
        params = (ended_at, ended_at, run_id, str(last_error or "superseded"), int(job_id))
    else:
        raise ValueError(f"unsupported_job_state:{state_now}")
    result = _execute_with_retry(target, sql, params, attempts=attempts)
    return int(getattr(result, "rowcount", 0) or 0)


def mark_running_auto_trade_job_retrying(
    target: Any,
    *,
    job_id: Any,
    retry_count: Any,
    next_retry_at: str,
    now_iso: str,
    last_error_if_blank: str = "worker_stuck_timeout",
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE auto_trade_jobs
        SET state = 'retrying',
            lease_until = '',
            retry_count = ?,
            next_retry_at = ?,
            ended_at = ?,
            updated_at = ?,
            last_error = CASE
                WHEN COALESCE(last_error, '') = '' THEN ?
                ELSE last_error
            END
        WHERE id = ?
          AND state = 'running'
        """,
        (int(retry_count or 0), str(next_retry_at or ""), now_iso, now_iso, last_error_if_blank, int(job_id)),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def mark_running_auto_trade_job_failed(
    target: Any,
    *,
    job_id: Any,
    retry_count: Any,
    now_iso: str,
    last_error_if_blank: str = "worker_stuck_timeout",
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE auto_trade_jobs
        SET state = 'failed',
            lease_until = '',
            retry_count = ?,
            ended_at = ?,
            updated_at = ?,
            last_error = CASE
                WHEN COALESCE(last_error, '') = '' THEN ?
                ELSE last_error
            END
        WHERE id = ?
          AND state = 'running'
        """,
        (int(retry_count or 0), now_iso, now_iso, last_error_if_blank, int(job_id)),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def mark_stale_auto_trade_jobs_stuck(
    target: Any,
    *,
    now_iso: str,
    cutoff_iso: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE auto_trade_jobs
        SET state='stuck',
            lease_until='',
            ended_at=CASE
              WHEN COALESCE(ended_at, '') = '' THEN ?
              ELSE ended_at
            END,
            last_error=CASE
              WHEN TRIM(COALESCE(last_error, '')) = '' THEN 'worker_stuck_timeout'
              ELSE last_error
            END,
            updated_at=?
        WHERE lower(COALESCE(state, '')) IN ('queued', 'running', 'retrying')
          AND (
            (COALESCE(lease_until, '') <> '' AND lease_until <= ?)
            OR (COALESCE(started_at, '') <> '' AND started_at <= ?)
            OR (COALESCE(updated_at, '') <> '' AND updated_at <= ?)
          )
        """,
        (now_iso, now_iso, now_iso, cutoff_iso, cutoff_iso),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def mark_running_jobs_timeout_older_than(db_path: str | Path, *, minutes: int = 10) -> int:
    path = Path(db_path).expanduser()
    with connect_archive_db_rw(
        path,
        owner="auto_trade_job_writer.mark_running_jobs_timeout",
        timeout_sec=10.0,
        busy_timeout_ms=10000,
        row_factory=False,
    ) as conn:
        result = _execute_with_retry(
            conn,
            """
            UPDATE auto_trade_jobs
            SET state='timeout', ended_at=datetime('now')
            WHERE state='running'
            AND datetime(started_at, '+' || ? || ' minutes') < datetime('now')
            """,
            (int(minutes),),
            attempts=3,
        )
        conn.commit()
        return int(getattr(result, "rowcount", 0) or 0)


def copy_auto_trade_jobs_for_archive_rebuild(
    target: Any,
    *,
    active_states: Sequence[str],
    cutoff: str,
) -> int:
    states = tuple(str(state) for state in active_states)
    placeholders = ",".join("?" for _ in states)
    before = int(getattr(target, "total_changes", 0) or 0)
    _execute_with_retry(
        target,
        f"""
        INSERT INTO main.auto_trade_jobs
        SELECT *
        FROM src.auto_trade_jobs
        WHERE state IN ({placeholders})
           OR substr(created_at, 1, 10) >= ?
        """,
        (*states, cutoff),
        attempts=1,
    )
    after = int(getattr(target, "total_changes", before) or before)
    return max(0, after - before)
