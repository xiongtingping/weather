from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import sequential_bayes_schema_apply as apply_script
from sequential_bayes_schema_contract import (
    INDEX_DDL,
    TABLE_DDL,
    TRIGGER_DDL,
    apply_forward_schema,
    build_forward_schema_plan,
    verify_additive_schema_compatibility,
    verify_schema,
)


def _empty_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE retained_history(id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO retained_history(note) VALUES('must_not_change')")


def test_forward_plan_is_read_only_and_additive(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _empty_db(db)

    plan = build_forward_schema_plan(db)

    assert plan["read_only"] is True
    assert plan["historical_backfill"] is False
    assert plan["existing_row_updates"] is False
    assert {item["name"] for item in plan["actions"]} == set(TABLE_DDL) | set(INDEX_DDL) | set(TRIGGER_DDL)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT note FROM retained_history").fetchone()[0] == "must_not_change"
        assert verify_schema(conn)["ok"] is False


def test_forward_apply_creates_exact_contract_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _empty_db(db)

    with sqlite3.connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        first = apply_forward_schema(conn)
        assert len(first["applied"]) == len(TABLE_DDL) + len(INDEX_DDL) + len(TRIGGER_DDL)
        assert verify_schema(conn)["ok"] is True
        conn.commit()

    with sqlite3.connect(db) as conn:
        second = apply_forward_schema(conn)
        assert second["applied"] == []
        assert conn.execute("SELECT note FROM retained_history").fetchone()[0] == "must_not_change"
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(canonical_observation_events)").fetchall()
        }
        assert {
            "canonical_observation_event_id",
            "canonical_observation_key",
            "first_actionable_seen_at",
            "normalized_physical_sha256",
        }.issubset(columns)
        execution_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(posterior_outbox_execution_contracts)").fetchall()
        }
        assert {
            "outbox_event_id",
            "posterior_run_id",
            "canonical_observation_event_id",
            "first_actionable_seen_at",
            "observation_to_order_hard_deadline_at",
            "transaction_b_committed_at",
            "execution_eligible_at_commit",
            "execution_na_reason",
        }.issubset(execution_columns)
        intent_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(posterior_execution_intents)").fetchall()
        }
        assert {
            "order_intent_id",
            "transition_plan_id",
            "position_snapshot_id",
            "position_lot_version",
            "strategy_artifact_sha256",
            "market_snapshot_id",
            "risk_reservation_id",
            "order_action",
            "live_submit_authorized",
            "observation_to_order_hard_deadline_at",
            "quote_expires_at",
            "idempotency_key",
        }.issubset(intent_columns)


def test_controlled_apply_requires_explicit_confirmation_and_quiescence(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _empty_db(db)

    blocked = apply_script.build_apply_contract(db, apply=True)
    assert set(blocked["apply_blockers"]) == {
        "confirmation_phrase_required",
        "writer_quiescent_assertion_required",
    }
    result = apply_script.apply_contract(
        db,
        confirm=apply_script.CONFIRMATION_PHRASE,
        writer_quiescent=True,
    )
    assert result["execution_mode"] == "applied"
    assert result["verification"]["ok"] is True


def test_schema_verification_rejects_preexisting_p1_object_with_wrong_ddl(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _empty_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE canonical_observation_events (id TEXT PRIMARY KEY)")

        verification = verify_schema(conn)

    assert verification["ok"] is False
    assert "canonical_observation_events" in verification["incompatible_tables"]


def test_runtime_compatibility_accepts_additive_columns_but_rejects_base_drift(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _empty_db(db)
    with sqlite3.connect(db) as conn:
        apply_forward_schema(conn)
        conn.execute("ALTER TABLE posterior_checkpoints ADD COLUMN future_generation_sha256 TEXT")
        compatible = verify_additive_schema_compatibility(conn)
        assert compatible["ok"] is True
        assert compatible["accepted_extension_columns"]["posterior_checkpoints"] == ["future_generation_sha256"]

    wrong = sqlite3.connect(":memory:")
    wrong.execute(TABLE_DDL["canonical_observation_events"].replace("revision_no INTEGER", "revision_no TEXT"))
    result = verify_additive_schema_compatibility(wrong)
    assert result["ok"] is False
    assert "revision_no:contract_mismatch" in result["incompatible_table_columns"]["canonical_observation_events"]
