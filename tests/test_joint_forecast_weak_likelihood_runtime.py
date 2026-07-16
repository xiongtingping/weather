from __future__ import annotations

from joint_forecast_weak_likelihood_runtime import (
    ARTIFACT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    build_joint_forecast_weak_likelihood,
    canonical_artifact_sha256,
    validate_joint_forecast_weak_artifact,
)
from joint_weak_bias_calibration_runtime import SCHEMA_VERSION as BIAS_CALIBRATION_SCHEMA_VERSION
from weak_likelihood_source_set_runtime import SOURCE_UNIVERSE, SURFACE_SCHEMA_VERSION


EVENT_DATE = "2026-07-03"
AS_OF = "2026-07-03T06:31:00Z"
GRID = [27.0 + index * 0.25 for index in range(33)]


def _correlation(source_ids: list[str]) -> dict:
    return {
        left: {
            right: 1.0 if left == right else 0.65 if {left, right} == {"wu", "twc"} else 0.2
            for right in source_ids
        }
        for left in source_ids
    }


def _artifact() -> dict:
    source_ids = ["ecmwf", "gfs", "taf", "wu", "twc"]
    def surface(available: list[str]) -> dict:
        empty = not available
        return {
            "schema": SURFACE_SCHEMA_VERSION,
            "available_source_ids": available,
            "missing_source_ids": [source_id for source_id in SOURCE_UNIVERSE if source_id not in available],
            "source_base_weights": {source_id: {"ecmwf": 0.25, "gfs": 0.25, "taf": 0.35, "wu": 0.15, "twc": 0.15}[source_id] for source_id in available},
            "source_error_calibration": {source_id: {"bias_c": 0.0, "error_sigma_c": 0.25} for source_id in available},
            "family_information_caps": {"nwp": 0.30, "taf": 0.35, "consumer_weather": 0.20},
            "source_correlation": _correlation(available),
            "joint_information_scale": 0.0 if empty else 1.0,
            "information_confidence": 0.0 if empty else 0.8,
        }
    context_key = "season=summer|weather_regime=clear|lead_bucket=0_6h"
    artifact = {
        "schema": ARTIFACT_SCHEMA_VERSION,
        "approval_state": "approved",
        "source_data_policy": "forecast_only_asof",
        "bias_correction": {
            "schema": BIAS_CALIBRATION_SCHEMA_VERSION,
            "residual_definition": "forecast_peak_c_minus_physical_x_final_c",
            "source_global_states": {},
            "context_states": {},
        },
        "source_catalog": {
            "ecmwf": {"provider_family": "nwp"},
            "gfs": {"provider_family": "nwp"},
            "taf": {"provider_family": "taf"},
            "wu": {"provider_family": "consumer_weather"},
            "twc": {"provider_family": "consumer_weather"},
        },
        "source_set_surfaces": {
            context_key: {
                "ecmwf+gfs+taf+wu+twc": surface(source_ids),
                "ecmwf": surface(["ecmwf"]),
                "gfs+taf": surface(["gfs", "taf"]),
                "empty": surface([]),
            }
        },
    }
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)
    return artifact


def _revision(source_id: str, peak: float, *, revision: str = "r1", available_at: str = "2026-07-03T06:30:00Z") -> dict:
    return {
        "source_id": source_id,
        "source_revision_id": f"{source_id}-{revision}",
        "source_class": "forecast_weak_source",
        "event_date": EVENT_DATE,
        "available_at": available_at,
        "forecast_peak_c": peak,
        "forecast_sigma_c": 0.20,
    }


def _build(revisions, **overrides) -> dict:
    values = {
        "temperature_grid_c": GRID,
        "source_revisions": revisions,
        "artifact": _artifact(),
        "forecast_context": {
            "season": "summer",
            "weather_regime": "clear",
            "lead_bucket": "0_6h",
            "forecast_evidence_snapshot_id": "forecast-snapshot-immutable-1",
        },
        "event_date": EVENT_DATE,
        "decision_as_of": AS_OF,
    }
    values.update(overrides)
    return build_joint_forecast_weak_likelihood(**values)


def test_all_sources_are_experts_family_caps_apply_and_no_model_taf_special_case_exists() -> None:
    revisions = [
        _revision("ecmwf", 29.0),
        _revision("gfs", 29.2),
        _revision("taf", 33.0),
        _revision("wu", 30.0),
        _revision("twc", 30.1),
    ]
    first = _build(revisions)
    second = _build(list(reversed(revisions)))

    assert first["schema"] == SCHEMA_VERSION
    assert first["ok"] is True
    assert first["joint_forecast_weak_likelihood_sha256"] == second["joint_forecast_weak_likelihood_sha256"]
    assert "model_taf_conflict" not in first
    assert first["boundary"]["model_taf_special_case_absent"] is True
    assert first["family_trace"]["nwp"]["cap_hit"] is True
    assert first["family_trace"]["consumer_weather"]["effective_information_weight"] <= 0.20
    assert first["forecast_multimodality_trace"]["diagnostic_only"] is True
    assert first["forecast_multimodality_trace"]["changes_probability"] is False
    assert first["forecast_multimodality_trace"]["mode_count"] >= 2
    mode_support = {
        round(float(mode["temperature_c"])): set(mode["supporting_source_ids"])
        for mode in first["forecast_multimodality_trace"]["modes"]
    }
    assert "taf" in mode_support[33]
    assert "ecmwf" not in mode_support[33]
    assert {"ecmwf", "gfs"} & mode_support[29]
    assert "taf" not in mode_support[29]
    assert all(
        abs(sum(mode["source_contribution_shares"].values()) - 1.0) < 1e-12
        for mode in first["forecast_multimodality_trace"]["modes"]
    )
    output_modes = [
        row["temperature_c"]
        for index, row in enumerate(first["joint_forecast_weak_likelihood"])
        if row["likelihood"] >= (first["joint_forecast_weak_likelihood"][index - 1]["likelihood"] if index else -1.0)
        and row["likelihood"] >= (first["joint_forecast_weak_likelihood"][index + 1]["likelihood"] if index + 1 < len(first["joint_forecast_weak_likelihood"]) else -1.0)
    ]
    assert len(output_modes) >= 2
    assert first["formula"] == "correlation_aware_tempered_mixture_of_context_calibrated_forecast_experts"
    assert first["boundary"]["weak_distribution_preserves_material_scenarios_but_allows_evidence_based_mode_merging"] is True


def test_close_bias_corrected_sources_may_form_one_mode_and_modes_are_not_forced() -> None:
    output = _build(
        [
            _revision("ecmwf", 30.00),
            _revision("gfs", 30.05),
            _revision("taf", 30.10),
            _revision("wu", 30.00),
            _revision("twc", 30.05),
        ]
    )

    assert output["ok"] is True
    assert output["forecast_multimodality_trace"]["multimodality_state"] == "unimodal"
    assert output["force_multimodality"] is False
    assert output["force_unimodality"] is False


def test_legal_negative_residual_correlation_does_not_block_runtime() -> None:
    artifact = _artifact()
    surface = artifact["source_set_surfaces"]["season=summer|weather_regime=clear|lead_bucket=0_6h"]["gfs+taf"]
    surface["source_correlation"]["gfs"]["taf"] = -0.4
    surface["source_correlation"]["taf"]["gfs"] = -0.4
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)

    output = _build([_revision("gfs", 30.0), _revision("taf", 31.0)], artifact=artifact)

    assert output["ok"] is True
    assert output["correlation_discount"] == 1.0


def test_missing_context_or_nonforecast_d0_context_fails_closed_without_uniform_likelihood() -> None:
    no_context = _build([_revision("ecmwf", 30.0)], forecast_context={"season": "winter", "weather_regime": "clear", "lead_bucket": "0_6h"})
    bad_context = _build(
        [_revision("ecmwf", 30.0)],
        forecast_context={
            "season": "summer",
            "weather_regime": "clear",
            "lead_bucket": "0_6h",
            "current_metar_temperature_c": 30.0,
        },
    )

    assert no_context["ok"] is False
    assert "weak_source_set_surface_missing_or_invalid" in no_context["block_reasons"]
    assert "joint_forecast_weak_likelihood" not in no_context
    assert bad_context["ok"] is False
    assert any(reason.startswith("forecast_context_contains_nonforecast_or_unknown_field") for reason in bad_context["block_reasons"])


def test_latest_visible_revision_is_selected_strictly_asof_and_future_revision_is_excluded() -> None:
    output = _build(
        [
            _revision("ecmwf", 29.0, revision="old", available_at="2026-07-03T06:20:00Z"),
            _revision("ecmwf", 34.0, revision="future", available_at="2026-07-03T06:32:00Z"),
        ]
    )

    assert output["ok"] is True
    assert output["source_expert_trace"][0]["source_revision_id"] == "ecmwf-old"
    assert any(row["exclusion_reason"] == "forecast_revision_after_asof" for row in output["source_trace"])


def test_gfs_taf_uses_its_own_exact_surface_and_reports_information_confidence() -> None:
    output = _build([_revision("gfs", 30.0), _revision("taf", 31.0)])

    assert output["ok"] is True
    assert output["weak_source_set"]["source_set_key"] == "gfs+taf"
    assert output["weak_source_surface_selection"]["source_set_key"] == "gfs+taf"
    assert output["weak_information_confidence"] == 0.8


def test_candidate_scenario_responsibility_comes_from_same_weak_mixture() -> None:
    gfs = _revision("gfs", 29.0)
    gfs["hourly_curve"] = [
        {"valid_time": "2026-07-03T06:00:00Z", "temperature_c": 28.0},
        {"valid_time": "2026-07-03T07:00:00Z", "temperature_c": 29.0},
    ]
    taf = _revision("taf", 33.0)
    output = _build([gfs, taf])

    rows = {
        float(row["continuous_y_c"]): {
            scenario["source_id"]: float(scenario["responsibility"])
            for scenario in row["scenarios"]
        }
        for row in output["candidate_scenario_responsibilities"]["rows"]
    }

    assert rows[29.0]["gfs"] > rows[29.0]["taf"]
    assert rows[33.0]["taf"] > rows[33.0]["gfs"]
    assert abs(sum(rows[29.0].values()) - 1.0) < 1e-12
    paths = {
        row["source_id"]: row
        for row in output["forecast_scenario_paths"]["paths"]
    }
    assert paths["gfs"]["future_forcing_available"] is True
    assert paths["taf"]["future_forcing_available"] is False
    assert output["boundary"]["hourly_paths_are_transition_controls_not_second_likelihood"] is True


def test_non_forecast_or_hard_eligible_rows_cannot_enter_weak_likelihood() -> None:
    hard = _revision("ecmwf", 31.0)
    hard["source_class"] = "hard_observation_candidate"
    output = _build([hard])

    assert output["ok"] is False
    assert "forecast_weak_hard_or_nonforecast_source_injected" in output["block_reasons"]
    assert output["source_trace"][0]["exclusion_reason"] == "source_class_not_forecast_weak"


def test_forecast_row_with_d0_or_market_surface_is_rejected_instead_of_silently_ignored() -> None:
    contaminated = _revision("ecmwf", 31.0)
    contaminated["current_temperature_c"] = 30.0
    contaminated["market_price"] = 0.55
    output = _build([contaminated])

    assert output["ok"] is False
    assert "forecast_weak_forbidden_observation_or_market_input_seen" in output["block_reasons"]
    assert output["source_trace"][0]["exclusion_reason"] == "forecast_revision_contains_forbidden_observation_or_market_input"
    assert output["source_trace"][0]["forbidden_input_fields"] == ["current_temperature_c", "market_price"]


def test_empty_source_set_uses_approved_neutral_surface_not_a_posterior_stop() -> None:
    output = _build([])

    assert output["ok"] is True
    assert output["weak_source_set"]["source_set_key"] == "empty"
    assert output["weak_evidence_state"] == "neutral_empty_source_set"
    assert output["weak_information_confidence"] == 0.0
    assert output["joint_information_scale"] == 0.0
    assert abs(sum(row["likelihood"] for row in output["joint_forecast_weak_likelihood"]) - 1.0) < 1e-12


def test_artifact_requires_historical_context_structure_and_approval() -> None:
    artifact = _artifact()
    artifact["approval_state"] = "shadow_only"
    artifact["source_set_surfaces"] = {}

    contract = validate_joint_forecast_weak_artifact(artifact)

    assert contract["ok"] is False
    assert "joint_weak_artifact_not_approved" in contract["issues"]
    assert "joint_weak_artifact_source_set_surfaces_missing" in contract["issues"]


def test_artifact_self_hash_mismatch_fails_closed() -> None:
    artifact = _artifact()
    artifact["source_set_surfaces"]["season=summer|weather_regime=clear|lead_bucket=0_6h"]["gfs+taf"]["joint_information_scale"] = 0.3

    contract = validate_joint_forecast_weak_artifact(artifact)

    assert contract["ok"] is False
    assert "joint_weak_artifact_sha256_mismatch" in contract["issues"]


def test_trained_bias_artifact_cannot_score_its_own_or_an_earlier_event_day() -> None:
    artifact = _artifact()
    artifact["bias_correction"].update(
        {
            "training_event_day_count": 30,
            "training_event_date_min": "2026-06-04",
            "training_event_date_max": EVENT_DATE,
            "state_asof_policy": "training_event_date_max_strictly_before_scored_event_date",
        }
    )
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)

    output = _build([_revision("ecmwf", 30.0)], artifact=artifact)

    assert output["ok"] is False
    assert "bias_correction_not_strictly_prior_to_scored_event" in output["block_reasons"]


def test_nonempty_bias_artifact_must_bind_training_cutoff_into_identity() -> None:
    artifact = _artifact()
    artifact["bias_correction"]["training_event_day_count"] = 30
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)

    contract = validate_joint_forecast_weak_artifact(artifact)

    assert contract["ok"] is False
    assert "joint_weak_artifact_bias_asof_policy_invalid" in contract["issues"]
    assert "joint_weak_artifact_bias_training_event_date_max_invalid" in contract["issues"]
