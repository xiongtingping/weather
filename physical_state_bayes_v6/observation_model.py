"""Single joint canonical-observation emission for production-successor v6."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .artifact_contracts import validate_observation_artifact
from .contracts import CanonicalObservationEmission, PreEventPrediction
from .core import pre_event_prediction_sha256
from .temporal_context import CandidateTemporalCondition, ObservationTemporalContext


@dataclass(frozen=True)
class ObservationFeatureVector:
    current_step_rate_c_per_hour: float | None
    dewpoint_depression_c: float | None
    weather_signature: str
    current_step_rate_interval_c_per_hour: tuple[float, float] | None = None
    dewpoint_depression_interval_c: tuple[float, float] | None = None
    continuous_measurement_covariance: tuple[tuple[float, float], tuple[float, float]] = (
        (0.0, 0.0),
        (0.0, 0.0),
    )
    visibility_m: float | None = None
    fog_or_mist_present: bool = False
    convective_evidence_kind: str = "none"
    physical_event_cluster_signature: str = ""


def _clip(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return min(upper, max(lower, float(value)))


def _candidate_lock_maturity(
    *,
    context: ObservationTemporalContext,
    condition: CandidateTemporalCondition,
    policy: Mapping[str, object],
) -> float:
    daylight_reference = float(policy["daylight_remaining_reference_hours"])
    peak_scale = float(policy["minutes_to_peak_scale"])
    future_rise_scale = float(policy["future_rise_scale_c"])
    daylight = 1.0 - _clip(context.hours_until_sunset / daylight_reference)
    peak = 1.0 / (1.0 + math.exp(_clip(condition.minutes_to_curve_peak / peak_scale, -20.0, 20.0)))
    future_rise = math.exp(-max(0.0, condition.future_max_increment_c) / future_rise_scale)
    weights = policy["lock_maturity_weights"]
    assert isinstance(weights, Mapping)
    forecast_maturity = (
        float(weights["daylight_progress"]) * daylight
        + float(weights["curve_peak_passage"]) * peak
        + float(weights["future_rise_exhaustion"]) * future_rise
    )
    fallback = daylight
    availability = _clip(condition.forcing_availability)
    return _clip(availability * forecast_maturity + (1.0 - availability) * fallback)


def _evidence_strengths(
    *,
    features: ObservationFeatureVector,
    cloud_suppressed: bool,
    precipitation_present: bool,
    cb_present: bool,
    policy: Mapping[str, object],
) -> dict[str, float]:
    rate_scale = float(policy["rate_strength_scale_c_per_hour"])
    rate = features.current_step_rate_c_per_hour
    cooling = _clip(-float(rate) / rate_scale) if rate is not None else 0.0
    warming = _clip(float(rate) / rate_scale) if rate is not None else 0.0
    depression = features.dewpoint_depression_c
    dry = _clip((float(depression) - 4.0) / 8.0) if depression is not None else 0.0
    humid = _clip((4.0 - float(depression)) / 4.0) if depression is not None else 0.0
    low_visibility = (
        _clip((5000.0 - float(features.visibility_m)) / 4000.0)
        if features.visibility_m is not None
        else 0.0
    )
    fog = 1.0 if features.fog_or_mist_present else 0.0
    convective_weights = policy["convective_evidence_adverse_strength"]
    assert isinstance(convective_weights, Mapping)
    convective_strength = float(
        convective_weights.get(features.convective_evidence_kind, 0.0)
    )
    adverse = max(
        cooling,
        0.45 if cloud_suppressed else 0.0,
        0.7 if precipitation_present else 0.0,
        convective_strength,
        0.55 * fog,
        0.35 * low_visibility,
        0.25 * humid if (cloud_suppressed or precipitation_present or fog) else 0.0,
    )
    fair = (1.0 - adverse) * max(warming, 0.45 * dry) * (0.0 if cloud_suppressed else 1.0)
    stable = (1.0 - adverse) * (1.0 - warming) * (0.35 + 0.35 * dry)
    return {
        "cooling": cooling,
        "warming": warming,
        "dry": dry,
        "humid": humid,
        "low_visibility": low_visibility,
        "fog_or_mist": fog,
        "adverse": _clip(adverse),
        "fair_warming": _clip(fair),
        "stable_fair": _clip(stable),
    }


def _phase_adjustment(
    *, state: str, maturity: float, strengths: Mapping[str, float], policy: Mapping[str, object],
    forecast_regime_probabilities: Mapping[str, float],
) -> float:
    weights = policy["state_log_adjustments"]
    assert isinstance(weights, Mapping)
    early_adverse = weights["early_adverse"]
    late_adverse = weights["late_adverse"]
    early_fair = weights["early_fair_warming"]
    late_stable = weights["late_stable_fair"]
    assert all(isinstance(row, Mapping) for row in (early_adverse, late_adverse, early_fair, late_stable))
    adjustment = strengths["adverse"] * (
        (1.0 - maturity) * float(early_adverse[state]) + maturity * float(late_adverse[state])
    )
    adjustment += strengths["fair_warming"] * (1.0 - maturity) * float(early_fair[state])
    adjustment += strengths["stable_fair"] * maturity * float(late_stable[state])
    regime_weights = policy["forecast_regime_state_log_adjustments"]
    assert isinstance(regime_weights, Mapping)
    clear_probability = _clip(forecast_regime_probabilities.get("clear_dry", 0.0))
    adverse_probability = _clip(
        forecast_regime_probabilities.get("wet_precipitation", 0.0)
        + forecast_regime_probabilities.get("convection_or_showers", 0.0)
        + forecast_regime_probabilities.get("fog_low_cloud", 0.0)
        + 0.5 * forecast_regime_probabilities.get("cloudy_stable", 0.0)
    )
    # Forecast regime has no standalone vote.  It can only modulate physical
    # adverse evidence already present in this canonical observation.
    rebound_strength = strengths["adverse"] * clear_probability * (1.0 - maturity)
    persistence_strength = strengths["adverse"] * adverse_probability
    rebound = regime_weights["clear_forecast_observed_adverse_rebound"]
    persistent_early = regime_weights["adverse_forecast_observed_adverse_early"]
    persistent_late = regime_weights["adverse_forecast_observed_adverse_late"]
    assert all(isinstance(row, Mapping) for row in (rebound, persistent_early, persistent_late))
    adjustment += rebound_strength * float(rebound[state])
    adjustment += persistence_strength * (
        (1.0 - maturity) * float(persistent_early[state])
        + maturity * float(persistent_late[state])
    )
    cap = float(policy["maximum_abs_log_adjustment"])
    return _clip(adjustment, -cap, cap)


def _robust_quadratic(quadratic: float, radius: float) -> float:
    distance = math.sqrt(max(0.0, quadratic))
    return quadratic if distance <= radius else 2.0 * radius * distance - radius * radius


def _event_information_weight(
    *,
    prediction: PreEventPrediction,
    cloud_suppressed: bool,
    precipitation_present: bool,
    cb_present: bool,
    tcu_present: bool,
    physical_event_cluster_signature: str,
    policy: Mapping[str, object],
) -> tuple[float, dict[str, object]]:
    """Temper correlated canonical reports without weakening factual hard bounds.

    METAR/SPECI reports inside one slowly evolving physical process are not
    conditionally independent.  The first observation is retained at full
    strength; later soft emissions are discounted by cadence and, when the
    same adverse regime persists, by its already-consumed duration.
    """

    clocks = prediction.duration_clocks_before
    first_observation = clocks.canonical_event_count == 0
    change_point = bool(
        not first_observation
        and physical_event_cluster_signature
        and physical_event_cluster_signature != clocks.physical_event_cluster_signature
    )
    budget = float(policy["cluster_information_budget"])
    if first_observation or change_point:
        requested = 1.0 if first_observation else float(policy["change_point_information_weight"])
        weight = min(requested, budget)
        return weight, {
            "event_information_weight": weight,
            "interval_weight": 1.0,
            "persistent_regime_weight": 1.0,
            "first_observation_full_weight_applied": first_observation,
            "physical_event_cluster_change_point": change_point,
            "physical_event_cluster_signature": physical_event_cluster_signature,
            "previous_physical_event_cluster_signature": clocks.physical_event_cluster_signature,
            "cluster_information_budget": budget,
            "cluster_information_spent_before": 0.0,
            "cluster_information_remaining_before": budget,
            "hard_bounds_discounted": False,
        }
    minimum = float(policy["minimum_event_information_weight"])
    interval_reference = float(policy["independent_interval_minutes"])
    interval_weight = minimum + (1.0 - minimum) * _clip(
        prediction.delta_minutes / interval_reference
    )
    active_durations = []
    if cloud_suppressed and clocks.cloud_suppression_active:
        active_durations.append(clocks.cloud_suppression_minutes)
    if precipitation_present and clocks.precipitation_active:
        active_durations.append(clocks.precipitation_minutes)
    if cb_present and clocks.cb_active:
        active_durations.append(clocks.cb_minutes)
    if tcu_present and clocks.tcu_active:
        active_durations.append(clocks.tcu_minutes)
    persistent_weight = 1.0
    persistent_minutes = max(active_durations, default=0.0)
    if active_durations:
        persistent_floor = float(policy["persistent_regime_minimum_weight"])
        reference = float(policy["persistent_regime_reference_minutes"])
        persistent_weight = persistent_floor + (1.0 - persistent_floor) / (
            1.0 + persistent_minutes / reference
        )
    requested_weight = _clip(min(interval_weight, persistent_weight), minimum, 1.0)
    spent = clocks.physical_event_cluster_information_spent
    remaining = max(0.0, budget - spent)
    weight = min(requested_weight, remaining)
    return weight, {
        "event_information_weight": weight,
        "interval_weight": interval_weight,
        "persistent_regime_weight": persistent_weight,
        "persistent_regime_minutes": persistent_minutes,
        "first_observation_full_weight_applied": False,
        "physical_event_cluster_change_point": False,
        "physical_event_cluster_signature": physical_event_cluster_signature,
        "previous_physical_event_cluster_signature": clocks.physical_event_cluster_signature,
        "cluster_information_budget": budget,
        "cluster_information_spent_before": spent,
        "cluster_information_remaining_before": remaining,
        "cluster_budget_exhausted": remaining <= 0.0,
        "hard_bounds_discounted": False,
    }


def _adverse_candidate_coupling_guard(
    *,
    prediction: PreEventPrediction,
    tempered_scores: Mapping[tuple[float, str], float],
    maturity_by_y: Mapping[float, float],
    adverse_strength: float,
    reported_temperature_c: float | None,
    policy: Mapping[str, object],
) -> tuple[dict[tuple[float, str], float], dict[str, object]]:
    """Remove latent-state-to-Y leakage and add only physical one-sided suppression."""

    y_mass: dict[float, float] = {}
    for cell in prediction.joint:
        y_mass[cell.y_c] = y_mass.get(cell.y_c, 0.0) + cell.probability
    marginal_log_by_y: dict[float, float] = {}
    for y_c, total in y_mass.items():
        terms = [
            (cell.probability / total, tempered_scores[(cell.y_c, cell.state)])
            for cell in prediction.joint
            if cell.y_c == y_c
        ]
        offset = max(score for _probability, score in terms)
        marginal_log_by_y[y_c] = offset + math.log(
            sum(probability * math.exp(score - offset) for probability, score in terms)
        )
    clocks = prediction.duration_clocks_before
    persistent_minutes = max(
        clocks.cloud_suppression_minutes if clocks.cloud_suppression_active else 0.0,
        clocks.precipitation_minutes if clocks.precipitation_active else 0.0,
        clocks.cb_minutes if clocks.cb_active else 0.0,
    )
    duration_reference = float(policy["persistent_duration_reference_minutes"])
    duration_exposure = 1.0 - math.exp(-persistent_minutes / duration_reference)
    instantaneous_floor = float(policy["instantaneous_adverse_exposure_floor"])
    gap_scale = float(policy["candidate_gap_scale_c"])
    maximum_penalty = float(policy["maximum_candidate_log_penalty"])
    adjusted: dict[tuple[float, str], float] = {}
    trace_by_y: dict[str, object] = {}
    shared_maturity = min((_clip(value) for value in maturity_by_y.values()), default=0.0)
    penalty_by_y: dict[float, float] = {}
    previous_penalty_magnitude = 0.0
    for y_c in sorted(y_mass):
        gap = (
            max(0.0, y_c - float(reported_temperature_c))
            if reported_temperature_c is not None
            else 0.0
        )
        exposure = adverse_strength * shared_maturity * max(
            duration_exposure, instantaneous_floor
        )
        penalty_magnitude = maximum_penalty * exposure * _clip(gap / gap_scale)
        previous_penalty_magnitude = max(previous_penalty_magnitude, penalty_magnitude)
        penalty_by_y[y_c] = -previous_penalty_magnitude
    for cell in prediction.joint:
        maturity = _clip(maturity_by_y.get(cell.y_c, 0.0))
        gap = (
            max(0.0, cell.y_c - float(reported_temperature_c))
            if reported_temperature_c is not None
            else 0.0
        )
        candidate_penalty = penalty_by_y[cell.y_c]
        centered = tempered_scores[(cell.y_c, cell.state)] - marginal_log_by_y[cell.y_c]
        adjusted[(cell.y_c, cell.state)] = centered + candidate_penalty
        trace_by_y[f"{cell.y_c:g}"] = {
            "lock_maturity": maturity,
            "shared_lock_maturity": shared_maturity,
            "candidate_gap_c": gap,
            "candidate_log_penalty": candidate_penalty,
            "candidate_marginal_log_removed": marginal_log_by_y[cell.y_c],
        }
    return adjusted, {
        "applied": True,
        "adverse_strength": adverse_strength,
        "persistent_regime_minutes": persistent_minutes,
        "persistent_duration_exposure": duration_exposure,
        "shared_lock_maturity": shared_maturity,
        "shared_maturity_aggregation": "minimum_across_candidates_conservative",
        "tempering_order": "temper_then_candidate_conditional_y_center_then_monotone_penalty",
        "latent_state_update": "candidate_conditional_y_neutral",
        "candidate_effect_direction": "one_sided_high_temperature_suppression_only",
        "hard_bounds_discounted": False,
        "candidate_trace": trace_by_y,
    }


def _bivariate_log_density(
    values: tuple[float, float], mean: list[float], covariance: list[list[float]], radius: float
) -> float:
    a, b = float(covariance[0][0]), float(covariance[0][1])
    c, d = float(covariance[1][0]), float(covariance[1][1])
    determinant = a * d - b * c
    x0, x1 = values[0] - float(mean[0]), values[1] - float(mean[1])
    quadratic = (d * x0 * x0 - (b + c) * x0 * x1 + a * x1 * x1) / determinant
    return -math.log(2.0 * math.pi) - 0.5 * math.log(determinant) - 0.5 * _robust_quadratic(quadratic, radius)


def _continuous_log_density(
    values: tuple[float | None, float | None],
    mean: list[float],
    covariance: list[list[float]],
    measurement_covariance: tuple[tuple[float, float], tuple[float, float]],
    radius: float,
) -> float:
    available = [index for index, value in enumerate(values) if value is not None]
    if not available:
        return 0.0
    if len(available) == 2:
        combined = [
            [float(covariance[row][column]) + float(measurement_covariance[row][column]) for column in range(2)]
            for row in range(2)
        ]
        return _bivariate_log_density(
            (float(values[0]), float(values[1])), mean, combined, radius
        )
    index = available[0]
    variance = float(covariance[index][index]) + float(measurement_covariance[index][index])
    difference = float(values[index]) - float(mean[index])
    quadratic = difference * difference / variance
    return -0.5 * (math.log(2.0 * math.pi * variance) + _robust_quadratic(quadratic, radius))


def build_joint_observation_emission(
    *,
    prediction: PreEventPrediction,
    is_regular_metar: bool,
    reported_temperature_c: float | None,
    cloud_suppressed: bool,
    precipitation_present: bool,
    cb_present: bool,
    features: ObservationFeatureVector,
    observation_artifact: Mapping[str, object],
    temporal_context: ObservationTemporalContext | None = None,
) -> CanonicalObservationEmission:
    artifact = validate_observation_artifact(observation_artifact)
    policy = artifact.get("quantized_observation_policy")
    robust_radius = (
        float(policy["robust_mahalanobis_radius"])
        if isinstance(policy, Mapping)
        else float("inf")
    )
    values = (
        float(features.current_step_rate_c_per_hour)
        if features.current_step_rate_c_per_hour is not None
        else None,
        float(features.dewpoint_depression_c)
        if features.dewpoint_depression_c is not None
        else None,
    )
    if any(value is not None and not math.isfinite(value) for value in values) or not features.weather_signature:
        raise ValueError("observation_feature_vector_invalid")
    measurement_covariance = features.continuous_measurement_covariance
    flat_measurement = [float(item) for row in measurement_covariance for item in row]
    if (
        any(not math.isfinite(item) for item in flat_measurement)
        or measurement_covariance[0][0] < 0.0
        or measurement_covariance[1][1] < 0.0
        or abs(measurement_covariance[0][1] - measurement_covariance[1][0]) > 1e-12
        or measurement_covariance[0][0] * measurement_covariance[1][1]
        - measurement_covariance[0][1] * measurement_covariance[1][0] < -1e-12
    ):
        raise ValueError("observation_measurement_covariance_invalid")
    models = artifact["state_models"]
    assert isinstance(models, Mapping)
    temporal_policy = artifact.get("time_conditioned_observation_policy")
    if temporal_context is not None and not isinstance(temporal_policy, Mapping):
        raise ValueError("time_conditioned_observation_policy_missing")
    strengths = (
        _evidence_strengths(
            features=features,
            cloud_suppressed=cloud_suppressed,
            precipitation_present=precipitation_present,
            cb_present=cb_present,
            policy=temporal_policy,
        )
        if isinstance(temporal_policy, Mapping) and temporal_context is not None
        else {}
    )
    correlation_policy = (
        temporal_policy.get("event_correlation_policy")
        if isinstance(temporal_policy, Mapping)
        else None
    )
    if temporal_context is not None and not isinstance(correlation_policy, Mapping):
        raise ValueError("event_correlation_policy_missing")
    coupling_policy = (
        temporal_policy.get("adverse_y_coupling_policy")
        if isinstance(temporal_policy, Mapping)
        else None
    )
    if temporal_context is not None and not isinstance(coupling_policy, Mapping):
        raise ValueError("adverse_y_coupling_policy_missing")
    event_information_weight, correlation_trace = (
        _event_information_weight(
            prediction=prediction,
            cloud_suppressed=cloud_suppressed,
            precipitation_present=precipitation_present,
            cb_present=cb_present,
            tcu_present=features.convective_evidence_kind == "tcu",
            physical_event_cluster_signature=features.physical_event_cluster_signature,
            policy=correlation_policy,
        )
        if isinstance(correlation_policy, Mapping)
        else (1.0, {"event_information_weight": 1.0, "hard_bounds_discounted": False})
    )
    scores: dict[tuple[float, str], float] = {}
    component_scores: dict[tuple[float, str], dict[str, float]] = {}
    temporal_trace: dict[str, object] = {}
    raw_scores: dict[tuple[float, str], float] = {}
    raw_components: dict[tuple[float, str], tuple[float, float, float]] = {}
    maturity_by_y: dict[float, float] = {}
    for cell in prediction.joint:
        model = models[cell.state]
        assert isinstance(model, Mapping)
        signature_probabilities = model["weather_signature_probabilities"]
        assert isinstance(signature_probabilities, Mapping)
        weather_probability = float(
            signature_probabilities.get(
                features.weather_signature, signature_probabilities["__other__"]
            )
        )
        continuous = _continuous_log_density(
            values,
            list(model["continuous_mean"]),
            [list(row) for row in model["continuous_covariance"]],
            measurement_covariance,
            robust_radius,
        )
        momentum = _continuous_log_density(
            (values[0], None),
            list(model["continuous_mean"]),
            [list(row) for row in model["continuous_covariance"]],
            measurement_covariance,
            robust_radius,
        )
        # The continuous model is joint in rate and dewpoint depression.  Use
        # p(rate) plus the exact residual p(dewpoint|rate) decomposition so
        # covariance and robust-tail interaction are counted once, not lost.
        physical_continuous = continuous - momentum
        phase_adjustment = 0.0
        if temporal_context is not None and isinstance(temporal_policy, Mapping):
            condition = temporal_context.conditions_by_y.get(cell.y_c)
            if condition is None:
                raise ValueError(f"observation_temporal_candidate_missing:{cell.y_c:g}")
            maturity = _candidate_lock_maturity(
                context=temporal_context, condition=condition, policy=temporal_policy
            )
            maturity_by_y[cell.y_c] = maturity
            phase_adjustment = _phase_adjustment(
                state=cell.state, maturity=maturity, strengths=strengths, policy=temporal_policy,
                forecast_regime_probabilities=temporal_context.forecast_regime_probabilities,
            )
            temporal_trace[f"{cell.y_c:g}"] = {
                "lock_maturity": maturity,
                "forecast_rate_c_per_hour": condition.forecast_rate_c_per_hour,
                "future_max_increment_c": condition.future_max_increment_c,
                "minutes_to_curve_peak": condition.minutes_to_curve_peak,
                "forcing_availability": condition.forcing_availability,
            }
        weather = math.log(weather_probability)
        raw_total_score = continuous + weather + phase_adjustment
        raw_scores[(cell.y_c, cell.state)] = raw_total_score
        raw_components[(cell.y_c, cell.state)] = (
            momentum,
            physical_continuous + weather,
            phase_adjustment,
        )
    adverse_strength = float(strengths.get("adverse", 0.0)) if strengths else 0.0
    tempered_scores = {
        key: value * event_information_weight for key, value in raw_scores.items()
    }
    guarded_scores = tempered_scores
    coupling_trace: dict[str, object] = {
        "applied": False,
        "reason": "no_adverse_physical_evidence",
        "hard_bounds_discounted": False,
    }
    if adverse_strength > 0.0 and isinstance(coupling_policy, Mapping):
        guarded_scores, coupling_trace = _adverse_candidate_coupling_guard(
            prediction=prediction,
            tempered_scores=tempered_scores,
            maturity_by_y=maturity_by_y,
            adverse_strength=adverse_strength,
            reported_temperature_c=reported_temperature_c,
            policy=coupling_policy,
        )
    for cell in prediction.joint:
        key = (cell.y_c, cell.state)
        raw_momentum, raw_physical, raw_temporal = raw_components[key]
        coupling_adjustment = guarded_scores[key] - tempered_scores[key]
        momentum = raw_momentum * event_information_weight
        physical_continuous = raw_physical * event_information_weight
        phase_adjustment = raw_temporal * event_information_weight + coupling_adjustment
        total_score = guarded_scores[key]
        scores[(cell.y_c, cell.state)] = total_score
        component_scores[(cell.y_c, cell.state)] = {
            "temperature_momentum_log_score": momentum,
            "physical_fields_log_score": physical_continuous,
            "temporal_adjustment_log_score": phase_adjustment,
            "total_log_emission": total_score,
            "raw_total_log_emission": raw_scores[key],
            "tempered_total_log_emission_before_y_centering": tempered_scores[key],
            "latent_y_coupling_adjustment_log_score": coupling_adjustment,
            "event_information_weight": event_information_weight,
        }
    return CanonicalObservationEmission(
        event_id=prediction.event_id,
        pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
        event_valid_at=prediction.event_valid_at,
        is_regular_metar=is_regular_metar,
        reported_temperature_c=reported_temperature_c,
        cloud_suppressed=cloud_suppressed,
        precipitation_present=precipitation_present,
        cb_present=cb_present,
        log_emission_by_cell=scores,
        observation_artifact_sha256=str(artifact["artifact_sha256"]),
        tcu_present=features.convective_evidence_kind == "tcu",
        convective_evidence_kind=features.convective_evidence_kind,
        physical_event_cluster_signature=features.physical_event_cluster_signature,
        physical_event_cluster_change_point=bool(
            correlation_trace.get("physical_event_cluster_change_point")
        ),
        event_information_weight=event_information_weight,
        observation_feature_trace={
            "current_step_rate_c_per_hour": features.current_step_rate_c_per_hour,
            "current_step_rate_interval_c_per_hour": features.current_step_rate_interval_c_per_hour,
            "dewpoint_depression_c": features.dewpoint_depression_c,
            "dewpoint_depression_interval_c": features.dewpoint_depression_interval_c,
            "continuous_measurement_covariance": measurement_covariance,
            "weather_signature": features.weather_signature,
            "convective_evidence_kind": features.convective_evidence_kind,
            "physical_event_cluster_signature": features.physical_event_cluster_signature,
            "visibility_m": features.visibility_m,
            "fog_or_mist_present": features.fog_or_mist_present,
            "robust_mahalanobis_radius": None if not math.isfinite(robust_radius) else robust_radius,
            "likelihood_policy": (
                "quantization_covariance_plus_huber_mahalanobis"
                if isinstance(policy, Mapping)
                else "candidate_legacy_point_gaussian"
            ),
            "time_conditioned_likelihood": (
                {
                    "schema": temporal_policy["schema"],
                    "local_time": temporal_context.local_time,
                    "local_hour": temporal_context.local_hour,
                    "solar_elevation_sine": temporal_context.solar_elevation_sine,
                    "hours_until_sunset": temporal_context.hours_until_sunset,
                    "evidence_strengths": strengths,
                    "forecast_regime_probabilities": dict(temporal_context.forecast_regime_probabilities),
                    "forecast_regime_unknown_probability": temporal_context.forecast_regime_unknown_probability,
                    "candidate_conditions": temporal_trace,
                    "forecast_role": "conditioning_modulator_not_independent_likelihood",
                    "event_correlation_discount": correlation_trace,
                    "adverse_y_coupling_guard": coupling_trace,
                }
                if temporal_context is not None and isinstance(temporal_policy, Mapping)
                else None
            ),
        },
        log_component_by_cell=component_scores,
    )


__all__ = ["ObservationFeatureVector", "build_joint_observation_emission"]
