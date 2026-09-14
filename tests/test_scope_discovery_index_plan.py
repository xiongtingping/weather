"""Scope discovery reduced 422k rows to 4k through a temp B-tree, twelve times a cycle.

``_decision_scopes`` asks

    SELECT DISTINCT station_id,event_date,settlement_timezone,stage
      FROM basket_probability_envelope WHERE state='COMMITTED'

and every row in that table is COMMITTED, so the leading column of the
publisher index selects the whole table.  The plan read every covering-index
entry and then built a TEMP B-TREE to fold them down.  Measured warm against
the live SSOT on 2026-09-11: 4.1-4.5s a call, and ``listing_entry_scopes``
called it twelve times inside one 595-second cycle -- about 49 seconds of the
cycle in this one query, while the resident's single writer slot was held and
the forecast and sweep lanes waited.

An index leading with the DISTINCT key, in the SELECT's own order, lets the
scan emit distinct rows as it goes.  What is pinned here is that: the PLAN,
not a duration.  A timing test on a 38GB file measures the page cache.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.ssot import (  # noqa: E402
    apply_production_ssot_schema,
)

SCOPE_DISCOVERY_SQL = (
    "SELECT DISTINCT station_id,event_date,settlement_timezone,stage"
    " FROM basket_probability_envelope"
    " WHERE station_id<>'' AND event_date<>'' AND state='COMMITTED'"
)


def _seeded(tmp_path: Path) -> sqlite3.Connection:
    """A table big enough that the planner has a reason to use an index."""

    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    columns = [
        row[1] for row in conn.execute(
            "PRAGMA table_info(basket_probability_envelope)"
        )
    ]
    required = [
        row[1] for row in conn.execute(
            "PRAGMA table_info(basket_probability_envelope)"
        ) if row[3] and row[4] is None
    ]
    rows = []
    for index in range(4000):
        value = {
            "envelope_id": f"env-{index:06d}",
            "station_id": f"K{index % 45:02d}",
            "event_date": f"2026-09-{(index % 20) + 1:02d}",
            "settlement_timezone": "UTC",
            "stage": "P0",
            "state": "COMMITTED",
        }
        rows.append(tuple(
            value.get(name) if name in value
            else ("" if name in required or name in columns else None)
            for name in columns
        ))
    conn.executemany(
        f"INSERT OR IGNORE INTO basket_probability_envelope"
        f" VALUES({','.join('?' for _ in columns)})",
        rows,
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    return conn


def _plan(conn: sqlite3.Connection, sql: str) -> str:
    return "\n".join(
        str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
    )


def test_scope_discovery_does_not_build_a_temp_btree(tmp_path):
    conn = _seeded(tmp_path)
    plan = _plan(conn, SCOPE_DISCOVERY_SQL)

    assert "TEMP B-TREE" not in plan.upper(), plan
    assert "basket_probability_envelope_scope_distinct_idx" in plan, plan


def test_the_index_is_declared_in_the_schema_this_repo_applies(tmp_path):
    """Not in a migration script somebody has to remember to run."""

    conn = _seeded(tmp_path)
    names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='basket_probability_envelope'"
        )
    }

    assert "basket_probability_envelope_scope_distinct_idx" in names


def test_the_worker_still_asks_the_question_the_index_is_shaped_for():
    """The index leads with these four columns IN THIS ORDER.

    Reorder the SELECT and the plan silently goes back to a temp B-tree --
    silently, because the query keeps returning exactly the right answer.
    """

    from bayes_basket_target.production_worker import (
        DECISION_SCOPE_PROBABILITY_SQL,
    )

    assert DECISION_SCOPE_PROBABILITY_SQL.format(
        stage_projection="stage"
    ).startswith(
        "SELECT DISTINCT station_id,event_date,settlement_timezone,stage"
    ), "scope discovery no longer selects the columns the index leads with"
