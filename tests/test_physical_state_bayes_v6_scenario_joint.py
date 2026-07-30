import math

from physical_latent_state_contract import LATENT_STATES
from physical_state_bayes_v6.contracts import (
    CandidateForcingSlice, CanonicalObservationEmission, DurationClocks, JointCell, ScenarioTransition,
)
from physical_state_bayes_v6.core import pre_event_prediction_sha256
from physical_state_bayes_v6.scenario_joint import (
    initialize_scenario_joint, marginalize_scenario_joint, predict_scenario_joint_to_event,
    update_scenario_joint_with_observation,
)


def _matrix(target: str | None = None):
    return {
        source: {state: float(state == (target or source)) for state in LATENT_STATES}
        for source in LATENT_STATES
    }


def _forcing(matrix_a, matrix_b):
    return (CandidateForcingSlice(35.0, (
        ScenarioTransition("a", 0.5, matrix_a),
        ScenarioTransition("b", 0.5, matrix_b),
    )),)


def _emission(prediction, event_id):
    scores = {
        (cell.y_c, cell.state): (-2.0 if cell.state == "weather_suppressed" else 0.0)
        for cell in prediction.joint
    }
    return CanonicalObservationEmission(
        event_id=event_id, pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
        event_valid_at=prediction.event_valid_at, is_regular_metar=False,
        reported_temperature_c=None, cloud_suppressed=False, precipitation_present=False,
        cb_present=False, log_emission_by_cell=scores, observation_artifact_sha256="d" * 64,
    )


def test_scenario_downweight_is_checkpointed_and_persists_into_next_event():
    initial = initialize_scenario_joint(
        (JointCell(35.0, "warming", 1.0),),
        {"rows": [{"continuous_y_c": 35.0, "scenarios": [
            {"scenario_id": "a", "responsibility": 0.5},
            {"scenario_id": "b", "responsibility": 0.5},
        ]}]},
    )
    first_prediction, first_predicted_joint = predict_scenario_joint_to_event(
        previous_scenario_joint=initial, previous_checkpoint_sha256="0" * 64,
        previous_reported_regular_max_c=None, previous_y_lower_bound_c=None,
        duration_clocks_before=DurationClocks(), event_id="event-1",
        event_valid_at="2026-07-17T01:00:00Z", delta_minutes=60.0,
        forcing_generation_sha256="1" * 64, transition_artifact_sha256="2" * 64,
        candidate_forcing=_forcing(_matrix("weather_suppressed"), _matrix("warming")),
    )
    _post, first_checkpoint_joint, first_trace = update_scenario_joint_with_observation(
        prediction=first_prediction, predicted_scenario_joint=first_predicted_joint,
        emission=_emission(first_prediction, "event-1"),
    )
    first_after = {row["scenario_id"]: row["probability_after"] for row in first_trace["rows"]}
    assert first_after["a"] < 0.2
    second_prediction, second_predicted_joint = predict_scenario_joint_to_event(
        previous_scenario_joint=first_checkpoint_joint, previous_checkpoint_sha256="3" * 64,
        previous_reported_regular_max_c=None, previous_y_lower_bound_c=None,
        duration_clocks_before=DurationClocks(), event_id="event-2",
        event_valid_at="2026-07-17T02:00:00Z", delta_minutes=60.0,
        forcing_generation_sha256="1" * 64, transition_artifact_sha256="2" * 64,
        candidate_forcing=_forcing(_matrix(), _matrix()),
    )
    _post2, _second_checkpoint_joint, second_trace = update_scenario_joint_with_observation(
        prediction=second_prediction, predicted_scenario_joint=second_predicted_joint,
        emission=_emission(second_prediction, "event-2"),
    )
    second_before = {row["scenario_id"]: row["probability_before"] for row in second_trace["rows"]}
    assert math.isclose(second_before["a"], first_after["a"], abs_tol=1e-12)
    assert abs(sum(cell.probability for cell in marginalize_scenario_joint(first_checkpoint_joint)) - 1.0) < 1e-12
    assert second_trace["carried_forward_to_next_event"] is True
