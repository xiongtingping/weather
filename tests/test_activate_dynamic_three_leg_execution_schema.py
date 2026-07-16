from __future__ import annotations

import sqlite3

from scripts.activate_dynamic_three_leg_execution_schema import (
    CONFIRMATION,
    INDEX_NAMES,
    TABLE_NAMES,
    apply_cutover,
    build_cutover_plan,
)


def _base_db(path) -> None:
    with sqlite3.connect(path) as conn:
        for table in ("posterior_commit_outbox", "event_fact_history", "event_fact_latest", "auto_trade_decisions"):
            conn.execute(f"CREATE TABLE {table}(id TEXT)")


def _backup(source, destination) -> None:
    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as backup_conn:
        source_conn.backup(backup_conn)


def test_execution_schema_cutover_is_preview_first_atomic_and_idempotent(tmp_path) -> None:
    db = tmp_path / "archive.db"
    _base_db(db)

    preview = build_cutover_plan(db)
    assert preview["state"] == "ready_to_apply"
    assert {row["name"] for row in preview["actions"]} == set(TABLE_NAMES) | set(INDEX_NAMES)
    with sqlite3.connect(db) as conn:
        assert not any(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() for name in TABLE_NAMES)

    blocked = apply_cutover(db_path=db, backup_path=None, confirm="", writer_quiescent=False)
    assert blocked["state"] == "blocked"
    assert "confirmation_phrase_required" in blocked["blockers"]
    assert "writer_quiescent_assertion_required" in blocked["blockers"]
    assert "fresh_backup_required" in blocked["blockers"]

    backup = tmp_path / "archive.pre-execution-schema.bak"
    _backup(db, backup)
    applied = apply_cutover(db_path=db, backup_path=backup, confirm=CONFIRMATION, writer_quiescent=True)
    assert applied["state"] == "applied"
    assert applied["inspection_after"]["ok"] is True
    assert applied["backup"]["integrity_check"] == {
        "mode": "sqlite_backup_parity",
        "source": None,
        "backup": None,
        "deferred_full_quick_check": True,
    }
    refreshed_backup = tmp_path / "archive.post-execution-schema.bak"
    _backup(db, refreshed_backup)
    assert apply_cutover(db_path=db, backup_path=refreshed_backup, confirm=CONFIRMATION, writer_quiescent=True)["state"] == "already_applied"


def test_execution_schema_cutover_can_request_full_integrity_audit(tmp_path) -> None:
    db = tmp_path / "archive.db"
    _base_db(db)
    backup = tmp_path / "archive.pre-execution-schema.bak"
    _backup(db, backup)

    applied = apply_cutover(
        db_path=db,
        backup_path=backup,
        confirm=CONFIRMATION,
        writer_quiescent=True,
        full_integrity_check=True,
    )

    assert applied["state"] == "applied"
    assert applied["backup"]["integrity_check"] == {
        "mode": "full_quick_check",
        "source": "ok",
        "backup": "ok",
        "deferred_full_quick_check": False,
    }


def test_execution_schema_cutover_refuses_shadow_database(tmp_path) -> None:
    db = tmp_path / "shadow.db"
    _base_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE v2_shadow_runtime_manifest(id TEXT)")

    plan = build_cutover_plan(db)

    assert plan["state"] == "blocked"
    assert "production_cutover_refuses_shadow_runtime_manifest" in plan["blockers"]
