from __future__ import annotations

from datetime import date, timedelta

from joint_forecast_weak_likelihood_runtime import validate_joint_forecast_weak_artifact
from joint_forecast_weak_likelihood_training_runtime import (
    build_joint_weak_training_dataset,
    train_joint_weak_source_set_artifact,
)
from joint_forecast_weak_likelihood_oos_runtime import evaluate_joint_weak_expanding_window, evaluate_joint_weak_walk_forward


CATALOG = {
    "ecmwf": {"provider_family": "nwp"},
    "icon": {"provider_family": "nwp"},
    "gfs": {"provider_family": "nwp"},
    "taf": {"provider_family": "taf"},
}


def _event(index: int, sources: list[str], *, bad_tier: bool = False) -> dict:
    day = date(2025, 5, 1) + timedelta(days=index)
    target = 30.0 + (index % 5) * 0.2
    return {
        "event_id": f"event-{index}-{'+'.join(sources)}",
        "event_date": day.isoformat(),
        "decision_as_of": f"{day.isoformat()}T06:30:00Z",
        "forecast_context": {"season": "summer", "weather_regime": "clear", "lead_bucket": "0_6h"},
        "physical_x_final": {"label_kind": "physical_x_final", "final": True, "temperature_c": target, "raw_sha256": f"x-{index}-{'+'.join(sources)}"},
        "source_revisions": [
            {
                "source_id": source,
                "source_revision_id": f"{source}-{index}",
                "source_class": "forecast_weak_source",
                "strict_asof_tier_a": not bad_tier,
                "response_headers_observed": not bad_tier,
                "available_at": f"{day.isoformat()}T06:00:00Z",
                "forecast_peak_c": target - 0.2 + (0.1 if source == "taf" else 0.0),
                "raw_sha256": f"raw-{source}-{index}",
            }
            for source in sources
        ],
    }


def test_dataset_and_trainer_fit_independent_exact_source_set_surfaces() -> None:
    events = [_event(index, ["ecmwf", "icon", "gfs"]) for index in range(30)]
    events += [_event(index + 40, ["gfs", "taf"]) for index in range(30)]

    dataset = build_joint_weak_training_dataset(events=events, source_catalog=CATALOG)
    report = train_joint_weak_source_set_artifact(
        dataset=dataset, minimum_event_days_per_surface=30, minimum_pair_samples=20
    )

    assert dataset["state"] == "available"
    assert dataset["manifest"]["sample_count"] == 60
    assert report["state"] == "trained_shadow_only"
    artifact = report["artifact"]
    surfaces = artifact["source_set_surfaces"]["season=summer|weather_regime=clear|lead_bucket=0_6h"]
    assert set(surfaces) == {"ecmwf+icon+gfs", "gfs+taf", "empty"}
    assert surfaces["ecmwf+icon+gfs"]["missing_source_ids"] == ["jma", "taf", "wu", "twc"]
    assert surfaces["gfs+taf"]["source_error_calibration"]["taf"]["sample_count"] == 30
    assert surfaces["empty"]["joint_information_scale"] == 0.0
    assert report["promotion"]["live_amsc_ingest_blocked"] is False
    assert "joint_weak_artifact_not_approved" in validate_joint_forecast_weak_artifact(artifact)["issues"]


def test_source_set_error_scale_is_fitted_after_context_bias_correction() -> None:
    events = []
    for index in range(60):
        event = _event(index, ["gfs"])
        target = event["physical_x_final"]["temperature_c"]
        if index < 30:
            event["decision_as_of"] = f"{event['event_date']}T02:30:00Z"  # local 10:30
            event["source_revisions"][0]["available_at"] = f"{event['event_date']}T02:00:00Z"
            event["source_revisions"][0]["forecast_peak_c"] = target + 1.0
        else:
            event["decision_as_of"] = f"{event['event_date']}T06:30:00Z"  # local 14:30
            event["source_revisions"][0]["available_at"] = f"{event['event_date']}T06:00:00Z"
            event["source_revisions"][0]["forecast_peak_c"] = target - 1.0
        events.append(event)

    dataset = build_joint_weak_training_dataset(events=events, source_catalog=CATALOG)
    report = train_joint_weak_source_set_artifact(
        dataset=dataset,
        minimum_event_days_per_surface=30,
        minimum_pair_samples=20,
    )

    surface = report["artifact"]["source_set_surfaces"][
        "season=summer|weather_regime=clear|lead_bucket=0_6h"
    ]["gfs"]
    calibration = surface["source_error_calibration"]["gfs"]
    assert calibration["error_sigma_c"] < 0.5
    assert calibration["error_residual_definition"] == (
        "forecast_peak_c_minus_selected_hierarchical_bias_c_minus_physical_x_final_c"
    )


def test_training_quarantines_non_tier_a_or_market_contaminated_revisions() -> None:
    bad_tier = _event(1, ["gfs"], bad_tier=True)
    contaminated = _event(2, ["taf"])
    contaminated["source_revisions"][0]["market_price"] = 0.55

    dataset = build_joint_weak_training_dataset(events=[bad_tier, contaminated], source_catalog=CATALOG)

    assert dataset["state"] == "blocked"
    assert dataset["manifest"]["excluded_counts"]["no_strict_asof_forecasts_at_cutoff"] == 2


def test_walk_forward_oos_uses_label_only_after_weak_likelihood() -> None:
    events = [_event(index, ["gfs", "taf"]) for index in range(30)]
    events += [_event(index + 31, ["gfs", "taf"]) for index in range(5)]
    dataset = build_joint_weak_training_dataset(events=events, source_catalog=CATALOG)

    result = evaluate_joint_weak_walk_forward(
        dataset=dataset,
        evaluation_start_date="2025-05-31",
        minimum_event_days_per_surface=30,
        minimum_pair_samples=20,
    )

    assert result["state"] == "evaluated_shadow_only"
    assert result["sample_count"] == 5
    assert result["boundary"]["physical_x_label_used_only_after_likelihood"] is True
    assert result["boundary"]["strict_asof_oos_runtime_gate"] is False
    assert result["bias_alpha_selection"]["comparison_complete"] is True
    assert len(result["bias_alpha_comparison"]) == 2


def test_walk_forward_marks_nonwinning_alpha_without_changing_live_gate() -> None:
    events = [_event(index, ["gfs", "taf"]) for index in range(30)]
    events += [_event(index + 31, ["gfs", "taf"]) for index in range(5)]
    dataset = build_joint_weak_training_dataset(events=events, source_catalog=CATALOG)

    result = evaluate_joint_weak_walk_forward(
        dataset=dataset,
        evaluation_start_date="2025-05-31",
        minimum_event_days_per_surface=30,
        minimum_pair_samples=20,
        candidate_bias_ewma_alpha=0.05,
    )

    assert result["state"] == "evaluated_shadow_only"
    assert result["bias_alpha_selection"]["selected_bias_ewma_alpha"] is None
    assert result["bias_alpha_selection"]["candidate_is_selected"] is False
    assert result["oos_safety"]["state"] == "blocked"


def test_expanding_window_retrains_strictly_before_each_origin_and_never_becomes_promotion_evidence() -> None:
    events = [_event(index, ["gfs", "taf"]) for index in range(30)]
    events += [_event(index + 31, ["gfs", "taf"]) for index in range(5)]
    dataset = build_joint_weak_training_dataset(events=events, source_catalog=CATALOG)

    result = evaluate_joint_weak_expanding_window(
        dataset=dataset,
        evaluation_start_date="2025-05-31",
        minimum_event_days_per_surface=30,
        minimum_pair_samples=20,
    )

    assert result["state"] == "evaluated_research_only"
    assert result["evaluation_mode"] == "expanding_window_rolling_origin"
    assert result["fold_count"] == 5
    assert [fold["training_event_day_count"] for fold in result["folds"]] == [30, 31, 32, 33, 34]
    assert result["promotion_eligible"] is False
    assert result["boundary"]["strict_time_order_per_fold"] is True
    assert result["boundary"]["strict_asof_oos_runtime_gate"] is False
