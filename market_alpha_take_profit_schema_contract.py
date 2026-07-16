"""Independent controlled schema for dynamic top-YES take-profit cycles."""

from __future__ import annotations

import sqlite3
from typing import Any


SCHEMA_VERSION = "market_alpha_take_profit_schema_v1"

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS market_alpha_take_profit_cycles (
    market_alpha_cycle_id TEXT PRIMARY KEY,
    posterior_run_id TEXT NOT NULL,
    realization_state_id TEXT NOT NULL,
    fact_snapshot_id TEXT NOT NULL,
    position_snapshot_id TEXT NOT NULL,
    position_lot_id TEXT NOT NULL,
    position_lot_version TEXT NOT NULL,
    strategy_artifact_sha256 TEXT NOT NULL,
    market_snapshot_id TEXT NOT NULL,
    bucket TEXT NOT NULL,
    token_id TEXT NOT NULL,
    requested_sell_size REAL NOT NULL CHECK(requested_sell_size > 0),
    frozen_take_profit_price REAL NOT NULL CHECK(frozen_take_profit_price = 0.90),
    executable_sell_price REAL NOT NULL CHECK(executable_sell_price >= frozen_take_profit_price AND executable_sell_price < 1),
    reason_family TEXT NOT NULL CHECK(reason_family='market_alpha_take_profit'),
    reason_code TEXT NOT NULL CHECK(reason_code='market_alpha_take_profit_top_yes_wave_completed'),
    decision_id TEXT NOT NULL UNIQUE,
    order_intent_id TEXT NOT NULL UNIQUE,
    activation_contract_sha256 TEXT NOT NULL,
    quote_expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('prepared','submitting','gateway_accepted','reconcile_required','partially_filled','completed','blocked','expired')),
    cycle_json TEXT NOT NULL,
    cycle_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(position_lot_id,position_lot_version,posterior_run_id,reason_code)
)
"""

INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_market_alpha_take_profit_cycles_ready
ON market_alpha_take_profit_cycles(state,quote_expires_at,updated_at)
"""


def market_alpha_take_profit_schema_plan(conn: sqlite3.Connection) -> dict[str, Any]:
    table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='market_alpha_take_profit_cycles'"
    ).fetchone()
    index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_market_alpha_take_profit_cycles_ready'"
    ).fetchone()
    actions = []
    if table is None:
        actions.append("create_market_alpha_take_profit_cycles")
    if index is None:
        actions.append("create_market_alpha_take_profit_ready_index")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not actions,
        "state": "ready" if not actions else "apply_required",
        "actions": actions,
        "writer_quiescence_required": bool(actions),
        "places_orders": False,
    }


def apply_market_alpha_take_profit_schema(
    conn: sqlite3.Connection, *, writer_quiescent: bool,
) -> dict[str, Any]:
    plan = market_alpha_take_profit_schema_plan(conn)
    if plan["actions"] and not writer_quiescent:
        return {**plan, "ok": False, "state": "blocked", "block_reasons": ["writer_quiescent_assertion_required"]}
    conn.execute(TABLE_DDL)
    conn.execute(INDEX_DDL)
    return {**market_alpha_take_profit_schema_plan(conn), "applied": bool(plan["actions"]), "places_orders": False}


__all__ = [
    "SCHEMA_VERSION",
    "apply_market_alpha_take_profit_schema",
    "market_alpha_take_profit_schema_plan",
]
