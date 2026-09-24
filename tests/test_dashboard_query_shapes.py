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
        "CREATE INDEX basket_probability_envelope_publisher_scope_idx"
        " ON basket_probability_envelope(state, event_date, station_id,"
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


def test_the_shape_gap_lookup_does_not_scan_the_envelope_table():
    """The third lookup of this shape, and the only one with no index at all.

    Measured 2026-09-09 against the live store: ``SCAN
    basket_probability_envelope``, 44,392 rows read off a 38 GB file to
    return 720 station-days, 134.8s -- half of the live_readiness section,
    which is 40% of the render.  Every one of the table's three indexes
    leads with station_id or state, and the query knew only stage and
    event_date, so there was no seekable prefix and the planner took the
    table.

    ``state='COMMITTED'`` supplies that prefix and is the filter every other
    reader of this table already applies.  2.36s after, covering, no table
    visits, and verified equivalent on the live store first: 720 station-days
    both ways, all 720 rowids identical.
    """

    # The scan must not come back, in the spelling it had.
    assert (
        '"SELECT station_id, event_date, created_at, rowid"' not in SOURCE
    ), "the created_at/rowid scan is back"

    conn = sqlite3.connect(":memory:")
    _schema(conn)
    plan = _plan(
        conn,
        "SELECT station_id, event_date, MAX(decision_at_utc), rowid"
        " FROM basket_probability_envelope"
        " WHERE state='COMMITTED' AND event_date >= ? AND stage='P0'"
        " GROUP BY station_id, event_date",
        "2026-08-15",
    )
    assert "SCAN basket_probability_envelope" not in plan, plan
    assert "basket_probability_envelope_publisher_scope_idx" in plan, plan


def test_max_picks_the_last_decision_of_the_station_day():
    """Same bare-column rule as the MIN lookup, in the other direction.

    The shape gap reads the claim of the LAST P0 of the day; if the bare
    rowid came from an arbitrary row of the group it would read some earlier
    decision's vector instead, and the card would be quietly wrong rather
    than slow.
    """

    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.executemany(
        "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?)",
        [
            ("e1", "P0", "KLGA", "2026-09-01", "America/New_York",
             "2026-09-01T06:00:00Z", "COMMITTED", '{"b": 0.1}'),
            ("e3", "P0", "KLGA", "2026-09-01", "America/New_York",
             "2026-09-01T18:00:00Z", "COMMITTED", '{"b": 0.3}'),
            ("e2", "P0", "KLGA", "2026-09-01", "America/New_York",
             "2026-09-01T12:00:00Z", "COMMITTED", '{"b": 0.2}'),
        ],
    )
    row = conn.execute(
        "SELECT station_id, event_date, MAX(decision_at_utc), rowid"
        " FROM basket_probability_envelope"
        " WHERE state='COMMITTED' AND event_date >= ? AND stage='P0'"
        " GROUP BY station_id, event_date",
        ("2026-08-15",),
    ).fetchone()
    assert row[2] == "2026-09-01T18:00:00Z"
    claim = conn.execute(
        "SELECT probabilities_json FROM basket_probability_envelope"
        " WHERE rowid=?",
        (row[3],),
    ).fetchone()[0]
    assert claim == '{"b": 0.3}', "the rowid must be the 18:00 row's"


def test_an_uncommitted_envelope_is_not_counted_by_the_shape_gap():
    """The filter that makes it seekable also fixes what it was counting."""

    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.executemany(
        "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?)",
        [
            ("e1", "P0", "KLGA", "2026-09-01", "America/New_York",
             "2026-09-01T06:00:00Z", "COMMITTED", '{"b": 0.1}'),
            ("e2", "P0", "KLGA", "2026-09-01", "America/New_York",
             "2026-09-01T23:00:00Z", "PREPARED", '{"b": 0.9}'),
        ],
    )
    row = conn.execute(
        "SELECT station_id, event_date, MAX(decision_at_utc), rowid"
        " FROM basket_probability_envelope"
        " WHERE state='COMMITTED' AND event_date >= ? AND stage='P0'"
        " GROUP BY station_id, event_date",
        ("2026-08-15",),
    ).fetchone()
    assert row[2] == "2026-09-01T06:00:00Z", "the uncommitted 23:00 row must not win"


def _oos_evaluation_schema(conn: sqlite3.Connection) -> None:
    """The table and its scope index exactly as the SSOT declares them.

    Taken from ``ssot.DDL`` rather than retyped: the cost this guards is a
    property of the real column order (the filter column sits behind both
    metric blobs) and of the real index, so a copy would test a table that
    does not exist.
    """

    from bayes_basket_target import ssot

    table = re.search(
        r"CREATE TABLE IF NOT EXISTS"
        r" calibration_cross_native_ensemble_oos_evaluation \(.*?\n\);",
        ssot.DDL,
        re.S,
    )
    index = re.search(
        r"CREATE INDEX IF NOT EXISTS"
        r" calibration_cross_native_oos_evaluation_scope_idx[^;]*;",
        ssot.DDL,
    )
    assert table and index, "the SSOT no longer declares the table or its scope index"
    conn.execute(table.group(0))
    conn.execute(index.group(0))


def _oos_row(evaluation_id, station, lead, cycle, evaluated, crps, system_set="a"):
    import json as _json

    return (
        evaluation_id, station, "PEAK", lead, cycle, "{}", "0" * 64, "tmax",
        "[]", system_set * 64, "0" * 64, "PRODUCTION_NO_LIVE_OOS_PASS",
        "PROFILE", 5, 3, "2026-09-10", "2026-09-11", "2026-09-13",
        _json.dumps({"crps": crps, "brier": 0.1, "nll": 1.0}),
        _json.dumps({"crps": 1.0, "brier": 0.2, "nll": 2.0}),
        evaluated, evaluation_id.ljust(64, "0"),
    )


def test_the_oos_scores_never_scan_the_evaluation_table():
    """Newest-per-scope is read off the covering scope index.

    On 2026-09-24 the week held 235,408 evaluations and 1.38 GB of metric
    JSON for 518 scopes.  Filtering ``evaluated_at_utc`` through the table
    reads every row's overflow chain to reach it; that read was 1,017 s of a
    render, and with the 1,500 s render deadline every render from 13:18Z on
    2026-09-23 was killed and the board kept an eighteen-hour-old payload.
    """

    import calibration_dashboard as board

    conn = sqlite3.connect(":memory:")
    _oos_evaluation_schema(conn)
    plan = _plan(conn, board.OOS_NEWEST_PER_SCOPE_SQL)
    assert "COVERING INDEX calibration_cross_native_oos_evaluation_scope_idx" in plan, plan
    body = SOURCE.split("def collect_oos_scores", 1)[1].split("\ndef ", 1)[0]
    assert "WHERE evaluation_id=?" in body, "the blobs must be fetched per chosen row"
    assert "evaluated_at_utc>=" not in body.split("OOS_NEWEST_PER_SCOPE_SQL", 1)[1], (
        "no second filter on evaluated_at_utc through the table"
    )


def test_the_oos_scores_read_the_newest_evaluation_of_each_scope():
    """Across system sets and seasons, as the ORDER BY + overwrite did."""

    import calibration_dashboard as board

    conn = sqlite3.connect(":memory:")
    _oos_evaluation_schema(conn)
    fresh = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hours')"
    ).fetchone()[0]
    older = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now','-3 hours')"
    ).fetchone()[0]
    stale = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now','-9 days')"
    ).fetchone()[0]
    conn.executemany(
        "INSERT INTO calibration_cross_native_ensemble_oos_evaluation"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            _oos_row("x1", "KLGA", "h00_12", "00Z", older, 0.9, "a"),
            _oos_row("x2", "KLGA", "h00_12", "00Z", fresh, 0.5, "b"),
            _oos_row("x3", "KLGA", "h12_24", "00Z", stale, 0.1, "a"),
        ],
    )
    result = board.collect_oos_scores(conn)
    assert result["scope_count"] == 1, "the nine-day-old scope is outside the week"
    (row,) = result["rows"]
    assert row["lead_bucket"] == "h00_12"
    assert row["crps"] == 0.5, "the newer evaluation (other system set) must win"
    assert result["latest_evaluated_at_utc"] == fresh


def test_the_banner_reads_each_familys_newest_run_off_its_index():
    """``members`` feeds the banner light and is read on every render.

    Until 2026-09-24 it counted each family's history in the same statement
    -- COUNT DISTINCT and the capture time live outside every index -- so it
    walked every member row through the table: 187-500 s a render, which
    under the paged render made it the read the WAL line kept interrupting.
    The depth moved to ``collect_member_depth`` (a slow-moving section); the
    light's own input is a covering index search.
    """

    import calibration_dashboard as board
    from bayes_basket_target import ssot

    conn = sqlite3.connect(":memory:")
    table = re.search(
        r"CREATE TABLE IF NOT EXISTS"
        r" calibration_native_ensemble_historical_member_run \(.*?\n\);",
        ssot.DDL,
        re.S,
    )
    index = re.search(
        r"CREATE INDEX IF NOT EXISTS calibration_native_member_skill_idx[^;]*;",
        ssot.DDL,
    )
    assert table and index
    conn.execute(table.group(0))
    conn.execute(index.group(0))
    plan = _plan(conn, board.MEMBER_LATEST_RUN_SQL, "NOAA_GEFS_05", "NOAA_GEFS_PDS_S3")
    assert "COVERING INDEX calibration_native_member_skill_idx" in plan, plan
    body = SOURCE.split("def collect_members(", 1)[1].split("\ndef ", 1)[0]
    assert "COUNT(DISTINCT" not in body, "the depth counts belong to collect_member_depth"
