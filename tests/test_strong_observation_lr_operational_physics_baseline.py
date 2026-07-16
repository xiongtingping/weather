from __future__ import annotations

from strong_observation_lr_artifact_contract import build_strong_observation_lr_input
from strong_observation_lr_evaluator import evaluate_strong_observation_raw_log_scores
from strong_observation_lr_operational_physics_baseline import build_operational_physics_strong_lr_baseline
from temperature_trajectory_feature_runtime import build_empty_pre_event_temperature_trajectory_feature_state


def _input(*, temperature: float, hour_iso: str, cloud: str = "CAVOK", weather: str = "") -> dict:
    event = {
        "canonical_observation_event_id": "canobs_operational_physics",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-14",
        "station_id": "ZUUU", "state": "accepted_primary",
        "selected_source_id": "zuuu_metar_raw_amsc", "report_type": "METAR",
        "valid_time": hour_iso,
        "parsed_observation": {"station_id": "ZUUU", "valid_time": hour_iso, "temperature_c": temperature, "dewpoint_c": temperature - 8.0, "cloud_text": cloud, "weather_text": weather},
    }
    trajectory = build_empty_pre_event_temperature_trajectory_feature_state(
        target_identity=event["target_identity"], station_id="ZUUU", decision_as_of=hour_iso,
    )
    return build_strong_observation_lr_input(
        canonical_observation_event=event, trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-operational", prior_context_id="prior-operational",
        source_reliability_pre_state={"selected_source_state": "verified"},
    )


def test_operational_physics_baseline_is_production_eligible_and_low_information() -> None:
    artifact = build_operational_physics_strong_lr_baseline()
    assert artifact["production_runtime_eligible"] is True
    assert artifact["shadow_only"] is False
    assert artifact["baseline_contract"]["strict_asof_oos_is_replacement_promotion_only"] is True

    evaluated = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_input(temperature=34.0, hour_iso="2026-07-14T03:00:00Z"),
        posterior_cells=[
            {"temperature_c": 34.0, "state": "warming", "probability": 0.5},
            {"temperature_c": 38.0, "state": "warming", "probability": 0.5},
        ],
    )
    assert evaluated["ok"] is True
    lower, higher = evaluated["raw_log_score"]
    assert -2.5 <= higher["raw_log_score"] <= 0.0
    assert lower["operational_physics_trace"]["candidate_remaining_warming_c"] == 0.0
    assert higher["operational_physics_trace"]["candidate_remaining_warming_c"] == 4.0
    assert higher["raw_log_score"] < lower["raw_log_score"]


def test_rain_and_late_day_penalize_unreached_high_candidate_more() -> None:
    artifact = build_operational_physics_strong_lr_baseline()
    clear = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_input(temperature=34.0, hour_iso="2026-07-14T05:00:00Z"),
        posterior_cells=[{"temperature_c": 38.0, "state": "warming", "probability": 1.0}],
    )["raw_log_score"][0]["raw_log_score"]
    rain = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=_input(temperature=34.0, hour_iso="2026-07-14T08:00:00Z", cloud="BKN020", weather="RA"),
        posterior_cells=[{"temperature_c": 38.0, "state": "warming", "probability": 1.0}],
    )["raw_log_score"][0]["raw_log_score"]
    assert rain < clear
