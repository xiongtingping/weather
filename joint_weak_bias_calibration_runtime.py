"""Immutable, forecast-only bias states for the joint weak likelihood.

This module never consumes observations available on D0, markets, or orders.
It fits closed physical-X residuals offline and emits an artifact payload that
the weak model can use only as a frozen centre correction.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "joint_weak_bias_calibration_v1"
ALLOWED_EWMA_ALPHAS = (0.03, 0.05)
DEFAULT_EWMA_ALPHA = 0.05
MINIMUM_EVENT_DAYS = 30
BIAS_CLAMP_C = 1.5
ROBUST_SCALE_FLOOR_C = 0.05
_LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _clamp(value: float, limit: float = BIAS_CLAMP_C) -> float:
    return max(-limit, min(limit, float(value)))


def robust_scale(values: Sequence[float]) -> float:
    """MAD scale from actual residuals; never infer sigma from MAE."""

    if not values:
        return ROBUST_SCALE_FLOOR_C
    centre = median(values)
    mad = median([abs(value - centre) for value in values])
    return max(ROBUST_SCALE_FLOOR_C, 1.4826 * float(mad))


def _horizon_and_bin(*, decision_as_of: str, lead_bucket: str) -> tuple[str, str]:
    lead = _text(lead_bucket).lower()
    if lead.startswith("d1") or "minus" in lead or lead.startswith("d_"):
        return "d1", "d1"
    moment = _time(decision_as_of)
    if moment is None:
        return "d0", "d0_unknown"
    hour = moment.astimezone(_LOCAL_ZONE).hour
    if hour < 6:
        return "d0", "00_06"
    if hour < 12:
        return "d0", "06_12"
    if hour < 15:
        return "d0", "12_15"
    return "d0", "15_close"


def _ewma(rows: Sequence[dict[str, Any]], *, alpha: float) -> float:
    residuals = [float(item["residual_c"]) for item in rows]
    centre = float(median(residuals)) if residuals else 0.0
    scale = robust_scale(residuals)
    state = 0.0
    for row in sorted(rows, key=lambda item: (item["event_date"], item["decision_as_of"], item["source_revision_id"])):
        # Winsorize around the frozen residual centre. Clipping around zero
        # would erase a stable systematic hot/cold bias whenever MAD is small.
        raw = float(row["residual_c"])
        value = max(centre - scale, min(centre + scale, raw))
        state = (1.0 - alpha) * state + alpha * value
    return state


def _deduplicated_rows(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One latest revision per source × event day × D0 bin, or D1 day."""

    candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    for sample in samples:
        event = dict(sample) if isinstance(sample, Mapping) else {}
        target = _finite(event.get("physical_x_final_c"))
        event_date = _text(event.get("event_date"))[:10]
        decision_as_of = _text(event.get("decision_as_of"))
        context = event.get("forecast_context") if isinstance(event.get("forecast_context"), Mapping) else {}
        if target is None or not event_date or _time(decision_as_of) is None:
            continue
        horizon, d0_bin = _horizon_and_bin(decision_as_of=decision_as_of, lead_bucket=_text(context.get("lead_bucket")))
        for revision in event.get("source_revisions") if isinstance(event.get("source_revisions"), Sequence) else []:
            source = _text(revision.get("source_id")) if isinstance(revision, Mapping) else ""
            peak = _finite(revision.get("forecast_peak_c")) if isinstance(revision, Mapping) else None
            if not source or peak is None:
                continue
            row = {
                "source_id": source,
                "event_date": event_date,
                "decision_as_of": decision_as_of,
                "source_revision_id": _text(revision.get("source_revision_id")),
                "season": _text(context.get("season")),
                "weather_regime": _text(context.get("weather_regime")),
                "horizon": horizon,
                "d0_time_bin": d0_bin,
                # Contract sign: positive means this source predicted too hot.
                "residual_c": peak - target,
            }
            key = (source, event_date, d0_bin if horizon == "d0" else "d1")
            old = candidates.get(key)
            if old is None or (row["decision_as_of"], row["source_revision_id"]) > (old["decision_as_of"], old["source_revision_id"]):
                candidates[key] = row
    return sorted(candidates.values(), key=lambda item: (item["event_date"], item["decision_as_of"], item["source_id"]))


def _state_id(level: str, key: Sequence[str]) -> str:
    return f"{level}:" + "|".join(key)


def fit_joint_weak_bias_calibration(
    *, samples: Sequence[Mapping[str, Any]], ewma_alpha: float = DEFAULT_EWMA_ALPHA,
    minimum_event_days: int = MINIMUM_EVENT_DAYS,
) -> dict[str, Any]:
    """Fit zero-centred, partial-pooled bias states from closed physical X only."""

    if ewma_alpha not in ALLOWED_EWMA_ALPHAS:
        raise ValueError("bias_ewma_alpha_not_preregistered")
    minimum = max(1, int(minimum_event_days))
    rows = _deduplicated_rows(samples)
    source_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    season_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    context_groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        source_groups[(row["source_id"], row["horizon"])].append(row)
        season_groups[(row["source_id"], row["season"], row["horizon"], row["d0_time_bin"])].append(row)
        context_groups[(row["source_id"], row["season"], row["weather_regime"], row["horizon"], row["d0_time_bin"])].append(row)

    # tau_global is learned per horizon from source-group EWMA estimates.
    group_ewmas = {key: _ewma(group, alpha=ewma_alpha) for key, group in source_groups.items()}
    tau_global: dict[str, float] = {}
    for horizon in {key[1] for key in source_groups}:
        tau_global[horizon] = robust_scale([value for key, value in group_ewmas.items() if key[1] == horizon])

    source_states: dict[str, dict[str, Any]] = {}
    source_bias: dict[tuple[str, str], float] = {}
    for key, group in sorted(source_groups.items()):
        source, horizon = key
        days = len({row["event_date"] for row in group})
        sigma = robust_scale([float(row["residual_c"]) for row in group])
        raw = group_ewmas[key]
        tau = tau_global[horizon]
        shrinkage = (tau * tau) / (tau * tau + sigma * sigma / max(days, 1)) if days >= minimum else 0.0
        bias = _clamp(shrinkage * raw) if days >= minimum else 0.0
        source_bias[key] = bias
        source_states[_state_id("source_global", key)] = {
            "level": "source_global", "source_id": source, "horizon": horizon,
            "event_day_count": days, "robust_scale_c": sigma, "tau_global_c": tau,
            "residual_median_c": float(median([float(row["residual_c"]) for row in group])),
            "ewma_alpha": ewma_alpha, "ewma_residual_c": raw, "shrinkage": shrinkage,
            "bias_c": bias, "fallback": "zero" if days < minimum else None,
        }

    # The contract's primary context is source × season × horizon/bin.  The
    # weather-regime layer below is only a finer refinement and can never skip
    # this season parent.
    season_ewmas = {key: _ewma(group, alpha=ewma_alpha) for key, group in season_groups.items()}
    tau_season: dict[tuple[str, str], float] = {}
    for source, horizon in source_groups:
        values = [value - source_bias[(source, horizon)] for key, value in season_ewmas.items() if key[0] == source and key[2] == horizon]
        tau_season[(source, horizon)] = robust_scale(values)
    season_states: dict[str, dict[str, Any]] = {}
    season_bias: dict[tuple[str, str, str, str], float] = {}
    for key, group in sorted(season_groups.items()):
        source, season, horizon, time_bin = key
        days = len({row["event_date"] for row in group})
        sigma = robust_scale([float(row["residual_c"]) for row in group])
        parent = source_bias[(source, horizon)]
        tau = tau_season[(source, horizon)]
        shrinkage = (tau * tau) / (tau * tau + sigma * sigma / max(days, 1)) if days >= minimum else 0.0
        bias = _clamp(shrinkage * season_ewmas[key] + (1.0 - shrinkage) * parent) if days >= minimum else parent
        season_bias[key] = bias
        season_states[_state_id("season", key)] = {
            "level": "source_season_horizon_bin", "source_id": source, "season": season,
            "horizon": horizon, "d0_time_bin": time_bin, "event_day_count": days,
            "robust_scale_c": sigma, "tau_context_c": tau, "ewma_alpha": ewma_alpha,
            "residual_median_c": float(median([float(row["residual_c"]) for row in group])),
            "ewma_residual_c": season_ewmas[key], "parent_source_bias_c": parent,
            "shrinkage": shrinkage, "bias_c": bias,
            "fallback": "source_global" if days < minimum else None,
        }

    # tau_context measures residual variation of context estimates around their
    # parent source state, then each small context automatically falls back.
    context_ewmas = {key: _ewma(group, alpha=ewma_alpha) for key, group in context_groups.items()}
    tau_context: dict[tuple[str, str], float] = {}
    for source, horizon in source_groups:
        values = [value - season_bias[(key[0], key[1], key[3], key[4])] for key, value in context_ewmas.items() if key[0] == source and key[3] == horizon]
        tau_context[(source, horizon)] = robust_scale(values)
    context_states: dict[str, dict[str, Any]] = {}
    for key, group in sorted(context_groups.items()):
        source, season, regime, horizon, time_bin = key
        days = len({row["event_date"] for row in group})
        sigma = robust_scale([float(row["residual_c"]) for row in group])
        parent = season_bias[(source, season, horizon, time_bin)]
        tau = tau_context[(source, horizon)]
        shrinkage = (tau * tau) / (tau * tau + sigma * sigma / max(days, 1)) if days >= minimum else 0.0
        bias = _clamp(shrinkage * context_ewmas[key] + (1.0 - shrinkage) * parent) if days >= minimum else parent
        context_states[_state_id("context", key)] = {
            "level": "source_season_weather_horizon_bin", "source_id": source, "season": season,
            "weather_regime": regime, "horizon": horizon, "d0_time_bin": time_bin,
            "event_day_count": days, "robust_scale_c": sigma, "tau_context_c": tau,
            "residual_median_c": float(median([float(row["residual_c"]) for row in group])),
            "ewma_alpha": ewma_alpha, "ewma_residual_c": context_ewmas[key],
            "parent_season_bias_c": parent, "shrinkage": shrinkage, "bias_c": bias,
            "fallback": "source_season" if days < minimum else None,
        }
    return {
        "schema": SCHEMA_VERSION,
        "residual_definition": "forecast_peak_c_minus_physical_x_final_c",
        "source_data_policy": "closed_physical_x_only_no_d0_observation_or_market",
        "state_asof_policy": "training_event_date_max_strictly_before_scored_event_date",
        "ewma_alpha": ewma_alpha, "preregistered_ewma_alpha_candidates": list(ALLOWED_EWMA_ALPHAS),
        "minimum_independent_event_days": minimum, "bias_clamp_c": BIAS_CLAMP_C,
        "robust_clip_method": "winsorize_around_frozen_residual_median_one_robust_scale",
        "d0_deduplication": "latest_strict_asof_revision_per_source_event_day_d0_time_bin",
        "source_global_states": source_states, "season_states": season_states, "context_states": context_states,
        "training_row_count": len(rows), "training_event_day_count": len({row["event_date"] for row in rows}),
        "training_event_date_min": min((row["event_date"] for row in rows), default=None),
        "training_event_date_max": max((row["event_date"] for row in rows), default=None),
        "promotion_note": "shadow_candidate_requires_walk_forward_alpha_selection_and_review",
    }


def select_joint_weak_bias_state(
    *, calibration: Mapping[str, Any], source_id: str, season: str, weather_regime: str, lead_bucket: str,
    decision_as_of: str | None = None,
) -> dict[str, Any]:
    """Resolve context -> source-global -> explicit zero, with full audit trace."""

    decision = decision_as_of or "1970-01-01T00:00:00+00:00"
    horizon, time_bin = _horizon_and_bin(decision_as_of=decision, lead_bucket=lead_bucket)
    context_id = _state_id("context", (source_id, season, weather_regime, horizon, time_bin))
    context = calibration.get("context_states", {}).get(context_id) if isinstance(calibration.get("context_states"), Mapping) else None
    if isinstance(context, Mapping) and context.get("fallback") is None:
        return {"bias_c": float(context["bias_c"]), "bias_level": _text(context.get("level")), "bias_state_id": context_id, "bias_sample_count": int(context["event_day_count"]), "bias_fallback": None}
    season_id = _state_id("season", (source_id, season, horizon, time_bin))
    season_state = calibration.get("season_states", {}).get(season_id) if isinstance(calibration.get("season_states"), Mapping) else None
    if isinstance(season_state, Mapping) and season_state.get("fallback") is None:
        return {"bias_c": float(season_state["bias_c"]), "bias_level": _text(season_state.get("level")), "bias_state_id": season_id, "bias_sample_count": int(season_state["event_day_count"]), "bias_fallback": "weather_regime_insufficient"}
    # Sparse D0 bins fall back to the corresponding D1 seasonal state. This
    # prevents a handful of same-day revisions from becoming an independent
    # calibration population.
    if horizon == "d0":
        d1_season_id = _state_id("season", (source_id, season, "d1", "d1"))
        d1_season = calibration.get("season_states", {}).get(d1_season_id) if isinstance(calibration.get("season_states"), Mapping) else None
        if isinstance(d1_season, Mapping) and d1_season.get("fallback") is None:
            return {"bias_c": float(d1_season["bias_c"]), "bias_level": _text(d1_season.get("level")), "bias_state_id": d1_season_id, "bias_sample_count": int(d1_season["event_day_count"]), "bias_fallback": "d0_context_insufficient_used_d1_season"}
    source_id_key = _state_id("source_global", (source_id, horizon))
    source = calibration.get("source_global_states", {}).get(source_id_key) if isinstance(calibration.get("source_global_states"), Mapping) else None
    if isinstance(source, Mapping) and source.get("fallback") is None:
        return {"bias_c": float(source["bias_c"]), "bias_level": _text(source.get("level")), "bias_state_id": source_id_key, "bias_sample_count": int(source["event_day_count"]), "bias_fallback": "season_context_insufficient"}
    if horizon == "d0":
        d1_source_id = _state_id("source_global", (source_id, "d1"))
        d1_source = calibration.get("source_global_states", {}).get(d1_source_id) if isinstance(calibration.get("source_global_states"), Mapping) else None
        if isinstance(d1_source, Mapping) and d1_source.get("fallback") is None:
            return {"bias_c": float(d1_source["bias_c"]), "bias_level": _text(d1_source.get("level")), "bias_state_id": d1_source_id, "bias_sample_count": int(d1_source["event_day_count"]), "bias_fallback": "d0_source_insufficient_used_d1_source"}
    return {"bias_c": 0.0, "bias_level": "zero", "bias_state_id": "zero", "bias_sample_count": 0, "bias_fallback": "source_global_insufficient"}


__all__ = ["ALLOWED_EWMA_ALPHAS", "BIAS_CLAMP_C", "DEFAULT_EWMA_ALPHA", "MINIMUM_EVENT_DAYS", "SCHEMA_VERSION", "fit_joint_weak_bias_calibration", "robust_scale", "select_joint_weak_bias_state"]
