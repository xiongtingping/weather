"""Independent schema for same-posterior sell-to-buy continuation cycles."""

from __future__ import annotations

import sqlite3
from typing import Any


SCHEMA_VERSION = "dynamic_three_leg_continuation_schema_v1"

TABLE_DDL = {
    "dynamic_three_leg_continuation_events": """
        CREATE TABLE IF NOT EXISTS dynamic_three_leg_continuation_events (
            continuation_event_id TEXT PRIMARY KEY,
            posterior_run_id TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            previous_transition_plan_id TEXT NOT NULL UNIQUE,
            transition_generation INTEGER NOT NULL CHECK(transition_generation >= 2),
            state TEXT NOT NULL CHECK(state IN ('pending','claimed','completed','superseded','blocked')),
            claim_generation INTEGER NOT NULL DEFAULT 0 CHECK(claim_generation >= 0),
            lease_owner TEXT,
            lease_until TEXT,
            completed_transition_plan_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(posterior_run_id,transition_generation)
        )
    """,
    "dynamic_three_leg_continuation_transition_plans": """
        CREATE TABLE IF NOT EXISTS dynamic_three_leg_continuation_transition_plans (
            transition_plan_id TEXT PRIMARY KEY,
            continuation_event_id TEXT NOT NULL UNIQUE,
            transition_generation INTEGER NOT NULL CHECK(transition_generation >= 2),
            trading_plan_id TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL,
            realization_state_id TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            position_snapshot_id TEXT NOT NULL,
            strategy_artifact_sha256 TEXT NOT NULL,
            market_snapshot_id TEXT NOT NULL,
            capital_snapshot_id TEXT NOT NULL,
            risk_snapshot_id TEXT NOT NULL,
            capital_epoch_id TEXT NOT NULL,
            risk_reservation_id TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('SELL_OLD','BUY_NEW','HOLD','BLOCKED')),
            transition_json TEXT NOT NULL,
            transition_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','completed','superseded','blocked')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(posterior_run_id,transition_generation)
        )
    """,
}

INDEX_DDL = {
    "idx_dynamic_three_leg_continuation_ready": """
        CREATE INDEX IF NOT EXISTS idx_dynamic_three_leg_continuation_ready
        ON dynamic_three_leg_continuation_events(state,lease_until,created_at)
    """,
    "idx_dynamic_three_leg_continuation_transition_state": """
        CREATE INDEX IF NOT EXISTS idx_dynamic_three_leg_continuation_transition_state
        ON dynamic_three_leg_continuation_transition_plans(phase,state,updated_at)
    """,
}


def continuation_schema_plan(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
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


def apply_continuation_schema(conn: sqlite3.Connection, *, writer_quiescent: bool) -> dict[str, Any]:
    plan = continuation_schema_plan(conn)
    if plan["writer_quiescence_required"] and not writer_quiescent:
        return {**plan, "ok": False, "state": "blocked", "block_reasons": ["writer_quiescent_assertion_required"]}
    for ddl in TABLE_DDL.values():
        conn.execute(ddl)
    for ddl in INDEX_DDL.values():
        conn.execute(ddl)
    return {**continuation_schema_plan(conn), "applied": bool(plan["writer_quiescence_required"]), "places_orders": False}


__all__ = ["apply_continuation_schema", "continuation_schema_plan", "SCHEMA_VERSION"]
