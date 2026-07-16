from __future__ import annotations

from pathlib import Path
import sqlite3

from reported_x_successor_schema_contract import COLUMNS, build_forward_plan, verify_schema


def _db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE posterior_checkpoints(posterior_checkpoint_id TEXT PRIMARY KEY)"
        )


def test_forward_plan_is_additive_read_only_and_requires_successor_cutover(
    tmp_path: Path,
) -> None:
    db = tmp_path / "archive.db"
    _db(db)

    result = build_forward_plan(db)

    assert result["ok"] is True
    assert len(result["actions"]) == len(COLUMNS)
    assert all(row["action"] == "add_column" for row in result["actions"])
    assert result["read_only"] is True
    assert result["writes_archive_db"] is False
    assert result["places_orders"] is False
    assert result["requires_writer_quiescence"] is True
    assert result["requires_successor_activation"] is True
    with sqlite3.connect(db) as conn:
        assert verify_schema(conn)["ok"] is False


def test_verify_accepts_complete_additive_surface(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _db(db)
    with sqlite3.connect(db) as conn:
        for column, sql_type in COLUMNS.items():
            conn.execute(
                f"ALTER TABLE posterior_checkpoints ADD COLUMN {column} {sql_type}"
            )
        assert verify_schema(conn)["ok"] is True
    assert build_forward_plan(db)["state"] == "already_applied"
