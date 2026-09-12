"""The market-lean gate: the book is information about direction.

Measured before wiring (2026-08-28, 481 morning station-days): when the
book's implied mean sat more than half a bucket warm of the model's,
settlement landed warm of the model's mean 94% of the time against a 56%
base rate, and the cold side mirrored at 82%.  The signal lives in the
consensus bridge's dead zone (0.5-1.5C gaps), so nothing else reads it.
"""

import inspect

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


def test_gate_is_registered_in_the_funnel_between_shape_and_leg_floor():
    """Placement family, not price family, and the ledger's assertion set
    (GATE_REFUSAL_REASON == funnel) forces the label to exist there too.
    """

    funnel = list(WINDOW_REFUSAL_FUNNEL)
    assert funnel.index("market_lean") == funnel.index("shape") + 1
    assert funnel.index("market_lean") < funnel.index("leg_floor")


def test_default_mode_is_shadow_by_charter_131():
    """Promoted 2026-09-05 (user ruling); this test said ``shadow`` until
    2026-09-08 and had been red for three days.

    The 2026-08-28 ruling wired the gate dark on purpose -- ``shadow``
    counts what it would refuse and refuses nothing -- and held it there
    until a ledger replay priced the on-arm.  The replay read +0.9 points
    alone, +12.4 from 08-23 and +4.3 inside the best combination, and the
    default moved.  Production agrees: zero refusals through 09-04, then
    17 / 27 / 15 / 1 on 09-05..08 against 1178 / 1328 / 617 / 53 windows
    enumerated, while the shadow counter went 64 / 34 / 2 / 0 across the
    handover.
    """

    import bayes_basket_target.delta_v2_runtime as runtime

    # Charter 131 (2) (user ruling 2026-09-12, Q2): the market's mean bucket
    # is data being collected, not yet a rule; the gate counts and stands
    # aside.  The 09-05 promotion to ``on`` is the history below.
    assert runtime.MARKET_LEAN_GATE_MODE == "shadow"
    # ``shadow`` and ``off`` both remain reachable: one measures, one
    # retires.  The gate reads ``!= "off"`` to compute and ``== "on"`` to
    # refuse, so shadow is exactly "compute, do not refuse".
    source = inspect.getsource(runtime._support_windows)
    assert 'MARKET_LEAN_GATE_MODE != "off"' in source
    assert 'MARKET_LEAN_GATE_MODE == "on"' in source


# --------------------------------------------------------------------------
# The claims the design makes for itself, pinned (2026-09-08).  Every one of
# these is written down in the constants block as a property of the gate; a
# gate whose stated properties are untested is a gate nobody can refactor.
# --------------------------------------------------------------------------

def test_a_one_sided_quote_is_read_as_itself_not_dropped():
    """The docstring's rule: mids where both sides exist, one-sided as itself.

    A book quoting only asks on the warm tail still says the warm tail is
    dear, and dropping those rungs would read the day as flatter than the
    book does.
    """

    two_sided, ordinals = _book((0.10, 0.06), (0.20, 0.16), (0.40, 0.36))
    ask_only, _ = _book(0.08, 0.18, 0.38)
    probabilities = {"b0": 0.34, "b1": 0.33, "b2": 0.33}
    assert market_lean_ordinal(two_sided, probabilities, ordinals) == pytest.approx(
        market_lean_ordinal(ask_only, probabilities, ordinals), abs=1e-9
    )


def test_a_book_with_too_few_rungs_or_too_little_mass_states_nothing():
    """Two independent floors, and ``None`` must never read as 'aligned'."""

    from bayes_basket_target.delta_v2_runtime import MARKET_LEAN_MIN_QUOTED_MASS

    probabilities = {"b0": 0.5, "b1": 0.5, "b2": 0.0}
    # Fewer than three priced rungs: no mean to state.
    thin, ordinals = _book(0.40, 0.40, None)
    assert market_lean_ordinal(thin, probabilities, ordinals) is None
    # Three rungs but summed mid mass under the floor: still nothing.
    faint, ordinals2 = _book(0.02, 0.02, 0.02)
    assert sum((0.02, 0.02, 0.02)) < MARKET_LEAN_MIN_QUOTED_MASS
    assert market_lean_ordinal(faint, {"b0": 0.4, "b1": 0.3, "b2": 0.3}, ordinals2) is None
    # And a ``None`` lean never opposes anything, which is the half of the
    # contract a caller could get wrong.
    assert market_lean_opposes(None, window_mid_ordinal=9.0, model_mean_ordinal=1.0) is False


def test_the_incumbent_is_exempt_because_holding_is_not_leaning():
    """The seat we already hold is not a bet on direction."""

    source = inspect.getsource(
        __import__(
            "bayes_basket_target.delta_v2_runtime", fromlist=["_support_windows"]
        )._support_windows
    )
    gate = source[source.index("The market-lean gate:"):]
    gate = gate[:gate.index("market_lean_refused")]
    assert "not is_incumbent" in gate


def test_the_gate_only_subtracts():
    """It refuses windows; it never seats, prices or sizes one.

    A window it passes still faces the leg floor, the pricing and the edge
    test, so the gate cannot be the reason anything is bought -- only the
    reason something was not considered.
    """

    funnel = list(WINDOW_REFUSAL_FUNNEL)
    for later in ("leg_floor", "unpriced", "edge"):
        assert funnel.index("market_lean") < funnel.index(later), later
    runtime = __import__(
        "bayes_basket_target.delta_v2_runtime", fromlist=["_support_windows"]
    )
    source = inspect.getsource(runtime._support_windows)
    start = source.index("The market-lean gate:")
    gate = source[start:source.index("market_lean_refused += 1", start) + 200]
    # The only thing the gate does on a refusal is count and continue: no
    # seat, no price, no size.
    assert "market_lean_refused += 1" in gate
    assert "continue" in gate
    for forbidden in ("target_shares", "stake", "clip", "authorized"):
        assert forbidden not in gate, forbidden


def test_the_funnel_arithmetic_still_closes_with_the_gate_counted():
    """Every enumerated window is either shortlisted or refused by one gate.

    ``_window_audit_closes`` is the invariant the ledger's refusal page
    stands on; market_lean joining the funnel must not break it, in either
    mode -- shadow counts zero refusals and the sum still has to add up.
    """

    runtime = __import__(
        "bayes_basket_target.delta_v2_runtime", fromlist=["_window_audit_closes"]
    )
    audit = {"enumerated_window_count": 10, "shortlisted_window_count": 4}
    for gate in WINDOW_REFUSAL_FUNNEL:
        key = (
            "gapped_window_refused_count" if gate == "gapped"
            else "%s_refused_window_count" % gate
        )
        audit[key] = 0
    audit["market_lean_refused_window_count"] = 6
    assert runtime._window_audit_closes(audit)
    # One window unaccounted for and the page would be stating causes that
    # cannot produce the effect.
    audit["market_lean_refused_window_count"] = 5
    assert not runtime._window_audit_closes(audit)


def test_opposition_is_symmetric_in_both_signs():
    """Warm and cold are the same rule with the sign flipped.

    The measurement is two-sided (94% warm, 82% cold) and the gate must not
    quietly become a one-sided hot-tail rule.
    """

    far = MARKET_LEAN_MIN_BUCKETS + 0.5
    # Book warm, window cold -> opposed.  Book cold, window warm -> opposed.
    assert market_lean_opposes(+far, window_mid_ordinal=5.0 - far, model_mean_ordinal=5.0)
    assert market_lean_opposes(-far, window_mid_ordinal=5.0 + far, model_mean_ordinal=5.0)
    # Same side as the book -> never opposed, either sign.
    assert not market_lean_opposes(+far, window_mid_ordinal=5.0 + far, model_mean_ordinal=5.0)
    assert not market_lean_opposes(-far, window_mid_ordinal=5.0 - far, model_mean_ordinal=5.0)
