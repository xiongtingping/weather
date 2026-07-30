from __future__ import annotations

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
from production_probability_v6_p0_runtime import compile_v6_p0_candidate


def _activation():
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
        "nwp_hourly_curve_not_reapplied_as_independent_likelihood": True,
        "runtime_manifest_sha256": "b" * 64,
        "transition_artifact_sha256": "c" * 64,
        "observation_artifact_sha256": "d" * 64,
        "prior_model_artifact_sha256": "f" * 64,
        "weak_likelihood_artifact_sha256": "e" * 64,
        "production_transaction_b_authorized": True,
        "places_orders": False,
        "authorizes_clob": False,
    }
    value["activation_contract_sha256"] = activation_sha256(value)
    return value


def test_p0_compiles_one_continuous_joint_and_reported_x_projection() -> None:
    result = compile_v6_p0_candidate(
        activation=_activation(),
        target_identity="physical_weather_day_v1:ZUUU:2026-07-15",
        event_date="2026-07-15",
        decision_as_of="2026-07-15T00:00:00Z",
        state_reference_valid_at="2026-07-14T16:00:00Z",
        prior_context_id="prior-1",
        prior_model_artifact_sha256="f" * 64,
        forecast_evidence_snapshot_id="forecast-1",
        weak_likelihood_artifact_sha256="e" * 64,
        resolution_contract_id="resolution-1",
        forcing_generation_sha256="1" * 64,
        initial_joint=(
            JointCell(34.4, "weather_suppressed", 0.3),
            JointCell(34.6, "warming", 0.2),
            JointCell(38.2, "warming", 0.5),
        ),
        candidate_scenario_responsibilities={"rows": [
            {"continuous_y_c": y, "scenarios": [{"scenario_id": "test", "responsibility": 1.0}]}
            for y in (34.4, 34.6, 38.2)
        ]},
        replay_generation=2,
        supersedes_checkpoint_id="checkpoint-v5-last",
    )

    checkpoint = result["checkpoint_row"]
    assert result["state"] == "production_successor_p0_candidate_ready"
    assert checkpoint["checkpoint_probability_schema"] == CHECKPOINT_SCHEMA
    assert result["reported_physical_x_distribution"] == {"34": 0.3, "35": 0.2, "38": 0.5}
    assert checkpoint["last_canonical_event_id"] == ""
    assert checkpoint["reported_x_lower_bound_c"] is None
    assert result["places_orders"] is False


def test_v6_p0_prunes_only_within_frozen_numeric_error_budget() -> None:
    result = compile_v6_p0_candidate(
        activation=_activation(),
        target_identity="physical_weather_day_v1:ZUUU:2026-07-15",
        event_date="2026-07-15",
        decision_as_of="2026-07-14T16:00:00Z",
        state_reference_valid_at="2026-07-14T16:00:00Z",
        prior_context_id="prior-1",
        prior_model_artifact_sha256="f" * 64,
        forecast_evidence_snapshot_id="forecast-1",
        weak_likelihood_artifact_sha256="e" * 64,
        resolution_contract_id="resolution-1",
        forcing_generation_sha256="1" * 64,
        initial_joint=(
            JointCell(34.0, "warming", 1.0 - 5e-11),
            JointCell(60.0, "warming", 5e-11),
            JointCell(20.0, "warming", 0.0),
        ),
        candidate_scenario_responsibilities={"rows": [
            {"continuous_y_c": y, "scenarios": [{"scenario_id": "test", "responsibility": 1.0}]}
            for y in (20.0, 34.0, 60.0)
        ]},
        replay_generation=0,
    )
    checkpoint = result["checkpoint_row"]
    assert 0.0 <= checkpoint["p0_pruned_probability_mass"] <= 1e-10
    assert checkpoint["p0_pruned_cell_count"] == 2
    rows = __import__("json").loads(checkpoint["joint_posterior_json"])
    assert rows == [{"continuous_y_c": 34.0, "probability": 1.0, "state": "warming"}]
