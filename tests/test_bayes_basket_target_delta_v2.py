from __future__ import annotations

import copy

import pytest

from bayes_basket_target.basket_kernel.contracts import ContractError
from bayes_basket_target.basket_kernel.delta_v2 import (
    ACTION_EVALUATION_SCHEMA,
    ExecutionWeightsV2,
    ValueWeightsV2,
    allocate_cross_event_bundle_capital_v2,
    evaluate_action_v2,
    evaluate_basket_value_v2,
    select_action_v2,
    rescore_maker_bundle_v2,
    select_complete_maker_bundle_v2,
    solve_reservation_price_v2,
    solve_target_inventory_v2,
)


def _value_weights(**overrides: float) -> ValueWeightsV2:
    raw = {
        "lambda_profit_floor": 0.0,
        "lambda_cvar": 0.0,
        "lambda_shortfall": 0.0,
        "lambda_breakthrough": 0.0,
        "lambda_overfill": 0.0,
        "lambda_excess_hhi": 0.0,
        "cvar_alpha": 0.25,
        "hhi_max": 0.75,
        "expected_pnl_scale": 1.0,
        "profit_floor_scale": 1.0,
        "cvar_scale": 1.0,
        "coverage_scale": 1.0,
        "overfill_scale": 1.0,
        "excess_hhi_scale": 1.0,
    }
    raw.update(overrides)
    return ValueWeightsV2.from_mapping(raw)


def _execution_weights(**overrides: float) -> ExecutionWeightsV2:
    raw = {
        "epsilon_score": 0.0001,
        "lambda_wait": 1.0,
        "lambda_queue": 1.0,
        "lambda_adverse_selection": 1.0,
        "lambda_reprice": 1.0,
        "lambda_capital_lock": 1.0,
        "wait_scale": 1.0,
        "queue_scale": 1.0,
        "adverse_selection_scale": 1.0,
        "reprice_scale": 1.0,
        "capital_lock_scale": 1.0,
        "maximum_candidates": 16,
    }
    raw.update(overrides)
    return ExecutionWeightsV2.from_mapping(raw)


def _gates(**overrides: bool) -> dict[str, bool]:
    value = {
        "unique_ssot": True,
        "lineage_complete": True,
        "registry_timezone_matches": True,
        "bucket_contract_matches": True,
        "official_ws_fresh": True,
        "inventory_known": True,
        "order_state_known": True,
        "positive_inventory_gap": True,
        "budget_ok": True,
        "profit_floor_ok": True,
        "resource_probe_ok": True,
        "reservation_price_ok": True,
    }
    value.update(overrides)
    return value


def _action_kwargs(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "event_id": "RKPK-2026-07-28",
        "bucket": "36",
        "cash_clip": 1.0,
        "limit_price": 0.10,
        "timeout_seconds": 60.0,
        "order_mode": "MAKER",
        "fill_distribution": [
            {"fill_fraction": 0.0, "probability": 0.2},
            {"fill_fraction": 0.5, "probability": 0.4},
            {"fill_fraction": 1.0, "probability": 0.4},
        ],
        "probabilities": {"35": 0.4, "36": 0.6},
        "current_shares_by_bucket": {"35": 0.0, "36": 0.0},
        "current_cost_by_bucket": {"35": 0.0, "36": 0.0},
        "target_shares_by_bucket": {"35": 0.0, "36": 12.0},
        "protected_buckets": ["35", "36"],
        "shortfall_buckets": ["35"],
        "breakthrough_buckets": [],
        "value_weights": _value_weights(),
        "execution_weights": _execution_weights(),
        "hard_gates": _gates(),
        "capital_available_usdc": 12.0,
        "minimum_profit_floor": -12.0,
        "reservation_price": 0.8,
        "wait_cost": 0.0,
        "queue_risk": 0.0,
        "adverse_selection": 0.0,
        "reprice_risk": 0.0,
        "capital_lock_cost": 0.0,
        "fee_rate": 0.0,
    }
    value.update(overrides)
    return value


def test_value_v2_has_exact_scenario_curve_expected_pnl_and_lower_cvar() -> None:
    result = evaluate_basket_value_v2(
        probabilities={"cold": 0.25, "warm": 0.75},
        shares_by_bucket={"cold": 1.0, "warm": 3.0},
        cost_by_bucket={"cold": 0.2, "warm": 0.6},
        protected_buckets=["cold", "warm"],
        shortfall_buckets=["cold"],
        breakthrough_buckets=["warm"],
        weights=_value_weights(lambda_cvar=1.0),
    )

    assert result["scenario_pnl"] == pytest.approx(
        {"cold": 0.2, "warm": 2.2}
    )
    assert result["expected_pnl"] == pytest.approx(1.7)
    assert result["cvar"] == pytest.approx(0.2)
    assert result["value"] == pytest.approx(1.9)


def test_hhi_penalty_only_applies_above_limit_and_overfill_only_above_target() -> None:
    weights = _value_weights(
        lambda_overfill=1.0,
        lambda_excess_hhi=1.0,
        hhi_max=0.5,
    )
    balanced = evaluate_basket_value_v2(
        probabilities={"a": 0.5, "b": 0.5},
        shares_by_bucket={"a": 1.0, "b": 1.0},
        cost_by_bucket={},
        protected_buckets=["a", "b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=weights,
        target_shares_by_bucket={"a": 1.0, "b": 1.0},
    )
    concentrated = evaluate_basket_value_v2(
        probabilities={"a": 0.5, "b": 0.5},
        shares_by_bucket={"a": 3.0, "b": 0.0},
        cost_by_bucket={},
        protected_buckets=["a", "b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=weights,
        target_shares_by_bucket={"a": 2.0, "b": 0.0},
    )

    assert balanced["excess_hhi"] == 0.0
    assert balanced["overfill"] == 0.0
    assert concentrated["excess_hhi"] == pytest.approx(0.25)
    assert concentrated["overfill"] == pytest.approx(1.0)


def test_target_inventory_is_exact_argmax_not_second_target_objective() -> None:
    # Both live candidates cover the whole support so the floor is not what
    # separates them; the assertion stays what it always was, exact argmax.
    result = solve_target_inventory_v2(
        probabilities={"a": 0.2, "b": 0.8},
        feasible_inventory_candidates=[
            {
                "candidate_id": "hold",
                "shares_by_bucket": {},
                "cost_by_bucket": {},
            },
            {
                "candidate_id": "lean-a",
                "shares_by_bucket": {"a": 2.0, "b": 1.0},
                "cost_by_bucket": {"a": 0.4, "b": 0.4},
            },
            {
                "candidate_id": "lean-b",
                "shares_by_bucket": {"a": 1.0, "b": 2.0},
                "cost_by_bucket": {"a": 0.4, "b": 0.4},
            },
        ],
        protected_buckets=["a", "b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )

    assert result["winner_candidate_id"] == "lean-b"
    assert result["target_shares_by_bucket"] == {"a": 1.0, "b": 2.0}


def test_support_floor_outlaws_lottery_shapes_and_reports_the_dials() -> None:
    """The basket promise (user ruling; charter 93 tightened it to zero): a
    support settlement may not lose.  A single-leg candidate over a
    two-bucket support settles the unheld bucket at minus the whole premium
    -- the measured production shape this ruling exists to kill (median win
    rate 29%, spike ratio 0.92) -- so however large its score, it must lose
    to the flat basket or, failing that, to HOLD.
    """

    lottery = {
        "candidate_id": "lottery",
        "shares_by_bucket": {"a": 9.6},
        "cost_by_bucket": {"a": 0.5},
    }
    flat = {
        "candidate_id": "flat",
        # Worst support settlement: 1.0 - 0.9 = +0.1, on the right side of
        # charter 93's zero floor; leaning toward the likely bucket keeps EV
        # positive so the floor, not the score, separates it from the lottery.
        "shares_by_bucket": {"a": 1.0, "b": 1.4},
        "cost_by_bucket": {"a": 0.45, "b": 0.45},
    }
    hold = {
        "candidate_id": "hold",
        "shares_by_bucket": {},
        "cost_by_bucket": {},
    }
    result = solve_target_inventory_v2(
        probabilities={"a": 0.2, "b": 0.8},
        feasible_inventory_candidates=[hold, lottery, flat],
        protected_buckets=["a", "b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )
    assert result["winner_candidate_id"] == "flat"
    assert result["floor_rejected_candidate_count"] == 1
    assert result["target_support_floor_pnl"] == pytest.approx(0.1)
    assert result["support_scenario_floor_fraction"] == 0.0
    # The dials: both rungs now pay, so the win probability is the whole
    # mass and the odds ratio has no loss side to divide by.
    assert result["target_win_probability"] == pytest.approx(1.0)
    assert result["target_odds_ratio"] is None

    # A book that only offers the lottery resolves to HOLD, not the lottery.
    held = solve_target_inventory_v2(
        probabilities={"a": 0.2, "b": 0.8},
        feasible_inventory_candidates=[hold, lottery],
        protected_buckets=["a", "b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )
    assert held["winner_candidate_id"] == "hold"


def test_risk_scope_cannot_expand_beyond_two_layers() -> None:
    with pytest.raises(ContractError, match="exceeds_two_layer_cap"):
        evaluate_basket_value_v2(
            probabilities={"a": 0.3, "b": 0.3, "c": 0.4},
            shares_by_bucket={},
            cost_by_bucket={},
            protected_buckets=["a"],
            shortfall_buckets=["a", "b", "c"],
            breakthrough_buckets=[],
            weights=_value_weights(),
        )


def test_maker_requires_zero_and_genuine_partial_fill_support() -> None:
    with pytest.raises(ContractError, match="partial_fill_support_required"):
        evaluate_action_v2(
            **_action_kwargs(
                fill_distribution=[
                    {"fill_fraction": 0.0, "probability": 0.5},
                    {"fill_fraction": 1.0, "probability": 0.5},
                ]
            )
        )
    with pytest.raises(ContractError, match="zero_fill_support_required"):
        evaluate_action_v2(
            **_action_kwargs(
                fill_distribution=[
                    {"fill_fraction": 0.5, "probability": 0.5},
                    {"fill_fraction": 1.0, "probability": 0.5},
                ]
            )
        )


def test_partial_fill_action_uses_all_outcomes_and_is_no_live() -> None:
    result = evaluate_action_v2(**_action_kwargs())

    assert result["schema"] == ACTION_EVALUATION_SCHEMA
    assert [row["filled_shares"] for row in result["fill_outcomes"]] == [
        0.0,
        5.0,
        10.0,
    ]
    assert result["expected_delta_value"] > 0.0
    assert result["authorized"] is True
    assert result["state"] == "TRADE"
    assert result["calls_order_gateway"] is False
    assert result["live_submit_authorized"] is False
    assert result["orders_submitted"] == 0


@pytest.mark.parametrize(
    ("gate", "expected_state"),
    [
        ("resource_probe_ok", "HOLD"),
        ("official_ws_fresh", "HOLD"),
        ("registry_timezone_matches", "HOLD"),
        ("lineage_complete", "HOLD"),
        ("reservation_price_ok", "HOLD"),
        ("order_state_known", "RECONCILE"),
    ],
)
def test_hard_gates_cannot_be_overridden_by_high_score(
    gate: str,
    expected_state: str,
) -> None:
    result = evaluate_action_v2(
        **_action_kwargs(hard_gates=_gates(**{gate: False}))
    )

    assert result["delta_basket_score_v2"] > 0.0
    assert result["authorized"] is False
    assert result["state"] == expected_state
    assert gate in result["blockers"]


def test_budget_reservation_floor_and_inventory_gap_are_derived_not_trusted() -> None:
    over_budget = evaluate_action_v2(
        **_action_kwargs(capital_available_usdc=0.5)
    )
    above_reserve = evaluate_action_v2(
        **_action_kwargs(reservation_price=0.05)
    )
    below_floor = evaluate_action_v2(
        **_action_kwargs(minimum_profit_floor=0.0)
    )
    over_gap = evaluate_action_v2(
        **_action_kwargs(
            target_shares_by_bucket={"35": 0.0, "36": 5.0}
        )
    )

    assert over_budget["authorized"] is False
    assert "budget_ok" in over_budget["blockers"]
    assert above_reserve["authorized"] is False
    assert "reservation_price_ok" in above_reserve["blockers"]
    assert below_floor["authorized"] is False
    assert "profit_floor_ok" in below_floor["blockers"]
    assert over_gap["authorized"] is False
    assert "clip_exceeds_inventory_gap" in over_gap["blockers"]


def test_confirmed_pending_reduces_gap_without_entering_realised_value() -> None:
    result = evaluate_action_v2(
        **_action_kwargs(
            cash_clip=0.5,
            target_shares_by_bucket={"35": 0.0, "36": 10.0},
            confirmed_pending_shares_by_bucket={"35": 0.0, "36": 5.0},
            confirmed_pending_cost_by_bucket={"35": 0.0, "36": 0.5},
        )
    )

    assert "clip_exceeds_inventory_gap" not in result["blockers"]
    assert result["confirmed_pending_in_realised_value"] is False
    assert result["confirmed_pending_shares_by_bucket"]["36"] == 5.0
    assert result["fill_outcomes"][0]["resulting_profit_floor"] == pytest.approx(
        -0.0
    )


def test_explicit_hold_beats_nonpositive_trade() -> None:
    expensive = evaluate_action_v2(
        **_action_kwargs(
            limit_price=0.9,
            wait_cost=100.0,
        )
    )
    selected = select_action_v2(
        [expensive],
        execution_weights=_execution_weights(),
    )

    assert selected["selected_state"] == "HOLD"
    assert selected["selected_score"] == 0.0
    assert selected["authorized"] is False


def test_reservation_price_is_value_inversion_not_market_copy() -> None:
    kwargs = _action_kwargs()
    kwargs.pop("cash_clip")
    kwargs.pop("limit_price")
    result = solve_reservation_price_v2(
        cash_clip=1.0,
        price_grid=[0.2, 0.4, 0.6, 0.7],
        action_kwargs=kwargs,
    )

    assert result["reservation_price"] == pytest.approx(0.4)
    assert result["orders_submitted"] == 0


def test_fok_is_full_or_zero_contract() -> None:
    good = evaluate_action_v2(
        **_action_kwargs(
            order_mode="FOK",
            timeout_seconds=0.0,
            fill_distribution=[
                {"fill_fraction": 1.0, "probability": 1.0}
            ],
        )
    )
    assert good["fill_outcomes"][0]["filled_shares"] == pytest.approx(10.0)

    # The kill.  Refused here until 2026-09-11, which made the only
    # distribution a fill-or-kill could carry "fills, with certainty" -- not a
    # model of this order type but the assumption that the book cannot move
    # between the quote that priced it and the order's arrival.
    killable = evaluate_action_v2(
        **_action_kwargs(
            order_mode="FOK",
            timeout_seconds=0.0,
            fill_distribution=[
                {"fill_fraction": 0.0, "probability": 0.5},
                {"fill_fraction": 1.0, "probability": 0.5},
            ],
        )
    )
    assert [
        row["filled_shares"] for row in killable["fill_outcomes"]
    ] == pytest.approx([0.0, 10.0])

    # A fraction between the two is the outcome this order type does not
    # have, and admitting it would let a FOK be scored as the maker it is not.
    with pytest.raises(ContractError, match="fok_distribution_partial_forbidden"):
        evaluate_action_v2(
            **_action_kwargs(
                order_mode="FOK",
                timeout_seconds=0.0,
                fill_distribution=[
                    {"fill_fraction": 0.4, "probability": 0.5},
                    {"fill_fraction": 1.0, "probability": 0.5},
                ],
            )
        )

    # And a distribution with no full fill at all describes an order that
    # cannot do the one thing it is for.
    with pytest.raises(ContractError, match="must_be_full_fill"):
        evaluate_action_v2(
            **_action_kwargs(
                order_mode="FOK",
                timeout_seconds=0.0,
                fill_distribution=[
                    {"fill_fraction": 0.0, "probability": 1.0},
                ],
            )
        )


def _complete_bundle(actions: list[dict[str, object]], capital: float = 12.0) -> dict[str, object]:
    return select_complete_maker_bundle_v2(
        actions,
        required_buckets=["35", "36"],
        probabilities={"35": 0.5, "36": 0.5},
        current_shares_by_bucket={"35": 0.0, "36": 0.0},
        current_cost_by_bucket={"35": 0.0, "36": 0.0},
        target_shares_by_bucket={"35": 12.0, "36": 12.0},
        protected_buckets=["35", "36"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        value_weights=_value_weights(),
        execution_weights=_execution_weights(),
        capital_available_usdc=capital,
        minimum_profit_floor=-12.0,
    )


def _event_bundle(
    event_id: str,
    *,
    cash_clip: float = 1.0,
    limit_price: float = 0.10,
) -> dict[str, object]:
    common = {
        "event_id": event_id,
        "cash_clip": cash_clip,
        "limit_price": limit_price,
        "probabilities": {"35": 0.5, "36": 0.5},
        "target_shares_by_bucket": {"35": 12.0, "36": 12.0},
    }
    selection = _complete_bundle([
        evaluate_action_v2(**_action_kwargs(bucket=bucket, **common))
        for bucket in ("35", "36")
    ])
    assert selection["authorized"] is True
    return selection["selected_bundle"]


def test_cross_event_allocator_obeys_shared_capital_and_keeps_bundles_whole() -> None:
    first = _event_bundle("e1")
    weaker_same_event = _event_bundle(
        "e1",
        cash_clip=0.5,
        limit_price=0.20,
    )
    second = _event_bundle("e2")
    result = allocate_cross_event_bundle_capital_v2(
        [first, weaker_same_event, second],
        capital_available_usdc=4.0,
        lambda_portfolio_concentration=0.0,
        portfolio_concentration_scale=4.0,
    )

    assert len(result["selected_bundles"]) == 2
    assert len(result["selected_actions"]) == 4
    assert {
        row["action"]["event_id"] for row in result["selected_actions"]
    } == {"e1", "e2"}
    assert {
        row["action"]["bucket"] for row in result["selected_actions"]
    } == {"35", "36"}
    assert result["capital_reserved_usdc"] == pytest.approx(4.0)


def test_cross_event_allocator_rejects_individual_leg_and_candidate_overflow() -> None:
    action = evaluate_action_v2(**_action_kwargs())
    result = allocate_cross_event_bundle_capital_v2(
        [action],
        capital_available_usdc=100.0,
        lambda_portfolio_concentration=0.0,
        portfolio_concentration_scale=1.0,
    )
    assert result["hold_selected"] is True
    assert result["selected_actions"] == []

    bundle = _event_bundle("e1")
    with pytest.raises(ContractError, match="candidate_bound_exceeded"):
        allocate_cross_event_bundle_capital_v2(
            [bundle] * 17,
            capital_available_usdc=100.0,
            lambda_portfolio_concentration=0.0,
            portfolio_concentration_scale=1.0,
            maximum_candidates=16,
        )


def test_complete_maker_bundle_requires_every_bucket_and_is_order_deterministic() -> None:
    common = {
        "probabilities": {"35": 0.5, "36": 0.5},
        "target_shares_by_bucket": {"35": 12.0, "36": 12.0},
    }
    leg_35 = evaluate_action_v2(
        **_action_kwargs(bucket="35", **common)
    )
    leg_36 = evaluate_action_v2(
        **_action_kwargs(bucket="36", **common)
    )

    missing = _complete_bundle([leg_35])
    forward = _complete_bundle([leg_35, leg_36])
    reversed_input = _complete_bundle([leg_36, leg_35])

    assert missing["authorized"] is False
    assert "missing_leg_candidate:36" in missing["blockers"]
    assert forward["authorized"] is True
    assert [
        row["action"]["bucket"] for row in forward["selected_actions"]
    ] == ["35", "36"]
    assert reversed_input["content_sha256"] == forward["content_sha256"]


def test_empty_target_is_economic_hold_not_missing_leg_candidate() -> None:
    result = select_complete_maker_bundle_v2(
        [],
        required_buckets=[],
        probabilities={"35": 0.5, "36": 0.5},
        current_shares_by_bucket={"35": 0.0, "36": 0.0},
        current_cost_by_bucket={"35": 0.0, "36": 0.0},
        target_shares_by_bucket={"35": 0.0, "36": 0.0},
        protected_buckets=["35", "36"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        value_weights=_value_weights(),
        execution_weights=_execution_weights(),
        capital_available_usdc=12.0,
        minimum_profit_floor=-12.0,
    )

    assert result["authorized"] is False
    assert result["blockers"] == ["complete_basket_target_not_activated"]
    assert result["required_buckets"] == []
    assert result["candidate_action_count"] == 0


def _bounded_multi_bucket_actions(
    bucket_count: int,
    *,
    clips: tuple[float, ...],
) -> tuple[list[str], list[dict[str, object]]]:
    buckets = [f"bucket-{index}" for index in range(bucket_count)]
    probabilities = {bucket: 1.0 / bucket_count for bucket in buckets}
    current = {bucket: 0.0 for bucket in buckets}
    target = {bucket: 40.0 for bucket in buckets}
    actions: list[dict[str, object]] = []
    for bucket in buckets:
        for clip in clips:
            actions.append(evaluate_action_v2(**_action_kwargs(
                bucket=bucket,
                cash_clip=clip,
                limit_price=0.10,
                probabilities=probabilities,
                current_shares_by_bucket=current,
                current_cost_by_bucket=current,
                target_shares_by_bucket=target,
                protected_buckets=buckets,
                shortfall_buckets=[],
                breakthrough_buckets=[],
                capital_available_usdc=100.0,
                minimum_profit_floor=-100.0,
            )))
    return buckets, actions


def _select_bounded_multi_bucket_bundle(
    buckets: list[str],
    actions: list[dict[str, object]],
) -> dict[str, object]:
    probabilities = {bucket: 1.0 / len(buckets) for bucket in buckets}
    zeros = {bucket: 0.0 for bucket in buckets}
    return select_complete_maker_bundle_v2(
        actions,
        required_buckets=buckets,
        probabilities=probabilities,
        current_shares_by_bucket=zeros,
        current_cost_by_bucket=zeros,
        target_shares_by_bucket={bucket: 40.0 for bucket in buckets},
        protected_buckets=buckets,
        shortfall_buckets=[],
        breakthrough_buckets=[],
        value_weights=_value_weights(),
        execution_weights=_execution_weights(),
        capital_available_usdc=100.0,
        minimum_profit_floor=-100.0,
    )


def test_complete_maker_bundle_prunes_normal_five_bucket_grid_before_outcome_overflow() -> None:
    buckets, actions = _bounded_multi_bucket_actions(
        5,
        clips=(1.0, 2.0),
    )

    result = _select_bounded_multi_bucket_bundle(buckets, actions)
    reverse = _select_bounded_multi_bucket_bundle(buckets, list(reversed(actions)))
    resource = result["joint_outcome_resource_bound"]

    # 5 buckets x 2 Maker choices x (zero, partial, full) would otherwise
    # require 2**5 * 3**5 = 7,776 retained joint outcomes.  The selector
    # keeps every required bucket and reduces only optional alternatives.
    assert resource["requested_joint_outcome_count"] == 7776
    assert resource["outcome_row_limit"] == 4096
    assert resource["evaluated_joint_outcome_count"] == 3888
    assert resource["requested_candidate_action_count"] == 10
    assert resource["evaluated_candidate_action_count"] == 9
    assert len(resource["pruned_action_sha256s"]) == 1
    assert result["bundle_candidate_count"] == 16
    assert result["content_sha256"] == reverse["content_sha256"]
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_complete_maker_bundle_deduplicates_gap_clipped_identical_actions_before_pruning() -> None:
    buckets, actions = _bounded_multi_bucket_actions(
        5,
        clips=(1.0, 2.0),
    )
    # A runtime cash clip can be capped by the same inventory gap twice.  The
    # resulting immutable action is exactly identical and must not inflate the
    # joint outcome Cartesian product.
    actions[-1] = copy.deepcopy(actions[-2])

    result = _select_bounded_multi_bucket_bundle(buckets, actions)
    resource = result["joint_outcome_resource_bound"]

    assert resource["raw_candidate_action_count"] == 10
    assert resource["deduplicated_candidate_action_count"] == 9
    assert resource["requested_joint_outcome_count"] == 3888
    assert resource["evaluated_joint_outcome_count"] == 3888
    assert len(resource["duplicate_action_sha256s"]) == 1
    assert resource["pruned_action_sha256s"] == []


def test_complete_maker_bundle_still_fails_closed_when_one_leg_per_bucket_exceeds_outcome_budget() -> None:
    buckets, actions = _bounded_multi_bucket_actions(8, clips=(1.0,))

    with pytest.raises(ContractError, match="bundle_outcome_bound_exceeded"):
        _select_bounded_multi_bucket_bundle(buckets, actions)


def test_complete_maker_bundle_holds_when_joint_reservation_exceeds_capital() -> None:
    common = {
        "probabilities": {"35": 0.5, "36": 0.5},
        "target_shares_by_bucket": {"35": 12.0, "36": 12.0},
    }
    actions = [
        evaluate_action_v2(**_action_kwargs(bucket=bucket, **common))
        for bucket in ("35", "36")
    ]

    result = _complete_bundle(actions, capital=1.5)

    assert result["authorized"] is False
    assert result["selected_actions"] == []
    assert result["capital_reserved_usdc"] == 0.0


def test_low_high_low_and_repeated_replay_are_content_deterministic() -> None:
    low = evaluate_action_v2(**_action_kwargs(limit_price=0.10))
    high = evaluate_action_v2(**_action_kwargs(limit_price=0.20))
    low_replay = evaluate_action_v2(**_action_kwargs(limit_price=0.10))
    repeated = [
        evaluate_action_v2(**copy.deepcopy(_action_kwargs(limit_price=0.10)))
        for _ in range(25)
    ]

    assert low["content_sha256"] != high["content_sha256"]
    assert low_replay["content_sha256"] == low["content_sha256"]
    assert {row["content_sha256"] for row in repeated} == {
        low["content_sha256"]
    }


def test_invalid_scales_probabilities_and_candidate_sizes_fail_closed() -> None:
    with pytest.raises(ContractError):
        _value_weights(expected_pnl_scale=0.0)
    with pytest.raises(ContractError):
        evaluate_basket_value_v2(
            probabilities={"a": 0.4, "b": 0.4},
            shares_by_bucket={},
            cost_by_bucket={},
            protected_buckets=["a"],
            shortfall_buckets=[],
            breakthrough_buckets=[],
            weights=_value_weights(),
        )
    with pytest.raises(ContractError, match="candidate_bound_exceeded"):
        solve_target_inventory_v2(
            probabilities={"a": 1.0},
            feasible_inventory_candidates=[
                {
                    "candidate_id": str(index),
                    "shares_by_bucket": {},
                    "cost_by_bucket": {},
                }
                for index in range(3)
            ],
            protected_buckets=["a"],
            shortfall_buckets=[],
            breakthrough_buckets=[],
            weights=_value_weights(),
            maximum_candidates=2,
        )


_BUNDLE_COMMON = {
    "probabilities": {"35": 0.5, "36": 0.5},
    "target_shares_by_bucket": {"35": 12.0, "36": 12.0},
}


def _rescore(
    bundle: dict[str, object],
    *,
    shares: dict[str, float],
    notional: dict[str, float],
    capital: float = 12.0,
    minimum_profit_floor: float = -12.0,
) -> dict[str, object]:
    return rescore_maker_bundle_v2(
        bundle["selected_actions"],
        required_buckets=["35", "36"],
        probabilities={"35": 0.5, "36": 0.5},
        current_shares_by_bucket={"35": 0.0, "36": 0.0},
        current_cost_by_bucket={"35": 0.0, "36": 0.0},
        target_shares_by_bucket={"35": 12.0, "36": 12.0},
        protected_buckets=["35", "36"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        value_weights=_value_weights(),
        execution_weights=_execution_weights(),
        capital_available_usdc=capital,
        minimum_profit_floor=minimum_profit_floor,
        sized_shares_by_bucket=shares,
        sized_notional_by_bucket=notional,
    )


def test_rescore_at_the_authorized_sizes_reproduces_the_authorization() -> None:
    # The property that makes a re-score trustworthy at all: fed the sizes
    # the selector authorized, it must return the selector's own verdict and
    # the selector's own score.  Anything else means execution is being
    # ruled on by different arithmetic than authorization was.
    bundle = _complete_bundle([
        evaluate_action_v2(**_action_kwargs(bucket=bucket, **_BUNDLE_COMMON))
        for bucket in ("35", "36")
    ])
    assert bundle["authorized"] is True
    authorized = {
        str(dict(row["action"])["bucket"]): dict(row["action"])
        for row in bundle["selected_actions"]
    }

    identical = _rescore(
        bundle,
        shares={
            bucket: float(action["maximum_shares"])
            for bucket, action in authorized.items()
        },
        notional={
            bucket: float(action["cash_clip"])
            for bucket, action in authorized.items()
        },
    )
    assert identical["authorized"] is True
    assert identical["delta_basket_score_v2"] == pytest.approx(
        float(bundle["selected_bundle"]["delta_basket_score_v2"])
    )
    assert identical["capital_reserved_usdc"] == pytest.approx(
        float(bundle["selected_bundle"]["capital_reserved_usdc"])
    )
    assert identical["blockers"] == []


def test_rescore_rules_on_the_cut_basket_and_refuses_an_illegal_one() -> None:
    bundle = _complete_bundle([
        evaluate_action_v2(**_action_kwargs(bucket=bucket, **_BUNDLE_COMMON))
        for bucket in ("35", "36")
    ])
    assert bundle["authorized"] is True
    authorized = {
        str(dict(row["action"])["bucket"]): dict(row["action"])
        for row in bundle["selected_actions"]
    }
    full_shares = {
        bucket: float(action["maximum_shares"])
        for bucket, action in authorized.items()
    }
    full_notional = {
        bucket: float(action["cash_clip"])
        for bucket, action in authorized.items()
    }

    # One leg cut in half is a different basket, and scored as one.
    cut = _rescore(
        bundle,
        shares={**full_shares, "35": full_shares["35"] * 0.5},
        notional={**full_notional, "35": full_notional["35"] * 0.5},
    )
    assert cut["delta_basket_score_v2"] != pytest.approx(
        float(bundle["selected_bundle"]["delta_basket_score_v2"])
    )
    assert cut["sized_shares_by_bucket"]["35"] == pytest.approx(
        full_shares["35"] * 0.5
    )

    # A floor the full basket cleared can be broken by the cut, and then the
    # cut basket is refused -- which is the whole point of asking.
    strict = _rescore(
        bundle,
        shares={**full_shares, "35": full_shares["35"] * 0.02},
        notional={**full_notional, "35": full_notional["35"] * 0.02},
        minimum_profit_floor=-0.55,
    )
    assert strict["authorized"] is False
    assert "profit_floor_ok" in strict["blockers"]

    # A proper subset is never re-scored: atomicity is not negotiable here
    # either.
    with pytest.raises(ContractError, match="rescore_bundle_incomplete"):
        rescore_maker_bundle_v2(
            [bundle["selected_actions"][0]],
            required_buckets=["35", "36"],
            probabilities={"35": 0.5, "36": 0.5},
            current_shares_by_bucket={"35": 0.0, "36": 0.0},
            current_cost_by_bucket={"35": 0.0, "36": 0.0},
            target_shares_by_bucket={"35": 12.0, "36": 12.0},
            protected_buckets=["35", "36"],
            shortfall_buckets=[],
            breakthrough_buckets=[],
            value_weights=_value_weights(),
            execution_weights=_execution_weights(),
            capital_available_usdc=12.0,
            minimum_profit_floor=-12.0,
            sized_shares_by_bucket=full_shares,
            sized_notional_by_bucket=full_notional,
        )


def test_charter_70_sub_lot_rungs_are_raised_to_one_lot_when_the_basket_can_pay() -> None:
    """User ruling 2026-09-02: 允许最小腿补到一手.

    SAEZ 2026-09-03, sized off the pool's 2% (6.17 USDC): 16C at 0.06 and
    18C at 0.16 solved to 4.17 and 1.56 shares against a five-share lot, so
    neither leg had an order the exchange would fill and the whole ladder
    held.  The raise lifts both to exactly one lot -- 0.60 USDC more -- and
    leaves the rung already over a lot alone.

    Pinned with the dollar floor off: charter 83 (2026-09-05) layers the
    venue's one-dollar minimum on the same function, and at 0.06 a lot of
    five is 0.30 -- its own test covers that arm.
    """
    from bayes_basket_target.basket_kernel.delta_v2 import (
        EXCHANGE_LOT_TOP_UP,
        EXCHANGE_LOT_TOP_UP_MODES,
        raise_sub_lot_targets_v2,
    )

    assert EXCHANGE_LOT_TOP_UP in EXCHANGE_LOT_TOP_UP_MODES
    targets = {"16": 4.17, "17": 39.1, "18": 1.56}
    lots = {"16": 5.0, "17": 5.0, "18": 5.0}
    asks = {"16": 0.06, "17": 0.05, "18": 0.16}

    raised = raise_sub_lot_targets_v2(
        target_shares_by_bucket=targets, support=("16", "17", "18"),
        min_lot_by_bucket=lots, ask_by_bucket=asks,
        capital_available_usdc=6.17,

        minimum_notional_usdc=0.0,
    )
    assert raised["raised_buckets"] == ("16", "18")
    assert raised["target_shares_by_bucket"] == {"16": 5.0, "17": 39.1, "18": 5.0}
    assert raised["lot_raise_usdc"] == pytest.approx((5 - 4.17) * 0.06 + (5 - 1.56) * 0.16)
    assert raised["refused_reason"] is None

    # All or none: a basket that can lift only one of its short rungs is
    # still not buyable, so nothing moves and the refusal is named.
    short = raise_sub_lot_targets_v2(
        target_shares_by_bucket={"16": 4.17, "18": 1.56}, support=("16", "18"),
        min_lot_by_bucket={"16": 5.0, "18": 5.0}, ask_by_bucket={"16": 0.06, "18": 0.16},
        capital_available_usdc=0.9,

        minimum_notional_usdc=0.0,
    )
    assert short["raised_buckets"] == ()
    assert short["target_shares_by_bucket"] == {"16": 4.17, "18": 1.56}
    assert short["refused_reason"] == "exchange_lot_top_up_unaffordable"
    assert short["requested_lot_raise_usdc"] == pytest.approx(raised["lot_raise_usdc"])

    # A market that states no lot is not known to be under one.
    unstated = raise_sub_lot_targets_v2(
        target_shares_by_bucket=targets, support=("16", "17", "18"),
        min_lot_by_bucket={"16": 0.0, "17": 0.0, "18": 0.0}, ask_by_bucket=asks,
        capital_available_usdc=6.17,

        minimum_notional_usdc=0.0,
    )
    assert unstated["raised_buckets"] == ()
    assert unstated["target_shares_by_bucket"] == targets

    # The pre-charter-70 book, kept as a replay arm.
    off = raise_sub_lot_targets_v2(
        target_shares_by_bucket=targets, support=("16", "17", "18"),
        min_lot_by_bucket=lots, ask_by_bucket=asks,
        capital_available_usdc=6.17, mode="off",

        minimum_notional_usdc=0.0,
    )
    assert off["raised_buckets"] == () and off["target_shares_by_bucket"] == targets

    with pytest.raises(ContractError, match="exchange_lot_top_up_mode_invalid"):
        raise_sub_lot_targets_v2(
            target_shares_by_bucket=targets, support=("16",),
            min_lot_by_bucket=lots, ask_by_bucket=asks,
            capital_available_usdc=6.17, mode="round_up",

            minimum_notional_usdc=0.0,
        )
