from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np


QM_VERSION = "member_quantile_mapping_v1"
SEASONAL_BIAS_VERSION = "seasonal_bias_correction_shrinkage_v1"
RESIDUAL_ERROR_VERSION = "historical_residual_error_expansion_v1"
LOCAL_CORRECTION_VERSION = "local_temperature_adjustment_v1"
BUCKET_PROB_VERSION = "integer_bucket_settlement_sim_v1"
SOURCE_ALIGNED_QM_TARGET_VERSION = "source_aligned_wu_twc_settlement_target_v1"


@dataclass(frozen=True)
class OutcomeSpec:
    label: str
    kind: str
    k: float
    k2: Optional[float]
    unit: Optional[str]
    precision: int


@dataclass(frozen=True)
class QuantileMappingModel:
    model: str
    forecast_sorted: Tuple[float, ...]
    observed_sorted: Tuple[float, ...]
    sample_days: int
    forecast_sample_count: int
    observed_sample_count: int
    start_date: str
    end_date: str

    def map_value(self, value: float) -> float:
        if not math.isfinite(float(value)):
            return float("nan")
        x = np.asarray(self.forecast_sorted, dtype=float)
        y = np.asarray(self.observed_sorted, dtype=float)
        if x.size == 0 or y.size == 0:
            return float(value)
        if x.size == 1 or y.size == 1:
            return float(value) + (float(y[0]) - float(x[0]))
        ranks = np.linspace(0.0, 1.0, num=x.size)
        quantile = float(np.interp(float(value), x, ranks, left=0.0, right=1.0))
        obs_ranks = np.linspace(0.0, 1.0, num=y.size)
        return float(np.interp(quantile, obs_ranks, y))


@dataclass(frozen=True)
class SeasonalBiasCorrectionModel:
    model: str
    overall_adjustment: float
    month_adjustments: Tuple[Tuple[int, float], ...]
    sample_days: int
    residual_sample_count: int
    min_seasonal_sample_days: int
    shrinkage_days: float
    max_abs_adjustment: float
    start_date: str
    end_date: str

    def adjustment_for_date(self, event_date: Optional[str]) -> float:
        month = _month_from_event_date(event_date)
        by_month = {int(item[0]): float(item[1]) for item in self.month_adjustments}
        if month is not None and month in by_month:
            return float(by_month[month])
        return float(self.overall_adjustment)


@dataclass(frozen=True)
class ResidualErrorModel:
    model: str
    residual_quantiles: Tuple[float, ...]
    residual_days: int
    residual_sample_count: int
    quantile_low: float
    quantile_high: float
    quantile_count: int
    start_date: str
    end_date: str


@dataclass(frozen=True)
class LocalAdjustmentConfig:
    enabled: bool = True
    lapse_rate_enabled: bool = False
    station_elevation_m: Optional[float] = None
    model_elevation_m: Optional[float] = None
    lapse_rate_c_per_m: float = -0.0065
    cloud_radiative_enabled: bool = False
    cloud_cover_pct: Optional[float] = None
    night_radiation_cooling_max_c: float = 0.0
    heat_island_enabled: bool = False
    urban_heat_island_c: float = 0.0


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _finite_array(values: Any) -> np.ndarray:
    try:
        arr = np.asarray(values, dtype=float).reshape(-1)
    except Exception:
        return np.asarray([], dtype=float)
    if arr.size == 0:
        return arr
    return arr[np.isfinite(arr)]


def _target_weight(row: Mapping[str, Any]) -> int:
    weight = _to_float(row.get("target_weight") if isinstance(row, Mapping) else None)
    if weight is None or not math.isfinite(float(weight)):
        return 1
    return max(1, min(200, int(round(float(weight)))))


def _month_from_event_date(event_date: Optional[str]) -> Optional[int]:
    text = str(event_date or "").strip()[:10]
    if len(text) < 7:
        return None
    try:
        month = int(text[5:7])
    except ValueError:
        return None
    return month if 1 <= month <= 12 else None


def _clamp(value: float, limit: float) -> float:
    cap = abs(float(limit))
    if not math.isfinite(cap) or cap <= 0:
        return float(value)
    return max(-cap, min(cap, float(value)))


def extract_open_meteo_member_values(
    payload: Mapping[str, Any],
    variable: str,
    *,
    model_suffixes: Optional[Sequence[str]] = None,
    aggregation: str = "daily",
) -> np.ndarray:
    """
    Extract decimal ensemble members from Open-Meteo-style daily/hourly payloads.

    `aggregation="max"` is intended for hourly temperature_2m -> target-day high
    conversion while preserving one value per member.
    """

    if not isinstance(payload, Mapping):
        return np.asarray([], dtype=float)
    suffixes = [str(s) for s in (model_suffixes or []) if str(s)]
    aggregation_key = str(aggregation or "daily").strip().lower()

    def _suffix_ok(key: str) -> bool:
        if not suffixes:
            return True
        return any(key.endswith(f"_{suffix}") for suffix in suffixes)

    daily = payload.get("daily")
    if isinstance(daily, Mapping):
        prefix = f"{variable}_member"
        members: List[float] = []
        for key, values in daily.items():
            if not isinstance(key, str) or not key.startswith(prefix) or not _suffix_ok(key):
                continue
            arr = _finite_array(values)
            if arr.size:
                members.append(float(arr[0]))
        if members:
            return np.asarray(members, dtype=float)
        direct = _finite_array(daily.get(variable))
        if direct.size:
            return direct

    hourly = payload.get("hourly")
    if isinstance(hourly, Mapping):
        prefix = f"{variable}_member"
        member_series: List[np.ndarray] = []
        for key, values in hourly.items():
            if not isinstance(key, str) or not key.startswith(prefix) or not _suffix_ok(key):
                continue
            arr = _finite_array(values)
            if arr.size:
                member_series.append(arr)
        if member_series:
            if aggregation_key == "max":
                return np.asarray([float(np.max(arr)) for arr in member_series if arr.size], dtype=float)
            if aggregation_key == "min":
                return np.asarray([float(np.min(arr)) for arr in member_series if arr.size], dtype=float)
            if aggregation_key == "mean":
                return np.asarray([float(np.mean(arr)) for arr in member_series if arr.size], dtype=float)
            if aggregation_key in {"daily", "first"}:
                return np.asarray([float(arr[0]) for arr in member_series if arr.size], dtype=float)
            raise ValueError("aggregation must be daily|first|max|min|mean")
        direct = _finite_array(hourly.get(variable))
        if direct.size:
            if aggregation_key == "max":
                return np.asarray([float(np.max(direct))], dtype=float)
            if aggregation_key == "min":
                return np.asarray([float(np.min(direct))], dtype=float)
            if aggregation_key == "mean":
                return np.asarray([float(np.mean(direct))], dtype=float)
            return direct
    return np.asarray([], dtype=float)


def normalize_unit(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    raw = str(unit).strip()
    u = raw.lower().replace("deg", "").replace("degree", "").replace("degrees", "")
    u = u.replace("°", "").strip()
    if u in {"c", "celsius"}:
        return "C"
    if u in {"f", "fahrenheit"}:
        return "F"
    if u in {"%", "percent", "percentage"}:
        return "%"
    return raw


def _decimal_places(raw: str) -> int:
    return len(raw.split(".", 1)[1]) if "." in raw else 0


def parse_outcome_label(label: str) -> OutcomeSpec:
    raw = str(label or "").strip()
    normalized = raw.replace("–", "-")
    num = r"(-?\d+(?:\.\d+)?)"
    unit = r"([A-Za-z°/%]+)?"
    m = re.match(rf"^{num}\s*-\s*{num}\s*{unit}$", normalized)
    if m:
        return OutcomeSpec(
            raw,
            "range",
            float(m.group(1)),
            float(m.group(2)),
            normalize_unit(m.group(3)),
            max(_decimal_places(m.group(1)), _decimal_places(m.group(2))),
        )
    m = re.match(rf"^{num}\s*{unit}\s+or\s+(below|lower|less)$", normalized, flags=re.I)
    if m:
        return OutcomeSpec(raw, "le", float(m.group(1)), None, normalize_unit(m.group(2)), _decimal_places(m.group(1)))
    m = re.match(rf"^{num}\s*{unit}\s+or\s+(higher|above|more)$", normalized, flags=re.I)
    if m:
        return OutcomeSpec(raw, "ge", float(m.group(1)), None, normalize_unit(m.group(2)), _decimal_places(m.group(1)))
    m = re.match(rf"^{num}\s*{unit}$", normalized)
    if m:
        return OutcomeSpec(raw, "eq", float(m.group(1)), None, normalize_unit(m.group(2)), _decimal_places(m.group(1)))
    raise ValueError(f"Unsupported outcome label format: {label}")


def apply_rounding(value: float, rounding_rule: Optional[str], unit: Optional[str]) -> float:
    rule = str(rounding_rule or "").strip().lower()
    if normalize_unit(unit) in {"C", "F"}:
        if rule == "whole_degrees":
            return float(math.floor(float(value)))
        if rule in {"rounded_degrees", "nearest_whole", "nearest", "round"}:
            x = float(value)
            return float(math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5))
        if rule in {"ceiling_degrees", "ceil", "ceiling"}:
            return float(math.ceil(float(value)))
    return float(value)


def bucket_label_for_value(
    labels: Sequence[str],
    value: float,
    rounding_rule: Optional[str],
) -> str:
    for label in labels:
        try:
            spec = parse_outcome_label(label)
        except ValueError:
            continue
        rounded = apply_rounding(float(value), rounding_rule, spec.unit)
        if spec.kind == "eq" and abs(rounded - spec.k) <= 1e-9:
            return spec.label
        if spec.kind == "le" and rounded <= spec.k:
            return spec.label
        if spec.kind == "ge" and rounded >= spec.k:
            return spec.label
        if spec.kind == "range" and spec.k2 is not None and spec.k <= rounded <= spec.k2:
            return spec.label
    return ""


def fit_quantile_mapping(
    history_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    min_sample_days: int = 60,
) -> Tuple[Dict[str, QuantileMappingModel], Dict[str, Any]]:
    fitted: Dict[str, QuantileMappingModel] = {}
    model_meta: Dict[str, Any] = {}
    min_days = max(1, int(min_sample_days or 1))
    for model, rows in (history_by_model or {}).items():
        by_date: Dict[str, Tuple[List[float], float, int]] = {}
        target_sources: Dict[str, int] = {}
        actual_tables: Dict[str, int] = {}
        weighted_day_count = 0
        target_weight_sum = 0
        for row in rows or []:
            event_date = str(row.get("event_date") or row.get("date") or "").strip()[:10]
            actual = _to_float(row.get("actual") if "actual" in row else row.get("settlement_actual"))
            forecasts = _finite_array(row.get("forecast_values") if "forecast_values" in row else row.get("members"))
            if not event_date or actual is None or forecasts.size == 0:
                continue
            weight = _target_weight(row)
            by_date[event_date] = ([float(v) for v in forecasts.tolist()], float(actual), weight)
            target_source = str(
                row.get("training_target_source")
                or row.get("actual_source")
                or "settlement_actual"
            )
            target_sources[target_source] = target_sources.get(target_source, 0) + 1
            actual_table = str(row.get("actual_table") or "")
            if actual_table:
                actual_tables[actual_table] = actual_tables.get(actual_table, 0) + 1
            if weight > 1:
                weighted_day_count += 1
            target_weight_sum += weight
        dates = sorted(by_date)
        forecast_values: List[float] = []
        observed_values: List[float] = []
        for event_date in dates:
            fvals, actual, weight = by_date[event_date]
            finite_fvals = [float(v) for v in fvals if math.isfinite(float(v))]
            for _ in range(weight):
                forecast_values.extend(finite_fvals)
                observed_values.append(float(actual))
        sample_days = len(dates)
        status = "ok" if sample_days >= min_days and forecast_values and observed_values else "not_enough_samples"
        model_meta[str(model)] = {
            "status": status,
            "sample_days": sample_days,
            "min_sample_days": min_days,
            "forecast_sample_count": len(forecast_values),
            "observed_sample_count": len(observed_values),
            "start_date": dates[0] if dates else "",
            "end_date": dates[-1] if dates else "",
            "target_sources": target_sources,
            "actual_tables": actual_tables,
            "target_weight_sum": target_weight_sum,
            "weighted_day_count": weighted_day_count,
        }
        if status != "ok":
            continue
        fitted[str(model)] = QuantileMappingModel(
            model=str(model),
            forecast_sorted=tuple(sorted(float(v) for v in forecast_values)),
            observed_sorted=tuple(sorted(float(v) for v in observed_values)),
            sample_days=sample_days,
            forecast_sample_count=len(forecast_values),
            observed_sample_count=len(observed_values),
            start_date=dates[0],
            end_date=dates[-1],
        )
    ok_models = sorted(fitted)
    return fitted, {
        "version": QM_VERSION,
        "status": "ok" if ok_models else "not_enough_samples",
        "min_sample_days": min_days,
        "ok_models": ok_models,
        "model_meta": model_meta,
    }


def apply_quantile_mapping(
    model_members: Mapping[str, Any],
    qm_models: Mapping[str, QuantileMappingModel],
) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
    out: Dict[str, np.ndarray] = {}
    model_meta: Dict[str, Any] = {}
    total = 0
    mapped = 0
    for model, raw_values in (model_members or {}).items():
        arr = _finite_array(raw_values)
        total += int(arr.size)
        qm = qm_models.get(str(model))
        if qm is None or arr.size == 0:
            out[str(model)] = arr.copy()
            model_meta[str(model)] = {
                "applied": False,
                "reason": "qm_model_missing" if qm is None else "no_members",
                "member_count": int(arr.size),
                "mapped_member_count": 0,
            }
            continue
        corrected = np.asarray([qm.map_value(float(v)) for v in arr], dtype=float)
        corrected = corrected[np.isfinite(corrected)]
        out[str(model)] = corrected
        mapped += int(corrected.size)
        model_meta[str(model)] = {
            "applied": True,
            "member_count": int(arr.size),
            "mapped_member_count": int(corrected.size),
            "sample_days": int(qm.sample_days),
            "history_window": {"start_date": qm.start_date, "end_date": qm.end_date},
        }
    coverage = (float(mapped) / float(total)) if total > 0 else 0.0
    return out, {
        "version": QM_VERSION,
        "applied": mapped > 0,
        "member_count": int(total),
        "mapped_member_count": int(mapped),
        "member_coverage": coverage,
        "model_meta": model_meta,
    }


def fit_seasonal_bias_correction(
    history_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    qm_models: Optional[Mapping[str, QuantileMappingModel]] = None,
    *,
    base: str = "qm",
    min_sample_days: int = 60,
    min_seasonal_sample_days: int = 20,
    shrinkage_days: float = 30.0,
    max_abs_adjustment: float = 3.0,
) -> Tuple[Dict[str, SeasonalBiasCorrectionModel], Dict[str, Any]]:
    fitted: Dict[str, SeasonalBiasCorrectionModel] = {}
    model_meta: Dict[str, Any] = {}
    min_days = max(1, int(min_sample_days or 1))
    min_season_days = max(1, int(min_seasonal_sample_days or 1))
    shrink = max(0.0, float(shrinkage_days or 0.0))
    cap = abs(float(max_abs_adjustment or 0.0))
    if not math.isfinite(cap) or cap <= 0:
        cap = 3.0
    base_key = str(base or "qm").strip().lower()
    if base_key not in {"qm", "raw"}:
        base_key = "qm"

    for model, rows in (history_by_model or {}).items():
        qm = qm_models.get(str(model)) if isinstance(qm_models, Mapping) else None
        by_date: Dict[str, Tuple[List[float], float, int]] = {}
        weighted_day_count = 0
        target_weight_sum = 0
        for row in rows or []:
            event_date = str(row.get("event_date") or row.get("date") or "").strip()[:10]
            actual = _to_float(row.get("actual") if "actual" in row else row.get("settlement_actual"))
            forecasts = _finite_array(row.get("forecast_values") if "forecast_values" in row else row.get("members"))
            if not event_date or actual is None or forecasts.size == 0:
                continue
            weight = _target_weight(row)
            by_date[event_date] = ([float(v) for v in forecasts.tolist()], float(actual), weight)
            if weight > 1:
                weighted_day_count += 1
            target_weight_sum += weight

        residuals: List[Tuple[str, int, float]] = []
        if base_key == "raw" or qm is not None:
            for event_date in sorted(by_date):
                fvals, actual, weight = by_date[event_date]
                if base_key == "raw":
                    mapped = np.asarray([float(v) for v in fvals], dtype=float)
                elif qm is not None:
                    mapped = np.asarray([qm.map_value(float(v)) for v in fvals], dtype=float)
                else:
                    mapped = np.asarray([], dtype=float)
                mapped = mapped[np.isfinite(mapped)]
                month = _month_from_event_date(event_date)
                if mapped.size == 0 or month is None:
                    continue
                residual = float(actual) - float(np.mean(mapped))
                for _ in range(weight):
                    residuals.append((event_date, month, residual))

        sample_days = len({event_date for event_date, _, _ in residuals})
        status = "ok" if sample_days >= min_days and residuals else "not_enough_samples"
        if base_key == "qm" and qm is None:
            status = "qm_model_missing"
        overall = float(np.mean([value for _, _, value in residuals])) if residuals else 0.0
        overall = _clamp(overall, cap)
        month_meta: Dict[str, Any] = {}
        month_adjustments: List[Tuple[int, float]] = []
        for month in range(1, 13):
            vals = [value for _, row_month, value in residuals if row_month == month]
            if not vals:
                continue
            month_mean = float(np.mean(vals))
            month_count = len(vals)
            if month_count >= min_season_days:
                alpha = float(month_count) / float(month_count + shrink) if shrink > 0 else 1.0
                adjustment = _clamp(overall + alpha * (month_mean - overall), cap)
                month_adjustments.append((month, adjustment))
                month_status = "ok"
            else:
                alpha = 0.0
                adjustment = overall
                month_status = "not_enough_seasonal_samples"
            month_meta[str(month)] = {
                "status": month_status,
                "sample_days": month_count,
                "min_seasonal_sample_days": min_season_days,
                "raw_mean_residual": month_mean,
                "overall_residual": overall,
                "shrinkage_alpha": alpha,
                "adjustment": adjustment,
            }

        model_meta[str(model)] = {
            "status": status,
            "base": base_key,
            "sample_days": sample_days,
            "min_sample_days": min_days,
            "residual_sample_count": len(residuals),
            "overall_adjustment": overall,
            "min_seasonal_sample_days": min_season_days,
            "shrinkage_days": shrink,
            "max_abs_adjustment": cap,
            "start_date": residuals[0][0] if residuals else "",
            "end_date": residuals[-1][0] if residuals else "",
            "month_meta": month_meta,
            "target_weight_sum": target_weight_sum,
            "weighted_day_count": weighted_day_count,
        }
        if status != "ok":
            continue
        fitted[str(model)] = SeasonalBiasCorrectionModel(
            model=str(model),
            overall_adjustment=overall,
            month_adjustments=tuple(month_adjustments),
            sample_days=sample_days,
            residual_sample_count=len(residuals),
            min_seasonal_sample_days=min_season_days,
            shrinkage_days=shrink,
            max_abs_adjustment=cap,
            start_date=residuals[0][0],
            end_date=residuals[-1][0],
        )

    ok_models = sorted(fitted)
    return fitted, {
        "version": SEASONAL_BIAS_VERSION,
        "status": "ok" if ok_models else "not_enough_samples",
        "enabled": True,
        "base": base_key,
        "min_sample_days": min_days,
        "min_seasonal_sample_days": min_season_days,
        "shrinkage_days": shrink,
        "max_abs_adjustment": cap,
        "ok_models": ok_models,
        "model_meta": model_meta,
    }


def apply_seasonal_bias_correction(
    model_members: Mapping[str, Any],
    seasonal_models: Mapping[str, SeasonalBiasCorrectionModel],
    *,
    target_event_date: Optional[str],
    market_unit: Optional[str],
) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
    out: Dict[str, np.ndarray] = {}
    model_meta: Dict[str, Any] = {}
    total = 0
    adjusted = 0
    for model, raw_values in (model_members or {}).items():
        arr = _finite_array(raw_values)
        total += int(arr.size)
        seasonal_model = seasonal_models.get(str(model))
        if seasonal_model is None or arr.size == 0:
            out[str(model)] = arr.copy()
            model_meta[str(model)] = {
                "applied": False,
                "reason": "seasonal_model_missing" if seasonal_model is None else "no_members",
                "member_count": int(arr.size),
                "adjustment": 0.0,
            }
            continue
        adjustment = seasonal_model.adjustment_for_date(target_event_date)
        corrected = arr + float(adjustment)
        out[str(model)] = corrected[np.isfinite(corrected)]
        adjusted += int(out[str(model)].size)
        month = _month_from_event_date(target_event_date)
        month_keys = {int(item[0]) for item in seasonal_model.month_adjustments}
        model_meta[str(model)] = {
            "applied": True,
            "member_count": int(arr.size),
            "adjusted_member_count": int(out[str(model)].size),
            "adjustment": float(adjustment),
            "target_month": month,
            "source": "month_adjustment" if month in month_keys else "overall_adjustment",
            "sample_days": int(seasonal_model.sample_days),
            "residual_sample_count": int(seasonal_model.residual_sample_count),
            "min_seasonal_sample_days": int(seasonal_model.min_seasonal_sample_days),
            "shrinkage_days": float(seasonal_model.shrinkage_days),
            "history_window": {
                "start_date": seasonal_model.start_date,
                "end_date": seasonal_model.end_date,
            },
        }
    coverage = (float(adjusted) / float(total)) if total > 0 else 0.0
    return out, {
        "version": SEASONAL_BIAS_VERSION,
        "enabled": True,
        "applied": adjusted > 0,
        "target_event_date": str(target_event_date or ""),
        "market_unit": normalize_unit(market_unit) or str(market_unit or ""),
        "member_count": int(total),
        "adjusted_member_count": int(adjusted),
        "member_coverage": coverage,
        "model_meta": model_meta,
    }


def fit_residual_error_models(
    history_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    qm_models: Mapping[str, QuantileMappingModel],
    *,
    seasonal_models: Optional[Mapping[str, SeasonalBiasCorrectionModel]] = None,
    min_residual_days: int = 30,
    residual_window_days: int = 180,
    quantile_low: float = 0.05,
    quantile_high: float = 0.95,
    quantile_count: int = 17,
) -> Tuple[Dict[str, ResidualErrorModel], Dict[str, Any]]:
    fitted: Dict[str, ResidualErrorModel] = {}
    model_meta: Dict[str, Any] = {}
    min_days = max(1, int(min_residual_days or 1))
    window_days = max(1, int(residual_window_days or 1))
    q_low = max(0.0, min(1.0, float(quantile_low)))
    q_high = max(0.0, min(1.0, float(quantile_high)))
    if q_high < q_low:
        q_low, q_high = q_high, q_low
    q_count = max(1, int(quantile_count or 1))

    for model, rows in (history_by_model or {}).items():
        qm = qm_models.get(str(model))
        by_date: Dict[str, Tuple[List[float], float, int]] = {}
        weighted_day_count = 0
        target_weight_sum = 0
        for row in rows or []:
            event_date = str(row.get("event_date") or row.get("date") or "").strip()[:10]
            actual = _to_float(row.get("actual") if "actual" in row else row.get("settlement_actual"))
            forecasts = _finite_array(row.get("forecast_values") if "forecast_values" in row else row.get("members"))
            if not event_date or actual is None or forecasts.size == 0:
                continue
            weight = _target_weight(row)
            by_date[event_date] = ([float(v) for v in forecasts.tolist()], float(actual), weight)
            if weight > 1:
                weighted_day_count += 1
            target_weight_sum += weight

        dates = sorted(by_date)[-window_days:]
        residuals: List[float] = []
        residual_dates: List[str] = []
        if qm is not None:
            for event_date in dates:
                fvals, actual, weight = by_date[event_date]
                mapped = np.asarray([qm.map_value(float(v)) for v in fvals], dtype=float)
                mapped = mapped[np.isfinite(mapped)]
                if mapped.size == 0:
                    continue
                seasonal_adjustment = 0.0
                if seasonal_models:
                    seasonal_model = seasonal_models.get(str(model))
                    if seasonal_model is not None:
                        seasonal_adjustment = seasonal_model.adjustment_for_date(event_date)
                residual = float(actual) - (float(np.mean(mapped)) + float(seasonal_adjustment))
                for _ in range(weight):
                    residuals.append(residual)
                    residual_dates.append(event_date)

        sample_days = len(residual_dates)
        status = "ok" if qm is not None and sample_days >= min_days and residuals else "not_enough_residual_samples"
        if qm is None:
            status = "qm_model_missing"
        model_meta[str(model)] = {
            "status": status,
            "sample_days": sample_days,
            "min_residual_days": min_days,
            "residual_sample_count": len(residuals),
            "residual_window_days": window_days,
            "quantile_low": q_low,
            "quantile_high": q_high,
            "quantile_count": q_count,
            "start_date": residual_dates[0] if residual_dates else "",
            "end_date": residual_dates[-1] if residual_dates else "",
            "target_weight_sum": target_weight_sum,
            "weighted_day_count": weighted_day_count,
        }
        if status != "ok":
            continue

        residual_arr = np.asarray(residuals, dtype=float)
        quantiles = np.quantile(residual_arr, np.linspace(q_low, q_high, num=q_count))
        fitted[str(model)] = ResidualErrorModel(
            model=str(model),
            residual_quantiles=tuple(float(v) for v in quantiles.tolist()),
            residual_days=sample_days,
            residual_sample_count=len(residuals),
            quantile_low=q_low,
            quantile_high=q_high,
            quantile_count=q_count,
            start_date=residual_dates[0],
            end_date=residual_dates[-1],
        )

    ok_models = sorted(fitted)
    return fitted, {
        "version": RESIDUAL_ERROR_VERSION,
        "status": "ok" if ok_models else "not_enough_residual_samples",
        "enabled": True,
        "min_residual_days": min_days,
        "residual_window_days": window_days,
        "quantile_low": q_low,
        "quantile_high": q_high,
        "quantile_count": q_count,
        "ok_models": ok_models,
        "model_meta": model_meta,
    }


def apply_residual_error_expansion(
    model_members: Mapping[str, Any],
    residual_models: Mapping[str, ResidualErrorModel],
) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
    out: Dict[str, np.ndarray] = {}
    model_meta: Dict[str, Any] = {}
    total = 0
    expanded_total = 0
    expanded_base = 0
    for model, raw_values in (model_members or {}).items():
        arr = _finite_array(raw_values)
        total += int(arr.size)
        residual_model = residual_models.get(str(model))
        if residual_model is None or arr.size == 0:
            out[str(model)] = arr.copy()
            model_meta[str(model)] = {
                "applied": False,
                "reason": "residual_model_missing" if residual_model is None else "no_members",
                "member_count": int(arr.size),
                "expanded_member_count": int(arr.size),
            }
            expanded_total += int(arr.size)
            continue
        residuals = _finite_array(residual_model.residual_quantiles)
        if residuals.size == 0:
            out[str(model)] = arr.copy()
            model_meta[str(model)] = {
                "applied": False,
                "reason": "empty_residual_quantiles",
                "member_count": int(arr.size),
                "expanded_member_count": int(arr.size),
            }
            expanded_total += int(arr.size)
            continue
        expanded = (arr.reshape(-1, 1) + residuals.reshape(1, -1)).reshape(-1)
        expanded = expanded[np.isfinite(expanded)]
        out[str(model)] = expanded
        expanded_base += int(arr.size)
        expanded_total += int(expanded.size)
        model_meta[str(model)] = {
            "applied": True,
            "member_count": int(arr.size),
            "expanded_member_count": int(expanded.size),
            "residual_days": int(residual_model.residual_days),
            "residual_sample_count": int(residual_model.residual_sample_count),
            "residual_quantile_count": int(residual_model.quantile_count),
            "residual_quantile_low": float(residual_model.quantile_low),
            "residual_quantile_high": float(residual_model.quantile_high),
            "history_window": {
                "start_date": residual_model.start_date,
                "end_date": residual_model.end_date,
            },
        }
    coverage = (float(expanded_base) / float(total)) if total > 0 else 0.0
    return out, {
        "version": RESIDUAL_ERROR_VERSION,
        "enabled": True,
        "applied": expanded_base > 0,
        "member_count": int(total),
        "expanded_base_member_count": int(expanded_base),
        "expanded_member_count": int(expanded_total),
        "member_coverage": coverage,
        "model_meta": model_meta,
    }


def compute_local_temperature_adjustment(
    config: Optional[LocalAdjustmentConfig],
) -> Tuple[float, Dict[str, Any]]:
    cfg = config or LocalAdjustmentConfig(enabled=False)
    components: Dict[str, float] = {}
    if not cfg.enabled:
        return 0.0, {
            "version": LOCAL_CORRECTION_VERSION,
            "enabled": False,
            "applied": False,
            "components_c": components,
            "total_c": 0.0,
        }
    if cfg.lapse_rate_enabled:
        station_elev = _to_float(cfg.station_elevation_m)
        model_elev = _to_float(cfg.model_elevation_m)
        lapse = _to_float(cfg.lapse_rate_c_per_m)
        if station_elev is not None and model_elev is not None and lapse is not None:
            components["lapse_rate"] = float(station_elev - model_elev) * float(lapse)
    if cfg.cloud_radiative_enabled:
        cloud = _to_float(cfg.cloud_cover_pct)
        cooling_max = _to_float(cfg.night_radiation_cooling_max_c)
        if cloud is not None and cooling_max is not None:
            clear_fraction = 1.0 - max(0.0, min(100.0, float(cloud))) / 100.0
            components["cloud_night_radiation"] = -abs(float(cooling_max)) * clear_fraction
    if cfg.heat_island_enabled:
        uhi = _to_float(cfg.urban_heat_island_c)
        if uhi is not None:
            components["urban_heat_island"] = float(uhi)
    total = float(sum(components.values()))
    return total, {
        "version": LOCAL_CORRECTION_VERSION,
        "enabled": True,
        "applied": bool(components),
        "components_c": components,
        "total_c": total,
        "config": {
            "lapse_rate_enabled": bool(cfg.lapse_rate_enabled),
            "station_elevation_m": cfg.station_elevation_m,
            "model_elevation_m": cfg.model_elevation_m,
            "lapse_rate_c_per_m": cfg.lapse_rate_c_per_m,
            "cloud_radiative_enabled": bool(cfg.cloud_radiative_enabled),
            "cloud_cover_pct": cfg.cloud_cover_pct,
            "night_radiation_cooling_max_c": cfg.night_radiation_cooling_max_c,
            "heat_island_enabled": bool(cfg.heat_island_enabled),
            "urban_heat_island_c": cfg.urban_heat_island_c,
        },
    }


def apply_local_temperature_adjustment(
    model_members: Mapping[str, Any],
    *,
    market_unit: Optional[str],
    config: Optional[LocalAdjustmentConfig],
) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
    adjustment_c, meta = compute_local_temperature_adjustment(config)
    adjustment = adjustment_c
    if normalize_unit(market_unit) == "F":
        adjustment = adjustment_c * 9.0 / 5.0
    out: Dict[str, np.ndarray] = {}
    total = 0
    for model, raw_values in (model_members or {}).items():
        arr = _finite_array(raw_values)
        total += int(arr.size)
        out[str(model)] = arr + float(adjustment)
    meta["total_adjustment"] = float(adjustment)
    meta["market_unit"] = normalize_unit(market_unit) or str(market_unit or "")
    meta["member_count"] = int(total)
    return out, meta


def bucket_probabilities_from_members(
    model_members: Mapping[str, Any],
    labels: Sequence[str],
    *,
    weights: Optional[Mapping[str, float]] = None,
    rounding_rule: Optional[str],
) -> Tuple[Dict[str, float], Dict[str, Any]]:
    label_list = [str(label or "").strip() for label in labels if str(label or "").strip()]
    counts_by_model: Dict[str, Dict[str, int]] = {}
    probs_by_model: Dict[str, Dict[str, float]] = {}
    unmatched_by_model: Dict[str, int] = {}
    total_members = 0
    for model, raw_values in (model_members or {}).items():
        arr = _finite_array(raw_values)
        total_members += int(arr.size)
        counts = {label: 0 for label in label_list}
        unmatched = 0
        for value in arr:
            label = bucket_label_for_value(label_list, float(value), rounding_rule)
            if label:
                counts[label] = int(counts.get(label, 0)) + 1
            else:
                unmatched += 1
        denom = float(arr.size) if arr.size > 0 else 0.0
        probs = {label: (float(counts[label]) / denom if denom > 0 else 0.0) for label in label_list}
        counts_by_model[str(model)] = counts
        probs_by_model[str(model)] = probs
        unmatched_by_model[str(model)] = int(unmatched)

    active = [model for model in probs_by_model if _finite_array(model_members.get(model)).size > 0]
    raw_weights: Dict[str, float] = {}
    if weights:
        for model in active:
            val = _to_float(weights.get(model))
            if val is not None and val > 0:
                raw_weights[model] = float(val)
    if not raw_weights and active:
        raw_weights = {model: 1.0 for model in active}
    weight_total = sum(raw_weights.values())
    normalized_weights = {
        model: (float(value) / float(weight_total))
        for model, value in raw_weights.items()
        if weight_total > 0
    }
    combined = {label: 0.0 for label in label_list}
    for model, weight in normalized_weights.items():
        for label in label_list:
            combined[label] += float(weight) * float(probs_by_model.get(model, {}).get(label, 0.0))

    prob_sum = sum(combined.values())
    normalized = False
    if prob_sum > 0 and abs(prob_sum - 1.0) > 1e-10:
        combined = {label: value / prob_sum for label, value in combined.items()}
        normalized = True
    return combined, {
        "version": BUCKET_PROB_VERSION,
        "rounding_rule": str(rounding_rule or ""),
        "labels": label_list,
        "model_weights": normalized_weights,
        "counts_by_model": counts_by_model,
        "probs_by_model": probs_by_model,
        "unmatched_by_model": unmatched_by_model,
        "raw_probability_sum": prob_sum,
        "probability_sum": sum(combined.values()),
        "normalized": normalized,
        "member_count": int(total_members),
    }


def calibrate_members_and_bucket_probabilities(
    model_members: Mapping[str, Any],
    history_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    labels: Sequence[str],
    *,
    weights: Optional[Mapping[str, float]] = None,
    rounding_rule: Optional[str],
    market_unit: Optional[str],
    target_event_date: Optional[str] = None,
    min_sample_days: int = 60,
    local_config: Optional[LocalAdjustmentConfig] = None,
    seasonal_correction_enabled: bool = False,
    seasonal_correction_base: str = "qm",
    min_seasonal_sample_days: int = 20,
    seasonal_shrinkage_days: float = 30.0,
    seasonal_max_abs_adjustment: float = 3.0,
    residual_expansion_enabled: bool = True,
    min_residual_days: int = 30,
    residual_window_days: int = 180,
    residual_quantile_low: float = 0.05,
    residual_quantile_high: float = 0.95,
    residual_quantile_count: int = 17,
) -> Dict[str, Any]:
    qm_models, qm_fit_meta = fit_quantile_mapping(history_by_model, min_sample_days=min_sample_days)
    qm_members, qm_apply_meta = apply_quantile_mapping(model_members, qm_models)
    seasonal_base = str(seasonal_correction_base or "qm").strip().lower()
    if seasonal_base not in {"qm", "raw"}:
        seasonal_base = "qm"
    if seasonal_correction_enabled:
        seasonal_models, seasonal_fit_meta = fit_seasonal_bias_correction(
            history_by_model,
            qm_models,
            base=seasonal_base,
            min_sample_days=min_sample_days,
            min_seasonal_sample_days=min_seasonal_sample_days,
            shrinkage_days=seasonal_shrinkage_days,
            max_abs_adjustment=seasonal_max_abs_adjustment,
        )
        seasonal_input_members = model_members if seasonal_base == "raw" else qm_members
        seasonal_members, seasonal_apply_meta = apply_seasonal_bias_correction(
            seasonal_input_members,
            seasonal_models,
            target_event_date=target_event_date,
            market_unit=market_unit,
        )
        seasonal_apply_meta["base"] = seasonal_base
    else:
        seasonal_models = {}
        seasonal_members = qm_members
        seasonal_input_members = model_members if seasonal_base == "raw" else qm_members
        seasonal_fit_meta = {
            "version": SEASONAL_BIAS_VERSION,
            "enabled": False,
            "base": seasonal_base,
            "status": "disabled",
            "ok_models": [],
            "model_meta": {},
        }
        seasonal_apply_meta = {
            "version": SEASONAL_BIAS_VERSION,
            "enabled": False,
            "base": seasonal_base,
            "applied": False,
            "target_event_date": str(target_event_date or ""),
            "member_count": int(sum(_finite_array(v).size for v in seasonal_input_members.values())),
            "adjusted_member_count": 0,
            "member_coverage": 0.0,
            "model_meta": {},
        }
    residual_enabled = bool(residual_expansion_enabled)
    if residual_enabled and seasonal_correction_enabled and seasonal_base == "raw":
        residual_enabled = False
    if residual_enabled:
        residual_models, residual_fit_meta = fit_residual_error_models(
            history_by_model,
            qm_models,
            seasonal_models=seasonal_models,
            min_residual_days=min_residual_days,
            residual_window_days=residual_window_days,
            quantile_low=residual_quantile_low,
            quantile_high=residual_quantile_high,
            quantile_count=residual_quantile_count,
        )
        residual_members, residual_apply_meta = apply_residual_error_expansion(seasonal_members, residual_models)
    else:
        residual_models = {}
        residual_members = seasonal_members
        residual_status = "disabled"
        if residual_expansion_enabled and seasonal_correction_enabled and seasonal_base == "raw":
            residual_status = "disabled_for_raw_seasonal_bias_base"
        residual_fit_meta = {
            "version": RESIDUAL_ERROR_VERSION,
            "enabled": False,
            "status": residual_status,
            "ok_models": [],
            "model_meta": {},
        }
        residual_apply_meta = {
            "version": RESIDUAL_ERROR_VERSION,
            "enabled": False,
            "applied": False,
            "member_count": int(sum(_finite_array(v).size for v in seasonal_members.values())),
            "expanded_base_member_count": 0,
            "expanded_member_count": int(sum(_finite_array(v).size for v in seasonal_members.values())),
            "member_coverage": 0.0,
            "model_meta": {},
        }
    local_members, local_meta = apply_local_temperature_adjustment(
        residual_members,
        market_unit=market_unit,
        config=local_config or LocalAdjustmentConfig(enabled=False),
    )
    bucket_probs, bucket_meta = bucket_probabilities_from_members(
        local_members,
        labels,
        weights=weights,
        rounding_rule=rounding_rule,
    )
    raw_bucket_probs, raw_bucket_meta = bucket_probabilities_from_members(
        model_members,
        labels,
        weights=weights,
        rounding_rule=rounding_rule,
    )
    return {
        "corrected_members": local_members,
        "qm_members": qm_members,
        "seasonal_bias_members": seasonal_members,
        "residual_expanded_members": residual_members,
        "bucket_probabilities": bucket_probs,
        "raw_bucket_probabilities": raw_bucket_probs,
        "provenance": {
            "version": "ensemble_qm_bucket_pipeline_v2",
            "qm_fit": qm_fit_meta,
            "qm_apply": qm_apply_meta,
            "seasonal_bias_correction": {
                "fit": seasonal_fit_meta,
                "apply": seasonal_apply_meta,
                "model_count": len(seasonal_models),
                "base": seasonal_base,
            },
            "residual_error": {
                "fit": residual_fit_meta,
                "apply": residual_apply_meta,
                "model_count": len(residual_models),
            },
            "local_adjustment": local_meta,
            "bucket_probability": bucket_meta,
            "raw_bucket_probability": raw_bucket_meta,
        },
    }


def _iter_dates(start_date: str, end_date: str) -> Iterable[str]:
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _normalize_qm_history_source(source: Optional[str]) -> str:
    key = str(source or "").strip().lower().replace("-", "_")
    if key in {"", "wu", "wunderground", "wunderground_history"}:
        return "wunderground"
    if key in {
        "wu_twc",
        "wu_twc_final",
        "wu_twc_settlement",
        "wu_twc_final_settlement",
        "source_aligned_wu_twc",
    }:
        return "wu_twc_final_settlement"
    if key in {"twc", "weather_com", "weatherdotcom", "weather.com"}:
        return "twc"
    if key in {"all", "any", "settlement_actual"}:
        return "any"
    return key


def _qm_history_source_predicate(column_sql: str, source_key: str) -> Tuple[str, List[str]]:
    column = f"lower(coalesce({column_sql}, ''))"
    key = _normalize_qm_history_source(source_key)
    if key == "wunderground":
        return f"AND {column} LIKE ?", ["%wunderground.com/history/daily%"]
    if key == "twc":
        return f"AND ({column} LIKE ? OR {column} LIKE ?)", ["%weather.com%", "%twc%"]
    if key == "wu_twc_final_settlement":
        return (
            f"AND ({column} LIKE ? OR {column} LIKE ? OR {column} LIKE ? OR {column} LIKE ?)",
            [
                "%wunderground.com/history/daily%",
                "%wu_twc_final_settlement%",
                "%weather.com%",
                "%twc%",
            ],
        )
    if key == "any":
        return "", []
    return f"AND {column} LIKE ?", [f"%{key}%"]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _qm_history_actual_rank(source_detail: str, actual_table: str) -> int:
    source = str(source_detail or "").lower()
    if "wunderground.com/history/daily" in source:
        base = 40
    elif "wu_twc_final_settlement" in source:
        base = 38
    elif "weather.com" in source or "twc" in source:
        base = 35
    elif "open-meteo" in source:
        base = 10
    else:
        base = 0
    if actual_table == "event_runs":
        base += 1
    return base


def summarize_qm_training_targets(
    history_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Dict[str, Any]:
    rows = 0
    dates: set[str] = set()
    target_sources: Dict[str, int] = {}
    actual_tables: Dict[str, int] = {}
    source_details: Dict[str, int] = {}
    target_weight_sum = 0
    weighted_rows = 0
    weighted_dates: set[str] = set()
    target_weight_reasons: Dict[str, int] = {}
    for model_rows in (history_by_model or {}).values():
        for row in model_rows or []:
            rows += 1
            event_date = str(row.get("event_date") or row.get("date") or "").strip()[:10]
            if event_date:
                dates.add(event_date)
            weight = _target_weight(row)
            target_weight_sum += weight
            if weight > 1:
                weighted_rows += 1
                if event_date:
                    weighted_dates.add(event_date)
                reason = str(row.get("target_weight_reason") or "weighted").strip()
                target_weight_reasons[reason] = target_weight_reasons.get(reason, 0) + 1
            target_source = str(
                row.get("training_target_source")
                or row.get("actual_source")
                or "settlement_actual"
            )
            target_sources[target_source] = target_sources.get(target_source, 0) + 1
            actual_table = str(row.get("actual_table") or "")
            if actual_table:
                actual_tables[actual_table] = actual_tables.get(actual_table, 0) + 1
            source_detail = str(row.get("actual_source_detail") or "")
            if source_detail:
                source_details[source_detail] = source_details.get(source_detail, 0) + 1
    return {
        "version": SOURCE_ALIGNED_QM_TARGET_VERSION,
        "role": "source_aligned_qm_training_target",
        "row_count": rows,
        "sample_days": len(dates),
        "start_date": min(dates) if dates else "",
        "end_date": max(dates) if dates else "",
        "target_sources": target_sources,
        "actual_tables": actual_tables,
        "actual_source_details": source_details,
        "target_weight_sum": target_weight_sum,
        "weighted_row_count": weighted_rows,
        "weighted_day_count": len(weighted_dates),
        "target_weight_reasons": target_weight_reasons,
    }


def load_station_temperature_qm_history(
    db_path: Path | str,
    *,
    loc_key: str,
    station_id: str,
    market_var: str = "temperature_2m_max",
    market_unit: str = "C",
    end_before: Optional[str] = None,
    source: str = "wu_twc_final_settlement",
    max_days: int = 365,
    recent_target_window_days: int = 0,
    recent_target_weight: float = 1.0,
) -> Dict[str, List[Dict[str, Any]]]:
    path = Path(db_path)
    if not path.exists():
        return {}
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        source_key = _normalize_qm_history_source(source)
        source_clause, source_params = _qm_history_source_predicate("resolution_source", source_key)
        params: List[Any] = [station_id, market_var, market_unit] + source_params
        if end_before:
            source_clause += " AND event_date < ?"
            params.append(str(end_before)[:10])
        rows: List[sqlite3.Row] = list(conn.execute(
            f"""
            SELECT id, event_date, settlement_actual, resolution_source, created_at,
                   'event_daily_temps' AS actual_table
            FROM event_daily_temps
            WHERE upper(coalesce(station_id, '')) = upper(?)
              AND market_var = ?
              AND market_unit = ?
              AND settlement_actual IS NOT NULL
              {source_clause}
            ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
            """,
            params,
        ).fetchall())
        if _table_exists(conn, "event_runs"):
            run_clause, run_source_params = _qm_history_source_predicate("resolution_source", source_key)
            run_params: List[Any] = [station_id, market_var, market_unit] + run_source_params
            if end_before:
                run_clause += " AND event_date < ?"
                run_params.append(str(end_before)[:10])
            rows.extend(
                conn.execute(
                    f"""
                    SELECT id, event_date, settlement_actual, resolution_source, created_at,
                           'event_runs' AS actual_table
                    FROM event_runs
                    WHERE upper(coalesce(station_id, '')) = upper(?)
                      AND market_var = ?
                      AND market_unit = ?
                      AND settlement_actual IS NOT NULL
                      {run_clause}
                    ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
                    """,
                    run_params,
                ).fetchall()
            )
        actuals: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            event_date = str(row["event_date"] or "")[:10]
            actual = _to_float(row["settlement_actual"])
            if not event_date or actual is None:
                continue
            actual_table = str(row["actual_table"] or "")
            source_detail = str(row["resolution_source"] or "")
            candidate = {
                "actual": float(actual),
                "source_detail": source_detail,
                "actual_table": actual_table,
                "created_at": str(row["created_at"] or ""),
                "id": int(row["id"] or 0),
                "rank": _qm_history_actual_rank(source_detail, actual_table),
            }
            previous = actuals.get(event_date)
            if previous is None or (
                int(candidate["rank"]),
                str(candidate["created_at"]),
                int(candidate["id"]),
            ) > (
                int(previous.get("rank") or 0),
                str(previous.get("created_at") or ""),
                int(previous.get("id") or 0),
            ):
                actuals[event_date] = candidate
        if not actuals:
            return {}
        selected_dates = sorted(actuals, reverse=True)[: max(1, int(max_days or 1))]
        actuals = {event_date: actuals[event_date] for event_date in selected_dates}
        start_date = min(actuals)
        end_date = max(actuals)
        recent_weight = max(1, min(200, int(round(float(recent_target_weight or 1.0)))))
        recent_window = max(0, int(recent_target_window_days or 0))
        recent_start_date = ""
        if (
            recent_weight > 1
            and recent_window > 0
            and source_key in {"wunderground", "twc", "wu_twc_final_settlement"}
        ):
            try:
                ref_date = date.fromisoformat(str(end_before or end_date)[:10])
                recent_start_date = (ref_date - timedelta(days=recent_window)).isoformat()
            except Exception:
                recent_start_date = ""
        cache_rows = conn.execute(
            """
            SELECT model, event_date, members_json, created_at
            FROM model_member_cache
            WHERE loc_key = ?
              AND event_date >= ?
              AND event_date <= ?
              AND market_var = ?
              AND market_unit = ?
            ORDER BY model, event_date, datetime(created_at) DESC, id DESC
            """,
            (loc_key, start_date, end_date, market_var, market_unit),
        ).fetchall()
    finally:
        conn.close()

    latest: Dict[Tuple[str, str], List[float]] = {}
    for row in cache_rows:
        model = str(row["model"] or "")
        event_date = str(row["event_date"] or "")[:10]
        if not model or event_date not in actuals:
            continue
        key = (model, event_date)
        if key in latest:
            continue
        try:
            raw = json.loads(row["members_json"] or "[]")
        except Exception:
            continue
        values = [float(v) for v in _finite_array(raw).tolist()]
        if values:
            latest[key] = values

    history: Dict[str, List[Dict[str, Any]]] = {}
    for (model, event_date), values in latest.items():
        actual_meta = actuals[event_date]
        target_weight = 1
        target_weight_reason = ""
        if recent_start_date and event_date >= recent_start_date:
            target_weight = recent_weight
            target_weight_reason = "recent_wu_twc_final_settlement_window"
        history.setdefault(model, []).append(
            {
                "event_date": event_date,
                "forecast_values": values,
                "actual": actual_meta["actual"],
                "actual_source": source_key,
                "actual_source_detail": actual_meta["source_detail"],
                "actual_table": actual_meta["actual_table"],
                "training_target_source": (
                    "wu_twc_final_settlement"
                    if source_key in {"wunderground", "twc", "wu_twc_final_settlement"}
                    else source_key
                ),
                "training_target_role": "source_aligned_qm_observed_final_settlement",
                "target_weight": target_weight,
                "target_weight_reason": target_weight_reason,
            }
        )
    for model in list(history):
        history[model].sort(key=lambda item: str(item.get("event_date") or ""))
    return history
