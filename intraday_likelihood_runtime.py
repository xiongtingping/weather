"""Intraday likelihood helper runtime for hierarchical temperature posterior.

This module owns D0 physical quality scaling, observed temperature trajectory
realization likelihood, and post-cool rebound conditional likelihood helpers. It
is read-only and intentionally avoids the retired additive evidence field names
that belonged to the retired additive runtime.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

import posterior_pipeline_entry_runtime as bpr

CTP_VERSION = bpr.CTP_VERSION
DEFAULT_BAYES_STAGE_Z = bpr.DEFAULT_BAYES_STAGE_Z
EPS = bpr.EPS
_d0_physical_time_weight = bpr._d0_physical_time_weight
_env_float = bpr._env_float
_feature_bool = bpr._feature_bool
_feature_float = bpr._feature_float
_group_log_likelihoods = bpr._group_log_likelihoods
_label_unit = bpr._label_unit
_normalize_text = bpr._normalize_text
_observed_path_temp_from_features = bpr._observed_path_temp_from_features
_physical_variable_current_value = bpr._physical_variable_current_value
_physical_variable_trend = bpr._physical_variable_trend
_probability_shift_from_group = bpr._probability_shift_from_group
_radar_echo_source_profile = bpr._radar_echo_source_profile
_target_bucket_text = bpr._target_bucket_text
_temperature_momentum_posterior_gate = bpr._temperature_momentum_posterior_gate
_temperature_momentum_variable = bpr._temperature_momentum_variable
_atomic_temperature_bucket_labels = bpr._atomic_temperature_bucket_labels
_clamp01 = bpr._clamp01
_compact_physical_likelihood_component_groups = bpr._compact_physical_likelihood_component_groups
_compact_physical_likelihood_components = bpr._compact_physical_likelihood_components
_extract_likelihood_features = bpr._extract_likelihood_features
_feature_date_text = bpr._feature_date_text
_feature_text = bpr._feature_text
_first_feature_number = bpr._first_feature_number
_normal_bucket_probs = bpr._normal_bucket_probs
_normalize = bpr._normalize
_physical_likelihood_scope_meta = bpr._physical_likelihood_scope_meta
_soft_forecast_scope_meta = bpr._soft_forecast_scope_meta
_taf_main_posterior_enabled = bpr._taf_main_posterior_enabled
_taf_time_scale = bpr._taf_time_scale
_taf_walk_forward_calibrated = bpr._taf_walk_forward_calibrated
_to_bool = bpr._to_bool
_to_float = bpr._to_float
_weather_com_forecast_time_scale = bpr._weather_com_forecast_time_scale
_weighted_model_probability = bpr._weighted_model_probability
_wu_forecast_time_scale = bpr._wu_forecast_time_scale
bucket_probabilities_by_label = bpr.bucket_probabilities_by_label
build_temperature_posterior_distribution = bpr.build_temperature_posterior_distribution
cap_climo_evidence_by_pp_delta = bpr.cap_climo_evidence_by_pp_delta
climo_regime_bucket_probabilities = bpr.climo_regime_bucket_probabilities
climo_regime_explanation = bpr.climo_regime_explanation
climo_regime_pp_cap = bpr.climo_regime_pp_cap
climo_runtime = bpr.climo_runtime
compute_weather_likelihood_vector = bpr.compute_weather_likelihood_vector
estimate_peak_temperature_from_features = bpr.estimate_peak_temperature_from_features
resolve_hard_source_posterior_override = bpr.resolve_hard_source_posterior_override
resolve_lcb_stage_z = bpr.resolve_lcb_stage_z


def physical_likelihood_quality_scale(
    features: Mapping[str, Any],
    meta: Mapping[str, Any],
    probs: List[float],
) -> Tuple[float, str, Dict[str, Any]]:
    if not _to_bool(os.environ.get("PM_BAYES_LIKELIHOOD_EVIDENCE_QUALITY_GATE_ENABLED"), default=True):
        return 1.0, "quality_gate_disabled", {}

    source = _normalize_text(features.get("source"))
    aux_source = _normalize_text(features.get("auxiliary_source"))
    max_prob = max(probs) if probs else 0.0
    extreme_threshold = _env_float("PM_BAYES_LIKELIHOOD_EXTREME_PROB_THRESHOLD", 0.90)
    proxy_scale = max(0.0, min(1.0, _env_float("PM_BAYES_LIKELIHOOD_PROXY_ONLY_EVIDENCE_SCALE", 0.35)))
    extreme_weak_scale = max(0.0, min(1.0, _env_float("PM_BAYES_LIKELIHOOD_EXTREME_WEAK_EVIDENCE_SCALE", 0.25)))
    sparse_core_scale = max(0.0, min(1.0, _env_float("PM_BAYES_LIKELIHOOD_SPARSE_CORE_EVIDENCE_SCALE", 0.50)))
    min_core_groups = max(0.0, _env_float("PM_BAYES_LIKELIHOOD_MIN_CORE_GROUPS_FULL_WEIGHT", 3.0))

    observed_temp = _observed_path_temp_from_features(features)
    observed_hour = _feature_float(features, "observation_hour_local", "hour_local", "local_hour")
    forecast_peak = _feature_float(features, "forecast_peak_temp_c", "forecast_peak_temp", "open_meteo_peak_temp_c")
    temp_trajectory_anchor = observed_temp is not None and observed_hour is not None and forecast_peak is not None
    observed_radiation_anchor = _feature_float(
        features,
        "solar_radiation_observed",
        "shortwave_radiation_observed",
        "direct_radiation_observed",
    ) is not None
    observed_precip_anchor = (
        _feature_float(
            features,
            "radar_precip_rate_mm_per_h",
            "qpe_precip_mm",
            "observed_precip_rate_mm_per_h",
            "observed_precipitation_mm",
        )
        is not None
        or _feature_bool(features, "is_raining_observed", "radar_precip_observed", "metar_rain_observed")
    )
    final_max_anchor = _feature_float(features, "observed_today_max_c", "daily_max_observed_c") is not None and _feature_bool(
        features,
        "observed_today_max_trusted",
        "observed_today_max_is_official",
        "observed_today_max_is_station_observed",
        "observed_today_max_is_final",
        "temperature_observation_final_daily_max",
    )
    strong_anchor = bool(temp_trajectory_anchor or observed_radiation_anchor or observed_precip_anchor or final_max_anchor)

    physical_evidence = meta.get("physical_evidence") if isinstance(meta.get("physical_evidence"), Mapping) else {}
    availability = physical_evidence.get("availability") if isinstance(physical_evidence.get("availability"), Mapping) else {}
    core_groups = (
        "temperature_trajectory",
        "radiation",
        "cloud_shading",
        "precipitation_evaporative_cooling",
    )
    core_available_count = 0
    for group in core_groups:
        entry = availability.get(group) if isinstance(availability, Mapping) else None
        if isinstance(entry, Mapping) and bool(entry.get("available")):
            core_available_count += 1

    scale = 1.0
    reasons: List[str] = []
    if source == "scan_model_members" and not strong_anchor:
        scale = min(scale, proxy_scale)
        reasons.append("proxy_model_members_without_strong_observed_anchor")
    if max_prob >= extreme_threshold and not strong_anchor:
        scale = min(scale, extreme_weak_scale)
        reasons.append("extreme_likelihood_without_strong_observed_anchor")
    if core_available_count < min_core_groups and not strong_anchor:
        scale = min(scale, sparse_core_scale)
        reasons.append("core_physical_evidence_groups_sparse")
    if "error" in aux_source and not strong_anchor:
        scale = min(scale, proxy_scale)
        reasons.append("auxiliary_physical_source_error_without_strong_anchor")

    details = {
        "source": source,
        "auxiliary_source": aux_source,
        "max_physical_likelihood_prob": float(max_prob),
        "strong_observed_anchor": strong_anchor,
        "temperature_trajectory_anchor": temp_trajectory_anchor,
        "observed_radiation_anchor": observed_radiation_anchor,
        "observed_precipitation_anchor": observed_precip_anchor,
        "final_observed_max_anchor": final_max_anchor,
        "core_available_group_count": core_available_count,
    }
    return max(0.0, min(1.0, float(scale))), ",".join(reasons) if reasons else "full_weight", details


def temperature_trajectory_realization_probs(
    labels: List[str],
    features: Mapping[str, Any],
    *,
    rounding_rule: Optional[str],
) -> Tuple[List[float], List[float], Dict[str, Any]]:
    if not _to_bool(os.environ.get("PM_BAYES_TEMPERATURE_TRAJECTORY_POSTERIOR_ENABLED"), default=True):
        return [0.0] * len(labels), [0.0] * len(labels), {"enabled": False, "status": "disabled"}
    if not labels or not all(labels):
        return [0.0] * len(labels), [0.0] * len(labels), {"enabled": False, "status": "missing_labels"}
    if not isinstance(features, Mapping) or not features:
        return [0.0] * len(labels), [0.0] * len(labels), {"enabled": False, "status": "missing_features"}
    if not any(_label_unit(label) in {"C", "F"} for label in labels):
        market_var = _normalize_text(features.get("market_var"))
        if "temp" not in market_var and "temperature" not in market_var:
            return [0.0] * len(labels), [0.0] * len(labels), {"enabled": False, "status": "not_temperature_market"}

    peak_meta = features.get("peak_estimator") if isinstance(features.get("peak_estimator"), Mapping) else None
    if not isinstance(peak_meta, Mapping):
        peak_meta = estimate_peak_temperature_from_features(features)
    if not isinstance(peak_meta, Mapping) or not peak_meta.get("enabled"):
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": str((peak_meta or {}).get("source") or "peak_estimator_disabled"),
            "peak_estimator": dict(peak_meta or {}),
        }

    observed_c = _feature_float(features, "observed_temp_c", "current_temp_c", "temperature_now_c", "metar_temperature_c")
    if observed_c is None:
        observed_c = _feature_float(features, "observed_temp", "current_temp", "temperature_now")
    hour_local = _feature_float(features, "observation_hour_local", "hour_local", "local_hour")
    if observed_c is None or hour_local is None:
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": "missing_observed_temperature_or_hour",
            "peak_estimator": dict(peak_meta),
        }

    energy_terms = peak_meta.get("energy_balance_terms") if isinstance(peak_meta.get("energy_balance_terms"), Mapping) else {}
    rate = _feature_float(
        features,
        "temperature_momentum_posterior_effective_c_per_hour",
        "temperature_trajectory_stable_rate_c_per_hour",
        "temperature_momentum_effective_c_per_hour",
        "temperature_rise_rate_c_per_hour",
        "observed_temp_rise_rate_c_per_hour",
        "metar_temp_rise_rate_c_per_hour",
    )
    if rate is None:
        rate = _to_float(peak_meta.get("temperature_momentum_effective_c_per_hour"))
    feature_state = str(features.get("temperature_momentum_realization_state") or "").strip().lower()
    peak_state = str(peak_meta.get("temperature_momentum_realization_state") or "").strip().lower()
    state = feature_state if feature_state and feature_state not in {"missing", "unknown"} else peak_state
    if state == "context_warming_watch":
        peak_momentum = peak_meta.get("temperature_momentum") if isinstance(peak_meta.get("temperature_momentum"), Mapping) else {}
        context_rate = _feature_float(features, "temperature_momentum_context_effective_c_per_hour")
        if context_rate is None:
            context_rate = _to_float(peak_meta.get("temperature_momentum_context_effective_c_per_hour"))
        if context_rate is None and isinstance(peak_momentum, Mapping):
            context_rate = _to_float(peak_momentum.get("context_effective_rate_c_per_hour"))
        if context_rate is not None and (rate is None or float(context_rate) > float(rate)):
            rate = float(context_rate)
    if rate is None and isinstance(energy_terms, Mapping):
        rate = _to_float(energy_terms.get("temperature_trajectory_rate_c_per_hour"))
    if rate is None or not math.isfinite(float(rate)):
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": "missing_temperature_trajectory_rate",
            "peak_estimator": dict(peak_meta),
        }

    state_scales = {
        "sustained_warming": 1.0,
        "emerging_warming": 0.78,
        "weak_warming": 0.52,
        "context_warming_watch": 0.38,
        "short_window_only": 0.0,
        "mixed": 0.35,
        "flat": 0.15,
        "not_realized": 0.0,
        "cooling": 0.0,
    }
    min_rate = max(0.0, _env_float("PM_BAYES_TEMPERATURE_TRAJECTORY_MIN_RATE_C_PER_H", 0.25))
    momentum_gate = _temperature_momentum_posterior_gate(features, state=state, rate=rate)
    if not momentum_gate["allowed"]:
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": "temperature_momentum_posterior_gate_blocked",
            "rate_c_per_hour": float(rate),
            "realization_state": state or None,
            "momentum_posterior_gate": momentum_gate,
            "peak_estimator": dict(peak_meta),
        }
    if float(rate) < min_rate:
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": "warming_rate_below_threshold",
            "rate_c_per_hour": float(rate),
            "min_rate_c_per_hour": float(min_rate),
            "realization_state": state or None,
            "peak_estimator": dict(peak_meta),
        }

    state_scale = state_scales.get(state, 0.0)
    if state_scale <= 0.0:
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": "temperature_trajectory_not_realized",
            "rate_c_per_hour": float(rate),
            "realization_state": state or None,
            "peak_estimator": dict(peak_meta),
        }

    extrapolation_c = _to_float(peak_meta.get("extrapolation_c"))
    mu_c = _to_float(peak_meta.get("mu_c"))
    if extrapolation_c is None and mu_c is None:
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": "missing_peak_estimator_mu",
            "rate_c_per_hour": float(rate),
            "realization_state": state or None,
            "peak_estimator": dict(peak_meta),
        }

    use_extrapolation = _to_bool(
        os.environ.get("PM_BAYES_TEMPERATURE_TRAJECTORY_USE_EXTRAPOLATION"),
        default=True,
    )
    selected_mu = float(extrapolation_c if use_extrapolation and extrapolation_c is not None else (mu_c if mu_c is not None else extrapolation_c))
    sigma = _to_float(peak_meta.get("extrapolation_sigma_c" if use_extrapolation else "sigma_c"))
    if sigma is None or sigma <= 0:
        sigma = _to_float(peak_meta.get("sigma_c"))
    if sigma is None or sigma <= 0:
        sigma = _env_float("PM_BAYES_TEMPERATURE_TRAJECTORY_SIGMA_C", 0.75)
    sigma = max(0.20, float(sigma))

    probs = _normal_bucket_probs(
        labels,
        mu=float(selected_mu),
        sigma=float(sigma),
        rounding_rule=rounding_rule or "rounded_degrees",
    )
    if not any(v > EPS for v in probs):
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": "trajectory_bucket_probs_empty",
            "mu_c": float(selected_mu),
            "sigma_c": float(sigma),
            "rate_c_per_hour": float(rate),
            "realization_state": state or None,
            "peak_estimator": dict(peak_meta),
        }

    atomic_probs = probs
    evidence_base = max(0.0, _env_float("PM_BAYES_TEMPERATURE_TRAJECTORY_EVIDENCE", 48.0))
    rate_full = max(min_rate, _env_float("PM_BAYES_TEMPERATURE_TRAJECTORY_FULL_RATE_C_PER_H", 0.70))
    rate_scale = max(0.0, min(1.25, float(rate) / max(EPS, float(rate_full))))
    hours_to_peak = _to_float(peak_meta.get("hours_to_peak_window"))
    if hours_to_peak is None:
        peak_hour = _feature_float(features, "local_peak_hour", "expected_peak_hour_local", "peak_hour_local")
        if peak_hour is not None and hour_local is not None:
            hours_to_peak = max(0.0, float(peak_hour) - float(hour_local))
    if hours_to_peak is None:
        time_scale = 0.75
    elif hours_to_peak <= 0.0:
        time_scale = 0.30
    elif hours_to_peak <= 3.5:
        time_scale = 1.0
    else:
        time_scale = 0.75
    source = str(
        features.get("temperature_momentum_source")
        or features.get("temperature_trajectory_stable_rate_source")
        or peak_meta.get("source")
        or "temperature_trajectory_extrapolation"
    )
    evidence_scale = max(0.0, min(1.25, float(state_scale) * float(rate_scale) * float(time_scale)))
    evidence = evidence_base * evidence_scale
    meta = {
        "enabled": bool(evidence > 0),
        "status": "enabled" if evidence > 0 else "zero_scaled",
        "source": source,
        "role": "intraday_temperature_trajectory_likelihood",
        "source_class": "airport_temperature_path",
        "derived_from": [
            "observed_temperature",
            "stable_warming_rate",
            "peak_estimator",
        ],
        "dedupe_policy": (
            "primary_intraday_temperature_signal; derived intraday channels must be "
            "incremental and pp-capped"
        ),
        "weaker_than_hard_source": True,
        "evidence": float(evidence),
        "evidence_base": float(evidence_base),
        "evidence_scale": float(evidence_scale),
        "state_scale": float(state_scale),
        "rate_scale": float(rate_scale),
        "time_scale": float(time_scale),
        "rate_c_per_hour": float(rate),
        "min_rate_c_per_hour": float(min_rate),
        "full_rate_c_per_hour": float(rate_full),
        "realization_state": state or None,
        "momentum_posterior_gate": momentum_gate,
        "mu_c": float(selected_mu),
        "sigma_c": float(sigma),
        "extrapolation_c": extrapolation_c,
        "blended_mu_c": mu_c,
        "use_extrapolation": bool(use_extrapolation),
        "observed_temp_c": float(observed_c),
        "hour_local": float(hour_local),
        "hours_to_peak_window": hours_to_peak,
        "peak_estimator": dict(peak_meta),
    }
    return probs, atomic_probs, meta


def post_cool_rebound_risk_payload(
    history: Mapping[str, Any],
    features: Mapping[str, Any],
) -> Dict[str, Any]:
    payload = history.get("post_cool_rebound_risk") if isinstance(history.get("post_cool_rebound_risk"), Mapping) else {}
    if not payload and isinstance(history.get("regime_risk"), Mapping):
        regime = history.get("regime_risk")  # type: ignore[assignment]
        payload = (
            regime.get("post_cool_rebound_risk")
            if isinstance(regime, Mapping) and isinstance(regime.get("post_cool_rebound_risk"), Mapping)
            else {}
        )
    if not payload and isinstance(features.get("post_cool_rebound_risk"), Mapping):
        payload = features.get("post_cool_rebound_risk")  # type: ignore[assignment]
    if isinstance(payload, Mapping):
        return dict(payload)
    return {}


def post_cool_rebound_channel_payload(
    history: Mapping[str, Any],
    features: Mapping[str, Any],
    *,
    channel: str,
) -> Dict[str, Any]:
    channel_key = (
        "post_cool_rebound_regime_prior"
        if channel == "regime_prior"
        else "post_cool_rebound_intraday_likelihood"
    )
    child_key = "regime_prior" if channel == "regime_prior" else "intraday_likelihood"
    for container in (history, features):
        payload = container.get(channel_key) if isinstance(container.get(channel_key), Mapping) else {}
        if isinstance(payload, Mapping) and payload:
            return dict(payload)
        risk_payload = post_cool_rebound_risk_payload(history, features)
        if isinstance(risk_payload.get(child_key), Mapping):
            return dict(risk_payload.get(child_key) or {})
        nested_features = risk_payload.get("features") if isinstance(risk_payload.get("features"), Mapping) else {}
        if isinstance(nested_features, Mapping) and isinstance(nested_features.get(child_key), Mapping):
            return dict(nested_features.get(child_key) or {})
    if channel == "intraday_likelihood":
        return post_cool_rebound_risk_payload(history, features)
    return {}


def post_cool_rebound_channel_probs(
    labels: List[str],
    history: Mapping[str, Any],
    features: Mapping[str, Any],
    *,
    rounding_rule: Optional[str],
    channel: str,
) -> Tuple[List[float], List[float], Dict[str, Any]]:
    is_regime_prior = channel == "regime_prior"
    channel_key = (
        "post_cool_rebound_regime_prior"
        if is_regime_prior
        else "post_cool_rebound_intraday_likelihood"
    )
    env_enabled_key = (
        "PM_BAYES_POST_COOL_REBOUND_REGIME_PRIOR_ENABLED"
        if is_regime_prior
        else "PM_BAYES_POST_COOL_REBOUND_INTRADAY_LIKELIHOOD_ENABLED"
    )
    enabled_default = (
        _to_bool(os.environ.get("PM_BAYES_POST_COOL_REBOUND_RISK_ENABLED"), default=True)
        if not is_regime_prior
        else True
    )
    if not _to_bool(os.environ.get(env_enabled_key), default=enabled_default):
        return [0.0] * len(labels), [0.0] * len(labels), {"enabled": False, "status": "disabled"}
    if not labels or not all(labels):
        return [0.0] * len(labels), [0.0] * len(labels), {"enabled": False, "status": "missing_labels"}
    payload = post_cool_rebound_channel_payload(history, features, channel=channel)
    risk_features = payload.get("features") if isinstance(payload.get("features"), Mapping) else payload
    if not isinstance(risk_features, Mapping) or not risk_features:
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": f"missing_{channel_key}_features",
        }
    state = str(risk_features.get("state") or payload.get("state") or "").strip().lower()
    status = str(risk_features.get("status") or payload.get("status") or "").strip()
    if is_regime_prior:
        active = bool(risk_features.get("enabled")) or state == "prior"
        if not active or state not in {"prior", "watch", "lift"}:
            return [0.0] * len(labels), [0.0] * len(labels), {
                "enabled": False,
                "status": status or state or "regime_prior_not_active",
                "state": state or None,
                "source": str(risk_features.get("source") or channel_key),
                "features": dict(risk_features),
            }
    elif state != "lift":
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": status or state or "not_lift",
            "state": state or None,
            "source": str(risk_features.get("source") or channel_key),
            "features": dict(risk_features),
        }
    if bool(risk_features.get("after_peak_gate")):
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": "after_peak_window",
            "state": state,
            "source": str(risk_features.get("source") or channel_key),
            "features": dict(risk_features),
        }
    mu_c = _to_float(
        risk_features.get("target_peak_c")
        if risk_features.get("target_peak_c") is not None
        else risk_features.get("projected_by_rate_c")
    )
    if mu_c is None:
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": "missing_target_peak_c",
            "state": state,
            "source": str(risk_features.get("source") or channel_key),
            "features": dict(risk_features),
        }
    sigma = _to_float(risk_features.get("sigma_c"))
    if sigma is None or sigma <= 0:
        sigma = _env_float("PM_BAYES_POST_COOL_REBOUND_RISK_SIGMA_C", 0.80)
    sigma = max(0.25, float(sigma))
    probs = _normal_bucket_probs(
        labels,
        mu=float(mu_c),
        sigma=float(sigma),
        rounding_rule=rounding_rule or "rounded_degrees",
    )
    if not any(v > EPS for v in probs):
        return [0.0] * len(labels), [0.0] * len(labels), {
            "enabled": False,
            "status": f"{channel_key}_bucket_probs_empty",
            "state": state,
            "source": str(risk_features.get("source") or channel_key),
            "mu_c": float(mu_c),
            "sigma_c": float(sigma),
            "features": dict(risk_features),
        }
    scale = max(0.0, min(1.0, _to_float(risk_features.get("evidence_scale")) or 0.0))
    evidence_base = max(
        0.0,
        _env_float(
            "PM_BAYES_POST_COOL_REBOUND_REGIME_PRIOR_EVIDENCE"
            if is_regime_prior
            else "PM_BAYES_POST_COOL_REBOUND_INTRADAY_LIKELIHOOD_EVIDENCE",
            2.0 if is_regime_prior else _env_float("PM_BAYES_POST_COOL_REBOUND_RISK_EVIDENCE", 8.0),
        ),
    )
    evidence = evidence_base * scale
    meta = {
        "enabled": bool(evidence > 0.0),
        "status": "enabled" if evidence > 0.0 else "zero_scaled",
        "state": state,
        "source": str(risk_features.get("source") or channel_key),
        "channel": channel_key,
        "role": "conditional_climate_prior" if is_regime_prior else "intraday_likelihood",
        "source_class": "conditional_climate_prior" if is_regime_prior else "derived_intraday_likelihood",
        "derived_from": (
            ["recent_settled_daily_tmax", "forecast_peak_setup"]
            if is_regime_prior
            else [
                "post_cool_rebound_regime_prior",
                "observed_temperature_rebound",
                "multi_window_warming_rate",
                "temperature_trajectory",
            ]
        ),
        "dedupe_policy": (
            "prior_only; must not reuse intraday observed temperature, METAR hard observation, or final settlement"
            if is_regime_prior
            else "derived from temperature trajectory; incremental only with pp cap; METAR/final settlement override later"
        ),
        "weaker_than_hard_source": True,
        "evidence": float(evidence),
        "evidence_raw": float(evidence),
        "evidence_base": float(evidence_base),
        "evidence_scale": float(scale),
        "mu_c": float(mu_c),
        "sigma_c": float(sigma),
        "target_peak_c": float(mu_c),
        "posterior_lift_pp_cap": _to_float(risk_features.get("posterior_lift_pp_cap")),
        "features": dict(risk_features),
    }
    return probs, probs, meta


def post_cool_rebound_risk_channel_probs(
    labels: List[str],
    history: Mapping[str, Any],
    features: Mapping[str, Any],
    *,
    rounding_rule: Optional[str],
) -> Tuple[List[float], List[float], Dict[str, Any]]:
    return post_cool_rebound_channel_probs(
        labels,
        history,
        features,
        rounding_rule=rounding_rule,
        channel="intraday_likelihood",
    )


def build_physical_d0_explanation(
    *,
    labels: List[str],
    rounding_rule: Optional[str],
    features: Mapping[str, Any],
    physical_likelihood_meta: Mapping[str, Any],
    base_no_physical_probs: List[float],
    physical_layer_strength: float,
    alpha_total: float,
    z_stage: Any,
    days_to_event: Optional[float],
) -> Dict[str, Any]:
    rows = (
        physical_likelihood_meta.get("rows")
        if isinstance(physical_likelihood_meta, Mapping)
        and isinstance(physical_likelihood_meta.get("rows"), list)
        else []
    )
    if not labels:
        return {"enabled": False, "status": "missing_labels", "variables": [], "combination": {}}
    if not isinstance(features, Mapping) or not features:
        return {"enabled": False, "status": "missing_features", "variables": [], "combination": {}}
    time_meta = _d0_physical_time_weight(features)
    time_weight = float(time_meta.get("weight") or 0.0)
    posterior_scale = 0.0
    if alpha_total > EPS and physical_layer_strength > 0:
        posterior_scale = min(1.0, max(0.0, float(physical_layer_strength) / float(alpha_total)))
    d0 = (_to_float(days_to_event) is not None and float(_to_float(days_to_event) or 0.0) <= 0.5)
    specs = [
        {
            "key": "cloud_cover",
            "label": "Cloud Cover 云量",
            "group": "cloud_shading",
            "reason": "云遮蔽改变午后短波入射，云量越高越压制热尾桶。",
        },
        {
            "key": "cloud_growth",
            "label": "Cloud Growth 云增长速度",
            "group": "cloud_shading",
            "reason": "云量快速增加表示午后辐射窗口收窄，偏向中低温桶。",
        },
        {
            "key": "radar_echo",
            "label": "Radar Echo 雷达/降雨回波",
            "group": "precipitation_evaporative_cooling",
            "reason": "雷达/降雨回波通过蒸发冷却和云雨维持压低继续升温概率。",
        },
        {
            "key": "dew_point",
            "label": "Dew Point 露点",
            "group": "wet_bulb_constraint",
            "reason": "露点/湿球限制低桶可行性，也会提高云雨和对流风险。",
        },
    ]
    variables: List[Dict[str, Any]] = []
    total_warm = 0.0
    total_cool = 0.0
    total_observed_warm = 0.0
    total_observed_cool = 0.0
    convection_score = 0.0
    for spec in specs:
        current = _physical_variable_current_value(features, str(spec["key"]))
        trend = _physical_variable_trend(features, str(spec["key"]))
        group_ll, group_available, na_reason, group_weight = _group_log_likelihoods(rows, str(spec["group"]), len(labels))
        if spec["key"] == "cloud_growth" and _to_float(current.get("value")) is None:
            group_available = False
            na_reason = na_reason or "cloud_growth_trend_missing"
        radar_profile: Dict[str, Any] = {}
        if spec["key"] == "radar_echo":
            radar_profile = _radar_echo_source_profile(features, current)
            if radar_profile.get("is_proxy_only"):
                group_available = False
                na_reason = (
                    "real_radar_qpe_metar_or_verified_precip_missing;"
                    f"proxy={radar_profile.get('proxy_source') or radar_profile.get('source_field') or 'unknown'}"
                )
        shift = _probability_shift_from_group(
            labels,
            base_no_physical_probs,
            group_ll,
            rounding_rule=rounding_rule,
            time_weight=time_weight,
        )
        observed_warming_pp = max(0.0, float(shift.get("warming_pp") or 0.0))
        observed_cooling_pp = max(0.0, float(shift.get("cooling_pp") or 0.0))
        if spec["key"] == "radar_echo" and radar_profile.get("is_proxy_only"):
            observed_warming_pp = 0.0
            observed_cooling_pp = 0.0
            target_buckets_override: List[str] = []
        elif spec["key"] == "radar_echo" and radar_profile.get("no_precip_confirmed"):
            observed_warming_pp = 0.0
            observed_cooling_pp = 0.0
            target_buckets_override = []
        else:
            target_buckets_override = list(shift.get("target_buckets") or [])
        if spec["key"] == "cloud_growth":
            trend_value = _to_float(current.get("value"))
            if trend_value is not None:
                trend_scale = min(1.0, abs(float(trend_value)) / 35.0)
                observed_warming_pp *= trend_scale
                observed_cooling_pp *= trend_scale
                if trend_value > 0:
                    observed_warming_pp *= 0.25
                    observed_cooling_pp = max(observed_cooling_pp, observed_warming_pp)
                elif trend_value < 0:
                    observed_cooling_pp *= 0.25
                    observed_warming_pp = max(observed_warming_pp, observed_cooling_pp)
        warming_pp = observed_warming_pp * posterior_scale
        cooling_pp = observed_cooling_pp * posterior_scale
        if group_available and max(observed_warming_pp, observed_cooling_pp) >= 0.15:
            direction = "warming" if observed_warming_pp > observed_cooling_pp + 0.05 else (
                "cooling" if observed_cooling_pp > observed_warming_pp + 0.05 else "neutral"
            )
        else:
            direction = "neutral" if group_available else "missing"
        if spec["key"] == "radar_echo" and group_available:
            rv = _to_float(current.get("value"))
            if rv is not None and rv > 0:
                convection_score += min(1.0, rv / 5.0) * 0.55
        if spec["key"] == "dew_point" and group_available:
            dv = _to_float(current.get("value"))
            if dv is not None and dv >= 22.0:
                convection_score += min(1.0, (dv - 22.0) / 6.0) * 0.30
        if spec["key"] == "cloud_growth":
            gv = _to_float(current.get("value"))
            if gv is not None and gv > 8.0:
                convection_score += min(1.0, gv / 30.0) * 0.25
        target_buckets = target_buckets_override
        variable = {
            "key": spec["key"],
            "label": (
                "Precip Proxy 降水代理"
                if spec["key"] == "radar_echo" and radar_profile.get("is_proxy_only")
                else (
                    "No Precip 无降水确认"
                    if spec["key"] == "radar_echo" and radar_profile.get("no_precip_confirmed")
                    else spec["label"]
                )
            ),
            "current": current,
            "trend": trend,
            "evidence_state": (
                "proxy"
                if spec["key"] == "radar_echo" and radar_profile.get("is_proxy_only")
                else (
                    "strong_no_precip"
                    if spec["key"] == "radar_echo" and radar_profile.get("no_precip_confirmed")
                    else ("available" if group_available else "missing")
                )
            ),
            "na_reason": "" if group_available else str(na_reason or "feature_missing"),
            "group_key": spec["group"],
            "group_weight": group_weight,
            "direction": direction,
            "warming_pp": float(warming_pp),
            "cooling_pp": float(cooling_pp),
            "observed_warming_pp": float(observed_warming_pp),
            "observed_cooling_pp": float(observed_cooling_pp),
            "neutral": bool(direction in {"neutral", "missing"} or max(warming_pp, cooling_pp) < 0.05),
            "target_buckets": target_buckets,
            "target_bucket_text": _target_bucket_text(target_buckets, direction),
            "reason": (
                "AWC/METAR、MRMS-QPE 或可验证站点已确认当前无降水，不触发蒸发冷却降温项。"
                if spec["key"] == "radar_echo" and radar_profile.get("no_precip_confirmed")
                else spec["reason"]
            ),
        }
        if radar_profile:
            variable["source_profile"] = radar_profile
        variables.append(variable)
        total_warm += warming_pp
        total_cool += cooling_pp
        total_observed_warm += observed_warming_pp
        total_observed_cool += observed_cooling_pp
    momentum_variable = _temperature_momentum_variable(features)
    variables.append(momentum_variable)
    net_pp = total_warm - total_cool
    observed_net_pp = total_observed_warm - total_observed_cool
    if convection_score >= 0.45:
        conclusion = "convection_risk"
        conclusion_label = "对流风险"
    elif net_pp > 0.10:
        conclusion = "warming"
        conclusion_label = "偏升温"
    elif net_pp < -0.10:
        conclusion = "cooling"
        conclusion_label = "偏降温"
    elif observed_net_pp > 0.20 and posterior_scale <= EPS:
        conclusion = "warming_observed_only"
        conclusion_label = "偏升温观察"
    elif observed_net_pp < -0.20 and posterior_scale <= EPS:
        conclusion = "cooling_observed_only"
        conclusion_label = "偏降温观察"
    else:
        conclusion = "neutral"
        conclusion_label = "中性"
    ranked_vars = sorted(
        variables,
        key=lambda item: max(float(item.get("warming_pp") or 0.0), float(item.get("cooling_pp") or 0.0), float(item.get("observed_warming_pp") or 0.0), float(item.get("observed_cooling_pp") or 0.0)),
        reverse=True,
    )
    combo_targets: List[str] = []
    for var in ranked_vars:
        for bucket in var.get("target_buckets") or []:
            if re.search(r"\bor\s+(?:below|higher|above|more)\b", str(bucket), re.IGNORECASE):
                continue
            if bucket not in combo_targets:
                combo_targets.append(str(bucket))
    return {
        "enabled": True,
        "status": "ok",
        "version": "physical_d0_explanation_v2",
        "charter": {
            "physical_evidence_role": "potential",
            "temperature_trajectory_role": "observed_realization",
            "temperature_momentum_role": "observed_realization_gate",
            "priority": "observed_temperature_trajectory_over_physical_potential",
            "hard_boundary": (
                "current_temp < soft_forecast - 2°C and warming_rate <= 0 blocks "
                "high-temperature bucket amplification from favorable cloud/radiation."
            ),
            "temperature_momentum": "24h/12h/6h/3h/2h/1h/45m/30m/15m/10m/5m/3m/2m/1m warming rates answer whether physical heating potential is being realized and sustained.",
        },
        "d0": bool(d0),
        "z_stage": str(z_stage or ""),
        "time_window": time_meta.get("window"),
        "hour_local": time_meta.get("hour_local"),
        "time_weight": float(time_weight),
        "posterior_scale": float(posterior_scale),
        "physical_evidence": float(physical_layer_strength),
        "variables": variables,
        "combination": {
            "conclusion": conclusion,
            "label": conclusion_label,
            "warming_pp": float(total_warm),
            "cooling_pp": float(total_cool),
            "net_pp": float(net_pp),
            "observed_warming_pp": float(total_observed_warm),
            "observed_cooling_pp": float(total_observed_cool),
            "observed_net_pp": float(observed_net_pp),
            "total_posterior_change_pp": float(net_pp),
            "convection_risk_score": float(min(1.0, convection_score)),
            "target_buckets": combo_targets[:4],
            "reason": "D0 physical residual explanation from cloud, cloud growth, radar/precipitation, and dew point.",
        },
    }
