"""The marginal-coverage reading: h/K, and Δh/ΔK for what a window adds.

Audit only, by ruling (2026-09-09: "记成审计列，不当闸").  Acting on the
criterion was measured and lost money -- building the window outward under it
took 118 opening seats from -10.34% to -19.12% on the served claim -- so what
is pinned here is that the numbers are right and that nothing reads them back.
"""

from __future__ import annotations

import pytest

from bayes_basket_target import delta_v2_runtime
from bayes_basket_target.basket_kernel.delta_v2 import (
    marginal_coverage_reading_v1,
)
from bayes_basket_target.delta_v2_runtime import compile_delta_v2_runtime_plan

from tests.test_bayes_basket_target_delta_v2_runtime import _delta_policy
from tests.test_charter_84_ladder_balance import (
    _basket_policy,
    _fill_resolver,
    _held_snapshot,
)


def test_the_ratio_answers_the_question_it_is_named_for() -> None:
    claim = {"a": 0.30, "b": 0.25, "c": 0.10}
    cost = {"a": 0.22, "b": 0.20, "c": 0.15}

    held = marginal_coverage_reading_v1(
        claim_by_bucket=claim, cost_by_bucket=cost,
        window=("a", "b"), held_buckets=("a", "b"),
    )
    assert held["kind"] == "shares_only"
    # Scaling an equal-share basket leaves h/K identically unchanged, so the
    # criterion has nothing to say about a top-up that only adds shares.  It
    # says so with None rather than with a verdict.
    assert held["marginal_claim_cost_ratio"] is None
    assert held["marginal_pays_its_way"] is None
    assert held["claim_cost_ratio"] == pytest.approx(0.55 / 0.42)

    widened = marginal_coverage_reading_v1(
        claim_by_bucket=claim, cost_by_bucket=cost,
        window=("a", "b", "c"), held_buckets=("a", "b"),
    )
    assert widened["kind"] == "widen"
    assert widened["added_buckets"] == ["c"]
    assert widened["marginal_claim_cost_ratio"] == pytest.approx(0.10 / 0.15)
    assert widened["held_claim_cost_ratio"] == pytest.approx(0.55 / 0.42)
    # 0.667 against 1.310: the rung does not pay its way.
    assert widened["marginal_pays_its_way"] is False

    # And that verdict is exactly the sign of the ROI move, which is the
    # whole claim the criterion rests on.
    assert widened["claim_cost_ratio"] < widened["held_claim_cost_ratio"]

    # A rung that does pay, on the same shape.
    cheap = marginal_coverage_reading_v1(
        claim_by_bucket={**claim, "c": 0.30}, cost_by_bucket=cost,
        window=("a", "b", "c"), held_buckets=("a", "b"),
    )
    assert cheap["marginal_pays_its_way"] is True
    assert cheap["claim_cost_ratio"] > cheap["held_claim_cost_ratio"]

    opening = marginal_coverage_reading_v1(
        claim_by_bucket=claim, cost_by_bucket=cost,
        window=("a", "b"), held_buckets=(),
    )
    assert opening["kind"] == "open"
    assert opening["marginal_pays_its_way"] is None


def test_the_seat_row_carries_it_and_the_projection_does_not_drop_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        delta_v2_runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )
    result = compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )
    seated = result["basket_support_selection"]["seated"]
    reading = seated["marginal_coverage"]

    # ``seated`` is a key-by-key whitelist, so this assertion is the one that
    # would have caught charter 97's lambda source being dropped on the way
    # out for the whole life of the clause.
    assert reading is not None
    assert reading["rule"] == "marginal_coverage_reading_v1"
    # This fixture holds two rungs and seats three, so it is a widening and
    # the marginal reading exists.
    assert reading["kind"] == "widen"
    assert reading["marginal_claim_cost_ratio"] is not None
    assert reading["held_claim_cost_ratio"] is not None
    assert reading["marginal_pays_its_way"] is (
        reading["marginal_claim_cost_ratio"]
        > reading["held_claim_cost_ratio"]
    )


def test_it_decides_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seat is what it was before the reading existed.

    There is no switch to turn this off, so the control is the arithmetic:
    a reading that refuses everything must still change no seat.
    """

    monkeypatch.setattr(
        delta_v2_runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )

    def compile_with(reading):
        monkeypatch.setattr(
            delta_v2_runtime, "marginal_coverage_reading_v1", reading
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

    real = compile_with(marginal_coverage_reading_v1)

    def always_refuses(**kwargs):
        out = marginal_coverage_reading_v1(**kwargs)
        return {**out, "marginal_pays_its_way": False, "claim_cost_ratio": 0.0}

    forced = compile_with(always_refuses)

    assert (
        forced["basket_support_selection"]["seated"]["buckets"]
        == real["basket_support_selection"]["seated"]["buckets"]
    )
    assert forced["basket_target_activated"] is real["basket_target_activated"]
    assert (
        forced["target_solution"]["target_shares_by_bucket"]
        == real["target_solution"]["target_shares_by_bucket"]
    )
