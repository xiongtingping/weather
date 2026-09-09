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
