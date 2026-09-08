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


@pytest.fixture(autouse=True)
def _pre_charter_87_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """These books are cheap on purpose; the lambda gate is tested separately.

    Charter 87 (2026-09-06) has the positive-EV gate read the lifted claim
    times charter 68's lambda(a).  The fixture book here offers a certain
    basket at 0.40 (a = 0.40, lambda 0.14), which that gate refuses on
    sight -- so leaving it on would make every test in this file assert
    "the gate fired" instead of the property it was written for.  The gate's
    own behaviour is pinned in test_clause_87_* and the charter 84 tests.
    """

    monkeypatch.setattr(
        delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off"
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


_NO_EXECUTION_COST = {
    "wait": 0.0,
    "queue": 0.0,
    "adverse_selection": 0.0,
    "reprice": 0.0,
    "capital_lock": 0.0,
}


def _fill_resolver(**kwargs: object) -> dict[str, object]:
    """A fill distribution of the shape the candidate's own mode requires.

    FOK is all-or-nothing by contract, so a partial-fill distribution on one
    is rejected outright; a resting maker must carry both a partial and a
    zero.  Charter 65 made taker the production family, so most callers here
    now get the FOK shape -- which is why this had to stop being one fixed
    distribution handed to every mode.
    """
    if kwargs.get("order_mode") == "FOK":
        return {
            "fill_distribution": [{"fill_fraction": 1.0, "probability": 1.0}],
            "execution_costs": dict(_NO_EXECUTION_COST),
        }
    return {
        "fill_distribution": [
            {"fill_fraction": 0.0, "probability": 0.1},
            {"fill_fraction": 0.5, "probability": 0.2},
            {"fill_fraction": 1.0, "probability": 0.7},
        ],
        "execution_costs": dict(_NO_EXECUTION_COST),
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


def test_min_tick_ask_reports_structural_maker_unavailability(monkeypatch) -> None:
    # Maker is an arm since charter 65 made taker the family
    # production forms; this test is about the arm.
    import bayes_basket_target.delta_v2_runtime as _rt
    monkeypatch.setattr(_rt, "EXECUTION_MODE", "maker")
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

    # Charter 78: an incumbent de-priced to a min-tick leg has nothing to
    # prove positive EV on, so it is not a target any more -- the plan holds
    # on the baseline and the audit names the unpriced refusal.
    assert result["basket_target_activated"] is False
    assert result["state"] == "HOLD"
    assert result["basket_state"] == "HOLD"
    assert result["basket_state_reason"] == "target_inventory_satisfied"
    seated = result["basket_support_selection"]["seated"]
    assert seated["is_incumbent"] is True
    assert seated["admissible"] is False
    assert seated["all_in_price"] is None


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

    # Each bucket is priced at its own posted offer -- 0.10 and 0.25 -- so
    # the same 0.25 clip buys 2.5 shares of one and 1.0 of the other.  That
    # is the standing basis (user ruling, 2026-08-27): the ask is treated as
    # information rather than as a cost the engine never pays.
    #
    # It was ``lane`` for one day in between, and this pair was written the
    # other way round on 2026-08-26 and not turned back on the 27th when the
    # ruling reversed, so this assertion sat red for six days.
    assert both["pricing_basis"] == "official_ws_ask_depth"
    assert both["cost_by_bucket"]["35"] == pytest.approx(0.25)
    assert both["cost_by_bucket"]["36"] == pytest.approx(0.25)
    assert both["shares_by_bucket"]["35"] == pytest.approx(0.25 / 0.10)
    assert both["shares_by_bucket"]["36"] == pytest.approx(0.25 / 0.25)


def test_target_grid_can_still_be_priced_at_the_resting_lane_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lane basis, pinned so the two can be replayed apart.

    Per-bucket depth from the book either way -- that is what this pair of
    tests has always been about -- but the lane arm pays the band its own
    orders would rest in, so the same clip buys more shares.  Still live:
    P0-M keeps lane pricing as the 2026-08-27 ruling's one exemption, and
    an untested arm is one nobody notices going wrong.
    """

    monkeypatch.setattr(
        delta_v2_runtime, "TARGET_PRICING_BASIS", "lane"
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

    # Charter 110 narrowed the anchored ladder to one rung, so the lane's
    # band is that rung alone: bucket 35 (bid 0.04) rests at 0.05, bucket 36
    # (bid 0.05) at 0.06.  Under charter 94's three rungs the lane paid the
    # three-rung mean (0.04 and 0.05), which is the arithmetic this pair of
    # assertions pins -- the lane price IS the ladder, whatever the ladder is.
    assert both["pricing_basis"] == "maker_resting_band"
    assert both["cost_by_bucket"]["35"] == pytest.approx(0.25)
    assert both["cost_by_bucket"]["36"] == pytest.approx(0.25)
    assert both["shares_by_bucket"]["35"] == pytest.approx(0.25 / 0.05)
    assert both["shares_by_bucket"]["36"] == pytest.approx(0.25 / 0.06)

    # The band arm still prices the old band: 35's is 0.05-0.09 (mean
    # 0.07), 36's 0.06-0.24 (mean 0.14625).
    monkeypatch.setattr(delta_v2_runtime, "MAKER_LADDER_SHAPE", "band")
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
    assert both["shares_by_bucket"]["35"] == pytest.approx(0.25 / 0.07)
    assert both["shares_by_bucket"]["36"] == pytest.approx(0.25 / 0.14625)


def test_maker_limit_is_quote_anchored_and_not_the_static_reservation_grid_floor(monkeypatch) -> None:
    # Maker is an arm since charter 65 made taker the family
    # production forms; this test is about the arm.
    import bayes_basket_target.delta_v2_runtime as _rt
    monkeypatch.setattr(_rt, "EXECUTION_MODE", "maker")
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


def test_the_execution_family_follows_charter_65_not_a_cancel_handshake(
    monkeypatch,
) -> None:
    """Which order shape production forms is the mode, and nothing else.

    Charter 65 (user ruling 2026-08-27) made taker the execution layer's law.
    Before it, FOK was a close-out tool: admitted only after a confirmed
    maker cancel and only inside a 120-minute urgency window.  Both were that
    rule's own implementation -- the cancel gate existed so an FOK could not
    double-buy against a resting order, and in taker mode nothing rests --
    and this test used to pin them.

    It pins the replacement instead: the family is chosen by EXECUTION_MODE,
    the two lanes are exclusive, and no handshake stands between the mode and
    the candidates.
    """
    import bayes_basket_target.delta_v2_runtime as runtime_module

    base_kwargs = {
        "snapshot": _snapshot(),
        "basket_policy": _basket_policy(),
        "delta_policy": _delta_policy(),
        "capital_available_usdc": 5.0,
        "minimum_profit_floor": -5.0,
        "resource_probe_ok": True,
    }

    def modes_under(mode: str) -> set[str]:
        seen: set[str] = set()

        def resolver(**kwargs: object) -> dict[str, object]:
            seen.add(str(kwargs.get("order_mode")))
            return _fill_resolver(**kwargs)

        monkeypatch.setattr(runtime_module, "EXECUTION_MODE", mode)
        runtime_module.compile_delta_v2_runtime_plan(
            **base_kwargs, fill_resolver=resolver
        )
        return seen

    assert modes_under("taker") == {"FOK"}
    assert modes_under("maker") == {"MAKER"}


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
    # Charter 84 (2026-09-05): one complete candidate may leave a held leg
    # alone -- the completion, which brings the short leg up to the held
    # level and touches nothing else.  Every other complete candidate still
    # adds to every seated leg.
    completion = [
        row for row in complete if row["candidate_origin"] == "LADDER_BALANCE"
    ]
    assert len(completion) == 1
    assert completion[0]["shares_by_bucket"]["35"] == pytest.approx(14.0)
    assert completion[0]["shares_by_bucket"]["36"] == pytest.approx(14.0)
    for row in complete:
        if row["candidate_origin"] == "LADDER_BALANCE":
            continue
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


def test_consolidation_tilt_scales_capital_floored_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lit light tilts the decision's capital, floored, and says so.

    The fixture book is two buckets, so the sharp light (top-two mass at
    60%) is lit by construction.  The trigger moved to ``stable`` alone on
    2026-09-08, so the signal set is pinned here: this test is about what a
    tilt does to capital, not about which lamp opens it, and it must not
    re-break every time that ruling moves.  The control pins the factor to one and must reproduce
    the untilted capital exactly; the tilted arm must never fall under
    one minimum clip per target leg, and the audit must carry the basis
    either way, because the ledger proves alignment off that field.
    """
    import bayes_basket_target.delta_v2_runtime as runtime

    monkeypatch.setattr(
        runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )

    tilted = compile_delta_v2_runtime_plan(
        snapshot=_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )
    audit = tilted["delta_v2_audit"]["consolidation_tilt"]
    assert audit["factor"] == runtime.CONSOLIDATION_TILT_FACTOR
    assert "sharp" in audit["signals_lit"]
    assert audit["capital_before_usdc"] == 5.0
    # The floor is the venue's minimum order on each of three candidate
    # legs (2026-09-06): both fixture rungs ask 0.20 with no stated lot, so
    # the dollar minimum makes each five shares for 1.00, and the third leg
    # repeats the largest.
    from bayes_basket_target.basket_kernel.delta_v2 import (
        consolidation_tilt_floor_usdc,
        venue_minimum_shares_v1,
    )

    snapshot = _snapshot()
    floor = consolidation_tilt_floor_usdc(
        minimum_clip_usdc=min(_delta_policy().cash_clip_grid),
        legs=runtime.CONSOLIDATION_TILT_FLOOR_LEGS,
        leg_minimum_costs_usdc=[
            venue_minimum_shares_v1(
                min_lot_shares=row.min_order_size, ask=row.ask_price
            ) * row.ask_price
            for row in snapshot.buckets
        ],
    )
    assert floor == 3.0
    assert audit["floor_usdc"] == floor
    assert audit["floor_rule"] == "venue_minimum_per_leg_v1"
    assert audit["capital_after_usdc"] == max(
        5.0 * runtime.CONSOLIDATION_TILT_FACTOR, floor
    )
    assert audit["capital_after_usdc"] < 5.0

    original = runtime.CONSOLIDATION_TILT_FACTOR
    runtime.CONSOLIDATION_TILT_FACTOR = 1.0
    try:
        control = compile_delta_v2_runtime_plan(
            snapshot=_snapshot(),
            basket_policy=_basket_policy(),
            delta_policy=_delta_policy(),
            fill_resolver=_fill_resolver,
            capital_available_usdc=5.0,
            minimum_profit_floor=-5.0,
            resource_probe_ok=True,
        )
    finally:
        runtime.CONSOLIDATION_TILT_FACTOR = original
    control_audit = control["delta_v2_audit"]["consolidation_tilt"]
    assert control_audit["factor"] == 1.0
    assert control_audit["capital_after_usdc"] == 5.0


def test_consolidation_tilt_inherits_the_opener_and_never_relights() -> None:
    """A later stage runs the opener's factor, or untilted -- never its own.

    The fixture's two-bucket book keeps sharp lit at every stage, which is
    exactly the trap: re-reading lights late was priced at -81.8 USDC
    against opener inheritance.  A later-stage compile with no opener record must
    ignore its own lit lights and run untilted; one handed the opener's
    factor must run it.
    """

    raw = _snapshot().to_dict()
    raw["evidence"]["probability_stage"] = "P0-M"
    policy_raw = _delta_policy().to_dict()
    policy_raw["stage"] = "P0-M"
    policy_raw.pop("content_sha256", None)
    policy_raw["content_sha256"] = content_sha256(policy_raw)
    policy = DeltaV2PolicyArtifact.from_mapping(policy_raw)

    unrecorded = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=_basket_policy(),
        delta_policy=policy,
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )
    audit = unrecorded["delta_v2_audit"]["consolidation_tilt"]
    assert audit["basis"] == "no_opener_record"
    assert audit["factor"] == 1.0
    assert audit["capital_after_usdc"] == 5.0

    inherited = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=_basket_policy(),
        delta_policy=policy,
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        chain_consolidation_tilt=0.1,
    )
    audit = inherited["delta_v2_audit"]["consolidation_tilt"]
    assert audit["basis"] == "opener_inherited"
    assert audit["factor"] == 0.1
    assert audit["capital_after_usdc"] < 5.0


def _compile_with_previous(previous: dict) -> dict:
    return compile_delta_v2_runtime_plan(
        snapshot=_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        previous_probabilities=previous,
    )


def test_charter_69_a_falling_centre_keeps_its_gap_under_the_guarded_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production path, not the kernel function: the fixture's ladder is
    35+36 with 36 the centre (0.3 / 0.7).  When the centre's probability has
    fallen since the previous checkpoint, charter 51's clamp holds it at
    zero gap and the basket opens on 35 alone -- a one-rung basket that
    misses its core.  Charter 69 (2026-09-02) narrows the clamp to the ends
    of the window and never the centre, so 36 keeps its gap and the ladder
    stays a ladder.  Both readings are pinned so the difference is visible.
    """

    import bayes_basket_target.delta_v2_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "DECLINE_CLAMP_MODE", "guarded")
    fell_at_centre = {"35": 0.3, "36": 0.75}

    guarded = _compile_with_previous(fell_at_centre)
    assert guarded["probability_clamp_mode"] == "guarded"
    assert guarded["probability_declined_buckets"] == ("36",)
    assert guarded["probability_clamped_buckets"] == ()
    assert guarded["execution_gap_support"] == ["35", "36"]

    monkeypatch.setattr(runtime_module, "DECLINE_CLAMP_MODE", "leg")
    leg = _compile_with_previous(fell_at_centre)
    assert leg["probability_clamp_mode"] == "leg"
    assert leg["probability_declined_buckets"] == ("36",)
    assert leg["probability_clamped_buckets"] == ("36",)
    assert leg["execution_gap_support"] == ["35"]


def test_charter_69_a_falling_end_rung_is_still_held_under_the_guarded_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guarded is not off: a falling rung at the end of the window is held
    exactly as charter 51 holds it.  Both rungs fell here; 35 is an end and
    goes, 36 is the centre and stays."""

    import bayes_basket_target.delta_v2_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "DECLINE_CLAMP_MODE", "guarded")
    both_fell = {"35": 0.35, "36": 0.75}
    result = _compile_with_previous(both_fell)
    assert result["probability_declined_buckets"] == ("35", "36")
    assert result["probability_clamped_buckets"] == ("35",)
    assert result["execution_gap_support"] == ["36"]


def test_held_only_never_clamps_an_opening_because_nothing_is_held_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User semantics (2026-09-02): "首次建仓如何评判概率下降？没有对比啊".

    The clamp holds a rung at what the book has.  At an opening the book
    has nothing, so there is nothing to hold and no rung is clamped -- even
    though the previous checkpoint exists and says 36 fell.  The decline is
    still recorded (it is a fact about the probability), only the clamp is
    withheld.
    """

    import bayes_basket_target.delta_v2_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "DECLINE_CLAMP_MODE", "held_only")
    both_fell = {"35": 0.35, "36": 0.75}
    result = _compile_with_previous(both_fell)
    assert result["probability_clamp_mode"] == "held_only"
    assert result["probability_declined_buckets"] == ("35", "36")
    assert result["probability_clamped_buckets"] == ()
    assert result["execution_gap_support"] == ["35", "36"]


def test_held_only_clamps_every_falling_rung_the_book_has_committed_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the book has bid for a rung (resting or filled) the clamp has
    its subject and holds it -- centre included, which is charter 51 as
    written.  The fixture rests an order on 36 only, so 36 (the centre,
    fallen) is held at what it has while 35 (fallen too, but nothing
    committed) is still bought: the clamp follows the commitment, not the
    probability alone."""

    import bayes_basket_target.delta_v2_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "DECLINE_CLAMP_MODE", "held_only")
    both_fell = {"35": 0.35, "36": 0.75}
    result = compile_delta_v2_runtime_plan(
        snapshot=_snapshot(pending_shares=0.5),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        previous_probabilities=both_fell,
    )
    assert result["probability_clamp_mode"] == "held_only"
    assert result["probability_declined_buckets"] == ("35", "36")
    assert result["probability_clamped_buckets"] == ("36",)
    assert result["execution_gap_support"] == ["35"]


def test_after_open_is_the_default_and_buys_the_opening_ladder_whole() -> None:
    """The clamp is retired (2026-09-05); an opening was never clamped anyway.

    Charter 69 was ruled on 2026-09-02 against a paper book that priced
    every checkpoint on the book *before* it -- an information-timing
    advantage worth about sixteen points, removed on 2026-09-04.  Re-priced
    on the honest pairing the clamp costs 3.4 points alone and 7.3 inside
    the best combination, and the user retired it.  ``off`` is the shipped
    default; the arms stay switchable so the old book re-derives.

    Under both the retired default and the ``after_open`` arm an opening
    that holds nothing buys its whole ladder -- 35 and 36 -- so this
    fixture reads the same either way, which is what it asserts.
    """

    import bayes_basket_target.delta_v2_runtime as runtime_module

    assert runtime_module.DECLINE_CLAMP_MODE == "off"
    both_fell = {"35": 0.35, "36": 0.75}
    result = _compile_with_previous(both_fell)
    assert result["probability_clamp_mode"] == "off"
    # Nothing is measured as falling, because nothing is compared.
    assert result["probability_declined_buckets"] == ()
    assert result["probability_clamped_buckets"] == ()
    assert result["execution_gap_support"] == ["35", "36"]


def test_after_open_holds_every_falling_rung_once_the_book_is_open() -> None:
    """"开仓之后，任何一格概率比上一个模型 run 低了，不管手里有没有它，
    这一轮都不买".  The fixture has an order resting on 36 only, so the
    book is open; 35 (nothing held) and 36 (the centre, held) both fell and
    both are held at what they have -- held_only would have bought 35."""

    import bayes_basket_target.delta_v2_runtime as runtime_module

    # The arm, not the default: the clamp was retired on 2026-09-05 and
    # this pins what ``after_open`` does when somebody runs it.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(runtime_module, "DECLINE_CLAMP_MODE", "after_open")
    both_fell = {"35": 0.35, "36": 0.75}
    result = compile_delta_v2_runtime_plan(
        snapshot=_snapshot(pending_shares=0.01),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        previous_probabilities=both_fell,
    )
    assert result["probability_clamp_mode"] == "after_open"
    assert result["probability_declined_buckets"] == ("35", "36")
    assert result["probability_clamped_buckets"] == ("35", "36")
    assert result["execution_gap_support"] == []
    monkey.undo()


@pytest.fixture(autouse=True)
def _symmetric_seat_rule_off(monkeypatch):
    """Charter 79 seats symmetric-day P0/P0-M windows on the mean bucket +-1.

    The fixtures in this module exercise other mechanisms on symmetric
    shapes; the rule is switched off here and tested on its own in the
    doctrine suite (clause 79).
    """

    monkeypatch.setattr(delta_v2_runtime, "SYMMETRIC_SEAT_MODE", "off")


def test_the_tilt_floor_is_three_venue_minimums_not_three_clips() -> None:
    """Operator ruling 2026-09-06: the floor keeps one exchange-minimum order
    per leg affordable, so a tilted chain can still add at P0-M and P1.

    ZGSZ 2026-09-06 reached its P1 window tilted to 0.75 USDC (three 0.25
    clips) against legs that cost 1.05 each -- a cut that had become a
    refusal.  The floor sums the largest ``legs`` per-leg venue minimums,
    each at least a clip, repeats the largest when fewer legs are priced,
    and falls back to ``legs`` clips when nothing is priced.
    """

    from bayes_basket_target.basket_kernel.delta_v2 import (
        consolidation_tilt_floor_usdc,
    )

    # Three legs at 1.05 / 2.15 / 1.00: the sum of the three.
    assert consolidation_tilt_floor_usdc(
        minimum_clip_usdc=0.25, legs=3, leg_minimum_costs_usdc=[1.05, 2.15, 1.0]
    ) == pytest.approx(4.20)
    # Four candidate legs: the three dearest.
    assert consolidation_tilt_floor_usdc(
        minimum_clip_usdc=0.25, legs=3,
        leg_minimum_costs_usdc=[1.05, 2.15, 1.0, 0.5],
    ) == pytest.approx(4.20)
    # A leg under a clip is raised to the clip.
    assert consolidation_tilt_floor_usdc(
        minimum_clip_usdc=0.25, legs=3, leg_minimum_costs_usdc=[0.1, 0.1, 0.1]
    ) == pytest.approx(0.75)
    # Fewer priced legs than the floor spans: the largest repeats
    # (2.0 + 1.0, then 2.0 again for the third leg).
    assert consolidation_tilt_floor_usdc(
        minimum_clip_usdc=0.25, legs=3, leg_minimum_costs_usdc=[1.0, 2.0]
    ) == pytest.approx(5.0)
    # Nothing priced: the floor it replaced.
    assert consolidation_tilt_floor_usdc(
        minimum_clip_usdc=0.25, legs=3, leg_minimum_costs_usdc=[]
    ) == pytest.approx(0.75)
    assert consolidation_tilt_floor_usdc(
        minimum_clip_usdc=0.25, legs=0, leg_minimum_costs_usdc=[1.0]
    ) == 0.0

