"""Charter 124: the worst claim inside a relative-entropy ball.

The proposal asked for ``min over p in P`` of the objective.  Every quantity
this system gates and sizes on is monotone increasing in the mass the served
partition puts inside the seated window, so that minimisation collapses to
one scalar -- the smallest inside-mass the ball allows -- and one function
answers for the gate, the stake, the curve and the growth test alike.

Landed on the shadow arm: the reading is computed on every enumerated window
and nothing reads it back, so what accumulates is "which seats would this
have refused" against how those seats actually settled.
"""

from __future__ import annotations

import math

import pytest

from bayes_basket_target import delta_v2_runtime
from bayes_basket_target.basket_kernel.contracts import ContractError
from bayes_basket_target.basket_kernel.delta_v2 import robust_window_claim_v1
from bayes_basket_target.delta_v2_runtime import compile_delta_v2_runtime_plan

from tests.charter_143_switch import set_charter_143
from tests.test_bayes_basket_target_delta_v2_runtime import _delta_policy
from tests.test_charter_84_ladder_balance import (
    _basket_policy,
    _fill_resolver,
    _held_snapshot,
)


def _kl(q: float, p: float) -> float:
    return q * math.log(q / p) + (1.0 - q) * math.log((1.0 - q) / (1.0 - p))


def test_the_worst_case_sits_on_the_ball_and_only_ever_lowers() -> None:
    # Zero budget is the identity, which is what makes the off arm exact.
    assert robust_window_claim_v1(0.6, divergence_budget=0.0) == (0.6, 1.0)

    for claim in (0.15, 0.4, 0.62, 0.88):
        for eta in (0.005, 0.02, 0.1):
            worst, factor = robust_window_claim_v1(
                claim, divergence_budget=eta
            )
            # It is a minimisation: never above the served claim.
            assert 0.0 <= worst <= claim
            assert factor == pytest.approx(worst / claim, rel=1e-9)
            # And it is ON the ball, not inside it -- a worst case strictly
            # inside the constraint set is not the worst case.
            assert _kl(worst, claim) == pytest.approx(eta, rel=1e-6)

    # Monotone in the budget: a wider ambiguity set cannot be less
    # conservative.
    tight, _ = robust_window_claim_v1(0.7, divergence_budget=0.01)
    wide, _ = robust_window_claim_v1(0.7, divergence_budget=0.05)
    assert wide < tight < 0.7

    # A ball big enough to reach zero says the window is unbuyable under that
    # budget.  That is an answer, not a convergence failure.
    assert robust_window_claim_v1(0.3, divergence_budget=5.0) == (0.0, 0.0)

    with pytest.raises(ContractError):
        robust_window_claim_v1(0.5, divergence_budget=-0.1)


def test_the_shadow_arm_records_a_reading_and_decides_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        delta_v2_runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )

    def compile_at(mode: str):
        monkeypatch.setattr(
            delta_v2_runtime, "CHARTER_124_ROBUST_CLAIM", mode
        )
        return compile_delta_v2_runtime_plan(
            snapshot=_held_snapshot(),
            basket_policy=_basket_policy(),
            delta_policy=_delta_policy(),
            fill_resolver=_fill_resolver,
            capital_available_usdc=10.0,
            minimum_profit_floor=-5.0,
            resource_probe_ok=True,
            book_equity_usdc=514.29,
        )

    off = compile_at("off")
    shadow = compile_at("shadow")

    seated_off = off["basket_support_selection"]["seated"]
    seated_shadow = shadow["basket_support_selection"]["seated"]

    # The reading exists only on the shadow arm...
    assert off["basket_support_selection"]["robust_claim_mode"] == "off"
    assert shadow["basket_support_selection"]["robust_claim_mode"] == "shadow"

    # ...and it decides nothing: same seat, same belief, same size, same
    # verdict.  This is the property that makes it safe to leave on by
    # default while the evidence accumulates.
    assert seated_shadow["buckets"] == seated_off["buckets"]
    assert seated_shadow["effective_win_probability"] == pytest.approx(
        seated_off["effective_win_probability"]
    )
    assert shadow["basket_target_activated"] is off["basket_target_activated"]
    assert (
        shadow["target_solution"]["target_shares_by_bucket"]
        == off["target_solution"]["target_shares_by_bucket"]
    )


# Charter 230 retired the lambda / second-belief arm this test pinned.


