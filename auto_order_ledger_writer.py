from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional, Tuple


AUTO_ORDER_COLUMNS = (
    "order_id",
    "correlation_id",
    "decision_id",
    "signal_id",
    "order_intent_id",
    "slug",
    "event_date",
    "outcome",
    "side",
    "action",
    "price",
    "size",
    "order_type",
    "q_raw",
    "q_calibrated",
    "q_calibrated_lcb",
    "q_calibrated_ucb",
    "q_calibrated_sigma",
    "q_calibration_version",
    "q_edge",
    "strategy",
    "created_at",
    "order_status",
    "order_checked_at",
)

OPTIONAL_AUTO_ORDER_LINEAGE_COLUMNS = frozenset(
    ("correlation_id", "decision_id", "signal_id", "order_intent_id")
)


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


def _table_columns(target: Any, table_name: str) -> set[str]:
    try:
        rows = _execute_with_retry(target, f"PRAGMA table_info({table_name})")
    except sqlite3.Error:
        return set()
    try:
        return {str(row[1]) for row in rows.fetchall()}
    except Exception:
        return set()


def insert_auto_order_ignore(
    target: Any,
    *,
    attempts: int = 5,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
    **fields: Any,
) -> int:
    available_columns = _table_columns(target, "auto_orders")
    columns = []
    for column in AUTO_ORDER_COLUMNS:
        if column not in fields:
            continue
        if (
            column in OPTIONAL_AUTO_ORDER_LINEAGE_COLUMNS
            and column not in available_columns
        ):
            continue
        columns.append(column)
    if not columns:
        return 0
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    result = _execute_with_retry(
        target,
        f"INSERT OR IGNORE INTO auto_orders ({column_sql}) VALUES ({placeholders})",
        tuple(fields.get(column) for column in columns),
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def merge_fill_auto_order_lineage_by_order_id(
    target: Any,
    *,
    order_id: Any,
    correlation_id: Any = "",
    decision_id: Any = "",
    signal_id: Any = "",
    order_intent_id: Any = "",
    attempts: int = 5,
    sleep_base_sec: float = 0.25,
    sleep_cap_sec: float = 2.0,
) -> int:
    order_id_text = str(order_id or "").strip()
    if not order_id_text:
        return 0
    available_columns = _table_columns(target, "auto_orders")
    incoming = {
        "correlation_id": str(correlation_id or "").strip(),
        "decision_id": str(decision_id or "").strip(),
        "signal_id": str(signal_id or "").strip(),
        "order_intent_id": str(order_intent_id or "").strip(),
    }
    assignments = []
    params = []
    for column, value in incoming.items():
        if column not in available_columns or not value:
            continue
        assignments.append(f"{column} = COALESCE(NULLIF({column}, ''), ?)")
        params.append(value)
    if not assignments:
        return 0
    params.append(order_id_text)
    result = _execute_with_retry(
        target,
        f"UPDATE auto_orders SET {', '.join(assignments)} WHERE order_id = ?",
        tuple(params),
        attempts=attempts,
        sleep_base_sec=sleep_base_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def update_auto_order_status_by_id(
    target: Any,
    *,
    row_id: Any,
    order_status: Any,
    order_checked_at: Any,
    only_if_blank: bool = False,
    attempts: int = 5,
) -> int:
    if only_if_blank:
        sql = """
        UPDATE auto_orders
        SET order_status = COALESCE(NULLIF(order_status, ''), ?),
            order_checked_at = ?
        WHERE id = ?
        """
    else:
        sql = """
        UPDATE auto_orders
        SET order_status = ?,
            order_checked_at = ?
        WHERE id = ?
        """
    result = _execute_with_retry(
        target,
        sql,
        (str(order_status or ""), str(order_checked_at or ""), int(row_id)),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def update_auto_order_status_by_order_id(
    target: Any,
    *,
    order_id: Any,
    order_status: Any,
    order_checked_at: Any,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE auto_orders
        SET order_status = ?,
            order_checked_at = ?
        WHERE order_id = ?
        """,
        (str(order_status or ""), str(order_checked_at or ""), str(order_id or "")),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def update_auto_order_strategy_by_id(
    target: Any,
    *,
    row_id: Any,
    strategy: Any,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        "UPDATE auto_orders SET strategy = ? WHERE id = ?",
        (str(strategy or ""), int(row_id)),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)
