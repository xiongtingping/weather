from __future__ import annotations

from strong_observation_training_dataset_runtime import build_strong_observation_training_dataset
from strong_observation_training_feature_runtime import (
    TRAINING_SCOPE_PHYSICAL_X,
    build_strong_observation_training_feature,
)
from temperature_trajectory_feature_runtime import build_temperature_trajectory_feature_state
from tests.test_temperature_incremental_bayes_training_samples import PREVIOUS_EVENT, TARGET, _features, _sample


def _event(*, event_id: str, event_date: str) -> dict:
    return {
        "canonical_observation_event_id": event_id,
        "canonical_observation_key": f"key-{event_id}",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "selected_source_id": "AMSC",
        "state": "accepted_primary",
        "valid_time": f"{event_date}T06:30:00Z",
        "first_actionable_seen_at": f"{event_date}T06:31:00Z",
        "parsed_observation": {
            "temperature_c": 30.0,
            "dewpoint_c": 22.0,
            "cloud_text": "FEW020",
            "weather_text": "RA",
        },
    }


def _previous_event(event_date: str) -> dict:
    return {
        "canonical_observation_event_id": PREVIOUS_EVENT,
        "canonical_observation_key": f"key-{PREVIOUS_EVENT}-{event_date}",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "state": "accepted_primary",
        "valid_time": f"{event_date}T06:00:00Z",
        "first_actionable_seen_at": f"{event_date}T06:01:00Z",
        "parsed_observation": {"temperature_c": 29.0},
    }


def _sample_and_feature(*, event_id: str = "canobs_feature_123456", event_date: str = "2026-07-03") -> tuple[dict, dict, dict]:
    event = _event(event_id=event_id, event_date=event_date)
    sample_features = _features()
    sample_features["trajectory_path_memory"] = {
        **sample_features["trajectory_path_memory"],
        "canonical_sequence_event_ids": [PREVIOUS_EVENT],
    }
    sample = _sample(
        event_id=event_id,
        event_date=event_date,
        canonical_observation_event=event,
        canonical_sequence_event_ids=[PREVIOUS_EVENT, event_id],
        previous_canonical_event_id=PREVIOUS_EVENT,
        feature_payload=sample_features,
    )
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=[_previous_event(event_date)],
        decision_as_of=event["first_actionable_seen_at"],
        temporal_role="pre_event",
    )
    feature = build_strong_observation_training_feature(
        training_sample=sample,
        canonical_observation_event=event,
        pre_event_trajectory_feature_state=trajectory,
        source_reliability_pre_state={"AMSC": {"state": "unknown"}},
    )
    return sample, feature, trajectory


def test_training_features_are_built_by_the_runtime_owner_and_exclude_final_labels() -> None:
    sample, feature, _trajectory = _sample_and_feature()

    assert feature["ok"] is True
    assert feature["training_sample_id"] == sample["training_sample_id"]
    assert feature["feature_values"] == {
        "canonical.temperature_c": 30.0,
        "timing.local_hour": 14.5,
        "timing.first_cb_local_hour": None,
        "trajectory.pre_event_path_state": "mixed_or_insufficient",
        "trajectory.current_step_state": "warming",
        "trajectory.current_step_rate_c_per_hour": 2.0,
        "trajectory.slope_60m_c_per_hour": None,
        "trajectory.slope_120m_c_per_hour": None,
        "trajectory.acceleration_c_per_hour_squared": None,
        "trajectory.observed_peak_including_current_c": 30.0,
        "trajectory.cooling_depth_including_current_c": 0.0,
        "trajectory.cooling_duration_minutes": 0.0,
        "candidate.remaining_warming_c": None,
        "physical.dewpoint_depression_c": 8.0,
        "physical.cloud_regime": "clear_or_few",
        "physical.first_cb_event_state": "no_cb_yet",
        "physical.first_cb_timing_phase": "no_cb_yet",
        "physical.first_cb_peak_relation": "no_cb_yet",
        "physical.weather_regime": "precipitation",
        "source_reliability.selected_source_state": "unknown",
    }
    assert "labels" not in feature
    assert feature["boundary"]["final_label_not_in_feature_object"] is True
    assert feature["boundary"]["trajectory_is_pre_event_only"] is True


def test_training_feature_rejects_same_event_trajectory_and_non_a_sample() -> None:
    sample, _feature, trajectory = _sample_and_feature()
    event = _event(event_id=sample["canonical_observation_event_id"], event_date=sample["event_date"])
    contaminated_trajectory = dict(trajectory)
    contaminated_trajectory["canonical_sequence_event_ids"] = [PREVIOUS_EVENT, event["canonical_observation_event_id"]]
    same_event = build_strong_observation_training_feature(
        training_sample=sample,
        canonical_observation_event=event,
        pre_event_trajectory_feature_state=contaminated_trajectory,
        source_reliability_pre_state={"AMSC": "unknown"},
    )
    b_sample = dict(sample)
    b_sample["quality_grade"] = "B"
    b_sample["settlement_evaluation_eligible"] = False
    physical_only = build_strong_observation_training_feature(
        training_sample=b_sample,
        canonical_observation_event=event,
        pre_event_trajectory_feature_state=trajectory,
        source_reliability_pre_state={"AMSC": "unknown"},
    )

    assert same_event["ok"] is False
    assert "strong_training_feature_pre_event_trajectory_sequence_mismatch" in same_event["block_reasons"]
    assert physical_only["ok"] is False
    assert "strong_training_feature_sample_not_a_class_settlement_eligible" in physical_only["block_reasons"]


def test_physical_x_scope_accepts_b_class_sample_without_settlement_or_bridge() -> None:
    sample, _feature, trajectory = _sample_and_feature()
    event = _event(event_id=sample["canonical_observation_event_id"], event_date=sample["event_date"])
    physical_only = dict(sample)
    physical_only["quality_grade"] = "B"
    physical_only["settlement_evaluation_eligible"] = False
    physical_only["physical_auxiliary_eligible"] = True
    physical_only["labels"] = {
        "final_physical_high_c": sample["labels"]["final_physical_high_c"],
        "strict_asof_training_eligible": True,
    }
    feature = build_strong_observation_training_feature(
        training_sample=physical_only,
        canonical_observation_event=event,
        pre_event_trajectory_feature_state=trajectory,
        source_reliability_pre_state={"AMSC": "unknown"},
        training_scope=TRAINING_SCOPE_PHYSICAL_X,
    )
    dataset = build_strong_observation_training_dataset(
        training_samples=[physical_only],
        training_features=[feature],
        training_scope=TRAINING_SCOPE_PHYSICAL_X,
    )

    assert feature["ok"] is True
    assert feature["boundary"]["physical_x_scope_does_not_require_settlement_or_bridge"] is True
    assert dataset["ok"] is True
    assert dataset["rows"][0]["labels"] == {
        "final_physical_high_c": sample["labels"]["final_physical_high_c"],
        "strict_asof_training_eligible": True,
    }
    assert dataset["boundary"]["physical_x_scope_does_not_require_settlement_or_bridge"] is True


def test_dataset_freezes_only_matched_a_class_rows_with_event_day_expanding_folds() -> None:
    rows = [
        _sample_and_feature(event_id=f"canobs_{year}_123456", event_date=f"{year}-07-03")
        for year in (2024, 2025, 2026)
    ]
    dataset = build_strong_observation_training_dataset(
        training_samples=[sample for sample, _feature, _trajectory in rows],
        training_features=[feature for _sample_row, feature, _trajectory in rows],
    )

    assert dataset["ok"] is True
    assert dataset["state"] == "ready_for_oos"
    assert dataset["oos_fold_count"] == 2
    assert [row["day_weight"] for row in dataset["rows"]] == [1.0, 1.0, 1.0]
    assert [fold["test_year"] for fold in dataset["training_manifest"]["fold_manifest"]["folds"]] == [2025, 2026]
    assert dataset["boundary"]["fits_or_activates_model"] is False
    assert dataset["boundary"]["strict_asof_oos_runtime_gate"] is False


def test_dataset_fails_closed_on_missing_or_mismatched_feature_lineage() -> None:
    sample, feature, _trajectory = _sample_and_feature()
    missing = build_strong_observation_training_dataset(training_samples=[sample], training_features=[])
    drift = dict(feature)
    drift["lineage"] = dict(feature["lineage"])
    drift["lineage"]["prior_context_id"] = "drifted-prior"
    mismatched = build_strong_observation_training_dataset(training_samples=[sample], training_features=[drift])

    assert missing["ok"] is False
    assert "strong_training_dataset_features_missing" in missing["block_reasons"]
    assert mismatched["ok"] is False
    assert "strong_training_dataset_feature_sample_lineage_mismatch" in mismatched["block_reasons"]
