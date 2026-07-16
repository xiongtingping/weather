"""Forward-only production schema plan for the v6 probability successor."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "physical_state_bayes_v6_schema_contract_v1"
MIGRATION_ID = "physical_state_bayes_v6_production_successor_v1"

TABLE_COLUMNS: Mapping[str, Mapping[str, str]] = {
    "weak_forecast_evidence_snapshots": {
        "forecast_scenario_paths_json": "TEXT",
        "forecast_scenario_paths_sha256": "TEXT",
        "candidate_scenario_responsibilities_json": "TEXT",
        "candidate_scenario_responsibilities_sha256": "TEXT",
        "forecast_background_context_json": "TEXT",
    },
    "weak_forecast_revision_updates": {
        "probability_update_schema": "TEXT",
        "previous_forcing_generation_sha256": "TEXT",
        "revised_forcing_generation_sha256": "TEXT",
        "scenario_control_lineage_sha256": "TEXT",
        "no_retroactive_state_transition": "INTEGER",
    },
    "strong_observation_updates": {
        "probability_update_schema": "TEXT",
        "pre_event_prediction_json": "TEXT",
        "pre_event_prediction_sha256": "TEXT",
        "observation_emission_json": "TEXT",
        "observation_emission_sha256": "TEXT",
        "duration_clocks_before_json": "TEXT",
        "duration_clocks_after_json": "TEXT",
        "forcing_generation_sha256": "TEXT",
        "transition_artifact_sha256": "TEXT",
        "observation_artifact_sha256": "TEXT",
        "scenario_control_lineage_sha256": "TEXT",
        "canonical_emission_input_sha256": "TEXT",
        "no_post_emission_transition": "INTEGER",
    },
    "posterior_checkpoints": {
        "state_space_schema": "TEXT",
        "continuous_y_joint_sha256": "TEXT",
        "duration_clocks_json": "TEXT",
        "last_event_valid_at": "TEXT",
        "forcing_generation_sha256": "TEXT",
        "transition_artifact_sha256": "TEXT",
        "observation_artifact_sha256": "TEXT",
        "pre_event_prediction_sha256": "TEXT",
        "scenario_control_lineage_sha256": "TEXT",
        "canonical_emission_input_sha256": "TEXT",
        "p0_pruned_probability_mass": "REAL",
        "p0_pruned_cell_count": "INTEGER",
    },
    "production_initial_posterior_lineage": {
        "probability_epoch_schema": "TEXT",
        "transition_artifact_sha256": "TEXT",
        "observation_artifact_sha256": "TEXT",
        "forcing_generation_sha256": "TEXT",
    },
}


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    return {
        str(row[1]): str(row[2]).upper()
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def build_forward_plan(db_path: Path | str) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    actions: list[dict[str, str]] = []
    blockers: list[str] = []
    incompatible: list[str] = []
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        for table, expected in TABLE_COLUMNS.items():
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                blockers.append(f"table_missing:{table}")
                continue
            actual = _columns(conn, table)
            for column, sql_type in expected.items():
                if column not in actual:
                    actions.append(
                        {
                            "action": "add_column",
                            "table": table,
                            "column": column,
                            "sql": f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}",
                        }
                    )
                elif actual[column] != sql_type:
                    incompatible.append(f"{table}.{column}")
    if incompatible:
        blockers.append("v6_column_type_mismatch")
    return {
        "schema": SCHEMA_VERSION,
        "migration_id": MIGRATION_ID,
        "ok": not blockers,
        "state": "already_applied" if not actions and not blockers else "planned",
        "db": str(db),
        "actions": actions,
        "incompatible_columns": incompatible,
        "block_reasons": blockers,
        "read_only": True,
        "requires_writer_quiescence": True,
        "requires_v6_activation": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


def verify_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    missing: list[str] = []
    incompatible: list[str] = []
    for table, expected in TABLE_COLUMNS.items():
        actual = _columns(conn, table)
        for column, sql_type in expected.items():
            if column not in actual:
                missing.append(f"{table}.{column}")
            elif actual[column] != sql_type:
                incompatible.append(f"{table}.{column}")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not missing and not incompatible,
        "missing_columns": sorted(missing),
        "incompatible_columns": sorted(incompatible),
        "read_only": True,
    }


__all__ = [
    "MIGRATION_ID",
    "SCHEMA_VERSION",
    "TABLE_COLUMNS",
    "build_forward_plan",
    "verify_schema",
]
