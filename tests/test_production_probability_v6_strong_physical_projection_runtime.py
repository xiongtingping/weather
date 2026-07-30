import json
import hashlib

from production_probability_v6_strong_physical_projection_runtime import (
    build_v6_strong_physical_evidence_projection,
)


def _base(*, termination=0.8, latent_terminal=0.7, path="cooling", weather=False):
    event = {
        "canonical_observation_event_id": "event-1",
        "state": "accepted_primary",
        "selected_source_id": "zuuu_metar_raw_amsc",
        "parsed_observation": {"temperature_c": 37.0},
    }
    trajectory = {
        "ok": True,
        "temporal_role": "post_event",
        "latest_canonical_observation_event_id": "event-1",
        "path_state_candidate": path,
    }
    auxiliary = {
        "current_observation_consumed_once": True,
        "no_post_emission_state_transition": True,
        "nwp_hourly_path_likelihood_reapplied": False,
        "termination_risk": {
            "probability_no_material_new_daily_high": termination,
            "canonical_observation_event_id": "event-1",
            "updates_posterior": False,
        },
        "temperature_trajectory_feature_state": trajectory,
        "duration_clocks_after": {
            "precipitation_active": weather,
            "cloud_suppression_active": False,
            "cb_active": False,
        },
    }
    emission = {
        "event_id": "event-1",
        "observation_artifact_sha256": "a" * 64,
        "cloud_suppressed": False,
        "precipitation_present": weather,
        "cb_present": False,
        "log_emission_by_cell": [{"continuous_y_c": 37.0, "pre_state": "warming", "log_emission": -1.0}],
    }
    update = {
        "strong_observation_update_id": "update-1",
        "canonical_observation_event_id": "event-1",
        "model_artifact_sha256": "m" * 64,
        "observation_artifact_sha256": "a" * 64,
        "probability_update_schema": "strong_observation_update_v6_pre_event_predict_emission",
        "state": "applied",
        "no_post_emission_transition": 1,
        "post_state_transition_json": "{}",
        "path_auxiliary_json": json.dumps(auxiliary),
        "observation_emission_json": json.dumps(emission),
    }
    checkpoint = {
        "last_canonical_event_id": "event-1",
        "last_strong_update_id": "update-1",
        "observation_artifact_sha256": "a" * 64,
        "reported_x_lower_bound_c": 37,
        "latent_post_state_json": json.dumps(
            {
                "terminal_cooling": latent_terminal,
                "warming": 1.0 - latent_terminal,
            }
        ),
    }
    policy = {
        "schema": "realization_state_policy_v2",
        "approval_state": "approved",
        "artifact_sha256": "policy-1",
        "strong_support_log_probability_ratio_floor": 0.0,
        "strong_exclusion_probability_ceiling": 0.01,
        "strong_exclusion_log_probability_ratio_ceiling": -1.0,
    }
    return event, update, checkpoint, policy


def _certificate(bucket):
    core = {
        "state": "valid",
        "reason": "full_authority_hard_support_excludes_bucket",
        "bucket": bucket,
        "resolution_contract_id": "resolution-1",
        "bridge_artifact_sha256": "a" * 64,
        "target_contract_sha256": "b" * 64,
        "physical_distribution_sha256": "c" * 64,
        "posterior_checkpoint_id": "checkpoint-1",
        "physical_hard_support_sha256": "d" * 64,
        "hard_residual_support_authority_sha256": "e" * 64,
        "physical_hard_support_c": [37.0],
        "hard_residual_support_c": [0.0],
        "bucket_bounds": {"lower_inclusive": None, "upper_exclusive": 36.5},
        "settlement_support_witness_c": [37.0],
        "full_support_exclusion_verified": True,
    }
    raw = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {**core, "certificate_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}


def test_v6_projection_keeps_soft_unattainability_as_diagnostic_not_no():
    event, update, checkpoint, policy = _base()
    result = build_v6_strong_physical_evidence_projection(
        previous_market_bucket_probabilities={"37C": 0.5, "40C": 0.5},
        posterior_market_bucket_probabilities={"37C": 0.995, "40C": 0.005},
        bucket_contract={"37C": {}, "40C": {}},
        settlement_exclusion_certificates={},
        canonical_observation_event=event,
        strong_observation_update=update,
        checkpoint=checkpoint,
        realization_policy=policy,
    )

    assert result["ok"] is True
    assert result["bucket_exclusions"] == []
    assert result["bucket_evidence"]["40C"]["soft_unreachability_diagnostic"] is True
    assert result["boundary"]["soft_unreachability_never_authorizes_no"] is True


def test_v6_projection_does_not_call_low_probability_alone_a_physical_exclusion():
    event, update, checkpoint, policy = _base(
        termination=0.2,
        latent_terminal=0.1,
        path="sustained_warming",
    )
    result = build_v6_strong_physical_evidence_projection(
        previous_market_bucket_probabilities={"37C": 0.5, "40C": 0.5},
        posterior_market_bucket_probabilities={"37C": 0.995, "40C": 0.005},
        bucket_contract={"37C": {}, "40C": {}},
        settlement_exclusion_certificates={},
        canonical_observation_event=event,
        strong_observation_update=update,
        checkpoint=checkpoint,
        realization_policy=policy,
    )

    assert result["ok"] is True
    assert result["bucket_exclusions"] == []
    assert result["top_bucket_joint_strong_support"] is True


def test_v6_projection_requires_closed_bridge_certificate_for_hard_market_no():
    event, update, checkpoint, policy = _base()
    result = build_v6_strong_physical_evidence_projection(
        previous_market_bucket_probabilities={"36C or below": 0.2, "37C": 0.8},
        posterior_market_bucket_probabilities={"36C or below": 0.0, "37C": 1.0},
        bucket_contract={"36C or below": {}, "37C": {}},
        settlement_exclusion_certificates={
            "36C or below": _certificate("36C or below"),
        },
        canonical_observation_event=event,
        strong_observation_update=update,
        checkpoint=checkpoint,
        realization_policy=policy,
    )

    assert result["bucket_exclusions"][0]["kind"] == "observed_daily_high_exceeded"
    assert result["bucket_exclusions"][0]["settlement_exclusion_certificate_state"] == "valid"
    assert result["bucket_exclusions"][0]["full_support_exclusion_verified"] is True
    assert result["boundary"]["physical_x_not_compared_directly_to_market_bucket_b"] is True
