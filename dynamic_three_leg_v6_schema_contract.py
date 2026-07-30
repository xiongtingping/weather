"""Additive SQLite schema owned by Target Realization v6.

The schema is installed before activation and is inert by default.  Runtime
activation remains a separate content-addressed authority.
"""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = "dynamic_three_leg_v6_schema_v1"

TABLE_DDL = {
    "target_realization_v6_plans": """
        CREATE TABLE target_realization_v6_plans (
            v6_trading_plan_id TEXT PRIMARY KEY,
            posterior_run_id TEXT NOT NULL,
            realization_state_id TEXT NOT NULL,
            canonical_observation_event_id TEXT NOT NULL,
            strategy_artifact_sha256 TEXT NOT NULL,
            main_yes_position_cycle_id TEXT NOT NULL,
            capital_allocation_id TEXT NOT NULL,
            main_yes_bucket TEXT NOT NULL,
            second_yes_bucket TEXT,
            no_mode TEXT NOT NULL CHECK(no_mode IN ('none','trajectory_no','hard_exclusion_no')),
            capital_scale_factor REAL NOT NULL CHECK(capital_scale_factor >= 0 AND capital_scale_factor <= 1),
            plan_json TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','committed','superseded','blocked')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,
    "dynamic_three_leg_v6_main_yes_cycles": """
        CREATE TABLE dynamic_three_leg_v6_main_yes_cycles (
            cycle_checkpoint_id TEXT PRIMARY KEY,
            main_yes_position_cycle_id TEXT NOT NULL,
            v6_trading_plan_id TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL,
            canonical_observation_event_id TEXT NOT NULL,
            bucket TEXT NOT NULL,
            main_yes_mode TEXT NOT NULL CHECK(main_yes_mode IN ('normal','upside_risk','hard_locked','invalidated')),
            cycle_generation INTEGER NOT NULL CHECK(cycle_generation >= 1),
            sell_stage_high_watermark INTEGER NOT NULL CHECK(sell_stage_high_watermark BETWEEN 0 AND 4),
            reentry_confirmation_count INTEGER NOT NULL CHECK(reentry_confirmation_count BETWEEN 0 AND 2),
            cycle_json TEXT NOT NULL,
            cycle_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('active','invalidated','closed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(v6_trading_plan_id) REFERENCES target_realization_v6_plans(v6_trading_plan_id)
        )
    """,
    "dynamic_three_leg_v6_capital_allocations": """
        CREATE TABLE dynamic_three_leg_v6_capital_allocations (
            capital_allocation_id TEXT PRIMARY KEY,
            v6_trading_plan_id TEXT NOT NULL,
            capital_snapshot_id TEXT NOT NULL,
            risk_snapshot_id TEXT NOT NULL,
            capital_epoch_id TEXT NOT NULL,
            capital_scale_factor REAL NOT NULL CHECK(capital_scale_factor >= 0 AND capital_scale_factor <= 1),
            requested_incremental_notional_usdc REAL NOT NULL CHECK(requested_incremental_notional_usdc >= 0),
            allocated_incremental_notional_usdc REAL NOT NULL CHECK(allocated_incremental_notional_usdc >= 0),
            allocation_json TEXT NOT NULL,
            allocation_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(v6_trading_plan_id) REFERENCES target_realization_v6_plans(v6_trading_plan_id)
        )
    """,
    "dynamic_three_leg_v6_risk_reservations": """
        CREATE TABLE dynamic_three_leg_v6_risk_reservations (
            risk_reservation_id TEXT PRIMARY KEY,
            v6_trading_plan_id TEXT NOT NULL UNIQUE,
            posterior_run_id TEXT NOT NULL,
            capital_allocation_id TEXT NOT NULL,
            capital_snapshot_id TEXT NOT NULL,
            risk_snapshot_id TEXT NOT NULL,
            capital_epoch_id TEXT NOT NULL,
            strategy_artifact_sha256 TEXT NOT NULL,
            reserved_buy_notional_usdc REAL NOT NULL CHECK(reserved_buy_notional_usdc >= 0),
            state TEXT NOT NULL CHECK(state IN ('active','bound','consumed','released','superseded')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(v6_trading_plan_id) REFERENCES target_realization_v6_plans(v6_trading_plan_id),
            FOREIGN KEY(capital_allocation_id) REFERENCES dynamic_three_leg_v6_capital_allocations(capital_allocation_id)
        )
    """,
    "dynamic_three_leg_v6_standing_exit_plans": """
        CREATE TABLE dynamic_three_leg_v6_standing_exit_plans (
            standing_exit_plan_id TEXT PRIMARY KEY,
            strategy_artifact_sha256 TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL,
            realization_state_id TEXT NOT NULL,
            strategy_leg TEXT NOT NULL CHECK(strategy_leg IN ('main_yes','second_yes','trajectory_no','hard_exclusion_no')),
            position_lot_id TEXT NOT NULL,
            position_lot_version TEXT NOT NULL,
            token_id TEXT NOT NULL,
            bucket TEXT NOT NULL,
            outcome_side TEXT NOT NULL CHECK(outcome_side IN ('YES','NO')),
            buy_intent_id TEXT NOT NULL,
            fill_identity TEXT NOT NULL UNIQUE,
            target_size REAL NOT NULL CHECK(target_size >= 0),
            covered_size REAL NOT NULL CHECK(covered_size >= 0),
            uncovered_size REAL NOT NULL CHECK(uncovered_size >= 0),
            stage_released_size REAL NOT NULL CHECK(stage_released_size >= 0),
            coverage_version INTEGER NOT NULL CHECK(coverage_version >= 1),
            standing_sell_intent_id TEXT,
            standing_sell_order_id TEXT,
            exit_reason_code TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('coverage_required','intent_prepared','covered','release_pending','released','closed','blocked')),
            plan_json TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(covered_size + uncovered_size + stage_released_size <= target_size + 0.000000001)
        )
    """,
    "dynamic_three_leg_v6_position_lots": """
        CREATE TABLE dynamic_three_leg_v6_position_lots (
            position_lot_id TEXT PRIMARY KEY,
            position_lot_version INTEGER NOT NULL CHECK(position_lot_version >= 1),
            management_scope TEXT NOT NULL CHECK(management_scope='target_realization_v6'),
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            bucket TEXT NOT NULL,
            outcome_side TEXT NOT NULL CHECK(outcome_side IN ('YES','NO')),
            token_id TEXT NOT NULL,
            entry_strategy_leg TEXT NOT NULL CHECK(entry_strategy_leg IN ('main_yes','second_yes','trajectory_no','hard_exclusion_no')),
            entry_strategy_artifact_sha256 TEXT NOT NULL,
            entry_order_intent_id TEXT NOT NULL UNIQUE,
            entry_posterior_run_id TEXT NOT NULL,
            main_yes_position_cycle_id TEXT,
            entry_price REAL NOT NULL CHECK(entry_price > 0 AND entry_price < 1),
            acquired_size REAL NOT NULL CHECK(acquired_size > 0),
            remaining_size REAL NOT NULL CHECK(remaining_size >= 0),
            remaining_cost_usdc REAL NOT NULL CHECK(remaining_cost_usdc >= 0),
            standing_exit_plan_id TEXT,
            state TEXT NOT NULL CHECK(state IN ('coverage_required','open','release_pending','closed','blocked')),
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            updated_at TEXT NOT NULL,
            CHECK(
                (state<>'closed' AND remaining_size > 0 AND closed_at IS NULL)
                OR (state='closed' AND remaining_size = 0 AND remaining_cost_usdc = 0 AND closed_at IS NOT NULL)
            )
        )
    """,
    "dynamic_three_leg_v6_position_lot_events": """
        CREATE TABLE dynamic_three_leg_v6_position_lot_events (
            position_lot_event_id TEXT PRIMARY KEY,
            position_lot_id TEXT NOT NULL,
            position_lot_version_before INTEGER NOT NULL,
            position_lot_version_after INTEGER NOT NULL,
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
            FOREIGN KEY(position_lot_id) REFERENCES dynamic_three_leg_v6_position_lots(position_lot_id)
        )
    """,
    "dynamic_three_leg_v6_standing_exit_coverage_events": """
        CREATE TABLE dynamic_three_leg_v6_standing_exit_coverage_events (
            coverage_event_id TEXT PRIMARY KEY,
            standing_exit_plan_id TEXT NOT NULL,
            coverage_version INTEGER NOT NULL CHECK(coverage_version >= 1),
            event_type TEXT NOT NULL CHECK(event_type IN ('fill_observed','sell_intent_prepared','sell_acknowledged','partial_fill','cancel_requested','cancel_reconciled','coverage_released','closed')),
            covered_size_before REAL NOT NULL CHECK(covered_size_before >= 0),
            covered_size_after REAL NOT NULL CHECK(covered_size_after >= 0),
            event_json TEXT NOT NULL,
            event_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(standing_exit_plan_id, coverage_version, event_type, event_sha256),
            FOREIGN KEY(standing_exit_plan_id) REFERENCES dynamic_three_leg_v6_standing_exit_plans(standing_exit_plan_id)
        )
    """,
    "dynamic_three_leg_v6_standing_exit_release_requests": """
        CREATE TABLE dynamic_three_leg_v6_standing_exit_release_requests (
            standing_exit_release_request_id TEXT PRIMARY KEY,
            standing_exit_plan_id TEXT NOT NULL,
            v6_trading_plan_id TEXT NOT NULL,
            main_yes_position_cycle_id TEXT NOT NULL,
            target_realization_reason_code TEXT NOT NULL,
            requested_release_size REAL NOT NULL CHECK(requested_release_size > 0),
            standing_sell_order_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN ('prepared','cancelling','reconcile_required','confirmed','failed')),
            gateway_response_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(standing_exit_plan_id) REFERENCES dynamic_three_leg_v6_standing_exit_plans(standing_exit_plan_id)
        )
    """,
    "dynamic_three_leg_v6_transaction_c_commits": """
        CREATE TABLE dynamic_three_leg_v6_transaction_c_commits (
            transaction_c_id TEXT PRIMARY KEY,
            outbox_event_id TEXT NOT NULL UNIQUE,
            v6_trading_plan_id TEXT NOT NULL UNIQUE,
            posterior_run_id TEXT NOT NULL,
            strategy_artifact_sha256 TEXT NOT NULL,
            exact_exit_reason_policy_sha256 TEXT NOT NULL,
            decision_count INTEGER NOT NULL CHECK(decision_count >= 0),
            intent_count INTEGER NOT NULL CHECK(intent_count >= 0),
            commit_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state='committed'),
            committed_at TEXT NOT NULL,
            FOREIGN KEY(v6_trading_plan_id) REFERENCES target_realization_v6_plans(v6_trading_plan_id)
        )
    """,
    "dynamic_three_leg_v6_target_sell_continuations": """
        CREATE TABLE dynamic_three_leg_v6_target_sell_continuations (
            target_sell_continuation_id TEXT PRIMARY KEY,
            standing_exit_release_request_id TEXT NOT NULL UNIQUE,
            v6_trading_plan_id TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL,
            order_intent_id TEXT NOT NULL UNIQUE,
            market_snapshot_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','completed','blocked')),
            continuation_json TEXT NOT NULL,
            continuation_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(standing_exit_release_request_id) REFERENCES dynamic_three_leg_v6_standing_exit_release_requests(standing_exit_release_request_id),
            FOREIGN KEY(v6_trading_plan_id) REFERENCES target_realization_v6_plans(v6_trading_plan_id)
        )
    """,
}

INDEX_DDL = {
    "idx_v6_plans_posterior": "CREATE INDEX idx_v6_plans_posterior ON target_realization_v6_plans(posterior_run_id,created_at)",
    "idx_v6_cycles_bucket_state": "CREATE INDEX idx_v6_cycles_bucket_state ON dynamic_three_leg_v6_main_yes_cycles(bucket,state,updated_at)",
    "idx_v6_cycles_identity": "CREATE UNIQUE INDEX idx_v6_cycles_identity ON dynamic_three_leg_v6_main_yes_cycles(main_yes_position_cycle_id,posterior_run_id)",
    "idx_v6_standing_coverage_state": "CREATE INDEX idx_v6_standing_coverage_state ON dynamic_three_leg_v6_standing_exit_plans(state,updated_at)",
    "idx_v6_standing_lot": "CREATE INDEX idx_v6_standing_lot ON dynamic_three_leg_v6_standing_exit_plans(position_lot_id,position_lot_version)",
    "idx_v6_lots_event_state": "CREATE INDEX idx_v6_lots_event_state ON dynamic_three_leg_v6_position_lots(slug,event_date,state)",
    "idx_v6_reservations_state": "CREATE INDEX idx_v6_reservations_state ON dynamic_three_leg_v6_risk_reservations(state,updated_at)",
    "idx_v6_release_requests_state": "CREATE INDEX idx_v6_release_requests_state ON dynamic_three_leg_v6_standing_exit_release_requests(state,updated_at)",
    "idx_v6_target_continuations_state": "CREATE INDEX idx_v6_target_continuations_state ON dynamic_three_leg_v6_target_sell_continuations(state,updated_at)",
}


def initialize_dynamic_three_leg_v6_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    for ddl in TABLE_DDL.values():
        conn.execute(ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
    for ddl in INDEX_DDL.values():
        conn.execute(ddl.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1))


__all__ = ["INDEX_DDL", "SCHEMA_VERSION", "TABLE_DDL", "initialize_dynamic_three_leg_v6_schema"]
