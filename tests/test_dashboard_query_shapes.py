"""The render's two heaviest lookups, pinned by SHAPE rather than by clock.

A render is a reader on a 38 GB store and the hosted sweep starts one every
thirty minutes, so a lookup that scans the whole envelope table does not
fail -- it just makes the page old.  That is how it presents: on 2026-09-09
a user refreshed the board after P1's band table moved and got the previous
table back, because the render that would have shown the new one was still
running twenty-four minutes in.  The section cache (2026-09-02, full build
~14 minutes) is the mitigation; these two queries were the cause, and by
then the build was ~24 minutes.

Measured that day against the live store, before and after:

  station -> timezone   127.60s -> 0.02s   45 rows wanted, 147,971 scanned
  first P0 claim         69.10s -> 47.50s  1,215 wanted, 147,971 scanned

The second number is honest about its own size.  The rewrite drops the
result set from 147,971 rows to 1,215 and the sort with it, but the scan was
never the cost: measured separately, the covering index scan is 2.2s and the
1,215 overflow-page reads are 45.3s, and a sequential scan of the old shape
gets its 147,971 large columns cheaply enough that removing them buys 31%,
not an order of magnitude.  So no index was added for it -- an index would
have targeted the 2.2s.  Together the two save ~149s of a ~24 minute render:
real, and about 10% of it.  The rest of that render is not diagnosed here.

Both are pinned as query plans and sources, never as elapsed time: a timing
test on a store this size measures the page cache, and it would go red on a
busy machine while the defect it guards is a shape.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

SOURCE = (ROOT / "scripts" / "calibration_dashboard.py").read_text()


def _plan(conn: sqlite3.Connection, sql: str, *params) -> str:
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return "\n".join(str(row[-1]) for row in rows)


def _schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE basket_probability_envelope ("
        " envelope_id TEXT PRIMARY KEY, stage TEXT NOT NULL,"
        " station_id TEXT NOT NULL, event_date TEXT NOT NULL,"
        " settlement_timezone TEXT NOT NULL, decision_at_utc TEXT NOT NULL,"
        " state TEXT NOT NULL, probabilities_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX basket_probability_envelope_asof_idx"
        " ON basket_probability_envelope(station_id, event_date, state,"
        " decision_at_utc, stage)"
    )
    conn.execute(
        "CREATE TABLE calibration_station_market_timezone_binding("
        " binding_id TEXT PRIMARY KEY, station_id TEXT NOT NULL UNIQUE,"
        " settlement_timezone TEXT NOT NULL,"
        " market_registry_timezone_count INTEGER NOT NULL"
        "  CHECK(market_registry_timezone_count=1))"
    )


def test_the_timezone_map_comes_from_the_48_row_authority():
    """Not from a DISTINCT over every envelope ever written.

    The old predicate was ``settlement_timezone IS NOT NULL`` on a NOT NULL
    column -- it excluded nothing, and no index the planner would pick
    covers that column, so it read every row to return one per station.
    """

    body = SOURCE[SOURCE.index("def _live_entry_edge"):] if (
        "def _live_entry_edge" in SOURCE
    ) else SOURCE
    assert "calibration_station_market_timezone_binding" in SOURCE
    # The scan it replaced must not come back, in either spelling.
    assert 'WHERE settlement_timezone IS NOT NULL' not in SOURCE
    assert 'SELECT DISTINCT station_id, settlement_timezone' not in SOURCE
    del body

    conn = sqlite3.connect(":memory:")
    _schema(conn)
    plan = _plan(
        conn,
        "SELECT station_id, settlement_timezone"
        " FROM calibration_station_market_timezone_binding",
    )
    assert "basket_probability_envelope" not in plan


def test_the_first_claim_of_a_station_day_is_chosen_in_sql():
    """One row per station-day out of the database, not out of Python.

    The loop keeps the FIRST P0 decision per station-day.  Reading every
    decision and dropping all but the first spent an overflow-page read per
    discarded row -- 1,215 kept out of 147,971 on 2026-09-09.
    """

    assert "GROUP BY station_id, event_date" in SOURCE
    # The full-table sort this replaced is gone.
    assert (
        '" WHERE event_date >= ? AND stage=\'P0\'"\n'
        '        " ORDER BY decision_at_utc",'
    ) not in SOURCE

    conn = sqlite3.connect(":memory:")
    _schema(conn)
    plan = _plan(
        conn,
        "SELECT station_id, event_date, probabilities_json,"
        " MIN(decision_at_utc) FROM basket_probability_envelope"
        " WHERE event_date >= ? AND stage='P0'"
        " GROUP BY station_id, event_date",
        "2026-08-15",
    )
    # The grouping rides the index order; a temp B-tree here would mean the
    # whole result set is being materialised and sorted again.
    assert "TEMP B-TREE" not in plan.upper(), plan
    assert "basket_probability_envelope_asof_idx" in plan, plan


def test_min_and_its_bare_columns_pick_the_same_row():
    """The rewrite rests on SQLite's documented min/max bare-column rule.

    If bare columns came from an arbitrary row of the group, the dashboard
    would show one decision's timestamp beside another decision's claim.
    Pinned because it is a dialect guarantee, not general SQL.
    """

    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.executemany(
        "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?)",
        [
            ("e3", "P0", "KLGA", "2026-09-01", "America/New_York",
             "2026-09-01T18:00:00Z", "COMMITTED", '{"b": 0.3}'),
            ("e1", "P0", "KLGA", "2026-09-01", "America/New_York",
             "2026-09-01T06:00:00Z", "COMMITTED", '{"b": 0.1}'),
            ("e2", "P0", "KLGA", "2026-09-01", "America/New_York",
             "2026-09-01T12:00:00Z", "COMMITTED", '{"b": 0.2}'),
        ],
    )
    row = conn.execute(
        "SELECT station_id, event_date, probabilities_json,"
        " MIN(decision_at_utc) FROM basket_probability_envelope"
        " WHERE event_date >= ? AND stage='P0'"
        " GROUP BY station_id, event_date",
        ("2026-08-15",),
    ).fetchone()
    assert row[3] == "2026-09-01T06:00:00Z"
    # The claim must be the 06:00 row's, not the 18:00 one inserted first.
    assert row[2] == '{"b": 0.1}'


def test_the_snapshot_sweep_does_not_sort_two_gigabytes_to_find_a_maximum():
    """The last non-zero bid per bucket, without a global sort.

    ``ORDER BY observed_at`` existed so the assignment could overwrite in
    time order and leave the LAST bid standing.  There is no index for that
    order, so SQLite built a temp B-tree over every matching row -- and the
    column it had to carry through the sort is ``buckets_json``, 2.05 GB
    across ~105k snapshots (2026-09-02).  The order was only ever needed
    within a station-day-bucket, so the maximum is taken explicitly and the
    scan stays on ``day_expiry_idx``, where ``event_date >=`` can seek.
    """

    assert 'FROM basket_official_ws_snapshot WHERE event_date >= ?"' in SOURCE
    assert '" ORDER BY observed_at",' not in SOURCE
    assert "last_bid_at" in SOURCE

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot ("
        " snapshot_id TEXT PRIMARY KEY, station_id TEXT NOT NULL,"
        " event_date TEXT NOT NULL, observed_at TEXT NOT NULL,"
        " quote_expires_at TEXT, buckets_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX basket_official_ws_snapshot_day_expiry_idx"
        " ON basket_official_ws_snapshot(event_date, quote_expires_at)"
    )
    plan = _plan(
        conn,
        "SELECT station_id, event_date, observed_at, buckets_json"
        " FROM basket_official_ws_snapshot WHERE event_date >= ?",
        "2026-08-15",
    )
    assert "TEMP B-TREE" not in plan.upper(), plan
    assert "day_expiry_idx" in plan, plan


def test_the_last_bid_wins_whatever_order_the_rows_arrive_in():
    """The property the dropped ORDER BY was providing, stated directly.

    Rows now arrive in index order, which is not time order, so the winner
    must be chosen by comparing timestamps rather than by arriving last.
    A bucket that goes quiet keeps its own last non-zero bid -- it does not
    inherit the final snapshot's silence -- which is why this cannot be
    rewritten as "read the newest snapshot per station-day".
    """

    rows = [
        ("KLGA", "2026-09-01", "2026-09-01T18:00:00Z", {"b1": 0.55, "b2": 0.0}),
        ("KLGA", "2026-09-01", "2026-09-01T06:00:00Z", {"b1": 0.20, "b2": 0.30}),
        ("KLGA", "2026-09-01", "2026-09-01T12:00:00Z", {"b1": 0.40, "b2": 0.0}),
    ]
    last_bid: dict[str, float] = {}
    last_bid_at: dict[str, str] = {}
    for _station, _event, observed, buckets in rows:
        for bucket, bid in buckets.items():
            if bid and float(bid) > 0:
                if observed >= last_bid_at.get(bucket, ""):
                    last_bid[bucket] = float(bid)
                    last_bid_at[bucket] = observed
    # b1 last traded at 18:00 even though that row was read first.
    assert last_bid["b1"] == 0.55
    # b2 went quiet after 06:00 and keeps that bid rather than losing it to
    # the later zero -- the old loop behaved the same way, because a zero
    # never entered the dict at all.
    assert last_bid["b2"] == 0.30
