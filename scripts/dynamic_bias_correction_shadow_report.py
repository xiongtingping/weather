#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "reports" / "chengdu_wu_model_bias_correction_latest.json"
DEFAULT_OUT = ROOT / "data" / "reports" / "dynamic_bias_correction_shadow_latest.json"


@dataclass(frozen=True)
class BiasPair:
    event_date: date
    model: str
    bias_c: float


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _mean(values: Iterable[float]) -> Optional[float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _sample_variance(values: Iterable[float]) -> Optional[float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)


def _season_for_date(day: date) -> str:
    if day.month in (12, 1, 2):
        return "winter"
    if day.month in (3, 4, 5):
        return "spring"
    if day.month in (6, 7, 8):
        return "summer"
    return "autumn"


def _doy_distance(a: date, b: date) -> int:
    diff = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(diff, 366 - diff)


def _stats_from_biases(biases: List[float]) -> Dict[str, Any]:
    if not biases:
        return {
            "n": 0,
            "mean_bias_c": None,
            "recommended_shift_c": None,
            "variance_c2": None,
            "cold_share": None,
        }
    mean_bias = sum(biases) / len(biases)
    return {
        "n": len(biases),
        "mean_bias_c": mean_bias,
        "recommended_shift_c": -mean_bias,
        "variance_c2": _sample_variance(biases),
        "cold_share": sum(1 for b in biases if b < 0.0) / len(biases),
    }


def _reliability(n: int, shrink_k: float, variance_c2: Optional[float]) -> float:
    if n <= 0:
        return 0.0
    sample_weight = float(n) / (float(n) + max(1.0, float(shrink_k)))
    if variance_c2 is None:
        variance_weight = 0.75
    else:
        # Broad, noisy residuals should still contribute, just less aggressively.
        variance_weight = 1.0 / (1.0 + math.sqrt(max(0.0, float(variance_c2))) / 3.0)
    return max(0.0, min(1.0, sample_weight * variance_weight))


def _shrunk_shift(component_shift: Optional[float], long_shift: Optional[float], reliability: float) -> Optional[float]:
    if component_shift is None:
        return None
    if long_shift is None:
        return float(component_shift)
    r = max(0.0, min(1.0, float(reliability)))
    return r * float(component_shift) + (1.0 - r) * float(long_shift)


def _component(
    name: str,
    pairs: List[BiasPair],
    *,
    long_shift: Optional[float],
    shrink_k: float,
    weight: float,
) -> Dict[str, Any]:
    biases = [p.bias_c for p in pairs]
    stats = _stats_from_biases(biases)
    rel = _reliability(int(stats["n"]), shrink_k, _to_float(stats.get("variance_c2")))
    raw_shift = _to_float(stats.get("recommended_shift_c"))
    return {
        "name": name,
        **stats,
        "shrink_k": float(shrink_k),
        "reliability": rel,
        "blend_weight": float(weight) * rel,
        "shrunk_shift_c": _shrunk_shift(raw_shift, long_shift, rel),
    }


def _static_shifts(payload: Mapping[str, Any]) -> Dict[str, float]:
    bias = payload.get("bias_correction") if isinstance(payload.get("bias_correction"), Mapping) else {}
    wu_only = bias.get("wu_only") if isinstance(bias.get("wu_only"), Mapping) else {}
    stats = wu_only.get("stats_by_model") if isinstance(wu_only.get("stats_by_model"), Mapping) else {}
    out: Dict[str, float] = {}
    for model, row in stats.items():
        if not isinstance(row, Mapping):
            continue
        shift = _to_float(row.get("recommended_temp_shift_c"))
        if shift is not None:
            out[str(model)] = float(shift)
    return out


def _load_pairs(payload: Mapping[str, Any], scope: str) -> List[BiasPair]:
    bias = payload.get("bias_correction") if isinstance(payload.get("bias_correction"), Mapping) else {}
    block = bias.get(scope) if isinstance(bias.get(scope), Mapping) else {}
    rows = block.get("pairs") if isinstance(block.get("pairs"), list) else []
    pairs: List[BiasPair] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        bias_c = _to_float(row.get("bias_c"))
        model = str(row.get("model") or "").strip()
        raw_date = str(row.get("event_date") or "").strip()
        if bias_c is None or not model or not raw_date:
            continue
        try:
            event_date = date.fromisoformat(raw_date[:10])
        except ValueError:
            continue
        pairs.append(BiasPair(event_date=event_date, model=model, bias_c=float(bias_c)))
    return pairs


def _model_shadow(
    model: str,
    pairs: List[BiasPair],
    *,
    target_date: date,
    static_shift_c: Optional[float],
    recent_days: int,
    doy_window_days: int,
    max_abs_shift_c: float,
) -> Dict[str, Any]:
    model_pairs = [p for p in pairs if p.model == model and p.event_date < target_date]
    long_stats = _stats_from_biases([p.bias_c for p in model_pairs])
    long_shift = _to_float(long_stats.get("recommended_shift_c"))
    target_season = _season_for_date(target_date)
    season_pairs = [p for p in model_pairs if _season_for_date(p.event_date) == target_season]
    month_pairs = [p for p in model_pairs if p.event_date.month == target_date.month]
    recent_pairs = [
        p for p in model_pairs if 0 <= (target_date - p.event_date).days <= int(recent_days)
    ]
    doy_pairs = [p for p in model_pairs if _doy_distance(p.event_date, target_date) <= int(doy_window_days)]

    components = [
        {
            "name": "long_term",
            **long_stats,
            "shrink_k": 0.0,
            "reliability": 1.0 if long_stats["n"] else 0.0,
            "blend_weight": 1.0 if long_stats["n"] else 0.0,
            "shrunk_shift_c": long_shift,
        },
        _component("season", season_pairs, long_shift=long_shift, shrink_k=45.0, weight=1.00),
        _component("month", month_pairs, long_shift=long_shift, shrink_k=30.0, weight=0.90),
        _component("recent", recent_pairs, long_shift=long_shift, shrink_k=20.0, weight=0.80),
        _component("day_of_year_window", doy_pairs, long_shift=long_shift, shrink_k=30.0, weight=0.70),
    ]
    weighted: List[tuple[float, float]] = []
    for comp in components:
        shift = _to_float(comp.get("shrunk_shift_c"))
        weight = _to_float(comp.get("blend_weight"))
        if shift is None or weight is None or weight <= 0:
            continue
        weighted.append((float(shift), float(weight)))
    if weighted:
        dynamic_shift = sum(s * w for s, w in weighted) / sum(w for _, w in weighted)
    else:
        dynamic_shift = static_shift_c
    if dynamic_shift is not None:
        dynamic_shift = max(-float(max_abs_shift_c), min(float(max_abs_shift_c), float(dynamic_shift)))
    delta_vs_static = None
    if dynamic_shift is not None and static_shift_c is not None:
        delta_vs_static = float(dynamic_shift) - float(static_shift_c)
    return {
        "model": model,
        "target_month": target_date.month,
        "target_season": target_season,
        "static_shift_c": static_shift_c,
        "dynamic_shift_c": dynamic_shift,
        "delta_vs_static_c": delta_vs_static,
        "components": components,
        "double_correction_guard": {
            "mode": "replace_static_shift_not_additive",
            "current_static_shift_c": static_shift_c,
            "candidate_runtime_shift_c": dynamic_shift,
            "must_not_add_static_and_dynamic": True,
        },
    }


def _mae(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(abs(float(v)) for v in values) / len(values)


def _improvement(before: Optional[float], after: Optional[float]) -> Optional[float]:
    if before is None or after is None or before <= 0:
        return None
    return (float(before) - float(after)) / float(before)


def _quantile(values: List[float], q: float) -> Optional[float]:
    vals = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = max(0.0, min(1.0, float(q))) * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _error_summary(errors: List[float]) -> Dict[str, Any]:
    if not errors:
        return {"n": 0, "mae_c": None, "mean_error_c": None, "median_error_c": None}
    vals = [float(v) for v in errors]
    return {
        "n": len(vals),
        "mae_c": _mae(vals),
        "mean_error_c": sum(vals) / len(vals),
        "median_error_c": _quantile(vals, 0.5),
    }


def _oracle_upper_bounds(
    pairs: List[BiasPair],
    static_shifts: Mapping[str, float],
) -> Dict[str, Any]:
    """Full-sample diagnostics only; these contain future information."""

    by_model: Dict[str, List[BiasPair]] = defaultdict(list)
    for pair in pairs:
        by_model[pair.model].append(pair)

    overall: Dict[str, List[float]] = defaultdict(list)
    per_model: Dict[str, Any] = {}
    for model, rows in sorted(by_model.items()):
        if model not in static_shifts:
            continue
        by_month: Dict[int, List[float]] = defaultdict(list)
        by_season: Dict[str, List[float]] = defaultdict(list)
        for row in rows:
            by_month[row.event_date.month].append(row.bias_c)
            by_season[_season_for_date(row.event_date)].append(row.bias_c)
        month_mean_shift = {month: -sum(vals) / len(vals) for month, vals in by_month.items()}
        season_mean_shift = {season: -sum(vals) / len(vals) for season, vals in by_season.items()}
        month_median_shift = {month: -float(_quantile(vals, 0.5) or 0.0) for month, vals in by_month.items()}
        season_median_shift = {
            season: -float(_quantile(vals, 0.5) or 0.0) for season, vals in by_season.items()
        }
        errors: Dict[str, List[float]] = {
            "raw": [],
            "current_static": [],
            "oracle_month_mean": [],
            "oracle_season_mean": [],
            "oracle_month_median": [],
            "oracle_season_median": [],
        }
        for row in rows:
            errors["raw"].append(row.bias_c)
            errors["current_static"].append(row.bias_c + float(static_shifts[model]))
            errors["oracle_month_mean"].append(row.bias_c + month_mean_shift[row.event_date.month])
            errors["oracle_season_mean"].append(row.bias_c + season_mean_shift[_season_for_date(row.event_date)])
            errors["oracle_month_median"].append(row.bias_c + month_median_shift[row.event_date.month])
            errors["oracle_season_median"].append(row.bias_c + season_median_shift[_season_for_date(row.event_date)])
        model_summary: Dict[str, Any] = {key: _error_summary(vals) for key, vals in errors.items()}
        static_mae = _to_float(model_summary["current_static"].get("mae_c"))
        for key, row in model_summary.items():
            row["mae_improvement_vs_current_static"] = _improvement(static_mae, _to_float(row.get("mae_c")))
            overall[key].extend(errors[key])
        per_model[model] = model_summary

    overall_summary: Dict[str, Any] = {key: _error_summary(vals) for key, vals in overall.items()}
    static_mae = _to_float(overall_summary.get("current_static", {}).get("mae_c"))
    for row in overall_summary.values():
        row["mae_improvement_vs_current_static"] = _improvement(static_mae, _to_float(row.get("mae_c")))
    return {
        "note": "diagnostic upper bound only; uses full-sample future information and must not be used as runtime evidence",
        "overall": overall_summary,
        "per_model": per_model,
    }


def _walk_forward_backtest(
    pairs: List[BiasPair],
    static_shifts: Mapping[str, float],
    *,
    recent_days: int,
    doy_window_days: int,
    max_abs_shift_c: float,
    min_history_days: int,
) -> Dict[str, Any]:
    by_model: Dict[str, List[BiasPair]] = defaultdict(list)
    for pair in pairs:
        by_model[pair.model].append(pair)

    per_model: Dict[str, Any] = {}
    overall_raw_errors: List[float] = []
    overall_static_errors: List[float] = []
    overall_wf_long_mean_errors: List[float] = []
    overall_wf_long_median_errors: List[float] = []
    overall_dynamic_errors: List[float] = []
    overall_dyn_minus_static: List[float] = []
    overall_dyn_minus_wf_long: List[float] = []
    skipped: Dict[str, int] = defaultdict(int)
    season_groups: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))

    for model, model_pairs in sorted(by_model.items()):
        ordered = sorted(model_pairs, key=lambda p: p.event_date)
        raw_errors: List[float] = []
        static_errors: List[float] = []
        wf_long_mean_errors: List[float] = []
        wf_long_median_errors: List[float] = []
        dynamic_errors: List[float] = []
        dyn_minus_static: List[float] = []
        dyn_minus_wf_long: List[float] = []
        evaluated = 0
        for pair in ordered:
            history = [p for p in ordered if p.event_date < pair.event_date]
            if len(history) < int(min_history_days):
                skipped["not_enough_history"] += 1
                continue
            history_biases = [p.bias_c for p in history]
            wf_long_shift = -sum(history_biases) / len(history_biases)
            wf_long_median_shift = -float(_quantile(history_biases, 0.5) or 0.0)
            dynamic = _model_shadow(
                model,
                history,
                target_date=pair.event_date,
                static_shift_c=static_shifts.get(model),
                recent_days=recent_days,
                doy_window_days=doy_window_days,
                max_abs_shift_c=max_abs_shift_c,
            )
            dynamic_shift = _to_float(dynamic.get("dynamic_shift_c"))
            if dynamic_shift is None:
                skipped["dynamic_shift_missing"] += 1
                continue
            evaluated += 1
            raw_error = float(pair.bias_c)
            season = _season_for_date(pair.event_date)
            raw_errors.append(raw_error)
            dynamic_errors.append(raw_error + float(dynamic_shift))
            wf_long_mean_errors.append(raw_error + wf_long_shift)
            wf_long_median_errors.append(raw_error + wf_long_median_shift)
            overall_raw_errors.append(raw_error)
            overall_wf_long_mean_errors.append(raw_error + wf_long_shift)
            overall_wf_long_median_errors.append(raw_error + wf_long_median_shift)
            overall_dynamic_errors.append(raw_error + float(dynamic_shift))
            season_groups[season]["raw"].append(raw_error)
            season_groups[season]["dynamic"].append(raw_error + float(dynamic_shift))
            static_shift = _to_float(static_shifts.get(model))
            if static_shift is not None:
                static_errors.append(raw_error + float(static_shift))
                overall_static_errors.append(raw_error + float(static_shift))
                dyn_minus_static.append(float(dynamic_shift) - float(static_shift))
                overall_dyn_minus_static.append(float(dynamic_shift) - float(static_shift))
                season_groups[season]["current_static"].append(raw_error + float(static_shift))
            dyn_minus_wf_long.append(float(dynamic_shift) - float(wf_long_shift))
            overall_dyn_minus_wf_long.append(float(dynamic_shift) - float(wf_long_shift))
        raw_mae = _mae(raw_errors)
        static_mae = _mae(static_errors)
        wf_long_mean_mae = _mae(wf_long_mean_errors)
        wf_long_median_mae = _mae(wf_long_median_errors)
        dynamic_mae = _mae(dynamic_errors)
        per_model[model] = {
            "evaluated": evaluated,
            "raw_mae_c": raw_mae,
            "current_static_full_sample_mae_c": static_mae,
            "walk_forward_long_mean_mae_c": wf_long_mean_mae,
            "walk_forward_long_median_mae_c": wf_long_median_mae,
            "dynamic_walk_forward_mae_c": dynamic_mae,
            "dynamic_vs_raw_mae_improvement": _improvement(raw_mae, dynamic_mae),
            "dynamic_vs_current_static_mae_improvement": _improvement(static_mae, dynamic_mae),
            "dynamic_vs_walk_forward_long_mean_mae_improvement": _improvement(wf_long_mean_mae, dynamic_mae),
            "dynamic_vs_walk_forward_long_median_mae_improvement": _improvement(wf_long_median_mae, dynamic_mae),
            "dynamic_minus_current_static_shift_c": {
                "mean_abs": _mean([abs(v) for v in dyn_minus_static]),
                "p50_abs": _quantile([abs(v) for v in dyn_minus_static], 0.5),
                "p90_abs": _quantile([abs(v) for v in dyn_minus_static], 0.9),
                "max_abs": max([abs(v) for v in dyn_minus_static], default=None),
            },
            "dynamic_minus_walk_forward_long_shift_c": {
                "mean_abs": _mean([abs(v) for v in dyn_minus_wf_long]),
                "p50_abs": _quantile([abs(v) for v in dyn_minus_wf_long], 0.5),
                "p90_abs": _quantile([abs(v) for v in dyn_minus_wf_long], 0.9),
                "max_abs": max([abs(v) for v in dyn_minus_wf_long], default=None),
            },
            "note": "current_static_full_sample uses the latest static report shift and is not a leak-free historical baseline",
        }

    raw_mae = _mae(overall_raw_errors)
    static_mae = _mae(overall_static_errors)
    wf_long_mean_mae = _mae(overall_wf_long_mean_errors)
    wf_long_median_mae = _mae(overall_wf_long_median_errors)
    dynamic_mae = _mae(overall_dynamic_errors)
    by_season: Dict[str, Any] = {}
    for season, errors in sorted(season_groups.items()):
        static_season_mae = _mae(errors.get("current_static", []))
        dynamic_season_mae = _mae(errors.get("dynamic", []))
        by_season[season] = {
            "n": len(errors.get("dynamic", [])),
            "raw_mae_c": _mae(errors.get("raw", [])),
            "current_static_full_sample_mae_c": static_season_mae,
            "dynamic_walk_forward_mae_c": dynamic_season_mae,
            "dynamic_vs_current_static_mae_improvement": _improvement(static_season_mae, dynamic_season_mae),
        }
    return {
        "method": "walk_forward_dynamic_shift_on_model_mean_residuals",
        "min_history_days": int(min_history_days),
        "evaluated": len(overall_dynamic_errors),
        "skipped": dict(skipped),
        "overall": {
            "raw_mae_c": raw_mae,
            "current_static_full_sample_mae_c": static_mae,
            "walk_forward_long_mean_mae_c": wf_long_mean_mae,
            "walk_forward_long_median_mae_c": wf_long_median_mae,
            "dynamic_walk_forward_mae_c": dynamic_mae,
            "dynamic_vs_raw_mae_improvement": _improvement(raw_mae, dynamic_mae),
            "dynamic_vs_current_static_mae_improvement": _improvement(static_mae, dynamic_mae),
            "dynamic_vs_walk_forward_long_mean_mae_improvement": _improvement(wf_long_mean_mae, dynamic_mae),
            "dynamic_vs_walk_forward_long_median_mae_improvement": _improvement(wf_long_median_mae, dynamic_mae),
            "dynamic_minus_current_static_shift_c": {
                "mean_abs": _mean([abs(v) for v in overall_dyn_minus_static]),
                "p50_abs": _quantile([abs(v) for v in overall_dyn_minus_static], 0.5),
                "p75_abs": _quantile([abs(v) for v in overall_dyn_minus_static], 0.75),
                "p90_abs": _quantile([abs(v) for v in overall_dyn_minus_static], 0.9),
                "max_abs": max([abs(v) for v in overall_dyn_minus_static], default=None),
            },
            "dynamic_minus_walk_forward_long_shift_c": {
                "mean_abs": _mean([abs(v) for v in overall_dyn_minus_wf_long]),
                "p50_abs": _quantile([abs(v) for v in overall_dyn_minus_wf_long], 0.5),
                "p75_abs": _quantile([abs(v) for v in overall_dyn_minus_wf_long], 0.75),
                "p90_abs": _quantile([abs(v) for v in overall_dyn_minus_wf_long], 0.9),
                "max_abs": max([abs(v) for v in overall_dyn_minus_wf_long], default=None),
            },
        },
        "by_season": by_season,
        "per_model": per_model,
        "apply_gate": {
            "status": "not_auto_approved",
            "required_before_runtime_apply": [
                "dynamic_walk_forward improves raw MAE and probability Brier/RPS",
                "dynamic does not degrade any season beyond tolerance",
                "user confirms replacing static shift with dynamic shift",
            ],
        },
    }


def build_report(
    payload: Mapping[str, Any],
    *,
    target_date: date,
    scope: str = "wu_only",
    recent_days: int = 30,
    doy_window_days: int = 30,
    max_abs_shift_c: float = 3.0,
    min_backtest_history_days: int = 60,
) -> Dict[str, Any]:
    pairs = _load_pairs(payload, scope)
    static = _static_shifts(payload)
    models = sorted({p.model for p in pairs} | set(static))
    model_reports = {
        model: _model_shadow(
            model,
            pairs,
            target_date=target_date,
            static_shift_c=static.get(model),
            recent_days=recent_days,
            doy_window_days=doy_window_days,
            max_abs_shift_c=max_abs_shift_c,
        )
        for model in models
    }
    additive_risks = [
        model
        for model, row in model_reports.items()
        if row.get("static_shift_c") is not None and row.get("dynamic_shift_c") is not None
    ]
    backtest = _walk_forward_backtest(
        pairs,
        static,
        recent_days=recent_days,
        doy_window_days=doy_window_days,
        max_abs_shift_c=max_abs_shift_c,
        min_history_days=min_backtest_history_days,
    )
    oracle = _oracle_upper_bounds(pairs, static)
    d0_underestimation = (
        payload.get("d0_afternoon_high_temp_underestimation")
        if isinstance(payload.get("d0_afternoon_high_temp_underestimation"), Mapping)
        else {
            "available": False,
            "reason": "source_report_missing_d0_afternoon_high_temp_underestimation",
        }
    )
    d0_shadow_gate = (
        d0_underestimation.get("shadow_parameter_gate")
        if isinstance(d0_underestimation, Mapping) and isinstance(d0_underestimation.get("shadow_parameter_gate"), Mapping)
        else {
            "status": "unavailable",
            "reason": "source_report_missing_d0_afternoon_shadow_parameter_gate",
            "runtime_probability_write": False,
        }
    )
    return {
        "ok": True,
        "read_only": True,
        "runtime_probability_write": False,
        "generated_at": _utc_now(),
        "source_report": {
            "path_hint": str(DEFAULT_INPUT),
            "generated_at": payload.get("generated_at"),
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "history_window_policy": payload.get("history_window_policy"),
            "scope": scope,
        },
        "target_date": target_date.isoformat(),
        "station_id": payload.get("station_id"),
        "city": payload.get("city"),
        "method": {
            "version": "dynamic_bias_shadow_shrinkage_v1",
            "formula": "dynamic_shift = weighted shrink(long_term, season, month, recent, day_of_year_window)",
            "bias_definition": "bias_c = forecast_mean_c - actual_c; recommended_shift_c = -mean_bias_c",
            "recent_days": int(recent_days),
            "day_of_year_window_days": int(doy_window_days),
            "max_abs_shift_c": float(max_abs_shift_c),
        },
        "double_correction_guard": {
            "current_static_shift_source": "chengdu_wu_model_bias_correction_latest",
            "candidate_dynamic_shift_mode": "replace_static_shift_not_additive",
            "must_not_add_to_existing_static_shift": True,
            "models_with_additive_risk": additive_risks,
        },
        "models": model_reports,
        "walk_forward_backtest": backtest,
        "oracle_upper_bounds": oracle,
        "d0_afternoon_high_temp_underestimation": d0_underestimation,
        "d0_afternoon_underestimation_shadow_gate": d0_shadow_gate,
        "diagnosis": {
            "why_dynamic_improvement_is_small": [
                "current static shift already removes most model-level mean bias",
                "dynamic shrinkage stays close to long-term/static shifts because seasonal/month/recent residuals are noisy",
                "oracle full-sample month/season shifts show limited additional MAE headroom",
                "remaining error is dominated by day-level forecast variance, not a stable additive temperature bias",
            ],
        },
        "apply_recommendation": {
            "status": "shadow_only",
            "reason": "requires_walk_forward_backtest_and_user_confirmation_before_runtime_apply",
            "next_gate": "extend dynamic shift backtest to bucket probabilities; only apply if Brier/RPS improve and bucket bias moves toward zero without seasonal degradation",
            "d0_afternoon_underestimation_shadow_gate_status": d0_shadow_gate.get("status"),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a read-only dynamic model bias correction shadow report.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input chengdu WU/model bias report JSON")
    parser.add_argument("--out", default="", help="Optional JSON output path. Omit to print only.")
    parser.add_argument("--target-date", default="", help="Target event date, YYYY-MM-DD. Defaults to source report end_date + 1 day.")
    parser.add_argument("--scope", default="wu_only", choices=["wu_only", "all_sources_fallback"])
    parser.add_argument("--recent-days", type=int, default=30)
    parser.add_argument("--doy-window-days", type=int, default=30)
    parser.add_argument("--max-abs-shift-c", type=float, default=3.0)
    parser.add_argument("--min-backtest-history-days", type=int, default=60)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if args.target_date:
        target_date = date.fromisoformat(str(args.target_date)[:10])
    else:
        end_date = date.fromisoformat(str(payload.get("end_date"))[:10])
        target_date = date.fromordinal(end_date.toordinal() + 1)
    report = build_report(
        payload,
        target_date=target_date,
        scope=str(args.scope),
        recent_days=int(args.recent_days),
        doy_window_days=int(args.doy_window_days),
        max_abs_shift_c=float(args.max_abs_shift_c),
        min_backtest_history_days=int(args.min_backtest_history_days),
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
