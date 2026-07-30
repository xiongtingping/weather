from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.basket_kernel.contracts import (
    ContractError,
    TargetBasketPolicy,
    TargetBasketSnapshot,
)
from bayes_basket_target.basket_kernel.strict_delta import (
    SOLVER_VERSION,
    StrictDeltaPolicy,
    build_fok_cost_curve,
    build_maker_cost_curve,
    evaluate_strict_delta,
    price_fok_curve,
)
from bayes_basket_target.basket_kernel.objective import (
    ObjectiveWeights,
    evaluate_objective_state,
)


def _snapshot(
    *,
    probability: float = 0.7,
    second_ask: float = 0.50,
    second_depth: float = 8.0,
) -> TargetBasketSnapshot:
    return TargetBasketSnapshot.from_mapping({
        "snapshot_id": "strict-delta-snapshot",
        "sequence": 1,
        "observed_at": "2026-07-26T21:00:01Z",
        "market_id": "market",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "quote_source": "OFFICIAL_CLOB_WS",
        "quote_observed_at": "2026-07-26T21:00:00Z",
        "quote_version": "a" * 64,
        "inventory_version": "inventory-v1",
        "order_state_version": "orders-v1",
        "remaining_budget_usdc": 20.0,
        "evidence_fresh": True,
        "order_state_known": True,
        "contract_matches": True,
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "p1-v1",
            "posterior_sha256": "b" * 64,
            "forecast_dispersion_c": 1.0,
            "model_envelope_lower_ordinal": 0,
            "model_envelope_upper_ordinal": 1,
        },
        "buckets": [
            {
                "bucket": "35°C",
                "ordinal": 0,
                "probability": probability,
                "bid_price": 0.19,
                "ask_levels": [
                    {"price": 0.20, "shares": 2.0},
                    {"price": second_ask, "shares": second_depth},
                ],
                "maker_queue_ahead_shares": 0.0,
                "maker_queue_known": True,
            },
            {
                "bucket": "36°C",
                "ordinal": 1,
                "probability": 1.0 - probability,
                "bid_price": 0.09,
                "ask_levels": [
                    {"price": 0.10, "shares": 10.0},
                ],
                "maker_queue_ahead_shares": 0.0,
                "maker_queue_known": True,
            },
        ],
    })


def _target_policy() -> TargetBasketPolicy:
    return TargetBasketPolicy.from_mapping({
        "policy_id": "strict-delta-target",
        "clip_max_notional_usdc": 10.0,
        "total_budget_usdc": 20.0,
        "taker_slippage_buffer_per_share": 0.01,
    })


def _strict_policy(**overrides) -> StrictDeltaPolicy:
    return StrictDeltaPolicy.from_mapping({
        "policy_version": "strict-delta-oos-v1",
        "epsilon_score": 0.0001,
        "lambda_profit_floor": 0.0,
        "lambda_downside": 0.0,
        "lambda_shortfall": 0.0,
        "lambda_breakthrough": 0.0,
        "lambda_concentration": 0.0,
        "lambda_execution": 1.0,
        "expected_pnl_policy_floor": -20.0,
        "subsidy_absolute_cap": 0.0,
        "subsidy_ratio_cap": 0.0,
        "coverage_policy_oos_activated": False,
        **overrides,
    })


def _versions(
    snapshot: TargetBasketSnapshot,
    strict: StrictDeltaPolicy,
) -> dict[str, str]:
    return {
        "probability_artifact_sha256": (
            snapshot.evidence.posterior_sha256
        ),
        "quote_snapshot_sha256": snapshot.quote_version,
        "inventory_version": snapshot.inventory_version,
        "order_state_version": snapshot.order_state_version,
        "policy_version": strict.policy_version,
        "solver_version": SOLVER_VERSION,
    }


def test_fok_curve_walks_multiple_levels_not_top_ask_linear_cost() -> None:
    snapshot = _snapshot()
    policy = _target_policy()
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        policy,
        quote_version=snapshot.quote_version,
    )

    priced = price_fok_curve(curve, 4.0)

    assert priced["fillable"] is True
    assert priced["exchange_vwap"] == pytest.approx(0.35)
    assert priced["all_in_vwap"] == pytest.approx(0.36)
    assert priced["all_in_cost"] == pytest.approx(1.44)
    assert priced["depth_levels_consumed"] == 2


def test_low_price_or_high_odds_cannot_bypass_negative_delta_ev() -> None:
    snapshot = _snapshot(probability=0.05)
    target = _target_policy()
    strict = _strict_policy()
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )

    result = evaluate_strict_delta(
        snapshot,
        target,
        strict,
        proposed_actions={
            "35°C": {"mode": "FOK", "shares": 1.0, "cost_curve": curve}
        },
        protected_buckets=["35°C", "36°C"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        version_vector=_versions(snapshot, strict),
    )

    assert result["delta_ev_by_bucket"]["35°C"] < 0.0
    assert result["authorized"] is False
    assert result["coverage_subsidized"] is True
    assert result["hard_constraints"]["coverage_oos_ok"] is False


def test_baseline_and_proposal_version_drift_fails_closed() -> None:
    snapshot = _snapshot()
    target = _target_policy()
    strict = _strict_policy()
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )
    versions = _versions(snapshot, strict)
    versions["quote_snapshot_sha256"] = "d" * 64

    with pytest.raises(
        ContractError,
        match="strict_delta_version_vector_mismatch",
    ):
        evaluate_strict_delta(
            snapshot,
            target,
            strict,
            proposed_actions={
                "35°C": {
                    "mode": "FOK",
                    "shares": 1.0,
                    "cost_curve": curve,
                }
            },
            protected_buckets=["35°C", "36°C"],
            shortfall_buckets=[],
            breakthrough_buckets=[],
            version_vector=versions,
        )


def test_maker_curve_requires_explicit_oos_activation() -> None:
    snapshot = _snapshot()
    target = _target_policy()

    with pytest.raises(
        ContractError,
        match="maker_cost_curve_oos_activation_required",
    ):
        build_maker_cost_curve(
            snapshot.buckets[0],
            target,
            quote_version=snapshot.quote_version,
            candidate_price=0.19,
            fill_probability=0.5,
            nonfill_opportunity_cost_per_share=0.01,
            adverse_selection_cost_per_filled_share=0.01,
            wait_cost_per_share=0.01,
            oos_activated=False,
            oos_artifact_sha256="e" * 64,
            oos_model_version="maker-oos-v1",
        )


def test_delta_decomposition_closes_and_positive_ev_authorizes() -> None:
    snapshot = _snapshot(probability=0.70)
    target = _target_policy()
    strict = _strict_policy()
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )

    result = evaluate_strict_delta(
        snapshot,
        target,
        strict,
        proposed_actions={
            "35°C": {"mode": "FOK", "shares": 1.0, "cost_curve": curve}
        },
        protected_buckets=["35°C", "36°C"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        version_vector=_versions(snapshot, strict),
    )

    assert result["delta_expected_pnl"] == pytest.approx(0.49)
    assert result["delta_basket_score"] == pytest.approx(0.49)
    assert result["delta_ev_by_bucket"]["35°C"] == pytest.approx(0.49)
    assert result["authorized"] is True
    assert result["mode_specific_cost_curve_sha256"]["35°C"]
    assert len(result["content_sha256"]) == 64


def test_same_top_ask_different_second_level_changes_cost_and_authority() -> None:
    target = _target_policy()
    strict = _strict_policy()
    cheap = _snapshot(probability=0.40, second_ask=0.25)
    costly = _snapshot(probability=0.40, second_ask=0.80)

    def evaluate(snapshot: TargetBasketSnapshot) -> dict:
        curve = build_fok_cost_curve(
            snapshot.buckets[0],
            target,
            quote_version=snapshot.quote_version,
        )
        return evaluate_strict_delta(
            snapshot,
            target,
            strict,
            proposed_actions={
                "35°C": {
                    "mode": "FOK",
                    "shares": 4.0,
                    "cost_curve": curve,
                }
            },
            protected_buckets=["35°C", "36°C"],
            shortfall_buckets=[],
            breakthrough_buckets=[],
            version_vector=_versions(snapshot, strict),
        )

    cheap_result = evaluate(cheap)
    costly_result = evaluate(costly)

    assert cheap_result["action_cost_by_bucket"]["35°C"] == pytest.approx(0.94)
    assert cheap_result["authorized"] is True
    assert costly_result["action_cost_by_bucket"]["35°C"] == pytest.approx(2.04)
    assert costly_result["authorized"] is False


def test_curve_content_tampering_with_stale_hash_fails_closed() -> None:
    snapshot = _snapshot()
    target = _target_policy()
    strict = _strict_policy()
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )
    tampered = deepcopy(curve)
    tampered["segments"][0]["all_in_unit_cost"] = 0.01

    with pytest.raises(
        ContractError,
        match="cost_curve_content_sha256_mismatch",
    ):
        evaluate_strict_delta(
            snapshot,
            target,
            strict,
            proposed_actions={
                "35°C": {
                    "mode": "FOK",
                    "shares": 1.0,
                    "cost_curve": tampered,
                }
            },
            protected_buckets=["35°C", "36°C"],
            shortfall_buckets=[],
            breakthrough_buckets=[],
            version_vector=_versions(snapshot, strict),
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("bucket", "36°C", "cost_curve_bucket_mismatch"),
        ("quote_version", "c" * 64, "cost_curve_quote_version_mismatch"),
    ],
)
def test_curve_identity_mismatch_fails_closed(
    field: str,
    value: str,
    error: str,
) -> None:
    snapshot = _snapshot()
    target = _target_policy()
    strict = _strict_policy()
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )
    curve[field] = value

    with pytest.raises(ContractError, match=error):
        evaluate_strict_delta(
            snapshot,
            target,
            strict,
            proposed_actions={
                "35°C": {
                    "mode": "FOK",
                    "shares": 1.0,
                    "cost_curve": curve,
                }
            },
            protected_buckets=["35°C", "36°C"],
            shortfall_buckets=[],
            breakthrough_buckets=[],
            version_vector=_versions(snapshot, strict),
        )


def test_negative_ev_bucket_cannot_borrow_another_tail_bucket_identity() -> None:
    snapshot = _snapshot(probability=0.05)
    target = _target_policy()
    strict = _strict_policy(
        subsidy_absolute_cap=10.0,
        subsidy_ratio_cap=10.0,
        coverage_policy_oos_activated=True,
        lambda_shortfall=10.0,
    )
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )

    result = evaluate_strict_delta(
        snapshot,
        target,
        strict,
        proposed_actions={
            "35°C": {
                "mode": "FOK",
                "shares": 1.0,
                "cost_curve": curve,
                "coverage_subsidized": True,
            }
        },
        protected_buckets=["35°C", "36°C"],
        shortfall_buckets=["36°C"],
        breakthrough_buckets=[],
        version_vector=_versions(snapshot, strict),
    )

    assert result["authorized"] is False
    assert result["hard_constraints"]["coverage_bucket_scope_ok"] is False
    assert result["hard_constraints"]["regular_bucket_delta_ev_nonnegative"] is False


@pytest.mark.parametrize(
    ("absolute_cap", "ratio_cap"),
    (
        (0.10, 10.0),
        (10.0, 0.10),
    ),
)
def test_negative_ev_insurance_cannot_exceed_either_subsidy_cap(
    absolute_cap: float,
    ratio_cap: float,
) -> None:
    snapshot = _snapshot(probability=0.05)
    target = _target_policy()
    strict = _strict_policy(
        subsidy_absolute_cap=absolute_cap,
        subsidy_ratio_cap=ratio_cap,
        coverage_policy_oos_activated=True,
        lambda_shortfall=10.0,
    )
    negative_curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )
    positive_curve = build_fok_cost_curve(
        snapshot.buckets[1],
        target,
        quote_version=snapshot.quote_version,
    )

    result = evaluate_strict_delta(
        snapshot,
        target,
        strict,
        proposed_actions={
            "35°C": {
                "mode": "FOK",
                "shares": 1.0,
                "cost_curve": negative_curve,
                "coverage_subsidized": True,
            },
            "36°C": {
                "mode": "FOK",
                "shares": 1.0,
                "cost_curve": positive_curve,
            },
        },
        protected_buckets=["35°C", "36°C"],
        shortfall_buckets=["35°C"],
        breakthrough_buckets=[],
        version_vector=_versions(snapshot, strict),
    )

    assert result["delta_basket_score"] > strict.epsilon_score
    assert result["negative_delta_ev_subsidy"] > result["subsidy_cap"]
    assert result["hard_constraints"]["subsidy_cap_ok"] is False
    assert result["authorized"] is False


def test_delta_equal_to_epsilon_is_rejected_strictly() -> None:
    snapshot = _snapshot(probability=0.70)
    target = _target_policy()
    strict = _strict_policy(epsilon_score=0.49)
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )

    result = evaluate_strict_delta(
        snapshot,
        target,
        strict,
        proposed_actions={
            "35°C": {"mode": "FOK", "shares": 1.0, "cost_curve": curve}
        },
        protected_buckets=["35°C", "36°C"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        version_vector=_versions(snapshot, strict),
    )

    assert result["delta_basket_score"] == pytest.approx(0.49)
    assert result["hard_constraints"]["delta_score_strictly_positive"] is False
    assert result["authorized"] is False


def test_fok_depth_boundary_and_round_trip_are_deterministic() -> None:
    snapshot = _snapshot()
    target = _target_policy()
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )

    low_first = price_fok_curve(curve, 2.0)
    exact = price_fok_curve(curve, 10.0)
    insufficient = price_fok_curve(curve, 10.0 + 1e-9)
    low_again = price_fok_curve(curve, 2.0)

    assert exact["fillable"] is True
    assert insufficient["fillable"] is False
    assert low_again == low_first


def test_empty_protected_scope_and_zero_action_fail_closed() -> None:
    snapshot = _snapshot()
    target = _target_policy()
    strict = _strict_policy()
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )

    with pytest.raises(
        ContractError,
        match="strict_delta_protected_buckets_required",
    ):
        evaluate_strict_delta(
            snapshot,
            target,
            strict,
            proposed_actions={
                "35°C": {
                    "mode": "FOK",
                    "shares": 1.0,
                    "cost_curve": curve,
                }
            },
            protected_buckets=[],
            shortfall_buckets=[],
            breakthrough_buckets=[],
            version_vector=_versions(snapshot, strict),
        )

    result = evaluate_strict_delta(
        snapshot,
        target,
        strict,
        proposed_actions={
            "35°C": {"mode": "FOK", "shares": 0.0, "cost_curve": curve}
        },
        protected_buckets=["35°C", "36°C"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        version_vector=_versions(snapshot, strict),
    )
    assert result["authorized"] is False
    assert "35°C:action_shares_not_positive" in result["blockers"]


def test_delta_ev_by_bucket_reconciles_exactly_to_delta_expected_pnl() -> None:
    snapshot = _snapshot(probability=0.65)
    target = _target_policy()
    strict = _strict_policy()
    actions = {
        item.bucket: {
            "mode": "FOK",
            "shares": 1.0,
            "cost_curve": build_fok_cost_curve(
                item,
                target,
                quote_version=snapshot.quote_version,
            ),
        }
        for item in snapshot.buckets
    }

    result = evaluate_strict_delta(
        snapshot,
        target,
        strict,
        proposed_actions=actions,
        protected_buckets=["35°C", "36°C"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        version_vector=_versions(snapshot, strict),
    )

    assert sum(result["delta_ev_by_bucket"].values()) == pytest.approx(
        result["delta_expected_pnl"],
        abs=1e-12,
    )
    assert result["delta_ev_reconciliation_slack"] == pytest.approx(
        0.0,
        abs=1e-12,
    )


def test_pending_maker_state_never_pollutes_realised_pnl_floor_or_coverage() -> None:
    base_raw = _snapshot(probability=0.70).to_dict()
    pending_raw = deepcopy(base_raw)
    pending_raw["snapshot_id"] = "strict-delta-pending-isolated"
    pending_raw["inventory_version"] = "inventory-with-pending"
    pending_raw["order_state_version"] = "orders-with-pending"
    pending_raw["buckets"][1]["pending_shares"] = 3.0
    pending_raw["buckets"][1]["pending_notional_usdc"] = 0.30
    base = TargetBasketSnapshot.from_mapping(base_raw)
    pending = TargetBasketSnapshot.from_mapping(pending_raw)
    target = _target_policy()
    strict = _strict_policy(lambda_shortfall=2.0)

    def run(snapshot: TargetBasketSnapshot) -> dict:
        curve = build_fok_cost_curve(
            snapshot.buckets[0],
            target,
            quote_version=snapshot.quote_version,
        )
        return evaluate_strict_delta(
            snapshot,
            target,
            strict,
            proposed_actions={
                "35°C": {
                    "mode": "FOK",
                    "shares": 1.0,
                    "cost_curve": curve,
                }
            },
            protected_buckets=["35°C", "36°C"],
            shortfall_buckets=["36°C"],
            breakthrough_buckets=[],
            version_vector=_versions(snapshot, strict),
        )

    without_pending = run(base)
    with_pending = run(pending)

    for side in ("baseline", "proposed"):
        assert with_pending[side]["scenario_pnl"] == pytest.approx(
            without_pending[side]["scenario_pnl"]
        )
        assert with_pending[side]["profit_floor"] == pytest.approx(
            without_pending[side]["profit_floor"]
        )
        assert with_pending[side]["shortfall_coverage"] == pytest.approx(
            without_pending[side]["shortfall_coverage"]
        )
    assert (
        with_pending["constraint_slacks"]["budget_usdc"]
        < without_pending["constraint_slacks"]["budget_usdc"]
    )
    assert (
        with_pending["pending_execution_state"][
            "included_in_realised_objective"
        ]
        is False
    )


def test_negative_ev_cannot_pass_from_abstract_coverage_without_tail_improvement() -> None:
    snapshot = _snapshot(probability=0.05)
    target = _target_policy()
    strict = _strict_policy(
        subsidy_absolute_cap=10.0,
        subsidy_ratio_cap=10.0,
        coverage_policy_oos_activated=True,
        lambda_shortfall=100.0,
    )
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )

    result = evaluate_strict_delta(
        snapshot,
        target,
        strict,
        proposed_actions={
            "35°C": {
                "mode": "FOK",
                "shares": 1.0,
                "cost_curve": curve,
                "coverage_subsidized": True,
            }
        },
        protected_buckets=["35°C", "36°C"],
        shortfall_buckets=["35°C"],
        breakthrough_buckets=[],
        version_vector=_versions(snapshot, strict),
    )

    assert result["delta_shortfall_coverage"] > 0.0
    assert result["delta_basket_score"] > strict.epsilon_score
    assert result["delta_profit_floor"] <= 0.0
    assert result["delta_protected_tail_loss"] >= -1e-12
    assert (
        result["hard_constraints"]["coverage_improves_protected_risk"]
        is False
    )
    assert result["authorized"] is False


def test_objective_hash_binds_canonical_inputs_not_only_aggregates() -> None:
    weights = ObjectiveWeights(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    common = {
        "probabilities": {"a": 0.5, "b": 0.5},
        "filled_cost_by_bucket": {"a": 0.0, "b": 0.0},
        "action_shares_by_bucket": {},
        "action_cost_by_bucket": {},
        "protected_buckets": ["a", "b"],
        "shortfall_buckets": [],
        "breakthrough_buckets": [],
        "weights": weights,
        "execution_risk": 0.0,
        "concentration_scale_usdc": 10.0,
    }
    first = evaluate_objective_state(
        **common,
        filled_shares_by_bucket={"a": 1.0, "b": 0.0},
    )
    second = evaluate_objective_state(
        **common,
        filled_shares_by_bucket={"a": 0.0, "b": 1.0},
    )

    # Symmetry gives identical aggregate score/risk values, but lineage must
    # still distinguish the two realised inventories.
    assert first["score"] == pytest.approx(second["score"])
    assert first["content_sha256"] != second["content_sha256"]
    assert first["filled_shares_by_bucket"] != second[
        "filled_shares_by_bucket"
    ]

    different_scale = evaluate_objective_state(
        **{
            **common,
            "concentration_scale_usdc": 20.0,
        },
        filled_shares_by_bucket={"a": 1.0, "b": 0.0},
    )
    assert different_scale["score"] == pytest.approx(first["score"])
    assert different_scale["content_sha256"] != first["content_sha256"]

    action_a = evaluate_objective_state(
        **{
            **common,
            "action_shares_by_bucket": {"a": 1.0},
        },
        filled_shares_by_bucket={"a": 0.0, "b": 0.0},
    )
    action_b = evaluate_objective_state(
        **{
            **common,
            "action_shares_by_bucket": {"b": 1.0},
        },
        filled_shares_by_bucket={"a": 0.0, "b": 0.0},
    )
    assert action_a["score"] == pytest.approx(action_b["score"])
    assert action_a["content_sha256"] != action_b["content_sha256"]

    cost_a = evaluate_objective_state(
        **{
            **common,
            "filled_cost_by_bucket": {"a": 0.5, "b": 0.0},
        },
        filled_shares_by_bucket={"a": 0.0, "b": 0.0},
    )
    cost_b = evaluate_objective_state(
        **{
            **common,
            "filled_cost_by_bucket": {"a": 0.0, "b": 0.5},
        },
        filled_shares_by_bucket={"a": 0.0, "b": 0.0},
    )
    assert cost_a["score"] == pytest.approx(cost_b["score"])
    assert cost_a["content_sha256"] != cost_b["content_sha256"]


def test_protected_tail_loss_uses_risk_scope_before_generic_floor_scope() -> None:
    result = evaluate_objective_state(
        probabilities={"cold": 0.2, "center": 0.6, "warm": 0.2},
        filled_shares_by_bucket={
            "cold": 0.0,
            "center": 0.0,
            "warm": 0.0,
        },
        filled_cost_by_bucket={
            "cold": 0.0,
            "center": 0.0,
            "warm": 0.0,
        },
        action_shares_by_bucket={"center": 1.0},
        action_cost_by_bucket={"center": 0.5},
        protected_buckets=["cold", "center", "warm"],
        shortfall_buckets=["cold"],
        breakthrough_buckets=["warm"],
        weights=ObjectiveWeights(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        execution_risk=0.0,
        concentration_scale_usdc=10.0,
    )

    assert result["protected_tail_buckets"] == ["cold", "warm"]
    assert result["protected_tail_loss"] == pytest.approx(0.2)


def test_positive_absolute_j1_cannot_authorize_when_j1_is_below_j0() -> None:
    raw = _snapshot(probability=0.05).to_dict()
    raw["snapshot_id"] = "positive-j1-negative-delta"
    raw["inventory_version"] = "profitable-filled-baseline"
    raw["buckets"][1]["filled_shares"] = 10.0
    raw["buckets"][1]["filled_cost_usdc"] = 1.0
    snapshot = TargetBasketSnapshot.from_mapping(raw)
    target = _target_policy()
    strict = _strict_policy()
    curve = build_fok_cost_curve(
        snapshot.buckets[0],
        target,
        quote_version=snapshot.quote_version,
    )

    result = evaluate_strict_delta(
        snapshot,
        target,
        strict,
        proposed_actions={
            "35°C": {
                "mode": "FOK",
                "shares": 1.0,
                "cost_curve": curve,
            }
        },
        protected_buckets=["35°C", "36°C"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        version_vector=_versions(snapshot, strict),
    )

    assert result["proposed_score"] > 0.0
    assert result["proposed_score"] < result["baseline_score"]
    assert result["delta_basket_score"] < 0.0
    assert result["authorized"] is False
