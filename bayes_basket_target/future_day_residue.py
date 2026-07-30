"""Audit legacy future-settlement-day residue without mutating target SSOT.

The target ledger is append-only.  A historical worker may therefore leave a
blocked row for an ET day that had not started when the row was written.  Such
rows are audit residue, not model, market, basket, order, or calibration input.
This module proves that boundary with a read-only connection and never deletes
or rewrites ledger data.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import sqlite3
from typing import Any, Mapping

from .contracts import content_sha256
from .et_history import event_day_window
from .market_registry import load_started_registry_rows


SCHEMA = "bayes_basket_target_future_day_residue_audit_v1"
OPERATIONAL_TABLES = ("basket_target_execution_cycles", "auto_trade_decisions")
FORMAL_EVENT_TABLES = (
    "basket_probability_envelope",
    "basket_official_ws_snapshot",
    "basket_confirmed_inventory_snapshot",
    "basket_order_state_snapshot",
    "basket_cash_phase_policy_artifact",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _event_state(
    event_date: str,
    settlement_timezone: str,
    as_of_utc: str,
) -> str:
    return event_day_window(
        event_date,
        as_of_utc=as_of_utc,
        target_timezone=settlement_timezone,
    )["state"]


def _created_before_settlement_start(
    event_date: str,
    settlement_timezone: str,
    created_at: str,
) -> bool:
    window = event_day_window(
        event_date,
        as_of_utc=created_at,
        target_timezone=settlement_timezone,
    )
    return window["state"] == "not_started"


def _operational_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    as_of_utc: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not _table_exists(conn, table):
        return [], [f"{table}_missing"]
    columns = _columns(conn, table)
    required = {"event_date", "settlement_timezone", "created_at"}
    missing = sorted(required - columns)
    if missing:
        return [], [f"{table}_columns_missing:{','.join(missing)}"]
    rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    for raw in conn.execute(f"SELECT * FROM {table}").fetchall():
        values = dict(zip((str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")), raw))
        event_date = str(values.get("event_date") or "").strip()
        settlement_timezone = str(
            values.get("settlement_timezone") or ""
        ).strip()
        created_at = str(values.get("created_at") or "").strip()
        try:
            currently_future = (
                _event_state(event_date, settlement_timezone, as_of_utc)
                == "not_started"
            )
            created_before_start = _created_before_settlement_start(
                event_date, settlement_timezone, created_at
            )
        except (TypeError, ValueError, KeyError) as exc:
            blockers.append(f"{table}_invalid_time:{type(exc).__name__}")
            continue
        rows.append({
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "created_at": created_at,
            "currently_future_settlement": currently_future,
            "created_before_settlement_start": created_before_start,
            "cycle_id": str(values.get("cycle_id") or ""),
            "decided_state": str(values.get("decided_state") or ""),
            "will_order": values.get("will_order"),
            "state": str(values.get("state") or ""),
        })
    return rows, blockers


def _future_event_counts(conn: sqlite3.Connection, table: str, *, as_of_utc: str) -> dict[str, Any]:
    if (
        not _table_exists(conn, table)
        or not {"event_date", "settlement_timezone"} <= _columns(conn, table)
    ):
        return {"table": table, "row_count": 0, "currently_future_settlement": 0, "state": "missing_or_no_settlement_scope"}
    counts: Counter[str] = Counter()
    invalid = 0
    for event_date, settlement_timezone, amount in conn.execute(
        f"SELECT event_date,settlement_timezone,COUNT(*) FROM {table} "
        f"GROUP BY event_date,settlement_timezone"
    ):
        try:
            counts[
                _event_state(
                    str(event_date or ""),
                    str(settlement_timezone or ""),
                    as_of_utc,
                )
            ] += int(amount or 0)
        except (TypeError, ValueError, KeyError):
            invalid += int(amount or 0)
    return {
        "table": table,
        "row_count": sum(counts.values()) + invalid,
        "currently_future_settlement": counts["not_started"],
        "started_or_closed_settlement": counts["open"] + counts["closed"],
        "invalid": invalid,
        "state": "checked",
    }


def audit_future_day_residue(conn: sqlite3.Connection, *, as_of_utc: str) -> dict[str, Any]:
    """Return an immutable audit projection; the supplied connection is never written."""

    _utc(as_of_utc)
    blockers: list[str] = []
    operational: dict[str, list[dict[str, Any]]] = {}
    for table in OPERATIONAL_TABLES:
        rows, table_blockers = _operational_rows(conn, table, as_of_utc=as_of_utc)
        operational[table] = rows
        blockers.extend(table_blockers)

    future_cycles = [row for row in operational.get("basket_target_execution_cycles", []) if row["created_before_settlement_start"]]
    future_decisions = [row for row in operational.get("auto_trade_decisions", []) if row["created_before_settlement_start"]]
    future_cycle_ids = {row["cycle_id"] for row in future_cycles if row["cycle_id"]}
    future_decision_cycle_ids = {row["cycle_id"] for row in future_decisions if row["cycle_id"]}

    non_blocked_future_decisions = [
        row for row in future_decisions
        if row["decided_state"] != "blocked" or int(row["will_order"] or 0) != 0
    ]
    non_prepared_future_cycles = [row for row in future_cycles if row["state"] != "prepared"]
    if non_blocked_future_decisions:
        blockers.append("future_decision_not_blocked_or_will_order_nonzero")
    if non_prepared_future_cycles:
        blockers.append("future_cycle_not_prepared")

    downstream: dict[str, int] = {}
    for table in ("basket_target_order_intents", "basket_target_capital_reservations", "basket_target_order_reconciliations"):
        if not _table_exists(conn, table):
            downstream[table] = 0
            continue
        columns = _columns(conn, table)
        if "cycle_id" in columns:
            downstream[table] = sum(
                int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE cycle_id=?", (cycle_id,)).fetchone()[0])
                for cycle_id in future_cycle_ids
            )
        elif "order_intent_id" in columns:
            downstream[table] = 0
        else:
            downstream[table] = 0
    if any(value for value in downstream.values()):
        blockers.append("future_residue_has_order_or_capital_downstream")

    formal_future_counts = [
        _future_event_counts(conn, table, as_of_utc=as_of_utc)
        for table in FORMAL_EVENT_TABLES
    ]
    for item in formal_future_counts:
        if item.get("invalid"):
            blockers.append(f"{item['table']}_invalid_event_dates")

    try:
        started_registry = load_started_registry_rows(conn, as_of_utc=as_of_utc)
        registry_future_rows = [
            row for row in started_registry
            if _event_state(
                str(row.get("event_date") or ""),
                str(row.get("settlement_timezone") or ""),
                as_of_utc,
            ) == "not_started"
        ]
    except Exception as exc:
        started_registry = []
        registry_future_rows = []
        blockers.append(f"started_registry_filter_failed:{type(exc).__name__}")

    if registry_future_rows:
        blockers.append("started_registry_returned_future_scope")

    body = {
        "schema": SCHEMA,
        "as_of_utc": as_of_utc,
        "target_timezone_authority": "market_registry.settlement_timezone",
        "residue_definition": "created_at_before_registry_settlement_day_start",
        "operational": {
            table: {
                "row_count": len(rows),
                "created_before_settlement_start_count": sum(int(row["created_before_settlement_start"]) for row in rows),
                "currently_future_settlement_count": sum(int(row["currently_future_settlement"]) for row in rows),
                "event_dates": sorted({row["event_date"] for row in rows}),
            }
            for table, rows in operational.items()
        },
        "future_residue": {
            "cycle_count": len(future_cycles),
            "decision_count": len(future_decisions),
            "cycle_ids_match_decisions": future_cycle_ids == future_decision_cycle_ids,
            "non_blocked_decision_count": len(non_blocked_future_decisions),
            "non_prepared_cycle_count": len(non_prepared_future_cycles),
            "downstream_counts": downstream,
        },
        "formal_input_future_partitions": formal_future_counts,
        "consumer_filter": {
            "started_registry_scope_count": len(started_registry),
            "started_registry_future_scope_count": len(registry_future_rows),
            "future_scope_excluded": not registry_future_rows,
        },
        "checks": {
            "future_residue_isolated": not blockers,
            "no_order_intents_or_capital_downstream": not any(downstream.values()),
            "future_decisions_are_blocked_no_order": not non_blocked_future_decisions,
            "started_registry_excludes_future": not registry_future_rows,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
        "blockers": sorted(set(blockers)),
    }
    return {**body, "audit_sha256": content_sha256(body)}


__all__ = ["SCHEMA", "audit_future_day_residue"]
