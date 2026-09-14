"""A sharded lane does a bounded amount and returns; a run count is not a bound.

``regional_point_lane.capture_p1`` learned this on 2026-09-09, after the CONUS
sweep had spent its whole 1,800-second subprocess budget inside one loop and
been ``timeout_killed`` for three days running -- and because the resident
admits one writer at a time, those thirty minutes are thirty minutes in which
the production cycle cannot start.  ``capture_noaa_conus_forward`` was given a
wall-clock bound beside its run count in the same pass.

Its sibling was not.  ``capture_jma_msm_forward`` kept the P1 budget and a run
cap of four, written against "a station-day costs about 86 seconds" -- six
minutes on paper.  Measured 2026-09-10 a successful pass took 17m00s at 0%
CPU, entirely ranged reads off a slow mirror, and the lane's clock read
``timeout_killed`` with its last success seven and a half hours earlier.

So the invariant is checked here rather than left to whoever writes the next
lane: a capture loop with a run cap has a seconds cap too, and the two
budgets a pass can spend have to fit inside the timeout the scheduler will
kill it at.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import weather_basket_yes_daemon as daemon  # noqa: E402


def _sweep_timeouts() -> dict[str, float]:
    """(name, timeout) for every hosted sweep, off the daemon's own table.

    The table, not a regex over the source: several entries carry their
    rationale as a comment INSIDE the tuple, between the paren and the name,
    and a regex anchored on ``("name"`` silently skipped those -- including
    the MSM lane, which is the one this file exists for.  A discovery step
    that quietly returns fewer rows makes every test over it vacuous.
    """

    return {
        str(name): float(timeout)
        for name, _script, _interval, timeout in daemon.HOSTED_SWEEPS
    }


def _lane_module(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_lane_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CAPTURE_LANES = sorted(
    name for name in _sweep_timeouts()
    if name.startswith("capture_") and name.endswith("_forward")
)


def test_the_capture_lanes_are_actually_discovered():
    """A regex that matched nothing would make every test below vacuous."""

    assert len(CAPTURE_LANES) >= 3, CAPTURE_LANES


@pytest.mark.parametrize("lane", CAPTURE_LANES)
def test_a_run_cap_is_accompanied_by_a_seconds_cap(lane):
    module = _lane_module(lane)
    if not hasattr(module, "MAX_RUNS_PER_PASS"):
        pytest.skip(f"{lane} has no loop of its own to bound")

    budget = getattr(module, "BUDGET_SEC_PER_PASS", None)
    assert budget is not None, (
        f"{lane} caps runs but not seconds; the seconds are what the "
        "exclusive writer domain rations"
    )
    assert budget > 0


# What the gap between a lane's budget and its subprocess timeout has to
# cover: whatever can still be RUNNING when the deadline fires.  The deadline
# is checked before work is started, never mid-work, so something is always
# in flight at that moment.
#
# How much depends on how deep the deadline reaches.  ``capture_jma_msm_forward``
# carries it into the fetch itself (``pass_deadline``), so the remainder is ONE
# ranged request -- measured overshoot 2026-09-11: 0.66s on a 900-second
# budget.  ``capture_noaa_conus_forward`` joined it on 2026-09-14, after the bet
# below lost (timeout_killed at 03:15Z).  The other lanes check only between
# runs, so their remainder is a whole run, and their margin is a bet on a run
# finishing rather than a proof that it will.  Ten minutes is stated here as the floor
# under both, and named as the bet it is for the lanes without the deeper
# check.
MIN_TIMEOUT_MARGIN_SEC = 600.0


@pytest.mark.parametrize("lane", CAPTURE_LANES)
def test_the_loop_budget_leaves_room_under_the_timeout(lane):
    """The loop is not the whole pass, and the deadline is not instantaneous.

    A budget that reaches the timeout is a budget that gets the pass killed
    on the last thing it started -- killed, which writes nothing at all and
    holds the exclusive writer slot for the full timeout.
    """

    module = _lane_module(lane)
    budget = getattr(module, "BUDGET_SEC_PER_PASS", None)
    if budget is None:
        pytest.skip(f"{lane} has no loop of its own to bound")
    timeout = _sweep_timeouts()[lane]
    margin = timeout - float(budget)

    assert margin >= MIN_TIMEOUT_MARGIN_SEC, (
        f"{lane}: budget {float(budget):.0f}s under a {timeout:.0f}s timeout "
        f"leaves {margin:.0f}s for whatever is still in flight"
    )


def test_the_msm_lane_specifically_carries_one():
    """Named, because this is the one that did not and the measurements are
    what put it here: 17m00s wall for a successful pass at 0% CPU, then a
    900-second pass that stopped on time and wrote nothing."""

    module = _lane_module("capture_jma_msm_forward")
    timeout = _sweep_timeouts()["capture_jma_msm_forward"]

    assert module.BUDGET_SEC_PER_PASS >= 1200.0
    assert timeout - module.BUDGET_SEC_PER_PASS >= MIN_TIMEOUT_MARGIN_SEC


def test_the_msm_fetches_carry_the_deadline_the_others_only_bound_runs():
    """The distinction the margin rule rests on, pinned so it stays true.

    If the MSM lane ever stops carrying the deadline into its fetches, its
    remainder goes back to being a whole station-day and ten minutes stops
    being a floor anyone measured.
    """

    from bayes_basket_target import jma_msm_archive

    assert hasattr(jma_msm_archive, "pass_deadline")
    assert hasattr(jma_msm_archive, "set_pass_deadline")
    source = (
        ROOT / "scripts" / "capture_jma_msm_forward.py"
    ).read_text(encoding="utf-8")
    assert "set_pass_deadline(BUDGET_SEC_PER_PASS)" in source
