"""Additive storage contract for live weak-forecast replacement updates."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping


SCHEMA_VERSION = "weak_forecast_revision_schema_v1"
MIGRATION_ID = "weak_forecast_revision_forward_schema_v1"


TABLE_DDL: Mapping[str, str] = {
    "weak_forecast_evidence_snapshots": """
        CREATE TABLE IF NOT EXISTS weak_forecast_evidence_snapshots (
            forecast_evidence_snapshot_id TEXT PRIMARY KEY,
            target_identity TEXT NOT NULL,
            event_date TEXT NOT NULL,
            decision_as_of TEXT NOT NULL,
            weak_likelihood_artifact_sha256 TEXT NOT NULL,
            weak_likelihood_sha256 TEXT NOT NULL,
            source_set_key TEXT NOT NULL,
            source_revision_ids_json TEXT NOT NULL,
            source_revisions_json TEXT NOT NULL,
            temperature_grid_json TEXT NOT NULL,
            likelihood_by_x_json TEXT NOT NULL,
            forecast_peak_timing_prior_json TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('active', 'superseded')),
            created_at TEXT NOT NULL
        )
    """,
    "weak_forecast_revision_updates": """
        CREATE TABLE IF NOT EXISTS weak_forecast_revision_updates (
            weak_forecast_revision_update_id TEXT PRIMARY KEY,
            target_identity TEXT NOT NULL,
            previous_posterior_run_id TEXT NOT NULL,
            previous_posterior_sha256 TEXT NOT NULL,
            previous_checkpoint_id TEXT NOT NULL,
            successor_checkpoint_id TEXT NOT NULL UNIQUE,
            previous_forecast_evidence_snapshot_id TEXT NOT NULL,
            revised_forecast_evidence_snapshot_id TEXT NOT NULL,
            trigger_source_id TEXT NOT NULL,
            trigger_source_revision_id TEXT NOT NULL,
            revision_kind TEXT NOT NULL CHECK(revision_kind IN ('original', 'amendment', 'correction')),
            weak_likelihood_artifact_sha256 TEXT NOT NULL,
            decision_as_of TEXT NOT NULL,
            replacement_log_lr_by_x_json TEXT NOT NULL,
            replacement_log_lr_sha256 TEXT NOT NULL,
            previous_weak_likelihood_sha256 TEXT NOT NULL,
            revised_weak_likelihood_sha256 TEXT NOT NULL,
            posterior_delta_json TEXT NOT NULL,
            posterior_delta_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('applied', 'neutral', 'blocked')),
            quality_json TEXT NOT NULL,
            block_reasons_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(previous_checkpoint_id, revised_forecast_evidence_snapshot_id)
        )
    """,
}

INDEX_DDL: Mapping[str, str] = {
    "idx_weak_forecast_snapshots_target_asof": """
        CREATE INDEX IF NOT EXISTS idx_weak_forecast_snapshots_target_asof
        ON weak_forecast_evidence_snapshots(target_identity, decision_as_of)
    """,
    "idx_weak_forecast_updates_target_asof": """
        CREATE INDEX IF NOT EXISTS idx_weak_forecast_updates_target_asof
        ON weak_forecast_revision_updates(target_identity, decision_as_of)
    """,
}


def _canonical(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    text = text.replace("create table if not exists ", "create table ", 1)
    text = text.replace("create index if not exists ", "create index ", 1)
    return text.rstrip(";").strip()


def ensure_weak_forecast_revision_schema(conn: sqlite3.Connection) -> None:
    """Create the additive contract; caller owns the surrounding transaction."""

    for ddl in TABLE_DDL.values():
        conn.execute(ddl)
    for ddl in INDEX_DDL.values():
        conn.execute(ddl)


def audit_weak_forecast_revision_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    issues: list[str] = []
    for name, ddl in {**TABLE_DDL, **INDEX_DDL}.items():
        row = conn.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?",
            (name,),
        ).fetchone()
        if row is None:
            issues.append(f"schema_object_missing:{name}")
        elif _canonical(row[1]) != _canonical(ddl):
            issues.append(f"schema_object_drift:{name}")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not issues,
        "issues": issues,
        "read_only": True,
    }


__all__ = [
    "INDEX_DDL",
    "MIGRATION_ID",
    "SCHEMA_VERSION",
    "TABLE_DDL",
    "audit_weak_forecast_revision_schema",
    "ensure_weak_forecast_revision_schema",
]
