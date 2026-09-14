"""The truth increment asks the registry without reading its table, and skips days still open.

2026-09-14: every production cycle the stage ran, its backlog probe read the
whole market registry (23-25s) and returned today's and the listed days first,
because the older days already held truth; the stage then asked twelve scopes
whose settlement days had not closed and recorded
``skipped_open_settlement_day`` twelve times -- 43-204 seconds for nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import market_registry as mr
from bayes_basket_target import production_worker as pw
from bayes_basket_target.production_worker import (
    TargetProductionWorker,
    _settlement_day_open_in_every_zone,
)


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE basket_target_market_registry_snapshot(snapshot_sha256 TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE basket_target_market_registry(snapshot_sha256 TEXT, slug TEXT, station_id TEXT,"
        " event_date TEXT, settlement_timezone TEXT, source_eligible INTEGER, refreshed_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX basket_target_market_registry_latest_idx"
        " ON basket_target_market_registry(snapshot_sha256, station_id, event_date, source_eligible)"
    )
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth(station_id TEXT, local_date TEXT, qc_status TEXT)"
    )
    return conn


def _ingest(conn, sha, rows, refreshed="2026-09-14T00:00:00Z") -> None:
    conn.execute("INSERT INTO basket_target_market_registry_snapshot VALUES(?)", (sha,))
    conn.executemany(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        [(sha, f"{s}-{d}", s, d, z, e, refreshed) for s, d, z, e in rows],
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _fresh_memo():
    mr._SNAPSHOT_ANSWERS.clear()
    yield
    mr._SNAPSHOT_ANSWERS.clear()


def _worker(conn, monkeypatch):
    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.canonical_source_conn = conn
    asked: list[tuple[str, str]] = []

    def materialize(_target, _canonical, *, station_id, event_date, as_of_utc):
        asked.append((station_id, event_date))
        return {"state": "idempotent", "wrote": False}

    monkeypatch.setattr(pw, "materialize_settled_station_truth", materialize)
    return worker, asked


def test_a_day_still_open_everywhere_is_not_asked_and_a_closed_day_behind_it_is(tmp_path, monkeypatch) -> None:
    conn = _db(tmp_path / "ssot.db")
    rows = [
        # 09-13: closed everywhere by 09:30Z on the 14th, no truth yet.
        ("RJTT", "2026-09-13", "Asia/Tokyo", 1),
        ("KLAX", "2026-09-13", "America/Los_Angeles", 1),
        # 09-14: open in Tokyo's evening and in Los Angeles's small hours.
        ("RJTT", "2026-09-14", "Asia/Tokyo", 1),
        ("KLAX", "2026-09-14", "America/Los_Angeles", 1),
        # Listed.
        ("RJTT", "2026-09-15", "Asia/Tokyo", 1),
        # Already has truth.
        ("EGLC", "2026-09-12", "Europe/London", 1),
        # Never eligible.
        ("LLBG", "2026-09-13", "Asia/Jerusalem", 0),
    ]
    _ingest(conn, "s1", rows)
    conn.execute("INSERT INTO calibration_station_daily_high_truth VALUES('EGLC','2026-09-12','ACCEPTED')")
    conn.commit()
    worker, asked = _worker(conn, monkeypatch)
    receipt = worker._materialize_truth_increment(as_of_utc="2026-09-14T09:30:00Z")
    assert asked == [("KLAX", "2026-09-13"), ("RJTT", "2026-09-13")]
    assert receipt["not_closed_scope_count"] == 3


def test_the_cap_is_spent_on_closed_days_only(tmp_path, monkeypatch) -> None:
    conn = _db(tmp_path / "ssot.db")
    # Same event date, sorted by station: the Honolulu scopes come first and
    # their 09-13 is still open at 09:30Z on the 14th (it ends at 10:00Z).
    rows = [(f"A{i:02d}", "2026-09-13", "Pacific/Honolulu", 1) for i in range(20)]   # open
    rows += [(f"Z{i:02d}", "2026-09-13", "Asia/Tokyo", 1) for i in range(20)]        # closed
    _ingest(conn, "s1", rows)
    worker, asked = _worker(conn, monkeypatch)
    worker._materialize_truth_increment(as_of_utc="2026-09-14T09:30:00Z")
    assert len(asked) == pw.TRUTH_INCREMENT_MAX_SCOPES
    assert all(station.startswith("Z") for station, _day in asked)


def test_a_scope_the_registry_no_longer_admits_is_not_asked(tmp_path, monkeypatch) -> None:
    conn = _db(tmp_path / "ssot.db")
    _ingest(conn, "s1", [("LTFM", "2026-09-13", "Europe/Istanbul", 1)], refreshed="2026-09-13T01:00:00Z")
    _ingest(conn, "s2", [("LTFM", "2026-09-13", "Europe/Istanbul", 0)], refreshed="2026-09-13T03:00:00Z")
    worker, asked = _worker(conn, monkeypatch)
    worker._materialize_truth_increment(as_of_utc="2026-09-14T09:30:00Z")
    assert asked == []


def test_the_whole_registry_is_never_read_for_the_backlog(tmp_path, monkeypatch) -> None:
    conn = _db(tmp_path / "ssot.db")
    _ingest(conn, "s1", [("RJTT", "2026-09-13", "Asia/Tokyo", 1)])
    worker, _asked = _worker(conn, monkeypatch)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    worker._materialize_truth_increment(as_of_utc="2026-09-14T09:30:00Z")
    conn.set_trace_callback(None)
    assert not any(
        "FROM basket_target_market_registry" in sql
        and "DISTINCT" in sql
        and "snapshot_sha256=" not in sql
        for sql in statements
    ), statements


def test_open_means_open_in_every_zone_the_registry_named() -> None:
    from datetime import datetime, timezone

    at = datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc)
    assert _settlement_day_open_in_every_zone("2026-09-14", {"Asia/Tokyo"}, at)
    # Tokyo's 09-13 ended at 15:00Z on the 13th.
    assert not _settlement_day_open_in_every_zone("2026-09-13", {"Asia/Tokyo"}, at)
    # One zone closed is enough to leave it to the materializer.
    # Honolulu's 09-13 is still open (to 10:00Z); Tokyo's is closed.
    assert _settlement_day_open_in_every_zone("2026-09-13", {"Pacific/Honolulu"}, at)
    assert not _settlement_day_open_in_every_zone("2026-09-13", {"Asia/Tokyo", "Pacific/Honolulu"}, at)
    # Nothing usable: undecidable here.
    assert not _settlement_day_open_in_every_zone("2026-09-14", {""}, at)
    assert not _settlement_day_open_in_every_zone("2026-09-14", {"Not/AZone"}, at)
