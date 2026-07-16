from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from lineage_identity_runtime import lineage_id_from_parts


AUTO_TRADE_DECISION_COLUMNS: Tuple[str, ...] = (
    "run_id",
    "run_date",
    "slug",
    "event_date",
    "title",
    "outcome",
    "side",
    "action",
    "decided_state",
    "will_order",
    "reason_codes_json",
    "reasons_json",
    "details_json",
    "created_at",
)

AUTO_TRADE_DECISION_INSERT_SQL = """
INSERT INTO auto_trade_decisions(
    run_id, run_date, slug, event_date, title,
    outcome, side, action,
    decided_state, will_order,
    reason_codes_json, reasons_json, details_json,
    created_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _sqlite_is_locked_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    return "locked" in text or "busy" in text


def _execute_with_retry(
    target: Any,
    sql: str,
    params: Tuple[Any, ...] = (),
    *,
    attempts: int = 8,
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


def _executemany_with_retry(
    target: Any,
    sql: str,
    rows: Sequence[Tuple[Any, ...]],
    *,
    attempts: int = 8,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
) -> Any:
    executemany_fn = target.executemany if hasattr(target, "executemany") else None
    if not callable(executemany_fn):
        raise TypeError("target must be a sqlite cursor/connection with executemany")
    max_attempts = max(1, int(attempts))
    base_sleep = max(0.0, float(sleep_base_sec))
    cap_sleep = max(0.0, float(sleep_cap_sec))
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return executemany_fn(sql, rows)
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
    return executemany_fn(sql, rows)


def _json_text(
    explicit_json: Any,
    value: Any,
    *,
    default: Any,
    ensure_ascii: bool,
) -> str:
    if explicit_json is not None:
        return str(explicit_json)
    payload = default if value is None else value
    return json.dumps(payload, ensure_ascii=ensure_ascii, separators=(",", ":"))


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


def _merge_fill_inserted_decision_identity(
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
    columns = _table_columns(target, "auto_trade_decisions")
    identity_columns = (
        "correlation_id",
        "fact_snapshot_id",
        "posterior_run_id",
        "decision_id",
        "signal_id",
        "order_intent_id",
    )
    writable_columns = [column for column in identity_columns if column in columns]
    if not writable_columns:
        return
    details = _parse_json_mapping(fields.get("details"))
    if not details:
        details = _parse_json_mapping(fields.get("details_json"))
    natural_key = {
        "table": "auto_trade_decisions",
        "id": row_id,
        "run_id": fields.get("run_id"),
        "run_date": fields.get("run_date"),
        "slug": fields.get("slug"),
        "event_date": fields.get("event_date"),
        "outcome": fields.get("outcome"),
        "side": fields.get("side"),
        "action": fields.get("action"),
        "created_at": fields.get("created_at"),
    }
    values = {
        "correlation_id": _first_text(
            fields.get("correlation_id"),
            details.get("correlation_id"),
            lineage_id_from_parts("correlation_id", natural_key),
        ),
        "decision_id": _first_text(
            fields.get("decision_id"),
            details.get("decision_id"),
            lineage_id_from_parts("decision_id", natural_key),
        ),
        "fact_snapshot_id": _first_text(fields.get("fact_snapshot_id"), details.get("fact_snapshot_id")),
        "posterior_run_id": _first_text(fields.get("posterior_run_id"), details.get("posterior_run_id")),
        "signal_id": _first_text(fields.get("signal_id"), details.get("signal_id")),
        "order_intent_id": _first_text(
            fields.get("order_intent_id"),
            details.get("order_intent_id"),
            details.get("idempotency_key"),
        ),
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
        f"UPDATE auto_trade_decisions SET {assignments} WHERE id = ?",
        tuple(values[column] for column in set_columns) + (row_id,),
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )


def build_auto_trade_decision_row(
    *,
    run_id: Any = None,
    run_date: Any = "",
    slug: Any = "",
    event_date: Any = "",
    title: Any = "",
    outcome: Any = "",
    side: Any = "",
    action: Any = "",
    decided_state: Any = "",
    will_order: Any = 0,
    reason_codes: Any = None,
    reasons: Any = None,
    details: Any = None,
    reason_codes_json: Any = None,
    reasons_json: Any = None,
    details_json: Any = None,
    created_at: Any = "",
    ensure_ascii: bool = False,
) -> Tuple[Any, ...]:
    return (
        run_id,
        str(run_date or ""),
        str(slug or ""),
        str(event_date or ""),
        str(title or ""),
        str(outcome or ""),
        str(side or ""),
        str(action or ""),
        str(decided_state or ""),
        1 if bool(will_order) else 0,
        _json_text(reason_codes_json, reason_codes, default=[], ensure_ascii=ensure_ascii),
        _json_text(reasons_json, reasons, default=[], ensure_ascii=ensure_ascii),
        _json_text(details_json, details, default={}, ensure_ascii=ensure_ascii),
        str(created_at or ""),
    )


def build_auto_trade_decision_row_from_payload(
    payload: Mapping[str, Any],
    *,
    ensure_ascii: bool = False,
) -> Tuple[Any, ...]:
    return build_auto_trade_decision_row(
        run_id=payload.get("run_id"),
        run_date=payload.get("run_date"),
        slug=payload.get("slug"),
        event_date=payload.get("event_date"),
        title=payload.get("title"),
        outcome=payload.get("outcome"),
        side=payload.get("side"),
        action=payload.get("action"),
        decided_state=payload.get("decided_state"),
        will_order=payload.get("will_order"),
        reason_codes=payload.get("reason_codes"),
        reasons=payload.get("reasons"),
        details=payload.get("details"),
        reason_codes_json=payload.get("reason_codes_json"),
        reasons_json=payload.get("reasons_json"),
        details_json=payload.get("details_json"),
        created_at=payload.get("created_at"),
        ensure_ascii=ensure_ascii,
    )


def insert_auto_trade_decision(
    target: Any,
    *,
    attempts: int = 8,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
    ensure_ascii: bool = False,
    **fields: Any,
) -> int:
    row = build_auto_trade_decision_row(ensure_ascii=ensure_ascii, **fields)
    result = _execute_with_retry(
        target,
        AUTO_TRADE_DECISION_INSERT_SQL,
        row,
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    cursor = result if hasattr(result, "lastrowid") else target
    try:
        row_id = int(getattr(cursor, "lastrowid", 0) or 0)
    except Exception:
        return 0
    _merge_fill_inserted_decision_identity(
        target,
        row_id=row_id,
        fields=fields,
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return row_id


def insert_auto_trade_decision_payload(
    target: Any,
    payload: Mapping[str, Any],
    *,
    attempts: int = 8,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
    ensure_ascii: bool = False,
) -> int:
    row = build_auto_trade_decision_row_from_payload(payload, ensure_ascii=ensure_ascii)
    result = _execute_with_retry(
        target,
        AUTO_TRADE_DECISION_INSERT_SQL,
        row,
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    cursor = result if hasattr(result, "lastrowid") else target
    try:
        row_id = int(getattr(cursor, "lastrowid", 0) or 0)
    except Exception:
        return 0
    _merge_fill_inserted_decision_identity(
        target,
        row_id=row_id,
        fields=payload,
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return row_id


def insert_auto_trade_decision_rows(
    target: Any,
    rows: Iterable[Tuple[Any, ...]],
    *,
    attempts: int = 8,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
) -> int:
    row_list = list(rows)
    if not row_list:
        return 0
    _executemany_with_retry(
        target,
        AUTO_TRADE_DECISION_INSERT_SQL,
        row_list,
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return len(row_list)


def fill_missing_auto_trade_decision_run_id(
    target: Any,
    *,
    decision_id: Any,
    run_id: Any,
    attempts: int = 8,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
) -> bool:
    try:
        decision_id_int = int(decision_id)
        run_id_int = int(run_id)
    except Exception:
        return False
    if decision_id_int <= 0 or run_id_int <= 0:
        return False
    _execute_with_retry(
        target,
        """
        UPDATE auto_trade_decisions
        SET run_id = ?
        WHERE id = ?
          AND (run_id IS NULL OR run_id <= 0)
        """,
        (run_id_int, decision_id_int),
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return True
