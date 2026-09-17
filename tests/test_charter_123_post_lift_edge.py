"""Charter 123: the whole-basket edge gate re-read on the lifted targets.

Charters 78/87/92 admit a window on the increment's expected payout over the
seated lambda*p partition, less the cash it spends, and they read the
SOLVER's shape.  Charters 70/83 then raise rungs to the venue's lot and its
dollar minimum, which only ever adds cost.  So a basket can clear the gate at
the size it was solved to and be under water at the size that goes out --
which is the same defect charter 93 closed for the floor and the maker bundle
re-score closed for execution sizing, at a third point.
"""

from __future__ import annotations

import dataclasses

import pytest

from bayes_basket_target import delta_v2_runtime
from bayes_basket_target.basket_kernel.delta_v2 import (
    whole_basket_edge_delta_v1,
)
from bayes_basket_target.delta_v2_runtime import compile_delta_v2_runtime_plan

from tests.charter_143_switch import set_charter_143
from tests.test_bayes_basket_target_delta_v2_runtime import _delta_policy
from tests.test_charter_84_ladder_balance import (
    _basket_policy,
    _fill_resolver,
    _held_snapshot,
)


# The two readings below exist only while the clause is on.  Skipping rather
# than failing under ``off`` is the point: a clause with a rollback arm whose
# tests go red on that arm makes the rollback unusable, which is the same
# landmine as a direction test that flips with the dial it tests.
pytestmark = pytest.mark.skipif(
    delta_v2_runtime.CHARTER_123_POST_LIFT_EDGE != "on",
    reason="charter 123 is switched off in this environment",
)


def _compile(**overrides):
    return compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=overrides.pop("delta_policy", None) or _delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
        **overrides,
    )


@pytest.fixture(autouse=True)
def _pin_tilt_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same pin as the charter-84 tests: these fixtures are ``sharp`` shapes
    # and the tilt trigger moved to ``stable`` alone on 2026-09-08.  A test of
    # the post-lift edge must not re-break every time the lamp ruling moves.
    monkeypatch.setattr(
        delta_v2_runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )


def test_the_reading_is_taken_on_the_lifted_targets_not_the_solved_ones() -> None:
    result = _compile()

    assert result["basket_target_activated"] is True
    assert result["post_lift_edge_rule"] == "whole_basket_edge_after_lift_v1"
    edge = result["whole_basket_edge_after_lift"]
    assert edge is not None
    # An activated basket cleared it, and by the policy's own epsilon rather
    # than by a zero this clause invented for itself.
    assert edge > result["post_lift_edge_epsilon"]
    assert result["post_lift_edge_epsilon"] == pytest.approx(
        _delta_policy().execution_weights.epsilon_score
    )
    assert "support_edge_after_lift" not in result["reason_codes"]

    # The point of the clause: a rung WAS lifted on this fixture, so the
    # number the gate now reads is not the number the solver was judged on.
    # Recomputing on the solved targets -- what the admission gate saw -- must
    # give a different answer, or this test would pass on a plan where the
    # lift changed nothing and prove none of it.
    assert result["lot_raised_buckets"] or result["notional_raised_buckets"]
    solution = result["target_solution"]
    snapshot = _held_snapshot()
    by_bucket = {row.bucket: row for row in snapshot.buckets}
    support = tuple(result["final_support"])
    held = {
        bucket: float(by_bucket[bucket].filled_shares)
        + float(by_bucket[bucket].pending_shares)
        for bucket in support
    }
    held_cost = {
        bucket: float(by_bucket[bucket].filled_cost_usdc)
        + float(by_bucket[bucket].pending_notional_usdc)
        for bucket in support
    }
    solved_edge = whole_basket_edge_delta_v1(
        edge_probabilities=(
            (result["basket_support_selection"].get("seated") or {})
            .get("window_probabilities") or {}
        ),
        current_shares=held,
        current_cost=held_cost,
        resulting_shares={
            bucket: float(solution["target_shares_by_bucket"][bucket])
            for bucket in support
        },
        resulting_cost={
            bucket: float(
                (solution.get("target_cost_by_bucket") or {}).get(bucket, 0.0)
            )
            for bucket in support
        },
    )
    # The two readings are different numbers, which is the whole claim: the
    # gate now reads the basket that goes out.
    #
    # NOT "the lift can only make it worse".  A lift adds shares as well as
    # cost, so a rung whose realisable claim beats its ask is +EV to raise --
    # on this fixture the lifted reading is the BETTER one (0.265 against
    # 0.190, raising "32" from five shares to seven at 0.15).  The clause is
    # therefore not a tightening dressed as a re-read: it refuses strictly
    # less often than a reader who assumed the lift always costs, and what it
    # guarantees is only that the number the gate judges is the number the
    # book will be charged for.
    assert edge != pytest.approx(solved_edge)


def test_a_basket_the_lift_puts_under_water_holds_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This fixture's winner is a completion, which charter 143 exempts from
    # this re-read; the refusal this clause names is pinned under its rollback.
    set_charter_143(monkeypatch, "off")
    # Raise the authorization epsilon above what this basket reads after the
    # lift.  The gate, the threshold and the arithmetic are the policy's; what
    # is being tested is that a failure here reaches the plan as a refusal
    # rather than as a basket nobody re-checked.
    baseline = _compile()
    reading = float(baseline["whole_basket_edge_after_lift"])

    policy = _delta_policy()
    strict = dataclasses.replace(
        policy,
        execution_weights=dataclasses.replace(
            policy.execution_weights, epsilon_score=reading + 1.0
        ),
    )
    result = _compile(delta_policy=strict)

    assert result["basket_target_activated"] is False
    assert result["state"] == "HOLD"
    assert "support_edge_after_lift" in result["reason_codes"]
    # Recorded either way: "how much did the lift cost this basket" has to be
    # answerable from a plan that refused as well as one that bought.
    assert result["whole_basket_edge_after_lift"] is not None


def test_the_clause_is_switchable_and_off_reproduces_the_prior_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _delta_policy()
    strict = dataclasses.replace(
        policy,
        execution_weights=dataclasses.replace(
            policy.execution_weights, epsilon_score=1e6
        ),
    )
    monkeypatch.setattr(
        delta_v2_runtime, "CHARTER_123_POST_LIFT_EDGE", "off"
    )
    result = _compile(delta_policy=strict)

    # With the clause off nothing reads the lifted edge, so a basket no
    # threshold could clear still trades -- which is what production did
    # before this ruling, and is the rollback arm.
    assert result["whole_basket_edge_after_lift"] is None
    assert "support_edge_after_lift" not in result["reason_codes"]
    assert result["basket_target_activated"] is True
