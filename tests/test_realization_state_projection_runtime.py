from __future__ import annotations

import json

import pytest

from realization_state_projection_runtime import (
    POLICY_SCHEMA_VERSION,
    build_realization_state_projection,
    materialize_projected_realization_state_row,
)


def _contract() -> dict:
    return {
        "bucket_contract": {
            "29C": {"lower_inclusive": 28.5, "upper_exclusive": 29.5},
            "30C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5},
            "31C": {"lower_inclusive": 30.5, "upper_exclusive": 31.5},
            "32C": {"lower_inclusive": 31.5, "upper_exclusive": 32.5},
        }
    }


def _checkpoint() -> dict:
    return {
        "posterior_run_id": "post_1234567890abcdef",
        "posterior_checkpoint_id": "pchk_1234567890abcdef",
        "resolution_contract_id": "resolution-v1",
        "last_canonical_event_id": "canobs_1234567890abcdef",
        "last_strong_update_id": "stupd_1234567890abcdef",
    }


def _bridge() -> dict:
    return {
        "ok": True,
        "state": "available",
        "resolution_contract_id": "resolution-v1",
        "market_bucket_probabilities": {"29C": 0.0, "30C": 0.1, "31C": 0.2, "32C": 0.7},
        "settlement_exclusion_certificates": {"29C": {"state": "valid"}},
    }


def _market_contract() -> dict:
    return {**_contract(), "resolution_contract_id": "resolution-v1"}


def _event() -> dict:
    return {
        "canonical_observation_event_id": "canobs_1234567890abcdef",
        "state": "accepted_primary",
        "selected_source_id": "zuuu_metar_raw_amsc",
        "parsed_observation": {"temperature_c": 31.0},
    }


def _update(state: str = "applied") -> dict:
    return {
        "strong_observation_update_id": "stupd_1234567890abcdef",
        "model_artifact_sha256": "strong-artifact-1",
        "state": state,
    }


def _policy() -> dict:
    return {
        "schema": POLICY_SCHEMA_VERSION,
        "artifact_sha256": "realization-policy-immutable-1",
        "approval_state": "approved",
        "credible_probability_floor": 0.15,
        "strong_support_log_probability_ratio_floor": 0.0,
        "strong_exclusion_probability_ceiling": 0.01,
        "strong_exclusion_log_probability_ratio_ceiling": -1.0,
    }


def _strong_evidence() -> dict:
    return {
        "schema": "strong_physical_evidence_projection_v1",
        "ok": True,
        "state": "available",
        "canonical_observation_event_id": "canobs_1234567890abcdef",
        "strong_observation_update_id": "stupd_1234567890abcdef",
        "model_artifact_sha256": "strong-artifact-1",
        "realization_policy_artifact_sha256": "realization-policy-immutable-1",
        "top_bucket": "32C",
        "top_bucket_joint_strong_support": True,
        "bucket_evidence": {},
        "bucket_exclusions": [{"bucket": "29C", "excluded": True, "kind": "observed_daily_high_exceeded"}],
        "thresholds": {
            "strong_support_log_probability_ratio_floor": 0.0,
            "strong_exclusion_probability_ceiling": 0.01,
            "strong_exclusion_log_probability_ratio_ceiling": -1.0,
        },
    }


def _build(**overrides) -> dict:
    values = {
        "checkpoint": _checkpoint(),
        "resolution_bridge": _bridge(),
        "resolution_contract": _market_contract(),
        "latest_canonical_observation_event": _event(),
        "strong_observation_update": _update(),
        "strong_physical_evidence": _strong_evidence(),
        "realization_policy": _policy(),
        "temperature_quantization_c": 1.0,
    }
    values.update(overrides)
    return build_realization_state_projection(**values)


def test_l3_projection_embeds_composer_owned_strong_evidence_without_retired_no_fields() -> None:
    state = _build()

    assert state["ok"] is True
    assert state["target_bucket"] == "32C"
    assert state["second_target_bucket"] == "31C"
    assert state["current_report_bucket"] == ""
    assert state["probability_above_current_report_bucket"] is None
    assert state["current_report_bucket_na_reason"] == "physical_x_must_not_be_directly_mapped_to_market_bucket_b"
    assert state["strong_physical_evidence"]["top_bucket_joint_strong_support"] is True
    assert "trajectory_no_weather_eligible" not in state
    assert "safe_remote_no_buckets" not in state
    assert "hard_zero_buckets" not in state
    assert state["boundary"]["raw_weather_not_exposed_to_strategy"] is True
    assert state["boundary"]["market_data_consumed"] is False


def test_policy_and_checkpoint_identity_fail_closed() -> None:
    bad_policy = _policy()
    bad_policy["strong_exclusion_log_probability_ratio_ceiling"] = 0.3
    bad_checkpoint = _checkpoint()
    bad_checkpoint["last_canonical_event_id"] = "canobs_other"
    state = _build(realization_policy=bad_policy, checkpoint=bad_checkpoint)

    assert state["ok"] is False
    assert "realization_policy_strong_exclusion_log_probability_ratio_ceiling_invalid" in state["block_reasons"]
    assert "realization_projection_latest_event_checkpoint_mismatch" in state["block_reasons"]


def test_projection_materializes_one_l3_realization_row_bound_to_checkpoint_and_fact_snapshot() -> None:
    projection = _build()
    checkpoint = _checkpoint()

    row = materialize_projected_realization_state_row(
        checkpoint_row=checkpoint,
        realization_projection=projection,
        fact_snapshot_id="factsnap_1234567890abcdef",
        created_at="2026-07-03T07:00:00Z",
        committed_at="2026-07-03T07:00:01Z",
        candidate_mode="shadow",
    )

    assert row["realization_state_id"].startswith("rstate_")
    assert row["posterior_run_id"] == checkpoint["posterior_run_id"]
    assert row["fact_snapshot_id"] == "factsnap_1234567890abcdef"
    assert row["target_bucket"] == "32C"
    assert row["second_target_bucket"] == "31C"
    assert row["current_report_bucket"] == ""
    assert row["current_report_bucket_probability"] is None
    assert row["probability_above_current_report_bucket"] is None
    assert row["current_report_bucket_hard_zero"] == 0
    assert len(row["state_sha256"]) == 64
    state = json.loads(row["state_json"])
    assert state["schema"] == "realization_state_l3_v2"
    assert state["candidate_mode"] == "shadow"
    assert state["shadow_only"] is True
    assert state["projection"]["strong_physical_evidence"]["top_bucket_joint_strong_support"] is True
    assert state["projection"]["boundary"]["market_data_consumed"] is False


def test_projection_materialization_rejects_cross_posterior_run_binding() -> None:
    checkpoint = _checkpoint()
    checkpoint["posterior_run_id"] = "post_other_1234567890"

    with pytest.raises(ValueError, match="projected_realization_state_posterior_run_mismatch"):
        materialize_projected_realization_state_row(
            checkpoint_row=checkpoint,
            realization_projection=_build(),
            fact_snapshot_id="factsnap_1234567890abcdef",
            created_at="2026-07-03T07:00:00Z",
            committed_at="2026-07-03T07:00:01Z",
            candidate_mode="shadow",
        )


def test_projection_accepts_real_market_open_edge_buckets() -> None:
    contract = {
        "resolution_contract_id": "resolution-v1",
        "bucket_contract": {
            "29C": {"lower_inclusive": None, "upper_exclusive": 29.5},
            "30C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5},
            "31C": {"lower_inclusive": 30.5, "upper_exclusive": None},
        }
    }
    bridge = {
        "ok": True,
        "state": "available",
        "resolution_contract_id": "resolution-v1",
        "market_bucket_probabilities": {"29C": 0.0, "30C": 0.2, "31C": 0.8},
        "settlement_exclusion_certificates": {"29C": {"state": "valid"}},
    }
    state = _build(resolution_contract=contract, resolution_bridge=bridge)
    assert state["ok"] is True
    assert state["current_report_bucket"] == ""
    assert state["target_bucket"] == "31C"
