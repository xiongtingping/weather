"""Content-addressed artifact contracts for production-successor v6."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from physical_latent_state_contract import LATENT_STATES


TRANSITION_ARTIFACT_SCHEMA = "physical_state_bayes_v6_transition_artifact_v1"
OBSERVATION_ARTIFACT_SCHEMA = "physical_state_bayes_v6_observation_artifact_v1"
QUANTIZED_OBSERVATION_POLICY_SCHEMA = "quantized_integer_temperature_robust_likelihood_v1"
TIME_CONDITIONED_OBSERVATION_POLICY_SCHEMA = "time_conditioned_all_weather_regime_conditioned_observation_likelihood_v2"
EVENT_CORRELATION_POLICY_SCHEMA = "canonical_physical_event_cluster_information_budget_v2"
ADVERSE_Y_COUPLING_POLICY_SCHEMA = "adverse_regime_candidate_y_coupling_guard_v2"
TRANSITION_FEATURES = frozenset(
    {
        "remaining_warming_c",
        "forecast_rate_c_per_hour",
        "future_max_increment_c",
        "future_max_drawdown_c",
        "minutes_to_curve_peak_scaled",
        "forcing_availability",
        "precipitation_probability",
        "precipitation_feature_available",
        "cloud_suppression_probability",
        "cloud_feature_available",
        "cb_probability",
        "cb_feature_available",
        "cloud_suppression_duration_hours",
        "precipitation_duration_hours",
        "cb_duration_hours",
    }
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in dict(value).items() if key != "artifact_sha256"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def artifact_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def default_time_conditioned_observation_policy() -> dict[str, Any]:
    return {
        "schema": TIME_CONDITIONED_OBSERVATION_POLICY_SCHEMA,
        "timezone": "Asia/Shanghai",
        "station_latitude_deg": 30.5785,
        "station_longitude_deg": 103.9471,
        "daylight_remaining_reference_hours": 12.0,
        "minutes_to_peak_scale": 90.0,
        "future_rise_scale_c": 1.5,
        "rate_strength_scale_c_per_hour": 3.0,
        "maximum_abs_log_adjustment": 2.0,
        "convective_evidence_adverse_strength": {
            "none": 0.0,
            "tcu": 0.35,
            "cb": 0.80,
            "thunderstorm": 1.0,
        },
        "lock_maturity_weights": {
            "daylight_progress": 0.35,
            "curve_peak_passage": 0.35,
            "future_rise_exhaustion": 0.30,
        },
        "state_log_adjustments": {
            "early_adverse": {
                "warming": -0.15, "slowing_warming": 0.10, "temporary_platform": 0.40,
                "temporary_cooling": 0.85, "cooling_then_rebound": 0.75,
                "weather_suppressed": 0.80, "terminal_cooling": -0.60, "final_locked": -1.40,
            },
            "late_adverse": {
                "warming": -0.40, "slowing_warming": -0.20, "temporary_platform": -0.10,
                "temporary_cooling": 0.10, "cooling_then_rebound": -0.30,
                "weather_suppressed": 0.35, "terminal_cooling": 0.75, "final_locked": 1.00,
            },
            "early_fair_warming": {
                "warming": 0.75, "slowing_warming": 0.40, "temporary_platform": 0.0,
                "temporary_cooling": -0.40, "cooling_then_rebound": 0.10,
                "weather_suppressed": -0.50, "terminal_cooling": -0.70, "final_locked": -1.00,
            },
            "late_stable_fair": {
                "warming": -0.40, "slowing_warming": 0.0, "temporary_platform": 0.35,
                "temporary_cooling": 0.10, "cooling_then_rebound": -0.20,
                "weather_suppressed": -0.15, "terminal_cooling": 0.45, "final_locked": 0.65,
            },
        },
        "forecast_regime_state_log_adjustments": {
            "clear_forecast_observed_adverse_rebound": {
                "warming": 0.05, "slowing_warming": 0.10, "temporary_platform": 0.20,
                "temporary_cooling": 0.40, "cooling_then_rebound": 0.65,
                "weather_suppressed": -0.15, "terminal_cooling": -0.35, "final_locked": -0.65,
            },
            "adverse_forecast_observed_adverse_early": {
                "warming": -0.15, "slowing_warming": -0.05, "temporary_platform": 0.10,
                "temporary_cooling": 0.25, "cooling_then_rebound": -0.10,
                "weather_suppressed": 0.50, "terminal_cooling": -0.10, "final_locked": -0.20,
            },
            "adverse_forecast_observed_adverse_late": {
                "warming": -0.35, "slowing_warming": -0.20, "temporary_platform": -0.05,
                "temporary_cooling": 0.05, "cooling_then_rebound": -0.25,
                "weather_suppressed": 0.40, "terminal_cooling": 0.45, "final_locked": 0.55,
            },
        },
        "forecast_role": "conditioning_modulator_not_independent_likelihood",
        "event_correlation_policy": {
            "schema": EVENT_CORRELATION_POLICY_SCHEMA,
            "independent_interval_minutes": 60.0,
            "minimum_event_information_weight": 0.10,
            "persistent_regime_reference_minutes": 45.0,
            "persistent_regime_minimum_weight": 0.20,
            "combination": "minimum_interval_and_persistent_regime",
            "first_observation_full_weight": True,
            "first_observation_basis": "canonical_event_count",
            "physical_event_cluster_signature": "weather_cloud_fog_and_quantization_robust_trajectory_phase",
            "change_point_resets_cluster": True,
            "change_point_information_weight": 1.0,
            "cluster_information_budget": 2.0,
            "scope": "soft_observation_emission_only_hard_bounds_undiscounted",
        },
        "adverse_y_coupling_policy": {
            "schema": ADVERSE_Y_COUPLING_POLICY_SCHEMA,
            "latent_state_update": "candidate_conditional_y_neutral",
            "candidate_effect_direction": "one_sided_high_temperature_suppression_only",
            "persistent_duration_reference_minutes": 120.0,
            "instantaneous_adverse_exposure_floor": 0.20,
            "candidate_gap_scale_c": 5.0,
            "maximum_candidate_log_penalty": 1.5,
            "shared_maturity_aggregation": "minimum_across_candidates_conservative",
            "candidate_penalty_monotonicity": "non_increasing_log_penalty_with_candidate_y",
            "tempering_order": "temper_then_candidate_conditional_y_center_then_monotone_penalty",
            "hard_bounds_discounted": False,
        },
    }


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _common_issues(value: Mapping[str, Any], schema: str) -> list[str]:
    issues: list[str] = []
    if _text(value.get("schema")) != schema:
        issues.append("artifact_schema_invalid")
    if _text(value.get("state")) not in {"candidate", "approved"}:
        issues.append("artifact_state_invalid")
    if value.get("production_runtime_eligible") is True and (
        _text(value.get("state")) != "approved" or value.get("shadow_only") is not False
    ):
        issues.append("production_artifact_not_approved")
    declared = _text(value.get("artifact_sha256"))
    if declared and declared != artifact_sha256(value):
        issues.append("artifact_sha256_mismatch")
    for field in ("training_dataset_sha256", "training_code_sha256", "promotion_evidence_sha256"):
        if value.get("production_runtime_eligible") is True and len(_text(value.get(field))) != 64:
            issues.append(f"production_artifact_lineage_invalid:{field}")
    return issues


def validate_transition_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    artifact = dict(value)
    issues = _common_issues(artifact, TRANSITION_ARTIFACT_SCHEMA)
    transitions = artifact.get("transitions")
    if not isinstance(transitions, Mapping) or set(transitions) != set(LATENT_STATES):
        issues.append("transition_source_state_set_invalid")
        transitions = {}
    for source in LATENT_STATES:
        targets = transitions.get(source) if isinstance(transitions, Mapping) else None
        if not isinstance(targets, Mapping):
            issues.append(f"transition_targets_invalid:{source}")
            continue
        if source == "final_locked" and targets:
            issues.append("final_locked_must_be_absorbing")
        for target, parameters in targets.items():
            if target not in LATENT_STATES or target == source or not isinstance(parameters, Mapping):
                issues.append(f"transition_target_invalid:{source}:{target}")
                continue
            base_rate = _finite(parameters.get("base_rate_per_hour"))
            if base_rate is None or base_rate < 0.0:
                issues.append(f"transition_base_rate_invalid:{source}:{target}")
            coefficients = parameters.get("coefficients")
            if not isinstance(coefficients, Mapping):
                issues.append(f"transition_coefficients_invalid:{source}:{target}")
                continue
            unknown = set(coefficients) - TRANSITION_FEATURES
            if unknown:
                issues.append(f"transition_feature_unknown:{source}:{target}")
            if any(_finite(item) is None for item in coefficients.values()):
                issues.append(f"transition_coefficient_non_finite:{source}:{target}")
    if issues:
        raise ValueError(";".join(sorted(set(issues))))
    artifact["artifact_sha256"] = artifact_sha256(artifact)
    return artifact


def validate_observation_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    artifact = dict(value)
    issues = _common_issues(artifact, OBSERVATION_ARTIFACT_SCHEMA)
    models = artifact.get("state_models")
    policy = artifact.get("quantized_observation_policy")
    temporal_policy = artifact.get("time_conditioned_observation_policy")
    if artifact.get("production_runtime_eligible") is True:
        if not isinstance(policy, Mapping):
            issues.append("production_quantized_observation_policy_missing")
        else:
            if _text(policy.get("schema")) != QUANTIZED_OBSERVATION_POLICY_SCHEMA:
                issues.append("quantized_observation_policy_schema_invalid")
            quantization = _finite(policy.get("temperature_quantization_c"))
            radius = _finite(policy.get("robust_mahalanobis_radius"))
            if quantization is None or abs(quantization - 1.0) > 1e-12:
                issues.append("quantized_observation_temperature_quantization_invalid")
            if radius is None or radius < 2.0 or radius > 10.0:
                issues.append("quantized_observation_robust_radius_invalid")
            if _text(policy.get("measurement_error_model")) != "independent_uniform_rounding_moment_matched_gaussian":
                issues.append("quantized_observation_measurement_error_model_invalid")
            if policy.get("huberize_mahalanobis_tail") is not True:
                issues.append("quantized_observation_huber_policy_invalid")
        if not isinstance(temporal_policy, Mapping):
            issues.append("production_time_conditioned_observation_policy_missing")
        else:
            if _text(temporal_policy.get("schema")) != TIME_CONDITIONED_OBSERVATION_POLICY_SCHEMA:
                issues.append("time_conditioned_observation_policy_schema_invalid")
            if _text(temporal_policy.get("timezone")) != "Asia/Shanghai":
                issues.append("time_conditioned_observation_timezone_invalid")
            for field in (
                "station_latitude_deg", "station_longitude_deg", "daylight_remaining_reference_hours",
                "minutes_to_peak_scale", "future_rise_scale_c", "rate_strength_scale_c_per_hour",
                "maximum_abs_log_adjustment",
            ):
                number = _finite(temporal_policy.get(field))
                if number is None or (field not in {"station_latitude_deg", "station_longitude_deg"} and number <= 0.0):
                    issues.append(f"time_conditioned_observation_parameter_invalid:{field}")
            maturity_weights = temporal_policy.get("lock_maturity_weights")
            if not isinstance(maturity_weights, Mapping) or set(maturity_weights) != {
                "daylight_progress", "curve_peak_passage", "future_rise_exhaustion"
            } or any(_finite(item) is None or float(item) < 0.0 for item in maturity_weights.values()) or abs(
                sum(float(item) for item in maturity_weights.values()) - 1.0
            ) > 1e-8:
                issues.append("time_conditioned_observation_maturity_weights_invalid")
            convective_strength = temporal_policy.get("convective_evidence_adverse_strength")
            if (
                not isinstance(convective_strength, Mapping)
                or set(convective_strength) != {"none", "tcu", "cb", "thunderstorm"}
                or any(
                    _finite(item) is None or not 0.0 <= float(item) <= 1.0
                    for item in convective_strength.values()
                )
                or not (
                    float(convective_strength["none"])
                    <= float(convective_strength["tcu"])
                    <= float(convective_strength["cb"])
                    <= float(convective_strength["thunderstorm"])
                )
            ):
                issues.append("convective_evidence_adverse_strength_invalid")
            adjustments = temporal_policy.get("state_log_adjustments")
            expected_regimes = {"early_adverse", "late_adverse", "early_fair_warming", "late_stable_fair"}
            if not isinstance(adjustments, Mapping) or set(adjustments) != expected_regimes:
                issues.append("time_conditioned_observation_adjustment_regimes_invalid")
            else:
                cap = _finite(temporal_policy.get("maximum_abs_log_adjustment"))
                for regime, row in adjustments.items():
                    if not isinstance(row, Mapping) or set(row) != set(LATENT_STATES):
                        issues.append(f"time_conditioned_observation_state_set_invalid:{regime}")
                    elif any(
                        _finite(item) is None or (cap is not None and abs(float(item)) > cap)
                        for item in row.values()
                    ):
                        issues.append(f"time_conditioned_observation_adjustment_invalid:{regime}")
            regime_adjustments = temporal_policy.get("forecast_regime_state_log_adjustments")
            expected_regime_adjustments = {
                "clear_forecast_observed_adverse_rebound",
                "adverse_forecast_observed_adverse_early",
                "adverse_forecast_observed_adverse_late",
            }
            if not isinstance(regime_adjustments, Mapping) or set(regime_adjustments) != expected_regime_adjustments:
                issues.append("forecast_regime_conditioning_adjustments_invalid")
            else:
                cap = _finite(temporal_policy.get("maximum_abs_log_adjustment"))
                for regime, row in regime_adjustments.items():
                    if not isinstance(row, Mapping) or set(row) != set(LATENT_STATES):
                        issues.append(f"forecast_regime_conditioning_state_set_invalid:{regime}")
                    elif any(
                        _finite(item) is None or (cap is not None and abs(float(item)) > cap)
                        for item in row.values()
                    ):
                        issues.append(f"forecast_regime_conditioning_adjustment_invalid:{regime}")
            if _text(temporal_policy.get("forecast_role")) != "conditioning_modulator_not_independent_likelihood":
                issues.append("time_conditioned_observation_forecast_role_invalid")
            correlation_policy = temporal_policy.get("event_correlation_policy")
            if not isinstance(correlation_policy, Mapping):
                issues.append("event_correlation_policy_missing")
            else:
                if _text(correlation_policy.get("schema")) != EVENT_CORRELATION_POLICY_SCHEMA:
                    issues.append("event_correlation_policy_schema_invalid")
                interval = _finite(correlation_policy.get("independent_interval_minutes"))
                minimum = _finite(correlation_policy.get("minimum_event_information_weight"))
                reference = _finite(correlation_policy.get("persistent_regime_reference_minutes"))
                persistent_minimum = _finite(correlation_policy.get("persistent_regime_minimum_weight"))
                if interval is None or interval <= 0.0:
                    issues.append("event_correlation_interval_invalid")
                if minimum is None or minimum <= 0.0 or minimum > 1.0:
                    issues.append("event_correlation_minimum_weight_invalid")
                if reference is None or reference <= 0.0:
                    issues.append("event_correlation_persistent_reference_invalid")
                if persistent_minimum is None or persistent_minimum <= 0.0 or persistent_minimum > 1.0:
                    issues.append("event_correlation_persistent_minimum_invalid")
                if _text(correlation_policy.get("combination")) != "minimum_interval_and_persistent_regime":
                    issues.append("event_correlation_combination_invalid")
                if correlation_policy.get("first_observation_full_weight") is not True:
                    issues.append("event_correlation_first_observation_policy_invalid")
                if _text(correlation_policy.get("first_observation_basis")) != "canonical_event_count":
                    issues.append("event_correlation_first_observation_basis_invalid")
                if _text(correlation_policy.get("physical_event_cluster_signature")) != "weather_cloud_fog_and_quantization_robust_trajectory_phase":
                    issues.append("event_correlation_cluster_signature_invalid")
                if correlation_policy.get("change_point_resets_cluster") is not True:
                    issues.append("event_correlation_change_point_reset_invalid")
                change_weight = _finite(correlation_policy.get("change_point_information_weight"))
                budget = _finite(correlation_policy.get("cluster_information_budget"))
                if change_weight is None or not 0.0 < change_weight <= 1.0:
                    issues.append("event_correlation_change_point_weight_invalid")
                if budget is None or budget < 1.0:
                    issues.append("event_correlation_cluster_budget_invalid")
                if _text(correlation_policy.get("scope")) != "soft_observation_emission_only_hard_bounds_undiscounted":
                    issues.append("event_correlation_scope_invalid")
            coupling_policy = temporal_policy.get("adverse_y_coupling_policy")
            if not isinstance(coupling_policy, Mapping):
                issues.append("adverse_y_coupling_policy_missing")
            else:
                if _text(coupling_policy.get("schema")) != ADVERSE_Y_COUPLING_POLICY_SCHEMA:
                    issues.append("adverse_y_coupling_policy_schema_invalid")
                if _text(coupling_policy.get("latent_state_update")) != "candidate_conditional_y_neutral":
                    issues.append("adverse_y_coupling_latent_update_invalid")
                if _text(coupling_policy.get("candidate_effect_direction")) != "one_sided_high_temperature_suppression_only":
                    issues.append("adverse_y_coupling_direction_invalid")
                for field in (
                    "persistent_duration_reference_minutes",
                    "instantaneous_adverse_exposure_floor",
                    "candidate_gap_scale_c",
                    "maximum_candidate_log_penalty",
                ):
                    number = _finite(coupling_policy.get(field))
                    if number is None or number <= 0.0:
                        issues.append(f"adverse_y_coupling_parameter_invalid:{field}")
                floor = _finite(coupling_policy.get("instantaneous_adverse_exposure_floor"))
                if floor is not None and floor > 1.0:
                    issues.append("adverse_y_coupling_exposure_floor_invalid")
                if coupling_policy.get("hard_bounds_discounted") is not False:
                    issues.append("adverse_y_coupling_hard_bound_policy_invalid")
                if _text(coupling_policy.get("shared_maturity_aggregation")) != "minimum_across_candidates_conservative":
                    issues.append("adverse_y_coupling_shared_maturity_invalid")
                if _text(coupling_policy.get("candidate_penalty_monotonicity")) != "non_increasing_log_penalty_with_candidate_y":
                    issues.append("adverse_y_coupling_monotonicity_invalid")
                if _text(coupling_policy.get("tempering_order")) != "temper_then_candidate_conditional_y_center_then_monotone_penalty":
                    issues.append("adverse_y_coupling_tempering_order_invalid")
    if not isinstance(models, Mapping) or set(models) != set(LATENT_STATES):
        issues.append("observation_state_model_set_invalid")
        models = {}
    for state in LATENT_STATES:
        model = models.get(state) if isinstance(models, Mapping) else None
        if not isinstance(model, Mapping):
            issues.append(f"observation_state_model_invalid:{state}")
            continue
        mean = model.get("continuous_mean")
        covariance = model.get("continuous_covariance")
        if not isinstance(mean, list) or len(mean) != 2 or any(_finite(item) is None for item in mean):
            issues.append(f"observation_mean_invalid:{state}")
        if (
            not isinstance(covariance, list)
            or len(covariance) != 2
            or any(not isinstance(row, list) or len(row) != 2 for row in covariance)
        ):
            issues.append(f"observation_covariance_invalid:{state}")
        else:
            values = [[_finite(item) for item in row] for row in covariance]
            if any(item is None for row in values for item in row):
                issues.append(f"observation_covariance_non_finite:{state}")
            else:
                a, b, c, d = values[0][0], values[0][1], values[1][0], values[1][1]
                assert a is not None and b is not None and c is not None and d is not None
                if abs(b - c) > 1e-9 or a <= 0.0 or d <= 0.0 or a * d - b * c <= 1e-9:
                    issues.append(f"observation_covariance_not_positive_definite:{state}")
        signatures = model.get("weather_signature_probabilities")
        if not isinstance(signatures, Mapping) or "__other__" not in signatures:
            issues.append(f"observation_weather_signature_missing:{state}")
        elif any(_finite(item) is None or float(item) <= 0.0 for item in signatures.values()):
            issues.append(f"observation_weather_probability_invalid:{state}")
        elif abs(sum(float(item) for item in signatures.values()) - 1.0) > 1e-8:
            issues.append(f"observation_weather_probability_not_normalized:{state}")
    if issues:
        raise ValueError(";".join(sorted(set(issues))))
    artifact["artifact_sha256"] = artifact_sha256(artifact)
    return artifact


__all__ = [
    "OBSERVATION_ARTIFACT_SCHEMA",
    "TRANSITION_ARTIFACT_SCHEMA",
    "TRANSITION_FEATURES",
    "TIME_CONDITIONED_OBSERVATION_POLICY_SCHEMA",
    "EVENT_CORRELATION_POLICY_SCHEMA",
    "ADVERSE_Y_COUPLING_POLICY_SCHEMA",
    "artifact_sha256",
    "default_time_conditioned_observation_policy",
    "validate_observation_artifact",
    "validate_transition_artifact",
]
