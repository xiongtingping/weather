from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.activate_production_initial_posterior_lineage_schema import (
    CONFIRMATION_PHRASE,
    apply_contract,
    build_contract,
)


def _db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE retained_history(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO retained_history(value) VALUES('keep')")


def test_p0_lineage_schema_apply_is_guarded_additive_and_idempotent(tmp_path: Path) -> None:
    db, backup = tmp_path / "archive.db", tmp_path / "archive.backup.db"
    _db(db); _db(backup)
    blocked = apply_contract(db, backup_path=backup, writer_quiescent=False, confirm="")
    assert set(blocked["blockers"]) == {"writer_quiescent_assertion_required", "confirmation_phrase_required"}

    applied = apply_contract(
        db, backup_path=backup, writer_quiescent=True, confirm=CONFIRMATION_PHRASE,
    )
    assert applied["execution_mode"] == "applied"
    assert applied["after"]["ok"] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM retained_history").fetchone()[0] == "keep"
        assert conn.execute("SELECT COUNT(*) FROM production_initial_posterior_lineage").fetchone()[0] == 0

    retry = apply_contract(
        db, backup_path=backup, writer_quiescent=True, confirm=CONFIRMATION_PHRASE,
    )
    assert retry["execution_mode"] == "already_applied_no_write"


def test_p0_lineage_schema_plan_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"; _db(db)
    plan = build_contract(db)
    assert plan["state"] == "apply_ready"
    assert plan["read_only"] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='production_initial_posterior_lineage'").fetchone()[0] == 0
