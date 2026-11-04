"""Maintenance keeps the writer and the watchdog; it gives up the clock.

Three stages in the cycle are not inputs to the current action, and each
says so itself: closed-day truth increment, the native-ensemble OOS sweep
(its own receipt reports ``isolated_from_probability_and_execution``), and
the delta_v2 settlement truth/labels (docstring: "closed-day truth/label
work outside the current quote lease").  Every previous fix moved them
later in the cycle.  That fixes the order and not the arithmetic: the
decision path compiles against a book with ``max_quote_age_seconds`` of
life, so what it races is the length of the whole cycle, not its own
position in it.  Measured 2026-09-07: 1436.6s of stages ran before the
books were read at all, and 86 of 90 scopes then died on
``production_quote_expired``.

They stay in the cycle -- one writer, one heartbeat, one watchdog, which is
why they were brought in -- and give up only the right to take unbounded
time from a decision.  The risk that buys is starvation, which is the
failure mode this repo has hit before (the listing turn, the TAF rotation),
so the budget carries an override and the turn is persisted across
restarts.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bayes_basket_target.production_worker import (
    MAINTENANCE_BUDGET_SEC,
    MAINTENANCE_STAGES,
    MAINTENANCE_STARVATION_SEC,
    TargetProductionWorker,
)


def _worker(tmp_path: Path) -> TargetProductionWorker:
    db = tmp_path / "target-read.db"
    if not db.exists():
        with sqlite3.connect(db) as source:
            source.execute("CREATE TABLE marker(value TEXT)")
    runtime = tmp_path / "nwp"
    runtime.mkdir(exist_ok=True)
    return TargetProductionWorker(db, commit_conn=None, nwp_runtime_root=runtime)


def _stamp(seconds_ago: float) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _seed_turn(
    worker: TargetProductionWorker,
    ages: dict[str, float],
    costs: dict[str, float] | None = None,
) -> None:
    worker._maintenance_turn_path().write_text(json.dumps({
        "schema": "bayes_basket_target_maintenance_lane_turn_v1",
        "served_at_utc": {
            stage: _stamp(age) for stage, age in ages.items()
        },
        "last_elapsed_sec": dict(costs or {}),
    }))
    # Both caches are loaded together, so both are dropped together.
    worker._maintenance_turn_cache = None
    worker._maintenance_cost_cache = None


def test_a_worker_that_has_never_run_serves_every_stage(tmp_path: Path) -> None:
    """A fresh process must not defer work it has no record of doing."""

    worker = _worker(tmp_path)
    worker._maintenance_spent_sec = 0.0
    worker._maintenance_admissions = {}
    for stage in MAINTENANCE_STAGES:
        assert worker._maintenance_admits(stage), stage
        # Charged as it goes, so a later stage sees what the earlier spent.
        worker._record_maintenance_served(stage, 0.0)


def test_the_budget_defers_the_stage_that_would_overrun_it(tmp_path: Path) -> None:
    """One spent allowance, and the rest of the pass waits for a cycle."""

    worker = _worker(tmp_path)
    worker._maintenance_spent_sec = 0.0
    worker._maintenance_admissions = {}
    _seed_turn(
        worker,
        {stage: 10.0 for stage in MAINTENANCE_STAGES},
        {stage: 1.0 for stage in MAINTENANCE_STAGES},
    )

    first = MAINTENANCE_STAGES[0]
    assert worker._maintenance_admits(first)
    worker._record_maintenance_served(first, MAINTENANCE_BUDGET_SEC + 1.0)

    for stage in MAINTENANCE_STAGES[1:]:
        assert not worker._maintenance_admits(stage), stage
        receipt = worker._maintenance_deferred(stage)
        state = receipt["state"]
        # The board reads lane state by substring.  A deferred pass is the
        # lane working as designed and must not render as an outage.
        assert "BLOCK" not in state.upper()
        assert "FAIL" not in state.upper()
        assert receipt["orders_submitted"] == 0
        assert receipt["calls_order_gateway"] is False
        assert receipt["unserved_seconds"] is not None


def test_starvation_overrides_a_spent_budget(tmp_path: Path) -> None:
    """A lane never served is a worse failure than one long cycle."""

    worker = _worker(tmp_path)
    worker._maintenance_spent_sec = MAINTENANCE_BUDGET_SEC * 10
    worker._maintenance_admissions = {}
    starved, fresh = MAINTENANCE_STAGES[0], MAINTENANCE_STAGES[1]
    _seed_turn(
        worker,
        {starved: MAINTENANCE_STARVATION_SEC + 60.0, fresh: 5.0},
        {starved: 1.0, fresh: 1.0},
    )
    assert worker._maintenance_admits(starved)
    assert not worker._maintenance_admits(fresh)
    assert worker._maintenance_lane_report()["stages"][starved][
        "admitted"
    ] == "starved"


def test_the_turn_survives_a_restart(tmp_path: Path) -> None:
    """Held in memory it would reseed on every deploy; there were nineteen
    restarts on 2026-09-02, so the tail of a fixed order would never run."""

    first = _worker(tmp_path)
    first._maintenance_spent_sec = 0.0
    first._maintenance_admissions = {}
    stage = MAINTENANCE_STAGES[0]
    first._record_maintenance_served(stage, 1.0)

    # A new process, same runtime root.
    second = _worker(tmp_path)
    second._maintenance_spent_sec = MAINTENANCE_BUDGET_SEC * 10
    second._maintenance_admissions = {}
    assert not second._maintenance_admits(stage), (
        "a restart must not hand a just-served stage a fresh turn"
    )
    unserved = second._maintenance_unserved_seconds(stage, now_utc=_stamp(0.0))
    assert unserved is not None and unserved < 60.0


def test_an_unbudgeted_stage_is_never_gated(tmp_path: Path) -> None:
    """Only the named stages are maintenance.  Everything else is a
    decision input and must not be deferrable by accident."""

    worker = _worker(tmp_path)
    worker._maintenance_spent_sec = MAINTENANCE_BUDGET_SEC * 10
    worker._maintenance_admissions = {}
    for stage in ("market_ingress", "decision_loop", "intent_reconcile"):
        assert stage not in MAINTENANCE_STAGES
        assert worker._maintenance_admits(stage)


def test_every_named_stage_is_actually_gated_in_the_cycle() -> None:
    """A stage in the list that no call site asks about is a lie the
    receipt tells: the lane reports a budget nobody spends."""

    # Wrapped call sites are the norm at this indentation, so compare with
    # whitespace collapsed rather than writing the assertion to match one
    # particular line break.
    source = " ".join(
        (
            inspect.getsource(TargetProductionWorker._run_stages)
            # The settlement lane's gate lives in its own turn.
            + inspect.getsource(TargetProductionWorker._serve_delta_v2_settlement)
        ).split()
    )
    for stage in MAINTENANCE_STAGES:
        assert f'self._maintenance_admits("{stage}")' in source, stage
        assert f'self._maintenance_deferred( "{stage}" )' in source or (
            f'self._maintenance_deferred("{stage}")' in source
        ), stage
        assert f'self._record_maintenance_served( "{stage}",' in source, stage


def test_the_lane_reports_itself_on_every_return_path(tmp_path: Path) -> None:
    """Including a cycle that fails closed -- which is exactly when someone
    asks what else it skipped."""

    worker = _worker(tmp_path)
    worker._run_stages = lambda: {"ok": False, "state": "blocked_fail_closed"}
    report = worker.run()["maintenance_lane"]
    assert report["budget_seconds"] == MAINTENANCE_BUDGET_SEC
    assert set(report["stages"]) == set(MAINTENANCE_STAGES)


def test_a_budget_refusal_is_not_relabelled_as_the_interval_throttle() -> None:
    """"throttled" says the OOS sweep had nothing new to do; "deferred"
    says it did and the cycle could not afford it.  Opposite fixes."""

    source = inspect.getsource(TargetProductionWorker._run_stages)
    assert "elif oos_admitted:" in source, (
        "a plain else would overwrite the deferred receipt with throttled"
    )


def test_a_stage_costing_more_than_the_whole_budget_is_not_admitted_by_it(
    tmp_path: Path,
) -> None:
    """Checking only the running total admits a stage that costs three
    times the allowance, because at the moment it is asked the total is
    still zero.  Then the budget is decorative and the overrun returns."""

    worker = _worker(tmp_path)
    worker._maintenance_spent_sec = 0.0
    worker._maintenance_admissions = {}
    stage = MAINTENANCE_STAGES[0]
    _seed_turn(worker, {stage: 10.0}, {stage: MAINTENANCE_BUDGET_SEC * 3})
    assert not worker._maintenance_admits(stage)
    # It is not dropped, only paced: the starvation arm still owns it.
    _seed_turn(
        worker,
        {stage: MAINTENANCE_STARVATION_SEC + 1.0},
        {stage: MAINTENANCE_BUDGET_SEC * 3},
    )
    assert worker._maintenance_admits(stage)


def test_a_measured_cost_survives_a_restart(tmp_path: Path) -> None:
    """Otherwise the first cycle after every deploy re-learns each cost by
    paying it, and deploys are frequent enough that it never stops."""

    first = _worker(tmp_path)
    first._maintenance_spent_sec = 0.0
    first._maintenance_admissions = {}
    stage = MAINTENANCE_STAGES[0]
    first._record_maintenance_served(stage, 42.5)

    second = _worker(tmp_path)
    assert second._maintenance_last_cost(stage) == 42.5
