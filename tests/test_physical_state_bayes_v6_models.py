from __future__ import annotations

from physical_latent_state_contract import LATENT_STATES
from physical_state_bayes_v6.artifact_contracts import (
    OBSERVATION_ARTIFACT_SCHEMA,
    TRANSITION_ARTIFACT_SCHEMA,
    validate_observation_artifact,
    validate_transition_artifact,
)
from physical_state_bayes_v6.contracts import DurationClocks, JointCell
from physical_state_bayes_v6.core import predict_to_event, update_with_observation
from physical_state_bayes_v6.observation_model import (
    ObservationFeatureVector,
    build_joint_observation_emission,
)
from physical_state_bayes_v6.transition_model import (
    ForecastScenarioControl,
    build_candidate_forcing_slices,
)


def _transition_artifact():
    transitions = {state: {} for state in LATENT_STATES}
    transitions["warming"] = {
        "slowing_warming": {
            "base_rate_per_hour": 0.2,
            "coefficients": {"forecast_rate_c_per_hour": -0.4},
        },
        "terminal_cooling": {
            "base_rate_per_hour": 0.05,
            "coefficients": {
                "future_max_increment_c": -0.8,
                "precipitation_probability": 2.0,
            },
        },
    }
    transitions["slowing_warming"] = {
        "warming": {
            "base_rate_per_hour": 0.15,
            "coefficients": {"forecast_rate_c_per_hour": 0.5},
        }
    }
    return validate_transition_artifact(
        {
            "schema": TRANSITION_ARTIFACT_SCHEMA,
            "state": "candidate",
            "production_runtime_eligible": False,
            "shadow_only": False,
            "transitions": transitions,
        }
    )


def _observation_artifact():
    models = {}
    for state in LATENT_STATES:
        mean_rate = 1.0 if state == "warming" else (-1.0 if state == "terminal_cooling" else 0.0)
        models[state] = {
            "continuous_mean": [mean_rate, 8.0],
            "continuous_covariance": [[0.5, 0.0], [0.0, 4.0]],
            "weather_signature_probabilities": {
                "clear_dry": 0.8 if state == "warming" else 0.2,
                "precip_convective": 0.1 if state == "warming" else 0.7,
                "__other__": 0.1,
            },
        }
    return validate_observation_artifact(
        {
            "schema": OBSERVATION_ARTIFACT_SCHEMA,
            "state": "candidate",
            "production_runtime_eligible": False,
            "shadow_only": False,
            "state_models": models,
        }
    )


def test_transition_model_is_candidate_conditioned_and_row_normalized() -> None:
    scenarios = {
        34.0: (
            ForecastScenarioControl("low", 1.0, -0.5, 0.2, 1.5, -30.0, 1.0, 0.8, 1.0, 0.8, 1.0, 0.2, 1.0),
        ),
        38.0: (
            ForecastScenarioControl("high", 1.0, 1.0, 4.0, 0.2, 120.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0),
        ),
    }
    slices = build_candidate_forcing_slices(
        y_grid_c=(34.0, 38.0),
        reported_regular_max_c=33,
        pre_event_duration_clocks=DurationClocks(),
        scenarios_by_y=scenarios,
        delta_minutes=30.0,
        transition_artifact=_transition_artifact(),
    )

    low = slices[0].scenarios[0].matrix["warming"]
    high = slices[1].scenarios[0].matrix["warming"]
    assert abs(sum(low.values()) - 1.0) < 1e-9
    assert abs(sum(high.values()) - 1.0) < 1e-9
    assert low["terminal_cooling"] > high["terminal_cooling"]


def test_joint_emission_updates_candidate_mass_through_predicted_state() -> None:
    forcing = build_candidate_forcing_slices(
        y_grid_c=(34.0, 38.0),
        reported_regular_max_c=33,
        pre_event_duration_clocks=DurationClocks(),
        scenarios_by_y={
            34.0: (ForecastScenarioControl("low", 1.0, -0.5, 0.2, 1.5, -30.0, 1.0, 0.8, 1.0, 0.8, 1.0, 0.2, 1.0),),
            38.0: (ForecastScenarioControl("high", 1.0, 1.0, 4.0, 0.2, 120.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0),),
        },
        delta_minutes=90.0,
        transition_artifact=_transition_artifact(),
    )
    prediction = predict_to_event(
        previous_joint=(
            JointCell(34.0, "warming", 0.5),
            JointCell(38.0, "warming", 0.5),
        ),
        previous_checkpoint_sha256="a" * 64,
        previous_reported_regular_max_c=33,
        previous_y_lower_bound_c=32.5,
        duration_clocks_before=DurationClocks(),
        event_id="event-1",
        event_valid_at="2026-07-15T08:00:00Z",
        delta_minutes=90.0,
        forcing_generation_sha256="b" * 64,
        transition_artifact_sha256=str(_transition_artifact()["artifact_sha256"]),
        candidate_forcing=forcing,
    )
    emission = build_joint_observation_emission(
        prediction=prediction,
        is_regular_metar=True,
        reported_temperature_c=34.0,
        cloud_suppressed=False,
        precipitation_present=False,
        cb_present=False,
        features=ObservationFeatureVector(1.0, 8.0, "clear_dry"),
        observation_artifact=_observation_artifact(),
    )
    post = update_with_observation(prediction=prediction, emission=emission)
    mass = {}
    for row in post.joint:
        mass[row.y_c] = mass.get(row.y_c, 0.0) + row.probability

    assert mass[38.0] > 0.5
    assert post.invariant_report["current_event_consumed_only_in_emission"] is True
