"""Initialize the complete isolated schema required by the resident P10 shadow.

The P2 canonical mirror is deliberately evidence-only and therefore starts
without the P9 decision/journal/telemetry/prepared-cycle tables.  This module
extends only a database that already carries the explicit isolated-shadow
sentinel.  It creates no posterior, fact, decision, intent, order, or
prepared-cycle row.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dynamic_three_leg_shadow_decision_writer import initialize_isolated_dynamic_three_leg_shadow_decision_schema
from initial_posterior_checkpoint_shadow import (
    INITIAL_LINEAGE_REQUIRED_COLUMNS,
    INITIAL_LINEAGE_TABLE,
    initialize_initial_posterior_lineage_shadow_schema,
)
from observation_fast_lane_shadow_telemetry_writer import initialize_observation_fast_lane_shadow_telemetry_schema
from resident_amsc_specli_arrival_journal_shadow import initialize_resident_amsc_specli_arrival_journal_shadow_schema
from resident_p10_shadow_prepared_cycle import initialize_resident_p10_shadow_prepared_cycle_schema
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
    initialize_isolated_public_ssot_shadow_schema,
)
from sequential_bayes_schema_contract import apply_forward_schema, verify_schema
from strong_observation_lr_runtime_registry import (
    ACTIVATION_REQUIRED_COLUMNS as STRONG_LR_ACTIVATION_REQUIRED_COLUMNS,
    ACTIVATION_TABLE as STRONG_LR_ACTIVATION_TABLE,
    ARTIFACT_REQUIRED_COLUMNS as STRONG_LR_ARTIFACT_REQUIRED_COLUMNS,
    ARTIFACT_TABLE as STRONG_LR_ARTIFACT_TABLE,
    initialize_strong_lr_runtime_registry_schema,
)


SCHEMA_VERSION = "p10_shadow_runtime_schema_v1"
REQUIRED_P10_TABLES = (
    "canonical_observation_events",
    "canonical_observation_event_sources",
    "strong_observation_updates",
    "posterior_checkpoints",
    STRONG_LR_ARTIFACT_TABLE,
    STRONG_LR_ACTIVATION_TABLE,
    INITIAL_LINEAGE_TABLE,
    "realization_states",
    "posterior_commit_outbox",
    "posterior_outbox_execution_contracts",
    "dynamic_three_leg_transition_plans",
    "posterior_execution_intents",
    "event_fact_history",
    "event_fact_latest",
    "trading_plans",
    "auto_trade_decisions",
    "decision_intents",
    "resident_p10_prepared_cycles_shadow",
    "resident_amsc_specli_arrival_journal",
    "observation_fast_lane_latency_packets",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {_text(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ValueError("p10_shadow_runtime_schema_manifest_missing")
    row = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ValueError("p10_shadow_runtime_schema_manifest_invalid")


def build_p10_shadow_runtime_schema_plan(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return only the missing isolated tables; no database mutation occurs."""

    _assert_isolated_shadow_db(conn)
    p1 = verify_schema(conn)
    missing = [table for table in REQUIRED_P10_TABLES if not _table_exists(conn, table)]
    initial_lineage_missing_columns = (
        sorted(set(INITIAL_LINEAGE_REQUIRED_COLUMNS) - _table_columns(conn, INITIAL_LINEAGE_TABLE))
        if _table_exists(conn, INITIAL_LINEAGE_TABLE)
        else []
    )
    missing_columns = {INITIAL_LINEAGE_TABLE: initial_lineage_missing_columns} if initial_lineage_missing_columns else {}
    for table, required_columns in (
        (STRONG_LR_ARTIFACT_TABLE, STRONG_LR_ARTIFACT_REQUIRED_COLUMNS),
        (STRONG_LR_ACTIVATION_TABLE, STRONG_LR_ACTIVATION_REQUIRED_COLUMNS),
    ):
        if _table_exists(conn, table):
            missing_for_table = sorted(set(required_columns) - _table_columns(conn, table))
            if missing_for_table:
                missing_columns[table] = missing_for_table
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "ready" if not missing and not missing_columns else "changes_pending",
        "p1_schema": p1,
        "missing_tables": missing,
        "missing_columns": missing_columns,
        "shadow_only": True,
        "read_only": True,
        "places_orders": False,
        "boundary": {
            "requires_isolated_shadow_manifest": True,
            "production_archive_write": False,
            "creates_runtime_rows": False,
        },
    }


def initialize_p10_shadow_runtime_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Create idempotent P1/P9/P10 empty tables in an isolated shadow DB."""

    _assert_isolated_shadow_db(conn)
    before = build_p10_shadow_runtime_schema_plan(conn)
    # The public shadow tables must exist before P9's isolated decision schema.
    initialize_isolated_public_ssot_shadow_schema(conn)
    p1_apply = apply_forward_schema(conn)
    if verify_schema(conn).get("ok") is not True:
        raise ValueError("p10_shadow_runtime_schema_p1_verification_failed")
    # P0 is a persisted predecessor of every P8/P10 strong update.  Its
    # lineage is append-only and separate from trajectory JSON, because P0
    # must bind exactly one prior/weak/strong-artifact/resolution identity.
    initialize_initial_posterior_lineage_shadow_schema(conn)
    initialize_strong_lr_runtime_registry_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    initialize_resident_p10_shadow_prepared_cycle_schema(conn)
    # These two initializers commit their table-only DDL internally.
    initialize_resident_amsc_specli_arrival_journal_shadow_schema(conn)
    initialize_observation_fast_lane_shadow_telemetry_schema(conn)
    after = build_p10_shadow_runtime_schema_plan(conn)
    if after["missing_tables"] or after["missing_columns"]:
        raise ValueError("p10_shadow_runtime_schema_required_schema_missing_after_apply")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "initialized" if before["missing_tables"] or before["missing_columns"] else "already_initialized",
        "p1_apply": p1_apply,
        "missing_tables_before": before["missing_tables"],
        "missing_tables_after": after["missing_tables"],
        "missing_columns_before": before["missing_columns"],
        "missing_columns_after": after["missing_columns"],
        "shadow_only": True,
        "places_orders": False,
        "boundary": {
            "requires_isolated_shadow_manifest": True,
            "production_archive_write": False,
            "creates_runtime_rows": False,
            "creates_posterior_or_order": False,
        },
    }


__all__ = [
    "REQUIRED_P10_TABLES",
    "SCHEMA_VERSION",
    "build_p10_shadow_runtime_schema_plan",
    "initialize_p10_shadow_runtime_schema",
]
