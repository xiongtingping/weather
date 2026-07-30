from __future__ import annotations

import sqlite3

from scripts import quarantine_bayes_basket_target_future_operational_residue as cleanup


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE basket_target_execution_cycles (
            cycle_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            station_id TEXT NOT NULL,
            event_date TEXT NOT NULL,
            strategy_lane TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            requested_reserve_usdc REAL NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE auto_trade_decisions (
            decision_id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            station_id TEXT NOT NULL,
            event_date TEXT NOT NULL,
            strategy_lane TEXT NOT NULL,
            decided_state TEXT NOT NULL,
            will_order INTEGER NOT NULL,
            reason_codes_json TEXT NOT NULL,
            details_json TEXT NOT NULL,
            decision_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    for cycle_id, decision_id, event_date, created_at in (
        ("future-cycle", "future-decision", "2026-07-25", "2026-07-24T14:00:00Z"),
        ("started-cycle", "started-decision", "2026-07-24", "2026-07-24T14:00:00Z"),
    ):
        conn.execute(
            "INSERT INTO basket_target_execution_cycles VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, "plan-" + cycle_id, "ZBAA", event_date, "basket", "a" * 64, 0.0, "prepared", created_at, created_at),
        )
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, cycle_id, "plan-" + cycle_id, "ZBAA", event_date, "basket", "blocked", 0, "[]", "{}", "b" * 64, created_at),
        )
    conn.commit()
    conn.close()


def test_cleanup_purges_only_pre_et_start_zero_order_rows(tmp_path, monkeypatch) -> None:
    db = tmp_path / "target.db"
    output = tmp_path / "cleanup.json"
    _db(db)
    monkeypatch.setattr(cleanup, "DEFAULT_DB", db)
    monkeypatch.setattr(cleanup, "PROTECTED_DB", tmp_path / "archive.db")

    result = cleanup.run(
        db=db,
        output=output,
        as_of_utc="2026-07-24T23:00:00Z",
        purge=True,
    )

    assert result["candidate_count"] == 1
    assert result["deleted_counts"] == {
        "basket_target_execution_cycles": 1,
        "auto_trade_decisions": 1,
    }
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM basket_target_execution_cycles").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1


def test_cleanup_fails_closed_when_future_row_has_downstream_intent(tmp_path, monkeypatch) -> None:
    db = tmp_path / "target.db"
    output = tmp_path / "cleanup.json"
    _db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE basket_target_order_intents (order_intent_id TEXT PRIMARY KEY, cycle_id TEXT)")
        conn.execute("INSERT INTO basket_target_order_intents VALUES ('intent-1','future-cycle')")
        conn.commit()
    monkeypatch.setattr(cleanup, "DEFAULT_DB", db)
    monkeypatch.setattr(cleanup, "PROTECTED_DB", tmp_path / "archive.db")

    result = cleanup.run(
        db=db,
        output=output,
        as_of_utc="2026-07-24T23:00:00Z",
        purge=False,
    )

    assert result["candidate_count"] == 0
    assert any("future_residue_has_downstream:future-cycle" in item for item in result["blockers"])


def test_cleanup_preserves_append_only_ssot_and_records_blocker(tmp_path, monkeypatch) -> None:
    db = tmp_path / "target.db"
    output = tmp_path / "cleanup.json"
    _db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER auto_trade_decisions_no_delete BEFORE DELETE ON auto_trade_decisions "
            "BEGIN SELECT RAISE(ABORT,'append_only'); END"
        )
        conn.commit()
    monkeypatch.setattr(cleanup, "DEFAULT_DB", db)
    monkeypatch.setattr(cleanup, "PROTECTED_DB", tmp_path / "archive.db")

    result = cleanup.run(
        db=db,
        output=output,
        as_of_utc="2026-07-24T23:00:00Z",
        purge=True,
    )

    assert result["purged"] is False
    assert result["deleted_counts"] == {
        "basket_target_execution_cycles": 0,
        "auto_trade_decisions": 0,
    }
    assert any(item.startswith("target_ssot_append_only:") for item in result["blockers"])
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM basket_target_execution_cycles").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 2
