"""The per-station quiescence guard must never scan the ledgers.

The guard runs inside ``materialize_resident_no_live_market_evidence``,
which the evidence pass calls once per station: forty-five times for a
fleet pass, and again for every mid-loop quote refresh.  Measured
2026-09-07, one refresh cost 173.6s and one cycle spent 2083.3s across
twelve of them -- 99.1% of its decision loop -- while the decisions those
refreshes existed to enable took 12.0s.

Two of its five questions could not be answered from an index:

  * ``will_order<>0`` is an inequality on the second column of
    basket_target_decision_blocker_idx, and the station predicate is
    ``(?='' OR station_id=?)`` -- a disjunction with a parameter, which no
    index can seek.  So it scanned all 33,074 rows of a table living in a
    38GB database: 1,065ms warm against production, 1.5-2.1s cold.  Per
    station.
  * The cycles guard is the same shape one table over: ``state IN (?,?,?)``
    over 73,205 rows.

With the two indexes the whole guard is 2.79ms a station, from 1,084ms.

This is the seventh instance of one bug class in this repo -- an unindexed
predicate over a growing append-only ledger, read per scope, inside a path
that holds up a decision.  The others were each found by measuring a
stall.  This test is here so the eighth is found by a red test instead:
it reads the query planner's own answer, not a timing, so it is
deterministic and says which predicate regressed.

The eighth was found by measuring a stall anyway, on 2026-09-08, before
this file was read: the truth-increment probe scanning all 429k rows of
basket_target_market_registry to find eligible station-days without truth,
54.7 seconds in one statement, in a cycle whose decision loop was racing a
quote lease it then lost -- 43 of 44 scopes died on production_quote_expired
behind it.  Its plan is asserted below, so there is no ninth of this shape
in this table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.order_state_producer import (
    _SCOPE_PREDICATES,
    TARGET_CYCLE_BLOCKING_STATES,
    TARGET_DECISION_BLOCKING_STATES,
    TARGET_INTENT_BLOCKING_STATES,
    TARGET_RECONCILIATION_BLOCKING_STATES,
)
from bayes_basket_target.ssot import apply_production_ssot_schema

ROOT = Path(__file__).resolve().parents[1]

LIVE_AUTHORITY_SQL = (
    "SELECT 1 FROM auto_trade_decisions WHERE will_order<>0"
    " AND (?='' OR station_id=?) AND (?='' OR event_date=?) LIMIT 1"
)

BLOCKING_STATES = {
    "basket_target_execution_cycles": TARGET_CYCLE_BLOCKING_STATES,
    "basket_target_order_intents": TARGET_INTENT_BLOCKING_STATES,
    "basket_target_order_reconciliations": TARGET_RECONCILIATION_BLOCKING_STATES,
    "auto_trade_decisions": TARGET_DECISION_BLOCKING_STATES,
}


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    apply_production_ssot_schema(connection)
    return connection


def _plan(connection: sqlite3.Connection, sql: str, args: tuple) -> list[str]:
    return [
        str(row[3]) for row in connection.execute(
            "EXPLAIN QUERY PLAN " + sql, args
        )
    ]


def test_the_live_authority_probe_reads_an_index_not_the_table(
    conn: sqlite3.Connection,
) -> None:
    """The single heaviest statement in the cycle, by the planner's answer.

    Asserted against the plan rather than a clock: a timing test on an
    in-memory table with no rows would pass while production scanned a
    38GB file, which is exactly how this survived six earlier hunts.
    """

    steps = _plan(conn, LIVE_AUTHORITY_SQL, ("", "", "", ""))
    assert steps, "the planner returned no steps"
    assert any(
        "auto_trade_decisions_live_authority_idx" in step for step in steps
    ), steps
    assert not any(
        step.strip() == "SCAN auto_trade_decisions" for step in steps
    ), steps


def test_the_cycles_quiescence_guard_reads_an_index(
    conn: sqlite3.Connection,
) -> None:
    """73,205 rows, three states, none of them present in no-live."""

    states = sorted(TARGET_CYCLE_BLOCKING_STATES)
    sql = _SCOPE_PREDICATES["basket_target_execution_cycles"].format(
        states=",".join("?" for _ in states)
    )
    steps = _plan(conn, sql, (*states, "", "", "", ""))
    assert any(
        "basket_target_execution_cycles_state_scope_idx" in step
        for step in steps
    ), steps
    assert not any(step.strip() == "SCAN c" for step in steps), steps


@pytest.mark.parametrize("table", sorted(_SCOPE_PREDICATES))
def test_no_quiescence_predicate_scans_its_base_table(
    conn: sqlite3.Connection, table: str
) -> None:
    """Every guard, not only the two that were caught.

    A predicate added later inherits the shape that caused this -- scoped
    by ``(?='' OR col=?)`` over an append-only ledger -- so the guard is
    asserted for the whole family rather than for today's two offenders.
    """

    states = sorted(BLOCKING_STATES[table])
    sql = _SCOPE_PREDICATES[table].format(
        states=",".join("?" for _ in states)
    )
    steps = _plan(conn, sql, (*states, "", "", "", ""))
    scans = [
        step for step in steps
        if step.startswith("SCAN") and "USING" not in step
    ]
    assert not scans, f"{table} scans its base table: {steps}"


def test_both_indexes_are_declared_in_the_schema(
    conn: sqlite3.Connection,
) -> None:
    """Created by the schema, so a fresh deployment is not slow until
    someone notices and adds them by hand."""

    for name in (
        "auto_trade_decisions_live_authority_idx",
        "basket_target_execution_cycles_state_scope_idx",
        "basket_target_order_intents_state_cycle_idx",
        "basket_target_order_intents_cycle_idx",
    ):
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone() is not None, name


def test_the_partial_index_still_finds_a_live_order_row(
    conn: sqlite3.Connection,
) -> None:
    """A faster wrong answer is not the goal.

    The index is empty in production_no_live because the column is
    constrained to zero there, which is what makes it free -- but the
    guard exists for the case where a row does carry authority, and that
    row must still be found through it.
    """

    columns = [row[1] for row in conn.execute(
        "PRAGMA table_info(auto_trade_decisions)"
    )]
    values = {
        name: (1 if name == "will_order" else 0) if "order" in name
        else "x" * 64 if name.endswith("sha256")
        else "KORD" if name == "station_id"
        else "2026-09-07" if name == "event_date"
        else "basket" if name == "strategy_lane"
        else "prepared" if name == "decided_state"
        else name
        for name in columns
    }
    values["will_order"] = 1
    conn.execute(
        "INSERT INTO auto_trade_decisions(" + ",".join(columns) + ")"
        " VALUES(" + ",".join("?" for _ in columns) + ")",
        tuple(values[name] for name in columns),
    )
    assert conn.execute(
        LIVE_AUTHORITY_SQL, ("KORD", "KORD", "2026-09-07", "2026-09-07")
    ).fetchone() is not None
    # ...and stays scoped: another station's day must not see it.
    assert conn.execute(
        LIVE_AUTHORITY_SQL, ("KJFK", "KJFK", "2026-09-07", "2026-09-07")
    ).fetchone() is None


# The settlement query, lifted from its call site rather than retyped, so a
# change there cannot leave this test asserting a plan nobody runs.
def _settle_live_cycles_sql() -> str:
    import re

    body = (ROOT / "bayes_basket_target" / "production_worker.py").read_text()
    start = body.index("SELECT c.cycle_id, c.station_id, c.event_date")
    match = re.search(
        r"rows = self\.commit_conn\.execute\(\s*(.*?)\n\s*\)\.fetchall\(\)",
        body[start - 200:start + 3000],
        re.S,
    )
    assert match, "the settlement query moved; this test has to follow it"
    return "".join(re.findall(r'"([^"]*)"', match.group(1))).replace(
        "LIMIT ?", "LIMIT 200"
    )


def test_live_cycle_settlement_seeks_its_intents_by_cycle(
    conn: sqlite3.Connection,
) -> None:
    """Four correlated subqueries, one cycle at a time.

    Two of them carry no state predicate, so the state-first index cannot
    serve them and they scanned the whole intents table once per candidate
    cycle.  Measured 2026-09-07: 56,577 cycles in ``prepared``, about 436
    million row visits, 112.1s against production to return one row -- the
    largest stage in the cycle at the time, 139.1s of 400.8s.

    A timing test would pass on this empty in-memory schema while
    production scanned for two minutes, so the planner's answer is what is
    asserted.
    """

    steps = _plan(conn, _settle_live_cycles_sql(), ())
    scans = [
        step for step in steps
        if step.startswith("SCAN") and "USING" not in step
    ]
    assert not scans, f"settlement scans a base table: {steps}"
    assert any("basket_target_order_intents_cycle_idx" in s for s in steps), steps


TRUTH_INCREMENT_PROBE_SQL = (
    "SELECT r.station_id, r.event_date"
    " FROM (SELECT DISTINCT station_id, event_date"
    "       FROM basket_target_market_registry"
    "       WHERE source_eligible=1 AND event_date>=?) r"
    " LEFT JOIN (SELECT DISTINCT station_id, local_date"
    "       FROM calibration_station_daily_high_truth"
    "       WHERE qc_status='ACCEPTED') t"
    "  ON t.station_id=r.station_id"
    " AND t.local_date=r.event_date"
    " WHERE t.station_id IS NULL"
    " ORDER BY r.event_date, r.station_id LIMIT ?"
)


@pytest.fixture()
def registry_conn() -> sqlite3.Connection:
    """The registry lives in its own schema module, applied beside the SSOT."""

    from bayes_basket_target.market_registry import (  # noqa: PLC0415
        apply_target_market_registry_schema,
    )

    connection = sqlite3.connect(":memory:")
    apply_production_ssot_schema(connection)
    apply_target_market_registry_schema(connection)
    return connection


def test_the_truth_increment_probe_seeks_the_day_it_asks_about(
    registry_conn: sqlite3.Connection,
) -> None:
    """The eighth instance: two unseekable predicates, 429k rows, per cycle.

    ``source_eligible=1`` is on no index at all, and the scope index leads
    with the station while this read knows only a date -- so the planner
    scanned the whole table to build the DISTINCT.  Day-first and partial on
    the eligible rows makes the date a seek and covers both columns.

    Asserted on the plan, not a clock: an in-memory table with no rows is
    fast whatever the planner does, which is how the previous seven of these
    survived until somebody measured production.
    """

    steps = _plan(registry_conn, TRUTH_INCREMENT_PROBE_SQL, ("2026-08-25", 40))
    assert steps, "the planner returned no steps"
    registry_steps = [
        step for step in steps
        if "basket_target_market_registry" in step
    ]
    assert registry_steps, steps
    assert all("SCAN" not in step for step in registry_steps), registry_steps
    assert any(
        "basket_target_market_registry_eligible_day_idx" in step
        for step in registry_steps
    ), registry_steps
