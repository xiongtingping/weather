"""Frozen feature vocabulary shared by strong-LR production and training."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _text(value: Any) -> str:
    return str(value or "").strip()


def _surface(default: float) -> dict[str, float]:
    return {"default": float(default)}


def _adjustment(default: float = 0.0) -> dict[str, Any]:
    return {"raw_log_score_adjustment_by_temperature_and_pre_state": _surface(default)}


def _categorical(labels: Sequence[str]) -> dict[str, Any]:
    values = {label: _adjustment() for label in sorted({_text(label) for label in labels if _text(label)})}
    values["__missing__"] = _adjustment()
    values["__other__"] = _adjustment()
    return {"kind": "categorical", "value_score_adjustments": values}


def _numeric(bins: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = [dict(row) for row in bins]
    values = {_text(row.get("label")): _adjustment() for row in normalized}
    values["__missing__"] = _adjustment()
    return {"kind": "numeric_binned", "bins": normalized, "value_score_adjustments": values}


def build_strong_observation_feature_definitions() -> dict[str, dict[str, Any]]:
    """Return the immutable runtime feature schema; training may not infer it."""

    rate_bins = (
        {"label": "rapid_cooling", "upper_exclusive": -2.0},
        {"label": "cooling", "lower_inclusive": -2.0, "upper_exclusive": -0.25},
        {"label": "near_flat", "lower_inclusive": -0.25, "upper_exclusive": 0.25},
        {"label": "warming", "lower_inclusive": 0.25, "upper_exclusive": 2.0},
        {"label": "rapid_warming", "lower_inclusive": 2.0},
    )
    return {
        "canonical.temperature_c": _numeric((
            {"label": "below_20", "upper_exclusive": 20.0},
            {"label": "20_to_25", "lower_inclusive": 20.0, "upper_exclusive": 25.0},
            {"label": "25_to_30", "lower_inclusive": 25.0, "upper_exclusive": 30.0},
            {"label": "30_to_35", "lower_inclusive": 30.0, "upper_exclusive": 35.0},
            {"label": "at_or_above_35", "lower_inclusive": 35.0},
        )),
        "timing.local_hour": _numeric((
            {"label": "overnight", "upper_exclusive": 6.0},
            {"label": "early_morning", "lower_inclusive": 6.0, "upper_exclusive": 9.0},
            {"label": "late_morning", "lower_inclusive": 9.0, "upper_exclusive": 12.0},
            {"label": "early_afternoon", "lower_inclusive": 12.0, "upper_exclusive": 15.0},
            {"label": "late_afternoon", "lower_inclusive": 15.0, "upper_exclusive": 18.0},
            {"label": "evening", "lower_inclusive": 18.0},
        )),
        "timing.first_cb_local_hour": _numeric((
            {"label": "early_before_peak_window", "upper_exclusive": 14.0},
            {"label": "peak_window", "lower_inclusive": 14.0, "upper_exclusive": 16.0},
            {"label": "late_after_peak_window", "lower_inclusive": 16.0},
        )),
        "trajectory.pre_event_path_state": _categorical(("no_prior_observation", "sustained_warming", "temporary_platform", "cooling", "rebound", "pullback_watch", "mixed_or_insufficient")),
        "trajectory.current_step_state": _categorical(("no_prior_observation", "warming", "flat", "cooling")),
        "trajectory.current_step_rate_c_per_hour": _numeric(rate_bins),
        "trajectory.slope_60m_c_per_hour": _numeric(rate_bins),
        "trajectory.slope_120m_c_per_hour": _numeric(rate_bins),
        "trajectory.acceleration_c_per_hour_squared": _numeric((
            {"label": "decelerating_fast", "upper_exclusive": -1.0},
            {"label": "decelerating", "lower_inclusive": -1.0, "upper_exclusive": -0.2},
            {"label": "steady", "lower_inclusive": -0.2, "upper_exclusive": 0.2},
            {"label": "accelerating", "lower_inclusive": 0.2, "upper_exclusive": 1.0},
            {"label": "accelerating_fast", "lower_inclusive": 1.0},
        )),
        "trajectory.observed_peak_including_current_c": _numeric((
            {"label": "below_20", "upper_exclusive": 20.0},
            {"label": "20_to_25", "lower_inclusive": 20.0, "upper_exclusive": 25.0},
            {"label": "25_to_30", "lower_inclusive": 25.0, "upper_exclusive": 30.0},
            {"label": "30_to_35", "lower_inclusive": 30.0, "upper_exclusive": 35.0},
            {"label": "at_or_above_35", "lower_inclusive": 35.0},
        )),
        "trajectory.cooling_depth_including_current_c": _numeric((
            {"label": "at_peak", "upper_exclusive": 0.5},
            {"label": "minor_pullback", "lower_inclusive": 0.5, "upper_exclusive": 1.5},
            {"label": "confirmed_cooling", "lower_inclusive": 1.5, "upper_exclusive": 3.0},
            {"label": "deep_cooling", "lower_inclusive": 3.0},
        )),
        "trajectory.cooling_duration_minutes": _numeric((
            {"label": "none_or_brief", "upper_exclusive": 30.0},
            {"label": "developing", "lower_inclusive": 30.0, "upper_exclusive": 90.0},
            {"label": "sustained", "lower_inclusive": 90.0, "upper_exclusive": 180.0},
            {"label": "persistent", "lower_inclusive": 180.0},
        )),
        "candidate.remaining_warming_c": _numeric((
            {"label": "at_observed_peak", "upper_exclusive": 0.5},
            {"label": "within_one_degree", "lower_inclusive": 0.5, "upper_exclusive": 1.5},
            {"label": "one_to_three_degrees", "lower_inclusive": 1.5, "upper_exclusive": 3.5},
            {"label": "three_to_five_degrees", "lower_inclusive": 3.5, "upper_exclusive": 5.5},
            {"label": "above_five_degrees", "lower_inclusive": 5.5},
        )),
        "physical.dewpoint_depression_c": _numeric((
            {"label": "saturated", "upper_exclusive": 2.0},
            {"label": "humid", "lower_inclusive": 2.0, "upper_exclusive": 5.0},
            {"label": "moderate", "lower_inclusive": 5.0, "upper_exclusive": 10.0},
            {"label": "dry", "lower_inclusive": 10.0},
        )),
        "physical.cloud_regime": _categorical(("overcast_or_obscured", "broken", "scattered", "clear_or_few")),
        "physical.first_cb_event_state": _categorical(("no_cb_yet", "first_cb_this_event", "cb_already_seen")),
        "physical.first_cb_timing_phase": _categorical(("no_cb_yet", "early_before_peak_window", "peak_window", "late_after_peak_window")),
        "physical.first_cb_peak_relation": _categorical(("no_cb_yet", "at_observed_peak", "below_observed_peak")),
        "physical.weather_regime": _categorical(("none", "thunderstorm", "precipitation", "visibility_reduction")),
        "source_reliability.selected_source_state": _categorical(("unknown", "verified", "degraded")),
    }


# Compatibility alias for existing training fixtures; ownership remains here.
_feature_definitions = build_strong_observation_feature_definitions


__all__ = ["build_strong_observation_feature_definitions"]
