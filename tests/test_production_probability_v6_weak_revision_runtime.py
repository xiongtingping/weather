from __future__ import annotations

import json

from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    FORMULA_ID,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION,
    STRONG_UPDATE_SCHEMA,
    activation_sha256,
)
from production_probability_v6_weak_revision_runtime import compile_v6_weak_revision_candidate


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
        "nwp_hourly_curve_is_transition_control_only": True,
        "runtime_manifest_sha256": "1" * 64,
        "transition_artifact_sha256": "2" * 64,
        "observation_artifact_sha256": "3" * 64,
        "prior_model_artifact_sha256": "4" * 64,
        "weak_likelihood_artifact_sha256": "5" * 64,
        "production_transaction_b_authorized": True,
        "places_orders": False,
        "authorizes_clob": False,
    }
    value["activation_contract_sha256"] = activation_sha256(value)
    return value


def _snapshot(snapshot_id, low, high, forcing):
    return {
        "forecast_evidence_snapshot_id": snapshot_id,
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-15",
        "weak_likelihood_artifact_sha256": "5" * 64,
        "weak_likelihood_sha256": ("6" if snapshot_id == "old" else "7") * 64,
        "likelihood_by_x_json": json.dumps({"34": low, "38": high}),
        "forecast_scenario_paths_sha256": forcing * 64,
        "candidate_scenario_responsibilities_sha256": "9" * 64,
    }


def test_revision_reweights_y_but_preserves_state_conditionals_and_history() -> None:
    previous = {
        "checkpoint_probability_schema": CHECKPOINT_SCHEMA,
        "posterior_checkpoint_id": "checkpoint-1",
        "posterior_run_id": "run-1",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-15",
        "forecast_evidence_snapshot_id": "old",
        "joint_posterior_json": json.dumps(
            [
                {"continuous_y_c": 34.0, "state": "warming", "probability": 0.2},
                {"continuous_y_c": 34.0, "state": "weather_suppressed", "probability": 0.2},
                {"continuous_y_c": 38.0, "state": "warming", "probability": 0.3},
                {"continuous_y_c": 38.0, "state": "weather_suppressed", "probability": 0.3},
            ]
        ),
        "joint_posterior_sha256": "a" * 64,
        "forcing_generation_sha256": "b" * 64,
        "consumed_event_ids_json": json.dumps(["metar-1", "speci-1"]),
        "last_canonical_event_id": "speci-1",
        "last_event_valid_at": "2026-07-15T06:10:00Z",
        "reported_x_lower_bound_c": 33,
        "latent_observed_lower_bound_c": 32.5,
        "observation_artifact_sha256": "3" * 64,
        "replay_generation": 2,
    }
    result = compile_v6_weak_revision_candidate(
        activation=_activation(),
        previous_checkpoint=previous,
        previous_snapshot=_snapshot("old", 1.0, 1.0, "b"),
        revised_snapshot=_snapshot("new", 3.0, 0.5, "8"),
        trigger_source_id="zuuu_taf_forecast_amsc",
        trigger_source_revision_id="taf-amd-1",
        revision_kind="amendment",
        decision_as_of="2026-07-15T06:12:00Z",
    )

    checkpoint = result["checkpoint_row"]
    rows = json.loads(checkpoint["joint_posterior_json"])
    by_y = {}
    for row in rows:
        by_y.setdefault(row["continuous_y_c"], []).append(row["probability"])
    assert by_y[34.0][0] / sum(by_y[34.0]) == 0.5
    assert by_y[38.0][0] / sum(by_y[38.0]) == 0.5
    assert checkpoint["consumed_event_ids_json"] == previous["consumed_event_ids_json"]
    assert checkpoint["last_event_valid_at"] == previous["last_event_valid_at"]
    assert checkpoint["forcing_generation_sha256"] == "8" * 64
    assert result["past_state_transitions_replayed"] is False
    assert result["future_forcing_replaced"] is True
    assert result["places_orders"] is False
