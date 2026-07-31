from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from bayes_basket_target.basket_kernel.contracts import (
    ContractError,
    TargetBasketPolicy,
    TargetBasketSnapshot,
    content_sha256,
)
from bayes_basket_target.delta_v2_artifact import (
    ARTIFACT_SCHEMA,
    DeltaV2PolicyArtifact,
)
from bayes_basket_target.delta_v2_runtime import (
    _target_candidates,
    compile_delta_v2_runtime_plan,
)


def _snapshot(*, pending_shares: float = 0.0) -> TargetBasketSnapshot:
    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "rkpk-v2-snapshot",
        "sequence": 1,
        "observed_at": "2026-07-28T03:00:00Z",
        "market_id": "rkpk-market",
        "station_id": "RKPK",
        "event_date": "2026-07-28",
        "quote_version": "quote-v2",
        "quote_observed_at": "2026-07-28T03:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v2",
        "order_state_version": "order-v2",
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "p0-v1",
            "posterior_sha256": "a" * 64,
        },
        "buckets": [
            {
                "bucket": "35",
                "ordinal": 35,
                "probability": 0.3,
                "bid_price": 0.04,
                "bid_levels": [{"price": 0.04, "shares": 10.0}],
                "ask_levels": [{"price": 0.20, "shares": 100.0}],
                "price_tick_size": 0.01,
                "maker_queue_ahead_shares": 2.0,
                "maker_queue_known": True,
            },
            {
                "bucket": "36",
                "ordinal": 36,
                "probability": 0.7,
                "bid_price": 0.05,
                "bid_levels": [{"price": 0.05, "shares": 10.0}],
                "ask_levels": [{"price": 0.20, "shares": 100.0}],
                "price_tick_size": 0.01,
                "maker_queue_ahead_shares": 2.0,
                "maker_queue_known": True,
                "pending_shares": pending_shares,
                "pending_notional_usdc": pending_shares * 0.1,
            },
        ],
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 1440.0,
    })


def _basket_policy() -> TargetBasketPolicy:
    return TargetBasketPolicy.from_mapping({
        "total_budget_usdc": 5.0,
        "base_coverage": 0.6,
        "maximum_coverage": 0.8,
        "entropy_coverage_gain": 0.0,
        "clip_min_notional_usdc": 0.01,
        "clip_max_notional_usdc": 1.0,
        "protected_floor_usdc": -5.0,
        "max_outside_loss_usdc": 5.0,
    })


def _delta_policy() -> DeltaV2PolicyArtifact:
    raw = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": "rkpk-p0-delta-v2-oos",
        "station_id": "RKPK",
        "stage": "P0",
        "season_bin": "summer",
        "maturity_bin": "d0",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v2",
        "value_weights": {
            "lambda_profit_floor": 0.0,
            "lambda_cvar": 0.0,
            "lambda_shortfall": 0.0,
            "lambda_breakthrough": 0.0,
            "lambda_overfill": 0.1,
            "lambda_excess_hhi": 0.0,
            "cvar_alpha": 0.1,
            "hhi_max": 1.0,
            "expected_pnl_scale": 1.0,
            "profit_floor_scale": 1.0,
            "cvar_scale": 1.0,
            "coverage_scale": 1.0,
            "overfill_scale": 1.0,
            "excess_hhi_scale": 1.0,
        },
        "execution_weights": {
            "epsilon_score": 0.0001,
            "lambda_wait": 0.0,
            "lambda_queue": 0.0,
            "lambda_adverse_selection": 0.0,
            "lambda_reprice": 0.0,
            "lambda_capital_lock": 0.0,
            "wait_scale": 1.0,
            "queue_scale": 1.0,
            "adverse_selection_scale": 1.0,
            "reprice_scale": 1.0,
            "capital_lock_scale": 1.0,
            "maximum_candidates": 16,
        },
        "cash_clip_grid": [0.25],
        "price_grid": [0.1],
        "timeout_grid_seconds": [60.0],
        "fill_distribution_artifact_sha256": "b" * 64,
        "training_evidence_sha256": "c" * 64,
        "train_start_date": "2026-05-01",
        "train_end_date": "2026-05-31",
        "holdout_start_date": "2026-06-01",
        "holdout_end_date": "2026-06-30",
        "sample_n": 200,
        "holdout_n": 80,
        "consecutive_forward_windows": 2,
        "compared_baselines": sorted(
            ["v1", "EV_ONLY", "ZERO_WEIGHT", "HOLD"]
        ),
        "oos_metrics": {"delta_score_lift": 0.1},
        "oos_gate_version": "delta-v2-oos-v1",
        "oos_gate_passed": True,
        "trained_through_utc": "2026-07-01T00:00:00Z",
        "valid_from_utc": "2026-07-02T00:00:00Z",
        "valid_until_utc": "2027-01-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE",
    }
    raw["content_sha256"] = content_sha256(raw)
    return DeltaV2PolicyArtifact.from_mapping(raw)


def _p1_delta_policy() -> DeltaV2PolicyArtifact:
    raw = _delta_policy().to_dict()
    raw.pop("content_sha256", None)
    raw["artifact_id"] = "rkpk-p1-delta-v2-oos"
    raw["stage"] = "P1"
    raw["content_sha256"] = content_sha256(raw)
    return DeltaV2PolicyArtifact.from_mapping(raw)


def _fill_resolver(**_: object) -> dict[str, object]:
    return {
        "fill_distribution": [
            {"fill_fraction": 0.0, "probability": 0.1},
            {"fill_fraction": 0.5, "probability": 0.2},
            {"fill_fraction": 1.0, "probability": 0.7},
        ],
        "execution_costs": {
            "wait": 0.0,
            "queue": 0.0,
            "adverse_selection": 0.0,
            "reprice": 0.0,
            "capital_lock": 0.0,
        },
    }


def test_v2_runtime_selects_complete_no_live_bundle_without_v1_optimizer() -> None:
    result = compile_delta_v2_runtime_plan(
        snapshot=_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["basket_optimizer_id"] == "DeltaBasketScoreV2"
    assert result["delta_v2_audit"]["authorized"] is True
    assert result["state"] == "TRADE"
    assert result["basket_state"] == "SEED"
    assert result["basket_state_reason"] == (
        "initial_complete_support_build"
    )
    assert result["basket_state_transition"]["positive_gap_buckets"] == (
        result["execution_gap_support"]
    )
    assert result["calls_order_gateway"] is False
    assert result["live_submit_authorized"] is False
    selected_targets = [
        row for row in result["targets"]
        if row["execution_candidate"] != "NONE"
    ]
    assert {row["bucket"] for row in selected_targets} == set(
        result["execution_gap_support"]
    )
    assert result["final_support"] == [
        row["bucket"] for row in result["targets"]
    ]
    assert all(row["target_shares"] > 0.0 for row in result["targets"])
    assert set(result["selected_buckets"]) == set(
        result["execution_gap_support"]
    )
    assert set(result["final_support"]) <= set(result["candidate_support"])


def test_terminal_snapshot_stops_new_bundle_even_when_delta_is_positive() -> None:
    raw = _snapshot().to_dict()
    raw["terminal"] = True

    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["state"] == "HOLD"
    assert result["basket_state"] == "STOP"
    assert result["selected_buckets"] == []
    assert result["delta_v2_action_selection"]["authorized"] is False
    assert result["delta_v2_action_selection"]["blockers"] == [
        "terminal_stop_adding"
    ]


def test_existing_support_positive_gap_is_replenish() -> None:
    raw = _snapshot().to_dict()
    for bucket in raw["buckets"]:
        bucket["active_before"] = True

    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["state"] == "TRADE"
    assert result["basket_state"] == "REPLENISH"


def test_exact_prior_core_is_not_inferred_from_all_active_inventory() -> None:
    raw = _snapshot().to_dict()
    raw["current_core_buckets"] = ["36"]
    for bucket in raw["buckets"]:
        bucket["active_before"] = True
    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["current_core_buckets"] == ["36"]
    assert result["core_transition"]["current_core_buckets"] == ["36"]
    assert set(result["selected_before"]) == {
        bucket["bucket"] for bucket in raw["buckets"]
    }
    assert result["basket_state_transition"]["added_buckets"] == []
    assert result["basket_state_transition"]["positive_gap_buckets"] == [
        "36"
    ]


def test_sparse_candidate_support_persists_contiguous_probability_core() -> None:
    raw = _snapshot().to_dict()
    raw["buckets"] = [
        {
            **copy.deepcopy(raw["buckets"][0]),
            "bucket": "35",
            "ordinal": 35,
            "probability": 0.45,
        },
        {
            **copy.deepcopy(raw["buckets"][0]),
            "bucket": "36",
            "ordinal": 36,
            "probability": 0.10,
        },
        {
            **copy.deepcopy(raw["buckets"][1]),
            "bucket": "37",
            "ordinal": 37,
            "probability": 0.45,
        },
    ]
    policy = TargetBasketPolicy.from_mapping({
        **_basket_policy().to_dict(),
        "base_coverage": 0.80,
        "maximum_coverage": 0.80,
    })

    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=policy,
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["candidate_support"] == ["35", "37"]
    assert result["core_buckets"] == ["35", "36", "37"]
    assert result["core_transition"]["next_core_buckets"] == [
        "35",
        "36",
        "37",
    ]


def test_multi_bucket_target_starts_every_gap_leg_in_one_bundle() -> None:
    raw = _snapshot().to_dict()
    raw["buckets"][0]["probability"] = 0.5
    raw["buckets"][1]["probability"] = 0.5
    policy = TargetBasketPolicy.from_mapping({
        **_basket_policy().to_dict(),
        "base_coverage": 0.8,
        "maximum_coverage": 0.9,
    })

    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=policy,
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["candidate_support"] == ["35", "36"]
    assert result["final_support"] == ["35", "36"]
    assert result["basket_completion_required"] is True
    assert result["basket_target_activated"] is True
    assert result["execution_gap_support"] == ["35", "36"]
    assert {row["bucket"] for row in result["targets"]} == {"35", "36"}
    assert all(row["target_shares"] > 0.0 for row in result["targets"])
    assert sum(
        row["execution_candidate"] != "NONE"
        for row in result["targets"]
    ) == 2
    assert result["selected_buckets"] == ["35", "36"]
    assert len(
        result["delta_v2_action_selection"]["selected_actions"]
    ) == 2


def test_min_tick_ask_reports_structural_maker_unavailability() -> None:
    raw = _snapshot().to_dict()
    raw["buckets"][0].update({
        "probability": 0.5,
        "bid_price": 0.0,
        "bid_levels": [],
        "ask_levels": [{"price": 0.01, "shares": 100.0}],
        "price_tick_size": 0.01,
    })
    raw["buckets"][1]["probability"] = 0.5
    policy = TargetBasketPolicy.from_mapping({
        **_basket_policy().to_dict(),
        "base_coverage": 0.8,
        "maximum_coverage": 0.9,
    })

    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=policy,
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["basket_target_activated"] is True
    assert result["state"] == "HOLD"
    assert result["basket_state"] == "HOLD"
    assert result["basket_state_reason"] == (
        "complete_gap_bundle_not_authorized"
    )
    assert result["maker_candidate_diagnostics"]["35"] == (
        "maker_post_only_price_unavailable_at_min_tick"
    )
    assert (
        "maker_post_only_price_unavailable_at_min_tick:35"
        in result["reason_codes"]
    )
    assert "missing_maker_candidate:35" in (
        result["delta_v2_action_selection"]["blockers"]
    )


def test_bid_only_selected_bucket_is_retained_without_buy_candidate() -> None:
    raw = _snapshot().to_dict()
    raw["buckets"][0]["probability"] = 0.5
    raw["buckets"][1]["probability"] = 0.5
    raw["buckets"][0].update({
        "ask_levels": [],
        "ask_available": False,
        "bid_price": 0.99,
        "bid_levels": [{"price": 0.99, "shares": 100.0}],
    })
    policy = TargetBasketPolicy.from_mapping({
        **_basket_policy().to_dict(),
        "base_coverage": 0.8,
        "maximum_coverage": 0.9,
    })

    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=policy,
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["state"] == "HOLD"
    assert result["maker_candidate_diagnostics"]["35"] == (
        "official_ws_ask_unavailable"
    )
    assert not any(
        row["bucket"] == "35"
        and row["execution_candidate"] != "NONE"
        for row in result["targets"]
    )


def test_profitable_single_bucket_cannot_bypass_incomplete_basket() -> None:
    raw = _snapshot().to_dict()
    raw["buckets"][0]["probability"] = 0.5
    raw["buckets"][1]["probability"] = 0.5
    raw["buckets"][0]["ask_levels"] = [
        {"price": 0.99, "shares": 100.0}
    ]
    raw["buckets"][1]["ask_levels"] = [
        {"price": 0.49, "shares": 100.0}
    ]
    policy = TargetBasketPolicy.from_mapping({
        **_basket_policy().to_dict(),
        "base_coverage": 0.8,
        "maximum_coverage": 0.9,
    })

    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=policy,
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["candidate_support"] == ["35", "36"]
    assert result["final_support"] == []
    assert result["execution_gap_support"] == []
    assert result["targets"] == []
    assert result["basket_completion_required"] is True
    assert result["basket_target_activated"] is False
    assert result["state"] == "HOLD"


def test_no_positive_joint_target_holds_with_empty_final_support() -> None:
    raw = _snapshot().to_dict()
    raw["buckets"][0]["probability"] = 0.5
    raw["buckets"][1]["probability"] = 0.5
    for bucket in raw["buckets"]:
        bucket["ask_levels"] = [{"price": 0.9, "shares": 100.0}]
    policy = TargetBasketPolicy.from_mapping({
        **_basket_policy().to_dict(),
        "base_coverage": 0.8,
        "maximum_coverage": 0.9,
    })

    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=policy,
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["candidate_support"] == ["35", "36"]
    assert result["final_support"] == []
    assert result["execution_gap_support"] == []
    assert result["selected_buckets"] == []
    assert result["targets"] == []
    assert result["basket_completion_required"] is True
    assert result["basket_target_activated"] is False
    assert result["state"] == "HOLD"


def test_target_grid_uses_each_bucket_official_ask_depth_not_static_price_grid() -> None:
    raw = _snapshot().to_dict()
    raw["buckets"][0]["ask_levels"] = [{"price": 0.10, "shares": 100.0}]
    raw["buckets"][1]["ask_levels"] = [{"price": 0.25, "shares": 100.0}]
    snapshot = TargetBasketSnapshot.from_mapping(raw)

    candidates = _target_candidates(
        snapshot=snapshot,
        desired=("35", "36"),
        artifact=_delta_policy(),
        capital_available_usdc=5.0,
        minimum_clip_notional_usdc=0.01,
    )
    both = next(
        row
        for row in candidates
        if row["shares_by_bucket"]["35"] > 0.0
        and row["shares_by_bucket"]["36"] > 0.0
    )

    assert both["pricing_basis"] == "official_ws_ask_depth"
    assert both["cost_by_bucket"]["35"] == pytest.approx(0.25)
    assert both["cost_by_bucket"]["36"] == pytest.approx(0.25)
    assert both["shares_by_bucket"]["35"] == pytest.approx(2.5)
    assert both["shares_by_bucket"]["36"] == pytest.approx(1.0)


def test_maker_limit_is_quote_anchored_and_not_the_static_reservation_grid_floor() -> None:
    raw = _snapshot().to_dict()
    raw["buckets"][1]["bid_price"] = 0.19
    raw["buckets"][1]["bid_levels"] = [{"price": 0.19, "shares": 10.0}]
    raw["buckets"][1]["ask_levels"] = [{"price": 0.21, "shares": 100.0}]

    result = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    selected = result["delta_v2_audit"]["selected_actions"][0]["action"]
    assert selected["bucket"] == "36"
    assert selected["limit_price"] == pytest.approx(0.20)
    assert selected["limit_price"] != min(_delta_policy().price_grid)
    assert selected["limit_price"] < 0.21


def test_confirmed_pending_reduces_gap_but_not_realised_profit_curve() -> None:
    result = compile_delta_v2_runtime_plan(
        snapshot=_snapshot(pending_shares=5.0),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=0.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["state"] == "HOLD"
    assert result["scenario_pnl_usdc"] == {"35": 0.0, "36": 0.0}


def test_resource_probe_failure_is_a_hard_fail_closed_gate() -> None:
    with pytest.raises(ContractError, match="resource_probe_failed"):
        compile_delta_v2_runtime_plan(
            snapshot=_snapshot(),
            basket_policy=_basket_policy(),
            delta_policy=_delta_policy(),
            fill_resolver=_fill_resolver,
            capital_available_usdc=5.0,
            minimum_profit_floor=-5.0,
            resource_probe_ok=False,
        )


def test_fok_is_fail_closed_until_maker_cancel_is_confirmed() -> None:
    raw = _snapshot().to_dict()
    raw["time_left_minutes"] = 0.0
    for bucket in raw["buckets"]:
        bucket["ask_levels"] = [{"price": 0.10, "shares": 100.0}]
    modes_without_cancel: list[str] = []
    modes_with_cancel: list[str] = []

    def resolver_without_cancel(**kwargs: object) -> dict[str, object]:
        modes_without_cancel.append(str(kwargs["order_mode"]))
        return _fill_resolver(**kwargs)

    def resolver_with_cancel(**kwargs: object) -> dict[str, object]:
        modes_with_cancel.append(str(kwargs["order_mode"]))
        if kwargs["order_mode"] == "FOK":
            return {
                "fill_distribution": [
                    {"fill_fraction": 1.0, "probability": 1.0},
                ],
                "execution_costs": {
                    "wait": 0.0,
                    "queue": 0.0,
                    "adverse_selection": 0.0,
                    "reprice": 0.0,
                    "capital_lock": 0.0,
                },
            }
        return _fill_resolver(**kwargs)

    base_kwargs = {
        "snapshot": TargetBasketSnapshot.from_mapping(raw),
        "basket_policy": _basket_policy(),
        "delta_policy": _delta_policy(),
        "capital_available_usdc": 5.0,
        "minimum_profit_floor": -5.0,
        "resource_probe_ok": True,
    }
    blocked = compile_delta_v2_runtime_plan(
        **base_kwargs,
        fill_resolver=resolver_without_cancel,
    )
    admitted = compile_delta_v2_runtime_plan(
        **base_kwargs,
        fill_resolver=resolver_with_cancel,
        confirmed_cancel_for_fok=True,
    )

    assert "FOK" not in modes_without_cancel
    assert "FOK" in modes_with_cancel
    assert blocked["fok_cancel_acknowledged"] is False
    assert admitted["fok_cancel_acknowledged"] is True


def test_low_high_low_is_content_deterministic() -> None:
    low = compile_delta_v2_runtime_plan(
        snapshot=_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )
    high_raw = _snapshot().to_dict()
    high_raw["buckets"][1]["ask_levels"] = [
        {"price": 0.30, "shares": 100.0}
    ]
    high = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(high_raw),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )
    replay = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(copy.deepcopy(_snapshot().to_dict())),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert low["content_sha256"] != high["content_sha256"]
    assert replay["content_sha256"] == low["content_sha256"]


def test_pending_zero_partial_zero_rewinds_deterministically() -> None:
    def compile_pending(value: float) -> dict[str, object]:
        return compile_delta_v2_runtime_plan(
            snapshot=_snapshot(pending_shares=value),
            basket_policy=_basket_policy(),
            delta_policy=_delta_policy(),
            fill_resolver=_fill_resolver,
            capital_available_usdc=5.0,
            minimum_profit_floor=-5.0,
            resource_probe_ok=True,
        )

    zero = compile_pending(0.0)
    partial = compile_pending(1.25)
    zero_replay = compile_pending(0.0)

    assert partial["content_sha256"] != zero["content_sha256"]
    assert zero_replay["content_sha256"] == zero["content_sha256"]


def test_p1_recomputes_complete_gap_bundle_beyond_three_changes() -> None:
    """Checkpoint/quote/fill/inventory changes are triggers, not tranches."""

    base = _snapshot().to_dict()
    base["evidence"]["probability_stage"] = "P1"
    base["evidence"].update({
        "reported_high_bucket": "35",
        "reported_high_ordinal": 35,
        "reported_high_observation_id": "metar-rkpk-p1",
        "hard_bound_id": "hard-bound-rkpk-p1",
    })
    policy = _p1_delta_policy()
    plans = []
    for generation in range(1, 7):
        raw = copy.deepcopy(base)
        raw["snapshot_id"] = f"rkpk-p1-snapshot-{generation}"
        raw["sequence"] = generation
        raw["evidence"]["posterior_version"] = f"p1-v{generation}"
        raw["evidence"]["posterior_sha256"] = (
            f"{generation:x}" * 64
        )[:64]
        raw["quote_version"] = f"quote-p1-v{generation}"
        raw["inventory_version"] = f"inventory-p1-v{generation}"
        raw["order_state_version"] = f"order-p1-v{generation}"
        raw["buckets"][0]["filled_shares"] = generation * 0.01
        raw["buckets"][0]["filled_cost_usdc"] = generation * 0.0005
        raw["buckets"][1]["filled_shares"] = generation * 0.01
        raw["buckets"][1]["filled_cost_usdc"] = generation * 0.0005

        plan = compile_delta_v2_runtime_plan(
            snapshot=TargetBasketSnapshot.from_mapping(raw),
            basket_policy=_basket_policy(),
            delta_policy=policy,
            fill_resolver=_fill_resolver,
            capital_available_usdc=5.0,
            minimum_profit_floor=-5.0,
            resource_probe_ok=True,
        )
        plans.append(plan)

    assert len(plans) > 3
    assert all(plan["state"] == "TRADE" for plan in plans)
    assert all(plan["basket_completion_required"] is True for plan in plans)
    assert all(
        set(plan["selected_buckets"]) == set(plan["execution_gap_support"])
        for plan in plans
    )
    assert all(
        len(plan["delta_v2_action_selection"]["selected_actions"])
        == len(plan["execution_gap_support"])
        for plan in plans
    )
    assert len({plan["content_sha256"] for plan in plans}) == len(plans)


@pytest.mark.parametrize(
    "trigger",
    ["checkpoint", "quote", "fill", "inventory"],
)
def test_each_p1_authority_change_recomputes_a_complete_gap_bundle(
    trigger: str,
) -> None:
    raw = _snapshot().to_dict()
    raw["evidence"].update({
        "probability_stage": "P1",
        "reported_high_bucket": "35",
        "reported_high_ordinal": 35,
        "reported_high_observation_id": "metar-rkpk-p1",
        "hard_bound_id": "hard-bound-rkpk-p1",
    })
    raw.update({
        "checkpoint_changed": False,
        "quote_changed": False,
        "fill_changed": False,
        "time_gate_changed": False,
    })
    if trigger == "checkpoint":
        raw["checkpoint_changed"] = True
        raw["evidence"]["posterior_version"] = "p1-checkpoint-v2"
        raw["evidence"]["posterior_sha256"] = "e" * 64
    elif trigger == "quote":
        raw["quote_changed"] = True
        raw["quote_version"] = "quote-p1-v2"
        raw["buckets"][0]["bid_price"] = 0.05
        raw["buckets"][0]["bid_levels"] = [
            {"price": 0.05, "shares": 10.0}
        ]
    elif trigger == "fill":
        raw["fill_changed"] = True
        raw["buckets"][0]["filled_shares"] = 0.05
        raw["buckets"][0]["filled_cost_usdc"] = 0.0025
    else:
        raw["inventory_version"] = "inventory-p1-v2"
        raw["buckets"][1]["filled_shares"] = 0.05
        raw["buckets"][1]["filled_cost_usdc"] = 0.0025

    plan = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=_basket_policy(),
        delta_policy=_p1_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert plan["state"] == "TRADE"
    assert plan["basket_completion_required"] is True
    assert set(plan["selected_buckets"]) == set(
        plan["execution_gap_support"]
    )
    assert len(plan["delta_v2_action_selection"]["selected_actions"]) == len(
        plan["execution_gap_support"]
    )


def test_target_execution_code_has_no_lifetime_three_batch_cap() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = re.compile(
        r"\b(?:MAX_(?:ENTRY|BUILD|EXECUTION)_BATCHES|"
        r"MAX_BATCH_COUNT|THREE_BATCH(?:ES)?|"
        r"(?:entry|build|execution)_batch_count)\b",
        re.IGNORECASE,
    )
    production_files = [
        root / "bayes_basket_target/production_worker.py",
        root / "bayes_basket_target/p0_preselection.py",
        root / "bayes_basket_target/delta_v2_runtime.py",
        root / "bayes_basket_target/resident_market_ingress.py",
    ]

    hits = {
        str(path.relative_to(root)): sorted(set(forbidden.findall(
            path.read_text(encoding="utf-8")
        )))
        for path in production_files
        if forbidden.search(path.read_text(encoding="utf-8"))
    }
    assert hits == {}
