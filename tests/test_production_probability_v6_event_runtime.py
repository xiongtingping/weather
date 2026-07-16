from __future__ import annotations

from physical_latent_state_contract import LATENT_STATES
from physical_state_bayes_v6.artifact_contracts import (
    OBSERVATION_ARTIFACT_SCHEMA,
    TRANSITION_ARTIFACT_SCHEMA,
    artifact_sha256,
)
from physical_state_bayes_v6.contracts import JointCell
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    FORMULA_ID,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION,
    STRONG_UPDATE_SCHEMA,
    activation_sha256,
)
from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_p0_runtime import compile_v6_p0_candidate


def _approved(value):
    value.update(
        {
            "state": "approved",
            "production_runtime_eligible": True,
            "shadow_only": False,
            "training_dataset_sha256": "1" * 64,
            "training_code_sha256": "2" * 64,
            "promotion_evidence_sha256": "3" * 64,
        }
    )
    value["artifact_sha256"] = artifact_sha256(value)
    return value


def _transition_artifact():
    transitions = {state: {} for state in LATENT_STATES}
    transitions["warming"] = {
        "slowing_warming": {
            "base_rate_per_hour": 0.2,
            "coefficients": {"forecast_rate_c_per_hour": -0.3},
        },
        "weather_suppressed": {
            "base_rate_per_hour": 0.05,
            "coefficients": {"cloud_suppression_probability": 1.0},
        },
    }
    return _approved({"schema": TRANSITION_ARTIFACT_SCHEMA, "transitions": transitions})


def _observation_artifact():
    models = {
        state: {
            "continuous_mean": [1.0 if state == "warming" else 0.0, 10.0],
            "continuous_covariance": [[1.0, 0.0], [0.0, 4.0]],
            "weather_signature_probabilities": {
                "cloud=clear_or_few|weather=none|convective=0": 0.8,
                "__other__": 0.2,
            },
        }
        for state in LATENT_STATES
    }
    return _approved({"schema": OBSERVATION_ARTIFACT_SCHEMA, "state_models": models})


def _activation(transition, observation):
    value = {
        "schema": SCHEMA_VERSION,
        "state": "production_enabled",
        "bayes_formula_id": FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "v5_fallback_forbidden": True,
        "pre_event_predict_does_not_consume_current_observation": True,
        "no_post_emission_state_transition": True,
        "nwp_hourly_curve_is_transition_control_only": True,
        "runtime_manifest_sha256": "4" * 64,
        "transition_artifact_sha256": transition["artifact_sha256"],
        "observation_artifact_sha256": observation["artifact_sha256"],
        "prior_model_artifact_sha256": "5" * 64,
        "weak_likelihood_artifact_sha256": "6" * 64,
        "production_transaction_b_authorized": True,
        "places_orders": False,
        "authorizes_clob": False,
    }
    value["activation_contract_sha256"] = activation_sha256(value)
    return value


def test_unique_event_runtime_builds_production_transaction_candidate() -> None:
    transition = _transition_artifact()
    observation = _observation_artifact()
    activation = _activation(transition, observation)
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity="physical_weather_day_v1:ZUUU:2026-07-15",
        event_date="2026-07-15",
        decision_as_of="2026-07-14T16:00:00Z",
        state_reference_valid_at="2026-07-14T16:00:00Z",
        prior_context_id="prior-1",
        prior_model_artifact_sha256="5" * 64,
        forecast_evidence_snapshot_id="forecast-1",
        weak_likelihood_artifact_sha256="6" * 64,
        resolution_contract_id="resolution-1",
        forcing_generation_sha256="7" * 64,
        initial_joint=(
            JointCell(34.0, "warming", 0.4),
            JointCell(38.0, "warming", 0.6),
        ),
        replay_generation=2,
        supersedes_checkpoint_id="v5-last",
    )
    weak = {
        "artifact_sha256": "6" * 64,
        "joint_forecast_weak_likelihood_sha256": "8" * 64,
        "forecast_scenario_paths": {
            "sha256": "7" * 64,
            "paths": [
                {
                    "scenario_id": "gfs-r1",
                    "hourly_curve": [
                        {"valid_time_utc": "2026-07-14T16:00:00Z", "temperature_c": 25.0},
                        {"valid_time_utc": "2026-07-14T17:00:00Z", "temperature_c": 26.0},
                        {"valid_time_utc": "2026-07-15T08:00:00Z", "temperature_c": 38.0},
                    ],
                }
            ],
        },
        "candidate_scenario_responsibilities": {
            "sha256": "9" * 64,
            "rows": [
                {"continuous_y_c": y, "scenarios": [{"scenario_id": "gfs-r1", "responsibility": 1.0}]}
                for y in (34.0, 38.0)
            ],
        },
    }
    event = {
        "canonical_observation_event_id": "event-1",
        "valid_time": "2026-07-14T16:30:00Z",
        "report_type": "METAR",
        "state": "accepted_primary",
        "parsed_observation": {
            "temperature_c": 25.0,
            "dewpoint_c": 15.0,
            "cloud_text": "FEW040",
            "weather_text": "",
        },
    }

    result = compose_v6_canonical_event(
        activation=activation,
        previous_checkpoint=p0["checkpoint_row"],
        canonical_event=event,
        pre_event_history=[],
        weak_likelihood=weak,
        transition_artifact=transition,
        observation_artifact=observation,
        decision_as_of="2026-07-14T16:30:00.100Z",
        completed_at="2026-07-14T16:30:00.150Z",
    )

    assert result["state"] == "production_successor_event_candidate_ready"
    assert result["delta_minutes"] == 30.0
    assert result["current_observation_consumed_once"] is True
    assert result["nwp_hourly_path_likelihood_reapplied"] is False
    assert result["strong_update_row"]["post_state_transition_json"] == "{}"
    assert result["checkpoint_row"]["reported_x_lower_bound_c"] == 25


def test_frozen_responsibility_grid_may_be_superset_after_hard_bound() -> None:
    transition = _transition_artifact()
    observation = _observation_artifact()
    activation = _activation(transition, observation)
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity="physical_weather_day_v1:ZUUU:2026-07-15",
        event_date="2026-07-15",
        decision_as_of="2026-07-14T16:00:00Z",
        state_reference_valid_at="2026-07-14T16:00:00Z",
        prior_context_id="prior-1",
        prior_model_artifact_sha256="5" * 64,
        forecast_evidence_snapshot_id="forecast-1",
        weak_likelihood_artifact_sha256="6" * 64,
        resolution_contract_id="resolution-1",
        forcing_generation_sha256="7" * 64,
        initial_joint=(JointCell(24.0, "warming", 0.4), JointCell(38.0, "warming", 0.6)),
        replay_generation=2,
        supersedes_checkpoint_id="v5-last",
    )
    weak = {
        "artifact_sha256": "6" * 64,
        "joint_forecast_weak_likelihood_sha256": "8" * 64,
        "forecast_scenario_paths": {"sha256": "7" * 64, "paths": []},
        "candidate_scenario_responsibilities": {
            "sha256": "9" * 64,
            "rows": [
                {"continuous_y_c": y, "scenarios": [{"scenario_id": "missing", "responsibility": 1.0}]}
                for y in (24.0, 38.0)
            ],
        },
    }
    first = compose_v6_canonical_event(
        activation=activation,
        previous_checkpoint=p0["checkpoint_row"],
        canonical_event={
            "canonical_observation_event_id": "event-1",
            "valid_time": "2026-07-14T16:30:00Z",
            "report_type": "METAR",
            "state": "accepted_primary",
            "parsed_observation": {"temperature_c": 25.0, "dewpoint_c": 15.0, "cloud_text": "FEW040", "weather_text": ""},
        },
        pre_event_history=[],
        weak_likelihood=weak,
        transition_artifact=transition,
        observation_artifact=observation,
        decision_as_of="2026-07-14T16:30:00Z",
        completed_at="2026-07-14T16:30:00Z",
    )
    assert {row["continuous_y_c"] for row in __import__("json").loads(first["checkpoint_row"]["joint_posterior_json"])} == {38.0}
    second = compose_v6_canonical_event(
        activation=activation,
        previous_checkpoint=first["checkpoint_row"],
        canonical_event={
            "canonical_observation_event_id": "event-2",
            "valid_time": "2026-07-14T17:00:00Z",
            "report_type": "METAR",
            "state": "accepted_primary",
            "parsed_observation": {"temperature_c": 26.0, "dewpoint_c": 15.0, "cloud_text": "FEW040", "weather_text": ""},
        },
        pre_event_history=[{
            "canonical_observation_event_id": "event-1",
            "valid_time": "2026-07-14T16:30:00Z",
            "report_type": "METAR",
            "parsed_observation": {"temperature_c": 25.0},
        }],
        weak_likelihood=weak,
        transition_artifact=transition,
        observation_artifact=observation,
        decision_as_of="2026-07-14T17:00:00Z",
        completed_at="2026-07-14T17:00:00Z",
    )
    assert second["checkpoint_row"]["reported_x_lower_bound_c"] == 26
