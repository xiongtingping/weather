from dynamic_three_leg_v6_capital_allocator import allocate_v6_incremental_capital
from dynamic_three_leg_v6_position_cycle_runtime import advance_main_yes_position_cycle
from standing_exit_999_runtime import build_standing_exit_plan, release_standing_coverage
from target_realization_v6_runtime import build_target_realization_v6_plan


def _hard(bucket: str, posterior: str = "p1", realization: str = "r1") -> dict:
    return {
        "state": "valid",
        "bucket": bucket,
        "posterior_run_id": posterior,
        "realization_state_id": realization,
        "canonical_observation_event_id": "evt1",
        "bridge_artifact_sha256": "a" * 64,
        "target_contract_sha256": "b" * 64,
        "authority_sha256": "c" * 64,
        "certificate_sha256": "d" * 64,
        "full_support_exclusion_verified": True,
    }


def _base(**overrides):
    value = {
        "posterior_run_id": "p1",
        "realization_state_id": "r1",
        "canonical_observation_event_id": "evt1",
        "posterior_buckets": [
            {"bucket": "34C", "ordinal": 34, "probability": 0.10, "soft_unreachable": True},
            {"bucket": "35C", "ordinal": 35, "probability": 0.55},
            {"bucket": "36C", "ordinal": 36, "probability": 0.25},
            {"bucket": "37C", "ordinal": 37, "probability": 0.10},
        ],
        "peak_timing_state": {"entry_peak_at": "2026-07-16T08:00:00Z"},
        "future_warming_or_rebound_risk": True,
        "second_yes_entry_allowed": True,
        "position_exposure_snapshot": {"complete": True, "exposures": []},
        "coverage_snapshot": {"complete": True, "all_conflicting_sell_and_cancel_reconciled": True},
        "prior_main_yes_cycle": None,
        "available_capital_usdc": 80.0,
        "capital_snapshot_id": "capital1",
        "risk_snapshot_id": "risk1",
        "capital_epoch_id": "epoch1",
        "strategy_artifact_sha256": "f" * 64,
        "decision_as_of": "2026-07-16T07:00:00Z",
    }
    value.update(overrides)
    return value


def test_capital_allocator_uses_one_ratio_after_all_exposure_is_deducted():
    result = allocate_v6_incremental_capital(
        candidates=[
            {"candidate_id": "main", "raw_target_notional_usdc": 20, "existing_exposure_notional_usdc": 5},
            {"candidate_id": "second", "raw_target_notional_usdc": 4.99, "existing_exposure_notional_usdc": 0},
            {"candidate_id": "hard", "raw_target_notional_usdc": 200, "existing_exposure_notional_usdc": 50},
        ],
        available_capital_usdc=82.495,
        capital_snapshot_id="c",
        risk_snapshot_id="r",
        capital_epoch_id="e",
    )
    assert result["ok"] is True
    assert abs(result["capital_scale_factor"] - (82.495 / 169.99)) < 1e-12
    ratios = {
        round(row["final_incremental_target_notional_usdc"] / row["raw_incremental_target_notional_usdc"], 12)
        for row in result["allocations"]
    }
    assert len(ratios) == 1
    assert abs(result["allocated_incremental_notional_usdc"] - 82.495) < 1e-9


def test_position_cycle_watermark_and_reentry_require_distinct_events_and_reconcile():
    first = advance_main_yes_position_cycle(
        posterior_run_id="p1", bucket="35C", mode="upside_risk", stage_target_ratio=0.50,
        canonical_observation_event_id="evt1", prior_cycle=None, coverage_reconciled=True,
    )
    assert first["new_cycle_created"] is True
    assert first["sell_stage_high_watermark"] == 0
    assert first["requested_sell_stage"] == 2
    first = {**first, "sell_stage_high_watermark": 2}
    repeated = advance_main_yes_position_cycle(
        posterior_run_id="p2", bucket="35C", mode="normal", stage_target_ratio=0.0,
        canonical_observation_event_id="evt2", prior_cycle=first, coverage_reconciled=True,
    )
    repeated_again = advance_main_yes_position_cycle(
        posterior_run_id="p3", bucket="35C", mode="normal", stage_target_ratio=0.0,
        canonical_observation_event_id="evt2", prior_cycle=repeated, coverage_reconciled=True,
    )
    assert repeated_again["reentry_confirmation_count"] == 1
    second_distinct_unreconciled = advance_main_yes_position_cycle(
        posterior_run_id="p4", bucket="35C", mode="normal", stage_target_ratio=0.0,
        canonical_observation_event_id="evt3", prior_cycle=repeated_again, coverage_reconciled=False,
    )
    assert second_distinct_unreconciled["reentry_ready"] is True
    assert second_distinct_unreconciled["new_cycle_created"] is False
    reopened = advance_main_yes_position_cycle(
        posterior_run_id="p5", bucket="35C", mode="normal", stage_target_ratio=0.0,
        canonical_observation_event_id="evt3", prior_cycle=second_distinct_unreconciled, coverage_reconciled=True,
    )
    assert reopened["new_cycle_created"] is True
    assert reopened["main_yes_position_cycle_id"] != first["main_yes_position_cycle_id"]
    assert reopened["sell_stage_high_watermark"] == 0


def test_standing_exit_requires_exact_tick_and_reconciled_release():
    fill = {
        "strategy_leg": "main_yes", "filled_size": 12.5, "posterior_run_id": "p1",
        "realization_state_id": "r1", "position_lot_id": "lot1", "position_lot_version": "1",
        "token_id": "token1", "bucket": "35C", "side": "YES", "buy_intent_id": "buy1",
        "fill_identity": "fill1",
    }
    unsupported = build_standing_exit_plan(fill=fill, strategy_artifact_sha256="a" * 64, tick_size="0.01")
    assert unsupported["ok"] is False
    assert "standing_exit_999_tick_unsupported" in unsupported["block_reasons"]
    plan = build_standing_exit_plan(fill=fill, strategy_artifact_sha256="a" * 64, tick_size="0.001")
    assert plan["limit_price"] == 0.999
    plan["covered_size"] = 12.5
    plan["uncovered_size"] = 0
    blocked = release_standing_coverage(
        plan=plan, release_size=6.25,
        target_realization_reason_code="target_realization_main_yes_upside_risk_stage_2",
        coverage_snapshot={"complete": True, "cancel_or_remaining_reconciled": False, "ack_unknown": True},
    )
    assert blocked["ok"] is False
    released = release_standing_coverage(
        plan=plan, release_size=6.25,
        target_realization_reason_code="target_realization_main_yes_upside_risk_stage_2",
        coverage_snapshot={"complete": True, "cancel_or_remaining_reconciled": True, "ack_unknown": False},
    )
    assert released["covered_size_after"] == 6.25
    assert released["stage_released_size_after"] == 6.25


def test_v6_composer_builds_four_state_main_trajectory_no_and_uniform_scaling():
    result = build_target_realization_v6_plan(**_base())
    assert result["ok"] is True
    assert result["main_yes_bucket"] == "35C"
    assert result["main_yes_mode"] == "upside_risk"
    assert result["p_above_top"] == 0.35
    assert result["risk_target_ratio"] == 0.75
    assert result["time_cap_ratio"] == 0.50
    assert result["main_yes_stage_target_ratio"] == 0.50
    assert result["target_realization_exit"]["exit_reason_code"] == "target_realization_main_yes_upside_risk_stage_2"
    assert result["no_mode"] == "trajectory_no"
    no = next(row for row in result["candidates"] if row.get("no_mode") == "trajectory_no")
    assert no["bucket"] == "34C"
    assert no["raw_target_notional_usdc"] == 50.0
    factors = {row["capital_scale_factor"] for row in result["candidates"]}
    assert len(factors) == 1


def test_v6_composer_prefers_hard_no_and_allows_proportional_below_200():
    rows = _base()["posterior_buckets"]
    rows[0] = {**rows[0], "hard_exclusion_certificate": _hard("34C")}
    result = build_target_realization_v6_plan(**_base(posterior_buckets=rows, available_capital_usdc=50.0))
    assert result["ok"] is True
    assert result["no_mode"] == "hard_exclusion_no"
    hard = next(row for row in result["candidates"] if row.get("no_mode") == "hard_exclusion_no")
    assert hard["raw_target_notional_usdc"] == 200.0
    assert 0 < hard["final_incremental_target_notional_usdc"] < 200.0


def test_v6_composer_hard_locks_when_all_hotter_buckets_have_certificates():
    rows = _base()["posterior_buckets"]
    rows[2] = {**rows[2], "hard_exclusion_certificate": _hard("36C")}
    rows[3] = {**rows[3], "hard_exclusion_certificate": _hard("37C")}
    result = build_target_realization_v6_plan(**_base(posterior_buckets=rows))
    assert result["ok"] is True
    assert result["main_yes_mode"] == "hard_locked"
    assert result["main_yes_stage_target_ratio"] == 0.0
    assert result["target_realization_exit"] is None


def test_trajectory_no_one_percentage_point_downshift_hysteresis():
    rows = [
        {"bucket": "36C", "ordinal": 36, "probability": 0.360},
        {"bucket": "37C", "ordinal": 37, "probability": 0.335},
        {"bucket": "34C", "ordinal": 34, "probability": 0.305},
    ]
    result = build_target_realization_v6_plan(
        **_base(
            posterior_buckets=rows,
            prior_no_state={
                "no_mode": "trajectory_no",
                "bucket": "34C",
                "raw_target_notional_usdc": 10.0,
            },
        )
    )
    assert result["ok"] is True
    no = next(row for row in result["candidates"] if row["strategy_leg"] == "trajectory_no")
    assert no["bucket"] == "34C"
    assert no["raw_target_notional_usdc"] == 10.0
    assert no["amount_band"] == "q_no_70_hysteresis_hold"


def test_missing_entry_peak_closes_only_second_yes_and_does_not_block_main_or_no():
    result = build_target_realization_v6_plan(
        **_base(peak_timing_state={"entry_peak_at": ""}, second_yes_entry_allowed=True)
    )
    assert result["ok"] is True
    assert result["peak_phase"] == "entry_peak_at_unavailable"
    assert result["time_cap_ratio"] == 0.0
    assert result["main_yes_bucket"] == "35C"
    second = next(row for row in result["candidates"] if row["strategy_leg"] == "second_yes")
    assert second["entry_allowed"] is False
    assert result["no_mode"] == "trajectory_no"


def test_dynamic_role_change_counts_cross_role_exposure_and_invalidates_old_main_lot():
    rows = [
        {"bucket": "35C", "ordinal": 35, "probability": 0.30},
        {"bucket": "36C", "ordinal": 36, "probability": 0.55},
        {"bucket": "34C", "ordinal": 34, "probability": 0.15},
    ]
    exposure = {
        "position_lot_id": "old-main", "main_yes_position_cycle_id": "old-cycle",
        "strategy_leg": "main_yes", "bucket": "35C", "side": "YES",
        "position_size": 10.0, "notional_usdc": 4.99,
    }
    result = build_target_realization_v6_plan(**_base(
        posterior_buckets=rows,
        position_exposure_snapshot={"complete": True, "exposures": [exposure]},
    ))
    assert result["main_yes_bucket"] == "36C"
    main = next(row for row in result["candidates"] if row["strategy_leg"] == "main_yes")
    assert main["existing_exposure_notional_usdc"] == 4.99
    second = next(row for row in result["candidates"] if row["strategy_leg"] == "second_yes")
    assert second["bucket"] == "35C"
    assert second["entry_allowed"] is False
    assert second["entry_block_reason"] == "v6_old_main_reclassification_exit_pending"
    assert second["final_incremental_target_notional_usdc"] == 0.0
    assert result["off_role_main_yes_exits"] == [{
        "position_lot_id": "old-main", "bucket": "35C",
        "main_yes_position_cycle_id": "old-cycle",
        "exit_reason_family": "target_realization",
        "exit_reason_code": "target_realization_main_yes_invalidated",
        "stage_target_ratio": 1.0, "release_standing_coverage_first": True,
    }]


def test_portfolio_conflicts_block_opposite_bucket_and_second_no_bucket_buys():
    exposures = [
        {"position_lot_id": "no-main", "strategy_leg": "trajectory_no", "bucket": "35C", "side": "NO", "position_size": 5.0, "notional_usdc": 2.0},
        {"position_lot_id": "other-no", "strategy_leg": "trajectory_no", "bucket": "37C", "side": "NO", "position_size": 5.0, "notional_usdc": 2.0},
    ]
    result = build_target_realization_v6_plan(**_base(
        position_exposure_snapshot={"complete": True, "exposures": exposures},
    ))
    main = next(row for row in result["candidates"] if row["strategy_leg"] == "main_yes")
    no = next(row for row in result["candidates"] if row["strategy_leg"] == "trajectory_no")
    assert main["entry_allowed"] is False
    assert main["entry_block_reason"] == "v6_same_bucket_opposite_no_exposure"
    assert main["final_incremental_target_notional_usdc"] == 0.0
    assert no["entry_allowed"] is False
    assert no["entry_block_reason"] == "v6_existing_different_no_bucket_must_close_first"
    assert no["final_incremental_target_notional_usdc"] == 0.0
