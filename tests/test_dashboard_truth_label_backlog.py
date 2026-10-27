"""The truth link counts days whose truth landed but whose facts are unlabelled.

2026-10-15 (user 「但是前端的"真值结算"显示健康」): the lamp judged arrival
alone and read green while 360 station-days since 10-05 waited for labels.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth(truth_id TEXT, station_id TEXT,"
        " local_date TEXT, revision_no INTEGER, source_lane TEXT, qc_status TEXT,"
        " canonical_committed_at_utc TEXT)"
    )
    conn.execute("CREATE TABLE basket_policy_training_fact(fact_id TEXT, station_id TEXT, event_date TEXT)")
    conn.execute("CREATE TABLE basket_policy_training_label(fact_id TEXT, truth_id TEXT, UNIQUE(fact_id, truth_id))")
    rows = [
        ("t-a0", "KLGA", "2026-10-10", 0, "AWC", "2026-10-11T06:00:00Z"),
        ("t-a1", "KLGA", "2026-10-10", 1, "SETTLEMENT_SOURCE", "2026-10-12T06:00:00Z"),  # newest
        ("t-b0", "EGLC", "2026-10-11", 0, "AWC", "2026-10-12T06:00:00Z"),
        ("t-c0", "RJTT", "2026-10-15", 0, "AWC", "2026-10-15T10:00:00Z"),  # inside the grace
    ]
    conn.executemany(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,'ACCEPTED',?)", rows)
    conn.executemany("INSERT INTO basket_policy_training_fact VALUES(?,?,?)", [
        ("d2fact_1", "KLGA", "2026-10-10"), ("d2fact_2", "EGLC", "2026-10-11"),
        ("d2fact_3", "RJTT", "2026-10-15"),
    ])
    # KLGA's fact carries the superseded truth's label only; EGLC's is done.
    conn.executemany("INSERT INTO basket_policy_training_label VALUES(?,?)", [
        ("d2fact_1", "t-a0"), ("d2fact_2", "t-b0"),
    ])
    return conn


def test_a_revision_its_facts_lack_is_a_backlog_and_a_fresh_truth_is_not(monkeypatch) -> None:
    monkeypatch.setattr(C, "_now", lambda: datetime(2026, 10, 15, 12, tzinfo=timezone.utc))
    backlog = C._truth_label_backlog(_db())
    assert backlog["station_days"] == 1
    assert backlog["oldest_date"] == "2026-10-10"


def test_labelled_revisions_clear_the_backlog(monkeypatch) -> None:
    monkeypatch.setattr(C, "_now", lambda: datetime(2026, 10, 15, 12, tzinfo=timezone.utc))
    conn = _db()
    conn.execute("INSERT INTO basket_policy_training_label VALUES('d2fact_1','t-a1')")
    assert C._truth_label_backlog(conn)["station_days"] == 0


def test_a_paged_run_checks_a_clean_day_again_only_when_its_truth_moves(monkeypatch, tmp_path) -> None:
    """2026-10-27: with no backlog every fact was looked up every render
    (~55k rows, 12-14 s; ~250 s swapping).  A clean station-day is kept with
    its truth; a revision is checked again."""

    monkeypatch.setattr(C, "_now", lambda: datetime(2026, 10, 15, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setattr(C, "_PAGED_RUN", {"paged": True})
    conn = _db()
    conn.execute("INSERT INTO basket_policy_training_label VALUES('d2fact_1','t-a1')")
    assert C._truth_label_backlog(conn)["station_days"] == 0
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    assert C._truth_label_backlog(conn)["station_days"] == 0
    conn.set_trace_callback(None)
    assert not [sql for sql in seen if "basket_policy_training_fact" in sql], seen
    # A third revision for KLGA: its facts lack it, and the kept day does not hide it.
    conn.execute("INSERT INTO calibration_station_daily_high_truth VALUES"
                 "('t-a2','KLGA','2026-10-10',2,'SETTLEMENT_SOURCE','ACCEPTED','2026-10-13T06:00:00Z')")
    backlog = C._truth_label_backlog(conn)
    assert backlog["station_days"] == 1 and backlog["oldest_date"] == "2026-10-10"
