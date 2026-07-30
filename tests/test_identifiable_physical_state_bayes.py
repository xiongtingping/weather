from __future__ import annotations

from dataclasses import replace
import json

import pytest

from identifiable_physical_state_bayes.contracts import (
    CanonicalPhysicalObservation,
    ContinuousState,
    MixtureComponent,
    PhysicalDriver,
    PhysicalStatePosterior,
    REGIMES,
    ReportingSlot,
)
from identifiable_physical_state_bayes.core import (
    OBSERVATION_ARTIFACT_SCHEMA,
    STATE_MODEL_ARTIFACT_SCHEMA,
    build_joint_observation_likelihood,
    canonical_sha256,
    condition_pre_event_state,
    predict_to_event,
)
from identifiable_physical_state_bayes.adapters import (
    canonical_observation_from_event,
    drivers_from_weak_likelihood,
    initialize_physical_posterior,
    scenario_weights_from_weak_likelihood,
)
from identifiable_physical_state_bayes.reporting import (
    REPORTING_OPERATOR_ARTIFACT_SCHEMA,
    project_future_paths,
)
from identifiable_physical_state_bayes.revision import (
    replace_future_forcing_generation,
)
from identifiable_physical_state_bayes.serialization import (
    posterior_from_payload,
    posterior_sha256,
)
from production_probability_physical_state_composer import (
    COMPOSER_SCHEMA,
    compile_physical_p0_extension,
    compose_physical_canonical_event_extension,
)
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    IDENTIFIABLE_PHYSICAL_FORMULA_ID,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    activation_sha256,
    validate_v6_activation,
)
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_weak_revision_runtime import (
    IDENTIFIABLE_PHYSICAL_UPDATE_SCHEMA,
    compile_v6_weak_revision_candidate,
)
from production_probability_v6_public_ssot_writer import (
    materialize_v6_public_physical_fact_row,
)


def _diagonal(values):
    return tuple(
        tuple(float(value) if row == column else 0.0 for column, value in enumerate(values))
        for row in range(len(values))
    )


def _state_artifact():
    covariance = _diagonal((0.04, 1e-7, 0.09, 0.0025, 0.04))
    value = {
        "schema": STATE_MODEL_ARTIFACT_SCHEMA,
        "state": "approved",
        "production_runtime_eligible": True,
        "shadow_only": False,
        "max_substep_minutes": 15.0,
        "memory_hours": {
            "net_temperature_tendency": {regime: 1.5 for regime in REGIMES},
            "specific_humidity": {regime: 3.0 for regime in REGIMES},
            "cold_pool_memory": {regime: 2.0 for regime in REGIMES},
        },
        "process_covariance_per_hour": {regime: covariance for regime in REGIMES},
        "regime_tendency_offset_c_per_hour": {
            "clear_heating": 0.6,
            "cloud_attenuated": -0.2,
            "precipitation_cold_pool": -1.5,
            "warm_advection_or_mixing": 0.8,
            "cold_advection": -1.0,
            "post_rain_recovery": 0.3,
            "evening_stable_decay": -0.8,
        },
        "state_bounds": {
            "latent_temperature": [-60.0, 60.0],
            "specific_humidity": [0.0, 0.05],
            "net_temperature_tendency": [-12.0, 12.0],
            "surface_wetness_memory": [0.0, 1.0],
            "cold_pool_memory": [0.0, 12.0],
        },
        "regime_transition_per_hour": _transition_rates(),
        "missing_driver_defaults": {
            "net_tendency_target_c_per_hour": 0.0,
            "specific_humidity_target": 0.012,
            "precipitation_probability": 0.0,
            "cloud_suppression_probability": 0.5,
        },
        "driver_coefficients": {
            "wetting_from_precipitation": 0.25,
            "drying_clear_fraction": 0.12,
            "cold_pool_from_precipitation": 0.8,
        },
        "missing_driver_noise_increment": 0.35,
        "forecast_regime_map": {
            "clear_dry": "clear_heating",
            "wet_precipitation": "precipitation_cold_pool",
        },
        "initial_state_defaults": {
            "latent_temperature": 24.0,
            "surface_wetness_memory": 0.25,
            "cold_pool_memory": 0.0,
            "regime_state": "cloud_attenuated",
        },
        "initial_covariance_by_regime": {regime: covariance for regime in REGIMES},
    }
    value["artifact_sha256"] = canonical_sha256(value)
    return value


def _observation_artifact():
    models = {}
    for regime in REGIMES:
        wet = regime in {"cloud_attenuated", "precipitation_cold_pool"}
        models[regime] = {
            "cloud_fraction_mean": 0.85 if wet else 0.2,
            "cloud_fraction_sigma": 0.2,
            "precipitation_probability": 0.8 if regime == "precipitation_cold_pool" else 0.08,
            "convective_probability": 0.55 if regime == "precipitation_cold_pool" else 0.05,
            "visibility_log_mean": 8.0 if wet else 9.0,
            "visibility_log_sigma": 0.8,
            "wind_speed_mean": 3.0,
            "wind_speed_sigma": 2.0,
            "wind_gust_mean": 5.0,
            "wind_gust_sigma": 3.0,
            "pressure_tendency_mean": 0.0,
            "pressure_tendency_sigma": 120.0,
        }
    value = {
        "schema": OBSERVATION_ARTIFACT_SCHEMA,
        "state": "approved",
        "production_runtime_eligible": True,
        "shadow_only": False,
        "specific_humidity_observation_sigma": 0.0015,
        "regime_joint_models": models,
    }
    value["artifact_sha256"] = canonical_sha256(value)
    return value


def _reporting_artifact():
    value = {
        "schema": REPORTING_OPERATOR_ARTIFACT_SCHEMA,
        "state": "approved",
        "production_runtime_eligible": True,
        "shadow_only": False,
        "temperature_encoding": "positive_celsius_half_up_integer",
        "path_merge_key": "scenario_regime_parameter_quantile",
        "path_quantiles": [
            {"z": -1.2815515655, "weight": 0.10},
            {"z": -0.5244005127, "weight": 0.20},
            {"z": 0.0, "weight": 0.40},
            {"z": 0.5244005127, "weight": 0.20},
            {"z": 1.2815515655, "weight": 0.10},
        ],
        "max_path_branches": 4096,
        "minimum_path_weight": 1e-12,
        "continuous_support_completion": {
            "schema": "bounded_continuous_quadrature_support_completion_v1",
            "kernel": "bounded_student_t_on_integer_reported_x",
            "total_mass": 1e-6,
            "degrees_of_freedom": 5.0,
            "minimum_scale_c": 1.0,
            "apply_only_with_future_contract_slots": True,
        },
        "timezone": "UTC",
        "scheduled_local_times": ["05:00:00", "06:00:00", "07:00:00"],
        "slot_policy": {
            "default": {
                "is_contract_slot": True,
                "encoding_step_c": 1.0,
                "report_probability": 1.0,
                "quality_acceptance_probability": 1.0,
            }
        },
    }
    value["artifact_sha256"] = canonical_sha256(value)
    return value


def _transition_rates():
    return {
        source: {
            target: (0.04 if target != source else 0.0)
            for target in REGIMES
        }
        for source in REGIMES
    }


def _driver(scenario="clear"):
    return PhysicalDriver(
        scenario_id=scenario,
        forcing_generation_sha256="a" * 64,
        net_tendency_target_c_per_hour=1.0 if scenario == "clear" else -0.5,
        specific_humidity_target=0.012,
        wetting_rate_per_hour=0.02,
        drying_rate_per_hour=0.08,
        cold_pool_source_c_per_hour2=0.0,
        process_noise_scale=1.0,
        regime_transition_per_hour=_transition_rates(),
    )


def _posterior():
    covariance = (
        (0.64, 0.0, 0.12, 0.0, 0.0),
        (0.0, 4e-6, 0.0, 0.0, 0.0),
        (0.12, 0.0, 0.49, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.04, 0.0),
        (0.0, 0.0, 0.0, 0.0, 0.25),
    )
    return PhysicalStatePosterior(
        components=(
            MixtureComponent(
                component_id="clear-component",
                normalized_weight=0.65,
                regime_state="clear_heating",
                forcing_scenario_id="clear",
                continuous_state_mean=ContinuousState(30.0, 0.011, 1.2, 0.2, 0.0),
                continuous_state_covariance=covariance,
            ),
            MixtureComponent(
                component_id="wet-component",
                normalized_weight=0.35,
                regime_state="precipitation_cold_pool",
                forcing_scenario_id="wet",
                continuous_state_mean=ContinuousState(28.5, 0.014, -0.4, 0.8, 1.0),
                continuous_state_covariance=covariance,
            ),
        )
    )


def test_covariance_must_be_positive_semidefinite() -> None:
    with pytest.raises(ValueError, match="covariance_not_positive_semidefinite"):
        MixtureComponent(
            component_id="bad",
            normalized_weight=1.0,
            regime_state="clear_heating",
            forcing_scenario_id="clear",
            continuous_state_mean=ContinuousState(30.0, 0.01, 1.0, 0.2, 0.0),
            continuous_state_covariance=((1.0, 2.0, 0, 0, 0), (2.0, 1.0, 0, 0, 0), (0, 0, 1, 0, 0), (0, 0, 0, 1, 0), (0, 0, 0, 0, 1)),
        )


def test_predict_is_deterministic_and_excludes_current_observation() -> None:
    arguments = dict(
        previous_posterior=_posterior(),
        previous_checkpoint_sha256="b" * 64,
        previous_valid_at="2026-07-19T04:00:00Z",
        canonical_event_id="event-1",
        event_valid_at="2026-07-19T05:00:00Z",
        drivers={"clear": _driver("clear"), "wet": _driver("wet")},
        state_model_artifact=_state_artifact(),
    )
    first = predict_to_event(**arguments)
    second = predict_to_event(**arguments)
    assert first == second
    assert first.current_observation_consumed is False
    assert first.delta_minutes == 60.0
    assert abs(sum(item.normalized_weight for item in first.posterior.components) - 1.0) < 1e-12
    assert len(first.posterior.components) <= len(REGIMES) * 2


def _prediction():
    return predict_to_event(
        previous_posterior=_posterior(),
        previous_checkpoint_sha256="b" * 64,
        previous_valid_at="2026-07-19T04:00:00Z",
        canonical_event_id="event-1",
        event_valid_at="2026-07-19T05:00:00Z",
        drivers={"clear": _driver("clear"), "wet": _driver("wet")},
        state_model_artifact=_state_artifact(),
    )


def test_joint_observation_consumes_temperature_once_and_noncontract_speci_does_not_raise_x() -> None:
    observation = CanonicalPhysicalObservation(
        canonical_event_id="event-1",
        event_valid_at="2026-07-19T05:00:00Z",
        report_type="SPECI",
        is_contract_report_slot=False,
        reported_temperature_c=30,
        reported_dewpoint_c=25,
        pressure_pa=100000.0,
        cloud_fraction=0.95,
        precipitation_present=True,
        convective_present=True,
        visibility_m=2500.0,
        wind_speed_mps=4.0,
        wind_gust_mps=8.0,
        pressure_tendency_pa_per_hour=20.0,
    )
    prediction = _prediction()
    likelihood = build_joint_observation_likelihood(
        prediction=prediction,
        observation=observation,
        observation_artifact=_observation_artifact(),
    )
    result = condition_pre_event_state(
        prediction=prediction,
        likelihood=likelihood,
        observation=observation,
        previous_observed_contract_max_c=29,
    )
    assert likelihood.current_event_consumption_count == 1
    assert "reported_temperature_c" in likelihood.evidence_fields_consumed
    assert result.observed_contract_max_c == 29
    assert result.invariant_report["non_contract_speci_does_not_raise_x_bound"] is True
    assert result.invariant_report["no_post_emission_transition"] is True
    for component in result.posterior.components:
        assert 29.5 <= component.continuous_state_mean.latent_temperature <= 30.5
        trace = next(
            row.component_trace
            for row in likelihood.component_conditioning
            if row.component_id in {item.component_id for item in prediction.posterior.components}
        )
        assert trace["temperature_and_derived_rate_double_counted"] is False


def test_contract_metar_raises_reported_x_hard_lower_bound() -> None:
    observation = CanonicalPhysicalObservation(
        canonical_event_id="event-1",
        event_valid_at="2026-07-19T05:00:00Z",
        report_type="METAR",
        is_contract_report_slot=True,
        reported_temperature_c=31,
    )
    prediction = _prediction()
    likelihood = build_joint_observation_likelihood(
        prediction=prediction,
        observation=observation,
        observation_artifact=_observation_artifact(),
    )
    result = condition_pre_event_state(
        prediction=prediction,
        likelihood=likelihood,
        observation=observation,
        previous_observed_contract_max_c=30,
    )
    assert result.observed_contract_max_c == 31


def test_reporting_operator_preserves_multimodality_and_hard_lower_bound() -> None:
    posterior = _posterior()
    slots = (
        ReportingSlot("2026-07-19T06:00:00Z", True, report_probability=1.0),
        ReportingSlot("2026-07-19T07:00:00Z", True, report_probability=0.7, quality_acceptance_probability=0.9),
        ReportingSlot("2026-07-19T07:30:00Z", False, report_probability=1.0),
    )
    projection = project_future_paths(
        posterior=posterior,
        state_reference_valid_at="2026-07-19T05:00:00Z",
        reporting_slots=slots,
        drivers_by_scenario={
            "clear": (_driver("clear"), _driver("clear"), _driver("clear")),
            "wet": (_driver("wet"), _driver("wet"), _driver("wet")),
        },
        observed_contract_max_c=29,
        state_model_artifact=_state_artifact(),
        reporting_operator_artifact=_reporting_artifact(),
    )
    assert abs(sum(projection.reported_x_distribution.values()) - 1.0) < 1e-9
    assert min(projection.reported_x_distribution) >= 29
    assert len(projection.reported_x_distribution) >= 2
    assert max(projection.reported_x_distribution) == 60
    assert all(
        projection.reported_x_distribution[bucket] > 0.0
        for bucket in range(29, 61)
    )
    assert projection.hard_condition_applied is True
    assert projection.probability_sum_error <= 1e-9
    assert projection.continuous_support_completion["applied"] is True
    assert projection.continuous_support_completion["total_mass"] == 1e-6


def test_projection_uses_only_state_transition_without_source_temperature_path() -> None:
    state_artifact = _state_artifact()
    state_artifact["artifact_sha256"] = canonical_sha256(
        {key: value for key, value in state_artifact.items() if key != "artifact_sha256"}
    )
    slots = (
        ReportingSlot("2026-07-19T06:00:00Z", True),
        ReportingSlot("2026-07-19T12:00:00Z", True),
        ReportingSlot("2026-07-19T18:00:00Z", True),
    )
    clear = replace(
        _driver("clear"),
        net_tendency_target_c_per_hour=0.0,
    )
    wet = replace(
        _driver("wet"),
        net_tendency_target_c_per_hour=0.0,
    )
    projection = project_future_paths(
        posterior=_posterior(),
        state_reference_valid_at="2026-07-19T05:00:00Z",
        reporting_slots=slots,
        drivers_by_scenario={
            "clear": (clear, clear, clear),
            "wet": (wet, wet, wet),
        },
        observed_contract_max_c=29,
        state_model_artifact=state_artifact,
        reporting_operator_artifact=_reporting_artifact(),
    )
    assert projection.continuous_support_completion["support_upper_c"] == 60
    assert 48 in projection.reported_x_distribution
    assert projection.reported_x_distribution.get(48, 0.0) < 1e-6


def test_reporting_quadrature_does_not_reuse_process_noise_as_a_temperature_drift() -> None:
    state_artifact = _state_artifact()
    slots = tuple(
        ReportingSlot(f"2026-07-19T{hour:02d}:00:00Z", True)
        for hour in range(6, 19)
    )
    clear = replace(_driver("clear"), net_tendency_target_c_per_hour=0.0)
    projection = project_future_paths(
        posterior=_posterior(),
        state_reference_valid_at="2026-07-19T05:00:00Z",
        reporting_slots=slots,
        drivers_by_scenario={
            "clear": (clear,) * len(slots),
            "wet": (replace(clear, scenario_id="wet"),) * len(slots),
        },
        observed_contract_max_c=29,
        state_model_artifact=state_artifact,
        reporting_operator_artifact=_reporting_artifact(),
    )
    # The old implementation repeatedly injected the same positive
    # quadrature noise and generated an artificial 48°C tail.
    assert projection.reported_x_distribution.get(48, 0.0) < 1e-6


def test_missing_temperature_driver_does_not_freeze_tendency_state() -> None:
    artifact = _state_artifact()
    driver = replace(_driver("clear"), availability=0.0)
    posterior = _posterior()
    component = replace(
        posterior.components[0],
        normalized_weight=1.0,
        continuous_state_mean=ContinuousState(
            latent_temperature=27.0,
            specific_humidity=0.01,
            net_temperature_tendency=1.5,
            surface_wetness_memory=0.2,
            cold_pool_memory=0.0,
        ),
    )
    one = PhysicalStatePosterior(components=(component,))
    projection = project_future_paths(
        posterior=one,
        state_reference_valid_at="2026-07-19T05:00:00Z",
        reporting_slots=(ReportingSlot("2026-07-19T06:00:00Z", True),),
        drivers_by_scenario={"clear": (driver,)},
        observed_contract_max_c=27,
        state_model_artifact=artifact,
        reporting_operator_artifact=_reporting_artifact(),
    )
    assert max(projection.reported_x_distribution, key=projection.reported_x_distribution.get) <= 29


def test_support_completion_does_not_smear_fully_observed_day_without_future_slots() -> None:
    projection = project_future_paths(
        posterior=_posterior(),
        state_reference_valid_at="2026-07-19T15:30:00Z",
        reporting_slots=(),
        drivers_by_scenario={"clear": (), "wet": ()},
        observed_contract_max_c=31,
        state_model_artifact=_state_artifact(),
        reporting_operator_artifact=_reporting_artifact(),
    )
    assert projection.reported_x_distribution == {31: 1.0}
    assert projection.hard_condition_applied is True
    assert projection.continuous_support_completion == {
        "applied": False,
        "reason": "no_future_contract_slot",
    }


def test_all_missing_contract_slots_produce_explicit_no_report_probability() -> None:
    slot = ReportingSlot(
        "2026-07-19T06:00:00Z",
        True,
        report_probability=0.0,
        missing_reason="frozen_missing_report_operator",
    )
    projection = project_future_paths(
        posterior=_posterior(),
        state_reference_valid_at="2026-07-19T05:00:00Z",
        reporting_slots=(slot,),
        drivers_by_scenario={"clear": (_driver("clear"),), "wet": (_driver("wet"),)},
        observed_contract_max_c=None,
        state_model_artifact=_state_artifact(),
        reporting_operator_artifact=_reporting_artifact(),
    )
    assert projection.reported_x_distribution == {}
    assert projection.no_accepted_contract_report_probability == pytest.approx(1.0)


def _weak_likelihood():
    return {
        "artifact_sha256": "3" * 64,
        "forecast_scenario_paths": {
            "sha256": "a" * 64,
            "paths": [
                {
                    "scenario_id": "clear",
                    "forecast_weather_regime": "clear_dry",
                    "hourly_curve": [
                        {"valid_time_utc": "2026-07-19T04:00:00Z", "temperature_c": 29.0, "dewpoint_c": 20.0, "precipitation_probability": 0.0, "cloud_suppression_probability": 0.1},
                        {"valid_time_utc": "2026-07-19T06:00:00Z", "temperature_c": 32.0, "dewpoint_c": 20.0, "precipitation_probability": 0.0, "cloud_suppression_probability": 0.1},
                    ],
                },
                {
                    "scenario_id": "wet",
                    "forecast_weather_regime": "wet_precipitation",
                    "hourly_curve": [
                        {"valid_time_utc": "2026-07-19T04:00:00Z", "temperature_c": 28.0, "dewpoint_c": 25.0, "precipitation_probability": 0.8, "cloud_suppression_probability": 0.9},
                        {"valid_time_utc": "2026-07-19T06:00:00Z", "temperature_c": 27.0, "dewpoint_c": 25.0, "precipitation_probability": 0.8, "cloud_suppression_probability": 0.9},
                    ],
                },
            ],
        },
        "physical_scenario_prior_weights": {"clear": 0.7, "wet": 0.3},
    }


def test_adapters_build_physical_p0_and_do_not_store_result_y_as_state() -> None:
    posterior, lineage = initialize_physical_posterior(
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        state_reference_valid_at="2026-07-19T05:00:00Z",
        state_model_artifact=_state_artifact(),
    )
    assert lineage["scenario_weight_method"] == "physical_scenario_prior_weights"
    assert {item.forcing_scenario_id for item in posterior.components} == {"clear", "wet"}
    assert {round(item.continuous_state_mean.latent_temperature, 1) for item in posterior.components} == {24.0}
    assert not hasattr(posterior.components[0].continuous_state_mean, "continuous_y_c")
    drivers = drivers_from_weak_likelihood(
        weak_likelihood=_weak_likelihood(),
        valid_at="2026-07-19T05:00:00Z",
        scenario_ids=("clear", "wet"),
        state_model_artifact=_state_artifact(),
    )
    assert drivers["clear"].net_tendency_target_c_per_hour == pytest.approx(0.0)
    assert drivers["wet"].net_tendency_target_c_per_hour == pytest.approx(0.0)


def test_driver_adapter_reads_nested_direct_controls_without_consuming_temperature() -> None:
    weak = _weak_likelihood()
    weak["forecast_scenario_paths"]["paths"][0]["hourly_curve"] = [
        {
            "valid_time_utc": "2026-07-19T04:00:00Z",
            "temperature_c": 40.0,
            "physical_regime": {
                "physical_evidence": {"total_cloud_cover_pct": 20.0}
            },
        },
        {
            "valid_time_utc": "2026-07-19T06:00:00Z",
            "temperature_c": 42.0,
            "physical_regime": {
                "physical_evidence": {"total_cloud_cover_pct": 40.0}
            },
        },
    ]
    drivers = drivers_from_weak_likelihood(
        weak_likelihood=weak,
        valid_at="2026-07-19T05:00:00Z",
        scenario_ids=("clear",),
        state_model_artifact=_state_artifact(),
    )
    driver = drivers["clear"]
    assert driver.drying_rate_per_hour == pytest.approx(0.12 * 0.7)
    assert driver.net_tendency_target_c_per_hour == pytest.approx(0.0)
    assert "net_tendency_target_c_per_hour" in driver.na_reasons
    assert "temperature_c" not in driver.na_reasons


def test_production_p0_uses_bound_physical_temperature_prior_for_z0() -> None:
    posterior, lineage = initialize_physical_posterior(
        prior_context={
            "schema": "full_physical_p0_prior_context_v1",
            "state": "available",
            "production_runtime_eligible": True,
            "shadow_only": False,
            "artifact_sha256": "a" * 64,
            "prior_model_artifact_sha256": "b" * 64,
            "prior_context_id": "physicalp0-test",
            "continuous_distribution": {
                "grid": [
                    {"temp_c": 27.0, "mass": 0.25},
                    {"temp_c": 30.0, "mass": 0.75},
                ]
            },
        },
        weak_likelihood=_weak_likelihood(),
        state_reference_valid_at="2026-07-19T05:00:00Z",
        state_model_artifact=_state_artifact(),
    )
    assert lineage["physical_temperature_prior_used"] is True
    assert lineage["default_initial_temperature_used"] is False
    assert lineage["source_temperature_path_consumed"] is False
    assert lineage["physical_temperature_prior_context_id"] == "physicalp0-test"
    assert {round(item.continuous_state_mean.latent_temperature, 1) for item in posterior.components} == {27.0, 30.0}
    assert sum(item.normalized_weight for item in posterior.components) == pytest.approx(1.0)


def test_invalid_bound_physical_temperature_prior_fails_closed() -> None:
    with pytest.raises(ValueError, match="physical_adapter_prior_temperature_mass_zero"):
        initialize_physical_posterior(
            prior_context={
                "schema": "full_physical_p0_prior_context_v1",
                "state": "available",
                "production_runtime_eligible": True,
                "shadow_only": False,
                "artifact_sha256": "a" * 64,
                "prior_model_artifact_sha256": "b" * 64,
                "continuous_distribution": {
                    "grid": [{"temp_c": 27.0, "mass": 0.0}]
                }
            },
            weak_likelihood=_weak_likelihood(),
            state_reference_valid_at="2026-07-19T05:00:00Z",
            state_model_artifact=_state_artifact(),
        )


def test_bounded_parameter_draws_remain_distinct_through_prediction() -> None:
    artifact = _state_artifact()
    artifact.pop("artifact_sha256")
    artifact["bounded_parameter_draws"] = [
        {
            "draw_id": "cool_tail",
            "normalized_weight": 0.2,
            "latent_temperature_bias_c": -2.0,
            "net_temperature_tendency_bias_c_per_hour": -0.2,
            "initial_covariance_scale": 1.0,
            "process_noise_scale_multiplier": 1.0,
        },
        {
            "draw_id": "center",
            "normalized_weight": 0.6,
            "latent_temperature_bias_c": 0.0,
            "net_temperature_tendency_bias_c_per_hour": 0.0,
            "initial_covariance_scale": 1.0,
            "process_noise_scale_multiplier": 1.0,
        },
        {
            "draw_id": "warm_tail",
            "normalized_weight": 0.2,
            "latent_temperature_bias_c": 2.0,
            "net_temperature_tendency_bias_c_per_hour": 0.2,
            "initial_covariance_scale": 1.0,
            "process_noise_scale_multiplier": 1.0,
        },
    ]
    artifact["artifact_sha256"] = canonical_sha256(artifact)
    posterior, lineage = initialize_physical_posterior(
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        state_reference_valid_at="2026-07-19T05:00:00Z",
        state_model_artifact=artifact,
    )
    assert len(posterior.components) == 6
    assert {item.bounded_parameter_draw_id for item in posterior.components} == {
        "cool_tail",
        "center",
        "warm_tail",
    }
    assert lineage["parameter_uncertainty_is_explicit_mixture"] is True
    predicted = predict_to_event(
        previous_posterior=posterior,
        previous_checkpoint_sha256="b" * 64,
        previous_valid_at="2026-07-19T05:00:00Z",
        canonical_event_id="parameter-draw-event",
        event_valid_at="2026-07-19T06:00:00Z",
        drivers=drivers_from_weak_likelihood(
            weak_likelihood=_weak_likelihood(),
            valid_at="2026-07-19T06:00:00Z",
            scenario_ids=("clear", "wet"),
            state_model_artifact=artifact,
        ),
        state_model_artifact=artifact,
    )
    assert {item.bounded_parameter_draw_id for item in predicted.posterior.components} == {
        "cool_tail",
        "center",
        "warm_tail",
    }


def test_future_forcing_rebind_preserves_state_marginal_and_replaces_scenarios() -> None:
    rebound, audit = replace_future_forcing_generation(
        posterior_at_effective_time=_posterior(),
        revised_scenario_weights={"cool_revision": 0.25, "warm_revision": 0.75},
        revised_forcing_generation_sha256="b" * 64,
        effective_from="2026-07-19T04:30:00Z",
    )
    assert audit["state_marginal_preserved"] is True
    assert audit["forecast_revision_used_as_likelihood"] is False
    assert audit["current_observation_consumed"] is False
    scenario_mass = {}
    for component in rebound.components:
        scenario_mass[component.forcing_scenario_id] = (
            scenario_mass.get(component.forcing_scenario_id, 0.0)
            + component.normalized_weight
        )
    assert scenario_mass == pytest.approx(
        {"cool_revision": 0.25, "warm_revision": 0.75}
    )


def _revised_weak_likelihood():
    value = json.loads(json.dumps(_weak_likelihood()))
    value["forecast_scenario_paths"]["sha256"] = "b" * 64
    value["physical_scenario_prior_weights"] = {
        "cool_revision": 0.2,
        "warm_revision": 0.8,
    }
    value["forecast_scenario_paths"]["paths"] = [
        {
            **value["forecast_scenario_paths"]["paths"][1],
            "scenario_id": "cool_revision",
        },
        {
            **value["forecast_scenario_paths"]["paths"][0],
            "scenario_id": "warm_revision",
        },
    ]
    return value


def test_physical_weak_revision_advances_old_driver_then_rebinds_without_likelihood() -> None:
    activation = _physical_activation()
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity="physical_weather_day_v1:ZUUU:2026-07-19",
        event_date="2026-07-19",
        decision_as_of="2026-07-19T04:00:00Z",
        state_reference_valid_at="2026-07-19T04:00:00Z",
        prior_context_id="prior-physical",
        prior_model_artifact_sha256="2" * 64,
        forecast_evidence_snapshot_id="weak-old",
        weak_likelihood_artifact_sha256="3" * 64,
        resolution_contract_id="resolution-physical",
        forcing_generation_sha256="a" * 64,
        initial_joint=(),
        candidate_scenario_responsibilities={},
        replay_generation=0,
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    previous = p0["checkpoint_row"]
    old_snapshot = {
        "forecast_evidence_snapshot_id": "weak-old",
        "target_identity": previous["target_identity"],
        "weak_likelihood_artifact_sha256": "3" * 64,
        "weak_likelihood_sha256": "c" * 64,
        "forecast_scenario_paths_sha256": "a" * 64,
    }
    new_snapshot = {
        "forecast_evidence_snapshot_id": "weak-new",
        "target_identity": previous["target_identity"],
        "weak_likelihood_artifact_sha256": "3" * 64,
        "weak_likelihood_sha256": "d" * 64,
        "forecast_scenario_paths_sha256": "b" * 64,
    }
    result = compile_v6_weak_revision_candidate(
        activation=activation,
        previous_checkpoint=previous,
        previous_snapshot=old_snapshot,
        revised_snapshot=new_snapshot,
        trigger_source_id="zuuu_taf_forecast_amsc",
        trigger_source_revision_id="taf-revision-2",
        revision_kind="amendment",
        decision_as_of="2026-07-19T04:30:00Z",
        previous_weak_likelihood=_weak_likelihood(),
        revised_weak_likelihood=_revised_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    checkpoint = result["checkpoint_row"]
    update = result["update_row"]
    quality = json.loads(update["quality_json"])
    rebind = json.loads(checkpoint["physical_forcing_rebind_json"])
    assert result["candidate_mode"] == "identifiable_physical_state"
    assert result["forecast_revision_used_as_likelihood"] is False
    assert update["probability_update_schema"] == IDENTIFIABLE_PHYSICAL_UPDATE_SCHEMA
    assert update["replacement_log_lr_by_x_json"] == "{}"
    assert quality["state_advanced_with_superseded_driver_to_effective_time"] is True
    assert quality["current_observation_consumed"] is False
    assert checkpoint["physical_state_reference_valid_at"] == "2026-07-19T04:30:00Z"
    assert checkpoint["last_event_valid_at"] == "2026-07-19T04:00:00Z"
    assert checkpoint["last_canonical_event_id"] == ""
    assert checkpoint["consumed_event_ids_json"] == "[]"
    assert checkpoint["physical_driver_generation_sha256"] == "b" * 64
    assert rebind["state_marginal_preserved"] is True
    assert min(map(int, result["reported_physical_x_distribution"])) >= (
        checkpoint["reported_x_lower_bound_c"] or -999
    )

    event = {
        "canonical_observation_event_id": "event-after-weak-revision",
        "valid_time": "2026-07-19T05:00:00Z",
        "report_type": "METAR",
        "is_contract_report_slot": True,
        "state": "accepted_primary",
        "parsed_observation": {"temperature_c": 30.0, "dewpoint_c": 24.0},
    }
    next_candidate = compose_v6_canonical_event(
        activation=activation,
        previous_checkpoint=checkpoint,
        canonical_event=event,
        pre_event_history=[],
        weak_likelihood=_revised_weak_likelihood(),
        transition_artifact={},
        observation_artifact={},
        decision_as_of="2026-07-19T05:00:00Z",
        completed_at="2026-07-19T05:00:00Z",
        physical_state_model_artifact=_state_artifact(),
        physical_observation_artifact=_observation_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    # The weak revision checkpoint has a wall-clock reference later than its
    # last consumed event.  Event processing normalizes that predecessor to
    # 04:00 before composing the 05:00 observation.
    assert next_candidate["delta_minutes"] == 60.0

    # Full piecewise replay from P0 must match the incremental weak-revision
    # checkpoint followed by the next canonical event under the same
    # predecessor normalization.
    initial_posterior = posterior_from_payload(
        json.loads(previous["physical_state_posterior_json"])
    )
    advanced = predict_to_event(
        previous_posterior=initial_posterior,
        previous_checkpoint_sha256=previous["physical_state_posterior_sha256"],
        previous_valid_at="2026-07-19T04:00:00Z",
        canonical_event_id="full-replay-weak-effective-time",
        event_valid_at="2026-07-19T04:30:00Z",
        drivers=drivers_from_weak_likelihood(
            weak_likelihood=_weak_likelihood(),
            valid_at="2026-07-19T04:30:00Z",
            scenario_ids=("clear", "wet"),
            state_model_artifact=_state_artifact(),
        ),
        state_model_artifact=_state_artifact(),
    )
    revised_weights, _, revised_generation = scenario_weights_from_weak_likelihood(
        weak_likelihood=_revised_weak_likelihood()
    )
    rebound, _ = replace_future_forcing_generation(
        posterior_at_effective_time=advanced.posterior,
        revised_scenario_weights=revised_weights,
        revised_forcing_generation_sha256=revised_generation,
        effective_from="2026-07-19T04:30:00Z",
    )
    full_prediction = predict_to_event(
        previous_posterior=rebound,
        previous_checkpoint_sha256=posterior_sha256(rebound),
        previous_valid_at="2026-07-19T04:00:00Z",
        canonical_event_id=event["canonical_observation_event_id"],
        event_valid_at=event["valid_time"],
        drivers=drivers_from_weak_likelihood(
            weak_likelihood=_revised_weak_likelihood(),
            valid_at=event["valid_time"],
            scenario_ids=("cool_revision", "warm_revision"),
            state_model_artifact=_state_artifact(),
        ),
        state_model_artifact=_state_artifact(),
    )
    full_observation = canonical_observation_from_event(event)
    full_likelihood = build_joint_observation_likelihood(
        prediction=full_prediction,
        observation=full_observation,
        observation_artifact=_observation_artifact(),
    )
    full_conditioned = condition_pre_event_state(
        prediction=full_prediction,
        likelihood=full_likelihood,
        observation=full_observation,
        previous_observed_contract_max_c=None,
    )
    assert posterior_sha256(full_conditioned.posterior) == next_candidate[
        "checkpoint_row"
    ]["physical_state_posterior_sha256"]


def test_canonical_adapter_preserves_contract_slot_semantics() -> None:
    observation = canonical_observation_from_event(
        {
            "canonical_observation_event_id": "event-adapter",
            "valid_time": "2026-07-19T05:00:00Z",
            "report_type": "SPECI",
            "is_contract_report_slot": False,
            "state": "accepted_primary",
            "parsed_observation": {
                "temperature_c": 30.0,
                "dewpoint_c": 25.0,
                "cloud_text": "BKN020CB",
                "weather_text": "+TSRA",
                "qnh_hpa": 1001.0,
            },
        }
    )
    assert observation.report_type == "SPECI"
    assert observation.is_contract_report_slot is False
    assert observation.precipitation_present is True
    assert observation.convective_present is True
    assert observation.cloud_fraction == 0.75
    assert observation.pressure_pa == 100100.0


def test_v6_skeleton_extension_persists_physical_state_and_projects_x() -> None:
    p0 = compile_physical_p0_extension(
        target_identity="physical_weather_day_v1:ZUUU:2026-07-19",
        state_reference_valid_at="2026-07-19T05:00:00Z",
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        state_model_artifact=_state_artifact(),
        reporting_operator_artifact=_reporting_artifact(),
    )
    assert p0["physical_composer_schema"] == COMPOSER_SCHEMA
    assert abs(sum(p0["reported_physical_x_distribution"].values()) - 1.0) < 1e-9
    previous = {
        **p0,
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-19",
        "last_event_valid_at": "2026-07-19T05:00:00Z",
        "reported_x_lower_bound_c": None,
    }
    event = {
        "canonical_observation_event_id": "physical-event-1",
        "valid_time": "2026-07-19T05:00:00Z",
        "report_type": "METAR",
        "is_contract_report_slot": True,
        "state": "accepted_primary",
        "parsed_observation": {
            "temperature_c": 30.0,
            "dewpoint_c": 24.0,
            "cloud_text": "BKN020",
            "weather_text": "RA",
            "qnh_hpa": 1000.0,
        },
    }
    result = compose_physical_canonical_event_extension(
        previous_checkpoint=previous,
        canonical_event=event,
        weak_likelihood=_weak_likelihood(),
        state_model_artifact=_state_artifact(),
        observation_artifact=_observation_artifact(),
        reporting_operator_artifact=_reporting_artifact(),
    )
    assert result["physical_composer_schema"] == COMPOSER_SCHEMA
    assert result["physical_current_event_consumption_count"] == 1
    assert result["reported_x_lower_bound_c"] == 30
    assert min(result["reported_physical_x_distribution"]) >= 30
    assert result["invariant_report"]["no_post_emission_transition"] is True


def _physical_activation():
    state = _state_artifact()
    observation = _observation_artifact()
    reporting = _reporting_artifact()
    value = {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "bayes_formula_id": IDENTIFIABLE_PHYSICAL_FORMULA_ID,
        "composer_schema": IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "v5_fallback_forbidden": True,
        "pre_event_predict_does_not_consume_current_observation": True,
        "no_post_emission_state_transition": True,
        "nwp_hourly_curve_not_reapplied_as_independent_likelihood": True,
        "runtime_manifest_sha256": "1" * 64,
        "prior_model_artifact_sha256": "2" * 64,
        "weak_likelihood_artifact_sha256": "3" * 64,
        "physical_state_model_artifact_sha256": state["artifact_sha256"],
        "physical_observation_artifact_sha256": observation["artifact_sha256"],
        "physical_reporting_operator_sha256": reporting["artifact_sha256"],
        "physical_state_vector": [
            "latent_temperature",
            "specific_humidity",
            "net_temperature_tendency",
            "surface_wetness_memory",
            "cold_pool_memory",
            "regime_state",
        ],
        "frozen_reporting_operator_enabled": True,
        "current_event_joint_consumption_count": 1,
        "production_transaction_b_authorized": True,
        "places_orders": False,
        "authorizes_clob": False,
    }
    value["activation_contract_sha256"] = activation_sha256(value)
    return value


def test_physical_activation_and_p0_forbid_legacy_result_space_joint() -> None:
    activation = _physical_activation()
    assert validate_v6_activation(activation)["composer_schema"] == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    kwargs = dict(
        activation=activation,
        target_identity="physical_weather_day_v1:ZUUU:2026-07-19",
        event_date="2026-07-19",
        decision_as_of="2026-07-18T16:00:00Z",
        state_reference_valid_at="2026-07-19T05:00:00Z",
        prior_context_id="prior-physical",
        prior_model_artifact_sha256="2" * 64,
        forecast_evidence_snapshot_id="weak-physical",
        weak_likelihood_artifact_sha256="3" * 64,
        resolution_contract_id="resolution-physical",
        forcing_generation_sha256="a" * 64,
        candidate_scenario_responsibilities={},
        replay_generation=1,
        supersedes_checkpoint_id="old-v6-checkpoint",
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    with pytest.raises(ValueError, match="legacy_result_space_joint_forbidden"):
        compile_v6_p0_candidate(initial_joint=(object(),), **kwargs)
    result = compile_v6_p0_candidate(initial_joint=(), **kwargs)
    checkpoint = result["checkpoint_row"]
    assert checkpoint["physical_composer_schema"] == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    assert checkpoint["state_space_schema"] == "identifiable_physical_state_bayes_v1"
    assert checkpoint["scenario_joint_schema"] == ""
    assert abs(sum(result["reported_physical_x_distribution"].values()) - 1.0) < 1e-9


def test_unique_v6_event_runtime_dispatches_only_to_physical_composer() -> None:
    activation = _physical_activation()
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity="physical_weather_day_v1:ZUUU:2026-07-19",
        event_date="2026-07-19",
        decision_as_of="2026-07-19T05:00:00Z",
        state_reference_valid_at="2026-07-19T05:00:00Z",
        prior_context_id="prior-physical",
        prior_model_artifact_sha256="2" * 64,
        forecast_evidence_snapshot_id="weak-physical",
        weak_likelihood_artifact_sha256="3" * 64,
        resolution_contract_id="resolution-physical",
        forcing_generation_sha256="a" * 64,
        initial_joint=(),
        candidate_scenario_responsibilities={},
        replay_generation=1,
        supersedes_checkpoint_id="old-v6-checkpoint",
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    event = {
        "canonical_observation_event_id": "physical-runtime-event",
        "valid_time": "2026-07-19T05:00:00Z",
        "report_type": "METAR",
        "is_contract_report_slot": True,
        "state": "accepted_primary",
        "parsed_observation": {
            "temperature_c": 30.0,
            "dewpoint_c": 24.0,
            "cloud_text": "BKN020",
            "weather_text": "RA",
            "qnh_hpa": 1000.0,
        },
    }
    result = compose_v6_canonical_event(
        activation=activation,
        previous_checkpoint=p0["checkpoint_row"],
        canonical_event=event,
        pre_event_history=[],
        weak_likelihood=_weak_likelihood(),
        transition_artifact={},
        observation_artifact={},
        decision_as_of="2026-07-19T05:00:00.100Z",
        completed_at="2026-07-19T05:00:00.200Z",
        physical_state_model_artifact=_state_artifact(),
        physical_observation_artifact=_observation_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    checkpoint = result["checkpoint_row"]
    update = result["strong_update_row"]
    assert result["physical_composer_schema"] == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    assert result["candidate_mode"] == "identifiable_physical_state"
    assert checkpoint["scenario_joint_schema"] == ""
    assert update["physical_current_event_consumption_count"] == 1
    assert update["post_state_transition_json"] == "{}"
    assert checkpoint["reported_x_lower_bound_c"] == 30
    public = materialize_v6_public_physical_fact_row(
        activation=activation,
        strong_update_row=update,
        checkpoint_row=checkpoint,
        fact_snapshot_id="physical-fact-1",
        slug="chengdu-high-2026-07-19",
        event_date="2026-07-19",
        fact_as_of="2026-07-19T05:00:00.200Z",
        correlation_id="physical-correlation-1",
        market_bucket_probabilities_na_reason="resolution_bridge_not_closed",
    )
    assert public["composer_version"] == "identifiable_physical_state_bayes_production"
    assert '"physical_state_posterior_sha256"' in public["fact_json"]


def test_physical_v6_dispatch_preserves_exact_pre_event_sequence() -> None:
    activation = _physical_activation()
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity="physical_weather_day_v1:ZUUU:2026-07-19",
        event_date="2026-07-19",
        decision_as_of="2026-07-19T05:00:00Z",
        state_reference_valid_at="2026-07-19T05:00:00Z",
        prior_context_id="prior-physical",
        prior_model_artifact_sha256="2" * 64,
        forecast_evidence_snapshot_id="weak-physical",
        weak_likelihood_artifact_sha256="3" * 64,
        resolution_contract_id="resolution-physical",
        forcing_generation_sha256="a" * 64,
        initial_joint=(),
        candidate_scenario_responsibilities={},
        replay_generation=0,
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    event = {
        "canonical_observation_event_id": "out-of-sequence-event",
        "valid_time": "2026-07-19T05:00:00Z",
        "report_type": "METAR",
        "is_contract_report_slot": True,
        "state": "accepted_primary",
        "parsed_observation": {"temperature_c": 30.0},
    }
    with pytest.raises(
        ValueError,
        match="physical_event_pre_event_history_checkpoint_sequence_mismatch",
    ):
        compose_v6_canonical_event(
            activation=activation,
            previous_checkpoint=p0["checkpoint_row"],
            canonical_event=event,
            pre_event_history=[
                {"canonical_observation_event_id": "not-in-checkpoint"}
            ],
            weak_likelihood=_weak_likelihood(),
            transition_artifact={},
            observation_artifact={},
            decision_as_of="2026-07-19T05:00:00.100Z",
            completed_at="2026-07-19T05:00:00.200Z",
            physical_state_model_artifact=_state_artifact(),
            physical_observation_artifact=_observation_artifact(),
            physical_reporting_operator_artifact=_reporting_artifact(),
        )


def test_physical_v6_dispatch_rejects_duplicate_and_out_of_order_events() -> None:
    activation = _physical_activation()
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity="physical_weather_day_v1:ZUUU:2026-07-19",
        event_date="2026-07-19",
        decision_as_of="2026-07-19T05:00:00Z",
        state_reference_valid_at="2026-07-19T05:00:00Z",
        prior_context_id="prior-physical",
        prior_model_artifact_sha256="2" * 64,
        forecast_evidence_snapshot_id="weak-physical",
        weak_likelihood_artifact_sha256="3" * 64,
        resolution_contract_id="resolution-physical",
        forcing_generation_sha256="a" * 64,
        initial_joint=(),
        candidate_scenario_responsibilities={},
        replay_generation=0,
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    common = {
        "activation": activation,
        "weak_likelihood": _weak_likelihood(),
        "transition_artifact": {},
        "observation_artifact": {},
        "decision_as_of": "2026-07-19T05:00:00Z",
        "completed_at": "2026-07-19T05:00:00Z",
        "physical_state_model_artifact": _state_artifact(),
        "physical_observation_artifact": _observation_artifact(),
        "physical_reporting_operator_artifact": _reporting_artifact(),
    }
    out_of_order = {
        "canonical_observation_event_id": "event-too-old",
        "valid_time": "2026-07-19T04:59:59Z",
        "report_type": "METAR",
        "is_contract_report_slot": True,
        "state": "accepted_primary",
        "parsed_observation": {"temperature_c": 30.0},
    }
    with pytest.raises(
        ValueError, match="physical_event_time_or_predecessor_reference_invalid"
    ):
        compose_v6_canonical_event(
            previous_checkpoint=p0["checkpoint_row"],
            canonical_event=out_of_order,
            pre_event_history=[],
            **common,
        )

    duplicate_checkpoint = {
        **p0["checkpoint_row"],
        "consumed_event_ids_json": json.dumps(["event-duplicate"]),
    }
    duplicate = {
        **out_of_order,
        "canonical_observation_event_id": "event-duplicate",
        "valid_time": "2026-07-19T05:00:00Z",
    }
    with pytest.raises(
        ValueError, match="physical_event_identity_or_duplicate_invalid"
    ):
        compose_v6_canonical_event(
            previous_checkpoint=duplicate_checkpoint,
            canonical_event=duplicate,
            pre_event_history=[
                {"canonical_observation_event_id": "event-duplicate"}
            ],
            **common,
        )


def test_physical_v6_dispatch_rejects_tampered_artifact_binding() -> None:
    activation = _physical_activation()
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity="physical_weather_day_v1:ZUUU:2026-07-19",
        event_date="2026-07-19",
        decision_as_of="2026-07-19T05:00:00Z",
        state_reference_valid_at="2026-07-19T05:00:00Z",
        prior_context_id="prior-physical",
        prior_model_artifact_sha256="2" * 64,
        forecast_evidence_snapshot_id="weak-physical",
        weak_likelihood_artifact_sha256="3" * 64,
        resolution_contract_id="resolution-physical",
        forcing_generation_sha256="a" * 64,
        initial_joint=(),
        candidate_scenario_responsibilities={},
        replay_generation=0,
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    tampered = {**_state_artifact(), "artifact_sha256": "f" * 64}
    with pytest.raises(
        ValueError, match="physical_event_state_model_artifact_activation_mismatch"
    ):
        compose_v6_canonical_event(
            activation=activation,
            previous_checkpoint=p0["checkpoint_row"],
            canonical_event={
                "canonical_observation_event_id": "event-tampered-artifact",
                "valid_time": "2026-07-19T05:00:00Z",
                "report_type": "METAR",
                "is_contract_report_slot": True,
                "state": "accepted_primary",
                "parsed_observation": {"temperature_c": 30.0},
            },
            pre_event_history=[],
            weak_likelihood=_weak_likelihood(),
            transition_artifact={},
            observation_artifact={},
            decision_as_of="2026-07-19T05:00:00Z",
            completed_at="2026-07-19T05:00:00Z",
            physical_state_model_artifact=tampered,
            physical_observation_artifact=_observation_artifact(),
            physical_reporting_operator_artifact=_reporting_artifact(),
        )


def test_physical_v6_dispatch_normalizes_stale_reference_to_last_event() -> None:
    activation = _physical_activation()
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity="physical_weather_day_v1:ZUUU:2026-07-19",
        event_date="2026-07-19",
        decision_as_of="2026-07-19T05:00:00Z",
        state_reference_valid_at="2026-07-19T05:00:00Z",
        prior_context_id="prior-physical",
        prior_model_artifact_sha256="2" * 64,
        forecast_evidence_snapshot_id="weak-physical",
        weak_likelihood_artifact_sha256="3" * 64,
        resolution_contract_id="resolution-physical",
        forcing_generation_sha256="a" * 64,
        initial_joint=(),
        candidate_scenario_responsibilities={},
        replay_generation=0,
        supersedes_checkpoint_id="old-v6-checkpoint",
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    previous = {
        **p0["checkpoint_row"],
        "physical_state_reference_valid_at": "2026-07-19T05:00:23Z",
        "last_event_valid_at": "2026-07-19T04:30:00Z",
        "consumed_event_ids_json": "[]",
    }
    event = {
        "canonical_observation_event_id": "event-after-stale-reference",
        "valid_time": "2026-07-19T05:00:00Z",
        "report_type": "METAR",
        "is_contract_report_slot": True,
        "state": "accepted_primary",
        "parsed_observation": {"temperature_c": 30.0, "dewpoint_c": 24.0},
    }
    result = compose_v6_canonical_event(
        activation=activation,
        previous_checkpoint=previous,
        canonical_event=event,
        pre_event_history=[],
        weak_likelihood=_weak_likelihood(),
        transition_artifact={},
        observation_artifact={},
        decision_as_of="2026-07-19T05:00:00.100Z",
        completed_at="2026-07-19T05:00:00.200Z",
        physical_state_model_artifact=_state_artifact(),
        physical_observation_artifact=_observation_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    assert result["delta_minutes"] == 30.0
    assert result["checkpoint_row"]["last_event_valid_at"] == "2026-07-19T05:00:00Z"
    assert result["checkpoint_row"]["physical_state_reference_valid_at"] == "2026-07-19T05:00:00Z"
    assert previous["physical_state_reference_valid_at"] == "2026-07-19T05:00:23Z"
