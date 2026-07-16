from __future__ import annotations

from joint_forecast_weak_likelihood_runtime import (
    build_joint_forecast_weak_likelihood,
    validate_joint_forecast_weak_artifact,
)
from joint_weak_baseline_artifact_runtime import build_joint_weak_operational_baseline_artifact


def _revision(source_id: str, peak: float) -> dict:
    return {
        "source_id": source_id,
        "source_revision_id": f"{source_id}-immutable-revision",
        "source_class": "forecast_weak_source",
        "event_date": "2026-07-13",
        "available_at": "2026-07-13T00:00:00Z",
        "forecast_peak_c": peak,
    }


def _runtime(revisions: list[dict]) -> dict:
    return build_joint_forecast_weak_likelihood(
        temperature_grid_c=[28.0 + 0.25 * index for index in range(45)],
        source_revisions=revisions,
        artifact=build_joint_weak_operational_baseline_artifact(),
        forecast_context={
            "season": "summer",
            "weather_regime": "unknown_forecast_regime",
            "lead_bucket": "d0_0600_0859",
            "forecast_evidence_snapshot_id": "forev_immutable_baseline_test",
        },
        event_date="2026-07-13",
        decision_as_of="2026-07-13T00:01:00Z",
    )


def test_operational_baseline_is_approved_and_supports_an_exact_missingness_surface() -> None:
    artifact = build_joint_weak_operational_baseline_artifact()

    assert validate_joint_forecast_weak_artifact(artifact)["ok"] is True
    result = _runtime([
        _revision("ecmwf", 35.4),
        _revision("icon", 37.6),
        _revision("gfs", 32.8),
        _revision("taf", 35.0),
    ])

    assert result["ok"] is True
    assert result["weak_source_set"]["source_set_key"] == "ecmwf+icon+gfs+taf"
    selection = result["weak_source_surface_selection"]
    assert selection["baseline_exact_surface_generated"] is True
    assert selection["surface_origin"] == "approved_precalibration_baseline_exact_generated"
    assert selection["boundary"]["no_generic_fallback_surface"] is True
    assert result["weak_information_confidence"] < 1.0
    assert result["joint_information_scale"] < 1.0
    assert abs(sum(row["likelihood"] for row in result["joint_forecast_weak_likelihood"]) - 1.0) < 1e-12


def test_baseline_missing_sources_reduce_information_without_imputing_a_source() -> None:
    full = _runtime([_revision("ecmwf", 35.0), _revision("icon", 36.0), _revision("gfs", 33.0), _revision("taf", 35.0)])
    sparse = _runtime([_revision("gfs", 33.0), _revision("taf", 35.0)])

    assert full["ok"] is True
    assert sparse["ok"] is True
    assert sparse["weak_source_set"]["missing_source_ids"] == ["ecmwf", "icon", "jma", "wu", "twc"]
    assert sparse["joint_information_scale"] < full["joint_information_scale"]
    assert sparse["weak_information_confidence"] < full["weak_information_confidence"]
    assert all(row["source_id"] in {"gfs", "taf"} for row in sparse["source_expert_trace"])


def test_baseline_empty_source_set_is_neutral_and_does_not_create_a_forecast() -> None:
    result = _runtime([])

    assert result["ok"] is True
    assert result["weak_evidence_state"] == "neutral_empty_source_set"
    assert result["joint_information_scale"] == 0.0
    assert result["weak_information_confidence"] == 0.0


def test_operational_baseline_preserves_material_source_disagreement_as_multiple_modes() -> None:
    result = _runtime([
        _revision("ecmwf", 35.365),
        _revision("icon", 37.625),
        _revision("gfs", 32.815),
        _revision("twc", 39.0),
    ])

    assert result["ok"] is True
    assert len(result["forecast_multimodality_trace"]["modes"]) >= 2
    assert result["forecast_multimodality_trace"]["multimodality_state"] == "multimodal"
    assert result["weak_information_confidence"] <= 0.35
    assert all(mode["supporting_source_ids"] for mode in result["forecast_multimodality_trace"]["modes"])


def test_operational_baseline_does_not_force_multiple_modes_when_sources_converge() -> None:
    result = _runtime([
        _revision("ecmwf", 35.00),
        _revision("icon", 35.05),
        _revision("gfs", 35.10),
        _revision("taf", 35.00),
    ])

    assert result["ok"] is True
    assert result["forecast_multimodality_trace"]["multimodality_state"] == "unimodal"
    assert result["mode_formation_policy"] == "emergent_from_bias_corrected_distance_residual_overlap_and_source_correlation"
