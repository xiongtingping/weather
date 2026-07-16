from __future__ import annotations

from trajectory_label_sample_runtime import (
    assign_temperature_incremental_event_day_weights,
    build_temperature_incremental_bayes_training_sample,
    build_temperature_incremental_expanding_year_folds,
)
from resolution_bridge_runtime import build_final_resolution_label


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"
CURRENT_EVENT = "canobs_current_1234567890"
PREVIOUS_EVENT = "canobs_previous_1234567890"


def _event(*, event_id: str = CURRENT_EVENT, state: str = "accepted_primary", event_date: str = "2026-07-03") -> dict:
    return {
        "canonical_observation_event_id": event_id,
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "state": state,
        "valid_time": f"{event_date}T06:30:00Z",
        "first_actionable_seen_at": f"{event_date}T06:31:00Z",
        "parsed_observation": {"temperature_c": 30.0},
    }


def _features() -> dict:
    return {
        "canonical_new_observation": {"temperature_c": 30.0},
        "observation_innovation": {"delta_c": 0.5},
        "trajectory_path_memory": {
            "temporal_role": "pre_event",
            "canonical_sequence_event_ids": [PREVIOUS_EVENT],
            "state": "warming",
        },
        "forecast_evidence": {"gfs": {"state": "available"}},
        "forecast_observation_residuals": {"gfs": {"delta_c": 0.2}},
        "prior_features": {"season": "summer"},
        "observed_max_lower_bound_c": 29.5,
        "time_context": {"solar_hour": 14},
        "quality_missingness_quantization": {"temperature_interval_c": 1.0},
    }


def _coverage(event_date: str = "2026-07-03") -> dict:
    return {
        "event_date": event_date,
        "station_id": "ZUUU",
        "coverage_manifest_sha256": "coverage-immutable-1",
        "coverage_snapshot_persisted": True,
        "replay_eligibility": {"a_class_as_of_replay_eligible": True},
    }


def _label(event_date: str = "2026-07-03") -> dict:
    return build_final_resolution_label(
        target_identity=TARGET,
        event_date=event_date,
        physical_high_label={
            "final_physical_high_c": 32.0,
            "source_id": "physical_truth_zuuu",
            "source_observation_ids": [f"physical-{event_date}"],
            "raw_sha256": "a" * 64,
            "raw_object_ref": f"raw://physical/{event_date}",
            "observed_at": f"{event_date}T18:30:00Z",
            "finality": "final",
        },
        settlement_label={
            "settlement_scalar_c": 32.0,
            "source_id": "official_resolution_zuuu",
            "source_observation_ids": [f"settlement-{event_date}"],
            "raw_sha256": "b" * 64,
            "raw_object_ref": f"raw://settlement/{event_date}",
            "observed_at": f"{event_date}T18:45:00Z",
            "finality": "final",
        },
        resolution_contract={
            "resolution_contract_id": "resolution-v1",
            "resolution_source_id": "official_resolution_zuuu",
            "resolution_local_date": event_date,
            "settlement_unit": "C",
            "physical_to_settlement_kernel": {
                "32": [{"settlement_value_c": 32.0, "probability": 1.0}],
            },
            "bucket_contract": {
                "32C": {"lower_inclusive": 31.5, "upper_exclusive": 32.5},
            },
            "bridge_closed": True,
            "validated": True,
        },
        label_available_at=f"{event_date}T19:00:00Z",
    )


def _sample(*, event_id: str = CURRENT_EVENT, event_date: str = "2026-07-03", **overrides) -> dict:
    event = overrides.pop("canonical_observation_event", _event(event_id=event_id, event_date=event_date))
    values = {
        "target_identity": TARGET,
        "event_date": event_date,
        "canonical_observation_event": event,
        "previous_canonical_event_id": PREVIOUS_EVENT,
        "canonical_sequence_event_ids": [PREVIOUS_EVENT, event_id],
        "prior_context_id": "prior-context-immutable-1",
        "forecast_evidence_snapshot_id": "forecast-snapshot-immutable-1",
        "source_revisions": [
            {
                "source_revision_id": "frev-gfs-1",
                "source_id": "gfs",
                "available_at": f"{event_date}T06:30:30Z",
            }
        ],
        "coverage_manifest": _coverage(event_date),
        "feature_payload": _features(),
        "resolution_label": _label(event_date),
        "feature_builder_version": "feature-builder-v2",
        "label_builder_version": "label-builder-v2",
    }
    values.update(overrides)
    return build_temperature_incremental_bayes_training_sample(**values)


def test_a_class_sample_binds_canonical_event_asof_features_and_final_x_s_b_labels() -> None:
    sample = _sample()

    assert sample["quality_grade"] == "A"
    assert sample["training_eligible"] is True
    assert sample["settlement_evaluation_eligible"] is True
    assert sample["cutoff_as_of"] == "2026-07-03T06:31:00Z"
    assert sample["canonical_sequence_event_ids"] == [PREVIOUS_EVENT, CURRENT_EVENT]
    assert sample["labels"]["final_physical_high_c"] == 32.0
    assert sample["labels"]["settlement_scalar_c"] == 32.0
    assert sample["labels"]["market_settlement_bucket"] == "32C"
    assert "final_physical_high_c" not in sample["features"]
    assert sample["guards"]["final_label_used_as_feature"] is False
    assert sample["day_weight"] is None


def test_confirmation_or_duplicate_cannot_become_trainable_canonical_event_sample() -> None:
    sample = _sample(canonical_observation_event=_event(state="confirmed"))

    assert sample["quality_grade"] == "C"
    assert sample["training_eligible"] is False
    assert "canonical_event_not_accepted" in sample["issues"]


def test_future_forecast_revision_and_label_surface_in_features_fail_closed() -> None:
    features = _features()
    features["forecast_evidence"]["settlement_actual"] = 32.0
    sample = _sample(
        feature_payload=features,
        source_revisions=[
            {
                "source_revision_id": "frev-future",
                "source_id": "gfs",
                "available_at": "2026-07-03T06:32:00Z",
            }
        ],
    )

    assert sample["quality_grade"] == "C"
    assert sample["training_eligible"] is False
    assert "forecast_source_revision_visible_after_cutoff" in sample["issues"]
    assert any(issue.startswith("label_or_future_surface_in_features") for issue in sample["issues"])
    assert sample["labels"]["settlement_scalar_c"] == 32.0


def test_current_event_in_trajectory_feature_is_rejected_as_same_event_double_counting() -> None:
    features = _features()
    features["trajectory_path_memory"]["canonical_sequence_event_ids"] = [PREVIOUS_EVENT, CURRENT_EVENT]
    sample = _sample(feature_payload=features)

    assert sample["quality_grade"] == "C"
    assert sample["training_eligible"] is False
    assert "trajectory_path_memory_contains_current_event" in sample["issues"]


def test_b_class_retains_physical_auxiliary_eligibility_but_not_settlement_promotion() -> None:
    coverage = _coverage()
    coverage["coverage_snapshot_persisted"] = False
    sample = _sample(coverage_manifest=coverage)

    assert sample["quality_grade"] == "B"
    assert sample["training_eligible"] is True
    assert sample["physical_auxiliary_eligible"] is True
    assert sample["settlement_evaluation_eligible"] is False


def test_mutated_resolution_label_cannot_be_used_for_settlement_evaluation() -> None:
    label = _label()
    label["settlement_scalar_c"] = 33.0
    sample = _sample(resolution_label=label)

    assert sample["quality_grade"] == "B"
    assert sample["settlement_evaluation_eligible"] is False
    assert any(issue.startswith("resolution_label_bridge_invalid:resolution_final_label_sha256_mismatch") for issue in sample["issues"])


def test_event_day_weights_and_expanding_year_folds_never_split_a_day_randomly() -> None:
    first = _sample(event_id="canobs_weight_first_123456", event_date="2024-07-03")
    second = _sample(event_id="canobs_weight_second_123456", event_date="2024-07-03")
    third = _sample(event_id="canobs_weight_third_123456", event_date="2025-07-03")
    fourth = _sample(event_id="canobs_weight_fourth_123456", event_date="2026-07-03")
    weighted = assign_temperature_incremental_event_day_weights([first, second, third, fourth])

    assert [row["day_weight"] for row in weighted[:2]] == [0.5, 0.5]
    assert weighted[2]["day_weight"] == 1.0
    folds = build_temperature_incremental_expanding_year_folds(weighted)

    assert [fold["test_year"] for fold in folds["folds"]] == [2025, 2026]
    assert all(fold["event_day_disjoint"] is True for fold in folds["folds"])
    assert folds["random_split"] is False
    assert folds["same_event_day_cross_fold"] is False
