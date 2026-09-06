from __future__ import annotations

from dataclasses import replace

import pytest

from bayes_basket_target import (
    BasketCashPhaseInput,
    BasketCashPhasePolicyArtifact,
    CashPhase,
    ContractError,
    OrderSizingDecision,
    evaluate_cash_phase,
    resolve_cash_phase_policy,
)


def _policy(
    *,
    version: str = "cash-test-v1",
    timezone_name: str = "Asia/Shanghai",
    cash_by_phase: dict[str, float] | None = None,
    valid_from: str = "2026-07-01T00:00:00Z",
    valid_until: str = "2026-08-01T00:00:00Z",
) -> BasketCashPhasePolicyArtifact:
    return BasketCashPhasePolicyArtifact.create(
        policy_version=version,
        station_id="ZUUU",
        station_timezone=timezone_name,
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase=cash_by_phase or {"HEAVY": 6.0, "STANDARD": 4.0, "TAIL": 1.0},
        valid_from_utc=valid_from,
        valid_until_utc=valid_until,
        evidence_status="DESIGN_CANDIDATE",
    )


def _input(
    policy: BasketCashPhasePolicyArtifact,
    *,
    current: CashPhase = CashPhase.HEAVY,
    observed: str = "2026-07-23T00:00:00Z",
    event_date: str = "2026-07-23",
    timezone_name: str | None = None,
    checkpoint: str = "cash-test-checkpoint",
) -> BasketCashPhaseInput:
    return BasketCashPhaseInput.from_mapping({
        "policy_version": policy.policy_version,
        "station_id": policy.station_id,
        "current_phase": current.value,
        "event_date_local": event_date,
        "observed_at_utc": observed,
        "station_timezone": timezone_name or policy.station_timezone,
        "checkpoint_id": checkpoint,
    })


def test_policy_content_addressing_and_unknown_version_fail_closed() -> None:
    policy = _policy()
    assert BasketCashPhasePolicyArtifact.from_mapping(policy.to_dict()) == policy
    bad_sha = {**policy.to_dict(), "content_sha256": "b" * 64}
    with pytest.raises(ContractError, match="content_sha_mismatch"):
        BasketCashPhasePolicyArtifact.from_mapping(bad_sha)
    with pytest.raises(ContractError, match="policy_version_mismatch"):
        evaluate_cash_phase(policy, _input(replace(_input(policy), policy_version="other")))
    with pytest.raises(ContractError, match="promoted_invalid"):
        BasketCashPhasePolicyArtifact.create(
            policy_version="cash-test-bad-bool",
            station_id="ZUUU",
            station_timezone="Asia/Shanghai",
            heavy_to_standard_local_time="19:54",
            standard_to_tail_local_time="09:58",
            cash_by_phase={"HEAVY": 6.0, "STANDARD": 4.0, "TAIL": 1.0},
            valid_from_utc="2026-07-01T00:00:00Z",
            valid_until_utc="2026-08-01T00:00:00Z",
            evidence_status="DESIGN_CANDIDATE",
            promoted="false",
        )


def test_unknown_or_missing_iana_timezone_and_naive_time_fail_closed() -> None:
    with pytest.raises(ContractError, match="unknown_iana_timezone"):
        _policy(timezone_name="Mars/Phobos")
    policy = _policy()
    with pytest.raises(ContractError, match="unknown_iana_timezone"):
        _input(policy, timezone_name="Mars/Phobos")
    with pytest.raises(ContractError, match="timezone_required"):
        _input(policy, observed="2026-07-23T00:00:00")


def test_valid_window_and_unobserved_phase_fail_closed() -> None:
    policy = _policy()
    with pytest.raises(ContractError, match="outside_validity"):
        evaluate_cash_phase(policy, _input(policy, observed="2026-08-01T00:00:00Z"))
    partial = _policy(cash_by_phase={"STANDARD": 4.0, "TAIL": 1.0})
    with pytest.raises(ContractError, match="unobserved_phase"):
        evaluate_cash_phase(
            partial,
            _input(partial, observed="2026-07-22T10:00:00Z"),
        )


def test_local_clock_boundaries_are_inclusive_then_exclusive() -> None:
    policy = _policy()
    before = evaluate_cash_phase(
        policy,
        _input(policy, observed="2026-07-22T11:53:59Z"),  # 19:53:59 local
    )
    at_standard = evaluate_cash_phase(
        policy,
        _input(policy, observed="2026-07-22T11:54:00Z"),  # D-1 19:54 local
    )
    at_tail = evaluate_cash_phase(
        policy,
        _input(policy, observed="2026-07-23T01:58:00Z"),  # D 09:58 local
    )
    assert before.clock_phase is CashPhase.HEAVY
    assert at_standard.clock_phase is CashPhase.STANDARD
    assert at_tail.clock_phase is CashPhase.TAIL
    assert at_standard.cash_template_usdc == 4.0
    assert at_tail.cash_template_usdc == 1.0


def test_dst_fall_back_uses_station_local_zoneinfo() -> None:
    policy = _policy(
        timezone_name="America/New_York",
        valid_from="2026-10-01T00:00:00Z",
        valid_until="2026-12-01T00:00:00Z",
    )
    standard = evaluate_cash_phase(
        policy,
        _input(
            policy,
            event_date="2026-11-01",
            observed="2026-11-01T13:58:00Z",  # 08:58 EST
        ),
    )
    tail = evaluate_cash_phase(
        policy,
        _input(
            policy,
            event_date="2026-11-01",
            observed="2026-11-01T14:58:00Z",  # 09:58 EST
        ),
    )
    assert standard.clock_phase is CashPhase.STANDARD
    assert tail.clock_phase is CashPhase.TAIL
    assert standard.observed_at_local.endswith("-05:00")
    assert tail.observed_at_local.endswith("-05:00")


def test_phase_transition_is_monotonic_under_time_reversal() -> None:
    policy = _policy()
    heavy_to_standard = evaluate_cash_phase(
        policy,
        _input(policy, current=CashPhase.HEAVY, observed="2026-07-22T11:54:00Z"),
    )
    held_after_reversal = evaluate_cash_phase(
        policy,
        _input(policy, current=heavy_to_standard.next_phase, observed="2026-07-22T11:53:00Z"),
    )
    tail_to_reversal = evaluate_cash_phase(
        policy,
        _input(policy, current=CashPhase.TAIL, observed="2026-07-22T11:53:00Z"),
    )
    assert heavy_to_standard.next_phase is CashPhase.STANDARD
    assert held_after_reversal.next_phase is CashPhase.STANDARD
    assert held_after_reversal.reason_code == "MONOTONIC_HOLD_NO_REGRESSION"
    assert tail_to_reversal.next_phase is CashPhase.TAIL


def test_repeated_forward_backward_cross_day_warp_keeps_phase_monotonic() -> None:
    policy = _policy()
    observations = [
        "2026-07-22T11:53:59Z",
        "2026-07-22T11:54:00Z",
        "2026-07-22T11:53:59Z",
        "2026-07-23T01:58:00Z",
        "2026-07-23T01:57:59Z",
        "2026-07-22T11:54:00Z",
        "2026-07-24T12:00:00Z",
    ]
    phases = []
    current = CashPhase.HEAVY
    for index, observed in enumerate(observations):
        result = evaluate_cash_phase(
            policy,
            _input(
                policy,
                current=current,
                observed=observed,
                event_date="2026-07-23",
                checkpoint=f"warp-{index}",
            ),
        )
        assert result.previous_phase is current
        if index == 1:
            assert result.next_phase is CashPhase.STANDARD
        if index >= 3:
            assert result.next_phase is CashPhase.TAIL
        phases.append(result.next_phase)
        current = result.next_phase
    assert phases == [CashPhase.HEAVY, CashPhase.STANDARD, CashPhase.STANDARD, CashPhase.TAIL, CashPhase.TAIL, CashPhase.TAIL, CashPhase.TAIL]


def test_order_sizing_formula_and_zero_candidate_boundaries() -> None:
    policy = _policy()
    phase = evaluate_cash_phase(policy, _input(policy))
    sized = OrderSizingDecision.compute(
        bucket="38",
        phase_decision=phase,
        delta_shares=100.0,
        r_limit=0.20,
        remaining_budget=10.0,
        executable_depth_cash=12.0,
        candidate_present=True,
    )
    assert sized.c_base == 4.0
    assert sized.c_order == pytest.approx(min(4.0, 0.20 * 100.0, 10.0, 12.0))
    assert sized.q_order == pytest.approx(sized.c_order / sized.r_limit)
    assert sized.remaining_budget_after == pytest.approx(6.0)

    cases = [
        {"delta_shares": 0.0, "r_limit": 0.20, "remaining_budget": 10.0, "executable_depth_cash": 12.0, "candidate_present": True},
        {"delta_shares": 100.0, "r_limit": 0.20, "remaining_budget": 0.0, "executable_depth_cash": 12.0, "candidate_present": True},
        {"delta_shares": 100.0, "r_limit": 0.20, "remaining_budget": 10.0, "executable_depth_cash": 0.0, "candidate_present": True},
        {"delta_shares": 100.0, "r_limit": 0.20, "remaining_budget": 10.0, "executable_depth_cash": 12.0, "candidate_present": False},
    ]
    for case in cases:
        result = OrderSizingDecision.compute(bucket="38", phase_decision=phase, **case)
        assert result.c_order == 0.0
        assert result.q_order == 0.0
        assert result.eligible is False
    with pytest.raises(ContractError, match="candidate_present_invalid"):
        OrderSizingDecision.compute(
            bucket="38",
            phase_decision=phase,
            delta_shares=1.0,
            r_limit=0.20,
            remaining_budget=1.0,
            executable_depth_cash=1.0,
            candidate_present="false",
        )


def test_discrete_clip_closes_target_gap_budget_depth_and_promotion_caps() -> None:
    policy = _policy(cash_by_phase={"HEAVY": 10.0, "STANDARD": 10.0, "TAIL": 10.0})
    phase = evaluate_cash_phase(policy, _input(policy))
    sized = OrderSizingDecision.compute(
        bucket="38",
        phase_decision=phase,
        delta_shares=100.0,
        r_limit=0.30,
        remaining_budget=20.0,
        executable_depth_cash=8.0,
        candidate_present=True,
        target_clip_notional_usdc=4.10,
        minimum_clip_notional_usdc=1.00,
        share_quantum=0.10,
    )
    assert sized.eligible is True
    assert sized.raw_c_order == pytest.approx(4.10)
    assert sized.q_order == pytest.approx(13.60)
    assert sized.c_order == pytest.approx(sized.q_order * sized.r_limit)
    assert sized.c_order <= 4.10 + 1e-9
    assert sized.q_order <= sized.delta_shares + 1e-9
    assert sized.q_order <= sized.depth_shares_cap + 1e-9

    below_minimum = OrderSizingDecision.compute(
        bucket="38",
        phase_decision=phase,
        delta_shares=100.0,
        r_limit=0.30,
        remaining_budget=20.0,
        executable_depth_cash=0.95,
        candidate_present=True,
        target_clip_notional_usdc=4.10,
        minimum_clip_notional_usdc=1.00,
        share_quantum=0.10,
    )
    assert below_minimum.eligible is False
    assert below_minimum.reason_code == "clip_below_minimum"
    assert below_minimum.c_order == 0.0
    assert below_minimum.remaining_budget_after == pytest.approx(20.0)


def test_delta_v2_target_clip_is_not_replaced_by_legacy_cash_template() -> None:
    policy = _policy(
        cash_by_phase={
            "HEAVY": 0.25,
            "STANDARD": 0.25,
            "TAIL": 0.25,
        }
    )
    phase = evaluate_cash_phase(policy, _input(policy))
    sized = OrderSizingDecision.compute(
        bucket="40°C",
        phase_decision=phase,
        delta_shares=232.2222222222,
        r_limit=0.004,
        remaining_budget=12.0,
        executable_depth_cash=2.26,
        candidate_present=True,
        target_clip_notional_usdc=0.9288888888,
        minimum_clip_notional_usdc=0.01,
        cash_template_caps_target=False,
        share_quantum=0.01,
    )

    assert sized.cash_template_caps_target is False
    assert sized.c_base == pytest.approx(0.25)
    assert sized.raw_c_order == pytest.approx(0.9288888888)
    assert sized.c_order == pytest.approx(0.92888)
    assert sized.q_order == pytest.approx(232.22)
    assert sized.eligible is True
    assert sized.reason_code == "sized_discrete"


def test_discrete_clip_is_monotonic_and_rejects_invalid_quantum() -> None:
    policy = _policy(cash_by_phase={"HEAVY": 10.0, "STANDARD": 10.0, "TAIL": 10.0})
    phase = evaluate_cash_phase(policy, _input(policy))

    def sized(budget: float, depth: float, gap: float) -> OrderSizingDecision:
        return OrderSizingDecision.compute(
            bucket="38",
            phase_decision=phase,
            delta_shares=gap,
            r_limit=0.25,
            remaining_budget=budget,
            executable_depth_cash=depth,
            candidate_present=True,
            target_clip_notional_usdc=10.0,
            minimum_clip_notional_usdc=0.25,
            share_quantum=0.01,
        )

    baseline = sized(5.0, 5.0, 100.0)
    assert sized(6.0, 5.0, 100.0).c_order + 1e-9 >= baseline.c_order
    assert sized(5.0, 6.0, 100.0).c_order + 1e-9 >= baseline.c_order
    assert sized(5.0, 5.0, 110.0).c_order + 1e-9 >= baseline.c_order
    with pytest.raises(ContractError, match="share_quantum_nonpositive"):
        OrderSizingDecision.compute(
            bucket="38",
            phase_decision=phase,
            delta_shares=1.0,
            r_limit=0.25,
            remaining_budget=1.0,
            executable_depth_cash=1.0,
            candidate_present=True,
            share_quantum=0.0,
        )


def test_cash_policy_resolution_requires_one_exact_valid_artifact() -> None:
    policy = _policy()
    assert resolve_cash_phase_policy(
        [policy],
        policy_version=policy.policy_version,
        station_id=policy.station_id,
        observed_at_utc="2026-07-23T00:00:00Z",
        required_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
    ) == policy
    with pytest.raises(
        ContractError,
        match="formal_resolution_requires_manifest_and_oos_receipt",
    ):
        resolve_cash_phase_policy(
            [policy],
            policy_version=policy.policy_version,
            station_id=policy.station_id,
            observed_at_utc="2026-07-23T00:00:00Z",
            required_authority_scope="FORMAL_PROMOTED",
        )
    with pytest.raises(ContractError, match="resolution_not_unique"):
        resolve_cash_phase_policy(
            [policy, policy],
            policy_version=policy.policy_version,
            station_id=policy.station_id,
            observed_at_utc="2026-07-23T00:00:00Z",
            required_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )


def test_a_leg_cut_below_its_authorized_clip_is_refused_not_shrunk() -> None:
    # The objective authorized a clip on a joint basket shape.  When a cap
    # other than that clip binds, sizing must refuse rather than serve a
    # smaller leg nobody scored -- floor, CVaR and concentration are joint.
    policy = _policy(cash_by_phase={"HEAVY": 10.0, "STANDARD": 10.0, "TAIL": 10.0})
    phase = evaluate_cash_phase(policy, _input(policy))
    common = {
        "bucket": "38",
        "phase_decision": phase,
        "delta_shares": 100.0,
        "r_limit": 0.30,
        "candidate_present": True,
        "target_clip_notional_usdc": 4.10,
        "minimum_clip_notional_usdc": 1.00,
        "cash_template_caps_target": False,
        "share_quantum": 0.10,
    }
    clamped = OrderSizingDecision.compute(
        remaining_budget=20.0, executable_depth_cash=3.00, **common
    )
    assert clamped.eligible is False
    assert clamped.reason_code == "sizing_clamped_below_authorized_clip"
    assert clamped.c_order == 0.0
    assert clamped.remaining_budget_after == pytest.approx(20.0)

    # Share-quantum rounding is the one permitted difference: it is bounded
    # by a quantum and cannot re-shape a basket.
    rounded = OrderSizingDecision.compute(
        remaining_budget=20.0, executable_depth_cash=99.0, **common
    )
    assert rounded.eligible is True
    assert rounded.reason_code == "sized_discrete"
    assert rounded.c_order < 4.10
    assert rounded.c_order > 4.10 - 0.10 * 0.30 - 1e-9

    # The legacy cadence path keeps capping by its cash template.
    legacy = OrderSizingDecision.compute(
        remaining_budget=20.0,
        executable_depth_cash=3.00,
        **{**common, "cash_template_caps_target": True},
    )
    assert legacy.eligible is True
    assert legacy.c_order <= 3.00 + 1e-9

    # Once the caller has put the cut sizes back through the bundle
    # evaluation and the same gates passed, the cut basket sizes -- and says
    # in its reason code that it did so on a re-score, not on the original
    # authorization.
    rescored = OrderSizingDecision.compute(
        remaining_budget=20.0,
        executable_depth_cash=3.00,
        **{**common, "clamped_clip_authorized": True},
    )
    assert rescored.eligible is True
    assert rescored.reason_code == "sized_discrete_after_rescore"
    assert rescored.c_order <= 3.00 + 1e-9
    assert rescored.c_order > 0.0

    # The permission does not reach a leg nothing cut: an uncut leg still
    # reports the ordinary reason.
    assert OrderSizingDecision.compute(
        remaining_budget=20.0,
        executable_depth_cash=99.0,
        **{**common, "clamped_clip_authorized": True},
    ).reason_code == "sized_discrete"

    with pytest.raises(ContractError, match="clamped_clip_authorized_invalid"):
        OrderSizingDecision.compute(
            remaining_budget=20.0,
            executable_depth_cash=3.00,
            **{**common, "clamped_clip_authorized": "yes"},
        )


def test_a_maker_bid_is_not_sized_by_the_offer_it_never_touches() -> None:
    from bayes_basket_target.basket_kernel.contracts import AskLevel
    from bayes_basket_target.runtime import _executable_depth_cash

    offer = (AskLevel(price=0.30, shares=5.0), AskLevel(price=0.40, shares=50.0))

    # Taker: the offer at or below its limit is exactly what it can lift.
    assert _executable_depth_cash(
        ask_levels=offer, execution_candidate="TAKER_FOK",
        limit_price=0.30, cash_clip=99.0,
    ) == pytest.approx(1.50)

    # Maker: bounded by its own clip, never by the offer -- including on a
    # book with no offer at all, where a resting bid is all that is left.
    assert _executable_depth_cash(
        ask_levels=offer, execution_candidate="MAKER",
        limit_price=0.25, cash_clip=4.10,
    ) == pytest.approx(4.10)
    assert _executable_depth_cash(
        ask_levels=(), execution_candidate="MAKER",
        limit_price=0.25, cash_clip=4.10,
    ) == pytest.approx(4.10)


def test_a_station_may_never_spend_past_its_own_bankroll() -> None:
    # The pool's remaining balance is the whole book's, and the allocator
    # hands it to a station unchanged whenever that station has no history
    # to split on -- 514.29 to each of forty-five stations against a book
    # that holds 514.29 once.  A station spending past its own allotment
    # does not merely over-commit the book, it breaks its own objective: the
    # curve is scored against that allotment, so once the committed cost
    # passes the effective bankroll every candidate scores minus infinity,
    # buying nothing included, and the station holds forever with no reason
    # code.  Measured: 20 spent still traded, 90 already held, 121 was the
    # wall.  The cap makes the wall unreachable rather than unlikely.
    from bayes_basket_target.runtime import _station_capital_usdc

    # Nothing committed: the whole allotment, never the pool's 514.29.
    assert _station_capital_usdc(
        station_bankroll_usdc=30.0, committed_usdc=0.0
    ) == pytest.approx(30.0)

    # Committed cost -- filled and confirmed pending alike -- spends it.
    assert _station_capital_usdc(
        station_bankroll_usdc=30.0, committed_usdc=18.5
    ) == pytest.approx(11.5)

    # Spent out is zero, never negative: a negative capital would fail a
    # nonnegative contract downstream instead of simply buying nothing.
    assert _station_capital_usdc(
        station_bankroll_usdc=30.0, committed_usdc=30.0
    ) == 0.0
    assert _station_capital_usdc(
        station_bankroll_usdc=30.0, committed_usdc=99.0
    ) == 0.0

    # The ceiling follows the resolved policy, so a station allotted more
    # needs no second dial kept in sync.
    assert _station_capital_usdc(
        station_bankroll_usdc=500.0, committed_usdc=18.5
    ) == pytest.approx(481.5)

    # The property that matters: committed can never exceed the bankroll, so
    # the worst settlement stays a fraction of the effective bankroll W/f
    # and curve_score can never return minus infinity.
    from bayes_basket_target.basket_kernel.delta_v2 import (
        DEFAULT_KELLY_FRACTION,
    )

    bankroll = 30.0
    committed = 0.0
    for _round in range(50):
        allowed = _station_capital_usdc(
            station_bankroll_usdc=bankroll, committed_usdc=committed
        )
        committed += min(allowed, 4.0)
    assert committed <= bankroll + 1e-9
    worst_wealth = 1.0 - committed / (bankroll / DEFAULT_KELLY_FRACTION)
    assert worst_wealth >= 0.75 - 1e-9


def test_leg_under_the_exchange_minimum_lot_is_raised_to_one_lot() -> None:
    """User ruling 2026-08-31 -- 多花: spend more, do not re-pick the rungs.

    The numbers here are a real ladder (decision_b6dd5e95ca0): 37/38/39C
    posted contiguous, with the centre rung quoted 0.94 and sized to 1.05
    shares -- about a ninth of a lot.  Dropping that leg, which is what this
    layer did for one day, left the two wings and threw away the bucket the
    market put 94% of the mass on.  Re-selecting a cheaper window would
    change what the objective picked, and picking is its job.  So the leg is
    raised instead, and what the raise costs is recorded rather than hidden
    inside the clip.
    """
    policy = _policy()
    phase = evaluate_cash_phase(policy, _input(policy))

    def sized(**overrides):
        kwargs = {
            "bucket": "38",
            "phase_decision": phase,
            "delta_shares": 1.05,
            "r_limit": 0.94,
            "remaining_budget": 10.0,
            "executable_depth_cash": 12.0,
            "candidate_present": True,
            "minimum_lot_shares": 5.0,
        }
        kwargs.update(overrides)
        return OrderSizingDecision.compute(**kwargs)

    raised = sized()
    assert raised.reason_code == "raised_to_exchange_minimum_lot"
    assert raised.eligible is True
    assert raised.q_order == pytest.approx(5.0)
    assert raised.c_order == pytest.approx(4.70)
    # The gap was 1.05 shares; buying a legal lot overshoots it, and that
    # overshoot is the "more" the ruling asks for.
    assert raised.delta_shares == pytest.approx(1.05)
    assert raised.lot_raise_usdc == pytest.approx(4.70 - 1.05 * 0.94)
    assert raised.remaining_budget_after == pytest.approx(10.0 - 4.70)

    # The clip the objective authorized is exceeded on purpose.  Nothing else
    # in this function may exceed it, which is why the raise is recorded.
    over_clip = sized(target_clip_notional_usdc=0.987, cash_template_caps_target=False,
                      clamped_clip_authorized=True)
    assert over_clip.eligible is True
    assert over_clip.c_order == pytest.approx(4.70)
    assert over_clip.c_order > over_clip.target_clip_notional_usdc

    # Budget and depth are not policy.  A lot that does not exist cannot be
    # bought, and the leg says so in its own words rather than looking like a
    # budget refusal.
    for label, kwargs in (
        ("budget", {"remaining_budget": 2.0}),
        ("depth", {"executable_depth_cash": 2.0}),
    ):
        broke = sized(**kwargs)
        assert broke.reason_code == "exchange_minimum_lot_unaffordable", label
        assert broke.eligible is False, label
        assert broke.c_order == 0.0, label
        assert broke.lot_raise_usdc == 0.0, label

    # A leg already at or above a lot is untouched, and the boundary is
    # inclusive.
    for delta in (5.0, 20.0):
        untouched = sized(delta_shares=delta, r_limit=0.20)
        assert untouched.reason_code != "raised_to_exchange_minimum_lot"
        assert untouched.eligible is True
        assert untouched.lot_raise_usdc == 0.0

    # Without this the first assertion would also pass on a fixture that was
    # being raised for some other reason.
    no_floor = sized(minimum_lot_shares=0.0)
    assert no_floor.eligible is True
    assert no_floor.q_order == pytest.approx(1.05)
    assert no_floor.lot_raise_usdc == 0.0

    # The floor is shares, so the cash it demands moves with the price: the
    # same lot on a two-cent rung costs a fortieth of what it costs here.
    cheap = sized(delta_shares=1.0, r_limit=0.02)
    assert cheap.eligible is True
    assert cheap.c_order == pytest.approx(0.10)

    assert "lot_raise_usdc" in raised.to_dict()
    assert raised.content_sha256 != no_floor.content_sha256


def test_the_restated_share_size_sits_on_the_grid_it_was_sized_on() -> None:
    """5 shares at 0.47 must leave as 5.0, not 4.999999999999999.

    The cash/share identity restates shares off the cash; 5.0 * 0.47 is
    2.3499999999999996 and dividing that back gave the venue a request it
    floored to 4.99 (KAUS 98-99°F, 2026-09-05), which the ledger then read as
    a partial fill of a five-share request.
    """

    policy = _policy(cash_by_phase={"HEAVY": 10.0, "STANDARD": 10.0, "TAIL": 10.0})
    phase = evaluate_cash_phase(policy, _input(policy))
    sized = OrderSizingDecision.compute(
        bucket="98-99°F",
        phase_decision=phase,
        delta_shares=5.0,
        r_limit=0.47,
        remaining_budget=10.0,
        executable_depth_cash=12.0,
        candidate_present=True,
        minimum_lot_shares=5.0,
    )
    assert sized.eligible is True
    assert repr(sized.q_order) == "5.0"
    assert sized.c_order == pytest.approx(2.35)
