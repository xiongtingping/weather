"""The market-lean gate: the book is information about direction.

Measured before wiring (2026-08-28, 481 morning station-days): when the
book's implied mean sat more than half a bucket warm of the model's,
settlement landed warm of the model's mean 94% of the time against a 56%
base rate, and the cold side mirrored at 82%.  The signal lives in the
consensus bridge's dead zone (0.5-1.5C gaps), so nothing else reads it.
"""

import pytest

from bayes_basket_target.delta_v2_runtime import (
    MARKET_LEAN_MIN_BUCKETS,
    WINDOW_REFUSAL_FUNNEL,
    market_lean_ordinal,
    market_lean_opposes,
)


class _Quote:
    def __init__(self, ordinal, ask=None, bid=None):
        self.ordinal = ordinal
        self.ask_available = ask is not None
        self.ask_price = ask
        self.bid_available = bid is not None
        self.bid_price = bid


def _book(*prices):
    """Buckets b0..bn at consecutive ordinals with (ask, bid) pairs."""

    quotes = {}
    ordinals = {}
    for index, pair in enumerate(prices):
        ask, bid = pair if isinstance(pair, tuple) else (pair, None)
        name = f"b{index}"
        quotes[name] = _Quote(index, ask=ask, bid=bid)
        ordinals[name] = index
    return quotes, ordinals


def test_lean_reads_warm_when_the_book_prices_the_warm_tail_up():
    """ZUCK 2026-08-27 in miniature: model mass low, book mass high."""

    quotes, ordinals = _book(0.05, 0.15, 0.30, 0.50)   # book mean ≈ 2.25
    model = {"b0": 0.30, "b1": 0.40, "b2": 0.20, "b3": 0.10}  # mean 1.10
    lean = market_lean_ordinal(quotes, model, ordinals)
    assert lean is not None and lean > 1.0


def test_no_lean_from_an_unquoted_book():
    """A book too empty to state a mean states nothing.

    None must read as "no signal": the gate abstains, it never treats an
    empty book as agreement.
    """

    quotes, ordinals = _book((None, None), (None, None), 0.05)
    model = {"b0": 0.5, "b1": 0.3, "b2": 0.2}
    assert market_lean_ordinal(quotes, model, ordinals) is None
    assert not market_lean_opposes(
        None, window_mid_ordinal=0.0, model_mean_ordinal=2.0
    )


def test_mid_price_used_when_both_sides_quote():
    quotes, ordinals = _book((0.40, 0.20), (0.40, 0.20), (0.40, 0.20))
    model = {"b0": 1 / 3, "b1": 1 / 3, "b2": 1 / 3}
    lean = market_lean_ordinal(quotes, model, ordinals)
    # Flat book against flat model: no lean either way.
    assert lean == pytest.approx(0.0, abs=1e-9)


def test_opposed_is_signed_and_gated_at_half_a_bucket():
    # Market warm of model by a full bucket.
    assert market_lean_opposes(
        1.0, window_mid_ordinal=1.0, model_mean_ordinal=2.0
    )  # window sits COLD of the mean -> opposed
    assert not market_lean_opposes(
        1.0, window_mid_ordinal=3.0, model_mean_ordinal=2.0
    )  # window sits WARM with the lean -> allowed
    # Mirror on the cold side.
    assert market_lean_opposes(
        -1.0, window_mid_ordinal=3.0, model_mean_ordinal=2.0
    )
    assert not market_lean_opposes(
        -1.0, window_mid_ordinal=1.0, model_mean_ordinal=2.0
    )


def test_small_leans_and_centred_windows_are_left_alone():
    """The gate abstains inside its own tolerance, both sides of it."""

    below = MARKET_LEAN_MIN_BUCKETS - 0.01
    assert not market_lean_opposes(
        below, window_mid_ordinal=0.0, model_mean_ordinal=2.0
    )
    # A strong lean but a window sitting ON the mean: not a lean either.
    assert not market_lean_opposes(
        2.0, window_mid_ordinal=2.2, model_mean_ordinal=2.0
    )


def test_gate_is_registered_in_the_funnel_between_skew_and_leg_floor():
    """Placement family, not price family, and the ledger's assertion set
    (GATE_REFUSAL_REASON == funnel) forces the label to exist there too.
    """

    funnel = list(WINDOW_REFUSAL_FUNNEL)
    assert funnel.index("market_lean") == funnel.index("skew") + 1
    assert funnel.index("market_lean") < funnel.index("leg_floor")


def test_default_mode_is_shadow():
    """Wired dark by default: counts, never refuses, until the ledger
    replay prices the on-arm.  Flipping the default is a reviewed change.
    """

    import bayes_basket_target.delta_v2_runtime as runtime

    assert runtime.MARKET_LEAN_GATE_MODE == "shadow"
