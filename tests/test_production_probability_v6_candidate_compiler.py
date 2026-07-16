from __future__ import annotations

from physical_latent_state_contract import LATENT_STATES
from physical_state_bayes_v6.contracts import (
    CandidateForcingSlice,
    CanonicalObservationEmission,
    DurationClocks,
    JointCell,
    ScenarioTransition,
)
from physical_state_bayes_v6.core import predict_to_event, pre_event_prediction_sha256
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    FORMULA_ID,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION,
    STRONG_UPDATE_SCHEMA,
    activation_sha256,
)
from production_probability_v6_candidate_compiler import build_v6_transaction_b_candidate
from production_probability_v6_public_ssot_writer import materialize_v6_public_physical_fact_row


def _identity():
    return {
        source: {target: 1.0 if source == target else 0.0 for target in LATENT_STATES}
        for source in LATENT_STATES
    }


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


def test_compiler_materializes_v6_update_without_post_transition() -> None:
    previous_joint = (
        JointCell(34.2, "warming", 0.4),
        JointCell(38.2, "warming", 0.6),
    )
    forcing = tuple(
        CandidateForcingSlice(
            y_c,
            (ScenarioTransition(f"scenario-{y_c}", 1.0, _identity()),),
        )
        for y_c in (34.2, 38.2)
    )
    previous = {
        "checkpoint_probability_schema": CHECKPOINT_SCHEMA,
        "posterior_checkpoint_id": "checkpoint-0",
        "posterior_run_id": "run-0",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-15",
        "joint_posterior": [
            {"continuous_y_c": row.y_c, "state": row.state, "probability": row.probability}
            for row in previous_joint
        ],
        "joint_posterior_sha256": "f" * 64,
        "reported_x_lower_bound_c": 33,
        "latent_observed_lower_bound_c": 32.5,
        "duration_clocks": {},
        "forcing_generation_sha256": "1" * 64,
        "forecast_evidence_snapshot_id": "forecast-1",
        "prior_context_id": "prior-1",
        "resolution_contract_id": "resolution-1",
        "consumed_event_ids": [],
        "replay_generation": 1,
    }
    event = {
        "canonical_observation_event_id": "event-1",
        "valid_time": "2026-07-15T07:30:00Z",
        "state": "accepted_primary",
    }
    prediction = predict_to_event(
        previous_joint=previous_joint,
        previous_checkpoint_sha256="f" * 64,
        previous_reported_regular_max_c=33,
        previous_y_lower_bound_c=32.5,
        duration_clocks_before=DurationClocks(),
        event_id="event-1",
        event_valid_at=event["valid_time"],
        delta_minutes=30.0,
        forcing_generation_sha256="1" * 64,
        transition_artifact_sha256="c" * 64,
        candidate_forcing=forcing,
    )
    emission = CanonicalObservationEmission(
        event_id="event-1",
        pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
        event_valid_at=event["valid_time"],
        is_regular_metar=True,
        reported_temperature_c=35.0,
        cloud_suppressed=False,
        precipitation_present=False,
        cb_present=False,
        log_emission_by_cell={(row.y_c, row.state): 0.0 for row in prediction.joint},
        observation_artifact_sha256="d" * 64,
    )

    candidate = build_v6_transaction_b_candidate(
        activation=_activation(),
        previous_checkpoint=previous,
        canonical_event=event,
        candidate_forcing=forcing,
        emission=emission,
        delta_minutes=30.0,
        decision_as_of="2026-07-15T07:30:00.100Z",
        completed_at="2026-07-15T07:30:00.150Z",
        scenario_control_lineage_sha256="2" * 64,
        canonical_emission_input_sha256="3" * 64,
    )

    update = candidate["strong_update_row"]
    checkpoint = candidate["checkpoint_row"]
    assert candidate["state"] == "production_successor_candidate_ready"
    assert update["probability_update_schema"] == STRONG_UPDATE_SCHEMA
    assert update["post_state_transition_json"] == "{}"
    assert update["no_post_emission_transition"] == 1
    assert update["scenario_control_lineage_sha256"] == "2" * 64
    assert update["canonical_emission_input_sha256"] == "3" * 64
    assert checkpoint["checkpoint_probability_schema"] == CHECKPOINT_SCHEMA
    assert checkpoint["reported_x_lower_bound_c"] == 35
    assert candidate["reported_physical_x_distribution"] == {38: 1.0}
    assert candidate["places_orders"] is False

    public = materialize_v6_public_physical_fact_row(
        activation=_activation(),
        strong_update_row=update,
        checkpoint_row=checkpoint,
        fact_snapshot_id="fact-v6-1",
        slug="chengdu-high-2026-07-15",
        event_date="2026-07-15",
        fact_as_of="2026-07-15T07:30:00.150Z",
        correlation_id="correlation-v6-1",
        market_bucket_probabilities_na_reason="resolution_bridge_not_closed",
    )
    assert public["composer_version"] == "physical_state_bayes_v6_production"
    assert '"reported_physical_x_distribution":{"38":1.0}' in public["fact_json"]
    assert '"latent_y_public":false' in public["fact_json"]
