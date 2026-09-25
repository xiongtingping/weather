"""The execution layer is judged by the objective, every round.

Charter 110.  One objective runs through all three stages -- from the
position we hold, on the information we have now, does this action raise
expected wealth and keep the support floor -- and opening is only its
special case at a flat book.  What the execution layer had instead was a
target handed over once and a ladder left alone until settlement.

Two facts this module has to encode, both measured 2026-09-07:

The completion ceiling was inverted.  ``ask <= claim`` bought 1,971 legs
that realized 0.28 of what we claimed for them and lost 0.038 a leg with a
station-day clustered interval of [-0.053, -0.021]; it refused 604 legs
that realized 1.60 and are break-even at worst.  Seventy-one percent of
the refused group is the market's own modal bucket -- the leg the market
took away from us.

And the action set is one-way.  Production emits BUY (``order_action`` is
the literal "BUY"; the only SELL lane sells buckets whose day already
settled our way).  A basket short a leg can be completed or carried
broken.  No rule may be written with a third branch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    BASKET_STATE_COMPLETE,
    BASKET_STATE_EMPTY,
    BASKET_STATE_PARTIAL,
    COMPLETION_CUTOFF_HOURS,
    LEG_ACTION_CANCEL,
    LEG_ACTION_CROSS,
    LEG_ACTION_POST,
    LEG_ACTION_REPRICE_UP,
    LEG_ACTION_REST,
    ONE_WAY_ACTION_SET,
    completion_conditioned_claim_v1,
    execution_completion_verdict_v1,
    support_scenario_floor_v1,
)

WINDOW = ("29", "30", "31")
TARGET = {"29": 5.0, "30": 5.0, "31": 5.0}


def _book(**over):
    book = {
        "29": {"bid": 0.28, "ask": 0.30, "tick": 0.01},
        "30": {"bid": 0.40, "ask": 0.42, "tick": 0.01},
        "31": {"bid": 0.20, "ask": 0.22, "tick": 0.01},
    }
    book.update(over)
    return book


def _verdict(**over):
    kwargs = dict(
        window=WINDOW,
        target_shares=TARGET,
        held_shares={"29": 0.0, "30": 5.0, "31": 5.0},
        held_cost_usdc={"29": 0.0, "30": 1.50, "31": 1.00},
        claim_by_bucket={"29": 0.20, "30": 0.45, "31": 0.27},
        book_by_bucket=_book(),
        rung_by_bucket={"29": {"limit": 0.29, "age_seconds": 1200.0}},
        hours_to_cutoff=6.0,
    )
    kwargs.update(over)
    return execution_completion_verdict_v1(**kwargs)


# -- the inversion ---------------------------------------------------------

def test_the_leg_the_market_took_away_is_the_one_worth_completing():
    """RKSI 2026-09-06's shape: 30 and 31 sold to us, 29 bid above our
    claim, truth 29.  The old ceiling refused this leg by construction."""

    verdict = _verdict()
    leg = verdict["legs"]["29"]
    assert leg["action"] == LEG_ACTION_CROSS
    assert leg["conditioned"]["ratio"] > 1.0, "the market bids it above us"
    assert leg["conditioned"]["conditioned_claim"] > leg["ask"]
    # And the old rule would have refused it: the raw claim is under the ask.
    assert verdict["legs"]["29"]["conditioned"]["claim"] < leg["ask"]


def test_the_leg_the_market_hands_us_is_not_worth_completing():
    """The zone the old ceiling bought in.  Its interval does not cross
    zero, so this is the one direction the table asserts rather than
    guesses."""

    verdict = _verdict(
        claim_by_bucket={"29": 0.40, "30": 0.45, "31": 0.27},
        book_by_bucket=_book(**{"29": {"bid": 0.10, "ask": 0.12, "tick": 0.01}}),
        rung_by_bucket={"29": {"limit": 0.11, "age_seconds": 1200.0}},
    )
    leg = verdict["legs"]["29"]
    assert leg["conditioned"]["ratio"] < 1.0
    assert leg["conditioned"]["buys"] is False
    assert leg["completion_declined"] == "market_discounts_this_leg"
    # The old rule bought here: raw claim 0.40 stands well over the 0.12 ask.
    assert leg["conditioned"]["claim"] > leg["ask"]
    # Declining to pay its ask is not declining the leg.  The rung stays: if
    # the market comes down to the anchor the basket still wants it, and
    # cancelling here would be the completion rule eating its own best
    # chance of completing.
    assert leg["action"] == LEG_ACTION_REST


def test_the_conditioned_claim_never_leaves_the_unit_interval():
    """A 1.6x factor on a claim of 0.8 is not a probability."""

    out = completion_conditioned_claim_v1(claim=0.80, ask=0.95)
    assert out["factor"] == pytest.approx(1.60)
    assert out["conditioned_claim"] == pytest.approx(1.0)
    assert out["buys"] is True


def test_a_claim_of_zero_takes_the_far_cell_and_still_refuses():
    """No ratio exists, and inventing the near cell would make a zero-claim
    leg look cheap.  Either cell multiplies zero by something, so what has
    to hold is that it does not buy."""

    out = completion_conditioned_claim_v1(claim=0.0, ask=0.05)
    assert out["ratio"] is None
    assert out["conditioned_claim"] == pytest.approx(0.0)
    assert out["buys"] is False


# -- the one-way action set ------------------------------------------------

def test_the_module_states_that_nothing_can_be_sold():
    """The rule that is easiest to write wrongly.  A residual basket has two
    futures, not three, and every branch below has to be one of them."""

    assert ONE_WAY_ACTION_SET is True


def test_past_the_cutoff_the_residual_is_carried_not_handed_back():
    """Inside the last three hours completing is measurably negative, so the
    open orders come down -- but the legs already bought stay bought.  The
    verdict may not offer an exit it does not have."""

    verdict = _verdict(hours_to_cutoff=1.0)
    leg = verdict["legs"]["29"]
    assert leg["action"] == LEG_ACTION_CANCEL
    assert leg["reason"] == "past_completion_cutoff"
    assert verdict["completion"]["in_completion_window"] is False
    # What is held is untouched and still short -- that is the exposure the
    # cutoff exists to stop us walking into, not one it can undo.
    assert verdict["basket_state"] == BASKET_STATE_PARTIAL
    assert verdict["held_shares"]["30"] == pytest.approx(5.0)
    assert verdict["exposure_usdc"] > 0.0
    actions = {leg["action"] for leg in verdict["legs"].values()}
    assert "sell" not in actions and "unwind" not in actions


# -- the floor and the cost ceiling are one inequality ----------------------

def test_completing_a_level_basket_under_a_share_is_the_floor_being_legal():
    """Charter 74's "whole-basket per-share cost < 1" and charter 93's
    support floor are the same inequality once the basket is level.  Two
    call sites deriving it separately is how they drift."""

    verdict = _verdict()
    assert verdict["completion"]["per_share_cost_after"] == pytest.approx(0.80)
    assert verdict["completion"]["cost_ceiling_ok"] is True
    completed = support_scenario_floor_v1(
        shares_by_bucket={b: 5.0 for b in WINDOW},
        cost_by_bucket={"29": 1.50, "30": 1.50, "31": 1.00},
        support=WINDOW,
    )
    assert completed["legal"] is True
    # And the basket as actually held fails it, which is what makes the
    # residual a state to act on rather than a smaller version of the trade.
    assert verdict["support_floor"]["legal"] is False


def test_a_completion_that_costs_a_whole_share_is_refused():
    """Model-free: a basket costing a share can never pay.  This is the one
    ceiling that does not read a probability."""

    verdict = _verdict(
        held_cost_usdc={"29": 0.0, "30": 3.00, "31": 2.20},
        book_by_bucket=_book(**{"29": {"bid": 0.28, "ask": 0.30, "tick": 0.01}}),
    )
    assert verdict["completion"]["per_share_cost_after"] > 1.0
    assert verdict["completion"]["cost_ceiling_ok"] is False
    assert verdict["legs"]["29"]["action"] == LEG_ACTION_CANCEL
    assert verdict["legs"]["29"]["reason"] == "completed_basket_costs_a_share"


# -- the resting rules -----------------------------------------------------

def test_a_vanished_bid_cancels_rather_than_leaving_the_rung_to_be_swept():
    """The loss concentrator, measured at -0.0234 a share against -0.0067
    for a rung the ask merely reaches.  The shipped rule kept it."""

    verdict = _verdict(
        book_by_bucket=_book(**{"29": {"bid": 0.0, "ask": 0.09, "tick": 0.01}}),
        rung_by_bucket={"29": {"limit": 0.29, "age_seconds": 60.0}},
    )
    assert verdict["legs"]["29"]["action"] == LEG_ACTION_CANCEL
    assert verdict["legs"]["29"]["reason"] == "no_anchor"


def test_the_rung_follows_the_bid_up_and_never_down():
    """Following it down re-mints the rung into the swept population by
    hand.  Measured: filled legs' asks moved -0.0058, missed legs' +0.0008."""

    up = _verdict(
        book_by_bucket=_book(**{"29": {"bid": 0.34, "ask": 0.38, "tick": 0.01}}),
        rung_by_bucket={"29": {"limit": 0.29, "age_seconds": 1200.0}},
        hours_to_cutoff=6.0,
        claim_by_bucket={"29": 0.60, "30": 0.45, "31": 0.27},
    )
    assert up["legs"]["29"]["action"] == LEG_ACTION_REPRICE_UP
    assert up["legs"]["29"]["post_at"] == pytest.approx(0.35)
    # Five ticks is past ``far_ticks``, and a bid that ran UP that far is
    # still a follow-up, not a plain cancel: the distance decides that the
    # rung is stale, the direction decides what replaces it.
    assert up["legs"]["29"]["reason"] == "bid_rose_far"

    down = _verdict(
        book_by_bucket=_book(**{"29": {"bid": 0.27, "ask": 0.31, "tick": 0.01}}),
        rung_by_bucket={"29": {"limit": 0.29, "age_seconds": 60.0}},
    )
    assert down["legs"]["29"]["action"] == LEG_ACTION_REST
    assert down["legs"]["29"]["limit_price"] == pytest.approx(0.29)


def test_declining_the_ask_is_not_the_same_decision_as_cancelling():
    """The ordering bug this suite caught on the first run: a leg whose ask
    the conditioned claim will not pay was being cancelled, which threw away
    a rung that was still the basket's cheapest route to completing.  The
    crossing decision may only ever produce a cross."""

    verdict = _verdict(
        claim_by_bucket={"29": 0.60, "30": 0.45, "31": 0.27},
        book_by_bucket=_book(**{"29": {"bid": 0.34, "ask": 0.38, "tick": 0.01}}),
        rung_by_bucket={"29": {"limit": 0.29, "age_seconds": 1200.0}},
    )
    leg = verdict["legs"]["29"]
    assert leg["conditioned"]["buys"] is False
    assert leg["action"] == LEG_ACTION_REPRICE_UP, (
        "the bid rose, so the rung follows it up -- the refused cross does "
        "not veto the resting rules"
    )
    assert leg["reason"] == "bid_rose_far"


def test_a_bid_that_ran_far_takes_the_rung_down_with_it():
    verdict = _verdict(
        book_by_bucket=_book(**{"29": {"bid": 0.20, "ask": 0.34, "tick": 0.01}}),
        rung_by_bucket={"29": {"limit": 0.29, "age_seconds": 60.0}},
    )
    assert verdict["legs"]["29"]["action"] == LEG_ACTION_CANCEL
    assert verdict["legs"]["29"]["reason"] == "bid_ran_far"


def test_a_leg_within_its_dwell_is_left_alone_to_keep_its_queue_place():
    verdict = _verdict(
        rung_by_bucket={"29": {"limit": 0.29, "age_seconds": 60.0}},
    )
    assert verdict["legs"]["29"]["action"] == LEG_ACTION_REST


def test_a_leg_with_nothing_standing_is_posted_on_the_anchor():
    verdict = _verdict(rung_by_bucket={})
    assert verdict["legs"]["29"]["action"] == LEG_ACTION_POST
    assert verdict["legs"]["29"]["post_at"] == pytest.approx(0.29)


def test_a_leg_at_its_target_is_left_alone():
    verdict = _verdict(held_shares={"29": 5.0, "30": 5.0, "31": 5.0})
    assert verdict["basket_state"] == BASKET_STATE_COMPLETE
    assert all(
        leg["action"] == "none" for leg in verdict["legs"].values()
    )
    assert verdict["uncovered_mass"] == pytest.approx(0.0)
    assert verdict["exposure_usdc"] == pytest.approx(0.0)


def test_an_empty_chain_is_an_opening_not_a_completion():
    """Nothing has been paid, so there is no exposure to complete against
    and no floor to restore -- the objective's flat-book special case."""

    verdict = _verdict(
        held_shares={b: 0.0 for b in WINDOW},
        held_cost_usdc={b: 0.0 for b in WINDOW},
        rung_by_bucket={b: {"limit": 0.20, "age_seconds": 1200.0} for b in WINDOW},
    )
    assert verdict["basket_state"] == BASKET_STATE_EMPTY
    assert verdict["premium_paid_usdc"] == pytest.approx(0.0)
    assert verdict["exposure_usdc"] == pytest.approx(0.0)
    assert LEG_ACTION_CROSS not in {
        leg["action"] for leg in verdict["legs"].values()
    }


def test_the_exposure_is_the_premium_the_missing_mass_puts_at_risk():
    verdict = _verdict()
    assert verdict["uncovered_mass"] == pytest.approx(0.20)
    assert verdict["premium_paid_usdc"] == pytest.approx(2.50)
    assert verdict["exposure_usdc"] == pytest.approx(0.50)


def test_the_cutoff_is_three_hours():
    """Measured: completing is +0.043 beyond twelve hours, ~0 at 3-12h,
    -0.026 at 1-3h and -0.075 inside the last hour."""

    assert COMPLETION_CUTOFF_HOURS == 3.0


# ---------------------------------------------------------------------------
# 110(七): a fill is an event.


def _moment(**kwargs):
    from bayes_basket_target.basket_kernel.delta_v2 import (
        is_decision_moment_v1,
    )

    base = {
        "probability_identity": "p-1",
        "edge_state": "admissible",
        "last_probability_identity": "p-1",
        "last_edge_state": "admissible",
    }
    return is_decision_moment_v1(**{**base, **kwargs})


def test_a_leg_landing_is_a_decision_moment_on_its_own():
    """Charter 110(七), and the defect it closes.

    Charter 77 asked the solver only when the model moved or the day's
    admissibility flipped.  A leg filling does neither, so a basket that got
    one rung of three sat at one rung of three: charter 84's balance
    candidate exists precisely to raise the short rungs to the held level,
    and it is built inside a compile this rule had already refused to run.
    """

    assert not _moment(basket_holding="", last_basket_holding="")
    assert _moment(basket_holding="34:5", last_basket_holding="")
    assert _moment(basket_holding="34:5|35:5", last_basket_holding="34:5")


def test_a_leg_growing_on_the_same_bucket_is_also_a_moment():
    """Composition alone is not enough.  A leg that fills 2 of 5 and later
    the other 3 leaves the seat uneven at 2 while charter 84 raises the
    others to 2; the level has to be in the signature or the second half of
    that fill is invisible."""

    assert _moment(basket_holding="34:5", last_basket_holding="34:2")


def test_our_own_unfilled_order_is_not_an_event():
    """The signature is read off FILLED shares.  Letting pending in would
    make placing a rung a reason to re-solve, which is a loop."""

    from bayes_basket_target.basket_kernel.delta_v2 import (
        basket_holding_signature_v1,
    )

    assert basket_holding_signature_v1({"34": 0.0, "35": 0.0}) == ""
    assert basket_holding_signature_v1({"35": 5.0, "34": 2.0}) == "34:2|35:5"
    # Dust is not a fill: whole shares are the venue's unit.
    assert basket_holding_signature_v1({"34": 0.4}) == "34:0"
    assert basket_holding_signature_v1({"34": 4.6}) == "34:5"


def test_the_two_older_arms_still_decide_on_their_own():
    """Narrowing is not what this clause does: a changed model or a changed
    admissibility is still a moment with the holding untouched."""

    assert _moment(probability_identity="p-2", basket_holding="34:5",
                   last_basket_holding="34:5")
    assert _moment(edge_state="none", basket_holding="34:5",
                   last_basket_holding="34:5")
    # And with no recorded decision at all, everything is a moment.
    assert _moment(last_probability_identity=None, basket_holding="",
                   last_basket_holding="")


def test_a_caller_with_no_holdings_view_keeps_the_two_arm_rule():
    """``basket_holding`` left None is what a caller that cannot see the
    chain honestly says, and it must not become "held nothing" -- that
    would make the first cycle a moment for every scope that holds
    anything."""

    assert not _moment()
    assert not _moment(basket_holding=None, last_basket_holding="34:5")


def test_production_calls_the_one_implementation():
    """Charter 110(七): "the basket changed" has one definition."""

    runtime = (
        ROOT / "bayes_basket_target" / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")
    assert "basket_holding_signature_v1(" in runtime
    # Production feeds it the snapshot's FILLED shares.  ``pending_shares``
    # is used elsewhere in that module and legitimately so -- charter 84
    # sizes against it -- so what is pinned here is what reaches THIS call.
    call = runtime.split("basket_holding = basket_holding_signature_v1(", 1)[1]
    fed = call.split(")", 1)[0]
    assert "filled_shares" in fed and "pending" not in fed, fed
