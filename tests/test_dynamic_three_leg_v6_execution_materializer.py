from dynamic_three_leg_v6_execution_materializer import materialize_target_realization_v6_execution
from target_realization_v6_runtime import build_target_realization_v6_plan


def _plan():
    return build_target_realization_v6_plan(
        posterior_run_id="p1", realization_state_id="r1", canonical_observation_event_id="e1",
        posterior_buckets=[
            {"bucket": "35C", "ordinal": 35, "probability": 0.55},
            {"bucket": "36C", "ordinal": 36, "probability": 0.30},
            {"bucket": "34C", "ordinal": 34, "probability": 0.15},
        ],
        peak_timing_state={"entry_peak_at": "2026-07-16T09:00:00Z"},
        future_warming_or_rebound_risk=True, second_yes_entry_allowed=True,
        position_exposure_snapshot={"complete": True, "exposures": []},
        coverage_snapshot={"complete": True, "all_conflicting_sell_and_cancel_reconciled": True},
        prior_main_yes_cycle=None, available_capital_usdc=80.0,
        capital_snapshot_id="cap1", risk_snapshot_id="risk1", capital_epoch_id="epoch1",
        strategy_artifact_sha256="a" * 64, decision_as_of="2026-07-16T08:30:00Z",
    )


def _market(tick="0.001"):
    return {
        "market_snapshot_id": "m1", "expires_at": "2026-07-16T08:31:00Z",
        "buckets": {
            "35C": {"yes_ask": 0.4, "yes_token_id": "y35", "yes_tick_size": tick},
            "36C": {"yes_ask": 0.3, "yes_token_id": "y36", "yes_tick_size": tick},
            "34C": {"no_ask": 0.2, "no_token_id": "n34", "no_tick_size": tick},
        },
    }


def _materialize(market):
    return materialize_target_realization_v6_execution(
        plan=_plan(), market_snapshot=market, position_snapshot_id="pos1", fact_snapshot_id="f1",
        slug="zuuu", event_date="2026-07-16", correlation_id="c1",
        observation_to_order_hard_deadline_at="2026-07-16T08:31:00Z",
        decision_as_of="2026-07-16T08:30:00Z",
    )


def test_materializer_preserves_uniform_scaled_notional_and_prevalidates_0999_tick():
    result = _materialize(_market())
    assert result["ok"] is True
    assert len(result["intents"]) == 3
    by_candidate = {row["candidate_id"]: row for row in _plan()["candidates"]}
    for intent in result["intents"]:
        assert intent["notional"] == by_candidate[intent["candidate_id"]]["final_incremental_target_notional_usdc"]
        assert '"token_tick_size":"0.001"' in intent["intent_json"]


def test_unsupported_tick_is_a_committed_no_order_decision_for_only_that_leg():
    market = _market()
    market["buckets"]["35C"]["yes_tick_size"] = "0.01"
    result = _materialize(market)
    assert result["ok"] is True
    main = next(row for row in result["decisions"] if row["outcome"] == "35C")
    assert main["will_order"] is False
    assert "v6_execution_future_standing_999_tick_unsupported" in main["reason_codes"]
    assert all(row["bucket"] != "35C" for row in result["intents"])


def test_target_stage_prepares_coverage_cancel_before_any_target_sell():
    plan = _plan()
    result = materialize_target_realization_v6_execution(
        plan=plan, market_snapshot=_market(), position_snapshot_id="pos1", fact_snapshot_id="f1",
        slug="zuuu", event_date="2026-07-16", correlation_id="c1",
        observation_to_order_hard_deadline_at="2026-07-16T08:31:00Z",
        decision_as_of="2026-07-16T08:30:00Z",
        position_exposures=[{
            "position_lot_id": "lot-main", "position_lot_version": "2",
            "strategy_leg": "main_yes", "bucket": "35C", "side": "YES",
            "token_id": "y35", "acquired_size": 40.0, "position_size": 40.0,
            "main_yes_position_cycle_id": plan["main_yes_position_cycle_id"],
        }],
        coverage_snapshot={"rows": [{
            "position_lot_id": "lot-main", "position_lot_version": "2",
            "standing_exit_plan_id": "stand1", "standing_sell_order_id": "order999",
            "state": "covered", "covered_size": 40.0, "stage_released_size": 0.0,
        }]},
    )
    assert len(result["coverage_release_requests"]) == 1
    request = result["coverage_release_requests"][0]
    assert request["requested_release_size"] == 20.0
    assert request["target_realization_reason_code"] == "target_realization_main_yes_upside_risk_stage_2"
    assert not any(row["order_action"] == "SELL" and row["reason_family"] == "target_realization" for row in result["intents"])


def test_off_role_old_main_prepares_full_invalidated_coverage_release():
    plan = _plan()
    plan["off_role_main_yes_exits"] = [{
        "position_lot_id": "old-main", "bucket": "34C",
        "main_yes_position_cycle_id": "old-cycle",
        "exit_reason_family": "target_realization",
        "exit_reason_code": "target_realization_main_yes_invalidated",
        "stage_target_ratio": 1.0, "release_standing_coverage_first": True,
    }]
    result = materialize_target_realization_v6_execution(
        plan=plan, market_snapshot=_market(), position_snapshot_id="pos1", fact_snapshot_id="f1",
        slug="zuuu", event_date="2026-07-16", correlation_id="c1",
        observation_to_order_hard_deadline_at="2026-07-16T08:31:00Z",
        decision_as_of="2026-07-16T08:30:00Z",
        position_exposures=[{
            "position_lot_id": "old-main", "position_lot_version": "2",
            "strategy_leg": "main_yes", "bucket": "34C", "side": "YES",
            "token_id": "y34", "acquired_size": 12.0, "position_size": 12.0,
            "main_yes_position_cycle_id": "old-cycle",
        }],
        coverage_snapshot={"rows": [{
            "position_lot_id": "old-main", "position_lot_version": "2",
            "standing_exit_plan_id": "stand-old", "standing_sell_order_id": "order-old",
            "state": "covered", "covered_size": 12.0, "stage_released_size": 0.0,
        }]},
    )
    requests = [row for row in result["coverage_release_requests"] if row["standing_exit_plan_id"] == "stand-old"]
    assert len(requests) == 1
    assert requests[0]["requested_release_size"] == 12.0
    assert requests[0]["target_realization_reason_code"] == "target_realization_main_yes_invalidated"
