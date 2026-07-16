from __future__ import annotations

import pytest

from strong_observation_lr_artifact_contract import build_strong_observation_lr_input
from strong_observation_lr_artifact_contract import REQUIRED_FEATURE_IDS
from strong_observation_lr_evaluator import evaluate_strong_observation_raw_log_scores
from temperature_trajectory_feature_runtime import build_temperature_trajectory_feature_state
from tests.strong_observation_artifact_fixtures import approved_strong_artifact


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"


def _event(index: int, temperature_c: float, *, cloud_text: str = "FEW020", weather_text: str = "") -> dict:
    minute = index * 30
    hour = 6 + minute // 60
    minute %= 60
    return {
        "canonical_observation_event_id": f"canobs_eval_{index}_1234567890",
        "canonical_observation_key": f"canokey_eval_{index}",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "selected_source_id": "AMSC",
        "state": "accepted_primary",
        "valid_time": f"2026-07-03T{hour:02d}:{minute:02d}:00Z",
        "first_actionable_seen_at": f"2026-07-03T{hour:02d}:{minute + 1:02d}:00Z",
        "parsed_observation": {
            "temperature_c": temperature_c,
            "dewpoint_c": 22.0,
            "cloud_text": cloud_text,
            "weather_text": weather_text,
        },
    }


def _input() -> dict:
    events = [_event(0, 28.0), _event(1, 29.0), _event(2, 30.0)]
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=events[:-1],
        decision_as_of="2026-07-03T07:30:00Z",
        temporal_role="pre_event",
    )
    assert trajectory["ok"] is True
    result = build_strong_observation_lr_input(
        canonical_observation_event=events[-1],
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-eval",
        prior_context_id="prior-eval",
        source_reliability_pre_state={"AMSC": {"state": "verified"}},
    )
    assert result["ok"] is True
    return result


def _cooling_input() -> dict:
    events = [_event(0, 32.0), _event(1, 31.0), _event(2, 30.0, cloud_text="BKN020", weather_text="RA")]
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=events[:-1],
        decision_as_of="2026-07-03T07:30:00Z",
        temporal_role="pre_event",
    )
    result = build_strong_observation_lr_input(
        canonical_observation_event=events[-1],
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-cooling",
        prior_context_id="prior-cooling",
        source_reliability_pre_state={"AMSC": {"state": "verified"}},
    )
    assert result["ok"] is True
    return result


def test_evaluator_consumes_temperature_trajectory_physics_and_pre_event_source_state() -> None:
    artifact = approved_strong_artifact(
        model_artifact_sha256="strong-evaluator-artifact",
        default_raw_log_score=0.0,
    )
    definitions = artifact["feature_evaluator"]["feature_definitions"]
    definitions["trajectory.current_step_rate_c_per_hour"]["value_score_adjustments"]["rapid_warming"][
        "raw_log_score_adjustment_by_temperature_and_pre_state"
    ] = {"29|warming": -0.2, "30|warming": 0.2, "default": 0.0}
    definitions["physical.cloud_regime"]["value_score_adjustments"]["clear_or_few"][
        "raw_log_score_adjustment_by_temperature_and_pre_state"
    ] = {"29|warming": -0.1, "30|warming": 0.1, "default": 0.0}
    definitions["source_reliability.selected_source_state"]["value_score_adjustments"]["verified"][
        "raw_log_score_adjustment_by_temperature_and_pre_state"
    ] = {"29|warming": -0.05, "30|warming": 0.05, "default": 0.0}
    result = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_input(),
        posterior_cells=[
            {"temperature_c": 29.0, "state": "warming"},
            {"temperature_c": 30.0, "state": "warming"},
        ],
    )

    assert result["ok"] is True
    assert result["raw_log_score"][1]["feature_values"]["trajectory.current_step_rate_c_per_hour"] == 2.0
    assert result["raw_log_score"][1]["feature_values"]["timing.local_hour"] == 15.0
    assert result["raw_log_score"][0]["feature_values"]["candidate.remaining_warming_c"] == -1.0
    assert result["raw_log_score"][1]["feature_values"]["candidate.remaining_warming_c"] == 0.0
    assert result["raw_log_score"][1]["feature_categories"]["physical.cloud_regime"] == "clear_or_few"
    assert result["raw_log_score"][1]["feature_categories"]["source_reliability.selected_source_state"] == "verified"
    assert result["raw_log_score"][0]["raw_log_score"] == pytest.approx(-0.35)
    assert result["raw_log_score"][1]["raw_log_score"] == pytest.approx(0.35)
    assert {entry["feature_id"] for entry in result["raw_log_score"][0]["feature_contributions"]} == set(REQUIRED_FEATURE_IDS)


def test_evaluator_fails_closed_when_a_required_feature_lacks_its_actual_bin_score() -> None:
    artifact = approved_strong_artifact(
        model_artifact_sha256="strong-evaluator-missing-bin",
        default_raw_log_score=0.0,
    )
    artifact["feature_evaluator"]["feature_definitions"]["physical.cloud_regime"]["value_score_adjustments"][
        "clear_or_few"
    ]["raw_log_score_adjustment_by_temperature_and_pre_state"] = {"29|warming": -0.1}
    result = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_input(),
        posterior_cells=[{"temperature_c": 30.0, "state": "warming"}],
    )

    assert result["ok"] is False
    assert "strong_lr_evaluator_feature_score_missing:physical.cloud_regime" in result["block_reasons"]


def test_current_report_cooling_and_candidate_remaining_warming_are_cell_specific() -> None:
    artifact = approved_strong_artifact(default_raw_log_score=0.0)
    definitions = artifact["feature_evaluator"]["feature_definitions"]
    definitions["trajectory.current_step_state"]["value_score_adjustments"]["cooling"][
        "raw_log_score_adjustment_by_temperature_and_pre_state"
    ] = {"32|warming": 0.2, "35|warming": -0.2, "default": 0.0}
    definitions["candidate.remaining_warming_c"]["value_score_adjustments"]["at_observed_peak"][
        "raw_log_score_adjustment_by_temperature_and_pre_state"
    ] = {"32|warming": 0.5, "default": 0.0}
    definitions["candidate.remaining_warming_c"]["value_score_adjustments"]["one_to_three_degrees"][
        "raw_log_score_adjustment_by_temperature_and_pre_state"
    ] = {"35|warming": -0.5, "default": 0.0}

    result = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_cooling_input(),
        posterior_cells=[
            {"temperature_c": 32.0, "state": "warming"},
            {"temperature_c": 35.0, "state": "warming"},
        ],
    )

    assert result["ok"] is True
    lower, upper = result["raw_log_score"]
    assert lower["feature_values"]["trajectory.current_step_rate_c_per_hour"] == -2.0
    assert lower["feature_values"]["trajectory.cooling_depth_including_current_c"] == 2.0
    assert lower["feature_values"]["trajectory.cooling_duration_minutes"] == 60.0
    assert lower["feature_values"]["candidate.remaining_warming_c"] == 0.0
    assert upper["feature_values"]["candidate.remaining_warming_c"] == 3.0
    assert lower["raw_log_score"] == pytest.approx(0.7)
    assert upper["raw_log_score"] == pytest.approx(-0.7)
