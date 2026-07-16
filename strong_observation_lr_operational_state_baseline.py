"""Approved state-aware operational strong-likelihood baseline.

This artifact is the sole operational successor to the temperature-only
physics baseline.  It evaluates one canonical observation against each
``temperature × pre-state`` cell.  It remains deliberately low-information:
the sequential composer still owns hard conditioning, centring,
normalisation, posterior mutation and every trading decision.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Mapping

from strong_observation_lr_artifact_contract import validate_strong_observation_lr_runtime_artifact
from physical_latent_state_contract import LATENT_STATES


SCHEMA_VERSION = "strong_observation_lr_operational_state_baseline_v3"
SCORING_KIND = "operational_physical_state_aware_lr_v3_first_cb_once"
STATE_MODEL_SCHEMA = "operational_physical_state_aware_lr_v3_first_cb_once"

STATE_PROFILES: dict[str, dict[str, float]] = {
    "warming": {"step_rate": 0.9, "trend_rate": 0.7, "remaining_capacity_fraction": 0.80, "remaining_floor_c": 0.50},
    "slowing_warming": {"step_rate": 0.3, "trend_rate": 0.35, "remaining_capacity_fraction": 0.45, "remaining_floor_c": 0.25},
    "temporary_platform": {"step_rate": 0.0, "trend_rate": 0.0, "remaining_capacity_fraction": 0.20, "remaining_floor_c": 0.00},
    "temporary_cooling": {"step_rate": -0.7, "trend_rate": -0.2, "remaining_capacity_fraction": 0.45, "remaining_floor_c": 0.25},
    "cooling_then_rebound": {"step_rate": -0.4, "trend_rate": 0.15, "remaining_capacity_fraction": 0.65, "remaining_floor_c": 0.50},
    "weather_suppressed": {"step_rate": -0.25, "trend_rate": -0.2, "remaining_capacity_fraction": 0.10, "remaining_floor_c": 0.00},
    "terminal_cooling": {"step_rate": -0.9, "trend_rate": -0.55, "remaining_capacity_fraction": 0.00, "remaining_floor_c": 0.00},
    "final_locked": {"step_rate": -0.05, "trend_rate": -0.15, "remaining_capacity_fraction": 0.00, "remaining_floor_c": 0.00},
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sha(value: Any) -> str:
    material = {
        key: item for key, item in dict(_mapping(value)).items()
        if key not in {"artifact_sha256", "model_artifact_sha256", "artifact_content_sha256"}
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_operational_state_aware_strong_lr_baseline() -> dict[str, Any]:
    """Return the deterministic approved v2 runtime artifact."""

    # Imported at construction time so the production evaluator does not form
    # an import cycle through the offline fitting module used by the v1 core.
    from strong_observation_lr_operational_physics_baseline import build_operational_physics_strong_lr_baseline

    artifact = copy.deepcopy(build_operational_physics_strong_lr_baseline())
    artifact["operational_physics_parameters"].update(
        {
            "diurnal_preheating_end_local_hour": 8.0,
            "diurnal_full_heating_start_local_hour": 9.0,
            "first_cb_remaining_warming_penalty_per_c": {
                "no_cb_yet": 0.0,
                "early_before_peak_window": 0.55,
                "peak_window": 0.28,
                "late_after_peak_window": 0.08,
            },
            "first_cb_below_observed_peak_multiplier": 1.25,
        }
    )
    artifact.update(
        {
            "approval_state": "approved",
            "approval_kind": "operational_precalibration_baseline",
            "feature_builder_version": SCHEMA_VERSION,
            "scoring_kind": SCORING_KIND,
            "production_runtime_eligible": True,
            "shadow_only": False,
            "operational_state_model": {
                "schema": STATE_MODEL_SCHEMA,
                "state_profiles": STATE_PROFILES,
                "step_rate_sigma_c_per_hour": 1.25,
                "trend_rate_sigma_c_per_hour": 0.85,
                "remaining_sigma_c": 0.85,
                "state_information_scale": 0.30,
                "maximum_relative_log_penalty": 1.75,
                "relative_score_transform": "global_best_zero_soft_cap_v1",
                "early_day_terminal_penalty": 1.25,
                "late_day_warming_penalty_per_hour": 0.12,
                "precipitation_warming_penalty": 0.75,
                "clear_weather_suppressed_penalty": 0.55,
                "early_first_cb_warming_penalty": 0.90,
                "peak_window_first_cb_warming_penalty": 0.55,
                "late_first_cb_warming_penalty": 0.20,
                "dynamic_post_state_transition": True,
                "post_state_target_logit_scale": 10.0,
                "post_state_persistence": 0.05,
                "post_state_probability_floor": 0.005,
            },
            "baseline_contract": {
                "not_trained_or_oos_promoted": True,
                "strict_asof_oos_is_replacement_promotion_only": True,
                "low_information_conservative": True,
                "uses_current_event_once": True,
                "uses_pre_event_trajectory_only": True,
                "temperature_and_latent_state_are_jointly_scored": True,
                "state_profiles_cover_exact_latent_state_grid": True,
                "post_state_transition_is_event_conditioned_and_row_normalized": True,
                "identity_state_transition_fallback_forbidden": True,
                "termination_risk_is_output_marginal_not_input": True,
                "first_cb_is_temporal_evidence_not_boolean_gate": True,
                "first_cb_direct_likelihood_consumed_once": True,
                "cumulative_evidence_replacement_delta_log_lr": True,
                "first_cb_only_penalizes_unrealized_remaining_warming": True,
                "first_cb_never_reverses_observed_max_hard_condition": True,
                "tcu_is_not_silently_promoted_to_cb": True,
                "replacement_policy": "only_whole_artifact_probability_epoch_cutover",
                "legacy_temperature_only_runtime_fallback_forbidden": True,
            },
        }
    )
    identity = _sha(artifact)
    artifact["artifact_content_sha256"] = identity
    artifact["artifact_sha256"] = identity
    artifact["model_artifact_sha256"] = identity
    validation = validate_strong_observation_lr_runtime_artifact(artifact)
    if validation.get("ok") is not True:
        raise ValueError("operational_state_baseline_invalid:" + ",".join(validation.get("issues") or []))
    return artifact


def _gaussian_penalty(value: float | None, mean: float, sigma: float) -> float:
    if value is None:
        return 0.0
    return -0.5 * ((value - mean) / max(0.05, sigma)) ** 2


def score_operational_state_adjustment(
    row: Mapping[str, Any],
    model: Mapping[str, Any],
) -> tuple[float, dict[str, Any]]:
    """Return one bounded state log-LR adjustment and an audit trace."""

    state = _text(row.get("pre_state"))
    profile = _mapping(_mapping(model.get("state_profiles")).get(state))
    if state not in LATENT_STATES or not profile:
        raise ValueError("operational_state_baseline_unknown_latent_state")
    values = _mapping(row.get("feature_values"))
    categories = _mapping(row.get("feature_categories"))
    physics = _mapping(row.get("operational_physics_trace"))
    current_rate = _finite(values.get("trajectory.current_step_rate_c_per_hour"))
    trend_rates = [
        _finite(values.get("trajectory.slope_60m_c_per_hour")),
        _finite(values.get("trajectory.slope_120m_c_per_hour")),
    ]
    available_trends = [value for value in trend_rates if value is not None]
    trend_rate = sum(available_trends) / len(available_trends) if available_trends else None
    remaining = max(0.0, _finite(physics.get("candidate_remaining_warming_c")) or 0.0)
    capacity = max(0.0, _finite(physics.get("physical_remaining_capacity_c")) or 0.0)
    expected_remaining = max(
        _finite(profile.get("remaining_floor_c")) or 0.0,
        capacity * (_finite(profile.get("remaining_capacity_fraction")) or 0.0),
    )
    components = {
        "step_rate": _gaussian_penalty(current_rate, _finite(profile.get("step_rate")) or 0.0, _finite(model.get("step_rate_sigma_c_per_hour")) or 1.25),
        "trend_rate": _gaussian_penalty(trend_rate, _finite(profile.get("trend_rate")) or 0.0, _finite(model.get("trend_rate_sigma_c_per_hour")) or 0.85),
        "remaining_warming": _gaussian_penalty(remaining, expected_remaining, _finite(model.get("remaining_sigma_c")) or 0.85),
        "phase": 0.0,
        "physical_regime": 0.0,
        "first_cb_state": 0.0,
    }
    local_hour = _finite(values.get("timing.local_hour"))
    if local_hour is not None and local_hour < 12.0 and state in {"terminal_cooling", "final_locked"}:
        components["phase"] -= (_finite(model.get("early_day_terminal_penalty")) or 1.25) * (12.0 - local_hour) / 6.0
    if local_hour is not None and local_hour > 15.0 and state in {"warming", "cooling_then_rebound"}:
        components["phase"] -= (_finite(model.get("late_day_warming_penalty_per_hour")) or 0.12) * (local_hour - 15.0)
    weather = _text(categories.get("physical.weather_regime"))
    cloud = _text(categories.get("physical.cloud_regime"))
    if weather in {"precipitation", "thunderstorm"} and state in {"warming", "slowing_warming", "cooling_then_rebound"}:
        components["physical_regime"] -= _finite(model.get("precipitation_warming_penalty")) or 0.75
    if weather == "none" and cloud == "clear_or_few" and state == "weather_suppressed":
        components["physical_regime"] -= _finite(model.get("clear_weather_suppressed_penalty")) or 0.55
    first_cb_phase = _text(categories.get("physical.first_cb_timing_phase"))
    first_cb_event_state = _text(categories.get("physical.first_cb_event_state"))
    if first_cb_event_state == "first_cb_this_event" and state in {"warming", "slowing_warming", "cooling_then_rebound"}:
        if first_cb_phase == "early_before_peak_window":
            components["first_cb_state"] -= _finite(model.get("early_first_cb_warming_penalty")) or 0.90
        elif first_cb_phase == "peak_window":
            components["first_cb_state"] -= _finite(model.get("peak_window_first_cb_warming_penalty")) or 0.55
        elif first_cb_phase == "late_after_peak_window":
            components["first_cb_state"] -= _finite(model.get("late_first_cb_warming_penalty")) or 0.20
    information_scale = _finite(model.get("state_information_scale")) or 0.30
    if local_hour is None or local_hour <= 8.0:
        diurnal_information_multiplier = 0.0
    elif local_hour < 9.0:
        diurnal_information_multiplier = local_hour - 8.0
    else:
        diurnal_information_multiplier = 1.0
    raw = sum(components.values()) * information_scale * diurnal_information_multiplier
    cap = _finite(model.get("maximum_relative_log_penalty")) or 1.75
    adjustment = max(-cap, min(0.0, raw))
    return adjustment, {
        "schema": STATE_MODEL_SCHEMA,
        "pre_state": state,
        "current_step_rate_c_per_hour": current_rate,
        "trend_rate_c_per_hour": trend_rate,
        "candidate_remaining_warming_c": remaining,
        "expected_remaining_warming_c": expected_remaining,
        "first_cb_event_state": _text(categories.get("physical.first_cb_event_state")),
        "first_cb_direct_penalty_consumed_this_event": first_cb_event_state == "first_cb_this_event",
        "first_cb_timing_phase": first_cb_phase,
        "first_cb_peak_relation": _text(categories.get("physical.first_cb_peak_relation")),
        "components_before_information_scale": components,
        "information_scale": information_scale,
        "diurnal_information_multiplier": diurnal_information_multiplier,
        "unbounded_state_log_score": raw,
        "bounded_state_log_score": adjustment,
    }


__all__ = [
    "SCHEMA_VERSION",
    "SCORING_KIND",
    "STATE_MODEL_SCHEMA",
    "STATE_PROFILES",
    "build_operational_state_aware_strong_lr_baseline",
    "score_operational_state_adjustment",
]
