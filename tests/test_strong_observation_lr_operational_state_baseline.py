from __future__ import annotations

from pathlib import Path

import pytest

from strong_observation_lr_artifact_contract import (
    build_strong_observation_lr_input,
    validate_strong_observation_lr_runtime_artifact,
)
from strong_observation_lr_evaluator import evaluate_strong_observation_raw_log_scores
from strong_observation_lr_operational_state_baseline import (
    SCORING_KIND,
    build_operational_state_aware_strong_lr_baseline,
)
from temperature_trajectory_feature_runtime import build_temperature_trajectory_feature_state


TARGET = "physical_weather_day_v1:ZUUU:2026-07-14"


def _event(index: int, temperature: float, *, cloud: str = "FEW030", weather: str = "") -> dict:
    valid = f"2026-07-14T{3 + index:02d}:00:00Z"
    return {
        "canonical_observation_event_id": f"canobs_prod_state_{index}",
        "canonical_observation_key": f"canokey_prod_state_{index}",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "selected_source_id": "zuuu_metar_raw_amsc",
        "state": "accepted_primary",
        "valid_time": valid,
        "first_actionable_seen_at": valid,
        "parsed_observation": {
            "station_id": "ZUUU",
            "valid_time": valid,
            "temperature_c": temperature,
            "dewpoint_c": temperature - 8.0,
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
    return build_strong_observation_lr_input(
        canonical_observation_event=events[-1],
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-production-state-v2",
        prior_context_id="prior-production-state-v2",
        source_reliability_pre_state={"zuuu_metar_raw_amsc": {"state": "verified"}},
    )


def _score(result: dict, state: str) -> float:
    return next(float(row["raw_log_score"]) for row in result["raw_log_score"] if row["pre_state"] == state)


def _first_cb_input(temperatures: list[float]) -> dict:
    events = [_event(index, value) for index, value in enumerate(temperatures)]
    events[-1]["parsed_observation"]["cloud_text"] = "FEW033CB FEW040"
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=events[:-1],
        decision_as_of=events[-1]["valid_time"],
        temporal_role="pre_event",
    )
    return build_strong_observation_lr_input(
        canonical_observation_event=events[-1],
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-first-cb-v2",
        prior_context_id="prior-first-cb-v2",
        source_reliability_pre_state={"zuuu_metar_raw_amsc": {"state": "verified"}},
    )


def _already_seen_cb_input(temperatures: list[float]) -> dict:
    events = [_event(index, value) for index, value in enumerate(temperatures)]
    events[-2]["parsed_observation"]["cloud_text"] = "FEW033CB FEW040"
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=events[:-1],
        decision_as_of=events[-1]["valid_time"],
        temporal_role="pre_event",
    )
    return build_strong_observation_lr_input(
        canonical_observation_event=events[-1],
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-cb-already-seen-v2",
        prior_context_id="prior-cb-already-seen-v2",
        source_reliability_pre_state={"zuuu_metar_raw_amsc": {"state": "verified"}},
    )


def test_state_aware_baseline_is_approved_content_addressed_and_complete() -> None:
    artifact = build_operational_state_aware_strong_lr_baseline()
    validation = validate_strong_observation_lr_runtime_artifact(artifact)
    assert validation["ok"] is True
    assert artifact["approval_state"] == "approved"
    assert artifact["production_runtime_eligible"] is True
    assert artifact["shadow_only"] is False
    assert artifact["scoring_kind"] == SCORING_KIND
    assert artifact["artifact_sha256"] == artifact["artifact_content_sha256"]
    assert len(artifact["operational_state_model"]["state_profiles"]) == 8


def test_unique_evaluator_jointly_scores_temperature_and_latent_state() -> None:
    artifact = build_operational_state_aware_strong_lr_baseline()
    result = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_input([31.0, 32.0, 33.0]),
        posterior_cells=[
            {"temperature_c": 35.0, "state": state, "probability": 0.125}
            for state in artifact["operational_state_model"]["state_profiles"]
        ],
    )
    assert result["ok"] is True
    assert result["scoring_kind"] == SCORING_KIND
    assert result["state_discriminating"] is True
    assert _score(result, "warming") > _score(result, "final_locked")
    assert _score(result, "slowing_warming") > _score(result, "terminal_cooling")
    transition = result["post_state_transition"]
    assert transition
    assert all(sum(row.values()) == pytest.approx(1.0) for row in transition.values())
    assert any(row != {state: 1.0} for state, row in transition.items())


def test_cooling_observation_dynamically_moves_post_state_away_from_warming() -> None:
    artifact = build_operational_state_aware_strong_lr_baseline()
    states = list(artifact["operational_state_model"]["state_profiles"])
    result = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_input([38.0, 37.0, 36.0], cloud="BKN020", weather="RA"),
        posterior_cells=[
            {"temperature_c": 38.0, "state": state, "probability": 1.0 / len(states)}
            for state in states
        ],
    )
    assert result["ok"] is True
    target = result["post_state_transition_trace"]["target_post_state_distribution"]
    assert target["temporary_cooling"] + target["terminal_cooling"] > target["warming"]
    assert result["post_state_transition_trace"]["physical_x_mass_unchanged_by_contract"] is True


def test_extreme_late_cooling_soft_cap_preserves_state_discrimination() -> None:
    artifact = build_operational_state_aware_strong_lr_baseline()
    states = list(artifact["operational_state_model"]["state_profiles"])
    result = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_input([38.0, 35.0, 29.0]),
        posterior_cells=[
            {"temperature_c": temperature, "state": state, "probability": 1.0 / (2 * len(states))}
            for temperature in (37.5, 38.0)
            for state in states
        ],
    )
    assert result["ok"] is True
    assert result["state_discriminating"] is True
    by_temperature: dict[float, set[float]] = {}
    for row in result["raw_log_score"]:
        by_temperature.setdefault(float(row["temperature_c"]), set()).add(
            round(float(row["state_log_score_adjustment"]), 12)
        )
        assert row["state_model_trace"]["relative_score_transform"] == "global_best_zero_soft_cap_v1"
    assert all(len(values) > 1 for values in by_temperature.values())


def test_first_cb_timing_only_penalizes_unrealized_remaining_warming() -> None:
    artifact = build_operational_state_aware_strong_lr_baseline()
    early = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_first_cb_input([37.0, 38.0]),  # 12:00 local
        posterior_cells=[
            {"temperature_c": 38.0, "state": "warming", "probability": 0.5},
            {"temperature_c": 40.0, "state": "warming", "probability": 0.5},
        ],
    )
    late = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_first_cb_input([35.0, 36.0, 37.0, 38.0, 38.0, 38.0, 38.0]),  # 17:00 local
        posterior_cells=[
            {"temperature_c": 38.0, "state": "warming", "probability": 0.5},
            {"temperature_c": 40.0, "state": "warming", "probability": 0.5},
        ],
    )

    assert early["ok"] is True and late["ok"] is True
    early_rows = {row["temperature_c"]: row for row in early["raw_log_score"]}
    late_rows = {row["temperature_c"]: row for row in late["raw_log_score"]}
    assert early_rows[38.0]["operational_physics_trace"]["first_cb_remaining_warming_penalty"] == 0.0
    assert late_rows[38.0]["operational_physics_trace"]["first_cb_remaining_warming_penalty"] == 0.0
    assert early_rows[40.0]["operational_physics_trace"]["first_cb_remaining_warming_penalty"] < late_rows[40.0]["operational_physics_trace"]["first_cb_remaining_warming_penalty"] < 0.0
    assert late_rows[40.0]["feature_values"]["timing.first_cb_local_hour"] == 17.0
    assert late_rows[40.0]["feature_categories"]["physical.first_cb_event_state"] == "first_cb_this_event"
    assert late_rows[40.0]["feature_categories"]["physical.first_cb_timing_phase"] == "late_after_peak_window"
    assert late_rows[40.0]["feature_categories"]["physical.first_cb_peak_relation"] == "at_observed_peak"


def test_first_cb_direct_likelihood_penalty_is_consumed_exactly_once() -> None:
    artifact = build_operational_state_aware_strong_lr_baseline()
    result = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_already_seen_cb_input([35.0, 36.0, 37.0]),
        posterior_cells=[
            {"temperature_c": 37.0, "state": "warming", "probability": 0.5},
            {"temperature_c": 40.0, "state": "warming", "probability": 0.5},
        ],
    )
    rows = {row["temperature_c"]: row for row in result["raw_log_score"]}
    assert rows[40.0]["feature_categories"]["physical.first_cb_event_state"] == "cb_already_seen"
    assert rows[40.0]["operational_physics_trace"]["first_cb_remaining_warming_penalty"] == 0.0
    assert rows[40.0]["operational_physics_trace"]["first_cb_direct_penalty_consumed_this_event"] is False


def test_preheating_observation_is_neutral_for_final_x_likelihood() -> None:
    events = [_event(0, 27.0), _event(1, 26.0)]
    for event, valid in zip(events, ("2026-07-13T22:30:00Z", "2026-07-13T23:00:00Z")):
        event["valid_time"] = valid
        event["first_actionable_seen_at"] = valid
        event["parsed_observation"]["valid_time"] = valid
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=events[:-1],
        decision_as_of=events[-1]["valid_time"],
        temporal_role="pre_event",
    )
    strong_input = build_strong_observation_lr_input(
        canonical_observation_event=events[-1],
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-preheating-v3",
        prior_context_id="prior-preheating-v3",
        source_reliability_pre_state={"zuuu_metar_raw_amsc": {"state": "verified"}},
    )
    result = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=build_operational_state_aware_strong_lr_baseline(),
        strong_observation_input=strong_input,
        posterior_cells=[
            {"temperature_c": 33.0, "state": "warming", "probability": 0.5},
            {"temperature_c": 38.0, "state": "warming", "probability": 0.5},
        ],
    )

    assert result["ok"] is True
    assert all(row["operational_physics_trace"]["preheating_is_x_likelihood_neutral"] is True for row in result["raw_log_score"])
    assert all(row["operational_physics_trace"]["diurnal_information_multiplier"] == 0.0 for row in result["raw_log_score"])


def test_production_p0_and_event_runtime_have_no_v1_builder_fallback() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in ("production_probability_p0_runtime.py", "production_sequential_bayes_event_runtime.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "build_operational_state_aware_strong_lr_baseline" in source
        assert "build_operational_physics_strong_lr_baseline" not in source
