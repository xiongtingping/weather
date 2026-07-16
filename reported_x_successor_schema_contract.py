"""Read-only forward schema plan for the reported-X successor epoch.

The active production checkpoint schema remains untouched until a controlled
successor cutover.  These additive columns make the latent scheduled-slot
state Z and the public whole-degree AMSC target X impossible to confuse.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any


SCHEMA_VERSION = "reported_x_successor_schema_contract_v1"
MIGRATION_ID = "posterior_checkpoint_reported_x_surfaces_v1"
TABLE = "posterior_checkpoints"
COLUMNS: dict[str, str] = {
    "checkpoint_probability_schema": "TEXT",
    "probability_target_type": "TEXT",
    "latent_scheduled_slot_high_distribution_json": "TEXT",
    "reported_physical_x_distribution_json": "TEXT",
    "latent_observed_lower_bound_c": "REAL",
    "reported_x_lower_bound_c": "INTEGER",
    "reported_x_termination_json": "TEXT",
    "reported_x_projection_sha256": "TEXT",
}


def _columns(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[1]): str(row[2]).upper()
        for row in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
    }


def build_forward_plan(db_path: Path | str) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone() is not None
        existing = _columns(conn) if table_exists else {}
    blockers = [] if table_exists else ["posterior_checkpoints_table_missing"]
    actions = [
        {
            "action": "add_column",
            "table": TABLE,
            "column": column,
            "sql": f"ALTER TABLE {TABLE} ADD COLUMN {column} {sql_type}",
        }
        for column, sql_type in COLUMNS.items()
        if column not in existing
    ]
    incompatible = sorted(
        column
        for column, sql_type in COLUMNS.items()
        if column in existing and existing[column] != sql_type
    )
    if incompatible:
        blockers.append("reported_x_successor_column_type_mismatch")
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
        "requires_successor_activation": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


def verify_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    existing = _columns(conn)
    missing = sorted(set(COLUMNS) - set(existing))
    incompatible = sorted(
        column
        for column, sql_type in COLUMNS.items()
        if column in existing and existing[column] != sql_type
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": not missing and not incompatible,
        "missing_columns": missing,
        "incompatible_columns": incompatible,
        "read_only": True,
    }


__all__ = [
    "COLUMNS",
    "MIGRATION_ID",
    "SCHEMA_VERSION",
    "build_forward_plan",
    "verify_schema",
]
