from parameter_change_governance import (
    build_parameter_change_state,
    build_rolling_calibration_summary,
    parameter_change_governance_contract,
)


def _record(event_date: str, *, hit: bool, brier: float, first_bad_link: str = "none"):
    return {
        "daily_calibration_record_id": f"dcal_{event_date.replace('-', '')}abcdef12",
        "review_id": f"rev_{event_date.replace('-', '')}abcdef12",
        "event_date": event_date,
        "state": "no_apply",
        "calibration_metrics": {
            "target_hit": hit,
            "posterior_brier": brier,
            "posterior_log_loss": brier + 0.5,
            "first_bad_link": first_bad_link,
        },
        "error_attribution": {"primary_link": first_bad_link},
        "strata": {"station_id": "ZUUU", "phase": "D0_midday"},
    }


def test_rolling_calibration_summary_builds_7_30_90_windows() -> None:
    summary = build_rolling_calibration_summary(
        [
            _record("2026-07-04", hit=False, brier=0.70, first_bad_link="execution"),
            _record("2026-06-20", hit=True, brier=0.20),
            _record("2026-03-01", hit=True, brier=0.10),
        ],
        as_of_date="2026-07-04",
    )

    assert summary["read_only"] is True
    assert summary["guards"]["changes_parameters"] is False
    assert summary["windows"]["7d"]["record_count"] == 1
    assert summary["windows"]["30d"]["record_count"] == 2
    assert summary["windows"]["90d"]["record_count"] == 2
    assert summary["windows"]["30d"]["target_hit_rate"] == 0.5
    assert summary["windows"]["7d"]["first_bad_link_counts"] == {"execution": 1}


def test_parameter_change_governance_contract_blocks_runtime_apply() -> None:
    contract = parameter_change_governance_contract()

    assert contract["schema"] == "parameter_change_governance_contract_v1"
    assert contract["read_only"] is True
    assert contract["guards"]["changes_parameters"] is False
    assert contract["state_machine"]["apply_allowed_in_state"] == "approved"
    assert "proposed" in contract["state_machine"]["states"]
    assert contract["impact_layers"]["phase_weight"] == "phase_weight_schedule_review"
    assert "explicit_user_approval" in contract["apply_gates"]
    assert contract["measurement_windows_days"] == [7, 30, 90]
    assert contract["write_boundary"]["runtime_requests_never_change_parameters"] is True


def test_parameter_change_state_blocks_direct_apply_from_candidate() -> None:
    rolling = build_rolling_calibration_summary([_record("2026-07-04", hit=False, brier=0.70)])
    state = build_parameter_change_state(
        calibration_candidates=[
            {
                "calibration_candidate_id": "cal_1234567890abcdef",
                "review_id": "rev_1234567890abcdef",
                "candidate_type": "execution_slo_review",
                "impact_layer": "execution",
                "expected_improvement_metric": "signal_to_order_slo",
                "requires_historical_recompute": False,
                "possible_side_effects": ["requires_review_before_parameter_change"],
                "proposal_json": {"no_auto_apply": True},
                "trigger_sample": {"fact_snapshot_id": "fact_1234567890abcdef"},
            }
        ],
        daily_calibration_record=_record("2026-07-04", hit=False, brier=0.70),
        rolling_calibration_summary=rolling,
    )

    assert state["schema"] == "parameter_change_governance_v1"
    assert state["state"] == "proposed"
    assert state["request_count"] == 1
    request = state["requests"][0]
    assert request["parameter_change_request_id"].startswith("pchg_")
    assert request["state"] == "proposed"
    assert request["change_type"] == "execution_slo_policy_review"
    assert request["governance_state"]["allowed_next_states"] == ["reviewed"]
    assert request["governance_state"]["can_apply"] is False
    assert "state_not_approved" in request["governance_state"]["apply_block_reasons"]
    assert request["guards"]["writes_parameter_change_events"] is False
    assert state["invariants"]["candidate_cannot_apply_without_approved"] is True


def test_parameter_change_state_no_candidates_is_read_only_wait_state() -> None:
    state = build_parameter_change_state(
        calibration_candidates=[],
        daily_calibration_record=_record("2026-07-04", hit=True, brier=0.10),
        rolling_calibration_summary=build_rolling_calibration_summary([]),
    )

    assert state["state"] == "no_candidates"
    assert state["request_count"] == 0
    assert state["guards"]["changes_parameters"] is False
    assert state["next_boundary"] == "wait_for_bias_correction_candidate"
