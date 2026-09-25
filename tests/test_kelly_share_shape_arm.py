"""The Kelly share-shape research arm (2026-09-06).

Charter 72 sizes the seat as one Kelly stake and buys equal shares of every
rung.  The arm keeps the stake and changes only the split, so ``equal`` must
reproduce the shipped candidate byte for byte and every other shape must
spend the same cash on a different ladder.
"""

from __future__ import annotations

from types import SimpleNamespace

import bayes_basket_target.delta_v2_runtime as R
from bayes_basket_target.basket_kernel.contracts import ContractError
import pytest

from bayes_basket_target.basket_kernel import delta_v2 as K


def _levels(price: float, shares: float = 1000.0):
    return [SimpleNamespace(price=price, shares=shares)]


def _candidate(shape: str, **overrides):
    ordered = ("29°C", "30°C", "31°C")
    prices = {"29°C": 0.14, "30°C": 0.30, "31°C": 0.32}
    kwargs = dict(
        ordered=ordered,
        lane_levels={b: _levels(p) for b, p in prices.items()},
        lane_prices=prices,
        pricing_basis="official_ws_ask_depth",
        current_shares={b: 0.0 for b in ordered},
        current_cost={b: 0.0 for b in ordered},
        pending_shares={b: 0.0 for b in ordered},
        pending_cost={b: 0.0 for b in ordered},
        capital_available_usdc=30.0,
        # Deflated belief: the centre rung carries the edge, the wings little.
        probabilities_by_bucket={"29°C": 0.15, "30°C": 0.50, "31°C": 0.30},
        book_equity_usdc=1000.0,
        quote_version="q1",
        centre_bucket="30°C",
        ordinals={"29°C": 4, "30°C": 5, "31°C": 6},
        share_shape=shape,
    )
    kwargs.update(overrides)
    return R._whole_basket_kelly_candidate(**kwargs)


def test_equal_is_the_shipped_candidate_and_carries_no_arm_keys() -> None:
    equal = _candidate("equal")
    assert equal is not None
    shares = [equal["shares_by_bucket"][b] for b in ("29°C", "30°C", "31°C")]
    assert max(shares) - min(shares) < 1e-9
    assert "share_shape" not in equal["kelly"]
    # The identity hashes exactly what it hashed before the arm existed.
    assert equal["candidate_id"].startswith("kelly-")
    same = _candidate("equal", centre_bucket="", ordinals=None)
    assert same["candidate_id"] == equal["candidate_id"]
    assert same["shares_by_bucket"] == equal["shares_by_bucket"]


@pytest.mark.parametrize("shape", ["edge_weighted", "centre_tilt"])
def test_a_shaped_split_spends_the_same_stake_on_a_different_ladder(shape) -> None:
    equal = _candidate("equal")
    shaped = _candidate(shape)
    assert shaped is not None
    assert abs(
        sum(shaped["cost_by_bucket"].values())
        - sum(equal["cost_by_bucket"].values())
    ) < 1e-6
    assert shaped["kelly"]["stake_usdc"] == pytest.approx(
        equal["kelly"]["stake_usdc"]
    )
    assert shaped["kelly"]["share_shape"] == shape
    assert shaped["candidate_id"] != equal["candidate_id"]
    # The rung with the largest deflated edge holds the most shares.
    by = shaped["shares_by_bucket"]
    assert by["30°C"] > by["29°C"] and by["30°C"] > by["31°C"]
    # And every rung is still bought: the basket stays complete.
    assert min(by.values()) > 0.0


def test_edge_weighted_keeps_the_basket_cost_and_never_breaks_the_floor() -> None:
    """The two invariants charter 72 and charter 93 each need one of."""

    # 0.82 a share: room above charter 93's 5% floor for a tilt to spend.
    prices = {"a": 0.10, "b": 0.40, "c": 0.32}
    unit = sum(prices.values())
    weights = R._kelly_share_weights(
        ordered=("a", "b", "c"),
        lane_prices=prices,
        probabilities_by_bucket={"a": 0.0318, "b": 0.5268, "c": 0.4414},
        centre_bucket="",
        ordinals={},
        shape="edge_weighted",
        stage="P0-M",
    )
    # Charter 72: the stake reads the same cost per share, so the sizing law
    # needs no amendment to admit this shape.
    assert sum(weights[b] * prices[b] for b in prices) == pytest.approx(unit)
    # Charter 93: the in-support floor is its own fraction of the premium
    # plus the cushion the tilt did not spend.
    floor = K.SUPPORT_SCENARIO_FLOOR_FRACTION * unit
    cushion = 1.0 - unit - floor
    assert min(weights.values()) - unit == pytest.approx(
        floor + (1.0 - K.KELLY_EDGE_TILT_FRACTION) * cushion
    )
    assert min(weights.values()) > 0.0


def test_edge_weighted_reads_the_realisable_claim_not_the_asserted_one() -> None:
    """A rung the book prices at a third of our claim must not win the tilt.

    ``d`` asserts the largest nominal edge on the ladder (0.45 claim against a
    0.15 offer, +0.30).  Its agreement is 0.333, which the charter-68 bucket
    arm reads at lambda 0.22, so its realisable claim is 0.099 -- under what
    it costs.  Two facts are checked separately because the runtime applies
    two rules here and they can each be right or wrong on their own: the
    lambda the runtime reads per rung, and what the shape does with it.
    """

    prices = {"b": 0.40, "d": 0.15}
    claims = {"b": 0.55, "d": 0.45}
    assert claims["d"] - prices["d"] > claims["b"] - prices["b"]   # 名义上 d 最大
    # 1. The lambdas the runtime reads: d falls in the left-cliff band.
    lam = {
        k: R.effective_edge_bucket_lambda_for_stage("P0", prices[k] / claims[k])
        for k in prices
    }
    assert lam["d"] == pytest.approx(0.22)
    realisable = {k: claims[k] * lam[k] for k in prices}
    assert realisable["d"] < prices["d"]      # 可兑现后 d 是负边际
    assert realisable["b"] > prices["b"]      # 只有 b 是正的
    # 2. What the shape does with them: only b can receive the cushion.
    tilted = K.floor_first_edge_weights_v1(
        ordered=("b", "d"), prices=prices, effective_claims=realisable,
    )
    assert tilted["b"] > tilted["d"]
    assert sum(tilted[x] * prices[x] for x in prices) == pytest.approx(
        sum(prices.values())
    )
    # 3. And on this ladder the whole-basket guard then refuses that tilt --
    #    the mass sits on ``d``, which the tilt trims -- so the runtime,
    #    which passes the partition, hands back the bed.
    assert R._kelly_share_weights(
        ordered=("b", "d"), lane_prices=prices,
        probabilities_by_bucket=claims,
        centre_bucket="", ordinals={}, shape="edge_weighted", stage="P0",
    ) == {"b": 1.0, "d": 1.0}


def test_the_cushion_is_cash_proportional_to_edge() -> None:
    """Cash in proportion to realisable edge; shares therefore to edge/price.

    Two rungs with the same realisable edge split the freed cash evenly, so
    the cheaper one comes away with more shares.  That is the density the
    charter-35 objective ranks by, not an accident of the arithmetic.
    """

    prices = {"cheap": 0.10, "dear": 0.40}
    claims = {"cheap": 0.20, "dear": 0.50}   # realisable edges equal by design
    w = K.floor_first_edge_weights_v1(
        ordered=("cheap", "dear"), prices=prices, effective_claims=claims,
    )
    # The bed is the formula's, not ``min(w)``: both rungs earned a tilt here,
    # so the cheaper one's weight is no guide to where the bed sits.
    unit = sum(prices.values())
    bed = 1.0 - K.KELLY_EDGE_TILT_FRACTION * (
        1.0 - (1.0 + K.SUPPORT_SCENARIO_FLOOR_FRACTION) * unit
    )
    cash = {b: (w[b] - bed) * prices[b] for b in prices}
    assert cash["cheap"] == pytest.approx(cash["dear"])
    assert w["cheap"] > w["dear"]
    assert sum(w[b] * prices[b] for b in prices) == pytest.approx(unit)


@pytest.mark.parametrize(
    "prices,claims",
    [
        ({"a": 0.30, "b": 0.30, "c": 0.30}, {"a": 0.10, "b": 0.60, "c": 0.40}),
        ({"a": 0.05, "b": 0.90}, {"a": 0.40, "b": 0.55}),
        ({"a": 0.33, "b": 0.33, "c": 0.33}, {"a": 0.33, "b": 0.34, "c": 0.33}),
        ({"a": 0.01, "b": 0.02, "c": 0.03}, {"a": 0.30, "b": 0.30, "c": 0.40}),
    ],
)
def test_the_shape_is_always_charter_93_legal(prices, claims) -> None:
    """Run the production floor function over the shape, not a copy of it.

    Legal whenever the equal basket is: the tilt cannot rescue a basket that
    already costs more than the floor allows, and must not break one that
    does not.  The third case costs 0.99 a share, under the 5% floor at one
    share a leg, so the arm hands back equal and the solver refuses it there.
    """

    ordered = tuple(prices)
    weights = R._kelly_share_weights(
        ordered=ordered, lane_prices=prices, probabilities_by_bucket=claims,
        centre_bucket="", ordinals={}, shape="edge_weighted", stage="P0-M",
    )

    def _verdict(shares):
        return K.support_scenario_floor_v1(
            shares_by_bucket=shares,
            cost_by_bucket={b: shares[b] * prices[b] for b in ordered},
            support=ordered,
        )

    if _verdict({b: 1.0 for b in ordered})["legal"]:
        assert _verdict(weights)["legal"], _verdict(weights)
    else:
        assert weights == {b: 1.0 for b in ordered}


def test_zero_tilt_and_a_dead_ladder_both_reproduce_equal() -> None:
    prices = {"a": 0.30, "b": 0.30, "c": 0.30}
    claims = {"a": 0.10, "b": 0.60, "c": 0.40}
    equal = {"a": 1.0, "b": 1.0, "c": 1.0}
    # The rollback: t=0 is the shipped shape, exactly.
    assert K.floor_first_edge_weights_v1(
        ordered=tuple(prices), prices=prices,
        effective_claims=claims, tilt_fraction=0.0,
    ) == equal
    # No positive realisable edge anywhere: nothing to tilt toward.
    assert K.floor_first_edge_weights_v1(
        ordered=tuple(prices), prices=prices,
        effective_claims={"a": 0.10, "b": 0.20, "c": 0.10},
    ) == equal
    # A basket that already costs a share has no cushion to spend.
    dear = {"a": 0.50, "b": 0.55}
    assert K.floor_first_edge_weights_v1(
        ordered=tuple(dear), prices=dear,
        effective_claims={"a": 0.90, "b": 0.90},
    ) == {"a": 1.0, "b": 1.0}


def test_centre_tilt_reproduces_the_anchored_shape_and_needs_a_centre() -> None:
    weights = R._kelly_share_weights(
        ordered=("a", "b", "c"),
        lane_prices={"a": 0.30, "b": 0.30, "c": 0.30},
        probabilities_by_bucket={"a": 0.10, "b": 0.60, "c": 0.35},
        centre_bucket="b",
        ordinals={"a": 1, "b": 2, "c": 3},
        shape="centre_tilt",
    )
    assert weights["b"] == pytest.approx(1.0 + R.CENTRE_ANCHOR_EDGE_TILT)
    assert weights["c"] == pytest.approx(
        R.CENTRE_ANCHOR_DISTANCE_DECAY * (1.0 + R.CENTRE_ANCHOR_EDGE_TILT * (0.05 / 0.30))
    )
    assert weights["a"] == pytest.approx(
        max(R.CENTRE_ANCHOR_DISTANCE_DECAY, R.CENTRE_ANCHOR_MIN_RUNG_SHARE)
    )
    no_centre = R._kelly_share_weights(
        ordered=("a", "b", "c"),
        lane_prices={"a": 0.30, "b": 0.30, "c": 0.30},
        probabilities_by_bucket={"a": 0.10, "b": 0.60, "c": 0.35},
        centre_bucket="",
        ordinals={},
        shape="centre_tilt",
    )
    assert no_centre == {"a": 1.0, "b": 1.0, "c": 1.0}


def test_an_unknown_shape_is_refused() -> None:
    with pytest.raises(ContractError):
        R._kelly_share_weights(
            ordered=("a",), lane_prices={"a": 0.5},
            probabilities_by_bucket={"a": 0.6}, centre_bucket="a",
            ordinals={"a": 1}, shape="lottery",
        )


def test_production_ships_the_equal_shape() -> None:
    assert R.KELLY_SHARE_SHAPE == "equal"


def test_the_tilt_never_expects_less_than_the_bed_it_came_from() -> None:
    """The shape's floor is a theorem; its edge is not, so it is compared.

    The tilt orders rungs by ``(lambda*p - c)/c`` and the expectation prices
    them at ``p``.  Where the mass sits on a rung the tilt trims, the tilted
    basket expects less than the equal one at the same cost -- and the window
    was admitted on the equal one.  The shape hands back the bed instead.
    """

    prices = {"a": 0.10, "b": 0.40, "c": 0.32}
    claims = {"a": 0.044, "b": 0.727, "c": 0.609}
    tilted = K.floor_first_edge_weights_v1(
        ordered=("a", "b", "c"), prices=prices, effective_claims=claims,
    )
    equal = {"a": 1.0, "b": 1.0, "c": 1.0}
    # Mass on the rung the tilt trims: expecting less, so the bed comes back.
    heavy_on_trimmed = {"a": 0.90, "b": 0.05, "c": 0.05}
    assert sum(heavy_on_trimmed[b] * tilted[b] for b in prices) < 1.0
    assert K.floor_first_edge_weights_v1(
        ordered=("a", "b", "c"), prices=prices, effective_claims=claims,
        partition_by_bucket=heavy_on_trimmed,
    ) == equal
    # Mass on the rungs it raises: the tilt is kept.
    heavy_on_raised = {"a": 0.05, "b": 0.55, "c": 0.40}
    assert K.floor_first_edge_weights_v1(
        ordered=("a", "b", "c"), prices=prices, effective_claims=claims,
        partition_by_bucket=heavy_on_raised,
    ) == tilted
    # Passing no partition keeps the unchecked shape, which is what every
    # caller that has no belief to compare against gets.
    assert K.floor_first_edge_weights_v1(
        ordered=("a", "b", "c"), prices=prices, effective_claims=claims,
    ) == tilted


def test_the_tilt_fraction_defaults_to_the_measured_corner() -> None:
    """t=1 by the 2026-09-08 scan; the floor is still charter 93's there."""

    assert K.KELLY_EDGE_TILT_FRACTION == 1.0
    prices = {"a": 0.10, "b": 0.40, "c": 0.32}
    unit = sum(prices.values())
    w = K.floor_first_edge_weights_v1(
        ordered=("a", "b", "c"), prices=prices,
        effective_claims={"a": 0.044, "b": 0.727, "c": 0.609},
    )
    assert sum(w[b] * prices[b] for b in prices) == pytest.approx(unit)
    # The whole cushion spent lands the floor exactly on charter 93's own
    # fraction of the premium -- legal, and 93(二) rechecks it after the lift.
    assert min(w.values()) - unit == pytest.approx(
        K.SUPPORT_SCENARIO_FLOOR_FRACTION * unit
    )
    assert K.support_scenario_floor_v1(
        shares_by_bucket=w,
        cost_by_bucket={b: w[b] * prices[b] for b in prices},
        support=tuple(prices),
    )["legal"]
