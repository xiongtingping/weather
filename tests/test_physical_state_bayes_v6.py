from __future__ import annotations

from collections import defaultdict

import pytest

from physical_latent_state_contract import LATENT_STATES
from physical_state_bayes_v6.contracts import (
    CandidateForcingSlice,
    CanonicalObservationEmission,
    DurationClocks,
    JointCell,
    ScenarioTransition,
)
from physical_state_bayes_v6.core import (
    predict_to_event,
    pre_event_prediction_sha256,
    project_reported_x,
    update_with_observation,
)


def _identity() -> dict[str, dict[str, float]]:
    return {
        source: {target: 1.0 if source == target else 0.0 for target in LATENT_STATES}
        for source in LATENT_STATES
    }


def _move(source: str, target: str) -> dict[str, dict[str, float]]:
    matrix = _identity()
    matrix[source] = {
        state: 1.0 if state == target else 0.0 for state in LATENT_STATES
    }
    return matrix


def _marginal(rows: tuple[JointCell, ...]) -> dict[float, float]:
    result: dict[float, float] = defaultdict(float)
    for row in rows:
        result[row.y_c] += row.probability
    return dict(result)


def _prediction():
    previous = (
        JointCell(34.2, "warming", 0.45),
        JointCell(38.2, "warming", 0.55),
    )
    forcing = (
        CandidateForcingSlice(
            34.2,
            (
                ScenarioTransition(
                    "low_cloud_rain",
                    1.0,
                    _move("warming", "weather_suppressed"),
                ),
            ),
        ),
        CandidateForcingSlice(
            38.2,
            (
                ScenarioTransition(
                    "hot_clear",
                    1.0,
                    _move("warming", "slowing_warming"),
                ),
            ),
        ),
    )
    return previous, predict_to_event(
        previous_joint=previous,
        previous_checkpoint_sha256="checkpoint-a",
        previous_reported_regular_max_c=33,
        previous_y_lower_bound_c=32.5,
        duration_clocks_before=DurationClocks(),
        event_id="metar-1",
        event_valid_at="2026-07-15T07:30:00Z",
        delta_minutes=30.0,
        forcing_generation_sha256="forcing-a",
        transition_artifact_sha256="transition-a",
        candidate_forcing=forcing,
    )


def test_predict_preserves_y_marginal_but_uses_candidate_conditioned_forcing() -> None:
    previous, prediction = _prediction()

    assert _marginal(prediction.joint) == pytest.approx(_marginal(previous))
    assert {(row.y_c, row.state) for row in prediction.joint} == {
        (34.2, "weather_suppressed"),
        (38.2, "slowing_warming"),
    }


def test_update_changes_y_marginal_and_does_not_run_post_transition() -> None:
    _, prediction = _prediction()
    scores = {
        (row.y_c, row.state): (1.5 if row.y_c == 38.2 else -1.5)
        for row in prediction.joint
    }
    emission = CanonicalObservationEmission(
        event_id=prediction.event_id,
        pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
        event_valid_at=prediction.event_valid_at,
        is_regular_metar=False,
        reported_temperature_c=34.0,
        cloud_suppressed=False,
        precipitation_present=False,
        cb_present=False,
        log_emission_by_cell=scores,
        observation_artifact_sha256="emission-a",
    )

    post = update_with_observation(prediction=prediction, emission=emission)

    assert _marginal(post.joint)[38.2] > 0.9
    assert {(row.y_c, row.state) for row in post.joint} == {
        (34.2, "weather_suppressed"),
        (38.2, "slowing_warming"),
    }
    assert post.invariant_report["no_post_emission_state_transition"] is True
    assert post.observed_reported_regular_max_c == 33
    assert post.y_lower_bound_c == 32.5


def test_regular_metar_applies_hard_bound_but_speci_does_not() -> None:
    _, prediction = _prediction()
    scores = {(row.y_c, row.state): 0.0 for row in prediction.joint}
    common = dict(
        event_id=prediction.event_id,
        pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
        event_valid_at=prediction.event_valid_at,
        reported_temperature_c=35.0,
        cloud_suppressed=True,
        precipitation_present=True,
        cb_present=True,
        log_emission_by_cell=scores,
        observation_artifact_sha256="emission-a",
    )

    speci = update_with_observation(
        prediction=prediction,
        emission=CanonicalObservationEmission(is_regular_metar=False, **common),
    )
    regular = update_with_observation(
        prediction=prediction,
        emission=CanonicalObservationEmission(is_regular_metar=True, **common),
    )

    assert set(_marginal(speci.joint)) == {34.2, 38.2}
    assert _marginal(regular.joint) == pytest.approx({38.2: 1.0})
    assert regular.duration_clocks_after.precipitation_minutes == 0.0
    assert regular.duration_clocks_after.cb_minutes == 0.0
    assert regular.duration_clocks_after.precipitation_active is True
    assert regular.duration_clocks_after.cb_active is True


def test_integer_report_bound_keeps_continuous_mass_inside_report_interval() -> None:
    previous = (
        JointCell(34.4, "warming", 0.5),
        JointCell(34.6, "warming", 0.5),
    )
    forcing = tuple(
        CandidateForcingSlice(
            y_c,
            (ScenarioTransition(f"scenario-{y_c}", 1.0, _identity()),),
        )
        for y_c in (34.4, 34.6)
    )
    prediction = predict_to_event(
        previous_joint=previous,
        previous_checkpoint_sha256="checkpoint-a",
        previous_reported_regular_max_c=None,
        previous_y_lower_bound_c=None,
        duration_clocks_before=DurationClocks(),
        event_id="metar-bound",
        event_valid_at="2026-07-15T08:00:00Z",
        delta_minutes=30.0,
        forcing_generation_sha256="forcing-a",
        transition_artifact_sha256="transition-a",
        candidate_forcing=forcing,
    )
    post = update_with_observation(
        prediction=prediction,
        emission=CanonicalObservationEmission(
            event_id=prediction.event_id,
            pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
            event_valid_at=prediction.event_valid_at,
            is_regular_metar=True,
            reported_temperature_c=35.0,
            cloud_suppressed=False,
            precipitation_present=False,
            cb_present=False,
            log_emission_by_cell={(row.y_c, row.state): 0.0 for row in prediction.joint},
            observation_artifact_sha256="emission-a",
        ),
    )

    assert _marginal(post.joint) == pytest.approx({34.6: 1.0})
    assert post.y_lower_bound_c == 34.5
    assert post.observed_reported_regular_max_c == 35


def test_final_locked_cells_must_match_observed_reported_x_without_masking_other_states() -> None:
    previous = (
        JointCell(37.75, "final_locked", 0.3),
        JointCell(39.25, "final_locked", 0.4),
        JointCell(39.25, "warming", 0.3),
    )
    forcing = tuple(
        CandidateForcingSlice(
            y_c,
            (ScenarioTransition(f"scenario-{y_c}", 1.0, _identity()),),
        )
        for y_c in (37.75, 39.25)
    )
    prediction = predict_to_event(
        previous_joint=previous,
        previous_checkpoint_sha256="checkpoint-locked",
        previous_reported_regular_max_c=38,
        previous_y_lower_bound_c=37.5,
        duration_clocks_before=DurationClocks(),
        event_id="speci-after-lock",
        event_valid_at="2026-07-16T15:30:00Z",
        delta_minutes=30.0,
        forcing_generation_sha256="forcing-a",
        transition_artifact_sha256="transition-a",
        candidate_forcing=forcing,
    )
    post = update_with_observation(
        prediction=prediction,
        emission=CanonicalObservationEmission(
            event_id=prediction.event_id,
            pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
            event_valid_at=prediction.event_valid_at,
            is_regular_metar=False,
            reported_temperature_c=29.0,
            cloud_suppressed=False,
            precipitation_present=False,
            cb_present=True,
            log_emission_by_cell={(row.y_c, row.state): 0.0 for row in prediction.joint},
            observation_artifact_sha256="emission-a",
        ),
    )

    assert {(row.y_c, row.state) for row in post.joint} == {
        (37.75, "final_locked"),
        (39.25, "warming"),
    }
    assert project_reported_x(post) == pytest.approx({38: 0.5, 39: 0.5})
    assert post.invariant_report[
        "final_locked_reported_x_compatibility_applied_before_normalization"
    ] is True


def test_all_final_locked_mass_collapses_to_observed_reported_x() -> None:
    previous = (
        JointCell(37.75, "final_locked", 0.3),
        JointCell(39.25, "final_locked", 0.7),
    )
    forcing = tuple(
        CandidateForcingSlice(
            y_c,
            (ScenarioTransition(f"scenario-{y_c}", 1.0, _identity()),),
        )
        for y_c in (37.75, 39.25)
    )
    prediction = predict_to_event(
        previous_joint=previous,
        previous_checkpoint_sha256="checkpoint-locked",
        previous_reported_regular_max_c=38,
        previous_y_lower_bound_c=37.5,
        duration_clocks_before=DurationClocks(),
        event_id="metar-after-lock",
        event_valid_at="2026-07-16T15:30:00Z",
        delta_minutes=30.0,
        forcing_generation_sha256="forcing-a",
        transition_artifact_sha256="transition-a",
        candidate_forcing=forcing,
    )
    post = update_with_observation(
        prediction=prediction,
        emission=CanonicalObservationEmission(
            event_id=prediction.event_id,
            pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
            event_valid_at=prediction.event_valid_at,
            is_regular_metar=True,
            reported_temperature_c=29.0,
            cloud_suppressed=False,
            precipitation_present=False,
            cb_present=True,
            log_emission_by_cell={(row.y_c, row.state): 0.0 for row in prediction.joint},
            observation_artifact_sha256="emission-a",
        ),
    )

    assert project_reported_x(post) == pytest.approx({38: 1.0})


def test_predict_advances_only_previously_active_duration_clocks() -> None:
    previous, _ = _prediction()
    forcing = tuple(
        CandidateForcingSlice(
            y_c,
            (ScenarioTransition(f"scenario-{y_c}", 1.0, _identity()),),
        )
        for y_c in (34.2, 38.2)
    )
    prediction = predict_to_event(
        previous_joint=previous,
        previous_checkpoint_sha256="checkpoint-active",
        previous_reported_regular_max_c=33,
        previous_y_lower_bound_c=32.5,
        duration_clocks_before=DurationClocks(
            precipitation_minutes=20.0,
            precipitation_active=True,
        ),
        event_id="metar-clock",
        event_valid_at="2026-07-15T08:00:00Z",
        delta_minutes=30.0,
        forcing_generation_sha256="forcing-a",
        transition_artifact_sha256="transition-a",
        candidate_forcing=forcing,
    )

    assert prediction.duration_clocks_before.precipitation_minutes == 50.0
    assert prediction.duration_clocks_before.cloud_suppression_minutes == 0.0


def test_reported_x_projection_uses_integer_report_operator() -> None:
    _, prediction = _prediction()
    scores = {(row.y_c, row.state): 0.0 for row in prediction.joint}
    post = update_with_observation(
        prediction=prediction,
        emission=CanonicalObservationEmission(
            event_id=prediction.event_id,
            pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
            event_valid_at=prediction.event_valid_at,
            is_regular_metar=False,
            reported_temperature_c=None,
            cloud_suppressed=False,
            precipitation_present=False,
            cb_present=False,
            log_emission_by_cell=scores,
            observation_artifact_sha256="emission-a",
        ),
    )

    assert project_reported_x(post) == pytest.approx({34: 0.45, 38: 0.55})


def test_emission_must_bind_exact_pre_event_prediction() -> None:
    _, prediction = _prediction()
    with pytest.raises(ValueError, match="emission_pre_event_binding_mismatch"):
        update_with_observation(
            prediction=prediction,
            emission=CanonicalObservationEmission(
                event_id=prediction.event_id,
                pre_event_prediction_sha256="wrong",
                event_valid_at=prediction.event_valid_at,
                is_regular_metar=False,
                reported_temperature_c=None,
                cloud_suppressed=False,
                precipitation_present=False,
                cb_present=False,
                log_emission_by_cell={
                    (row.y_c, row.state): 0.0 for row in prediction.joint
                },
                observation_artifact_sha256="emission-a",
            ),
        )
