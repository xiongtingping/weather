from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import migrate_reported_trajectory_hard_no_timezone as migration


def _old(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """CREATE TABLE auto_trade_decisions(
        decision_id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL UNIQUE,
        plan_id TEXT NOT NULL UNIQUE,station_id TEXT NOT NULL,event_date TEXT NOT NULL,
        strategy_lane TEXT NOT NULL,decided_state TEXT NOT NULL,will_order INTEGER NOT NULL,
        reason_codes_json TEXT NOT NULL,details_json TEXT NOT NULL,
        decision_sha256 TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE INDEX basket_target_decision_blocker_idx
        ON auto_trade_decisions(decided_state,will_order,created_at);
        CREATE TRIGGER auto_trade_decisions_no_update BEFORE UPDATE ON auto_trade_decisions
        BEGIN SELECT RAISE(ABORT,'append_only'); END;
        CREATE TRIGGER auto_trade_decisions_no_delete BEFORE DELETE ON auto_trade_decisions
        BEGIN SELECT RAISE(ABORT,'append_only'); END;"""
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("d", "c", "p", "RKPK", "2026-07-27", "hard_no", "blocked", 0, "[]", "{}", "a" * 64, "2026-07-27T00:00:00Z"),
    )


def test_plan_detects_pre_timezone_table() -> None:
    with sqlite3.connect(":memory:") as conn:
        _old(conn)
        plan = migration.inspect(conn)
    assert plan["state"] == "migration_required"
    assert plan["old_row_count"] == 1
    assert plan["unsafe_row_count"] == 0


def test_new_schema_requires_settlement_timezone() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(migration.NEW_DDL)
        columns = migration._columns(conn, "auto_trade_decisions")
        assert "settlement_timezone" in columns
        try:
            conn.execute(
                "INSERT INTO auto_trade_decisions("
                "decision_id,cycle_id,plan_id,station_id,event_date,strategy_lane,"
                "decided_state,will_order,reason_codes_json,details_json,decision_sha256,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("d", "c", "p", "RKPK", "2026-07-27", "hard_no", "blocked", 0, "[]", "{}", "a" * 64, "2026-07-27T00:00:00Z"),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("missing settlement timezone was accepted")


def test_unsafe_legacy_rows_are_detected() -> None:
    with sqlite3.connect(":memory:") as conn:
        _old(conn)
        conn.execute("DROP TRIGGER auto_trade_decisions_no_update")
        conn.execute(
            "UPDATE auto_trade_decisions SET decided_state='prepared',will_order=1"
        )
        plan = migration.inspect(conn)
    assert plan["unsafe_row_count"] == 1


def test_hard_no_backup_streams_bounded_rows_and_quick_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "archive.db"
    destination = tmp_path / "hard-no.backup.db"
    monkeypatch.setattr(migration, "DEFAULT_DB", source)
    with sqlite3.connect(source) as conn:
        _old(conn)
        result = migration._backup_table(conn, destination)

    assert result["row_count"] == 1
    assert result["payload_bytes"] > 0
    assert result["quick_check"] == "ok"
    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions_backup"
        ).fetchone()[0] == 1


def test_hard_no_backup_removes_partial_file_on_resource_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "archive.db"
    destination = tmp_path / "hard-no.partial.db"
    monkeypatch.setattr(migration, "DEFAULT_DB", source)
    monkeypatch.setattr(migration, "MAX_HARD_NO_BACKUP_ROWS", 0)
    with sqlite3.connect(source) as conn:
        _old(conn)
        with pytest.raises(
            RuntimeError,
            match="row_limit_exceeded",
        ):
            migration._backup_table(conn, destination)
    assert not destination.exists()
