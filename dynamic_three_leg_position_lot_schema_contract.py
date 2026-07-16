"""Production SSOT schema for dynamic-three-leg position lots.

The legacy ``edge_trades`` table remains an order/position compatibility
ledger.  It is deliberately not the strategy-position authority for the new
lane because it cannot bind a fill to a strategy leg, artifact, intent and
monotonic lot version.
"""

from __future__ import annotations

import sqlite3
from typing import Any


SCHEMA_VERSION = "dynamic_three_leg_position_lot_schema_v1"

TABLE_DDL = {
    "dynamic_three_leg_position_lots": """
        CREATE TABLE IF NOT EXISTS dynamic_three_leg_position_lots (
            position_lot_id TEXT PRIMARY KEY,
            position_lot_version INTEGER NOT NULL CHECK(position_lot_version >= 1),
            management_scope TEXT NOT NULL CHECK(management_scope='dynamic_three_leg_weather'),
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            bucket TEXT NOT NULL,
            outcome_side TEXT NOT NULL CHECK(outcome_side IN ('YES','NO')),
            token_id TEXT NOT NULL,
            entry_strategy_leg TEXT NOT NULL CHECK(entry_strategy_leg IN (
                'top_yes_wave','second_yes','strongly_excluded_no'
            )),
            entry_strategy_artifact_sha256 TEXT NOT NULL,
            entry_order_intent_id TEXT NOT NULL UNIQUE,
            entry_posterior_run_id TEXT NOT NULL,
            entry_price REAL NOT NULL CHECK(entry_price > 0 AND entry_price < 1),
            acquired_size REAL NOT NULL CHECK(acquired_size > 0),
            remaining_size REAL NOT NULL CHECK(remaining_size >= 0),
            remaining_cost_usdc REAL NOT NULL CHECK(remaining_cost_usdc >= 0),
            frozen_take_profit_price REAL,
            state TEXT NOT NULL CHECK(state IN ('open','closed')),
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            updated_at TEXT NOT NULL,
            CHECK(
                (entry_strategy_leg='top_yes_wave' AND frozen_take_profit_price=0.90)
                OR (entry_strategy_leg<>'top_yes_wave' AND frozen_take_profit_price IS NULL)
            ),
            CHECK(
                (state='open' AND remaining_size > 0 AND closed_at IS NULL)
                OR (state='closed' AND remaining_size = 0 AND remaining_cost_usdc = 0 AND closed_at IS NOT NULL)
            )
        )
    """,
    "dynamic_three_leg_position_lot_events": """
        CREATE TABLE IF NOT EXISTS dynamic_three_leg_position_lot_events (
            position_lot_event_id TEXT PRIMARY KEY,
            position_lot_id TEXT NOT NULL,
            position_lot_version_before INTEGER NOT NULL CHECK(position_lot_version_before >= 0),
            position_lot_version_after INTEGER NOT NULL CHECK(position_lot_version_after >= 1),
            order_intent_id TEXT NOT NULL,
            order_action TEXT NOT NULL CHECK(order_action IN ('BUY','SELL')),
            cumulative_filled_size REAL NOT NULL CHECK(cumulative_filled_size > 0),
            applied_size REAL NOT NULL CHECK(applied_size > 0),
            applied_price REAL NOT NULL CHECK(applied_price > 0 AND applied_price < 1),
            reason_family TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            event_json TEXT NOT NULL,
            event_sha256 TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            UNIQUE(order_intent_id,cumulative_filled_size),
            FOREIGN KEY(position_lot_id) REFERENCES dynamic_three_leg_position_lots(position_lot_id)
        )
    """,
}

INDEX_DDL = {
    "idx_dynamic_three_leg_position_lots_open_event": """
        CREATE INDEX IF NOT EXISTS idx_dynamic_three_leg_position_lots_open_event
        ON dynamic_three_leg_position_lots(slug,event_date,state,updated_at)
    """,
    "idx_dynamic_three_leg_position_lot_events_lot": """
        CREATE INDEX IF NOT EXISTS idx_dynamic_three_leg_position_lot_events_lot
        ON dynamic_three_leg_position_lot_events(position_lot_id,position_lot_version_after)
    """,
}


def dynamic_three_leg_position_lot_schema_plan(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_tables = sorted(set(TABLE_DDL) - tables)
    missing_indexes = sorted(set(INDEX_DDL) - indexes)
    return {
        "schema": SCHEMA_VERSION,
        "ok": not missing_tables and not missing_indexes,
        "state": "ready" if not missing_tables and not missing_indexes else "apply_required",
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "writer_quiescence_required": bool(missing_tables or missing_indexes),
        "places_orders": False,
    }


def apply_dynamic_three_leg_position_lot_schema(
    conn: sqlite3.Connection, *, writer_quiescent: bool,
) -> dict[str, Any]:
    plan = dynamic_three_leg_position_lot_schema_plan(conn)
    if plan["writer_quiescence_required"] and not writer_quiescent:
        return {
            **plan,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["writer_quiescent_assertion_required"],
        }
    for ddl in TABLE_DDL.values():
        conn.execute(ddl)
    for ddl in INDEX_DDL.values():
        conn.execute(ddl)
    result = dynamic_three_leg_position_lot_schema_plan(conn)
    return {**result, "applied": bool(plan["writer_quiescence_required"]), "places_orders": False}


__all__ = [
    "INDEX_DDL",
    "SCHEMA_VERSION",
    "TABLE_DDL",
    "apply_dynamic_three_leg_position_lot_schema",
    "dynamic_three_leg_position_lot_schema_plan",
]
