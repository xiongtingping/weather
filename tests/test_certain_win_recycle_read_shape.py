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
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bayes_basket_target.ssot import apply_production_ssot_schema

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "bayes_basket_target" / "certain_win_recycle.py").read_text()

READ = (
    "SELECT station_id, event_date, inventory_json, MAX(observed_at)"
    " FROM basket_confirmed_inventory_snapshot"
    " WHERE complete=1"
    " GROUP BY station_id, event_date"
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    return conn


def _plan(conn: sqlite3.Connection, sql: str) -> str:
    return "\n".join(str(row[-1]) for row in conn.execute(
        "EXPLAIN QUERY PLAN " + sql
    ))


def test_the_inventory_read_is_a_seek_not_a_scan():
    plan = _plan(_conn(), READ)
    assert "basket_confirmed_inventory_snapshot_recycle_idx" in plan, plan
    assert "SCAN" not in plan, plan


def test_the_self_join_shape_does_not_come_back():
    """The co-routine plus per-group seek this replaced.

    Its result was identical -- verified on the live store, 1,579 scopes both
    ways, the same scope set, no scope tied on observed_at -- so the rewrite
    is safe; what it is not is fast on its own.  Measured back to back the
    two forms were 93.0s and 82.4s; the index is what makes it a seek.
    """

    assert "MAX(observed_at) AS latest" not in SOURCE
    assert "GROUP BY station_id, event_date" in SOURCE


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
    row = conn.execute(READ).fetchone()
    assert row[3] == "2026-09-09T18:00:00Z", row
    assert row[2] == '{"pick": "newest"}', "the payload must be the newest row's"
