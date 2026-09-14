"""Scope discovery reads only the event days that can still be open or not started.

``_decision_scopes`` read every committed probability envelope ever written --
447k rows, 4,619 distinct scope-stage answers -- to keep the ~466 whose day was
open or not started, and ``_listing_entry_scopes`` calls it eight or more times
a cycle.  Measured 2026-09-14 against the live SSOT: 4.5-5.0s a call, 0.01s
with an event-date floor.  A local day ends by 12:00Z on the UTC day after it,
so nothing before the clock's UTC date minus one can matter; the floor keeps
two days.  These tests hold the plan and the answer, not the timing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target import production_worker as pw
from bayes_basket_target.production_worker import (
    DECISION_SCOPE_PROBABILITY_SQL,
    TargetProductionWorker,
    _decision_scope_event_date_floor,
)
from bayes_basket_target.ssot import apply_production_ssot_schema

# Settlement zones spanning the fleet's extremes and a half-hour offset.
ZONES = {
    "PHNL": "Pacific/Honolulu",      # UTC-10
    "KLAX": "America/Los_Angeles",
    "EGLC": "Europe/London",
    "VILK": "Asia/Kolkata",          # UTC+5:30
    "NZWN": "Pacific/Auckland",      # UTC+12/13
    "PTKK": "Pacific/Kiritimati",    # UTC+14
}


def test_the_floor_is_two_utc_days_back_and_an_unreadable_clock_reads_everything() -> None:
    assert _decision_scope_event_date_floor("2026-09-14T00:30:00Z") == "2026-09-12"
    assert _decision_scope_event_date_floor("2026-09-14T23:59:59+09:00") == "2026-09-12"
    assert _decision_scope_event_date_floor("not a clock") == "0000-00-00"
    assert _decision_scope_event_date_floor("2026-09-14T00:30:00") == "0000-00-00"


def test_the_production_index_serves_the_read_as_a_range() -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    plan = " ".join(
        str(row[-1])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN "
            + DECISION_SCOPE_PROBABILITY_SQL.format(stage_projection="stage"),
            ("2026-09-12",),
        )
    )
    assert "USING COVERING INDEX" in plan, plan
    assert "event_date>?" in plan, plan
    assert "SCAN basket_probability_envelope" not in plan, plan


def _worker_over(rows) -> TargetProductionWorker:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT, event_date TEXT, settlement_timezone TEXT, stage TEXT, state TEXT)"
    )
    conn.executemany("INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?)", rows)
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    return worker


def test_the_bounded_read_finds_the_same_scopes_at_every_clock_across_three_days(monkeypatch) -> None:
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    rows = []
    for station, zone in ZONES.items():
        for offset in range(-12, 5):
            day = (start + timedelta(days=offset)).date().isoformat()
            for stage in ("P0", "P0-M", "P1"):
                rows.append((station, day, zone, stage, "COMMITTED"))
    worker = _worker_over(rows)
    clocks = [
        (start + timedelta(minutes=30 * step)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for step in range(3 * 48)
    ]
    bounded = {clock: worker._decision_scopes(as_of_utc=clock) for clock in clocks}
    monkeypatch.setattr(pw, "_decision_scope_event_date_floor", lambda _clock: "0000-00-00")
    for clock in clocks:
        assert bounded[clock] == worker._decision_scopes(as_of_utc=clock), clock
    # Not vacuous: the fixture holds far more days than any clock admits.
    assert any(len(v) for v in bounded.values())
    assert max(len(v) for v in bounded.values()) < len({(r[0], r[1]) for r in rows})


def test_a_not_started_listed_day_is_still_found_under_the_floor() -> None:
    rows = [
        ("NZWN", "2026-09-15", ZONES["NZWN"], "P0", "COMMITTED"),
        ("KLAX", "2026-09-14", ZONES["KLAX"], "P0-M", "COMMITTED"),
        ("KLAX", "2026-09-01", ZONES["KLAX"], "P1", "COMMITTED"),
    ]
    worker = _worker_over(rows)
    scopes = worker._decision_scopes(as_of_utc="2026-09-14T03:00:00Z")
    assert ("NZWN", "2026-09-15") in scopes      # listed, not started until 12:00Z
    assert ("KLAX", "2026-09-14") in scopes      # not started in Los Angeles yet
    assert ("KLAX", "2026-09-01") not in scopes
