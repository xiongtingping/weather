from __future__ import annotations

import sqlite3

from p10_shadow_runtime_schema import (
    REQUIRED_P10_TABLES,
    build_p10_shadow_runtime_schema_plan,
    initialize_p10_shadow_runtime_schema,
)
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema


def test_p10_runtime_schema_extends_only_an_isolated_shadow_db_without_runtime_rows() -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)

    before = build_p10_shadow_runtime_schema_plan(conn)
    applied = initialize_p10_shadow_runtime_schema(conn)
    after = build_p10_shadow_runtime_schema_plan(conn)

    assert before["state"] == "changes_pending"
    assert applied["ok"] is True
    assert after["state"] == "ready"
    assert after["missing_tables"] == []
    assert all(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in REQUIRED_P10_TABLES
    )
    assert applied["boundary"]["creates_posterior_or_order"] is False


def test_p10_runtime_schema_rejects_a_database_without_isolation_manifest() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        build_p10_shadow_runtime_schema_plan(conn)
    except ValueError as exc:
        assert str(exc) == "p10_shadow_runtime_schema_manifest_missing"
    else:  # pragma: no cover
        raise AssertionError("missing manifest must fail closed")


def test_p10_runtime_schema_detects_and_migrates_legacy_strong_activation_columns() -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    conn.execute(
        """
        CREATE TABLE strong_lr_runtime_artifact_activations_shadow(
            activation_id TEXT PRIMARY KEY,
            target_identity TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            activated_by TEXT NOT NULL,
            activation_basis TEXT NOT NULL,
            activation_ref TEXT NOT NULL,
            previous_activation_id TEXT NOT NULL,
            activation_sha256 TEXT NOT NULL UNIQUE
        )
        """
    )

    before = build_p10_shadow_runtime_schema_plan(conn)
    assert before["missing_columns"]["strong_lr_runtime_artifact_activations_shadow"] == [
        "activation_evidence_json",
        "activation_evidence_sha256",
    ]

    applied = initialize_p10_shadow_runtime_schema(conn)
    assert applied["state"] == "initialized"
    assert build_p10_shadow_runtime_schema_plan(conn)["state"] == "ready"
