from __future__ import annotations

import sqlite3

from physical_state_bayes_v6.schema_contract import (
    TABLE_COLUMNS,
    build_forward_plan,
    verify_schema,
)


def _base_db(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE weak_forecast_evidence_snapshots(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE weak_forecast_revision_updates(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE strong_observation_updates(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE posterior_checkpoints(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE production_initial_posterior_lineage(id TEXT PRIMARY KEY)")


def test_forward_plan_is_read_only_and_additive(tmp_path) -> None:
    db = tmp_path / "archive.db"
    _base_db(db)

    plan = build_forward_plan(db)

    assert plan["ok"] is True
    assert plan["read_only"] is True
    assert len(plan["actions"]) == sum(len(value) for value in TABLE_COLUMNS.values())
    with sqlite3.connect(db) as conn:
        assert verify_schema(conn)["ok"] is False


def test_schema_verifies_after_forward_actions(tmp_path) -> None:
    db = tmp_path / "archive.db"
    _base_db(db)
    plan = build_forward_plan(db)
    with sqlite3.connect(db) as conn:
        for action in plan["actions"]:
            conn.execute(action["sql"])
        assert verify_schema(conn)["ok"] is True
