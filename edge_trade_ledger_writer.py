from __future__ import annotations

import sqlite3
import time
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


EDGE_TRADE_COLUMNS = {
    "slug",
    "event_date",
    "outcome",
    "side",
    "state",
    "entry_at",
    "last_order_id",
    "entry_price",
    "entry_size",
    "last_action",
    "last_action_price",
    "last_action_size",
    "last_action_order_type",
    "last_action_at",
    "last_status",
    "last_error",
    "exit_reasons",
    "exit_reason_codes",
    "exit_reason_family_codes",
    "exit_triggered_at",
    "stop_loss_probe_at",
    "exit_skip_reason",
    "exit_skip_detail",
    "exit_skip_at",
    "trend_limit_step",
    "entry_retry_count",
    "exit_retry_count",
    "position_size",
    "avg_price",
    "peak_price",
    "realized_pnl",
    "unrealized_pnl",
    "mark_price",
    "last_fill_at",
    "updated_at",
}


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


def _filtered_values(values: Mapping[str, Any], allowed_columns: Optional[Iterable[str]] = None) -> dict[str, Any]:
    allowed = set(allowed_columns or EDGE_TRADE_COLUMNS)
    return {str(key): value for key, value in values.items() if str(key) in EDGE_TRADE_COLUMNS and str(key) in allowed}


def _target_table_columns(target: Any, table: str) -> Optional[set[str]]:
    conn = target if isinstance(target, sqlite3.Connection) else getattr(target, "connection", None)
    if not isinstance(conn, sqlite3.Connection):
        return None
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return None
    return {str(row[1]) for row in rows}


def insert_edge_trade_row(
    target: Any,
    values: Mapping[str, Any],
    *,
    table_columns: Optional[Iterable[str]] = None,
    attempts: int = 5,
) -> int:
    filtered = _filtered_values(values, table_columns)
    if not filtered:
        return 0
    columns = list(filtered.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    result = _execute_with_retry(
        target,
        f"INSERT INTO edge_trades({column_sql}) VALUES({placeholders})",
        tuple(filtered[column] for column in columns),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def update_edge_trade_by_key(
    target: Any,
    *,
    slug: Any,
    event_date: Any,
    outcome: Any,
    side: Any,
    values: Mapping[str, Any],
    event_date_match: str = "exact",
    require_last_error: Any = None,
    require_state: Any = None,
    require_missing_last_order_id: bool = False,
    require_active_or_position: bool = False,
    require_positive_position: bool = False,
    max_position_size: Any = None,
    attempts: int = 5,
) -> int:
    filtered = _filtered_values(values)
    if not filtered:
        return 0
    set_sql = ", ".join(f"{column} = ?" for column in filtered)
    params: list[Any] = list(filtered.values())
    where_parts = ["slug = ?"]
    params.append(str(slug or ""))
    if event_date_match == "substr10":
        where_parts.append("substr(event_date, 1, 10) = ?")
    else:
        where_parts.append("event_date = ?")
    params.append(str(event_date or ""))
    where_parts.extend(["outcome = ?", "side = ?"])
    params.extend([str(outcome or ""), str(side or "")])
    if require_last_error is not None:
        where_parts.append("COALESCE(last_error, '') = ?")
        params.append(str(require_last_error or ""))
    if require_state is not None:
        where_parts.append("state = ?")
        params.append(str(require_state or ""))
    if require_missing_last_order_id:
        where_parts.append("COALESCE(last_order_id, '') = ''")
    if require_active_or_position:
        where_parts.append("(state IN ('entry', 'entry_pending') OR COALESCE(position_size, 0) > 0)")
    if require_positive_position:
        where_parts.append("COALESCE(position_size, 0) > 0")
    if max_position_size is not None:
        where_parts.append("COALESCE(position_size, 0) <= ?")
        params.append(max_position_size)
    result = _execute_with_retry(
        target,
        f"UPDATE edge_trades SET {set_sql} WHERE {' AND '.join(where_parts)}",
        tuple(params),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def update_edge_trade_by_id(
    target: Any,
    *,
    row_id: Any,
    values: Mapping[str, Any],
    require_positive_position: bool = False,
    attempts: int = 5,
) -> int:
    filtered = _filtered_values(values)
    if not filtered:
        return 0
    set_sql = ", ".join(f"{column} = ?" for column in filtered)
    params: list[Any] = list(filtered.values())
    params.append(int(row_id))
    where_sql = "id = ?"
    if require_positive_position:
        where_sql += " AND COALESCE(position_size, 0) > 0"
    result = _execute_with_retry(
        target,
        f"UPDATE edge_trades SET {set_sql} WHERE {where_sql}",
        tuple(params),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def upsert_smoke_test_entry(
    target: Any,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    order_id: str,
    price: Any,
    size: Any,
    now_iso: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        INSERT INTO edge_trades (
          slug, event_date, outcome, side, state,
          entry_at, last_order_id, entry_price, entry_size,
          last_action, last_action_price, last_action_size, last_action_order_type, last_action_at,
          last_status, last_error, position_size, avg_price, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug, event_date, outcome, side)
        DO UPDATE SET state=excluded.state,
                      entry_at=COALESCE(excluded.entry_at, edge_trades.entry_at),
                      last_order_id=excluded.last_order_id,
                      entry_price=COALESCE(excluded.entry_price, edge_trades.entry_price),
                      entry_size=COALESCE(excluded.entry_size, edge_trades.entry_size),
                      last_action=excluded.last_action,
                      last_action_price=excluded.last_action_price,
                      last_action_size=excluded.last_action_size,
                      last_action_order_type=excluded.last_action_order_type,
                      last_action_at=excluded.last_action_at,
                      last_status=excluded.last_status,
                      last_error=excluded.last_error,
                      position_size=excluded.position_size,
                      avg_price=COALESCE(excluded.avg_price, edge_trades.avg_price),
                      updated_at=excluded.updated_at
        """,
        (
            slug,
            event_date,
            outcome,
            side,
            "entry",
            now_iso,
            order_id,
            price,
            size,
            "smoke_test_entry",
            price,
            size,
            "GTC",
            now_iso,
            "buy_matched",
            "",
            size,
            price,
            now_iso,
        ),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def upsert_edge_trade_runtime_row(
    target: Any,
    values: Mapping[str, Any],
    *,
    attempts: int = 6,
) -> int:
    result = _execute_with_retry(
        target,
        """
        INSERT INTO edge_trades (
            slug, event_date, outcome, side, state,
            entry_at, last_order_id,
            entry_price, entry_size,
            last_action, last_action_price, last_action_size, last_action_order_type, last_action_at, last_status, last_error,
            exit_reasons, exit_reason_codes, exit_reason_family_codes, exit_triggered_at,
            exit_skip_reason, exit_skip_detail, exit_skip_at,
            trend_limit_step,
            entry_retry_count, exit_retry_count,
            position_size, avg_price, peak_price,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug, event_date, outcome, side)
        DO UPDATE SET state = excluded.state,
                      entry_at = COALESCE(excluded.entry_at, edge_trades.entry_at),
                      last_order_id = COALESCE(excluded.last_order_id, edge_trades.last_order_id),
                      entry_price = COALESCE(excluded.entry_price, edge_trades.entry_price),
                      entry_size = COALESCE(excluded.entry_size, edge_trades.entry_size),
                      last_action = COALESCE(excluded.last_action, edge_trades.last_action),
                      last_action_price = COALESCE(excluded.last_action_price, edge_trades.last_action_price),
                      last_action_size = COALESCE(excluded.last_action_size, edge_trades.last_action_size),
                      last_action_order_type = COALESCE(excluded.last_action_order_type, edge_trades.last_action_order_type),
                      last_action_at = COALESCE(excluded.last_action_at, edge_trades.last_action_at),
                      last_status = COALESCE(excluded.last_status, edge_trades.last_status),
                      last_error = COALESCE(excluded.last_error, edge_trades.last_error),
                      exit_reasons = COALESCE(excluded.exit_reasons, edge_trades.exit_reasons),
                      exit_reason_codes = COALESCE(excluded.exit_reason_codes, edge_trades.exit_reason_codes),
                      exit_reason_family_codes = COALESCE(NULLIF(excluded.exit_reason_family_codes, ''), edge_trades.exit_reason_family_codes),
                      exit_triggered_at = COALESCE(excluded.exit_triggered_at, edge_trades.exit_triggered_at),
                      exit_skip_reason = COALESCE(excluded.exit_skip_reason, edge_trades.exit_skip_reason),
                      exit_skip_detail = COALESCE(excluded.exit_skip_detail, edge_trades.exit_skip_detail),
                      exit_skip_at = COALESCE(excluded.exit_skip_at, edge_trades.exit_skip_at),
                      trend_limit_step = COALESCE(excluded.trend_limit_step, edge_trades.trend_limit_step),
                      entry_retry_count = COALESCE(excluded.entry_retry_count, edge_trades.entry_retry_count),
                      exit_retry_count = COALESCE(excluded.exit_retry_count, edge_trades.exit_retry_count),
                      position_size = COALESCE(excluded.position_size, edge_trades.position_size),
                      avg_price = COALESCE(excluded.avg_price, edge_trades.avg_price),
                      peak_price = COALESCE(excluded.peak_price, edge_trades.peak_price),
                      updated_at = excluded.updated_at
        """,
        (
            values.get("slug"),
            values.get("event_date"),
            values.get("outcome"),
            values.get("side"),
            values.get("state"),
            values.get("entry_at"),
            values.get("last_order_id"),
            values.get("entry_price"),
            values.get("entry_size"),
            values.get("last_action"),
            values.get("last_action_price"),
            values.get("last_action_size"),
            values.get("last_action_order_type"),
            values.get("last_action_at"),
            values.get("last_status"),
            values.get("last_error"),
            values.get("exit_reasons"),
            values.get("exit_reason_codes"),
            values.get("exit_reason_family_codes"),
            values.get("exit_triggered_at"),
            values.get("exit_skip_reason"),
            values.get("exit_skip_detail"),
            values.get("exit_skip_at"),
            values.get("trend_limit_step"),
            values.get("entry_retry_count"),
            values.get("exit_retry_count"),
            values.get("position_size"),
            values.get("avg_price"),
            values.get("peak_price"),
            values.get("updated_at"),
        ),
        attempts=attempts,
        sleep_base_sec=0.5,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def set_stop_loss_probe_at(
    target: Any,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    probe_at: Any,
    updated_at: str,
    attempts: int = 6,
) -> int:
    return update_edge_trade_by_key(
        target,
        slug=slug,
        event_date=event_date,
        outcome=outcome,
        side=side,
        values={"stop_loss_probe_at": probe_at, "updated_at": updated_at},
        attempts=attempts,
    )


def reconcile_exit_no_position_blockers(
    target: Any,
    *,
    now_iso: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE edge_trades
        SET last_action = 'exit_reconcile',
            last_action_at = ?,
            last_status = 'no_position',
            last_error = '',
            updated_at = ?
        WHERE state = 'exit'
          AND (position_size IS NULL OR position_size <= 0)
          AND (last_order_id IS NULL OR last_order_id = '')
          AND last_error IN ('pending_timeout', 'confirm_wait_sync', 'order_not_found', 'confirm_not_found')
        """,
        (now_iso, now_iso),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def settled_reconcile_clear_position(
    target: Any,
    *,
    now_iso: str,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    attempts: int = 5,
) -> int:
    return update_edge_trade_by_key(
        target,
        slug=slug,
        event_date=event_date,
        outcome=outcome,
        side=side,
        require_active_or_position=True,
        values={
            "state": "exit",
            "position_size": 0,
            "last_order_id": "",
            "last_action": "settled_reconcile_clear",
            "last_action_at": now_iso,
            "last_status": "settled_no_position",
            "last_error": "settled_no_position",
            "exit_reasons": "",
            "exit_reason_codes": "",
            "exit_reason_family_codes": "",
            "exit_triggered_at": None,
            "exit_skip_reason": "",
            "exit_skip_detail": "",
            "exit_skip_at": None,
            "stop_loss_probe_at": None,
            "updated_at": now_iso,
        },
        attempts=attempts,
    )


def settled_reconcile_mark_open(
    target: Any,
    *,
    now_iso: str,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    status: str,
    attempts: int = 5,
) -> int:
    return update_edge_trade_by_key(
        target,
        slug=slug,
        event_date=event_date,
        outcome=outcome,
        side=side,
        values={
            "last_status": status,
            "last_error": status,
            "exit_reasons": "",
            "exit_reason_codes": "",
            "exit_reason_family_codes": "",
            "exit_triggered_at": None,
            "exit_skip_reason": "",
            "exit_skip_detail": "",
            "exit_skip_at": None,
            "stop_loss_probe_at": None,
            "updated_at": now_iso,
        },
        attempts=attempts,
    )


def mark_no_orderbook_skip_details(
    target: Any,
    *,
    now_iso: str,
    slug: Optional[str] = None,
    event_date: Optional[str] = None,
    attempts: int = 2,
    retry_sleep_sec: float = 0.4,
    sleep_cap_sec: float = 0.8,
) -> int:
    params: list[Any] = [now_iso]
    where_parts = [
        "last_error IS NOT NULL",
        "lower(last_error) LIKE '%orderbook%'",
        "(lower(last_error) LIKE '%does not exist%' OR lower(last_error) LIKE '%not exist%')",
    ]
    if slug:
        where_parts.append("slug = ?")
        params.append(slug)
    if event_date:
        where_parts.append("event_date = ?")
        params.append(event_date)
    result = _execute_with_retry(
        target,
        f"""
        UPDATE edge_trades
        SET exit_skip_reason = 'no_orderbook',
            exit_skip_detail = COALESCE(exit_skip_detail, last_error),
            exit_skip_at = COALESCE(exit_skip_at, last_action_at, ?)
        WHERE {' AND '.join(where_parts)}
        """,
        tuple(params),
        attempts=attempts,
        sleep_base_sec=retry_sleep_sec,
        sleep_cap_sec=sleep_cap_sec,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def residual_clear_edge_trade(
    target: Any,
    *,
    now_iso: str,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    attempts: int = 5,
) -> int:
    return update_edge_trade_by_key(
        target,
        slug=slug,
        event_date=event_date,
        outcome=outcome,
        side=side,
        values={
            "state": "exit",
            "position_size": 0.0,
            "last_action": "residual_clear",
            "last_action_at": now_iso,
            "last_status": "residual_clear",
            "last_error": "",
            "updated_at": now_iso,
        },
        attempts=attempts,
    )


def upsert_shadow_position_from_fill(
    target: Any,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    state: str,
    entry_at: Any,
    order_id: str,
    position_size: float,
    avg_price: float,
    last_fill_at: str,
    updated_at: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        INSERT INTO edge_trades (
            slug, event_date, outcome, side, state,
            entry_at, last_order_id,
            position_size, avg_price, last_fill_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug, event_date, outcome, side)
        DO UPDATE SET
            state = excluded.state,
            entry_at = COALESCE(edge_trades.entry_at, excluded.entry_at),
            last_order_id = COALESCE(NULLIF(edge_trades.last_order_id, ''), excluded.last_order_id),
            position_size = excluded.position_size,
            avg_price = excluded.avg_price,
            last_fill_at = COALESCE(excluded.last_fill_at, edge_trades.last_fill_at),
            updated_at = excluded.updated_at
        """,
        (
            slug,
            event_date,
            outcome,
            side,
            state,
            entry_at,
            order_id,
            position_size,
            avg_price,
            last_fill_at,
            updated_at,
        ),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def upsert_synced_trade_position(
    target: Any,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    state: str,
    entry_at: Any,
    entry_price: Any,
    entry_size: Any,
    exit_skip_reason: Any,
    exit_skip_detail: Any,
    exit_skip_at: Any,
    position_size: float,
    avg_price: float,
    realized_pnl: Any,
    unrealized_pnl: Any,
    mark_price: Any,
    last_fill_at: Any,
    updated_at: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        INSERT INTO edge_trades (
            slug, event_date, outcome, side, state,
            entry_at, last_order_id,
            entry_price, entry_size,
            last_action, last_action_price, last_action_size, last_action_at, last_status,
            exit_skip_reason, exit_skip_detail, exit_skip_at,
            position_size, avg_price, realized_pnl, unrealized_pnl, mark_price, last_fill_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug, event_date, outcome, side)
        DO UPDATE SET state = excluded.state,
                      entry_at = CASE
                          WHEN excluded.state = 'exit' THEN NULL
                          ELSE COALESCE(excluded.entry_at, edge_trades.entry_at)
                      END,
                      position_size = excluded.position_size,
                      avg_price = excluded.avg_price,
                      realized_pnl = excluded.realized_pnl,
                      unrealized_pnl = excluded.unrealized_pnl,
                      mark_price = excluded.mark_price,
                      last_fill_at = excluded.last_fill_at,
                      exit_skip_reason = COALESCE(excluded.exit_skip_reason, edge_trades.exit_skip_reason),
                      exit_skip_detail = COALESCE(excluded.exit_skip_detail, edge_trades.exit_skip_detail),
                      exit_skip_at = COALESCE(excluded.exit_skip_at, edge_trades.exit_skip_at),
                      updated_at = excluded.updated_at
        """,
        (
            slug,
            event_date,
            outcome,
            side,
            state,
            entry_at,
            None,
            entry_price,
            entry_size,
            None,
            None,
            None,
            None,
            None,
            exit_skip_reason,
            exit_skip_detail,
            exit_skip_at,
            position_size,
            avg_price,
            realized_pnl,
            unrealized_pnl,
            mark_price,
            last_fill_at,
            updated_at,
        ),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def force_clear_expired_positions(
    target: Any,
    *,
    now_iso: str,
    today: str,
    min_position_size: Any,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE edge_trades
        SET position_size = 0,
            state = 'exit',
            last_order_id = '',
            last_action = ?,
            last_action_at = ?,
            last_status = 'no_position',
            last_error = '',
            exit_skip_reason = NULL,
            exit_skip_detail = NULL,
            exit_skip_at = NULL,
            updated_at = ?
        WHERE COALESCE(position_size, 0) > ?
          AND COALESCE(event_date, '') <> ''
          AND substr(event_date, 1, 10) < ?
        """,
        ("expired_force_clear", now_iso, now_iso, min_position_size, today),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def sync_position_clear_by_id(
    target: Any,
    *,
    row_id: Any,
    now_iso: str,
    attempts: int = 5,
) -> int:
    return update_edge_trade_by_id(
        target,
        row_id=row_id,
        values={
            "position_size": 0,
            "state": "exit",
            "last_action": "pos_sync_clear",
            "last_action_at": now_iso,
            "last_status": "no_position",
            "last_error": "",
            "updated_at": now_iso,
        },
        require_positive_position=True,
        attempts=attempts,
    )


def sync_position_mark_entry_by_id(
    target: Any,
    *,
    row_id: Any,
    size: Any,
    avg_price: Any,
    skip_reason: Any,
    skip_detail: Any,
    skip_at: Any,
    now_iso: str,
    attempts: int = 5,
) -> int:
    table_columns = _target_table_columns(target, "edge_trades")
    set_sql = [
        "position_size = ?",
        "avg_price = COALESCE(?, avg_price)",
        "state = 'entry'",
        "last_action = ?",
        "last_action_at = ?",
        "last_status = ?",
        "last_error = ''",
    ]
    params: list[Any] = [size, avg_price, "pos_sync", now_iso, "pos_sync"]
    if table_columns is None or "exit_skip_reason" in table_columns:
        set_sql.append("exit_skip_reason = COALESCE(?, exit_skip_reason)")
        params.append(skip_reason)
    if table_columns is None or "exit_skip_detail" in table_columns:
        set_sql.append("exit_skip_detail = COALESCE(?, exit_skip_detail)")
        params.append(skip_detail)
    if table_columns is None or "exit_skip_at" in table_columns:
        set_sql.append("exit_skip_at = COALESCE(?, exit_skip_at)")
        params.append(skip_at)
    set_sql.append("updated_at = ?")
    params.extend([now_iso, row_id])
    result = _execute_with_retry(
        target,
        f"UPDATE edge_trades SET {', '.join(set_sql)} WHERE id = ?",
        tuple(params),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def reconcile_pending_entry_order_id(
    target: Any,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    order_id: str,
    status: str,
    now_iso: str,
    attempts: int = 5,
) -> int:
    return update_edge_trade_by_key(
        target,
        slug=slug,
        event_date=event_date,
        event_date_match="substr10",
        outcome=outcome,
        side=side,
        require_state="entry_pending",
        require_missing_last_order_id=True,
        values={
            "last_order_id": order_id,
            "last_status": status,
            "last_error": "",
            "updated_at": now_iso,
        },
        attempts=attempts,
    )


def update_position_size_from_fill(
    target: Any,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    position_size: Any,
    last_fill_at: Any,
    now_iso: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE edge_trades
        SET position_size = ?,
            last_fill_at = COALESCE(?, last_fill_at),
            updated_at = ?
        WHERE slug = ? AND event_date = ? AND outcome = ? AND side = ?
        """,
        (position_size, last_fill_at, now_iso, slug, event_date, outcome, side),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def clear_balance_skip_after_no_position(
    target: Any,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    max_position_size: Any,
    now_iso: str,
    attempts: int = 5,
) -> int:
    return update_edge_trade_by_key(
        target,
        slug=slug,
        event_date=event_date,
        outcome=outcome,
        side=side,
        max_position_size=max_position_size,
        values={
            "last_error": None,
            "last_status": None,
            "exit_skip_reason": None,
            "exit_skip_detail": None,
            "exit_skip_at": None,
            "updated_at": now_iso,
        },
        attempts=attempts,
    )


def reconcile_no_position_errors(
    target: Any,
    *,
    now_iso: str,
    max_position_size: Any,
    today: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE edge_trades
        SET state = CASE
                WHEN LOWER(COALESCE(state, '')) IN ('entry', 'entry_pending') THEN 'exit'
                ELSE state
            END,
            position_size = 0,
            last_order_id = '',
            last_status = 'no_position',
            last_error = '',
            last_action = 'reconcile_no_position',
            last_action_at = ?,
            updated_at = ?
        WHERE COALESCE(last_error, '') <> ''
          AND COALESCE(position_size, 0) <= ?
          AND (
                LOWER(COALESCE(state, '')) = 'exit'
                OR (event_date <> '' AND event_date < ?)
              )
          AND (
                LOWER(COALESCE(last_error, '')) IN (
                    'pending_timeout',
                    'confirm_wait_sync',
                    'order_missing',
                    'no_order_id',
                    'order_not_found',
                    'confirm_not_found',
                    'open_order_cancel_empty',
                    'open_order_reprice_failed'
                )
                OR LOWER(COALESCE(last_error, '')) LIKE 'open_order_exists:%'
                OR LOWER(COALESCE(last_error, '')) LIKE '%timeout%'
              )
        """,
        (now_iso, now_iso, max_position_size, today),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def reconcile_past_no_position_entries(
    target: Any,
    *,
    now_iso: str,
    max_position_size: Any,
    today: str,
    attempts: int = 5,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE edge_trades
        SET state = 'exit',
            position_size = 0,
            last_order_id = '',
            last_status = 'no_position',
            last_error = '',
            last_action = 'reconcile_no_position',
            last_action_at = ?,
            exit_skip_reason = NULL,
            exit_skip_detail = NULL,
            exit_skip_at = NULL,
            updated_at = ?
        WHERE LOWER(COALESCE(state, '')) IN ('entry', 'entry_pending', 'entry_failed')
          AND COALESCE(position_size, 0) <= ?
          AND event_date <> ''
          AND event_date < ?
        """,
        (now_iso, now_iso, max_position_size, today),
        attempts=attempts,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def copy_acceptance_clear_target(
    target: Any,
    *,
    now_iso: str,
    slug: str,
    event_date: str,
    target_bucket: str,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE edge_trades
        SET state = 'exit',
            position_size = 0,
            avg_price = COALESCE(avg_price, 0),
            last_action = 'copy_acceptance_clear_target',
            last_status = 'copy_acceptance_forced_switch',
            updated_at = ?
        WHERE slug = ?
          AND substr(event_date, 1, 10) = ?
          AND outcome = ?
          AND side = 'YES'
        """,
        (now_iso, slug, event_date, target_bucket),
    )
    return int(getattr(result, "rowcount", 0) or 0)


def copy_acceptance_seed_old(
    target: Any,
    *,
    now_iso: str,
    slug: str,
    event_date: str,
    old_bucket: str,
) -> int:
    result = _execute_with_retry(
        target,
        """
        UPDATE edge_trades
        SET state = 'entry',
            position_size = CASE WHEN COALESCE(position_size, 0) > 1 THEN position_size ELSE 8.0 END,
            avg_price = CASE WHEN COALESCE(avg_price, 0) > 0 THEN avg_price ELSE 0.25 END,
            last_action = 'copy_acceptance_seed_old',
            last_status = 'copy_acceptance_forced_switch',
            updated_at = ?
        WHERE slug = ?
          AND substr(event_date, 1, 10) = ?
          AND outcome = ?
          AND side = 'YES'
        """,
        (now_iso, slug, event_date, old_bucket),
    )
    return int(getattr(result, "rowcount", 0) or 0)
