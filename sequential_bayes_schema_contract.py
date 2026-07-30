"""Forward-only storage contract for the sequential Bayes pipeline.

This module deliberately owns only the additive schema required by P1.  It
does not write facts, run inference, or alter existing historical rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "sequential_bayes_schema_contract_v1"
MIGRATION_ID = "sequential_bayes_p1_forward_schema_v1"


TABLE_DDL: Mapping[str, str] = {
    "canonical_observation_events": """
        CREATE TABLE IF NOT EXISTS canonical_observation_events (
            canonical_observation_event_id TEXT PRIMARY KEY,
            canonical_observation_key TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            station_id TEXT NOT NULL,
            report_type TEXT NOT NULL CHECK(report_type IN ('METAR', 'SPECI')),
            valid_time TEXT NOT NULL,
            revision_no INTEGER NOT NULL CHECK(revision_no >= 0),
            normalized_physical_sha256 TEXT NOT NULL,
            selected_source_observation_id TEXT NOT NULL,
            selected_source_id TEXT NOT NULL,
            selection_role TEXT NOT NULL CHECK(selection_role IN ('primary', 'substitute')),
            selection_policy_version TEXT NOT NULL,
            first_transport_seen_at TEXT NOT NULL,
            first_actionable_seen_at TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'candidate', 'accepted_primary', 'accepted_substitute',
                'confirmed', 'superseded', 'conflict_blocked'
            )),
            supersedes_event_id TEXT,
            conflict_group_id TEXT,
            parsed_observation_json TEXT NOT NULL,
            quality_flags_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(canonical_observation_key, revision_no),
            UNIQUE(canonical_observation_key, normalized_physical_sha256)
        )
    """,
    "canonical_observation_event_sources": """
        CREATE TABLE IF NOT EXISTS canonical_observation_event_sources (
            canonical_observation_event_id TEXT NOT NULL,
            source_observation_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            relation TEXT NOT NULL CHECK(relation IN (
                'selected', 'confirmation', 'substitute', 'conflicting', 'superseded'
            )),
            seen_at TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            PRIMARY KEY(canonical_observation_event_id, source_observation_id)
        )
    """,
    "strong_observation_updates": """
        CREATE TABLE IF NOT EXISTS strong_observation_updates (
            strong_observation_update_id TEXT PRIMARY KEY,
            target_identity TEXT NOT NULL,
            previous_posterior_run_id TEXT NOT NULL,
            previous_posterior_sha256 TEXT NOT NULL,
            previous_checkpoint_id TEXT NOT NULL,
            canonical_observation_event_id TEXT NOT NULL,
            forecast_evidence_snapshot_id TEXT NOT NULL,
            prior_context_id TEXT NOT NULL,
            model_artifact_sha256 TEXT NOT NULL,
            decision_as_of TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('applied', 'neutral', 'blocked')),
            raw_log_score_json TEXT NOT NULL,
            raw_log_score_sha256 TEXT NOT NULL,
            normalizer_offset REAL NOT NULL,
            posterior_delta_json TEXT NOT NULL,
            posterior_delta_sha256 TEXT NOT NULL,
            pre_state_transition_json TEXT NOT NULL,
            post_state_transition_json TEXT NOT NULL,
            source_reliability_before_json TEXT NOT NULL,
            source_reliability_after_json TEXT NOT NULL,
            path_auxiliary_json TEXT NOT NULL,
            observed_max_lower_bound_c REAL,
            quality_json TEXT NOT NULL,
            block_reasons_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(
                previous_posterior_run_id,
                previous_posterior_sha256,
                canonical_observation_event_id,
                forecast_evidence_snapshot_id,
                model_artifact_sha256
            )
        )
    """,
    "posterior_checkpoints": """
        CREATE TABLE IF NOT EXISTS posterior_checkpoints (
            posterior_checkpoint_id TEXT PRIMARY KEY,
            target_identity TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL UNIQUE,
            previous_checkpoint_id TEXT,
            checkpoint_kind TEXT NOT NULL CHECK(checkpoint_kind IN ('initial', 'incremental', 'replay', 'final')),
            forecast_evidence_snapshot_id TEXT NOT NULL,
            prior_context_id TEXT NOT NULL,
            resolution_contract_id TEXT NOT NULL,
            last_strong_update_id TEXT,
            last_canonical_event_id TEXT,
            canonical_sequence_sha256 TEXT NOT NULL,
            temperature_grid_id TEXT NOT NULL,
            state_grid_id TEXT NOT NULL,
            joint_posterior_json TEXT NOT NULL,
            joint_posterior_sha256 TEXT NOT NULL,
            physical_high_distribution_json TEXT NOT NULL,
            settlement_scalar_distribution_json TEXT NOT NULL,
            market_bucket_distribution_json TEXT NOT NULL,
            latent_post_state_json TEXT NOT NULL,
            trajectory_feature_state_json TEXT NOT NULL,
            source_reliability_state_json TEXT NOT NULL,
            observed_max_lower_bound_c REAL,
            consumed_event_ids_json TEXT NOT NULL,
            consumed_ancestor_sha256 TEXT NOT NULL,
            model_artifact_sha256 TEXT NOT NULL,
            replay_generation INTEGER NOT NULL CHECK(replay_generation >= 0),
            supersedes_checkpoint_id TEXT,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            committed_at TEXT NOT NULL
        )
    """,
    "realization_states": """
        CREATE TABLE IF NOT EXISTS realization_states (
            realization_state_id TEXT PRIMARY KEY,
            posterior_run_id TEXT NOT NULL UNIQUE,
            fact_snapshot_id TEXT NOT NULL UNIQUE,
            resolution_contract_id TEXT NOT NULL,
            target_bucket TEXT NOT NULL,
            second_target_bucket TEXT,
            credible_bucket_set_json TEXT NOT NULL,
            current_report_bucket TEXT,
            current_report_bucket_probability REAL,
            probability_above_current_report_bucket REAL,
            current_report_bucket_hard_zero INTEGER NOT NULL CHECK(current_report_bucket_hard_zero IN (0, 1)),
            settlement_exclusion_certificates_json TEXT NOT NULL,
            state_json TEXT NOT NULL,
            state_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            committed_at TEXT NOT NULL
        )
    """,
    "posterior_commit_outbox": """
        CREATE TABLE IF NOT EXISTS posterior_commit_outbox (
            outbox_event_id TEXT PRIMARY KEY,
            posterior_run_id TEXT NOT NULL UNIQUE,
            fact_snapshot_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending', 'claimed', 'completed', 'failed', 'dead_letter')),
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT,
            attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
            claim_generation INTEGER NOT NULL CHECK(claim_generation >= 0),
            lease_owner TEXT,
            lease_until TEXT,
            next_attempt_at TEXT,
            error_class TEXT,
            last_error TEXT,
            terminal_reason TEXT,
            completed_decision_id TEXT
        )
    """,
    "posterior_outbox_execution_contracts": """
        CREATE TABLE IF NOT EXISTS posterior_outbox_execution_contracts (
            outbox_event_id TEXT PRIMARY KEY,
            posterior_run_id TEXT NOT NULL UNIQUE,
            canonical_observation_event_id TEXT NOT NULL,
            first_actionable_seen_at TEXT NOT NULL,
            observation_to_order_hard_deadline_at TEXT NOT NULL,
            inference_decision_deadline_at TEXT NOT NULL,
            transaction_b_committed_at TEXT NOT NULL,
            execution_eligible_at_commit INTEGER NOT NULL CHECK(execution_eligible_at_commit IN (0, 1)),
            execution_na_reason TEXT,
            deadline_policy TEXT NOT NULL CHECK(deadline_policy='canonical_first_actionable_plus_3000ms'),
            created_at TEXT NOT NULL,
            CHECK(
                (execution_eligible_at_commit=1 AND COALESCE(execution_na_reason, '')='')
                OR
                (execution_eligible_at_commit=0 AND LENGTH(COALESCE(execution_na_reason, ''))>0)
            )
        )
    """,
    "dynamic_three_leg_transition_plans": """
        CREATE TABLE IF NOT EXISTS dynamic_three_leg_transition_plans (
            transition_plan_id TEXT PRIMARY KEY,
            trading_plan_id TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL UNIQUE,
            realization_state_id TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            position_snapshot_id TEXT NOT NULL,
            strategy_artifact_sha256 TEXT NOT NULL,
            market_snapshot_id TEXT NOT NULL,
            capital_snapshot_id TEXT NOT NULL,
            risk_snapshot_id TEXT NOT NULL,
            capital_epoch_id TEXT NOT NULL,
            risk_reservation_id TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('SELL_OLD', 'BUY_NEW', 'HOLD', 'BLOCKED')),
            transition_json TEXT NOT NULL,
            transition_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared', 'completed', 'superseded', 'blocked')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,
    "dynamic_three_leg_risk_reservations": """
        CREATE TABLE IF NOT EXISTS dynamic_three_leg_risk_reservations (
            risk_reservation_id TEXT PRIMARY KEY,
            trading_plan_id TEXT NOT NULL UNIQUE,
            posterior_run_id TEXT NOT NULL,
            capital_snapshot_id TEXT NOT NULL,
            risk_snapshot_id TEXT NOT NULL,
            capital_epoch_id TEXT NOT NULL,
            strategy_artifact_sha256 TEXT NOT NULL,
            available_cash_usdc REAL NOT NULL CHECK(available_cash_usdc >= 0),
            risk_limit_usdc REAL NOT NULL CHECK(risk_limit_usdc >= 0),
            reserved_buy_notional REAL NOT NULL CHECK(reserved_buy_notional >= 0),
            state TEXT NOT NULL CHECK(state IN ('active', 'bound', 'consumed', 'released', 'expired', 'superseded')),
            bound_transition_plan_id TEXT UNIQUE,
            expires_at TEXT NOT NULL,
            reservation_json TEXT NOT NULL,
            reservation_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,
    "posterior_execution_intents": """
        CREATE TABLE IF NOT EXISTS posterior_execution_intents (
            order_intent_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL UNIQUE,
            transition_plan_id TEXT NOT NULL,
            trading_plan_id TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL,
            realization_state_id TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            position_snapshot_id TEXT NOT NULL,
            position_lot_id TEXT,
            position_lot_version TEXT,
            strategy_artifact_sha256 TEXT NOT NULL,
            market_snapshot_id TEXT NOT NULL,
            capital_snapshot_id TEXT NOT NULL,
            risk_snapshot_id TEXT NOT NULL,
            capital_epoch_id TEXT NOT NULL,
            risk_reservation_id TEXT NOT NULL,
            candidate_id TEXT,
            bucket TEXT NOT NULL,
            outcome_side TEXT NOT NULL CHECK(outcome_side IN ('YES', 'NO')),
            order_action TEXT NOT NULL CHECK(order_action IN ('BUY', 'SELL')),
            token_id TEXT NOT NULL,
            limit_price REAL NOT NULL CHECK(limit_price > 0 AND limit_price < 1),
            size REAL NOT NULL CHECK(size > 0),
            notional REAL NOT NULL CHECK(notional > 0),
            reason_family TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'prepared', 'submitting', 'gateway_accepted', 'reconcile_required',
                'partially_filled', 'filled', 'cancelled_superseded', 'expired', 'failed'
            )),
            live_submit_authorized INTEGER NOT NULL CHECK(live_submit_authorized IN (0, 1)),
            observation_to_order_hard_deadline_at TEXT NOT NULL,
            quote_expires_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            intent_json TEXT NOT NULL,
            intent_sha256 TEXT NOT NULL,
            gateway_response_json TEXT NOT NULL DEFAULT '{}',
            exchange_post_started_at TEXT,
            expected_order_id TEXT,
            exchange_order_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,
}

INDEX_DDL: Mapping[str, str] = {
    "idx_canobs_target_valid_state": """
        CREATE INDEX IF NOT EXISTS idx_canobs_target_valid_state
        ON canonical_observation_events(target_identity, valid_time, state, revision_no)
    """,
    "idx_canobs_actionable": """
        CREATE INDEX IF NOT EXISTS idx_canobs_actionable
        ON canonical_observation_events(target_identity, first_actionable_seen_at, valid_time)
    """,
    "idx_canobs_sources_event": """
        CREATE INDEX IF NOT EXISTS idx_canobs_sources_event
        ON canonical_observation_event_sources(source_event_id, source_observation_id)
    """,
    "idx_strong_updates_previous": """
        CREATE INDEX IF NOT EXISTS idx_strong_updates_previous
        ON strong_observation_updates(target_identity, previous_checkpoint_id, decision_as_of)
    """,
    "idx_checkpoints_target_committed": """
        CREATE INDEX IF NOT EXISTS idx_checkpoints_target_committed
        ON posterior_checkpoints(target_identity, committed_at, replay_generation)
    """,
    "idx_outbox_claimable": """
        CREATE INDEX IF NOT EXISTS idx_outbox_claimable
        ON posterior_commit_outbox(state, next_attempt_at, created_at)
    """,
    "idx_outbox_execution_deadline": """
        CREATE INDEX IF NOT EXISTS idx_outbox_execution_deadline
        ON posterior_outbox_execution_contracts(execution_eligible_at_commit, observation_to_order_hard_deadline_at)
    """,
    "idx_transition_plans_phase_updated": """
        CREATE INDEX IF NOT EXISTS idx_transition_plans_phase_updated
        ON dynamic_three_leg_transition_plans(phase, state, updated_at)
    """,
    "idx_dynamic_three_leg_risk_reservations_active": """
        CREATE INDEX IF NOT EXISTS idx_dynamic_three_leg_risk_reservations_active
        ON dynamic_three_leg_risk_reservations(state, expires_at, capital_epoch_id)
    """,
    "idx_posterior_execution_intents_ready": """
        CREATE INDEX IF NOT EXISTS idx_posterior_execution_intents_ready
        ON posterior_execution_intents(state, live_submit_authorized, observation_to_order_hard_deadline_at)
    """,
    "idx_posterior_execution_intents_lot": """
        CREATE INDEX IF NOT EXISTS idx_posterior_execution_intents_lot
        ON posterior_execution_intents(position_lot_id, position_lot_version, order_action, state)
    """,
}


TRIGGER_DDL: Mapping[str, str] = {}


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0] or "")
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _existing_indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0] or "")
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }


def _existing_triggers(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0] or "")
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }


def _canonical_schema_sql(value: Any) -> str:
    """Compare SQLite's stored DDL with this contract, not merely its name."""

    text = " ".join(str(value or "").strip().lower().split())
    text = text.replace("create table if not exists ", "create table ", 1)
    text = text.replace("create index if not exists ", "create index ", 1)
    text = text.replace("create trigger if not exists ", "create trigger ", 1)
    return text.rstrip(";").strip()


def _schema_object_sql(conn: sqlite3.Connection, *, object_type: str, name: str) -> str:
    row = conn.execute(
        "SELECT COALESCE(sql, '') FROM sqlite_master WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    return str(row[0] if row else "")


def build_forward_schema_plan(db_path: Path | str) -> dict[str, Any]:
    """Return a read-only, additive DDL plan; never executes DDL."""

    db = Path(db_path).expanduser().resolve()
    with _connect_read_only(db) as conn:
        tables = _existing_tables(conn)
        indexes = _existing_indexes(conn)
        triggers = _existing_triggers(conn)
    actions: list[dict[str, str]] = []
    for table, sql in TABLE_DDL.items():
        if table not in tables:
            actions.append({"action": "create_table", "name": table, "sql": sql.strip()})
    for index, sql in INDEX_DDL.items():
        if index not in indexes:
            actions.append({"action": "create_index", "name": index, "sql": sql.strip()})
    for trigger, sql in TRIGGER_DDL.items():
        if trigger not in triggers:
            actions.append({"action": "create_trigger", "name": trigger, "sql": sql.strip()})
    return {
        "schema": SCHEMA_VERSION,
        "migration_id": MIGRATION_ID,
        "read_only": True,
        "db": str(db),
        "actions": actions,
        "table_count_before": len(tables),
        "index_count_before": len(indexes),
        "historical_backfill": False,
        "existing_row_updates": False,
        "requires_writer_quiescence": True,
        "requires_explicit_confirmation": True,
    }


def verify_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _existing_tables(conn)
    indexes = _existing_indexes(conn)
    triggers = _existing_triggers(conn)
    missing_tables = sorted(set(TABLE_DDL) - tables)
    missing_indexes = sorted(set(INDEX_DDL) - indexes)
    missing_triggers = sorted(set(TRIGGER_DDL) - triggers)
    incompatible_tables = sorted(
        table
        for table, expected_sql in TABLE_DDL.items()
        if table in tables
        and _canonical_schema_sql(_schema_object_sql(conn, object_type="table", name=table))
        != _canonical_schema_sql(expected_sql)
    )
    incompatible_indexes = sorted(
        index
        for index, expected_sql in INDEX_DDL.items()
        if index in indexes
        and _canonical_schema_sql(_schema_object_sql(conn, object_type="index", name=index))
        != _canonical_schema_sql(expected_sql)
    )
    incompatible_triggers = sorted(
        trigger
        for trigger, expected_sql in TRIGGER_DDL.items()
        if trigger in triggers
        and _canonical_schema_sql(_schema_object_sql(conn, object_type="trigger", name=trigger))
        != _canonical_schema_sql(expected_sql)
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": not missing_tables and not missing_indexes and not missing_triggers and not incompatible_tables and not incompatible_indexes and not incompatible_triggers,
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "missing_triggers": missing_triggers,
        "incompatible_tables": incompatible_tables,
        "incompatible_indexes": incompatible_indexes,
        "incompatible_triggers": incompatible_triggers,
        "tables_present": sorted(set(TABLE_DDL) & tables),
        "indexes_present": sorted(set(INDEX_DDL) & indexes),
        "triggers_present": sorted(set(TRIGGER_DDL) & triggers),
    }


def _expected_table_columns(table: str, ddl: str) -> list[tuple[Any, ...]]:
    """Materialize the contract DDL in isolation for PRAGMA-level comparison."""

    with sqlite3.connect(":memory:") as expected:
        expected.execute(ddl)
        return expected.execute(f'PRAGMA table_info("{table}")').fetchall()


def verify_additive_schema_compatibility(conn: sqlite3.Connection) -> dict[str, Any]:
    """Accept forward-added columns while preserving the P1 base contract.

    ``verify_schema`` remains the exact migration-authority check.  Runtime
    ingresses use this narrower compatibility check because later production
    generations legitimately add columns to P1 tables.  A required base
    column may not disappear or change its declared type, NOT NULL, default,
    or primary-key position.
    """

    tables = _existing_tables(conn)
    indexes = _existing_indexes(conn)
    triggers = _existing_triggers(conn)
    missing_tables = sorted(set(TABLE_DDL) - tables)
    missing_indexes = sorted(set(INDEX_DDL) - indexes)
    missing_triggers = sorted(set(TRIGGER_DDL) - triggers)
    incompatible_columns: dict[str, list[str]] = {}
    accepted_extension_columns: dict[str, list[str]] = {}
    for table, ddl in TABLE_DDL.items():
        if table not in tables:
            continue
        expected_rows = _expected_table_columns(table, ddl)
        actual_rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        expected_by_name = {str(row[1]): row for row in expected_rows}
        actual_by_name = {str(row[1]): row for row in actual_rows}
        problems: list[str] = []
        for name, expected in expected_by_name.items():
            actual = actual_by_name.get(name)
            if actual is None:
                problems.append(f"{name}:missing")
                continue
            # cid is layout-dependent and deliberately excluded.
            expected_contract = tuple(expected[index] for index in (2, 3, 4, 5))
            actual_contract = tuple(actual[index] for index in (2, 3, 4, 5))
            if actual_contract != expected_contract:
                problems.append(f"{name}:contract_mismatch")
        if problems:
            incompatible_columns[table] = problems
        extras = sorted(set(actual_by_name) - set(expected_by_name))
        if extras:
            accepted_extension_columns[table] = extras
    incompatible_indexes = sorted(
        index
        for index, expected_sql in INDEX_DDL.items()
        if index in indexes
        and _canonical_schema_sql(_schema_object_sql(conn, object_type="index", name=index))
        != _canonical_schema_sql(expected_sql)
    )
    incompatible_triggers = sorted(
        trigger
        for trigger, expected_sql in TRIGGER_DDL.items()
        if trigger in triggers
        and _canonical_schema_sql(_schema_object_sql(conn, object_type="trigger", name=trigger))
        != _canonical_schema_sql(expected_sql)
    )
    return {
        "schema": SCHEMA_VERSION,
        "verification_mode": "additive_runtime_compatibility",
        "ok": not missing_tables and not missing_indexes and not missing_triggers and not incompatible_columns and not incompatible_indexes and not incompatible_triggers,
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "missing_triggers": missing_triggers,
        "incompatible_table_columns": incompatible_columns,
        "incompatible_indexes": incompatible_indexes,
        "incompatible_triggers": incompatible_triggers,
        "accepted_extension_columns": accepted_extension_columns,
    }


def apply_forward_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Apply only the additive P1 DDL inside the caller's transaction."""

    applied: list[dict[str, str]] = []
    tables = _existing_tables(conn)
    indexes = _existing_indexes(conn)
    triggers = _existing_triggers(conn)
    for table, sql in TABLE_DDL.items():
        if table not in tables:
            conn.execute(sql)
            applied.append({"action": "create_table", "name": table})
    for index, sql in INDEX_DDL.items():
        if index not in indexes:
            conn.execute(sql)
            applied.append({"action": "create_index", "name": index})
    for trigger, sql in TRIGGER_DDL.items():
        if trigger not in triggers:
            conn.execute(sql)
            applied.append({"action": "create_trigger", "name": trigger})
    return {"schema": SCHEMA_VERSION, "migration_id": MIGRATION_ID, "applied": applied}
