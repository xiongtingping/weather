"""The recycle lane's inventory read must not scan the snapshot table.

It runs on the writer connection, after the main cycle, once per production
cycle.  The table is append-only at 172 snapshots per station-day -- 271,747
rows over 1,579 station-days on 2026-09-09 -- and the read is narrowed by
nothing, so the whole cost is paid every cycle whatever the event date.

Measured that day against the live store: 154.7s to return 1,579 rows.  While
it ran the forecast lane could not take a writer turn at all -- none between
the 12:35Z restart and 13:05Z -- so 2026-09-10's packets aged past their 6h
TTL unrefreshed, all 45 stations fail-closed on
native_ensemble_snapshot_stale, and the decision loop lost 41 of 80 blocked
scopes a cycle to latest_stage_stale_fail_closed three layers downstream.
None of those three names the read that caused it.

Pinned as a plan and as a source shape, never as elapsed time: a timing test
on a 38GB store measures the page cache.

2026-09-14: the one-pass form below the index was still a read of every row.
The bare column beside MAX() is fetched from the table whenever the running
maximum moves, which in index order is every row: 366,818 fetches for 1,771
station-days, minutes on a cold cache.  The read now finds the newest instant
on the covering index and fetches only the rows it names.

2026-09-18: still 3.6 s a cycle -- the join met g by scanning every complete
snapshot (484,069 rows) against an automatic index, and g was every
station-day since July (1,952) where the lane can only act on the last nine
days.  The read now has an event-date floor and a CROSS JOIN, so each newest
row is one seek on all four index columns: 495 station-days, 0.07-0.3 s.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bayes_basket_target.ssot import apply_production_ssot_schema

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "bayes_basket_target" / "certain_win_recycle.py").read_text()

READ = (
    "SELECT s.station_id, s.event_date, s.inventory_json, s.observed_at,"
    "       MAX(s.rowid)"
    " FROM (SELECT station_id, event_date, MAX(observed_at) AS newest"
    "       FROM basket_confirmed_inventory_snapshot"
    "       WHERE complete=1 AND event_date>=?"
    "       GROUP BY station_id, event_date) g"
    " CROSS JOIN basket_confirmed_inventory_snapshot s"
    "   ON s.complete=1 AND s.station_id=g.station_id"
    "  AND s.event_date=g.event_date AND s.observed_at=g.newest"
    " GROUP BY s.station_id, s.event_date"
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    return conn


def _plan(conn: sqlite3.Connection, sql: str) -> str:
    return "\n".join(str(row[-1]) for row in conn.execute(
        "EXPLAIN QUERY PLAN " + sql, ("2026-09-01",)
    ))


def test_the_read_is_what_the_lane_runs():
    """The plan and result tests below read ``READ``; this binds it to the
    inventory read inside ``plan_certain_win_recycles``, by the AST."""

    import ast

    tree = ast.parse(SOURCE)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "plan_certain_win_recycles"
    )
    reads = [
        node.args[0].value for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and "basket_confirmed_inventory_snapshot" in node.args[0].value
    ]
    assert reads == [READ]


def test_the_newest_instant_is_found_on_the_covering_index():
    plan = _plan(_conn(), READ)
    assert (
        "USING COVERING INDEX basket_confirmed_inventory_snapshot_recycle_idx"
        in plan
    ), plan


def test_each_newest_row_is_one_seek_not_a_scan_to_meet_g():
    plan = _plan(_conn(), READ)
    assert (
        "SEARCH s USING INDEX basket_confirmed_inventory_snapshot_recycle_idx"
        " (complete=? AND station_id=? AND event_date=? AND observed_at=?)"
        in plan
    ), plan


def test_the_one_pass_bare_column_read_does_not_come_back():
    """The form that fetched every row's payload to keep the newest one."""

    assert (
        '"SELECT station_id, event_date, inventory_json, MAX(observed_at)"'
        not in SOURCE
    )


def test_the_index_is_declared_in_the_schema():
    """Created by the schema, so a fresh deployment is not slow until
    somebody notices and adds it by hand."""

    conn = _conn()
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        ("basket_confirmed_inventory_snapshot_recycle_idx",),
    ).fetchone() is not None


def test_the_index_finds_the_newest_row_of_each_station_day():
    """A faster wrong answer is not the goal.

    The rewrite rests on SQLite's min/max bare-column rule: the columns
    beside MAX() come from the row that produced it.  If they came from an
    arbitrary row of the group the lane would recycle against a stale
    inventory, which is worse than being slow.
    """

    conn = _conn()
    columns = [row[1] for row in conn.execute(
        "PRAGMA table_info(basket_confirmed_inventory_snapshot)"
    )]
    def _row(version: str, observed: str, payload: str) -> tuple:
        values = {
            name: (
                1 if name == "complete"
                else "KLGA" if name == "station_id"
                else "2026-09-09" if name == "event_date"
                else "COMMITTED" if name == "state"
                else f"snap_{version}" if name == "inventory_snapshot_id"
                else version if name == "inventory_version"
                else observed if name == "observed_at"
                else payload if name == "inventory_json"
                else "x" * 64 if name.endswith("sha256")
                else 0 if "count" in name or "shares" in name
                else name
            )
            for name in columns
        }
        return tuple(values[name] for name in columns)

    conn.executemany(
        "INSERT INTO basket_confirmed_inventory_snapshot("
        + ",".join(columns) + ") VALUES("
        + ",".join("?" for _ in columns) + ")",
        [
            _row("inv_2", "2026-09-09T12:00:00Z", '{"pick": "middle"}'),
            _row("inv_3", "2026-09-09T18:00:00Z", '{"pick": "newest"}'),
            _row("inv_1", "2026-09-09T06:00:00Z", '{"pick": "oldest"}'),
        ],
    )
    rows = conn.execute(READ, ("2026-09-01",)).fetchall()
    assert len(rows) == 1, rows
    assert conn.execute(READ, ("2026-09-10",)).fetchall() == [], (
        "a station-day older than the floor is not read"
    )
    row = rows[0]
    assert row[3] == "2026-09-09T18:00:00Z", row
    assert row[2] == '{"pick": "newest"}', "the payload must be the newest row's"

    # Two snapshots tied on the newest instant still give one row.
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot("
        + ",".join(columns) + ") VALUES("
        + ",".join("?" for _ in columns) + ")",
        _row("inv_4", "2026-09-09T18:00:00Z", '{"pick": "tied-later"}'),
    )
    rows = conn.execute(READ, ("2026-09-01",)).fetchall()
    assert len(rows) == 1, rows
    assert rows[0][2] == '{"pick": "tied-later"}', rows


def test_a_one_bucket_registry_read_takes_that_buckets_newest_refresh_off_the_index():
    """2026-09-18: the planner read every refresh of every held
    station-day's registry (RJTT 2026-09-17: 3,201 rows) before knowing
    whether the day had closed on a held bucket.  It now reads one row: the
    won bucket's newest, walking the station-day index backwards."""

    from bayes_basket_target.certain_win_recycle import _registry_rows
    from bayes_basket_target.market_registry import apply_target_market_registry_schema

    conn = _conn()
    apply_target_market_registry_schema(conn)
    # The rows only need to exist for the read; the snapshot header they
    # reference is not what is under test.
    conn.execute("PRAGMA foreign_keys=OFF")
    columns = [row[1] for row in conn.execute(
        "PRAGMA table_info(basket_target_market_registry)"
    )]

    def _insert(outcome: str, refreshed: str, accepting: int, tick: str) -> None:
        values = {
            name: (
                "RJTT" if name == "station_id"
                else "2026-09-17" if name == "event_date"
                else outcome if name == "outcome"
                else f"tok_{outcome}" if name == "yes_token"
                else accepting if name == "market_accepting_orders"
                else refreshed if name == "refreshed_at"
                else '{"order_price_min_tick_size": "%s"}' % tick if name == "registry_json"
                else f"{outcome}_{refreshed}" if name == "snapshot_sha256"
                else 1 if name in ("source_eligible", "market_active")
                else "awc" if name == "source_lane"
                else "x" * 64 if name == "rules_sha256"
                else name
            )
            for name in columns
        }
        conn.execute(
            "INSERT INTO basket_target_market_registry(" + ",".join(columns)
            + ") VALUES(" + ",".join("?" for _ in columns) + ")",
            tuple(values[name] for name in columns),
        )

    _insert("25°C", "2026-09-17T08:00:00Z", 1, "0.01")
    _insert("26°C", "2026-09-17T08:00:00Z", 1, "0.01")
    _insert("25°C", "2026-09-17T15:30:00Z", 0, "0.001")
    _insert("26°C", "2026-09-17T15:30:00Z", 0, "0.001")

    narrow = _registry_rows(conn, None, station_id="RJTT", event_date="2026-09-17", outcome="25°C")
    wide = _registry_rows(conn, None, station_id="RJTT", event_date="2026-09-17")
    assert list(narrow) == ["25°C"]
    assert narrow["25°C"] == wide["25°C"]
    assert narrow["25°C"]["refreshed_at"] == "2026-09-17T15:30:00Z"
    assert narrow["25°C"]["accepting_orders"] == 0

    plan = "\n".join(str(row[-1]) for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT outcome, yes_token, registry_json,"
        " market_accepting_orders, refreshed_at FROM basket_target_market_registry"
        " WHERE station_id=? AND event_date=? AND outcome=? ORDER BY rowid DESC LIMIT 1",
        ("RJTT", "2026-09-17", "25°C"),
    ))
    assert "basket_target_market_registry_scope_idx" in plan, plan
    assert "TEMP B-TREE" not in plan, plan
