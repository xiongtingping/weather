"""Listing F's ladder read off the market registry (2026-10-18, found giving
WSSS an order ticket): the registry keeps every snapshot's rows, so reading
all of them handed the strict parser the ladder hundreds of times over and the
registry branch failed closed on every station-day (0 of 139) -- the keep-F
pick then had no ladder wherever no decision had been priced (WSSS 10-18..20,
WMKK 10-20).  The newest snapshot holding the scope is the ladder."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from bayes_basket_target.listing_forecast_f import station_day_rungs
from bayes_basket_target.market_registry import scope_ladder_rows

LADDER = [("25°C or below", None, 25.0), ("26°C", 26.0, 26.0), ("27°C or higher", 27.0, None)]


def _registry(snapshots: list[list[tuple[str, float | None, float | None]]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_target_market_registry_snapshot (snapshot_sha256 TEXT, ingested_at_utc TEXT)")
    conn.execute("CREATE TABLE basket_target_market_registry (snapshot_sha256 TEXT, station_id TEXT, event_date TEXT,"
                 " settlement_unit TEXT, outcome TEXT, registry_json TEXT)")
    for i, ladder in enumerate(snapshots):
        conn.execute("INSERT INTO basket_target_market_registry_snapshot VALUES (?,?)", (f"s{i}", f"2026-10-18T0{i}:00:00Z"))
        conn.executemany("INSERT INTO basket_target_market_registry VALUES (?,'WSSS','2026-10-18','C',?,?)",
                         [(f"s{i}", o, json.dumps({"lower": lo, "upper": hi})) for o, lo, hi in ladder])
    return conn


def _rungs(conn: sqlite3.Connection):
    return station_day_rungs(conn, station_id="WSSS", event_date=date(2026, 10, 18), prefer_registry=True)


def test_registry_ladder_reads_through_repeated_snapshots() -> None:
    conn = _registry([LADDER] * 5)
    assert len(scope_ladder_rows(conn, station_id="WSSS", event_date="2026-10-18")) == 3
    assert _rungs(conn) == [("25°C or below", None, 25.5), ("26°C", 25.5, 26.5), ("27°C or higher", 26.5, None)]


def test_registry_ladder_takes_the_newest_snapshot() -> None:
    wider = [("24°C or below", None, 24.0), ("25°C", 25.0, 25.0), ("26°C", 26.0, 26.0), ("27°C or higher", 27.0, None)]
    assert [r[0] for r in _rungs(_registry([LADDER, wider]))] == ["24°C or below", "25°C", "26°C", "27°C or higher"]


def test_registry_ladder_with_a_gap_still_fails_closed() -> None:
    gap = [("25°C or below", None, 25.0), ("27°C or higher", 27.0, None)]
    assert _rungs(_registry([gap])) is None                     # no decided ladder to fall back to either
    assert scope_ladder_rows(_registry([]), station_id="WSSS", event_date="2026-10-18") == []
