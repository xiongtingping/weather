from __future__ import annotations

from station_day_strong_physical_training_runtime import build_station_day_strong_physical_training_sample
from strong_observation_training_feature_runtime import TRAINING_SCOPE_PHYSICAL_X, build_strong_observation_training_feature


def _event(event_id: str = "canobs-first") -> dict:
    return {
        "canonical_observation_event_id": event_id,
        "canonical_observation_key": f"key-{event_id}",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-12",
        "station_id": "ZUUU",
        "state": "accepted_primary",
        "valid_time": "2026-07-12T06:30:00Z",
        "first_actionable_seen_at": "2026-07-12T06:31:00Z",
        "parsed_observation": {"temperature_c": 30.0, "dewpoint_c": 22.0, "cloud_text": "FEW020", "weather_text": "RA"},
    }


def _p0() -> dict:
    return {
        "schema": "full_physical_p0_prior_context_v1",
        "ok": True,
        "state": "available_shadow_only",
        "prior_context_id": "physicalp0_test",
        "basin_context": "sichuan_basin_zuuu",
    }


def _coverage() -> dict:
    return {
        "event_date": "2026-07-12",
        "station_id": "ZUUU",
        "coverage_manifest_sha256": "coverage-test",
        "coverage_snapshot_persisted": True,
        "replay_eligibility": {"a_class_as_of_replay_eligible": True},
    }


def test_station_day_strong_sample_binds_real_p0_forecast_and_final_x_without_s_or_market() -> None:
    event = _event()
    result = build_station_day_strong_physical_training_sample(
        canonical_observation_event=event,
        canonical_predecessors=[],
        coverage_manifest=_coverage(),
        final_physical_x_label={"final_physical_high_c": 32.0, "label_available_at": "2026-07-12T18:00:00Z", "label_finality": "final", "strict_asof_training_eligible": True, "label_provenance": {"raw_sha256": "a" * 64}},
        full_physical_p0_prior_context=_p0(),
        forecast_evidence={
            "forecast_evidence_snapshot_id": "fwctx-test",
            "source_revisions": [{"source_revision_id": "gfs-rev", "source_id": "gfs", "available_at": "2026-07-12T06:00:00Z", "forecast_peak_c": 31.5}],
        },
        source_reliability_pre_state={"AMSC": {"state": "unknown"}},
    )
    sample = result["training_sample"]
    feature = build_strong_observation_training_feature(
        training_sample=sample,
        canonical_observation_event=event,
        pre_event_trajectory_feature_state=result["pre_event_trajectory"],
        source_reliability_pre_state={"AMSC": {"state": "unknown"}},
        training_scope=TRAINING_SCOPE_PHYSICAL_X,
    )

    assert result["ok"] is True
    assert sample["quality_grade"] == "B"
    assert sample["labels"]["final_physical_high_c"] == 32.0
    assert sample["labels"]["settlement_scalar_c"] is None
    assert result["pre_event_trajectory"]["path_state_candidate"] == "no_prior_observation"
    assert feature["ok"] is True
    assert result["boundary"]["settlement_s_or_bucket_not_required"] is True


def test_station_day_strong_sample_rejects_missing_full_physical_p0() -> None:
    result = build_station_day_strong_physical_training_sample(
        canonical_observation_event=_event(),
        canonical_predecessors=[],
        coverage_manifest=_coverage(),
        final_physical_x_label={"final_physical_high_c": 32.0, "label_available_at": "2026-07-12T18:00:00Z", "label_finality": "final", "strict_asof_training_eligible": True},
        full_physical_p0_prior_context={},
        forecast_evidence={"forecast_evidence_snapshot_id": "fwctx-test", "source_revisions": [{"source_revision_id": "gfs-rev", "source_id": "gfs", "available_at": "2026-07-12T06:00:00Z"}]},
        source_reliability_pre_state={},
    )

    assert result["ok"] is False
    assert "station_day_strong_sample_full_physical_p0_unavailable" in result["block_reasons"]


def test_station_day_strong_sample_quarantines_non_strict_final_x_label() -> None:
    result = build_station_day_strong_physical_training_sample(
        canonical_observation_event=_event(),
        canonical_predecessors=[],
        coverage_manifest=_coverage(),
        final_physical_x_label={
            "final_physical_high_c": 32.0,
            "label_available_at": "2026-07-12T18:00:00Z",
            "label_finality": "final",
            "strict_asof_training_eligible": False,
        },
        full_physical_p0_prior_context=_p0(),
        forecast_evidence={
            "forecast_evidence_snapshot_id": "fwctx-test",
            "source_revisions": [{"source_revision_id": "gfs-rev", "source_id": "gfs", "available_at": "2026-07-12T06:00:00Z"}],
        },
        source_reliability_pre_state={},
    )

    assert result["ok"] is False
    assert "station_day_strong_sample_final_physical_x_not_strict_asof_eligible" in result["block_reasons"]
