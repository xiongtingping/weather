from __future__ import annotations

from dataclasses import replace
import math

import pytest

from maker_pair_state_machine import (
    ActionKind,
    BucketBook,
    ExposureState,
    MakerPolicy,
    Phase,
    TopOfBook,
    exposure_violations,
    hedge_edge_floor,
    plan_no_hedge,
    plan_yes_entry,
    replacement_reason,
    select_top_buckets,
)


def _top(bid: float | None, ask: float | None, tick: float = 0.01) -> TopOfBook:
    return TopOfBook(best_bid=bid, best_ask=ask, tick_size=tick, bid_size=20, ask_size=20)


def _book(
    outcome: str,
    yes_bid: float,
    yes_ask: float,
    no_bid: float,
    no_ask: float,
    tick: float = 0.01,
) -> BucketBook:
    return BucketBook(
        outcome=outcome,
        yes_token=f"yes-{outcome}",
        no_token=f"no-{outcome}",
        yes=_top(yes_bid, yes_ask, tick),
        no=_top(no_bid, no_ask, tick),
        maker_fee_yes_per_share_usdc=0.0,
        maker_fee_no_per_share_usdc=0.0,
        fee_evidence_complete=True,
    )


def test_market_probability_selects_exactly_top_two_from_both_binary_books() -> None:
    books = [
        _book("37°C", 0.22, 0.24, 0.76, 0.78),
        _book("38°C", 0.35, 0.43, 0.57, 0.65),
        _book("39°C", 0.18, 0.22, 0.78, 0.82),
        BucketBook(
            "incomplete",
            "yi",
            "ni",
            _top(None, 0.2),
            _top(0.8, 0.9),
            0.0,
            0.0,
            True,
        ),
    ]
    assert math.isclose(books[1].market_yes_probability(), 0.39)
    assert select_top_buckets(books) == ("38°C", "37°C")
    with pytest.raises(ValueError, match="duplicate_bucket_outcome"):
        select_top_buckets([books[0], books[0]])


def test_yes_entry_is_top_two_stable_profitable_and_yes_first_only() -> None:
    policy = MakerPolicy()
    book = _book("38°C", 0.35, 0.48, 0.52, 0.65)
    state = ExposureState(outcome="38°C", selection_streak=3)
    action = plan_yes_entry(
        book=book,
        state=state,
        selected_outcomes=("38°C", "37°C"),
        policy=policy,
        global_worst_case_yes_shares=0.0,
        snapshot_age_seconds=0.1,
    )
    assert action.kind == ActionKind.PLACE
    assert action.phase == Phase.YES_WORKING
    assert action.quote is not None
    assert (action.quote.side, action.quote.action, action.quote.price, action.quote.shares) == (
        "YES",
        "BUY",
        0.36,
        5.0,
    )


def test_yes_entry_fails_closed_outside_top_two_or_without_pair_edge() -> None:
    policy = MakerPolicy()
    state = ExposureState(outcome="39°C", selection_streak=3)
    outside = plan_yes_entry(
        book=_book("39°C", 0.18, 0.22, 0.78, 0.82),
        state=state,
        selected_outcomes=("38°C", "37°C"),
        policy=policy,
        global_worst_case_yes_shares=0.0,
        snapshot_age_seconds=0.1,
    )
    assert outside.reason == "outside_current_top_two"
    no_edge = plan_yes_entry(
        book=_book("37°C", 0.22, 0.24, 0.76, 0.78),
        state=ExposureState(outcome="37°C", selection_streak=3),
        selected_outcomes=("38°C", "37°C"),
        policy=policy,
        global_worst_case_yes_shares=0.0,
        snapshot_age_seconds=0.1,
    )
    assert no_edge.reason == "pair_edge_below_threshold"
    incomplete_selection = plan_yes_entry(
        book=_book("38°C", 0.35, 0.48, 0.52, 0.65),
        state=ExposureState(outcome="38°C", selection_streak=3),
        selected_outcomes=("38°C",),
        policy=policy,
        global_worst_case_yes_shares=0.0,
        snapshot_age_seconds=0.1,
    )
    assert incomplete_selection.phase == Phase.PAUSED
    assert incomplete_selection.reason == "top_two_selection_incomplete"


def test_entry_requires_explicit_fee_evidence_and_includes_nonzero_fee() -> None:
    policy = MakerPolicy()
    base = _book("38°C", 0.35, 0.48, 0.52, 0.65)
    state = ExposureState(outcome="38°C", selection_streak=3)
    missing = plan_yes_entry(
        book=replace(base, fee_evidence_complete=False),
        state=state,
        selected_outcomes=("38°C", "37°C"),
        policy=policy,
        global_worst_case_yes_shares=0.0,
        snapshot_age_seconds=0.1,
    )
    assert (missing.phase, missing.reason) == (
        Phase.PAUSED,
        "maker_fee_evidence_incomplete",
    )
    with_fee = plan_yes_entry(
        book=replace(
            base,
            maker_fee_yes_per_share_usdc=0.054,
            maker_fee_no_per_share_usdc=0.052,
        ),
        state=state,
        selected_outcomes=("38°C", "37°C"),
        policy=policy,
        global_worst_case_yes_shares=0.0,
        snapshot_age_seconds=0.1,
    )
    assert with_fee.reason == "pair_edge_below_threshold"


def test_exposure_invariants_forbid_realized_and_potential_no_bias() -> None:
    policy = MakerPolicy()
    safe = ExposureState(
        outcome="38°C",
        phase=Phase.NO_HEDGING,
        yes_filled_shares=5,
        no_buy_open_shares=5,
        yes_fill_vwap=0.36,
    )
    assert exposure_violations([safe], policy) == ()
    potential_no = ExposureState(
        outcome="38°C",
        yes_filled_shares=4,
        no_buy_open_shares=5,
    )
    assert "potential_no_directional_exposure:38°C" in exposure_violations(
        [potential_no], policy
    )
    realized_no = ExposureState(outcome="38°C", yes_filled_shares=4, no_filled_shares=5)
    violations = exposure_violations([realized_no], policy)
    assert "realized_no_directional_exposure:38°C" in violations


def test_exposure_invariants_apply_per_bucket_and_global_caps() -> None:
    policy = MakerPolicy()
    too_large = ExposureState(outcome="38°C", yes_buy_open_shares=5.1)
    assert "single_leg_yes_cap_exceeded:38°C" in exposure_violations([too_large], policy)
    three = [
        ExposureState(outcome=str(index), yes_buy_open_shares=5)
        for index in range(3)
    ]
    assert "global_yes_exposure_cap_exceeded" in exposure_violations(three, policy)


def test_no_hedge_requires_verified_full_minimum_lot_and_never_exceeds_yes() -> None:
    policy = MakerPolicy()
    book = _book("38°C", 0.35, 0.48, 0.52, 0.65)
    partial = plan_no_hedge(
        book=book,
        state=ExposureState(
            outcome="38°C", yes_filled_shares=2, yes_fill_vwap=0.36
        ),
        policy=policy,
        exposure_age_seconds=1,
        snapshot_age_seconds=0.1,
    )
    assert partial.kind == ActionKind.HOLD
    assert partial.phase == Phase.YES_RESIDUAL_HOLD
    assert partial.reason == "subminimum_yes_residual_hold_to_settlement"
    hedge = plan_no_hedge(
        book=book,
        state=ExposureState(
            outcome="38°C", yes_filled_shares=5, yes_fill_vwap=0.36
        ),
        policy=policy,
        exposure_age_seconds=1,
        snapshot_age_seconds=0.1,
    )
    assert hedge.kind == ActionKind.PLACE
    assert hedge.phase == Phase.NO_HEDGING
    assert hedge.quote is not None
    assert (hedge.quote.side, hedge.quote.price, hedge.quote.shares) == ("NO", 0.53, 5)


def test_partial_yes_keeps_safe_remainder_until_timeout_then_cancels_first() -> None:
    policy = MakerPolicy()
    book = _book("38°C", 0.35, 0.48, 0.52, 0.65)
    state = ExposureState(
        outcome="38°C",
        yes_filled_shares=2,
        yes_buy_open_shares=3,
        yes_fill_vwap=0.36,
    )
    working = plan_no_hedge(
        book=book,
        state=state,
        policy=policy,
        exposure_age_seconds=10,
        snapshot_age_seconds=0.1,
    )
    assert (working.kind, working.phase, working.reason) == (
        ActionKind.WAIT,
        Phase.YES_WORKING,
        "complete_minimum_yes_lot_before_hedging",
    )
    timed_out = plan_no_hedge(
        book=book,
        state=state,
        policy=policy,
        exposure_age_seconds=30,
        snapshot_age_seconds=0.1,
    )
    assert timed_out.kind == ActionKind.CANCEL
    assert timed_out.reason == "cancel_yes_remainder_before_hedging"


def test_no_hedge_price_is_capped_by_realized_yes_vwap_and_edge_floor() -> None:
    policy = MakerPolicy()
    book = _book("38°C", 0.35, 0.48, 0.69, 0.80)
    hedge = plan_no_hedge(
        book=book,
        state=ExposureState(
            outcome="38°C", yes_filled_shares=5, yes_fill_vwap=0.36
        ),
        policy=policy,
        exposure_age_seconds=1,
        snapshot_age_seconds=0.1,
    )
    assert hedge.quote is not None
    assert hedge.quote.price == 0.63
    assert hedge.quote.quote_mode == "edge_capped_passive_bid"
    assert hedge.quote.price <= 1.0 - 0.36 - policy.minimum_edge_per_share_usdc


def test_hedge_edge_relaxes_only_to_break_even_then_exits_yes_maker() -> None:
    policy = MakerPolicy()
    assert hedge_edge_floor(0, policy) == policy.minimum_edge_per_share_usdc
    assert 0 < hedge_edge_floor(30, policy) < policy.minimum_edge_per_share_usdc
    assert hedge_edge_floor(60, policy) is None
    exit_action = plan_no_hedge(
        book=_book("38°C", 0.35, 0.48, 0.52, 0.65),
        state=ExposureState(
            outcome="38°C", yes_filled_shares=5, yes_fill_vwap=0.36
        ),
        policy=policy,
        exposure_age_seconds=60,
        snapshot_age_seconds=0.1,
    )
    assert exit_action.phase == Phase.EXITING_YES
    assert exit_action.quote is not None
    assert (exit_action.quote.side, exit_action.quote.action, exit_action.quote.price) == (
        "YES",
        "SELL",
        0.47,
    )


def test_stale_snapshot_cancels_open_risk_and_invalid_exposure_reconciles() -> None:
    policy = MakerPolicy()
    book = _book("38°C", 0.35, 0.48, 0.52, 0.65)
    stale = plan_no_hedge(
        book=book,
        state=ExposureState(
            outcome="38°C",
            yes_filled_shares=5,
            no_buy_open_shares=5,
            yes_fill_vwap=0.36,
        ),
        policy=policy,
        exposure_age_seconds=1,
        snapshot_age_seconds=2.1,
    )
    assert (stale.kind, stale.phase, stale.reason) == (
        ActionKind.CANCEL,
        Phase.PAUSED,
        "market_ws_snapshot_stale",
    )
    invalid = plan_no_hedge(
        book=book,
        state=ExposureState(
            outcome="38°C",
            yes_filled_shares=4,
            no_buy_open_shares=5,
            yes_fill_vwap=0.36,
        ),
        policy=policy,
        exposure_age_seconds=1,
        snapshot_age_seconds=0.1,
    )
    assert (invalid.kind, invalid.phase, invalid.reason) == (
        ActionKind.RECONCILE,
        Phase.RECONCILE_REQUIRED,
        "exposure_invariant_invalid",
    )


def test_cancel_replace_safety_is_immediate_but_price_chasing_is_throttled() -> None:
    common = {
        "side": "YES",
        "action": "BUY",
        "current_price": 0.40,
        "desired_price": 0.41,
        "best_opposite_price": 0.50,
        "tick_size": 0.01,
        "minimum_rest_seconds": 1.5,
        "selected": True,
        "economics_valid": True,
        "snapshot_fresh": True,
    }
    assert replacement_reason(rest_age_seconds=1.0, **common) is None
    assert replacement_reason(rest_age_seconds=2.0, **common) == "price_drift_one_tick_or_more"
    dropped = {**common, "selected": False}
    assert replacement_reason(rest_age_seconds=0.1, **dropped) == "bucket_left_top_two"
    crossing = {**common, "current_price": 0.50}
    assert replacement_reason(rest_age_seconds=0.1, **crossing) == "maker_order_would_cross"
