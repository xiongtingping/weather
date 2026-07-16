from __future__ import annotations

from strong_observation_lr_artifact_contract import build_strong_observation_lr_input
from strong_observation_lr_operational_state_candidate import (
    build_operational_state_aware_strong_lr_candidate,
    evaluate_operational_state_aware_raw_log_scores,
)
from temperature_trajectory_feature_runtime import build_temperature_trajectory_feature_state


TARGET = "physical_weather_day_v1:ZUUU:2026-07-14"


def _event(index: int, temperature: float, *, cloud: str = "FEW030", weather: str = "") -> dict:
    hour = 3 + index // 2
    minute = 30 if index % 2 else 0
    valid = f"2026-07-14T{hour:02d}:{minute:02d}:00Z"
    return {
        "canonical_observation_event_id": f"canobs_state_{index}",
        "canonical_observation_key": f"canokey_state_{index}",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "selected_source_id": "AMSC",
        "state": "accepted_primary",
        "valid_time": valid,
        "first_actionable_seen_at": valid,
        "parsed_observation": {
            "station_id": "ZUUU",
            "valid_time": valid,
            "temperature_c": temperature,
            "dewpoint_c": temperature - 9.0,
            "cloud_text": cloud,
            "weather_text": weather,
        },
    }


def _input(temperatures: list[float], *, cloud: str = "FEW030", weather: str = "") -> dict:
    events = [_event(index, value, cloud=cloud, weather=weather) for index, value in enumerate(temperatures)]
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=events[:-1],
        decision_as_of=events[-1]["valid_time"],
        temporal_role="pre_event",
    )
    result = build_strong_observation_lr_input(
        canonical_observation_event=events[-1],
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-state-aware",
        prior_context_id="prior-state-aware",
        source_reliability_pre_state={"AMSC": {"state": "verified"}},
    )
    assert result["ok"] is True
    return result


def _score(result: dict, state: str) -> float:
    return next(row["raw_log_score"] for row in result["raw_log_score"] if row["pre_state"] == state)


def test_candidate_is_shadow_only_and_discriminates_warming_state() -> None:
    artifact = build_operational_state_aware_strong_lr_candidate()
    assert artifact["shadow_only"] is True
    assert artifact["production_runtime_eligible"] is False
    states = list(artifact["operational_state_model"]["state_profiles"])
    result = evaluate_operational_state_aware_raw_log_scores(
        candidate_artifact=artifact,
        strong_observation_input=_input([31.0, 32.0, 33.0]),
        posterior_cells=[{"temperature_c": 35.0, "state": state, "probability": 1.0 / len(states)} for state in states],
    )
    assert result["ok"] is True
    assert result["state_discriminating"] is True
    assert _score(result, "warming") > _score(result, "final_locked")
    assert _score(result, "slowing_warming") > _score(result, "terminal_cooling")


def test_late_rain_cooling_supports_suppression_or_terminal_over_warming() -> None:
    artifact = build_operational_state_aware_strong_lr_candidate()
    states = list(artifact["operational_state_model"]["state_profiles"])
    result = evaluate_operational_state_aware_raw_log_scores(
        candidate_artifact=artifact,
        strong_observation_input=_input([36.0, 35.0, 34.0], cloud="BKN020", weather="RA"),
        posterior_cells=[{"temperature_c": 36.0, "state": state, "probability": 1.0 / len(states)} for state in states],
    )
    assert result["ok"] is True
    assert _score(result, "terminal_cooling") > _score(result, "warming")
    assert _score(result, "weather_suppressed") > _score(result, "warming")


def test_same_temperature_cells_receive_distinct_state_likelihoods() -> None:
    artifact = build_operational_state_aware_strong_lr_candidate()
    result = evaluate_operational_state_aware_raw_log_scores(
        candidate_artifact=artifact,
        strong_observation_input=_input([35.0, 35.0, 35.0]),
        posterior_cells=[
            {"temperature_c": 35.0, "state": "temporary_platform", "probability": 0.5},
            {"temperature_c": 35.0, "state": "warming", "probability": 0.5},
        ],
    )
    assert result["ok"] is True
    first, second = result["raw_log_score"]
    assert first["temperature_only_raw_log_score"] == second["temperature_only_raw_log_score"]
    assert first["raw_log_score"] != second["raw_log_score"]
