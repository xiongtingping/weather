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
from bayes_basket_target import delta_v2_runtime
from bayes_basket_target.delta_v2_runtime import (
    _target_candidates,
    compile_delta_v2_runtime_plan,
)



@pytest.fixture(autouse=True)
def _certainty_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fixtures are sharp on purpose; the gate is tested separately.

    The certainty gate refuses to open into a market that has finished
    pricing the day, and a hand-built fixture distribution is almost always
    sharp -- so leaving it on would make every test in this file assert
    "the gate fired" instead of the property it was written for.  Its own
    behaviour is pinned in test_certainty_gate_* below.
    """

    monkeypatch.setattr(
        delta_v2_runtime, "CERTAINTY_GATE_SIGNALS", ()
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
        # These fixtures exercise maker limits, FOK sequencing and clip
        # floors on deliberately tiny books.  The three-rung seating floor
        # is a separate rule with its own tests; pinning it to one here
        # keeps each test about the thing it is named for.
        "minimum_support_buckets": 1,
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
            # 64, not 16.  The fixture is a policy the shipped ladder can
            # actually run: eight rungs across a window the solver may take
            # to four buckets is thirty-six candidates on this fixture's
            # single clip and single timeout, and sixteen refused them.  The
            # ceiling itself is asserted where it belongs, in
            # test_bayes_basket_target_delta_v2.py, which sets its own.
            "maximum_candidates": 64,
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
    # The core stays exactly ["36"] -- that is what this test guards.  What is
    # *bought* is a separate decision: both buckets offer at 0.20 against 0.30
    # and 0.70 of belief, so the objective seats the pair and covers the day
    # outright.  Core inference and support selection are allowed to differ.
    assert result["basket_state_transition"]["positive_gap_buckets"] == [
        "35",
        "36",
    ]
    assert result["basket_support_selection"]["seated"]["buckets"] == [
        "35",
        "36",
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

    # The probability core admits 35 and 37 and skips 36, but a basket with a
    # hole is priced for an outcome it does not cover, so 36 is bridged in
    # rather than 37 being dropped for sitting past the hole.
    assert result["candidate_support"] == ["35", "36", "37"]
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
    # Tight books on purpose.  The target is priced at the band this lane
    # would rest in, not at the offer (2026-08-26), so a wide spread would
    # make the basket affordable and this fixture would stop describing the
    # case it is named for.  One tick of spread leaves the band on a single
    # rung, which is where "the market is dear" survives the change.
    raw = _snapshot().to_dict()
    raw["buckets"][0]["probability"] = 0.5
    raw["buckets"][1]["probability"] = 0.5
    raw["buckets"][0]["bid_price"] = 0.98
    raw["buckets"][0]["bid_levels"] = [{"price": 0.98, "shares": 100.0}]
    raw["buckets"][0]["ask_levels"] = [
        {"price": 0.99, "shares": 100.0}
    ]
    raw["buckets"][1]["bid_price"] = 0.48
    raw["buckets"][1]["bid_levels"] = [{"price": 0.48, "shares": 100.0}]
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
    # Same reason as above: one tick of spread, so the band the target is
    # priced at is as dear as the offer and the basket still cannot pay for
    # itself.
    raw = _snapshot().to_dict()
    raw["buckets"][0]["probability"] = 0.5
    raw["buckets"][1]["probability"] = 0.5
    for bucket in raw["buckets"]:
        bucket["bid_price"] = 0.89
        bucket["bid_levels"] = [{"price": 0.89, "shares": 100.0}]
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
        bankroll_usdc=_basket_policy().total_budget_usdc,
        probabilities_by_bucket={
            row.bucket: row.probability for row in snapshot.buckets
        },
    )
    both = next(
        row
        for row in candidates
        if row["shares_by_bucket"]["35"] > 0.0
        and row["shares_by_bucket"]["36"] > 0.0
    )

    # Priced at the band this lane rests in, not at the offer it never
    # crosses (2026-08-26).  Bucket 35's band is 0.05-0.09 (mean 0.07) and
    # 36's is 0.06-0.24 (mean 0.14625), so the same 0.25 clip buys more
    # shares than crossing would -- which is the whole point: the objective
    # used to size, and refuse, against a price the engine never pays.
    assert both["pricing_basis"] == "maker_resting_band"
    assert both["cost_by_bucket"]["35"] == pytest.approx(0.25)
    assert both["cost_by_bucket"]["36"] == pytest.approx(0.25)
    assert both["shares_by_bucket"]["35"] == pytest.approx(0.25 / 0.07)
    assert both["shares_by_bucket"]["36"] == pytest.approx(0.25 / 0.14625)


def test_target_grid_can_still_be_priced_at_the_crossing_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-2026-08-26 basis, pinned so the two can be replayed apart.

    Per-bucket depth from the book either way -- that is what this pair of
    tests has always been about -- but the crossing arm pays the offer, so
    the same clip buys fewer shares.
    """

    monkeypatch.setattr(
        delta_v2_runtime, "TARGET_PRICING_BASIS", "ask_depth"
    )
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
        bankroll_usdc=_basket_policy().total_budget_usdc,
        probabilities_by_bucket={
            row.bucket: row.probability for row in snapshot.buckets
        },
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

    selected = next(
        entry["action"]
        for entry in result["delta_v2_audit"]["selected_actions"]
        if entry["action"]["bucket"] == "36"
    )
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
    # Same evening state as the parametrized trigger test above: the live
    # set the amended clause 38 floor protects is the pruned one.
    base["buckets"][0]["probability"] = 0.0
    base["buckets"][1]["probability"] = 1.0
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
    # Clause 38 (amended): with the live set coverable, the floor protects
    # every live bucket.  These tests exercise recomputation triggers, not
    # floor shape, so the evening state they model is the pruned one -- the
    # hard bound has cleared 35 and the whole mass sits on 36.  The earlier
    # 0.3/0.7 split relied on the pre-amendment drift that scoped the floor
    # to the chosen support and let a one-bucket basket run naked past a
    # thirty-percent live neighbour.
    raw["buckets"][0]["probability"] = 0.0
    raw["buckets"][1]["probability"] = 1.0
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


def test_a_sub_floor_clip_stays_visible_to_the_sizing_gate() -> None:
    # Filtering these out of the q* grid does stop an unfillable target, but it
    # also hides the bucket from the sizing decisions, and ``clip_below_minimum``
    # is raised there -- so the early filter traded one silent hold for another.
    # The candidate must reach the gate that owns the refusal.
    policy = TargetBasketPolicy.from_mapping({
        **_basket_policy().to_dict(),
        "clip_min_notional_usdc": 1.0,
    })

    result = compile_delta_v2_runtime_plan(
        snapshot=_snapshot(),
        basket_policy=policy,
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    assert result["basket_target_activated"] is True


def test_curve_objective_prefers_flat_curve_and_keeps_sound_bets() -> None:
    """The ranking charges spread, but never refuses a bet that cannot hurt.

    Both properties in one place because they pull against each other: the
    first is the whole point of the change, and the second is what the plain
    quadratic charge got wrong -- it scored a 50/50 paying +24.5 or -0.5
    against a 5 USDC book at -1.48 and held.
    """

    from bayes_basket_target.basket_kernel.delta_v2 import (
        CURVE_OBJECTIVE_FORM,
        curve_score,
        evaluate_basket_value_v2,
        solve_target_inventory_v2,
    )

    weights = _delta_policy().value_weights
    probabilities = {"35": 0.5, "36": 0.5}

    def _audit(shares, costs):
        return evaluate_basket_value_v2(
            probabilities=probabilities,
            shares_by_bucket=shares,
            cost_by_bucket=costs,
            protected_buckets=("35", "36"),
            shortfall_buckets=(),
            breakthrough_buckets=(),
            weights=weights,
        )

    # Equal mean, different spread: the flat curve must win.
    flat = _audit({"35": 10.0, "36": 10.0}, {"35": 2.0, "36": 2.0})
    spiked = _audit({"35": 20.0, "36": 0.0}, {"35": 4.0, "36": 0.0})
    assert flat["expected_pnl"] == pytest.approx(spiked["expected_pnl"])
    assert flat["variance"] < spiked["variance"]
    assert curve_score(flat, bankroll_usdc=30.0) > curve_score(
        spiked, bankroll_usdc=30.0
    )

    # A large payoff next to a small book is still taken.
    sound = _audit({"35": 25.0, "36": 0.0}, {"35": 0.5, "36": 0.0})
    assert curve_score(sound, bankroll_usdc=5.0) > 0.0

    # A smaller Kelly fraction ranks the same basket more cautiously.
    assert curve_score(spiked, bankroll_usdc=30.0, kelly_fraction=0.1) < (
        curve_score(spiked, bankroll_usdc=30.0, kelly_fraction=1.0)
    )

    # The solver honours the form, and reports what it ranked by.
    solution = solve_target_inventory_v2(
        probabilities=probabilities,
        feasible_inventory_candidates=[
            {
                "candidate_id": "flat",
                "shares_by_bucket": {"35": 10.0, "36": 10.0},
                "cost_by_bucket": {"35": 2.0, "36": 2.0},
            },
            {
                "candidate_id": "spiked",
                "shares_by_bucket": {"35": 20.0, "36": 0.0},
                "cost_by_bucket": {"35": 4.0, "36": 0.0},
            },
        ],
        protected_buckets=("35", "36"),
        shortfall_buckets=(),
        breakthrough_buckets=(),
        weights=weights,
        objective_form=CURVE_OBJECTIVE_FORM,
        bankroll_usdc=30.0,
    )
    assert solution["winner_candidate_id"] == "flat"
    assert solution["objective_form"] == CURVE_OBJECTIVE_FORM
    assert solution["target_variance"] == pytest.approx(flat["variance"])

    # The legacy form is still exactly itself, and still the default.
    legacy = solve_target_inventory_v2(
        probabilities=probabilities,
        feasible_inventory_candidates=[
            {
                "candidate_id": "flat",
                "shares_by_bucket": {"35": 10.0, "36": 10.0},
                "cost_by_bucket": {"35": 2.0, "36": 2.0},
            },
            {
                "candidate_id": "spiked",
                "shares_by_bucket": {"35": 20.0, "36": 0.0},
                "cost_by_bucket": {"35": 4.0, "36": 0.0},
            },
        ],
        protected_buckets=("35", "36"),
        shortfall_buckets=(),
        breakthrough_buckets=(),
        weights=weights,
    )
    assert legacy["objective_form"] == "value_v2"

    with pytest.raises(ContractError, match="delta_v2_curve_bankroll_required"):
        solve_target_inventory_v2(
            probabilities=probabilities,
            feasible_inventory_candidates=[{
                "candidate_id": "flat",
                "shares_by_bucket": {"35": 1.0, "36": 1.0},
                "cost_by_bucket": {"35": 0.2, "36": 0.2},
            }],
            protected_buckets=("35", "36"),
            shortfall_buckets=(),
            breakthrough_buckets=(),
            weights=weights,
            objective_form=CURVE_OBJECTIVE_FORM,
        )


@pytest.fixture
def _continuous_optimum_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """These three test the generator itself, not whether it is offered.

    The continuous shape was switched off in production on 2026-08-22 after
    the ledger measured it 275 USDC behind the centre-anchored tilt over 187
    paired station-days.  Its correctness contracts still have to hold: the
    switch is a policy about which shapes compete, and a generator nobody
    can turn back on is a generator nobody can measure again.
    """

    monkeypatch.setattr(
        delta_v2_runtime, "CONTINUOUS_OPTIMUM_CANDIDATES_ENABLED", True
    )


def test_candidate_support_contract_is_enforced_at_the_boundary(_continuous_optimum_enabled: None) -> None:
    """A new generator must fail loudly, not silently turn a trade into a hold.

    ``basket_target_activated`` is a literal comparison against the complete
    kind, and the support every later stage reads collapses to empty when it
    fails.  The first attempt at a second generator named its candidates
    after their provenance, passed every structural check, and quietly held
    the basket -- so provenance now lives in its own field and the three
    invariants are checked where a generator is introduced.
    """

    from bayes_basket_target.delta_v2_runtime import (
        CANDIDATE_KIND_BASELINE,
        CANDIDATE_KIND_COMPLETE,
        _assert_candidate_support_contract,
        _target_candidates,
    )

    support = ("35", "36")
    baseline = {
        "candidate_id": "base",
        "candidate_kind": CANDIDATE_KIND_BASELINE,
        "candidate_origin": "CLIP_GRID_PRODUCT",
        "shares_by_bucket": {"35": 0.0, "36": 0.0, "37": 2.0},
        "cost_by_bucket": {},
    }
    complete = {
        "candidate_id": "full",
        "candidate_kind": CANDIDATE_KIND_COMPLETE,
        "candidate_origin": "CONTINUOUS_LOG_OPTIMUM",
        "shares_by_bucket": {"35": 4.0, "36": 3.0, "37": 2.0},
        "cost_by_bucket": {},
    }
    # Existing inventory outside the support (37) is shared by every
    # candidate and is not an increment, so it is not a breach.
    assert len(
        _assert_candidate_support_contract(
            [baseline, complete], ordered_support=support
        )
    ) == 2

    partial = {**complete, "shares_by_bucket": {"35": 4.0, "36": 0.0, "37": 2.0}}
    with pytest.raises(
        ContractError, match="delta_v2_candidate_complete_basket_incomplete"
    ):
        _assert_candidate_support_contract(
            [baseline, partial], ordered_support=support
        )

    strays = {
        **complete,
        "shares_by_bucket": {"35": 4.0, "36": 3.0, "37": 9.0},
    }
    with pytest.raises(
        ContractError,
        match="delta_v2_candidate_moves_bucket_outside_support",
    ):
        _assert_candidate_support_contract(
            [baseline, strays], ordered_support=support
        )

    with pytest.raises(
        ContractError, match="delta_v2_candidate_baseline_not_unique"
    ):
        _assert_candidate_support_contract(
            [baseline, {**baseline, "candidate_id": "base2"}, complete],
            ordered_support=support,
        )

    with pytest.raises(ContractError, match="delta_v2_candidate_kind_invalid"):
        _assert_candidate_support_contract(
            [baseline, {**complete, "candidate_kind": "CONTINUOUS_LOG_OPTIMUM"}],
            ordered_support=support,
        )

    # The shipped generators satisfy it, and the continuous one adds the
    # resolution the grid cannot express.
    produced = _target_candidates(
        snapshot=_snapshot(), desired=support, artifact=_delta_policy(),
        capital_available_usdc=5.0,
        minimum_clip_notional_usdc=_basket_policy().clip_min_notional_usdc,
        bankroll_usdc=_basket_policy().total_budget_usdc,
        probabilities_by_bucket={
            row.bucket: row.probability for row in _snapshot().buckets
        },
    )
    origins = {row["candidate_origin"] for row in produced}
    assert origins == {"CLIP_GRID_PRODUCT", "CONTINUOUS_LOG_OPTIMUM"}
    grid_costs = {
        round(sum(row["cost_by_bucket"].values()), 2)
        for row in produced
        if row["candidate_origin"] == "CLIP_GRID_PRODUCT"
    }
    continuous_costs = {
        round(sum(row["cost_by_bucket"].values()), 2)
        for row in produced
        if row["candidate_origin"] == "CONTINUOUS_LOG_OPTIMUM"
    }
    assert continuous_costs - grid_costs == continuous_costs


def test_continuous_candidate_seats_the_clip_floor_inside_the_solve(_continuous_optimum_enabled: None) -> None:
    # The minimum clip is a lower bound on the decision, so it is solved as
    # one.  Pasting it on afterwards spent money the optimum had not
    # allocated and then rescaled the whole vector to fit, which could
    # re-breach the floor and drop the candidate outright.
    snapshot = _snapshot()
    floor = 0.50
    capital = 5.0
    produced = _target_candidates(
        snapshot=snapshot,
        desired=("35", "36"),
        artifact=_delta_policy(),
        capital_available_usdc=capital,
        minimum_clip_notional_usdc=floor,
        bankroll_usdc=_basket_policy().total_budget_usdc,
        probabilities_by_bucket={
            row.bucket: row.probability for row in snapshot.buckets
        },
    )
    continuous = [
        row for row in produced
        if row["candidate_origin"] == "CONTINUOUS_LOG_OPTIMUM"
    ]
    # The floor never drops the candidate any more, and the solve is what
    # divides whatever is left after every leg is seated.
    assert continuous
    for row in continuous:
        legs = [row["cost_by_bucket"][bucket] for bucket in ("35", "36")]
        assert all(cost + 1e-9 >= floor for cost in legs)
        assert sum(legs) <= capital + 1e-9


def test_continuous_candidate_reads_the_belief_the_ranking_scores(_continuous_optimum_enabled: None) -> None:
    # An off-centre window has its claim deflated before the objective sees
    # it.  The generator must propose against that deflated distribution, not
    # against the snapshot's, or it offers the optimum of a belief nobody
    # ranks.
    snapshot = _snapshot()
    common = dict(
        snapshot=snapshot,
        desired=("35", "36"),
        artifact=_delta_policy(),
        capital_available_usdc=5.0,
        minimum_clip_notional_usdc=0.01,
        bankroll_usdc=_basket_policy().total_budget_usdc,
    )

    def continuous_costs(probabilities: dict[str, float]) -> set[tuple[float, ...]]:
        return {
            tuple(
                round(row["cost_by_bucket"][bucket], 6)
                for bucket in ("35", "36")
            )
            for row in _target_candidates(
                **common, probabilities_by_bucket=probabilities
            )
            if row["candidate_origin"] == "CONTINUOUS_LOG_OPTIMUM"
        }

    snapshot_belief = {row.bucket: row.probability for row in snapshot.buckets}
    deflated = {
        bucket: value * (0.85 if bucket in {"35", "36"} else 1.0)
        for bucket, value in snapshot_belief.items()
    }
    assert continuous_costs(snapshot_belief) != continuous_costs(deflated)


def _sizing(
    *,
    bucket: str,
    depth_cash: float,
    clip: float,
    limit: float,
    clamped_clip_authorized: bool = False,
):
    from bayes_basket_target.cash_phase import (
        BasketCashPhaseInput,
        BasketCashPhasePolicyArtifact,
        OrderSizingDecision,
        evaluate_cash_phase,
    )

    artifact = BasketCashPhasePolicyArtifact.create(
        policy_version="rescore-test-v1",
        station_id="RKPK",
        station_timezone="Asia/Seoul",
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase={"HEAVY": 10.0, "STANDARD": 10.0, "TAIL": 10.0},
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-09-01T00:00:00Z",
        evidence_status="DESIGN_CANDIDATE",
    )
    phase = evaluate_cash_phase(
        artifact,
        BasketCashPhaseInput.from_mapping({
            "policy_version": artifact.policy_version,
            "station_id": "RKPK",
            "current_phase": "HEAVY",
            "event_date_local": "2026-07-28",
            "observed_at_utc": "2026-07-28T03:00:00Z",
            "station_timezone": "Asia/Seoul",
            "checkpoint_id": "ck-1",
        }),
    )
    return OrderSizingDecision.compute(
        bucket=bucket,
        phase_decision=phase,
        delta_shares=1000.0,
        r_limit=limit,
        remaining_budget=12.0,
        executable_depth_cash=depth_cash,
        candidate_present=True,
        target_clip_notional_usdc=clip,
        minimum_clip_notional_usdc=0.01,
        cash_template_caps_target=False,
        share_quantum=0.01,
        clamped_clip_authorized=clamped_clip_authorized,
    )


def test_a_cut_bundle_is_re_authorized_before_it_is_sent() -> None:
    # The clamp path is unreachable in the current production configuration
    # by construction, which is exactly why it is tested directly: an
    # unexercised fail-open is how a refusal quietly becomes an approval.
    from bayes_basket_target.basket_kernel.delta_v2 import evaluate_action_v2
    from bayes_basket_target.runtime import _rescore_clamped_bundle

    snapshot = _snapshot()
    policy = _basket_policy()
    delta_policy = _delta_policy()
    probabilities = {row.bucket: row.probability for row in snapshot.buckets}
    actions = [
        evaluate_action_v2(
            event_id="RKPK-2026-07-28",
            bucket=bucket,
            cash_clip=1.0,
            limit_price=0.10,
            timeout_seconds=60.0,
            order_mode="MAKER",
            fill_distribution=[
                {"fill_fraction": 0.0, "probability": 0.1},
                {"fill_fraction": 0.5, "probability": 0.2},
                {"fill_fraction": 1.0, "probability": 0.7},
            ],
            probabilities=probabilities,
            current_shares_by_bucket={key: 0.0 for key in probabilities},
            current_cost_by_bucket={key: 0.0 for key in probabilities},
            target_shares_by_bucket={key: 12.0 for key in probabilities},
            protected_buckets=("35", "36"),
            shortfall_buckets=(),
            breakthrough_buckets=(),
            value_weights=delta_policy.value_weights,
            execution_weights=delta_policy.execution_weights,
            hard_gates={gate: True for gate in (
                "unique_ssot", "lineage_complete", "registry_timezone_matches",
                "bucket_contract_matches", "official_ws_fresh",
                "inventory_known", "order_state_known",
                "positive_inventory_gap", "budget_ok", "profit_floor_ok",
                "resource_probe_ok", "reservation_price_ok",
            )},
            capital_available_usdc=12.0,
            minimum_profit_floor=-12.0,
            reservation_price=0.8,
            wait_cost=0.0, queue_risk=0.0, adverse_selection=0.0,
            reprice_risk=0.0, capital_lock_cost=0.0,
        )
        for bucket in ("35", "36")
    ]
    plan = {
        "execution_gap_support": ["35", "36"],
        "bundle_scored_scopes": {
            "protected_buckets": ["35", "36"],
            "shortfall_buckets": [],
            "breakthrough_buckets": [],
        },
        "target_solution": {
            "target_shares_by_bucket": {key: 12.0 for key in probabilities},
        },
    }

    def _call(decisions, resized, **overrides):
        return _rescore_clamped_bundle(
            sizing_decisions=decisions,
            resize=lambda *, clamped_clip_authorized: (resized, 12.0),
            delta_plan=plan,
            snapshot=snapshot,
            delta_policy=delta_policy,
            policy=overrides.get("policy", policy),
            capital_available_usdc=12.0,
            selected_action_evaluations=actions,
        )

    uncut = [
        _sizing(bucket=bucket, depth_cash=99.0, clip=1.0, limit=0.10)
        for bucket in ("35", "36")
    ]
    assert all(item.reason_code == "sized_discrete" for item in uncut)
    # Nothing was cut, so nothing is re-scored and no work is done.
    assert _call(uncut, uncut) is None

    cut = [
        _sizing(bucket="35", depth_cash=0.40, clip=1.0, limit=0.10),
        _sizing(bucket="36", depth_cash=99.0, clip=1.0, limit=0.10),
    ]
    assert cut[0].reason_code == "sizing_clamped_below_authorized_clip"
    permitted = [
        _sizing(
            bucket="35", depth_cash=0.40, clip=1.0, limit=0.10,
            clamped_clip_authorized=True,
        ),
        _sizing(bucket="36", depth_cash=99.0, clip=1.0, limit=0.10),
    ]
    verdict = _call(cut, permitted)
    assert verdict is not None
    assert verdict["sized_shares_by_bucket"]["35"] == pytest.approx(
        permitted[0].q_order
    )
    assert verdict["authorized"] is True

    # A floor the full basket cleared, broken by the cut, refuses it.
    strict = TargetBasketPolicy.from_mapping({
        **_basket_policy().to_dict(),
        "minimum_expected_pnl_after_usdc": -0.05,
    })
    refused = _call(cut, permitted, policy=strict)
    assert refused["authorized"] is False
    assert "profit_floor_ok" in refused["blockers"]


def test_a_station_that_holds_something_can_still_compile() -> None:
    # The candidate generator used to exempt a leg the station already held
    # from the atomicity filter, while the contract those candidates are
    # held to measures increments against the baseline and demands every
    # seated leg positive.  The two disagreed exactly when inventory existed
    # in a seated bucket: the exempted combination arrived as a complete
    # basket with a zero increment, was rejected as incomplete, and took the
    # window's whole solve with it -- every window, so
    # delta_v2_no_solvable_support_window, so no plan for the station at all.
    #
    # It never fired in production because production has held nothing since
    # 2026-07-29, which is also why it has to be pinned here: the second and
    # third stages of the three-stage flow only ever run on a held book.
    raw = _snapshot().to_dict()
    raw["buckets"][0]["filled_shares"] = 14.0
    raw["buckets"][0]["filled_cost_usdc"] = 2.8
    held = TargetBasketSnapshot.from_mapping(raw)

    candidates = _target_candidates(
        snapshot=held,
        desired=("35", "36"),
        artifact=_delta_policy(),
        capital_available_usdc=5.0,
        minimum_clip_notional_usdc=0.01,
        bankroll_usdc=_basket_policy().total_budget_usdc,
        probabilities_by_bucket={
            row.bucket: row.probability for row in held.buckets
        },
    )
    baseline = next(
        row for row in candidates
        if row["candidate_kind"] == "NO_NEW_ACTION_BASELINE"
    )
    complete = [
        row for row in candidates
        if row["candidate_kind"] == "COMPLETE_BASKET_TARGET"
    ]
    assert complete
    for row in complete:
        for bucket in ("35", "36"):
            assert (
                row["shares_by_bucket"][bucket]
                > baseline["shares_by_bucket"][bucket] + 1e-9
            )

    # And the whole compile survives it, which is the property that was
    # actually broken.
    plan = compile_delta_v2_runtime_plan(
        snapshot=held,
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )
    assert plan["state"] in {"TRADE", "HOLD"}
    for target in plan["targets"]:
        assert target["delta_shares"] >= 0.0


def test_certainty_gate_reads_the_two_signs_off_the_entry_distribution(
    _certainty_gate_off: None,
) -> None:
    # Sharp is the entry distribution's own top-two mass; stable needs the
    # centre to have held still across the lookback, and says so honestly
    # when the day has not produced enough readings yet.
    flat = {"33C": 0.25, "34C": 0.25, "35C": 0.25, "36C": 0.25}
    peaked = {"33C": 0.05, "34C": 0.05, "35C": 0.45, "36C": 0.45}
    assert delta_v2_runtime.certainty_signals(
        probabilities=flat
    ) == {"stable": False, "sharp": False}
    assert delta_v2_runtime.certainty_signals(
        probabilities=peaked
    )["sharp"] is True
    # Three prior readings on the same centre, plus this one, is stable.
    assert delta_v2_runtime.certainty_signals(
        probabilities=flat, prior_centre_buckets=["33C", "33C", "33C"]
    )["stable"] is True
    # One of them moved: not stable.
    assert delta_v2_runtime.certainty_signals(
        probabilities=flat, prior_centre_buckets=["33C", "34C", "33C"]
    )["stable"] is False
    # Too few readings is not stability.
    assert delta_v2_runtime.certainty_signals(
        probabilities=flat, prior_centre_buckets=["33C", "33C"]
    )["stable"] is False


def test_certainty_gate_refuses_a_finished_market_by_name(
    _certainty_gate_off: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The refusal names which sign fired, because "wait" and "cannot" are
    # different answers and the caller absorbs only the first.
    monkeypatch.setattr(
        delta_v2_runtime, "CERTAINTY_GATE_SIGNALS", ("stable", "sharp")
    )
    with pytest.raises(ContractError, match="delta_v2_certainty_gate_lit"):
        compile_delta_v2_runtime_plan(
            snapshot=_snapshot(),
            basket_policy=_basket_policy(),
            delta_policy=_delta_policy(),
            fill_resolver=_fill_resolver,
            capital_available_usdc=5.0,
            minimum_profit_floor=-5.0,
            resource_probe_ok=True,
        )


def _tilt_snapshot() -> TargetBasketSnapshot:
    """Three rungs, the outer one much the cheapest.

    The point of the shape is that the centre is the position and the flanks
    are the hedge around it.  A cheap flank is exactly where cash-weighting
    and share-weighting disagree, so the fixture makes one flank cheap.
    """

    def _row(bucket: str, ordinal: int, probability: float, ask: float):
        return {
            "bucket": bucket,
            "ordinal": ordinal,
            "probability": probability,
            "bid_price": max(0.01, round(ask - 0.03, 2)),
            "bid_levels": [
                {"price": max(0.01, round(ask - 0.03, 2)), "shares": 500.0}
            ],
            "ask_levels": [{"price": ask, "shares": 5000.0}],
            "price_tick_size": 0.01,
            "maker_queue_ahead_shares": 2.0,
            "maker_queue_known": True,
        }

    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "tilt-snapshot",
        "sequence": 1,
        "observed_at": "2026-08-26T03:00:00Z",
        "market_id": "tilt-market",
        "station_id": "KHOU",
        "event_date": "2026-08-26",
        "quote_version": "quote-tilt",
        "quote_observed_at": "2026-08-26T03:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-tilt",
        "order_state_version": "order-tilt",
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "p0-v1",
            "posterior_sha256": "a" * 64,
        },
        "buckets": [
            _row("94-95", 3, 0.30, 0.37),
            _row("96-97", 4, 0.40, 0.32),
            _row("98-99", 5, 0.30, 0.18),
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


def test_the_centre_anchored_shape_sizes_shares_not_cash() -> None:
    """Settlement pays per share, so the weights must size shares.

    Splitting the cash by weight puts the largest share count on the
    cheapest rung -- shares are cash over price -- so a shape built to hold
    its centre holds its outer rung instead and the payout curve peaks off
    centre.  ZGSZ 2026-08-17: 4.14 USDC on 30C bought 207 shares while 6.51
    on the centre bought 50.

    The paper ledger has sized shares since 2026-08-21; production split
    cash until 2026-08-26, so the two built different baskets out of the
    same weight vector.
    """

    snapshot = _tilt_snapshot()
    ordered = ("94-95", "96-97", "98-99")
    zero = {bucket: 0.0 for bucket in ordered}
    candidates = delta_v2_runtime._centre_anchored_candidates(
        snapshot=snapshot,
        ordered=ordered,
        centre_bucket="96-97",
        current_shares=dict(zero),
        current_cost=dict(zero),
        pending_shares=dict(zero),
        pending_cost=dict(zero),
        capital_available_usdc=20.0,
        minimum_clip_notional_usdc=0.01,
        probabilities_by_bucket={
            "94-95": 0.30, "96-97": 0.40, "98-99": 0.30,
        },
    )
    assert candidates, "the shape must offer at least one candidate"
    for candidate in candidates:
        shares = candidate["shares_by_bucket"]
        assert shares["96-97"] > shares["98-99"], (
            "the centre must hold more shares than the cheap flank; "
            f"got {shares}"
        )
        assert shares["96-97"] > shares["94-95"], shares
