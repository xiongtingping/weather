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


def test_edge_weighted_floors_a_rung_without_edge_and_falls_back_to_equal() -> None:
    weights = R._kelly_share_weights(
        ordered=("a", "b", "c"),
        lane_prices={"a": 0.30, "b": 0.30, "c": 0.30},
        # Edges: a -0.20 (no edge, floored), b +0.30 (best), c +0.10.
        probabilities_by_bucket={"a": 0.10, "b": 0.60, "c": 0.40},
        centre_bucket="b",
        ordinals={"a": 1, "b": 2, "c": 3},
        shape="edge_weighted",
    )
    assert weights["b"] == pytest.approx(1.0)
    assert weights["a"] == pytest.approx(R.KELLY_EDGE_MIN_RUNG_SHARE)
    assert weights["c"] == pytest.approx(max(R.KELLY_EDGE_MIN_RUNG_SHARE, 0.1 / 0.3))
    flat = R._kelly_share_weights(
        ordered=("a", "b"),
        lane_prices={"a": 0.50, "b": 0.50},
        probabilities_by_bucket={"a": 0.20, "b": 0.20},
        centre_bucket="a",
        ordinals={"a": 1, "b": 2},
        shape="edge_weighted",
    )
    assert flat == {"a": 1.0, "b": 1.0}


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


def test_the_ledger_reads_productions_switch() -> None:
    import scripts.paper_trading_ledger as L

    assert L.KELLY_SHARE_SHAPE is R.KELLY_SHARE_SHAPE
    assert R.KELLY_SHARE_SHAPE == "equal"
