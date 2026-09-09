"""Two refusals that could not say why, and now can.

**The funnel.**  ``_support_window_error`` puts the window funnel on the
solver's exception; ``_compile_target_plan`` absorbs that exception into a
blocked plan and moves the funnel to ``delta_v2_audit``; and the worker then
minted a *fresh* ``ContractError`` from the plan's first reason code before
the blocked-scope recorder read the funnel off it with ``getattr``.  So the
recorder found nothing.  Measured 2026-09-09: 0 of 1,808 blocked-scope rows
since 09-01 carried a funnel, including all 254 ``delta_v2_support_empty``
ones -- while the two tests guarding it asserted the source text of the
raise and of the recorder, both of which were correct.  The loss was the hop
between them.

**The verdict.**  Whether a station-day's book is recorded at all is decided
by ``probability_evidence_fresh`` and ``scope_blocker`` in the WS scope
catalog.  That verdict lived in a manifest rewritten every two seconds and a
status file overwritten every cycle, so ZUUU|2026-09-09's 21.6-hour bookless
stretch -- covering the first 13.2 hours of its own event day -- left no
record of what stopped it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.production_worker import (
    TargetProductionWorker,
    _blocked_plan_error,
    _window_funnel_from,
)
from bayes_basket_target.ssot import PRODUCTION_LEDGER_DDL


FUNNEL = {
    "enumerated": 9,
    "shortlisted": 0,
    "refused": {"center": 2, "skew": 3, "edge": 4},
    "last_refusing_gate": "edge",
}
FAILURES = [{"buckets": ["27°C", "28°C"], "reason": "ContractError:x"}]


def _blocked_candidate(**audit):
    return {
        "status": "blocked",
        "reason_codes": ["delta_v2_support_empty"],
        "probability_envelope": {"stage": "P1"},
        "delta_v2_audit": {
            "schema": "delta_basket_score_authorization_v2",
            "authorized": False,
            "blockers": ["delta_v2_support_empty"],
            **audit,
        },
    }


# -------------------------------------------------------------- the funnel


def test_the_reraised_blocked_plan_keeps_the_funnel_the_plan_holds() -> None:
    worker = object.__new__(TargetProductionWorker)
    candidate = _blocked_candidate(
        window_gate_counts=FUNNEL, window_failures=FAILURES
    )

    with pytest.raises(ContractError) as caught:
        worker._commit_current_stage_no_execution_candidate(
            candidate,
            station_id="ZUUU",
            event_date="2026-09-09",
            created_at_utc="2026-09-09T05:23:22.307Z",
        )

    # The message stays the bare reason -- every consumer compares by string.
    assert str(caught.value) == "delta_v2_support_empty"
    assert caught.value.window_gate_counts == FUNNEL
    assert caught.value.window_failures == FAILURES


def test_a_blocked_plan_with_no_funnel_reraises_exactly_as_before() -> None:
    worker = object.__new__(TargetProductionWorker)

    with pytest.raises(ContractError) as caught:
        worker._commit_current_stage_no_execution_candidate(
            _blocked_candidate(),
            station_id="ZUUU",
            event_date="2026-09-09",
            created_at_utc="2026-09-09T05:23:22.307Z",
        )

    assert str(caught.value) == "delta_v2_support_empty"
    assert not hasattr(caught.value, "window_gate_counts")


def test_a_blocked_plan_with_no_reason_codes_still_names_itself() -> None:
    worker = object.__new__(TargetProductionWorker)
    with pytest.raises(ContractError, match="^compiled_plan_blocked$"):
        worker._commit_current_stage_no_execution_candidate(
            {"status": "blocked", "probability_envelope": {"stage": "P0"}},
            station_id="ZUUU",
            event_date="2026-09-09",
            created_at_utc="2026-09-09T05:23:22.307Z",
        )


def test_the_recorder_reads_the_exception_first_and_the_plan_second() -> None:
    # Belt and braces: a future re-raise that forgets to move the funnel
    # costs a hop's provenance, not the whole diagnosis.
    bare = ContractError("delta_v2_support_empty")
    counts, failures = _window_funnel_from(
        bare,
        candidate=_blocked_candidate(
            window_gate_counts=FUNNEL, window_failures=FAILURES
        ),
    )
    assert counts == FUNNEL and failures == FAILURES

    carried = _blocked_plan_error(
        "delta_v2_support_empty",
        candidate=_blocked_candidate(window_gate_counts={"enumerated": 1}),
    )
    counts, failures = _window_funnel_from(carried, candidate=None)
    assert counts == {"enumerated": 1} and failures is None

    assert _window_funnel_from(bare, candidate=None) == (None, None)


def test_the_absorbed_hold_puts_both_halves_on_the_plan() -> None:
    # ``delta_v2_no_solvable_support_window`` is the one refusal whose counts
    # say nothing -- every window was admissible -- so a plan carrying only
    # the counts describes it as an empty funnel.
    runtime = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "bayes_basket_target" / "runtime.py"
    ).read_text(encoding="utf-8")
    assert 'window_failures=getattr(exc, "window_failures", None),' in runtime
    assert 'delta_audit["window_failures"] = [' in runtime


# ------------------------------------------------------------- the verdict


def _worker_with_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(PRODUCTION_LEDGER_DDL)
    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    return worker, conn


def _scope(station, *, fresh=True, blocker="", state="open", stage="P1"):
    return {
        "scope_key": f"{station}|2026-09-09",
        "station_id": station,
        "event_date": "2026-09-09",
        "event_day_state": state,
        "probability_stage": stage,
        "probability_evidence_fresh": fresh,
        "scope_blocker": blocker,
    }


def _row(conn, station):
    return conn.execute(
        "SELECT actionable,scope_blocker,state_since_utc,"
        "last_actionable_at_utc,transition_count,recent_transitions_json"
        " FROM basket_target_ws_scope_serving_state"
        " WHERE station_id=? AND event_date='2026-09-09'",
        (station,),
    ).fetchone()


def test_an_unserved_scope_says_since_when_and_what_stopped_it() -> None:
    worker, conn = _worker_with_db()

    # Two cycles served, then the verdict flips.
    worker._record_ws_scope_serving_state(
        [_scope("ZUUU"), _scope("ZGSZ")],
        as_of_utc="2026-09-08T07:00:00Z",
    )
    worker._record_ws_scope_serving_state(
        [_scope("ZUUU"), _scope("ZGSZ")],
        as_of_utc="2026-09-08T07:36:41Z",
    )
    result = worker._record_ws_scope_serving_state(
        [_scope("ZUUU", fresh=False), _scope("ZGSZ")],
        as_of_utc="2026-09-08T07:50:58Z",
    )

    actionable, blocker, since, last_ok, count, ring = _row(conn, "ZUUU")
    assert actionable == 0
    # No blocker string: the publisher simply read the probability as stale,
    # which is exactly the case ZUUU 2026-09-09 could not be told apart from.
    assert blocker == ""
    assert since == "2026-09-08T07:50:58Z"
    assert last_ok == "2026-09-08T07:36:41Z"
    assert count == 2
    assert json.loads(ring)[-1]["actionable"] is False
    assert result["unserved_scope_count"] == 1
    assert result["longest_unserved"][0]["scope_key"] == "ZUUU|2026-09-09"

    # The healthy scope's "still served" stamp keeps moving, and its verdict
    # never changed, so its own since-stamp does not.
    served_actionable, _b, served_since, served_last, served_count, _r = _row(
        conn, "ZGSZ"
    )
    assert served_actionable == 1
    assert served_since == "2026-09-08T07:00:00Z"
    assert served_last == "2026-09-08T07:50:58Z"
    assert served_count == 1


def test_a_repeated_unserved_verdict_does_not_restart_the_clock() -> None:
    # The whole point of the row: thirteen hours of the same refusal must
    # read as one thirteen-hour outage, not as a fresh one each cycle.
    worker, conn = _worker_with_db()
    worker._record_ws_scope_serving_state(
        [_scope("ZUUU")], as_of_utc="2026-09-08T07:36:41Z"
    )
    worker._record_ws_scope_serving_state(
        [_scope("ZUUU", blocker="resident_target_ws_open_scope_rootless")],
        as_of_utc="2026-09-08T16:00:00Z",
    )
    for hour in range(17, 24):
        result = worker._record_ws_scope_serving_state(
            [_scope("ZUUU", blocker="resident_target_ws_open_scope_rootless")],
            as_of_utc=f"2026-09-08T{hour}:00:00Z",
        )

    actionable, blocker, since, last_ok, count, _ring = _row(conn, "ZUUU")
    assert actionable == 0
    assert blocker == "resident_target_ws_open_scope_rootless"
    assert since == "2026-09-08T16:00:00Z"
    assert last_ok == "2026-09-08T07:36:41Z"
    assert count == 2
    assert result["written"] == 0
    assert result["longest_unserved"][0]["since"] == "2026-09-08T16:00:00Z"


def test_the_transition_ring_is_bounded() -> None:
    worker, conn = _worker_with_db()
    for index in range(30):
        worker._record_ws_scope_serving_state(
            [_scope("ZUUU", fresh=bool(index % 2))],
            as_of_utc=f"2026-09-09T{index // 2:02d}:{(index % 2) * 30:02d}:00Z",
        )
    _a, _b, _s, _l, count, ring = _row(conn, "ZUUU")
    rows = json.loads(ring)

    assert count == 30
    assert len(rows) == TargetProductionWorker.WS_SCOPE_SERVING_TRANSITIONS_KEPT


def test_bookkeeping_never_takes_the_cycle_down() -> None:
    # A diagnostic that can stop a decision cycle is worse than the
    # blindness it removes.
    worker, conn = _worker_with_db()
    conn.execute("DROP TABLE basket_target_ws_scope_serving_state")
    result = worker._record_ws_scope_serving_state(
        [_scope("ZUUU")], as_of_utc="2026-09-09T05:12:59Z"
    )
    assert result["state"] == "error"

    worker.commit_conn = None
    assert worker._record_ws_scope_serving_state(
        [_scope("ZUUU")], as_of_utc="2026-09-09T05:12:59Z"
    )["state"] == "not_attempted"


def test_the_table_is_bounded_by_the_scope_count_not_the_cycle_rate() -> None:
    worker, conn = _worker_with_db()
    scopes = [_scope(f"Z{index:03d}") for index in range(45)]
    for index in range(50):
        worker._record_ws_scope_serving_state(
            scopes, as_of_utc=f"2026-09-09T{index % 24:02d}:{index % 60:02d}:00Z"
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_ws_scope_serving_state"
    ).fetchone()[0] == 45
