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


@pytest.mark.parametrize("lane", CAPTURE_LANES)
def test_the_loop_budget_leaves_room_under_the_timeout(lane):
    """The loop is not the whole pass.

    Each of these lanes also runs a P1 capture before its own loop, under
    its own budget -- and those are named differently per lane (the CONUS
    one is per PRODUCT and spends twice), so this does not try to sum them.
    What it pins is the part that can be pinned uniformly: the loop's own
    budget has to leave real room, or the two together reach the timeout
    the scheduler kills the pass at, and a killed pass writes nothing while
    having held the exclusive writer domain for the whole of it.
    """

    module = _lane_module(lane)
    budget = getattr(module, "BUDGET_SEC_PER_PASS", None)
    if budget is None:
        pytest.skip(f"{lane} has no loop of its own to bound")
    timeout = _sweep_timeouts()[lane]

    assert float(budget) <= timeout * 0.6, (
        f"{lane}: loop budget {float(budget):.0f}s against a "
        f"{timeout:.0f}s subprocess timeout leaves nothing for the rest "
        "of the pass"
    )


def test_the_msm_lane_specifically_carries_one():
    """Named, because this is the one that did not and the measurement is
    what put it here: 17m00s wall for a successful pass, at 0% CPU."""

    module = _lane_module("capture_jma_msm_forward")
    timeout = _sweep_timeouts()["capture_jma_msm_forward"]

    assert module.BUDGET_SEC_PER_PASS >= 300.0
    assert module.BUDGET_SEC_PER_PASS <= timeout * 0.6
