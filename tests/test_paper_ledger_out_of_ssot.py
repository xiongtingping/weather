"""The production paper book lives beside the SSOT, not in it (2026-09-19, 「移出」).

The ledger's four tables sat in the SSOT, so its pass -- almost all reading --
held the SSOT writer for its whole lease: 62 production cycles waited on it,
4.25 hours of the 28.9 before the move.  These pin the split: the ledger
opens the SSOT read-only and writes only its own file, it refuses to run
while the SSOT still holds the old tables, replays keep their own book,
readers attach only to production's SSOT, and the migration moves every row
exactly before it drops anything.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import paper_ledger_store as store  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ledger(monkeypatch, tmp_path):
    module = _load("_ledger_split_under_test", ROOT / "scripts" / "paper_trading_ledger.py")
    ssot = tmp_path / "ssot.db"
    conn = sqlite3.connect(ssot)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE production_fact(x)")
    conn.execute("INSERT INTO production_fact VALUES (1)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, "DEFAULT_DB", ssot)
    monkeypatch.setattr(module, "PAPER_LEDGER_DB", tmp_path / "paper_ledger.db")
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_the_production_ledger_writes_its_own_file_and_never_the_ssot(ledger) -> None:
    before = _sha(ledger.DEFAULT_DB)
    conn = ledger._connect(ledger.DEFAULT_DB, writable=True)
    ledger.ensure_schema(conn)
    conn.execute(
        "INSERT INTO paper_trade_refusal(station_id,event_date,stage,reason,detail,"
        " cycles_examined,last_decision_at_utc,recorded_at_utc)"
        " VALUES('ZUUU','2026-09-19','P0',?,'d',1,'t','t')",
        (ledger.REFUSAL_REASONS[0],),
    )
    conn.commit()
    # The production table still reads under its own name, beside the book.
    assert conn.execute("SELECT x FROM production_fact").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM paper_trade_refusal").fetchone() == (1,)
    conn.close()
    assert _sha(ledger.DEFAULT_DB) == before
    assert not (_tables(ledger.DEFAULT_DB) & set(store.PAPER_LEDGER_TABLES))
    assert set(store.PAPER_LEDGER_TABLES) <= _tables(ledger.PAPER_LEDGER_DB)
    # The indexes went with their table, not into the read-only SSOT.
    check = sqlite3.connect(ledger.PAPER_LEDGER_DB)
    indexes = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_candidate_window_scope", "idx_candidate_window_axis"} <= indexes
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    check.close()


def test_it_refuses_while_the_ssot_still_holds_the_old_tables(ledger, monkeypatch, capsys) -> None:
    conn = sqlite3.connect(ledger.DEFAULT_DB)
    conn.execute("CREATE TABLE paper_trade_position(position_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    with pytest.raises(store.PaperLedgerNotMigrated):
        ledger._connect(ledger.DEFAULT_DB, writable=True)
    monkeypatch.setattr(sys, "argv", ["paper_trading_ledger.py", "--backfill", "--db", str(ledger.DEFAULT_DB)])
    assert ledger.main() == 2
    assert "not moved out of the SSOT" in capsys.readouterr().out
    assert not ledger.PAPER_LEDGER_DB.exists()


def test_a_replay_database_keeps_its_own_book(ledger, tmp_path) -> None:
    replay = tmp_path / "replay.db"
    sqlite3.connect(replay).close()
    conn = ledger._connect(replay, writable=True)
    ledger.ensure_schema(conn)
    conn.close()
    assert set(store.PAPER_LEDGER_TABLES) <= _tables(replay)
    assert not ledger.PAPER_LEDGER_DB.exists()


def test_readers_attach_only_to_production_and_only_after_the_move(ledger, tmp_path) -> None:
    conn = ledger._connect(ledger.DEFAULT_DB, writable=True)
    ledger.ensure_schema(conn)
    conn.close()
    kwargs = {"ssot_path": ledger.DEFAULT_DB, "ledger_path": ledger.PAPER_LEDGER_DB}

    reader = sqlite3.connect(f"file:{ledger.DEFAULT_DB}?mode=ro", uri=True)
    assert store.attach_paper_ledger(reader, **kwargs) is True
    assert store.attach_paper_ledger(reader, **kwargs) is True  # idempotent
    assert reader.execute("SELECT COUNT(*) FROM paper_trade_position").fetchone() == (0,)
    # The schema-wide existence checks the evidence readers use see it.
    assert reader.execute(
        "SELECT 1 FROM pragma_table_list WHERE name='paper_trade_candidate_window'"
    ).fetchone() is not None
    reader.close()

    # A replay is never handed production's book.
    replay = tmp_path / "replay.db"
    sqlite3.connect(replay).close()
    other = sqlite3.connect(f"file:{replay}?mode=ro", uri=True)
    assert store.attach_paper_ledger(other, **kwargs) is False
    other.close()

    # No file yet: nothing to attach, and nothing is created.
    missing = tmp_path / "absent.db"
    reader = sqlite3.connect(f"file:{ledger.DEFAULT_DB}?mode=ro", uri=True)
    assert store.attach_paper_ledger(reader, ssot_path=ledger.DEFAULT_DB, ledger_path=missing) is False
    assert not missing.exists()
    reader.close()


def _seed_book_in_ssot(ledger) -> dict[str, int]:
    """The pre-move state: the book inside the SSOT, written the old way."""

    conn = sqlite3.connect(ledger.DEFAULT_DB)
    for ddl in (ledger._POSITION_DDL, ledger._SETTLEMENT_DDL, ledger._CANDIDATE_WINDOW_DDL, ledger._REFUSAL_DDL):
        conn.execute(ddl)
    conn.execute("CREATE INDEX idx_candidate_window_scope ON paper_trade_candidate_window(stage, event_date)")
    reason = ledger.REFUSAL_REASONS[0]
    for i in range(7):
        conn.execute(
            "INSERT INTO paper_trade_refusal(station_id,event_date,stage,reason,detail,"
            " cycles_examined,last_decision_at_utc,recorded_at_utc)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (f"S{i:03d}", "2026-09-18", "P0", reason, f"detail {i}", i, "t", "t"),
        )
    conn.execute("DELETE FROM paper_trade_refusal WHERE station_id='S002'")  # a rowid gap
    conn.commit()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in store.PAPER_LEDGER_TABLES}
    conn.close()
    return counts


def test_the_migration_moves_every_row_exactly_then_drops(ledger, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    migration = _load("_paper_migration_under_test", ROOT / "scripts" / "migrate_paper_ledger_out_of_ssot.py")
    counts = _seed_book_in_ssot(ledger)
    source = sqlite3.connect(ledger.DEFAULT_DB)
    refusals = source.execute("SELECT rowid,* FROM paper_trade_refusal ORDER BY rowid").fetchall()
    source.close()

    dry = migration.migrate(ssot_path=ledger.DEFAULT_DB, ledger_path=ledger.PAPER_LEDGER_DB, apply=False)
    assert dry["state"] == "DRY_RUN" and dry["rows"] == counts
    assert not ledger.PAPER_LEDGER_DB.exists()

    done = migration.migrate(ssot_path=ledger.DEFAULT_DB, ledger_path=ledger.PAPER_LEDGER_DB, apply=True)
    assert done["state"] == "MOVED"
    assert all(copy["matches"] for copy in done["copies"].values())
    assert not (_tables(ledger.DEFAULT_DB) & set(store.PAPER_LEDGER_TABLES))
    assert "production_fact" in _tables(ledger.DEFAULT_DB)
    moved = sqlite3.connect(ledger.PAPER_LEDGER_DB)
    assert moved.execute("SELECT rowid,* FROM paper_trade_refusal ORDER BY rowid").fetchall() == refusals
    assert moved.execute(
        "SELECT 1 FROM sqlite_master WHERE name='idx_candidate_window_scope'"
    ).fetchone() is not None
    moved.close()
    assert not migration._lock_path().exists()  # released

    # The ledger now opens on the moved book and carries on.
    conn = ledger._connect(ledger.DEFAULT_DB, writable=True)
    ledger.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM paper_trade_refusal").fetchone() == (6,)
    conn.close()

    again = migration.migrate(ssot_path=ledger.DEFAULT_DB, ledger_path=ledger.PAPER_LEDGER_DB, apply=True)
    assert again["state"] == "ALREADY_MOVED"

    # Two books is a decision, not a guess.
    conn = sqlite3.connect(ledger.DEFAULT_DB)
    conn.execute(ledger._REFUSAL_DDL)
    conn.commit()
    conn.close()
    refused = migration.migrate(ssot_path=ledger.DEFAULT_DB, ledger_path=ledger.PAPER_LEDGER_DB, apply=True)
    assert refused["state"] == "REFUSED_TWO_BOOKS"


def test_the_migration_waits_out_a_running_ledger_pass(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    migration = _load("_paper_migration_lock_under_test", ROOT / "scripts" / "migrate_paper_ledger_out_of_ssot.py")
    lock = migration._lock_path()
    lock.parent.mkdir(parents=True)
    lock.write_text(f"{os.getpid()}\n")  # a live holder
    with pytest.raises(TimeoutError):
        migration.acquire_ledger_lock(wait_sec=0.0)
    lock.write_text("99999999\n")  # a holder that is gone
    assert migration.acquire_ledger_lock(wait_sec=0.0) == lock
    assert lock.read_text().strip() == str(os.getpid())
    lock.unlink()


def test_the_resident_no_longer_waits_on_the_ledger() -> None:
    daemon = _load("_daemon_paper_domain", ROOT / "scripts" / "weather_basket_yes_daemon.py")
    assert daemon.hosted_sweep_write_domain("paper_trading_ledger") == "paper_ledger"
    assert "paper_ledger" not in daemon._EXCLUSIVE_WRITE_DOMAINS
    assert daemon.writer_excluding_sweeps(["paper_trading_ledger"]) == []
    assert "paper_trading_ledger" not in daemon.HOSTED_SWEEP_LEASE_CONTRACT
    # It starts in the same slot as an SSOT sweep, and neither holds the other.
    due = [
        (5.0, 0, "settlement_truth", "a.sh", 1800.0),
        (4.0, 1, "paper_trading_ledger", "b.sh", 1800.0),
    ]
    admitted = daemon.admissible_hosted_sweeps(due, running_names=[])
    assert [item[2] for item in admitted] == ["settlement_truth", "paper_trading_ledger"]
    assert daemon.admissible_hosted_sweeps(
        [(9.0, 0, "paper_trading_ledger", "b.sh", 1800.0)], running_names=["settlement_truth"],
    ) != []
