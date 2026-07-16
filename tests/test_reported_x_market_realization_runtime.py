from __future__ import annotations

from datetime import date, timedelta

from reported_x_market_realization_runtime import compile_reported_x_market_realization
from resolution_bridge_artifact_runtime import artifact_sha256
from resolution_bridge_calibration_runtime import fit_resolution_bridge_candidate
from strong_observation_lr_artifact_contract import REQUIRED_FEATURE_IDS


def _artifact() -> dict:
    start = date(2025, 1, 1)
    pairs = []
    for index in range(120):
        x = 20 + index % 15
        pairs.append({
            "state": "available_for_bridge_calibration_only",
            "event_date": (start + timedelta(days=index)).isoformat(),
            "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
            "final_physical_high_c": x,
            "settlement_scalar_c": x + (-1, 0, 0, 0, 1)[index % 5],
            "observed_x_s_pair_sha256": f"{index:064x}",
            "physical_source": {"source_id": "amsc_zuuu_scheduled_metar_daily_max_v2"},
            "settlement_source": {"source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3"},
        })
    candidate = fit_resolution_bridge_candidate(pairs, authority_contract_sha256="a" * 64)["candidate_artifact"]
    core = {
        **candidate,
        "state": "production_approved",
        "bridge_closed": True,
        "validated": True,
        "production_runtime_eligible": True,
        "shadow_only": False,
        "approval_id": "ADR-bridge-test",
        "approval_receipt_sha256": "c" * 64,
    }
    core.pop("artifact_sha256")
    return {**core, "artifact_sha256": artifact_sha256(core)}


def _target() -> dict:
    return {
        "schema": "market_resolution_target_contract_v1",
        "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
        "authority_contract_sha256": "a" * 64,
        "resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
        "resolution_local_date": "2025-04-30",
        "target_contract_sha256": "b" * 64,
        "rounding_rule": "whole_degree_half_step_v1",
        "bucket_contract": {
            "29C or below": {"lower_inclusive": None, "upper_exclusive": 29.5},
            "30C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5},
            "31C or higher": {"lower_inclusive": 30.5, "upper_exclusive": None},
        },
        "bridge_closed": False,
        "validated": False,
    }


def _checkpoint(*, previous: bool) -> dict:
    return {
        "posterior_checkpoint_id": "pchk-prev" if previous else "pchk-current",
        "posterior_run_id": "post-prev" if previous else "post-current",
        "previous_checkpoint_id": "" if previous else "pchk-prev",
        "resolution_contract_id": "physical-resolution",
        "last_canonical_event_id": "canobs-1" if not previous else "canobs-0",
        "last_strong_update_id": "stupd-1" if not previous else "stupd-0",
        "reported_physical_x_distribution": (
            {"30": 0.5, "31": 0.5} if previous else {"30": 0.2, "31": 0.8}
        ),
        "reported_x_lower_bound_c": 30,
    }


def _update() -> dict:
    return {
        "ok": True,
        "state": "applied",
        "canonical_observation_event_id": "canobs-1",
        "strong_observation_update_id": "stupd-1",
        "model_artifact_sha256": "strong-artifact",
        "strong_feature_trace_by_cell": [{
            "feature_values": {key: 1.0 for key in REQUIRED_FEATURE_IDS},
            "feature_categories": {key: "handled" for key in REQUIRED_FEATURE_IDS},
        }],
    }


def _policy() -> dict:
    return {
        "schema": "realization_state_policy_v2",
        "artifact_sha256": "policy-artifact",
        "approval_state": "approved",
        "credible_probability_floor": 0.05,
        "strong_support_log_probability_ratio_floor": 0.0,
        "strong_exclusion_probability_ceiling": 0.01,
        "strong_exclusion_log_probability_ratio_ceiling": -1.0,
    }


def test_successor_market_realization_keeps_x_immutable_and_never_derives_report_bucket_b() -> None:
    result = compile_reported_x_market_realization(
        previous_checkpoint=_checkpoint(previous=True),
        checkpoint=_checkpoint(previous=False),
        physical_resolution_contract={
            "resolution_contract_id": "physical-resolution",
            "resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
            "resolution_local_date": "2025-04-30",
            "settlement_unit": "C",
            "bridge_closed": False,
            "validated": False,
        },
        market_target_contract=_target(),
        bridge_artifact=_artifact(),
        canonical_observation_event={
            "canonical_observation_event_id": "canobs-1",
            "state": "accepted_primary",
            "selected_source_id": "zuuu_metar_raw_amsc",
            "parsed_observation": {"temperature_c": 30.0},
        },
        strong_observation_update=_update(),
        peak_timing_state={
            "schema": "peak_timing_state_l3_v1",
            "ok": True,
            "state": "na",
            "na_reason": "test",
            "canonical_observation_event_id": "canobs-1",
            "strong_observation_update_id": "stupd-1",
            "entry_peak_at": "",
            "does_not_update_physical_x": True,
        },
        realization_policy=_policy(),
        fact_snapshot_id="factsnap-1",
        created_at="2025-04-30T08:00:00Z",
        committed_at="2025-04-30T08:00:00Z",
    )
    assert result["ok"] is True
    assert result["reported_x_unchanged"] is True
    assert result["realization_projection"]["current_report_bucket"] == ""
    assert result["realization_projection"]["target_bucket"]
    assert result["boundary"]["direct_physical_report_to_market_bucket"] is False
