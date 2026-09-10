import argparse
import datetime as dt
from pathlib import Path
import sqlite3

from scripts.archive_db_governance import _run_chengdu_tick_retention, _run_retention


def _make_args(**overrides):
    base = {
        "apply": True,
        "keep_ticks_days": 7,
        "keep_decisions_days": 60,
        "keep_facts_days": 30,
        "keep_event_runs_days": 60,
        "keep_snapshots_days": 30,
        "keep_observations_days": 60,
        "batch_size": 10,
        "db": Path(":memory:"),
        "chengdu_keep_event_dates": "",
        "chengdu_active_days_ahead": 2,
        "chengdu_recent_hours": 48,
        "chengdu_preserve_latest_per_key": True,
        "now_utc": "",
        "estimate_tick_rollup_groups": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_expired_ticks_are_archived_before_raw_delete() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE event_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            mid REAL,
            spread REAL,
            bid_depth_topN REAL,
            ask_depth_topN REAL,
            created_at TEXT,
            source TEXT,
            source_detail TEXT
        );
        """
    )
    rows = [
        ("2026-04-20", "2026-04-30", "slug-a", "20", "YES", 0.10, 0.20, 0.15, 0.10, 10.0, 12.0, "2026-04-20T04:01:00+00:00"),
        ("2026-04-20", "2026-04-30", "slug-a", "20", "YES", 0.12, 0.24, 0.18, 0.12, 11.0, 14.0, "2026-04-20T04:30:00+00:00"),
        ("2026-04-30", "2026-04-30", "slug-a", "20", "YES", 0.30, 0.40, 0.35, 0.10, 9.0, 10.0, "2026-04-30T04:00:00+00:00"),
        ("2026-04-20", "2026-04-30", "slug-b", "21", "YES", 0.11, 0.21, 0.16, 0.10, 5.0, 6.0, "2026-04-20T05:00:00+00:00"),
    ]
    conn.executemany(
        """
        INSERT INTO event_market_ticks (
            run_date, event_date, slug, outcome, side,
            best_bid, best_ask, mid, spread, bid_depth_topN, ask_depth_topN, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    _run_retention(conn, _make_args(), dt.date(2026, 4, 30))

    remaining = conn.execute("SELECT COUNT(*) FROM event_market_ticks").fetchone()[0]
    assert remaining == 2
    rollup = conn.execute(
        """
        SELECT *
        FROM event_market_tick_rollups
        WHERE slug='slug-a' AND outcome='20' AND side='YES'
        """
    ).fetchone()
    assert rollup is not None
    assert rollup["grain"] == "hour"
    assert rollup["bucket_start"] == "2026-04-20T04:00:00+00:00"
    assert rollup["tick_count"] == 2
    assert rollup["first_best_bid"] == 0.10
    assert rollup["last_best_bid"] == 0.12
    assert rollup["avg_best_ask"] == 0.22


def test_chengdu_tick_retention_rolls_up_non_chengdu_and_keeps_active_chengdu() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE event_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            mid REAL,
            spread REAL,
            bid_depth_topN REAL,
            ask_depth_topN REAL,
            created_at TEXT,
            source TEXT,
            source_detail TEXT
        );
        """
    )
    rows = [
        ("2026-06-19", "2026-06-19", "highest-temperature-in-seattle-on-june-19-2026", "25", "YES", 0.10, 0.20, 0.15, 0.10, 10.0, 12.0, "2026-06-19T04:01:00+00:00"),
        ("2026-06-19", "2026-06-19", "highest-temperature-in-seattle-on-june-19-2026", "25", "YES", 0.12, 0.24, 0.18, 0.12, 11.0, 14.0, "2026-06-19T04:30:00+00:00"),
        ("2026-06-19", "2026-06-19", "highest-temperature-in-chengdu-on-june-19-2026", "25", "YES", 0.31, 0.41, 0.36, 0.10, 7.0, 8.0, "2026-06-19T05:01:00+00:00"),
        ("2026-06-19", "2026-06-19", "highest-temperature-in-chengdu-on-june-19-2026", "25", "YES", 0.35, 0.45, 0.40, 0.10, 9.0, 10.0, "2026-06-19T05:30:00+00:00"),
        ("2026-06-21", "2026-06-21", "highest-temperature-in-chengdu-on-june-21-2026", "30", "YES", 0.50, 0.60, 0.55, 0.10, 20.0, 22.0, "2026-06-21T06:00:00+00:00"),
    ]
    conn.executemany(
        """
        INSERT INTO event_market_ticks (
            run_date, event_date, slug, outcome, side,
            best_bid, best_ask, mid, spread, bid_depth_topN, ask_depth_topN, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    _run_chengdu_tick_retention(
        conn,
        _make_args(
            chengdu_keep_event_dates="2026-06-21,2026-06-22,2026-06-23",
            now_utc="2026-06-22T00:00:00+00:00",
            chengdu_recent_hours=24,
        ),
        dt.date(2026, 6, 21),
    )

    remaining = conn.execute(
        """
        SELECT slug, event_date, outcome, side, created_at
        FROM event_market_ticks
        ORDER BY id
        """
    ).fetchall()
    assert [(row["slug"], row["created_at"]) for row in remaining] == [
        ("highest-temperature-in-chengdu-on-june-19-2026", "2026-06-19T05:30:00+00:00"),
        ("highest-temperature-in-chengdu-on-june-21-2026", "2026-06-21T06:00:00+00:00"),
    ]
    seattle_rollup = conn.execute(
        """
        SELECT tick_count, first_best_bid, last_best_bid
        FROM event_market_tick_rollups
        WHERE slug='highest-temperature-in-seattle-on-june-19-2026'
        """
    ).fetchone()
    assert seattle_rollup is not None
    assert seattle_rollup["tick_count"] == 2
    assert seattle_rollup["first_best_bid"] == 0.10
    assert seattle_rollup["last_best_bid"] == 0.12


def _db_with_freelist(path: Path, *, rows: int, delete: int) -> None:
    """A database with real free pages: fill it, delete most of it.

    Deleting rows does not shrink a SQLite file -- the pages go on the
    freelist and wait to be reused -- which is the entire reason VACUUM
    exists and the entire reason its cost has to be computed rather than
    guessed at.
    """

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE bulk (id INTEGER PRIMARY KEY, body BLOB)")
    conn.executemany(
        "INSERT INTO bulk (id, body) VALUES (?, ?)",
        [(i, b"x" * 4000) for i in range(rows)],
    )
    conn.commit()
    conn.execute("DELETE FROM bulk WHERE id < ?", (delete,))
    conn.commit()
    conn.close()


def test_vacuum_space_is_the_peak_not_a_multiple_of_the_file(tmp_path) -> None:
    """``current + compacted``, because VACUUM builds one beside the other.

    The rule this replaced was ``file * 1.25``, which is under the true
    peak for any database whose freelist is below 20% -- so it was not a
    margin, it was a coin flip that happened to land right while the disk
    was too full to reach it at all.  Measured on the live archive
    2026-09-11: 19.54 GiB on disk, 28.4% freelist, compacted 13.99 GiB,
    peak 33.53 GiB.  The old guard admitted that at 24.42 GiB free.
    """

    from scripts.archive_db_governance import (
        VACUUM_HEADROOM_BYTES,
        _vacuum_space,
    )

    db = tmp_path / "archive.db"
    _db_with_freelist(db, rows=2000, delete=1500)
    conn = sqlite3.connect(str(db))
    space = _vacuum_space(conn, db)
    conn.close()

    assert space["compacted"] < space["db_size"], (
        "the fixture must actually carry free pages"
    )
    assert space["reclaims"] == space["db_size"] - space["compacted"]
    assert space["required"] == (
        space["db_size"] + space["compacted"] + VACUUM_HEADROOM_BYTES
    )
    # And the old rule is genuinely below it -- this is the regression.
    assert int(space["db_size"] * 1.25) < space["required"]


def test_vacuum_refuses_at_the_peak_and_says_how_short_it_is(
    tmp_path, monkeypatch, capsys
) -> None:
    from scripts import archive_db_governance as governance

    db = tmp_path / "archive.db"
    _db_with_freelist(db, rows=2000, delete=1500)
    conn = sqlite3.connect(str(db))
    space = governance._vacuum_space(conn, db)

    class _Usage:
        def __init__(self, free):
            self.free = free
            self.total = free * 10
            self.used = free * 9

    # Enough for the retired rule, short of the real peak: the exact state
    # the live volume was in on 2026-09-11.
    between = int(space["db_size"] * 1.3)
    assert between < space["required"]
    monkeypatch.setattr(
        governance.shutil, "disk_usage", lambda _p: _Usage(between)
    )
    try:
        governance._vacuum(conn, db)
    except SystemExit as exit_error:
        message = str(exit_error)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("vacuum ran on a volume that cannot hold it")
    finally:
        conn.close()
    assert "refuse_vacuum_insufficient_free_space" in message
    assert "short=" in message
    # The plan is printed before the refusal, so an operator sees what it
    # would have reclaimed and what it would have needed.
    assert "vacuum_plan" in capsys.readouterr().out


def test_vacuum_runs_and_reclaims_when_the_peak_fits(tmp_path, monkeypatch):
    from scripts import archive_db_governance as governance

    db = tmp_path / "archive.db"
    _db_with_freelist(db, rows=2000, delete=1500)
    conn = sqlite3.connect(str(db))
    space = governance._vacuum_space(conn, db)
    before = db.stat().st_size

    class _Usage:
        def __init__(self, free):
            self.free = free
            self.total = free * 10
            self.used = free * 9

    monkeypatch.setattr(
        governance.shutil,
        "disk_usage",
        lambda _p: _Usage(space["required"] + 1),
    )
    governance._vacuum(conn, db)
    conn.close()
    assert db.stat().st_size < before
