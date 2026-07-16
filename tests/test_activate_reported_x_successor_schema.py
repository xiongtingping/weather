from __future__ import annotations

from pathlib import Path
import sqlite3

from scripts.activate_reported_x_successor_schema import (
    CONFIRMATION,
    apply_plan,
    build_plan,
)


def _archive(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE posterior_checkpoints(posterior_checkpoint_id TEXT PRIMARY KEY)"
        )


def _backup(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)


def test_default_plan_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _archive(db)
    result = build_plan(db)
    assert result["ok"] is True
    assert result["state"] == "ready_to_apply"
    assert result["read_only"] is True
    assert result["boundary"]["updates_existing_rows"] is False
    with sqlite3.connect(db) as conn:
        assert len(conn.execute("PRAGMA table_info(posterior_checkpoints)").fetchall()) == 1


def test_apply_requires_confirmation_quiescence_and_fresh_backup(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    backup = tmp_path / "backup.db"
    _archive(db)
    _backup(db, backup)
    result = apply_plan(
        db_path=db,
        backup_path=backup,
        confirm="wrong",
        writer_quiescent=False,
    )
    assert result["ok"] is False
    assert "confirmation_phrase_required" in result["blockers"]
    assert "writer_quiescent_assertion_required" in result["blockers"]


def test_apply_adds_columns_without_updating_rows(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    backup = tmp_path / "backup.db"
    _archive(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO posterior_checkpoints VALUES ('old')")
    _backup(db, backup)
    result = apply_plan(
        db_path=db,
        backup_path=backup,
        confirm=CONFIRMATION,
        writer_quiescent=True,
    )
    assert result["ok"] is True
    assert result["state"] == "applied"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT posterior_checkpoint_id,checkpoint_probability_schema "
            "FROM posterior_checkpoints"
        ).fetchone()
    assert row == ("old", None)
