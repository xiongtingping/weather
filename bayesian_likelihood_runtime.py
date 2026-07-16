from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EPS = 1e-12


@dataclass(frozen=True)
class OutcomeBucket:
    label: str
    kind: str
    low: Optional[float]
    high: Optional[float]
    unit: str
    representative: float


@dataclass(frozen=True)
class DynamicLikelihoodConfig:
    dewpoint_weight: float = 1.0
    observed_temp_weight: float = 1.0
    peak_estimator_weight: float = 1.0
    radiation_weight: float = 1.0
    cloud_weight: float = 1.0
    precipitation_weight: float = 1.0
    wind_weight: float = 1.0
    pressure_weight: float = 1.0
    wet_bulb_weight: float = 0.65
    bimodal_size_scale: float = 0.45
    cloud_low_pct: float = 0.12
    low_cti: float = 0.45
    high_cti: float = 0.78
    high_temp_delta_c: float = 2.0
    wind_strong_mps: float = 8.0
    bimodal_min_separation_c: float = 3.0
    bimodal_valley_ratio: float = 0.55
    ge_tail_hard_threshold_c: float = 1.0
    ge_tail_soft_width_c: float = 1.4
    ge_tail_soft_margin_c: float = 0.15
    ge_tail_soft_min_factor: float = 0.02
    ge_tail_support_weight: float = 1.0
    ge_tail_support_quantile: float = 0.95
    ge_tail_support_zscore: float = 2.4
    final_observed_max_weight: float = 1.0
    settlement_source_forecast_weight: float = 0.0


EVIDENCE_GROUPS: Tuple[str, ...] = (
    "temperature_trajectory",
    "radiation",
    "cloud_shading",
    "precipitation_evaporative_cooling",
    "airmass_mixing",
    "wet_bulb_constraint",
    "residual_correction",
)


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _env_float(key: str, default: float) -> float:
    val = _to_float(os.environ.get(key))
    return float(default) if val is None else float(val)


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _normalize(values: Sequence[float]) -> List[float]:
    cleaned = [max(0.0, float(v)) if math.isfinite(float(v)) else 0.0 for v in values]
    total = sum(cleaned)
    if total <= EPS:
        return [1.0 / len(cleaned) for _ in cleaned] if cleaned else []
    return [v / total for v in cleaned]


def _normalize_unit(unit: Any) -> str:
    raw = str(unit or "").strip().lower().replace("°", "")
    if raw in {"c", "celsius"}:
        return "C"
    if raw in {"f", "fahrenheit"}:
        return "F"
    return raw.upper()


def _f_to_c(value: float) -> float:
    return (float(value) - 32.0) * 5.0 / 9.0


def _c_to_f(value: float) -> float:
    return float(value) * 9.0 / 5.0 + 32.0


def _convert_temp(value: float, from_unit: str, to_unit: str) -> float:
    src = _normalize_unit(from_unit)
    dst = _normalize_unit(to_unit)
    if src == dst or not src or not dst:
        return float(value)
    if src == "F" and dst == "C":
        return _f_to_c(float(value))
    if src == "C" and dst == "F":
        return _c_to_f(float(value))
    return float(value)


def parse_temperature_bucket(label: str) -> Optional[OutcomeBucket]:
    raw = str(label or "").strip().replace("–", "-")
    if not raw:
        return None
    num = r"(-?\d+(?:\.\d+)?)"
    unit = r"([A-Za-z°/%]+)?"

    patterns = [
        (rf"^{num}\s*-\s*{num}\s*{unit}$", "range"),
        (rf"^{num}\s+to\s+{num}\s*{unit}$", "range"),
        (rf"^between\s+{num}\s*-\s*{num}\s*{unit}$", "range"),
        (rf"^between\s+{num}\s+(?:and|to)\s+{num}\s*{unit}$", "range"),
    ]
    for pattern, kind in patterns:
        match = re.match(pattern, raw, flags=re.I)
        if match:
            low = min(float(match.group(1)), float(match.group(2)))
            high = max(float(match.group(1)), float(match.group(2)))
            return OutcomeBucket(raw, kind, low, high, _normalize_unit(match.group(3)), (low + high) / 2.0)

    match = re.match(rf"^{num}\s*{unit}\s+or\s+below$", raw, flags=re.I)
    if match:
        k = float(match.group(1))
        return OutcomeBucket(raw, "le", None, k, _normalize_unit(match.group(2)), k)

    match = re.match(rf"^{num}\s*{unit}\s+or\s+(higher|above|more)$", raw, flags=re.I)
    if match:
        k = float(match.group(1))
        return OutcomeBucket(raw, "ge", k, None, _normalize_unit(match.group(2)), k)

    match = re.match(rf"^{num}\s*{unit}$", raw, flags=re.I)
    if match:
        k = float(match.group(1))
        return OutcomeBucket(raw, "eq", k, k, _normalize_unit(match.group(2)), k)
    return None


def dynamic_likelihood_config(hours_to_event: Optional[float] = None) -> DynamicLikelihoodConfig:
    hours = _to_float(hours_to_event)
    cfg = DynamicLikelihoodConfig(
        cloud_low_pct=_env_float("PM_BAYES_LIKELIHOOD_CLOUD_LOW_PCT", 0.12),
        wind_strong_mps=_env_float("PM_BAYES_LIKELIHOOD_WIND_STRONG_MPS", 8.0),
        bimodal_size_scale=_env_float("PM_BAYES_LIKELIHOOD_BIMODAL_SIZE_SCALE", 0.45),
        ge_tail_hard_threshold_c=_env_float("PM_BAYES_GE_TAIL_HARD_THRESHOLD_C", 1.0),
        ge_tail_soft_width_c=_env_float("PM_BAYES_GE_TAIL_SOFT_WIDTH_C", 1.4),
        ge_tail_soft_margin_c=_env_float("PM_BAYES_GE_TAIL_SOFT_MARGIN_C", 0.15),
        ge_tail_soft_min_factor=_env_float("PM_BAYES_GE_TAIL_SOFT_MIN_FACTOR", 0.02),
        ge_tail_support_weight=_env_float("PM_BAYES_GE_TAIL_SUPPORT_WEIGHT", 1.0),
        ge_tail_support_quantile=_env_float("PM_BAYES_GE_TAIL_SUPPORT_QUANTILE", 0.95),
        ge_tail_support_zscore=_env_float("PM_BAYES_GE_TAIL_ZSCORE", 2.4),
        final_observed_max_weight=_env_float("PM_BAYES_FINAL_OBSERVED_MAX_WEIGHT", 2.25),
        settlement_source_forecast_weight=_env_float("PM_BAYES_SETTLEMENT_SOURCE_FORECAST_WEIGHT", 0.0),
    )
    if hours is None:
        return cfg
    if hours <= 2.0:
        return DynamicLikelihoodConfig(
            dewpoint_weight=1.35,
            observed_temp_weight=1.45,
            peak_estimator_weight=1.35,
            radiation_weight=0.15,
            cloud_weight=0.10,
            precipitation_weight=1.35,
            wind_weight=0.20,
            pressure_weight=0.05,
            wet_bulb_weight=0.95,
            bimodal_size_scale=cfg.bimodal_size_scale,
            cloud_low_pct=cfg.cloud_low_pct,
            wind_strong_mps=cfg.wind_strong_mps,
            ge_tail_hard_threshold_c=cfg.ge_tail_hard_threshold_c,
            ge_tail_soft_width_c=cfg.ge_tail_soft_width_c,
            ge_tail_soft_margin_c=cfg.ge_tail_soft_margin_c,
            ge_tail_soft_min_factor=cfg.ge_tail_soft_min_factor,
            ge_tail_support_weight=cfg.ge_tail_support_weight,
            ge_tail_support_quantile=cfg.ge_tail_support_quantile,
            ge_tail_support_zscore=cfg.ge_tail_support_zscore,
            final_observed_max_weight=cfg.final_observed_max_weight,
            settlement_source_forecast_weight=cfg.settlement_source_forecast_weight,
        )
    if hours <= 12.0:
        return DynamicLikelihoodConfig(
            dewpoint_weight=1.15,
            observed_temp_weight=1.15,
            peak_estimator_weight=1.20,
            radiation_weight=0.85,
            cloud_weight=0.75,
            precipitation_weight=1.10,
            wind_weight=0.85,
            pressure_weight=0.35,
            wet_bulb_weight=0.80,
            bimodal_size_scale=cfg.bimodal_size_scale,
            cloud_low_pct=cfg.cloud_low_pct,
            wind_strong_mps=cfg.wind_strong_mps,
            ge_tail_hard_threshold_c=cfg.ge_tail_hard_threshold_c,
            ge_tail_soft_width_c=cfg.ge_tail_soft_width_c,
            ge_tail_soft_margin_c=cfg.ge_tail_soft_margin_c,
            ge_tail_soft_min_factor=cfg.ge_tail_soft_min_factor,
            ge_tail_support_weight=cfg.ge_tail_support_weight,
            ge_tail_support_quantile=cfg.ge_tail_support_quantile,
            ge_tail_support_zscore=cfg.ge_tail_support_zscore,
            final_observed_max_weight=cfg.final_observed_max_weight,
            settlement_source_forecast_weight=cfg.settlement_source_forecast_weight,
        )
    if hours > 48.0:
        return DynamicLikelihoodConfig(
            dewpoint_weight=0.85,
            observed_temp_weight=0.25,
            peak_estimator_weight=0.25,
            radiation_weight=0.55,
            cloud_weight=0.45,
            precipitation_weight=0.45,
            wind_weight=0.70,
            pressure_weight=1.15,
            wet_bulb_weight=0.40,
            bimodal_size_scale=cfg.bimodal_size_scale,
            cloud_low_pct=cfg.cloud_low_pct,
            wind_strong_mps=cfg.wind_strong_mps,
            ge_tail_hard_threshold_c=cfg.ge_tail_hard_threshold_c,
            ge_tail_soft_width_c=cfg.ge_tail_soft_width_c,
            ge_tail_soft_margin_c=cfg.ge_tail_soft_margin_c,
            ge_tail_soft_min_factor=cfg.ge_tail_soft_min_factor,
            ge_tail_support_weight=cfg.ge_tail_support_weight,
            ge_tail_support_quantile=cfg.ge_tail_support_quantile,
            ge_tail_support_zscore=cfg.ge_tail_support_zscore,
            final_observed_max_weight=cfg.final_observed_max_weight,
            settlement_source_forecast_weight=cfg.settlement_source_forecast_weight,
        )
    return cfg


def _feature(features: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        val = _to_float(features.get(key))
        if val is not None:
            return val
    return None


def _feature_unit(features: Mapping[str, Any]) -> str:
    return _normalize_unit(
        features.get("unit")
        or features.get("market_unit")
        or features.get("temperature_unit")
        or "C"
    )


def _bucket_bounds_c(bucket: OutcomeBucket) -> Tuple[Optional[float], Optional[float], float]:
    unit = bucket.unit or "C"
    low = _convert_temp(bucket.low, unit, "C") if bucket.low is not None else None
    high = _convert_temp(bucket.high, unit, "C") if bucket.high is not None else None
    rep = _convert_temp(bucket.representative, unit, "C")
    return low, high, rep


def _log_factor(factor: float, weight: float) -> float:
    safe = _clamp(float(factor), EPS, 10.0)
    return math.log(safe) * max(0.0, float(weight))


def _sigmoid(x: float) -> float:
    if x >= 50.0:
        return 1.0
    if x <= -50.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-float(x)))


def _in_direction(direction: float, sectors: Iterable[Tuple[float, float]]) -> bool:
    deg = float(direction) % 360.0
    for start, end in sectors:
        s = float(start) % 360.0
        e = float(end) % 360.0
        if s <= e and s <= deg <= e:
            return True
        if s > e and (deg >= s or deg <= e):
            return True
    return False


def _parse_sectors(raw: Any, default: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if raw is None or raw == "":
        return default
    out: List[Tuple[float, float]] = []
    for part in str(raw).split(","):
        if "-" not in part:
            continue
        a, b = part.split("-", 1)
        av = _to_float(a)
        bv = _to_float(b)
        if av is not None and bv is not None:
            out.append((av, bv))
    return out or default


def detect_bimodal_temperature_distribution(
    values: Sequence[Any],
    *,
    min_separation_c: float = 3.0,
    valley_ratio: float = 0.55,
) -> Dict[str, Any]:
    nums = sorted(float(v) for v in (_to_float(v) for v in values) if v is not None)
    if len(nums) < 6:
        return {"bimodal": False, "reason": "insufficient_values", "n": len(nums)}
    gaps = [(nums[idx + 1] - nums[idx], idx) for idx in range(len(nums) - 1)]
    gap, idx = max(gaps, key=lambda item: item[0])
    left = nums[: idx + 1]
    right = nums[idx + 1 :]
    balance = min(len(left), len(right)) / max(1, len(nums))
    bimodal = bool(gap >= float(min_separation_c) and balance >= float(valley_ratio) * 0.5)
    return {
        "bimodal": bimodal,
        "n": len(nums),
        "peak_low_c": sum(left) / len(left),
        "peak_high_c": sum(right) / len(right),
        "separation_c": gap,
        "balance": balance,
        "reason": "two_airmass_modes" if bimodal else "single_mode_or_weak_split",
    }


def _temperature_values_from_features(features: Mapping[str, Any]) -> List[float]:
    for key in ("temperature_distribution_c", "temperature_members_c", "model_peak_temps_c", "model_values_c"):
        raw = features.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            return [float(v) for v in (_to_float(x) for x in raw) if v is not None]
    unit = _feature_unit(features)
    for key in ("temperature_distribution", "temperature_members", "model_peak_temps", "model_values"):
        raw = features.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            return [_convert_temp(float(v), unit, "C") for v in (_to_float(x) for x in raw) if v is not None]
    return []


def _estimate_tail_support_max_c(
    bucket: OutcomeBucket,
    features: Mapping[str, Any],
    *,
    peak_mu: Optional[float],
    peak_sigma: Optional[float],
    quantile: Optional[float] = None,
    zscore: Optional[float] = None,
) -> Optional[float]:
    if bucket.kind != "ge":
        return None

    override = (
        _to_float(features.get("tail_support_max_c"))
        or _to_float(features.get("ge_tail_support_max_c"))
        or _to_float(features.get("physical_tail_support_max_c"))
    )
    if override is not None:
        return override

    support_candidates: List[float] = []
    forecast = _feature(
        features,
        "forecast_peak_temp_c",
        "model_peak_temp_c",
        "expected_temp_c",
        "temperature_2m_max_c",
        "open_meteo_peak_temp_c",
    )
    if forecast is None:
        forecast_raw = _feature(
            features,
            "forecast_peak_temp",
            "model_peak_temp",
            "expected_temp",
            "temperature_2m_max",
        )
        if forecast_raw is not None:
            forecast = _convert_temp(forecast_raw, _feature_unit(features), "C")
    if forecast is not None:
        forecast_sigma = _feature(features, "forecast_sigma_c", "model_sigma_c", "temperature_sigma_c")
        if forecast_sigma is None:
            model_values = _temperature_values_from_features(features)
            if len(model_values) >= 2:
                mean = sum(model_values) / len(model_values)
                var = sum((v - mean) ** 2 for v in model_values) / max(1, len(model_values) - 1)
                forecast_sigma = math.sqrt(var)
        if forecast_sigma is None:
            forecast_sigma = _env_float("PM_BAYES_GE_TAIL_FALLBACK_SIGMA_C", 1.4)
        z = _clamp(float(zscore if zscore is not None else 2.4), 0.5, 6.0)
        support_candidates.append(float(forecast) + z * float(forecast_sigma))

    if peak_mu is not None and peak_sigma is not None:
        z = _clamp(float(zscore if zscore is not None else 2.4), 0.5, 6.0)
        support_candidates.append(float(peak_mu) + z * float(peak_sigma))

    values = _temperature_values_from_features(features)
    if values:
        sorted_values = sorted(values)
        q = _clamp(float(quantile if quantile is not None else 0.95), 0.5, 1.0)
        idx = int(math.floor((len(sorted_values) - 1) * q))
        support_candidates.append(sorted_values[max(0, min(idx, len(sorted_values) - 1))])
        support_candidates.append(sorted_values[-1] + 0.8)

    if not support_candidates:
        return None
    # Conservative tail cap: use the lower envelope among evidence-based supports.
    # This avoids optimistic over-extends caused by single high-variance sources and
    # keeps "or higher" buckets from staying alive when temperature observations
    # already indicate a much tighter reachable range.
    return min(support_candidates)


def cloud_diurnal_discount(cloud_cover_pct: Optional[float]) -> float:
    cloud = _to_float(cloud_cover_pct)
    if cloud is None:
        return 1.0
    c = _clamp(float(cloud) / 100.0 if cloud > 1.0 else float(cloud), 0.0, 1.0)
    if c <= 0.2:
        discount = 1.0 - 0.25 * c
    elif c <= 0.5:
        discount = 0.95 - 0.50 * (c - 0.2)
    elif c <= 0.8:
        discount = 0.80 - 0.67 * (c - 0.5)
    else:
        discount = 0.60 - 0.625 * (c - 0.8)
    return _clamp(discount, 0.1, 1.0)


def wind_diurnal_discount(wind_speed_mps: Optional[float]) -> float:
    wind = _to_float(wind_speed_mps)
    if wind is None:
        return 1.0
    w = max(0.0, float(wind))
    if w <= 3.0:
        discount = 1.0 - 0.007 * w
    elif w <= 8.0:
        discount = 0.98 - 0.026 * (w - 3.0)
    elif w <= 15.0:
        discount = 0.85 - 0.029 * (w - 8.0)
    else:
        discount = 0.65 - 0.013 * (w - 15.0)
    return _clamp(discount, 0.3, 1.0)


def _feature_bool(features: Mapping[str, Any], *keys: str) -> Optional[bool]:
    for key in keys:
        if key not in features:
            continue
        val = features.get(key)
        if isinstance(val, bool):
            return val
        if val is None:
            continue
        text = str(val).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _explicit_final_daily_max_evidence(features: Mapping[str, Any]) -> bool:
    if _to_float(features.get("settlement_actual")) is not None or _to_float(features.get("settlement_actual_c")) is not None:
        return True
    return any(
        _feature_bool(features, key) is True
        for key in (
            "hard_source_final_daily_max",
            "hard_source_settlement_final",
            "observed_today_max_final",
            "observed_today_max_is_final",
            "temperature_observation_final_daily_max",
            "daily_max_observation_complete",
        )
    )


def _flatten_feature_tokens(value: Any) -> List[str]:
    tokens: List[str] = []
    if isinstance(value, Mapping):
        for item in value.values():
            tokens.extend(_flatten_feature_tokens(item))
        return tokens
    if isinstance(value, (list, tuple, set)):
        for item in value:
            tokens.extend(_flatten_feature_tokens(item))
        return tokens
    text = str(value or "").strip()
    return [text.lower()] if text else []


def _wu_twc_intraday_strong_observation_demote_reason(features: Mapping[str, Any]) -> str:
    if _to_float(features.get("settlement_actual")) is not None or _to_float(features.get("settlement_actual_c")) is not None:
        return ""
    if any(
        _feature_bool(features, key) is True
        for key in (
            "observed_today_max_final",
            "hard_source_final_daily_max",
            "hard_source_settlement_final",
            "temperature_observation_final_daily_max",
        )
    ):
        return ""

    alert = features.get("weather_source_delay_deformation_alert")
    alert_map = alert if isinstance(alert, Mapping) else {}
    alert_tokens = _flatten_feature_tokens(alert_map)
    alert_text = " ".join(alert_tokens)
    alert_active = _feature_bool(alert_map, "alert") is True
    if alert_active and (
        "wu" in alert_text
        or "twc" in alert_text
        or "nowcast" in alert_text
        or "temperature_deformation" in alert_text
        or "temperature_mismatch" in alert_text
    ):
        return "wu_twc_intraday_deformation_alert"

    field_tokens: List[str] = []
    for key in (
        "weather_source_alert_codes",
        "wu_twc_observed_intraday_current_error",
        "wu_twc_observed_intraday_max_error",
        "wu_twc_observed_intraday_history_error",
        "wu_twc_observed_intraday_date_mismatch_reason",
        "wu_twc_observed_intraday_event_date_mismatch_reason",
    ):
        field_tokens.extend(_flatten_feature_tokens(features.get(key)))
    field_text = " ".join(field_tokens)
    if "current_observation_event_date_mismatch" in field_text or "event_date_mismatch" in field_text:
        return "wu_twc_intraday_event_date_mismatch"
    if any(
        _feature_bool(features, key) is True
        for key in (
            "wu_twc_observed_intraday_date_mismatch",
            "wu_twc_observed_intraday_event_date_mismatch",
            "wu_twc_observed_intraday_current_observation_event_date_mismatch",
        )
    ):
        return "wu_twc_intraday_event_date_mismatch"
    return ""


def _observed_temp_c(features: Mapping[str, Any]) -> Optional[float]:
    amsc_usable = (
        _feature(features, "zuuu_metar_raw_amsc_latest_temp_c") is not None
        and not _feature_bool(features, "zuuu_metar_raw_amsc_date_mismatch")
        and not _feature_bool(features, "metar_observation_date_mismatch")
    )
    if amsc_usable:
        amsc_c = _feature(features, "zuuu_metar_raw_amsc_latest_temp_c", "metar_temperature_c")
        if amsc_c is not None:
            return amsc_c
    awc_usable = (
        _feature_bool(features, "zuuu_metar_raw_awc_available")
        and not _feature_bool(features, "zuuu_metar_raw_awc_date_mismatch")
    )
    if awc_usable:
        awc_c = _feature(features, "zuuu_metar_raw_awc_latest_temp_c", "metar_temperature_c")
        if awc_c is not None:
            return awc_c
    observed_c = _feature(features, "observed_temp_c", "current_temp_c", "temperature_now_c", "metar_temperature_c")
    if observed_c is not None:
        return observed_c
    obs_raw = _feature(features, "observed_temp", "current_temp", "temperature_now")
    if obs_raw is not None:
        return _convert_temp(obs_raw, _feature_unit(features), "C")
    return None


def _forecast_peak_c(features: Mapping[str, Any]) -> Optional[float]:
    forecast_c = _feature(
        features,
        "forecast_peak_temp_c",
        "taf_forecast_peak_c",
        "amsc_taf_forecast_peak_c",
        "settlement_source_forecast_peak_c",
        "model_peak_temp_c",
        "expected_temp_c",
        "temperature_2m_max_c",
        "open_meteo_peak_temp_c",
    )
    if forecast_c is not None:
        return forecast_c
    raw_fc = _feature(features, "forecast_peak_temp", "model_peak_temp", "expected_temp", "temperature_2m_max")
    if raw_fc is not None:
        return _convert_temp(raw_fc, _feature_unit(features), "C")
    return None


def _observation_hour_local_from_features(features: Mapping[str, Any]) -> Optional[float]:
    hour = _feature(features, "observation_hour_local", "hour_local", "local_hour")
    if hour is not None:
        return float(hour)
    tz_name = str(features.get("event_tz") or features.get("timezone") or "Asia/Shanghai").strip()
    for key in (
        "zuuu_metar_raw_amsc_latest_local",
        "zuuu_metar_raw_awc_latest_local",
        "valid_time_local",
        "latest_valid_time_local",
        "observed_time_local",
    ):
        parsed = _local_hour_from_timestamp(features.get(key), local_timezone="")
        if parsed is not None:
            return float(parsed)
    for key in (
        "zuuu_metar_raw_amsc_latest_at",
        "zuuu_metar_raw_awc_latest_at",
        "valid_time",
        "latest_valid_time",
        "observed_time",
    ):
        parsed = _local_hour_from_timestamp(features.get(key), local_timezone=tz_name)
        if parsed is not None:
            return float(parsed)
    return None


def _forecast_peak_hour_local_from_features(features: Mapping[str, Any]) -> Optional[float]:
    hour = _feature(features, "local_peak_hour", "expected_peak_hour_local", "peak_hour_local")
    if hour is not None:
        return float(hour)
    tz_name = str(features.get("event_tz") or features.get("timezone") or "Asia/Shanghai").strip()
    for key in (
        "taf_forecast_peak_local",
        "amsc_taf_forecast_peak_local",
        "expected_peak_at_local",
        "peak_at_local",
    ):
        parsed = _local_hour_from_timestamp(features.get(key), local_timezone="")
        if parsed is not None:
            return float(parsed)
    for key in (
        "taf_forecast_peak_at",
        "amsc_taf_forecast_peak_at",
        "expected_peak_at",
        "peak_at",
    ):
        parsed = _local_hour_from_timestamp(features.get(key), local_timezone=tz_name)
        if parsed is not None:
            return float(parsed)
    return None


def _soft_forecast_peak_for_guard_c(features: Mapping[str, Any], fallback_c: Optional[float] = None) -> Optional[float]:
    soft = _feature(
        features,
        "settlement_source_forecast_peak_c",
        "wu_forecast_peak_c",
        "taf_forecast_peak_c",
        "forecast_peak_temp_c",
        "model_peak_temp_c",
        "expected_temp_c",
        "temperature_2m_max_c",
    )
    if soft is not None:
        return soft
    raw = _feature(features, "forecast_peak_temp", "model_peak_temp", "expected_temp", "temperature_2m_max")
    if raw is not None:
        return _convert_temp(raw, _feature_unit(features), "C")
    return fallback_c


TEMPERATURE_MOMENTUM_WINDOWS: Tuple[str, ...] = (
    "24h",
    "12h",
    "6h",
    "3h",
    "2h",
    "1h",
    "45m",
    "30m",
    "15m",
    "10m",
    "5m",
    "3m",
    "2m",
    "1m",
)
TEMPERATURE_MOMENTUM_WEIGHTS: Dict[str, float] = {
    "1m": 0.12,
    "2m": 0.11,
    "3m": 0.10,
    "5m": 0.09,
    "10m": 0.08,
    "15m": 0.08,
    "30m": 0.07,
    "45m": 0.06,
    "1h": 0.08,
    "2h": 0.06,
    "3h": 0.05,
    "6h": 0.04,
    "12h": 0.03,
    "24h": 0.03,
}
TEMPERATURE_MOMENTUM_CORE_WINDOWS: Tuple[str, ...] = ("1h",)
TEMPERATURE_MOMENTUM_SUPPLEMENT_WINDOWS: Tuple[str, ...] = ("45m", "30m")
TEMPERATURE_MOMENTUM_CONTEXT_WINDOWS: Tuple[str, ...] = ("24h", "12h", "6h", "3h", "2h")
TEMPERATURE_MOMENTUM_DIAGNOSTIC_ONLY_WINDOWS: Tuple[str, ...] = ("15m", "10m", "5m", "3m", "2m", "1m")
TEMPERATURE_MOMENTUM_EFFECTIVE_WEIGHTS: Dict[str, float] = {
    "1h": 0.48,
    "45m": 0.12,
    "30m": 0.12,
}
TEMPERATURE_MOMENTUM_RECENT_WINDOWS: Tuple[str, ...] = ("30m", "45m", "1h")
TEMPERATURE_MOMENTUM_PEAK_RATE_CONFIRM_WINDOWS: Tuple[str, ...] = ("1h", "45m", "30m")
TEMPERATURE_MOMENTUM_PEAK_RATE_CONFIRM_MIN_C_PER_HOUR = 0.25
TEMPERATURE_MOMENTUM_CONTEXT_CONFIRM_WINDOWS: Tuple[str, ...] = ("2h", "3h", "6h")
TEMPERATURE_MOMENTUM_CONTEXT_CONFIRM_MIN_C_PER_HOUR = 0.35
TEMPERATURE_MOMENTUM_OBSERVATION_ONLY_WINDOWS: Tuple[str, ...] = ("15m", "10m", "5m", "3m", "2m", "1m")
TEMPERATURE_MOMENTUM_SOFT_INTRADAY_SOURCES: Tuple[str, ...] = (
    "wu_twc_observed_intraday",
    "twc_pws_rapid_history_aux",
)
TEMPERATURE_MOMENTUM_SOFT_INTRADAY_DIRECTION_WINDOWS: Tuple[str, ...] = (
    "1h",
    "45m",
    "30m",
    "15m",
    "10m",
    "5m",
)


def _temperature_momentum_rate_for_window(momentum: Mapping[str, Any], label: str) -> Optional[float]:
    rates = momentum.get("rates_c_per_hour") if isinstance(momentum, Mapping) else None
    if isinstance(rates, Mapping):
        rate = _to_float(rates.get(label))
        if rate is not None and math.isfinite(float(rate)):
            return float(rate)
    context_rates = momentum.get("context_rates_c_per_hour") if isinstance(momentum, Mapping) else None
    if isinstance(context_rates, Mapping):
        rate = _to_float(context_rates.get(label))
        if rate is not None and math.isfinite(float(rate)):
            return float(rate)
    return None


def _confirmed_peak_rate_windows(momentum: Mapping[str, Any]) -> List[str]:
    horizons = momentum.get("available_horizons") if isinstance(momentum, Mapping) else []
    if not isinstance(horizons, list):
        horizons = []
    confirmed: List[str] = []
    for label in TEMPERATURE_MOMENTUM_PEAK_RATE_CONFIRM_WINDOWS:
        if label not in horizons:
            continue
        rate = _temperature_momentum_rate_for_window(momentum, label)
        if rate is not None and rate >= TEMPERATURE_MOMENTUM_PEAK_RATE_CONFIRM_MIN_C_PER_HOUR:
            confirmed.append(label)
    if confirmed == ["1h"]:
        context_confirmed: List[str] = []
        for label in TEMPERATURE_MOMENTUM_CONTEXT_CONFIRM_WINDOWS:
            rate = _temperature_momentum_rate_for_window(momentum, label)
            if rate is not None and rate >= TEMPERATURE_MOMENTUM_CONTEXT_CONFIRM_MIN_C_PER_HOUR:
                context_confirmed.append(label)
        if context_confirmed:
            return confirmed + context_confirmed
        return []
    return confirmed


def _temperature_rise_rate_window(features: Mapping[str, Any]) -> str:
    window = str(features.get("temperature_rise_rate_window") or "").strip()
    if window:
        return window
    source = str(features.get("temperature_rise_rate_source") or "").strip()
    if ":" in source:
        tail = source.rsplit(":", 1)[-1].strip()
        if tail:
            return tail
    return ""


def _temperature_rise_rate_observation_only(features: Mapping[str, Any]) -> bool:
    return _temperature_rise_rate_window(features) in TEMPERATURE_MOMENTUM_OBSERVATION_ONLY_WINDOWS


def _temperature_momentum_recent_rate(rates: Mapping[str, float]) -> Optional[float]:
    recent = [float(rates[key]) for key in TEMPERATURE_MOMENTUM_RECENT_WINDOWS if key in rates]
    if recent:
        return sum(recent) / max(1, len(recent))
    return rates.get("1h")


def _temperature_momentum_prefixed_rates(features: Mapping[str, Any], prefix: str) -> Dict[str, float]:
    rates: Dict[str, float] = {}
    for label in TEMPERATURE_MOMENTUM_WINDOWS:
        rate = _feature(features, f"{prefix}_temperature_momentum_{label}_c_per_hour")
        if rate is not None and math.isfinite(float(rate)):
            rates[label] = float(rate)
    return rates


def _temperature_momentum_direction(rate: Optional[float]) -> str:
    if rate is None or not math.isfinite(float(rate)):
        return "missing"
    warm_threshold = _env_float("PM_BAYES_TRAJECTORY_SOFT_INTRADAY_DIRECTION_MIN_C_PER_HOUR", 0.12)
    warm_threshold = max(0.0, float(warm_threshold))
    if float(rate) >= warm_threshold:
        return "warming"
    if float(rate) <= -warm_threshold:
        return "cooling"
    return "flat"


def _temperature_momentum_direction_rate(rates: Mapping[str, float]) -> Optional[float]:
    selected = {
        label: float(rates[label])
        for label in TEMPERATURE_MOMENTUM_SOFT_INTRADAY_DIRECTION_WINDOWS
        if label in rates and math.isfinite(float(rates[label]))
    }
    if not selected:
        return None
    stable = {
        label: selected[label]
        for label in TEMPERATURE_MOMENTUM_PEAK_RATE_CONFIRM_WINDOWS
        if label in selected
    }
    if stable:
        recent = _temperature_momentum_recent_rate(stable)
        if recent is not None:
            return float(recent)
        return sum(stable.values()) / max(1, len(stable))
    return sum(selected.values()) / max(1, len(selected))


def _soft_intraday_temperature_momentum_profile(
    features: Mapping[str, Any],
    *,
    hard_rates: Mapping[str, float],
) -> Dict[str, Any]:
    hard_direction_rate = _temperature_momentum_direction_rate(hard_rates)
    hard_direction = _temperature_momentum_direction(hard_direction_rate)
    boost_per_source = _env_float("PM_BAYES_TRAJECTORY_SOFT_INTRADAY_CONFIDENCE_BOOST", 0.06)
    boost_cap = _env_float("PM_BAYES_TRAJECTORY_SOFT_INTRADAY_CONFIDENCE_BOOST_CAP", 0.08)
    boost_per_source = _clamp(float(boost_per_source), 0.0, 0.20)
    boost_cap = _clamp(float(boost_cap), 0.0, 0.25)
    sources: List[Dict[str, Any]] = []
    total_boost = 0.0
    consistent_sources: List[str] = []
    conflicting_sources: List[str] = []
    for source_id in TEMPERATURE_MOMENTUM_SOFT_INTRADAY_SOURCES:
        rates = _temperature_momentum_prefixed_rates(features, source_id)
        if not rates and str(features.get("temperature_momentum_source") or "").strip() == source_id:
            rates = {
                label: float(rate)
                for label in TEMPERATURE_MOMENTUM_WINDOWS
                if (rate := _feature(features, f"temperature_momentum_{label}_c_per_hour")) is not None
            }
        available_flag = _feature_bool(features, f"{source_id}_available")
        error = str(features.get(f"{source_id}_error") or "").strip()
        sample_count = _feature(
            features,
            f"{source_id}_sample_count",
            f"{source_id}_history_sample_count",
        )
        source_seen = bool(rates) or available_flag is not None or bool(error) or sample_count is not None
        if not source_seen:
            continue
        stable_rates = {
            label: rates[label]
            for label in TEMPERATURE_MOMENTUM_PEAK_RATE_CONFIRM_WINDOWS
            if label in rates
        }
        short_rates = {
            label: rates[label]
            for label in TEMPERATURE_MOMENTUM_DIAGNOSTIC_ONLY_WINDOWS
            if label in rates
        }
        direction_rate = _temperature_momentum_direction_rate(rates)
        direction = _temperature_momentum_direction(direction_rate)
        consistent = bool(
            hard_direction in {"warming", "cooling"}
            and direction == hard_direction
        )
        conflict = bool(
            hard_direction in {"warming", "cooling"}
            and direction in {"warming", "cooling"}
            and direction != hard_direction
        )
        source_boost = 0.0
        if consistent:
            window_scale = 1.0 if stable_rates else 0.55
            sample_scale = 1.0
            if sample_count is not None:
                sample_scale = _clamp(float(sample_count) / 3.0, 0.35, 1.0)
            source_boost = float(boost_per_source) * window_scale * sample_scale
            total_boost += source_boost
            consistent_sources.append(source_id)
        elif conflict:
            conflicting_sources.append(source_id)
        sources.append(
            {
                "source": source_id,
                "available": bool(available_flag) if available_flag is not None else bool(rates),
                "error": error,
                "sample_count": int(sample_count) if sample_count is not None else None,
                "rates_c_per_hour": {
                    label: float(rates[label])
                    for label in TEMPERATURE_MOMENTUM_SOFT_INTRADAY_DIRECTION_WINDOWS
                    if label in rates
                },
                "stable_rates_c_per_hour": stable_rates,
                "diagnostic_only_rates_c_per_hour": short_rates,
                "effective_direction_rate_c_per_hour": float(direction_rate) if direction_rate is not None else None,
                "direction": direction,
                "consistent_with_amsc": bool(consistent),
                "conflicts_with_amsc": bool(conflict),
                "consistent_with_awc": bool(consistent),
                "conflicts_with_awc": bool(conflict),
                "used_for_live_momentum": False,
                "used_for_regime_confidence_boost": bool(source_boost > 0.0),
                "confidence_boost": float(source_boost),
            }
        )
    confidence_boost = min(float(boost_cap), float(total_boost))
    if confidence_boost > 0:
        status = "consistent_confidence_boost"
    elif conflicting_sources:
        status = "direction_conflict_no_boost"
    elif hard_direction == "missing":
        status = "amsc_direction_missing_no_boost"
    elif hard_direction == "flat":
        status = "amsc_direction_flat_no_boost"
    elif sources:
        status = "soft_direction_missing_or_flat_no_boost"
    else:
        status = "soft_source_missing"
    return {
        "enabled": True,
        "available": bool(sources),
        "status": status,
        "role": "soft_intraday_diagnostic_not_hard_source",
        "policy": "does_not_override_amsc; confidence_boost_only_when_direction_agrees",
        "hard_source": "zuuu_metar_raw_amsc",
        "hard_direction_rate_c_per_hour": float(hard_direction_rate) if hard_direction_rate is not None else None,
        "hard_direction": hard_direction,
        "sources": sources,
        "consistent_sources": consistent_sources,
        "conflicting_sources": conflicting_sources,
        "confidence_boost": float(confidence_boost),
    }


def _temperature_momentum_profile(
    features: Mapping[str, Any],
    *,
    fallback_rise_rate: Optional[float] = None,
) -> Dict[str, Any]:
    generic_rates: Dict[str, float] = {}
    for label in TEMPERATURE_MOMENTUM_WINDOWS:
        rate = _feature(
            features,
            f"temperature_momentum_{label}_c_per_hour",
            f"temperature_rise_rate_{label}_c_per_hour",
        )
        if rate is not None and math.isfinite(float(rate)):
            generic_rates[label] = float(rate)
    amsc_rates = _temperature_momentum_prefixed_rates(features, "zuuu_metar_raw_amsc")
    awc_rates = _temperature_momentum_prefixed_rates(features, "zuuu_metar_raw_awc")
    generic_source = str(features.get("temperature_momentum_source") or "").strip()
    soft_generic_momentum_source = generic_source in TEMPERATURE_MOMENTUM_SOFT_INTRADAY_SOURCES
    if generic_source == "zuuu_metar_raw_amsc":
        for label, rate in generic_rates.items():
            amsc_rates.setdefault(label, rate)
    elif generic_source == "zuuu_metar_raw_awc":
        for label, rate in generic_rates.items():
            awc_rates.setdefault(label, rate)

    wu_demote_reason = _wu_twc_intraday_strong_observation_demote_reason(features)
    rates: Dict[str, float] = {}
    context_rates: Dict[str, float] = {}
    supplement_rates: Dict[str, float] = {}
    soft_intraday_demoted_reason = (
        "soft_intraday_source_diagnostic_only_not_live_momentum"
        if soft_generic_momentum_source
        else ""
    )
    diagnostic_only_rates: Dict[str, float] = {
        label: generic_rates[label]
        for label in TEMPERATURE_MOMENTUM_DIAGNOSTIC_ONLY_WINDOWS
        if label in generic_rates
    }
    source = generic_source or "temperature_momentum"
    core_source = source
    supplement_source = ""
    core_rates = amsc_rates or awc_rates
    core_metar_source = "zuuu_metar_raw_amsc" if amsc_rates else "zuuu_metar_raw_awc"
    if core_rates:
        core_source = core_metar_source
        source = f"{core_metar_source}_core"
        for label in TEMPERATURE_MOMENTUM_CORE_WINDOWS:
            if label in core_rates:
                rates[label] = core_rates[label]
        for label in TEMPERATURE_MOMENTUM_CONTEXT_WINDOWS:
            if label in core_rates:
                context_rates[label] = core_rates[label]
        if not rates and "1h" in generic_rates and generic_source == core_metar_source:
            rates["1h"] = generic_rates["1h"]
        for label in TEMPERATURE_MOMENTUM_SUPPLEMENT_WINDOWS:
            if label in core_rates and label not in rates:
                rates[label] = core_rates[label]
                supplement_rates[label] = core_rates[label]
        if supplement_rates:
            supplement_source = core_metar_source
    else:
        if not soft_generic_momentum_source:
            for label in TEMPERATURE_MOMENTUM_CORE_WINDOWS + TEMPERATURE_MOMENTUM_SUPPLEMENT_WINDOWS:
                if label in generic_rates:
                    rates[label] = generic_rates[label]
            for label in TEMPERATURE_MOMENTUM_CONTEXT_WINDOWS:
                if label in generic_rates:
                    context_rates[label] = generic_rates[label]
    if "1h" not in rates and fallback_rise_rate is not None and math.isfinite(float(fallback_rise_rate)):
        rates["1h"] = float(fallback_rise_rate)
    context_weights = {
        "2h": 0.42,
        "3h": 0.30,
        "6h": 0.18,
        "12h": 0.07,
        "24h": 0.03,
    }
    context_weight_sum = sum(
        context_weights.get(key, 0.0)
        for key, value in context_rates.items()
        if math.isfinite(float(value))
    )
    context_effective = (
        sum(float(value) * context_weights.get(key, 0.0) for key, value in context_rates.items())
        / max(1e-6, context_weight_sum)
        if context_weight_sum > 0
        else None
    )
    context_positive_count = sum(1 for value in context_rates.values() if float(value) >= 0.35)
    context_warming_watch = bool(
        context_effective is not None
        and float(context_effective) >= _env_float("PM_TEMPERATURE_MOMENTUM_CONTEXT_WATCH_MIN_C_PER_HOUR", 0.45)
        and context_positive_count >= _env_float("PM_TEMPERATURE_MOMENTUM_CONTEXT_WATCH_MIN_WINDOWS", 2.0)
    )
    if not rates:
        return {
            "available": False,
            "state": "missing",
            "label": "缺失",
            "rates_c_per_hour": {},
            "context_rates_c_per_hour": context_rates,
            "diagnostic_only_rates_c_per_hour": diagnostic_only_rates,
            "diagnostic_only_windows": [key for key in TEMPERATURE_MOMENTUM_WINDOWS if key in diagnostic_only_rates],
            "diagnostic_only_reason": "sub_30m_windows_not_used_for_live_momentum" if diagnostic_only_rates else "",
            "context_effective_rate_c_per_hour": context_effective,
            "context_positive_window_count": context_positive_count,
            "context_warming_watch": context_warming_watch,
            "effective_rate_c_per_hour": None,
            "realization_scale": 0.0,
            "sustained_warming": False,
            "source": source,
            "core_source": core_source,
            "supplement_source": supplement_source,
            "wu_twc_supplement_demoted_reason": wu_demote_reason,
            "soft_intraday_momentum_source_demoted_reason": soft_intraday_demoted_reason,
            "conclusion": "缺少 AMSC/METAR 1h 主动量，软 intraday 源只能作为 regime 诊断佐证，不能单独确认物理升温潜力是否兑现。",
        }
    denom = sum(TEMPERATURE_MOMENTUM_EFFECTIVE_WEIGHTS.get(key, 0.0) for key in rates) or float(len(rates))
    effective = sum(float(rate) * TEMPERATURE_MOMENTUM_EFFECTIVE_WEIGHTS.get(key, 1.0) for key, rate in rates.items()) / max(1e-6, denom)
    short = _temperature_momentum_recent_rate(rates)
    positive_count = sum(1 for rate in rates.values() if rate >= 0.25)
    nonpositive_count = sum(1 for rate in rates.values() if rate <= 0.0)
    if short is not None and short <= -0.20 and effective <= 0.0:
        state = "cooling"
        label = "降温兑现"
        scale = 0.0
        conclusion = "短窗口动量转负且多窗口有效动量不正，升温潜力没有兑现。"
    elif short is not None and short <= 0.0 and effective <= 0.10:
        if context_warming_watch:
            state = "context_warming_watch"
            label = "多窗口升温观察"
            scale = 0.35
            conclusion = "短窗口尚未确认升温，但 AMSC/METAR 多小时轨迹仍显示升温背景，保守保留突破观察通道。"
        else:
            state = "not_realized"
            label = "未兑现"
            scale = 0.15
            conclusion = "当前短窗口动量不正，物理升温潜力不能放大高温桶。"
    elif len(rates) >= 2 and short is not None and short >= 0.35 and positive_count >= 2 and effective >= 0.25:
        state = "sustained_warming"
        label = "持续升温"
        scale = 1.0
        conclusion = "分钟级到小时级温度动量显示升温正在持续兑现。"
    elif short is not None and short >= 0.35 and effective >= 0.10:
        state = "emerging_warming"
        label = "升温初现"
        scale = 0.75 if len(rates) >= 2 else 1.0
        conclusion = "短窗口升温较强，但仍需中长窗口继续确认持续性。"
    elif effective >= 0.15 and nonpositive_count == 0:
        state = "weak_warming"
        label = "弱升温"
        scale = 0.55 if len(rates) >= 2 else 1.0
        conclusion = "多周期动量偏正但不强，只能小幅确认升温潜力。"
    elif abs(effective) < 0.15:
        state = "flat"
        label = "走平"
        scale = 0.25 if len(rates) >= 2 else 1.0
        conclusion = "多周期温度动量接近走平，晴空等物理潜力暂未强兑现。"
    else:
        state = "mixed"
        label = "分歧"
        scale = 0.45 if len(rates) >= 2 else 1.0
        conclusion = "多窗口动量方向分歧，暂不按强持续升温处理。"
    return {
        "available": True,
        "state": state,
        "label": label,
        "rates_c_per_hour": rates,
        "context_rates_c_per_hour": context_rates,
        "supplement_rates_c_per_hour": supplement_rates,
        "diagnostic_only_rates_c_per_hour": diagnostic_only_rates,
        "effective_rate_c_per_hour": float(effective),
        "context_effective_rate_c_per_hour": float(context_effective) if context_effective is not None else None,
        "context_positive_window_count": int(context_positive_count),
        "context_warming_watch": bool(context_warming_watch),
        "realization_scale": float(_clamp(scale, 0.0, 1.0)),
        "sustained_warming": bool(state == "sustained_warming"),
        "available_horizons": [key for key in TEMPERATURE_MOMENTUM_WINDOWS if key in rates],
        "diagnostic_only_windows": [key for key in TEMPERATURE_MOMENTUM_WINDOWS if key in diagnostic_only_rates],
        "diagnostic_only_reason": "sub_30m_windows_not_used_for_live_momentum" if diagnostic_only_rates else "",
        "context_horizons": [key for key in TEMPERATURE_MOMENTUM_WINDOWS if key in context_rates],
        "source": source,
        "core_source": core_source,
        "supplement_source": supplement_source,
        "wu_twc_supplement_demoted_reason": wu_demote_reason,
        "soft_intraday_momentum_source_demoted_reason": soft_intraday_demoted_reason,
        "conclusion": conclusion,
    }


def _high_temp_amplification_guard_meta(
    features: Mapping[str, Any],
    *,
    observed_c: Optional[float],
    forecast_c: Optional[float],
    rise_rate: Optional[float],
) -> Dict[str, Any]:
    gap_threshold_c = _env_float("PM_CHENGDU_HIGH_TEMP_GAP_GUARD_C", 2.0)
    sustained_momentum_rate_threshold = _env_float("PM_CHENGDU_SUSTAINED_MOMENTUM_MIN_C_PER_HOUR", 0.25)
    gap_threshold_c = max(0.0, float(gap_threshold_c))
    sustained_momentum_rate_threshold = max(0.0, float(sustained_momentum_rate_threshold))
    soft_forecast_c = _soft_forecast_peak_for_guard_c(features, fallback_c=forecast_c)
    gap_c = None
    if observed_c is not None and soft_forecast_c is not None:
        gap_c = float(soft_forecast_c) - float(observed_c)
    momentum = _temperature_momentum_profile(
        features,
        fallback_rise_rate=None if _temperature_rise_rate_observation_only(features) else rise_rate,
    )
    momentum_rate = _to_float(momentum.get("effective_rate_c_per_hour")) if isinstance(momentum, Mapping) else None
    confirmed_peak_rate_windows = _confirmed_peak_rate_windows(momentum) if isinstance(momentum, Mapping) else []
    momentum_warming = bool(
        isinstance(momentum, Mapping)
        and momentum.get("sustained_warming")
        and confirmed_peak_rate_windows
        and momentum_rate is not None
        and float(momentum_rate) >= sustained_momentum_rate_threshold
    )
    stale_or_nonwarming = bool(rise_rate is not None and float(rise_rate) <= 0.0)
    active = bool(
        gap_c is not None
        and gap_c > gap_threshold_c
        and stale_or_nonwarming
        and not momentum_warming
    )
    gap_label = f"{gap_threshold_c:.1f}°C".replace(".0°C", "°C")
    return {
        "active": active,
        "rule": (
            f"current_temp < soft_forecast - {gap_label} and warming_rate <= 0 "
            "unless sustained momentum confirms warming"
        ),
        "gap_threshold_c": float(gap_threshold_c),
        "sustained_momentum_rate_threshold_c_per_hour": float(sustained_momentum_rate_threshold),
        "observed_temp_c": float(observed_c) if observed_c is not None else None,
        "soft_forecast_peak_c": float(soft_forecast_c) if soft_forecast_c is not None else None,
        "forecast_gap_c": float(gap_c) if gap_c is not None else None,
        "warming_rate_c_per_hour": float(rise_rate) if rise_rate is not None else None,
        "temperature_momentum": momentum,
        "temperature_momentum_realization_state": momentum.get("state"),
        "temperature_momentum_sustained_warming": bool(momentum.get("sustained_warming")),
        "temperature_momentum_effective_c_per_hour": float(momentum_rate) if momentum_rate is not None else None,
        "suppressed_by_sustained_momentum": bool(momentum_warming),
        "action": "block_high_temp_bucket_amplification" if active else "allow_normal_evidence_weighting",
    }


def _peak_window_from_features(
    features: Mapping[str, Any],
    *,
    peak_hour: Optional[float],
    explicit_peak_hour: bool,
) -> Dict[str, Any]:
    start = _feature(
        features,
        "expected_peak_window_start_hour_local",
        "peak_window_start_hour_local",
        "local_peak_window_start_hour",
    )
    end = _feature(
        features,
        "expected_peak_window_end_hour_local",
        "peak_window_end_hour_local",
        "local_peak_window_end_hour",
    )
    basis = "explicit_or_forecast_peak_window" if start is not None or end is not None else ""
    if start is None and end is None and peak_hour is not None:
        lead = max(0.0, _env_float("PM_BAYES_PEAK_ESTIMATOR_PEAK_WINDOW_LEAD_HOURS", 1.0))
        post = max(0.0, _env_float("PM_BAYES_PEAK_ESTIMATOR_PEAK_WINDOW_POST_HOURS", 1.5))
        start = max(0.0, float(peak_hour) - lead)
        end = min(23.99, float(peak_hour) + post)
        basis = "explicit_peak_hour_expanded_window" if explicit_peak_hour else "forecast_or_prior_peak_hour_expanded_window"
    if start is None and end is None:
        start = _env_float("PM_BAYES_PEAK_ESTIMATOR_DEFAULT_PEAK_WINDOW_START_HOUR", 13.5)
        end = _env_float("PM_BAYES_PEAK_ESTIMATOR_DEFAULT_PEAK_WINDOW_END_HOUR", 17.5)
        basis = "default_chengdu_peak_window_prior_not_single_point"
    if start is not None:
        start = max(0.0, min(23.99, float(start)))
    if end is not None:
        end = max(0.0, min(23.99, float(end)))
    if start is not None and end is not None and float(end) < float(start):
        end = start
    return {
        "start_hour_local": float(start) if start is not None else None,
        "end_hour_local": float(end) if end is not None else None,
        "basis": basis or "default_chengdu_peak_window_prior_not_single_point",
        "single_point_forbidden": True,
    }


def _momentum_window_hours(label: str) -> Optional[float]:
    raw = str(label or "").strip().lower()
    if not raw:
        return None
    try:
        if raw.endswith("m"):
            return max(0.0, float(raw[:-1]) / 60.0)
        if raw.endswith("h"):
            return max(0.0, float(raw[:-1]))
    except Exception:
        return None
    return None


def _peak_hour_distribution_from_features(
    features: Mapping[str, Any],
    *,
    peak_hour: Optional[float],
    peak_window_start: Optional[float],
    peak_window_end: Optional[float],
) -> List[Dict[str, float]]:
    raw = features.get("peak_hour_distribution") or features.get("expected_peak_hour_distribution")
    items: List[Tuple[float, float]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            hour = _to_float(key)
            prob = _to_float(value)
            if hour is not None and prob is not None and prob > 0:
                items.append((_clamp(float(hour), 0.0, 23.99), float(prob)))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for item in raw:
            if isinstance(item, Mapping):
                hour = _to_float(item.get("hour") or item.get("hour_local") or item.get("peak_hour"))
                prob = _to_float(item.get("prob") or item.get("probability") or item.get("weight"))
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)) and len(item) >= 2:
                hour = _to_float(item[0])
                prob = _to_float(item[1])
            else:
                hour = None
                prob = None
            if hour is not None and prob is not None and prob > 0:
                items.append((_clamp(float(hour), 0.0, 23.99), float(prob)))
    if not items:
        center = _to_float(peak_hour)
        start = _to_float(peak_window_start)
        end = _to_float(peak_window_end)
        if center is None:
            return []
        if start is None or end is None or abs(float(end) - float(start)) < 1e-6:
            items = [(_clamp(float(center), 0.0, 23.99), 1.0)]
        else:
            start_i = int(math.floor(_clamp(float(start), 0.0, 23.99)))
            end_i = int(math.ceil(_clamp(float(end), 0.0, 23.99)))
            for hour_i in range(start_i, end_i + 1):
                hour_f = _clamp(float(hour_i), 0.0, 23.99)
                if hour_f < float(start) - 1e-9 or hour_f > float(end) + 1e-9:
                    continue
                weight = 1.0 / (1.0 + abs(hour_f - float(center)))
                items.append((hour_f, weight))
            if not items:
                items = [(_clamp(float(center), 0.0, 23.99), 1.0)]
    probs = _normalize([prob for _, prob in items])
    return [
        {"hour_local": float(hour), "prob": float(prob)}
        for (hour, _), prob in zip(items, probs)
    ]
def _hour_probability_distribution_from_raw(raw: Any) -> List[Dict[str, float]]:
    items: List[Tuple[float, float]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            hour = _to_float(key)
            prob = _to_float(value)
            if hour is not None and prob is not None and prob > 0:
                items.append((_clamp(float(hour), 0.0, 23.99), float(prob)))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for item in raw:
            if isinstance(item, Mapping):
                hour = _to_float(
                    item.get("hour")
                    or item.get("hour_local")
                    or item.get("local_hour")
                    or item.get("onset_hour")
                    or item.get("time_hour")
                )
                prob = _to_float(item.get("prob") or item.get("probability") or item.get("weight"))
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)) and len(item) >= 2:
                hour = _to_float(item[0])
                prob = _to_float(item[1])
            else:
                hour = None
                prob = None
            if hour is not None and prob is not None and prob > 0:
                items.append((_clamp(float(hour), 0.0, 23.99), float(prob)))
    probs = _normalize([prob for _, prob in items])
    return [
        {"hour_local": float(hour), "prob": float(prob)}
        for (hour, _), prob in zip(items, probs)
    ]


def _local_hour_from_timestamp(value: Any, *, local_timezone: str = "") -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    tz_name = str(local_timezone or "").strip()
    if tz_name:
        try:
            parsed = parsed.astimezone(ZoneInfo(tz_name))
        except ZoneInfoNotFoundError:
            pass
    return _clamp(float(parsed.hour) + float(parsed.minute) / 60.0 + float(parsed.second) / 3600.0, 0.0, 23.99)


def _hour_value_series_from_raw(
    raw: Any,
    *,
    feature_unit: str = "C",
    local_timezone: str = "",
    value_keys: Sequence[str] = (),
) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            hour = _to_float(key)
            if isinstance(value, Mapping) and value_keys:
                val = None
                for value_key in value_keys:
                    val = _to_float(value.get(value_key))
                    if val is not None:
                        break
            else:
                val = _to_float(value)
            if hour is not None and val is not None:
                out.append((_clamp(float(hour), 0.0, 23.99), float(val)))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for item in raw:
            if isinstance(item, Mapping):
                hour = _to_float(
                    item.get("hour")
                    or item.get("hour_local")
                    or item.get("local_hour")
                    or item.get("observation_hour_local")
                    or item.get("time_hour")
                )
                if hour is None:
                    hour = _local_hour_from_timestamp(
                        item.get("at")
                        or item.get("observed_at")
                        or item.get("observation_time")
                        or item.get("report_time")
                        or item.get("reportTime"),
                        local_timezone=local_timezone,
                    )
                if value_keys:
                    val = None
                    for value_key in value_keys:
                        val = _to_float(item.get(value_key))
                        if val is not None:
                            break
                elif "temp_f" in item or "temperature_f" in item:
                    val_raw = _to_float(item.get("temp_f") or item.get("temperature_f"))
                    val = _convert_temp(float(val_raw), "F", "C") if val_raw is not None else None
                else:
                    val = _to_float(
                        item.get("temp_c")
                        or item.get("temperature_c")
                        or item.get("observed_temp_c")
                        or item.get("value_c")
                        or item.get("value")
                        or item.get("temperature")
                    )
                    if val is not None:
                        val = _convert_temp(float(val), str(item.get("unit") or feature_unit or "C"), "C")
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)) and len(item) >= 2:
                hour = _to_float(item[0])
                val = _to_float(item[1])
            else:
                hour = None
                val = None
            if hour is not None and val is not None:
                out.append((_clamp(float(hour), 0.0, 23.99), float(val)))
    return sorted(out, key=lambda pair: pair[0])


def _temperature_observation_points_from_features(
    features: Mapping[str, Any],
    *,
    hour_local: float,
    observed_c: float,
) -> List[Tuple[float, float]]:
    feature_unit = _feature_unit(features) or "C"
    local_timezone = str(
        features.get("observation_hour_local_timezone")
        or features.get("event_tz")
        or features.get("timezone")
        or ""
    ).strip()
    raw_values = [
        features.get("temperature_path_observations"),
        features.get("observed_temperature_path"),
        features.get("observed_temperature_points"),
        features.get("temperature_observation_points"),
        features.get("amsc_temperature_observations"),
        features.get("zuuu_metar_raw_amsc_temperature_observations"),
        features.get("zuuu_metar_raw_amsc_today_temperature_points"),
        features.get("awc_temperature_observations"),
        features.get("zuuu_metar_raw_awc_temperature_observations"),
        features.get("zuuu_metar_raw_awc_today_temperature_points"),
        features.get("metar_temperature_observations"),
    ]
    points: List[Tuple[float, float]] = []
    for raw in raw_values:
        points.extend(_hour_value_series_from_raw(raw, feature_unit=feature_unit, local_timezone=local_timezone))
    points.append((_clamp(float(hour_local), 0.0, 23.99), float(observed_c)))
    by_hour: Dict[float, float] = {}
    for hour, temp in points:
        if float(hour) <= float(hour_local) + 0.15:
            by_hour[round(float(hour), 3)] = float(temp)
    return sorted(by_hour.items(), key=lambda pair: pair[0])


def _solve_3x3(matrix: Sequence[Sequence[float]], rhs: Sequence[float]) -> Optional[Tuple[float, float, float]]:
    rows = [[float(matrix[i][j]) for j in range(3)] + [float(rhs[i])] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda idx: abs(rows[idx][col]))
        if abs(rows[pivot][col]) <= 1e-9:
            return None
        if pivot != col:
            rows[col], rows[pivot] = rows[pivot], rows[col]
        div = rows[col][col]
        rows[col] = [value / div for value in rows[col]]
        for row_idx in range(3):
            if row_idx == col:
                continue
            factor = rows[row_idx][col]
            rows[row_idx] = [rows[row_idx][j] - factor * rows[col][j] for j in range(4)]
    return rows[0][3], rows[1][3], rows[2][3]


def _local_quadratic_temperature_fit(
    features: Mapping[str, Any],
    *,
    hour_local: float,
    observed_c: float,
) -> Dict[str, Any]:
    distinct = _temperature_observation_points_from_features(
        features,
        hour_local=float(hour_local),
        observed_c=float(observed_c),
    )
    if len(distinct) < 3:
        return {
            "available": False,
            "na_reason": "insufficient_observed_temperature_points",
            "point_count": int(len(distinct)),
            "points": [{"hour_local": float(hour), "temp_c": float(temp)} for hour, temp in distinct],
        }
    xs = [float(hour) - float(hour_local) for hour, _ in distinct]
    span = max(xs) - min(xs)
    if abs(span) < 0.75:
        return {
            "available": False,
            "na_reason": "observed_temperature_points_span_too_short",
            "point_count": int(len(distinct)),
            "span_hours": float(abs(span)),
            "points": [{"hour_local": float(hour), "temp_c": float(temp)} for hour, temp in distinct],
        }
    matrix = [[0.0, 0.0, 0.0] for _ in range(3)]
    rhs = [0.0, 0.0, 0.0]
    for x, (_, temp) in zip(xs, distinct):
        basis = [1.0, float(x), float(x) * float(x)]
        weight = 1.0 / (0.35 + abs(float(x)))
        for i in range(3):
            rhs[i] += weight * basis[i] * float(temp)
            for j in range(3):
                matrix[i][j] += weight * basis[i] * basis[j]
    solved = _solve_3x3(matrix, rhs)
    if solved is None:
        return {
            "available": False,
            "na_reason": "observed_temperature_quadratic_fit_singular",
            "point_count": int(len(distinct)),
            "span_hours": float(abs(span)),
            "points": [{"hour_local": float(hour), "temp_c": float(temp)} for hour, temp in distinct],
        }
    intercept, rate, quad = solved
    residuals = []
    for x, (_, temp) in zip(xs, distinct):
        pred = intercept + rate * x + quad * x * x
        residuals.append(float(temp) - pred)
    rmse = math.sqrt(sum(value * value for value in residuals) / max(1, len(residuals)))
    confidence = _clamp(0.30 + 0.10 * len(distinct) + 0.08 * min(abs(span), 4.0) - 0.18 * rmse, 0.05, 0.92)
    return {
        "available": True,
        "source": "local_quadratic_observed_temperature_path",
        "point_count": int(len(distinct)),
        "span_hours": float(abs(span)),
        "points": [{"hour_local": float(hour), "temp_c": float(temp)} for hour, temp in distinct],
        "fit_temp_c": float(intercept),
        "rate_hat_c_per_hour": float(_clamp(rate, -2.5, 2.5)),
        "accel_hat_c_per_hour2": float(_clamp(2.0 * quad, -2.5, 2.5)),
        "residual_rmse_c": float(rmse),
        "confidence": float(confidence),
    }


def _remaining_peak_window_probability(
    distribution: Sequence[Mapping[str, Any]],
    *,
    hour_local: float,
    pressure: float,
) -> float:
    if not distribution:
        return float(_clamp(pressure, 0.0, 1.0))
    prob = 0.0
    for item in distribution:
        hour = _to_float(item.get("hour_local"))
        weight = _to_float(item.get("prob"))
        if hour is None or weight is None:
            continue
        if float(hour) >= float(hour_local) - 1e-9:
            prob += float(weight)
    return float(_clamp(prob, 0.0, 1.0))


def _pct_value(value: Any) -> Optional[float]:
    raw = _to_float(value)
    if raw is None:
        return None
    return _clamp(float(raw) if float(raw) > 1.0 else float(raw) * 100.0, 0.0, 100.0)


def _cloud_forcing_pressure(value: Any, *, layer: str = "total") -> float:
    pct = _pct_value(value)
    if pct is None:
        return 0.0
    layer_key = str(layer or "total").strip().lower()
    if layer_key in {"low", "low_cloud"}:
        return _clamp((pct - 32.0) / 58.0, 0.0, 1.0)
    if layer_key in {"mid", "middle", "mid_cloud"}:
        return 0.78 * _clamp((pct - 48.0) / 48.0, 0.0, 1.0)
    if layer_key in {"high", "high_cloud"}:
        return 0.38 * _clamp((pct - 65.0) / 35.0, 0.0, 1.0)
    return _clamp((pct - 55.0) / 45.0, 0.0, 1.0)


def _forcing_pressure_from_value(value: Any, kind: str) -> float:
    kind_key = str(kind or "").strip().lower()
    if kind_key == "cloud_pct":
        return _cloud_forcing_pressure(value, layer="total")
    if kind_key == "low_cloud_pct":
        return _cloud_forcing_pressure(value, layer="low")
    if kind_key == "mid_cloud_pct":
        return _cloud_forcing_pressure(value, layer="mid")
    if kind_key == "high_cloud_pct":
        return _cloud_forcing_pressure(value, layer="high")
    val = _to_float(value)
    if val is None:
        return 0.0
    if kind_key == "precip_mm_h":
        return _clamp(math.log1p(max(0.0, float(val))) / math.log(4.0), 0.0, 1.0)
    if kind_key == "low_radiation_ratio":
        return _clamp((0.78 - float(val)) / 0.58, 0.0, 1.0)
    if kind_key == "dewpoint_spread_c":
        return _clamp((2.8 - float(val)) / 2.8, 0.0, 1.0)
    return _clamp(float(val), 0.0, 1.0)


def _forcing_onset_profile(
    features: Mapping[str, Any],
    *,
    name: str,
    pressure: float,
    hour_local: float,
    peak_distribution: Sequence[Mapping[str, Any]],
    distribution_keys: Sequence[str],
    hourly_keys: Sequence[str],
    hourly_pressure_kind: str,
    hourly_value_keys: Sequence[str] = (),
) -> Dict[str, Any]:
    distribution: List[Dict[str, float]] = []
    source = ""
    for key in distribution_keys:
        distribution = _hour_probability_distribution_from_raw(features.get(key))
        if distribution:
            source = key
            break
    single_hour = _feature(
        features,
        f"{name}_onset_hour_local",
        f"expected_{name}_onset_hour_local",
        f"{name}_expected_onset_hour_local",
    )
    if not distribution and single_hour is not None:
        distribution = [{"hour_local": _clamp(float(single_hour), 0.0, 23.99), "prob": 1.0}]
        source = f"{name}_onset_hour_local"

    peak_max = max((_to_float(item.get("hour_local")) or float(hour_local) for item in peak_distribution), default=float(hour_local))
    hourly_pressure = None
    hourly_source = ""
    hourly_pressure_points: List[Dict[str, float]] = []
    for key in hourly_keys:
        series = _hour_value_series_from_raw(
            features.get(key),
            feature_unit="C",
            value_keys=hourly_value_keys,
        )
        in_window = [
            (hour, value)
            for hour, value in series
            if float(hour_local) - 1e-9 <= float(hour) <= float(peak_max) + 1e-9
        ]
        if not in_window:
            continue
        hourly_pressure_points = [
            {
                "hour_local": float(hour),
                "value": float(value),
                "pressure": float(_forcing_pressure_from_value(value, hourly_pressure_kind)),
            }
            for hour, value in in_window
        ]
        hourly_pressure = max((item["pressure"] for item in hourly_pressure_points), default=0.0)
        hourly_source = key
        break
    if not distribution and hourly_pressure_points:
        weighted_hours = [
            (float(item["hour_local"]), float(item["pressure"]))
            for item in hourly_pressure_points
            if float(item["pressure"]) > 0.03
        ]
        if weighted_hours:
            probs = _normalize([weight for _, weight in weighted_hours])
            distribution = [
                {"hour_local": float(hour), "prob": float(prob)}
                for (hour, _), prob in zip(weighted_hours, probs)
            ]
            source = f"{hourly_source}:derived_pressure_hours"
    within = _remaining_peak_window_probability(distribution, hour_local=float(hour_local), pressure=float(pressure))
    effective_pressure = max(float(_clamp(pressure, 0.0, 1.0)), float(within) * float(_clamp(pressure, 0.0, 1.0)))
    if hourly_pressure is not None:
        effective_pressure = max(effective_pressure, float(hourly_pressure))
    return {
        "pressure": float(_clamp(effective_pressure, 0.0, 1.0)),
        "current_pressure": float(_clamp(pressure, 0.0, 1.0)),
        "within_peak_window_probability": float(within),
        "onset_distribution": distribution,
        "source": source or "current_pressure_fallback",
        "hourly_source": hourly_source,
        "hourly_pressure_points": hourly_pressure_points,
    }


def _observed_temperature_trajectory_regime_v1(
    features: Mapping[str, Any],
    *,
    observed_c: float,
    hour_local: float,
    peak_hour: Optional[float],
    peak_window_start: Optional[float],
    peak_window_end: Optional[float],
    hours_to_peak: float,
    momentum: Mapping[str, Any],
    base_rate_c_per_hour: Optional[float],
    default_delta_mu_c: float,
    default_delta_sigma_c: float,
    radiation_factor: float,
    cloud_discount: float,
    precip_cooling_c: float,
    wet_bulb_penalty_c: float,
) -> Dict[str, Any]:
    rates_raw = momentum.get("rates_c_per_hour") if isinstance(momentum, Mapping) else {}
    context_raw = momentum.get("context_rates_c_per_hour") if isinstance(momentum, Mapping) else {}
    rates: Dict[str, float] = {
        str(key): float(value)
        for key, value in (rates_raw.items() if isinstance(rates_raw, Mapping) else [])
        if _to_float(value) is not None
    }
    context_rates: Dict[str, float] = {
        str(key): float(value)
        for key, value in (context_raw.items() if isinstance(context_raw, Mapping) else [])
        if _to_float(value) is not None
    }
    local_quadratic_fit = _local_quadratic_temperature_fit(
        features,
        hour_local=float(hour_local),
        observed_c=float(observed_c),
    )
    local_fit_available = bool(local_quadratic_fit.get("available"))
    seed_rates = {
        key: value
        for key, value in rates.items()
        if (_momentum_window_hours(key) or 0.0) >= 0.5
    }
    momentum_seed_rate = _temperature_momentum_recent_rate(seed_rates)
    if momentum_seed_rate is None and context_rates:
        momentum_seed_rate = sum(context_rates.values()) / max(1, len(context_rates))
    if (
        not local_fit_available
        and (base_rate_c_per_hour is None or not math.isfinite(float(base_rate_c_per_hour)))
        and momentum_seed_rate is None
    ):
        return {
            "enabled": True,
            "available": False,
            "source": "observed_temperature_trajectory_regime_v1",
            "na_reason": "confirmed_temperature_trajectory_rate_missing",
            "role": "observed_realization",
            "source_class": "airport_temperature_path",
            "dedupe_policy": "replaces_legacy_linear_rate_projection_when_available",
        }

    peak_distribution = _peak_hour_distribution_from_features(
        features,
        peak_hour=peak_hour,
        peak_window_start=peak_window_start,
        peak_window_end=peak_window_end,
    )
    if not peak_distribution:
        return {
            "enabled": True,
            "available": False,
            "source": "observed_temperature_trajectory_regime_v1",
            "na_reason": "peak_hour_distribution_missing",
            "role": "observed_realization",
            "source_class": "airport_temperature_path",
            "dedupe_policy": "replaces_legacy_linear_rate_projection_when_available",
        }

    recent_rate = momentum_seed_rate if rates else _temperature_momentum_recent_rate(rates)
    context_rate = _to_float(momentum.get("context_effective_rate_c_per_hour")) if isinstance(momentum, Mapping) else None
    if context_rate is None and context_rates:
        context_rate = sum(context_rates.values()) / max(1, len(context_rates))
    momentum_state = str(momentum.get("state") or "").strip() if isinstance(momentum, Mapping) else ""
    context_warming_watch = bool(isinstance(momentum, Mapping) and momentum.get("context_warming_watch"))
    base_rate_seed = (
        float(base_rate_c_per_hour)
        if base_rate_c_per_hour is not None and math.isfinite(float(base_rate_c_per_hour))
        else float(momentum_seed_rate or 0.0)
    )
    rate_hat = (
        float(_to_float(local_quadratic_fit.get("rate_hat_c_per_hour")) or 0.0)
        if local_fit_available
        else float(base_rate_seed)
    )
    if not local_fit_available:
        if context_warming_watch and context_rate is not None:
            if recent_rate is not None and float(recent_rate) > float(context_rate) and float(context_rate) >= 0.25:
                rate_hat = 0.70 * float(recent_rate) + 0.30 * float(context_rate)
            else:
                rate_hat = float(context_rate)
        elif recent_rate is not None and context_rate is not None:
            rate_hat = 0.62 * float(recent_rate) + 0.38 * float(context_rate)
        elif recent_rate is not None:
            rate_hat = 0.70 * float(recent_rate) + 0.30 * float(base_rate_seed)
        elif context_rate is not None:
            rate_hat = 0.65 * float(base_rate_seed) + 0.35 * float(context_rate)

    accel_hat = (
        float(_to_float(local_quadratic_fit.get("accel_hat_c_per_hour2")) or 0.0)
        if local_fit_available
        else 0.0
    )
    if not local_fit_available and recent_rate is not None and context_rate is not None:
        recent_windows = [_momentum_window_hours(k) for k in rates.keys()]
        context_windows = [_momentum_window_hours(k) for k in context_rates.keys()]
        recent_h = min([v for v in recent_windows if v is not None] or [1.0])
        context_h = max([v for v in context_windows if v is not None] or [max(2.0, recent_h + 1.0)])
        accel_hat = (float(recent_rate) - float(context_rate)) / max(0.5, float(context_h) - float(recent_h))
    accel_hat = _clamp(float(accel_hat), -1.5, 1.5)

    all_rates = list(rates.values()) + list(context_rates.values())
    if len(all_rates) >= 2:
        mean_rate = sum(all_rates) / len(all_rates)
        spread = math.sqrt(sum((rate - mean_rate) ** 2 for rate in all_rates) / max(1, len(all_rates) - 1))
    else:
        spread = 0.0
    soft_intraday_momentum = _soft_intraday_temperature_momentum_profile(features, hard_rates=rates)
    soft_intraday_confidence_boost = _to_float(soft_intraday_momentum.get("confidence_boost")) or 0.0
    curvature_confidence = _clamp(
        0.35 + 0.12 * len(all_rates) - 0.12 * spread + float(soft_intraday_confidence_boost),
        0.05,
        0.95,
    )
    if local_fit_available:
        curvature_confidence = max(
            float(curvature_confidence),
            float(_to_float(local_quadratic_fit.get("confidence")) or 0.0),
        )
    realization_scale = _to_float(momentum.get("realization_scale")) if isinstance(momentum, Mapping) else None
    if realization_scale is None:
        realization_scale = 1.0
    if local_fit_available:
        path_realization_scale = 1.0
    elif context_warming_watch:
        path_realization_scale = max(0.80, _clamp(float(realization_scale), 0.0, 1.0))
    else:
        path_realization_scale = _clamp(float(realization_scale), 0.0, 1.0)
    rate_hat *= path_realization_scale
    accel_hat *= path_realization_scale

    cloud = _effective_cloud_cover_pct(features)
    low_cloud = _feature(features, "cloud_cover_low_pct", "low_cloud_cover_pct", "cloud_low_pct")
    mid_cloud = _feature(features, "cloud_cover_mid_pct", "mid_cloud_cover_pct", "cloud_mid_pct")
    high_cloud = _feature(features, "cloud_cover_high_pct", "high_cloud_cover_pct", "cloud_high_pct")
    low_cloud_pressure = _cloud_forcing_pressure(low_cloud, layer="low") if low_cloud is not None else 0.0
    mid_cloud_pressure = _cloud_forcing_pressure(mid_cloud, layer="mid") if mid_cloud is not None else 0.0
    high_cloud_pressure = _cloud_forcing_pressure(high_cloud, layer="high") if high_cloud is not None else 0.0
    cloud_pressure = max(
        _cloud_forcing_pressure(cloud, layer="total") if cloud is not None else 0.0,
        low_cloud_pressure,
        mid_cloud_pressure,
        high_cloud_pressure,
    )
    precip_pressure = _clamp(float(precip_cooling_c) / 2.0, 0.0, 1.0)
    radiation_pressure = _clamp((0.78 - float(radiation_factor)) / 0.58, 0.0, 1.0)
    dewpoint_pressure = _clamp(float(wet_bulb_penalty_c) / 1.1, 0.0, 1.0)
    cloud_onset = _forcing_onset_profile(
        features,
        name="cloud",
        pressure=float(cloud_pressure),
        hour_local=float(hour_local),
        peak_distribution=peak_distribution,
        distribution_keys=(
            "cloud_onset_hour_distribution",
            "low_cloud_onset_hour_distribution",
            "mid_cloud_onset_hour_distribution",
            "high_cloud_onset_hour_distribution",
            "cloud_cover_onset_hour_distribution",
        ),
        hourly_keys=(
            "cloud_cover_hourly_pct",
            "cloud_hourly_pct",
            "total_cloud_hourly_pct",
            "cloud_cover_total_hourly_pct",
        ),
        hourly_pressure_kind="cloud_pct",
        hourly_value_keys=(
            "cloud_cover_pct",
            "cloud_cover",
            "total_cloud_pct",
            "cloud_pct",
            "value_pct",
            "value",
        ),
    )
    low_cloud_onset = _forcing_onset_profile(
        features,
        name="low_cloud",
        pressure=float(low_cloud_pressure),
        hour_local=float(hour_local),
        peak_distribution=peak_distribution,
        distribution_keys=(
            "low_cloud_onset_hour_distribution",
            "cloud_cover_low_onset_hour_distribution",
            "low_cloud_cover_onset_hour_distribution",
        ),
        hourly_keys=(
            "low_cloud_hourly_pct",
            "cloud_cover_low_hourly_pct",
            "cloud_low_hourly_pct",
            "low_cloud_cover_hourly_pct",
        ),
        hourly_pressure_kind="low_cloud_pct",
        hourly_value_keys=(
            "low_cloud_pct",
            "cloud_cover_low_pct",
            "low_cloud_cover_pct",
            "cloud_low_pct",
            "value_pct",
            "value",
        ),
    )
    mid_cloud_onset = _forcing_onset_profile(
        features,
        name="mid_cloud",
        pressure=float(mid_cloud_pressure),
        hour_local=float(hour_local),
        peak_distribution=peak_distribution,
        distribution_keys=(
            "mid_cloud_onset_hour_distribution",
            "cloud_cover_mid_onset_hour_distribution",
            "mid_cloud_cover_onset_hour_distribution",
        ),
        hourly_keys=(
            "mid_cloud_hourly_pct",
            "cloud_cover_mid_hourly_pct",
            "cloud_mid_hourly_pct",
            "mid_cloud_cover_hourly_pct",
        ),
        hourly_pressure_kind="mid_cloud_pct",
        hourly_value_keys=(
            "mid_cloud_pct",
            "cloud_cover_mid_pct",
            "mid_cloud_cover_pct",
            "cloud_mid_pct",
            "value_pct",
            "value",
        ),
    )
    high_cloud_onset = _forcing_onset_profile(
        features,
        name="high_cloud",
        pressure=float(high_cloud_pressure),
        hour_local=float(hour_local),
        peak_distribution=peak_distribution,
        distribution_keys=(
            "high_cloud_onset_hour_distribution",
            "cloud_cover_high_onset_hour_distribution",
            "high_cloud_cover_onset_hour_distribution",
        ),
        hourly_keys=(
            "high_cloud_hourly_pct",
            "cloud_cover_high_hourly_pct",
            "cloud_high_hourly_pct",
            "high_cloud_cover_hourly_pct",
        ),
        hourly_pressure_kind="high_cloud_pct",
        hourly_value_keys=(
            "high_cloud_pct",
            "cloud_cover_high_pct",
            "high_cloud_cover_pct",
            "cloud_high_pct",
            "value_pct",
            "value",
        ),
    )
    precip_onset = _forcing_onset_profile(
        features,
        name="rain",
        pressure=float(precip_pressure),
        hour_local=float(hour_local),
        peak_distribution=peak_distribution,
        distribution_keys=(
            "rain_onset_hour_distribution",
            "precip_onset_hour_distribution",
            "precipitation_onset_hour_distribution",
            "rain_start_hour_distribution",
            "precipitation_start_hour_distribution",
        ),
        hourly_keys=(
            "precip_rate_hourly_mm_per_h",
            "rain_rate_hourly_mm_per_h",
            "precipitation_rate_hourly_mm_per_h",
            "hourly_precip_rate_mm_per_h",
            "hourly_rain_rate_mm_per_h",
        ),
        hourly_pressure_kind="precip_mm_h",
        hourly_value_keys=(
            "precip_rate_mm_per_h",
            "precipitation_rate_mm_per_h",
            "rain_rate_mm_per_h",
            "precip_mm_per_h",
            "rain_mm_per_h",
            "value",
        ),
    )
    radiation_onset = _forcing_onset_profile(
        features,
        name="radiation",
        pressure=float(radiation_pressure),
        hour_local=float(hour_local),
        peak_distribution=peak_distribution,
        distribution_keys=(
            "low_radiation_onset_hour_distribution",
            "radiation_drop_hour_distribution",
            "clear_sky_ratio_drop_hour_distribution",
        ),
        hourly_keys=("radiation_ratio_hourly", "cti_hourly", "clear_sky_ratio_hourly"),
        hourly_pressure_kind="low_radiation_ratio",
        hourly_value_keys=(
            "radiation_ratio",
            "clear_sky_ratio",
            "cti",
            "value",
        ),
    )
    dewpoint_onset = _forcing_onset_profile(
        features,
        name="dewpoint",
        pressure=float(dewpoint_pressure),
        hour_local=float(hour_local),
        peak_distribution=peak_distribution,
        distribution_keys=(
            "dewpoint_ceiling_onset_hour_distribution",
            "dewpoint_spread_narrowing_hour_distribution",
        ),
        hourly_keys=("dewpoint_spread_hourly_c", "dewpoint_depression_hourly_c"),
        hourly_pressure_kind="dewpoint_spread_c",
        hourly_value_keys=(
            "dewpoint_spread_c",
            "dewpoint_depression_c",
            "spread_c",
            "value_c",
            "value",
        ),
    )
    cloud_layer_onsets = {
        "low": low_cloud_onset,
        "mid": mid_cloud_onset,
        "high": high_cloud_onset,
    }
    cloud_pressure = max(
        float(cloud_onset["pressure"]),
        float(low_cloud_onset["pressure"]),
        float(mid_cloud_onset["pressure"]),
        float(high_cloud_onset["pressure"]),
    )
    precip_pressure = float(precip_onset["pressure"])
    radiation_pressure = float(radiation_onset["pressure"])
    dewpoint_pressure = float(dewpoint_onset["pressure"])
    forcing_turn_probability = max(cloud_pressure, precip_pressure, radiation_pressure, dewpoint_pressure)
    peak_width = (
        max(0.0, float(peak_window_end) - float(peak_window_start))
        if peak_window_start is not None and peak_window_end is not None
        else 0.0
    )
    positive_context_count = sum(
        1
        for rate in context_rates.values()
        if float(rate) >= TEMPERATURE_MOMENTUM_CONTEXT_CONFIRM_MIN_C_PER_HOUR
    )
    negative_rate_count = sum(1 for rate in all_rates if float(rate) <= -0.05)
    warming_acceleration_confirmed = bool(
        recent_rate is not None
        and context_rate is not None
        and float(recent_rate) > float(context_rate)
        and float(context_rate) >= TEMPERATURE_MOMENTUM_PEAK_RATE_CONFIRM_MIN_C_PER_HOUR
        and positive_context_count > 0
        and negative_rate_count == 0
    )
    conflict_score = _clamp(spread / 1.4, 0.0, 1.0)
    if warming_acceleration_confirmed:
        conflict_score *= 0.35

    raw_regimes = {
        "accelerating_warming": 0.05,
        "decelerating_warming": 0.05,
        "forced_turn_risk": 0.05,
        "rebound_warming": 0.05,
        "plateau": 0.05,
        "low_confidence": 0.05,
    }
    if rate_hat > 0.20 and accel_hat >= 0.03:
        raw_regimes["accelerating_warming"] += 0.75 * (1.0 - forcing_turn_probability)
    if rate_hat > 0.20 and accel_hat < 0.03:
        raw_regimes["decelerating_warming"] += 0.65 * (1.0 - forcing_turn_probability)
    if rate_hat > 0.05 and forcing_turn_probability > 0.20:
        raw_regimes["forced_turn_risk"] += 0.85 * forcing_turn_probability
    if rate_hat <= 0.05 and radiation_factor >= 0.82 and cloud_discount >= 0.65 and precip_cooling_c <= 0.05:
        raw_regimes["rebound_warming"] += 0.45
    if abs(rate_hat) < 0.18 or peak_width >= 2.5:
        raw_regimes["plateau"] += 0.35 + min(0.25, peak_width / 10.0)
    if (not local_fit_available and len(all_rates) < 2) or conflict_score > 0.45:
        raw_regimes["low_confidence"] += 0.25 + 0.55 * conflict_score
    if soft_intraday_confidence_boost > 0:
        confidence_relief = 0.35 * _clamp(float(soft_intraday_confidence_boost) / 0.08, 0.0, 1.0)
        raw_regimes["low_confidence"] *= max(0.65, 1.0 - confidence_relief)
    regime_keys = list(raw_regimes.keys())
    regime_probs_vals = _normalize([raw_regimes[key] for key in regime_keys])
    regime_probs = {key: float(value) for key, value in zip(regime_keys, regime_probs_vals)}

    max_tau = max(0.0, max(float(item["hour_local"]) - float(hour_local) for item in peak_distribution))
    envelope = max(0.0, float(default_delta_mu_c))
    path_values: List[Tuple[float, float, str, float]] = []
    path_candidates: List[Dict[str, Any]] = []

    def _kinematic_baseline_delta(tau: float) -> float:
        if tau <= EPS:
            return 0.0
        steps = max(2, min(8, int(math.ceil(float(tau) * 2.0))))
        candidates = []
        for step in range(1, steps + 1):
            dt = float(tau) * float(step) / float(steps)
            candidates.append(float(rate_hat) * dt + 0.5 * float(accel_hat) * dt * dt)
        if float(accel_hat) < -EPS and float(rate_hat) > 0:
            vertex = -float(rate_hat) / float(accel_hat)
            if 0.0 < vertex < float(tau):
                candidates.append(float(rate_hat) * vertex + 0.5 * float(accel_hat) * vertex * vertex)
        cap = max(0.0, envelope * _clamp(float(tau) / max(0.25, max_tau or tau), 0.15, 1.25) * 1.20 + 0.35)
        return _clamp(max(candidates or [0.0]), 0.0, cap)

    kinematic_baseline_mu = float(observed_c) + _kinematic_baseline_delta(max_tau)

    def _regime_path_delta(regime: str, tau: float, sample_forcing_turn_probability: float) -> float:
        if tau <= EPS:
            return 0.0
        tau_scale = tau / max(0.25, float(hours_to_peak) if hours_to_peak > 0 else max_tau or tau)
        forcing_pressure = _clamp(float(sample_forcing_turn_probability), 0.0, 1.0)
        tail_allowance = 0.30 + 0.18 * _clamp(float(rate_hat) / 1.0, 0.0, 1.0) * (1.0 - forcing_pressure)
        cap_scale = _clamp(tau_scale, 0.15, 1.25)
        cap_multiplier = 1.15
        effective_rate = float(rate_hat)
        effective_accel = float(accel_hat)
        multiplier = 1.0
        if regime == "accelerating_warming":
            effective_accel = max(float(accel_hat), 0.03)
            multiplier = 1.0 - 0.20 * forcing_pressure
        elif regime == "decelerating_warming":
            effective_accel = min(float(accel_hat), -0.04)
            multiplier = 1.0 - 0.10 * forcing_pressure
        elif regime == "forced_turn_risk":
            effective_accel = min(float(accel_hat), -0.08)
            effective_rate *= 1.0 - 0.35 * forcing_pressure
            multiplier = 1.0 - 0.45 * forcing_pressure
            cap_multiplier = 0.72
        elif regime == "rebound_warming":
            effective_rate = max(float(rate_hat), 0.18 * _clamp(float(radiation_factor), 0.0, 1.2))
            effective_accel = max(float(accel_hat), 0.02)
            multiplier = (1.0 - 0.30 * forcing_pressure) * _clamp(float(cloud_discount) + 0.25, 0.25, 1.0)
            cap_multiplier = 0.85
        elif regime == "plateau":
            effective_rate = max(0.0, float(rate_hat)) * 0.35
            effective_accel = min(float(accel_hat), 0.0)
            cap_multiplier = 0.52
        elif regime == "low_confidence":
            effective_rate = 0.50 * float(rate_hat)
            effective_accel = 0.0
            multiplier = 0.70
            cap_multiplier = 0.90
        cap = envelope * cap_scale * cap_multiplier + tail_allowance
        steps = max(2, min(8, int(math.ceil(float(tau) * 2.0))))
        candidates = []
        for step in range(1, steps + 1):
            dt = float(tau) * float(step) / float(steps)
            candidates.append((effective_rate * dt + 0.5 * effective_accel * dt * dt) * multiplier)
        if effective_accel < -EPS and effective_rate > 0:
            vertex = -effective_rate / effective_accel
            if 0.0 < vertex < float(tau):
                candidates.append((effective_rate * vertex + 0.5 * effective_accel * vertex * vertex) * multiplier)
        return _clamp(max(candidates or [0.0]), 0.0, max(0.0, cap))

    def _weighted_pick(items: Sequence[Tuple[Any, float]], u: float) -> Any:
        cleaned = [(item, max(0.0, float(weight))) for item, weight in items]
        total = sum(weight for _, weight in cleaned)
        if total <= EPS:
            return cleaned[0][0] if cleaned else None
        target = (float(u) % 1.0) * total
        cumulative = 0.0
        for item, weight in cleaned:
            cumulative += weight
            if cumulative >= target:
                return item
        return cleaned[-1][0]

    def _sample_onset_hour(profile: Mapping[str, Any], u: float) -> Optional[float]:
        raw_distribution = profile.get("onset_distribution") if isinstance(profile, Mapping) else None
        if not isinstance(raw_distribution, Sequence) or isinstance(raw_distribution, (str, bytes, bytearray)):
            return None
        items: List[Tuple[float, float]] = []
        for raw in raw_distribution:
            if not isinstance(raw, Mapping):
                continue
            hour = _to_float(raw.get("hour_local"))
            prob = _to_float(raw.get("prob"))
            if hour is not None and prob is not None and prob > 0:
                items.append((float(hour), float(prob)))
        picked = _weighted_pick(items, u) if items else None
        return float(picked) if picked is not None else None

    def _sample_forcing_pressure(peak_hour_sample: float, u: float) -> float:
        profiles = (
            (cloud_onset, cloud_pressure, 0.11),
            (low_cloud_onset, float(low_cloud_onset["pressure"]), 0.19),
            (mid_cloud_onset, float(mid_cloud_onset["pressure"]), 0.27),
            (high_cloud_onset, float(high_cloud_onset["pressure"]), 0.31),
            (precip_onset, precip_pressure, 0.37),
            (radiation_onset, radiation_pressure, 0.61),
            (dewpoint_onset, dewpoint_pressure, 0.83),
        )
        pressure = float(forcing_turn_probability)
        for profile, base_pressure, offset in profiles:
            onset_hour = _sample_onset_hour(profile, u + offset)
            if onset_hour is None:
                continue
            if onset_hour <= float(peak_hour_sample):
                lead = max(0.0, float(onset_hour) - float(hour_local))
                timing_scale = _clamp(1.18 - 0.07 * lead, 0.55, 1.18)
            else:
                timing_scale = 0.35
            pressure = max(pressure, float(base_pressure) * timing_scale)
        return _clamp(pressure, 0.0, 1.0)

    def _first_onset_hour(profile: Mapping[str, Any]) -> Optional[float]:
        raw_distribution = profile.get("onset_distribution") if isinstance(profile, Mapping) else None
        if not isinstance(raw_distribution, Sequence) or isinstance(raw_distribution, (str, bytes, bytearray)):
            return None
        hours = [
            float(hour)
            for item in raw_distribution
            if isinstance(item, Mapping)
            for hour in [_to_float(item.get("hour_local"))]
            if hour is not None
        ]
        return min(hours) if hours else None

    forcing_contributors = []
    for key, profile in (
        ("cloud", cloud_onset),
        ("low_cloud", low_cloud_onset),
        ("mid_cloud", mid_cloud_onset),
        ("high_cloud", high_cloud_onset),
        ("precipitation", precip_onset),
        ("radiation", radiation_onset),
        ("dewpoint", dewpoint_onset),
    ):
        if not isinstance(profile, Mapping):
            continue
        forcing_contributors.append(
            {
                "key": key,
                "pressure": float(_to_float(profile.get("pressure")) or 0.0),
                "current_pressure": float(_to_float(profile.get("current_pressure")) or 0.0),
                "next_onset_hour_local": _first_onset_hour(profile),
                "source": str(profile.get("source") or ""),
                "hourly_source": str(profile.get("hourly_source") or ""),
            }
        )
    forcing_contributors.sort(key=lambda item: float(item.get("pressure") or 0.0), reverse=True)

    regime_items = [(key, float(regime_probs[key])) for key in regime_keys]
    peak_items = [(dict(item), float(item.get("prob") or 0.0)) for item in peak_distribution]
    draw_count = int(_clamp(_env_float("PM_BAYES_TRAJECTORY_REGIME_V1_MC_DRAWS", 512.0), 64.0, 2000.0))
    for draw_idx in range(draw_count):
        u_regime = ((draw_idx + 0.5) * 0.6180339887498949) % 1.0
        u_peak = ((draw_idx + 0.5) * 0.4142135623730950) % 1.0
        u_forcing = ((draw_idx + 0.5) * 0.7320508075688772) % 1.0
        regime = str(_weighted_pick(regime_items, u_regime))
        peak_item = _weighted_pick(peak_items, u_peak)
        if not isinstance(peak_item, Mapping):
            continue
        peak_hour_sample = float(peak_item.get("hour_local") or hour_local)
        tau = max(0.0, peak_hour_sample - float(hour_local))
        sample_forcing = _sample_forcing_pressure(peak_hour_sample, u_forcing)
        steps = max(2, min(12, int(math.ceil(max(0.25, tau) * 2.0))))
        path_max = float(observed_c)
        path_max_delta = 0.0
        for step in range(1, steps + 1):
            dt = tau * float(step) / float(steps)
            delta = _regime_path_delta(regime, dt, sample_forcing)
            value = float(observed_c) + delta
            if value > path_max:
                path_max = value
                path_max_delta = delta
        path_values.append((1.0, peak_hour_sample, regime, path_max))
        if len(path_candidates) < 80:
            path_candidates.append(
                {
                    "draw": int(draw_idx),
                    "hour_local": float(peak_hour_sample),
                    "regime": regime,
                    "forcing_turn_probability": float(sample_forcing),
                    "temp_c": float(path_max),
                    "delta_c": float(path_max_delta),
                }
            )
    total_path_weight = sum(weight for weight, _, _, _ in path_values) or 1.0
    path_mean = sum(weight * value for weight, _, _, value in path_values) / total_path_weight
    path_mu = max(float(observed_c), float(path_mean))
    path_upper_candidate = max((value for _, _, _, value in path_values), default=float(observed_c))
    path_var = sum(weight * (value - path_mean) ** 2 for weight, _, _, value in path_values) / total_path_weight
    ordered_path = sorted(path_values, key=lambda row: row[3])
    p90_index = min(len(ordered_path) - 1, max(0, int(math.ceil(0.90 * len(ordered_path))) - 1))
    path_p90 = ordered_path[p90_index][3] if ordered_path else path_upper_candidate
    regime_uncertainty = 0.25 + 0.55 * regime_probs.get("low_confidence", 0.0) + 0.35 * regime_probs.get("forced_turn_risk", 0.0)
    path_sigma = math.sqrt(
        max(0.0, path_var)
        + (max(0.2, float(default_delta_sigma_c)) * 0.35) ** 2
        + regime_uncertainty**2
    )
    path_sigma = max(0.25, float(path_sigma))
    one_hour_rate = _temperature_momentum_rate_for_window(momentum, "1h") if isinstance(momentum, Mapping) else None
    context_confirmed_windows = [
        label
        for label in TEMPERATURE_MOMENTUM_CONTEXT_CONFIRM_WINDOWS
        if _temperature_momentum_rate_for_window(momentum, label) is not None
        and float(_temperature_momentum_rate_for_window(momentum, label) or 0.0)
        >= TEMPERATURE_MOMENTUM_CONTEXT_CONFIRM_MIN_C_PER_HOUR
    ]
    one_hour_context_confirmed = bool(
        one_hour_rate is not None
        and float(one_hour_rate) >= _env_float("PM_BAYES_TRAJECTORY_REGIME_V1_ONE_HOUR_CONFIRM_MIN_C_PER_HOUR", 0.75)
        and context_confirmed_windows
        and negative_rate_count == 0
    )
    strong_warming_confirmed = bool(
        float(rate_hat) >= _env_float("PM_BAYES_TRAJECTORY_REGIME_V1_STRONG_RATE_MIN_C_PER_HOUR", 0.75)
        and positive_context_count >= 2
        and negative_rate_count == 0
    )
    evidence_base = 1.0 / max(0.25, path_sigma) ** 2
    momentum_scale = 1.0
    if strong_warming_confirmed:
        momentum_scale = 2.40
    elif warming_acceleration_confirmed:
        momentum_scale = 2.10
    elif one_hour_context_confirmed:
        momentum_scale = 1.75
    elif context_warming_watch:
        momentum_scale = 1.35
    elif str(momentum_state) in {"sustained_warming", "emerging_warming", "weak_warming"} and float(rate_hat) >= 0.45:
        momentum_scale = 1.25
    confidence_scale = _clamp(0.72 + 0.46 * float(curvature_confidence), 0.60, 1.18)
    forcing_scale = _clamp(
        1.0
        - 0.42 * float(forcing_turn_probability)
        - 0.18 * float(regime_probs.get("forced_turn_risk", 0.0))
        - 0.14 * float(regime_probs.get("low_confidence", 0.0)),
        0.35,
        1.0,
    )
    evidence_scale = _clamp(
        momentum_scale * confidence_scale * forcing_scale,
        _env_float("PM_BAYES_TRAJECTORY_REGIME_V1_EVIDENCE_SCALE_MIN", 0.30),
        _env_float("PM_BAYES_TRAJECTORY_REGIME_V1_EVIDENCE_SCALE_MAX", 3.0),
    )
    precision_cap = max(0.0, _env_float("PM_BAYES_TRAJECTORY_REGIME_V1_PRECISION_CAP", 8.0))
    effective_evidence = min(float(precision_cap), float(evidence_base) * float(evidence_scale))
    return {
        "enabled": True,
        "available": True,
        "source": "observed_temperature_trajectory_regime_v1",
        "na_reason": "",
        "status": "enabled",
        "group": "temperature_trajectory",
        "role": "observed_realization",
        "source_class": "airport_temperature_path",
        "dedupe_policy": "replaces_legacy_linear_rate_projection_when_available; forcing_only_shapes_path_mu_sigma_before_independent_physics_residual",
        "regime_model_status": "heuristic_live_no_bundle",
        "rate_hat": float(rate_hat),
        "accel_hat": float(accel_hat),
        "rate_hat_c_per_hour": float(rate_hat),
        "accel_hat_c_per_hour2": float(accel_hat),
        "curvature_confidence": float(curvature_confidence),
        "soft_intraday_momentum": soft_intraday_momentum,
        "soft_intraday_confidence_boost": float(soft_intraday_confidence_boost),
        "local_quadratic_fit": local_quadratic_fit,
        "path_realization_scale": float(path_realization_scale),
        "regime_probs": regime_probs,
        "peak_hour_distribution": peak_distribution,
        "forcing_onset_probs": {
            "cloud_pressure": float(cloud_pressure),
            "low_cloud_pressure": float(low_cloud_onset["pressure"]),
            "mid_cloud_pressure": float(mid_cloud_onset["pressure"]),
            "high_cloud_pressure": float(high_cloud_onset["pressure"]),
            "precip_pressure": float(precip_pressure),
            "radiation_pressure": float(radiation_pressure),
            "dewpoint_pressure": float(dewpoint_pressure),
            "forced_turn_probability": float(forcing_turn_probability),
            "cloud": cloud_onset,
            "cloud_layers": cloud_layer_onsets,
            "precipitation": precip_onset,
            "radiation": radiation_onset,
            "dewpoint": dewpoint_onset,
            "dominant_contributors": forcing_contributors[:5],
        },
        "forcing_contributors": forcing_contributors,
        "path_aggregation": "deterministic_path_monte_carlo_max",
        "path_draw_count": int(len(path_values)),
        "path_candidates": path_candidates,
        "path_mu_c": float(path_mu),
        "mean_c": float(path_mu),
        "path_upper_candidate_c": float(path_upper_candidate),
        "path_p90_c": float(path_p90),
        "path_sigma_c": float(path_sigma),
        "sigma_c": float(path_sigma),
        "precision": float(effective_evidence),
        "effective_evidence": float(effective_evidence),
        "evidence": float(effective_evidence),
        "evidence_base": float(evidence_base),
        "evidence_scale": float(evidence_scale),
        "precision_cap": float(precision_cap),
        "momentum_evidence_scale": float(momentum_scale),
        "confidence_evidence_scale": float(confidence_scale),
        "forcing_evidence_scale": float(forcing_scale),
        "kinematic_baseline_mu": float(kinematic_baseline_mu),
        "kinematic_baseline_mu_c": float(kinematic_baseline_mu),
        "path_delta_c": max(0.0, float(path_mu) - float(observed_c)),
        "rate_spread_c_per_hour": float(spread),
        "warming_acceleration_confirmed_by_context": bool(warming_acceleration_confirmed),
        "one_hour_context_confirmed": bool(one_hour_context_confirmed),
        "strong_warming_confirmed": bool(strong_warming_confirmed),
        "positive_context_window_count": int(positive_context_count),
        "context_confirmed_windows": ",".join(context_confirmed_windows),
        "forcing_turn_probability": float(forcing_turn_probability),
        "live_apply_default": True,
    }


def precipitation_evidence_profile(features: Mapping[str, Any]) -> Dict[str, Any]:
    """Classify precipitation evidence before it can affect physical likelihood.

    Real radar/QPE and explicit METAR/AWC-style present weather may enter the
    strong precipitation group. Model/API precipitation proxies stay observable
    but do not become evaporative-cooling evidence.
    """
    if not isinstance(features, Mapping):
        return {
            "available_strong": False,
            "available_proxy": False,
            "evidence_state": "missing",
            "source_class": "missing",
            "source_field": "",
            "source": "",
            "rate_mm_per_h": None,
            "precip_present": False,
            "no_precip_confirmed": False,
            "na_reason": "precip_features_missing",
            "is_real_radar_or_metar": False,
            "is_proxy_only": False,
        }
    source = str(
        features.get("radar_qpe_proxy_source")
        or features.get("precipitation_source")
        or features.get("observed_precip_source")
        or features.get("weather_precip_source")
        or ""
    ).strip()
    source_l = source.lower()
    weak_source = any(token in source_l for token in ("open_meteo", "forecast", "model", "proxy"))
    strong_source = any(
        token in source_l
        for token in (
            "metar",
            "awc",
            "aviationweather",
            "iem",
            "mrms",
            "radar",
            "qpe",
            "nws_latest_station_observation",
            "station_observation",
            "present_weather",
        )
    )
    metar_conflict = _feature_bool(features, "metar_present_weather_conflict") or bool(
        (
            _feature_bool(features, "metar_rain_observed")
            or _feature_bool(features, "is_raining_observed")
        )
        and (
            _feature_bool(features, "awc_metar_no_precip_observed")
            or _feature_bool(features, "metar_no_precip_observed")
            or _feature_bool(features, "metar_cavok_observed")
        )
    )
    if metar_conflict:
        return {
            "available_strong": False,
            "available_proxy": False,
            "evidence_state": "conflict",
            "source_class": "metar_conflict",
            "source_field": "metar_present_weather_conflict",
            "source": source or "metar_present_weather_conflict",
            "rate_mm_per_h": None,
            "precip_present": False,
            "no_precip_confirmed": False,
            "na_reason": str(
                features.get("metar_present_weather_conflict_reason")
                or "metar_precip_and_no_precip_flags_conflict"
            ),
            "is_real_radar_or_metar": False,
            "is_proxy_only": False,
        }

    def strong_profile(
        *,
        rate: float,
        source_class: str,
        source_field: str,
        precip_present: bool,
        no_precip_confirmed: bool = False,
    ) -> Dict[str, Any]:
        rate_val = max(0.0, float(rate))
        no_precip = bool(no_precip_confirmed or (rate_val <= EPS and not precip_present))
        return {
            "available_strong": True,
            "available_proxy": False,
            "evidence_state": "strong_no_precip" if no_precip else "strong",
            "source_class": source_class,
            "source_field": source_field,
            "source": source,
            "rate_mm_per_h": rate_val,
            "precip_present": bool(precip_present and rate_val > 0.0),
            "no_precip_confirmed": bool(no_precip),
            "na_reason": "",
            "is_real_radar_or_metar": True,
            "is_proxy_only": False,
        }

    strong_fields = (
        "radar_precip_rate_mm_per_h",
        "qpe_precip_rate_mm_per_h",
        "qpe_precip_mm",
    )
    for field in strong_fields:
        value = _feature(features, field)
        if value is not None and not weak_source:
            source_class = "radar_qpe" if field.startswith(("radar", "qpe")) else "verifiable_observed"
            rate = max(0.0, float(value))
            return strong_profile(
                rate=rate,
                source_class=source_class,
                source_field=field,
                precip_present=rate > EPS,
                no_precip_confirmed=rate <= EPS,
            )

    no_precip_fields = (
        "awc_metar_no_precip_observed",
        "metar_no_precip_observed",
        "metar_cavok_observed",
        "radar_qpe_no_precip_observed",
        "qpe_no_precip_observed",
        "station_no_precip_observed",
    )
    for bool_field in no_precip_fields:
        no_precip = _feature_bool(features, bool_field)
        if no_precip:
            if bool_field.startswith(("awc", "metar")):
                source_class = "metar"
            elif bool_field.startswith(("radar", "qpe")):
                source_class = "radar_qpe"
            else:
                source_class = "verifiable_observed"
            return strong_profile(
                rate=0.0,
                source_class=source_class,
                source_field=bool_field,
                precip_present=False,
                no_precip_confirmed=True,
            )

    observed_rate = _feature(features, "observed_precip_rate_mm_per_h")
    if observed_rate is not None and observed_rate > EPS and strong_source and not weak_source:
        source_class = "metar" if "metar" in source_l or "awc" in source_l else "verifiable_observed"
        return strong_profile(
            rate=float(observed_rate),
            source_class=source_class,
            source_field="observed_precip_rate_mm_per_h",
            precip_present=True,
        )

    for bool_field in ("metar_rain_observed", "radar_precip_observed"):
        observed = _feature_bool(features, bool_field)
        if observed:
            return strong_profile(
                rate=0.1,
                source_class="metar" if bool_field.startswith("metar") else "radar_qpe",
                source_field=bool_field,
                precip_present=True,
            )

    observed_flag = _feature_bool(features, "is_raining_observed")
    if observed_flag and strong_source and not weak_source:
        return strong_profile(
            rate=0.1,
            source_class="verifiable_observed",
            source_field="is_raining_observed",
            precip_present=True,
        )

    if observed_rate is not None and strong_source and not weak_source:
        source_class = "metar" if "metar" in source_l or "awc" in source_l else "verifiable_observed"
        return strong_profile(
            rate=float(observed_rate),
            source_class=source_class,
            source_field="observed_precip_rate_mm_per_h",
            precip_present=False,
            no_precip_confirmed=observed_rate <= EPS,
        )

    proxy_fields = (
        "observed_precip_rate_mm_per_h",
        "precipitation_rate_mm_per_h",
        "rain_rate_mm_per_h",
        "showers_rate_mm_per_h",
    )
    for field in proxy_fields:
        value = _feature(features, field)
        if value is not None:
            return {
                "available_strong": False,
                "available_proxy": True,
                "evidence_state": "proxy",
                "source_class": "weak_proxy",
                "source_field": field,
                "source": source,
                "rate_mm_per_h": max(0.0, float(value)),
                "precip_present": bool(float(value) > EPS),
                "no_precip_confirmed": False,
                "na_reason": "real_radar_qpe_metar_or_verified_precip_missing",
                "is_real_radar_or_metar": False,
                "is_proxy_only": True,
            }
    proxy_raining = _feature_bool(features, "is_raining")
    if proxy_raining:
        return {
            "available_strong": False,
            "available_proxy": True,
            "evidence_state": "proxy",
            "source_class": "weak_proxy",
            "source_field": "is_raining",
            "source": source,
            "rate_mm_per_h": 0.1,
            "precip_present": True,
            "no_precip_confirmed": False,
            "na_reason": "real_radar_qpe_metar_or_verified_precip_missing",
            "is_real_radar_or_metar": False,
            "is_proxy_only": True,
        }

    return {
        "available_strong": False,
        "available_proxy": False,
        "evidence_state": "missing",
        "source_class": "missing",
        "source_field": "",
        "source": source,
        "rate_mm_per_h": None,
        "precip_present": False,
        "no_precip_confirmed": False,
        "na_reason": "real_radar_qpe_metar_or_verified_precip_missing",
        "is_real_radar_or_metar": False,
        "is_proxy_only": False,
    }


def _clear_sky_ratio(features: Mapping[str, Any]) -> Optional[float]:
    cti = _feature(features, "cti_observed", "cti", "clear_sky_ratio", "radiation_clear_sky_ratio")
    if cti is not None:
        return _clamp(cti, 0.0, 1.5)
    radiation = _feature(
        features,
        "solar_radiation_observed",
        "shortwave_radiation_observed",
        "solar_radiation",
        "shortwave_radiation",
        "shortwave_radiation_sum",
    )
    clear_sky = _feature(
        features,
        "clear_sky_radiation_observed",
        "clear_sky_shortwave_observed",
        "clear_sky_radiation",
        "clear_sky_shortwave",
        "clear_sky_shortwave_radiation_sum",
    )
    if radiation is not None and clear_sky is not None and clear_sky > 0:
        return _clamp(float(radiation) / float(clear_sky), 0.0, 1.5)
    observed_flux = _feature(
        features,
        "solar_radiation_observed",
        "shortwave_radiation_observed",
        "solar_radiation",
        "shortwave_radiation",
    )
    if observed_flux is not None:
        flux = float(observed_flux)
        if flux <= 1.5:
            return _clamp(flux, 0.0, 1.5)
        return _clamp(flux / 850.0, 0.0, 1.2)
    return None


def _effective_cloud_cover_pct(features: Mapping[str, Any]) -> Optional[float]:
    low = _feature(features, "cloud_cover_low_pct", "low_cloud_cover_pct", "cloud_low_pct")
    mid = _feature(features, "cloud_cover_mid_pct", "mid_cloud_cover_pct", "cloud_mid_pct")
    high = _feature(features, "cloud_cover_high_pct", "high_cloud_cover_pct", "cloud_high_pct")
    if low is not None or mid is not None or high is not None:
        low_f = _clamp(float(low or 0.0) / 100.0 if float(low or 0.0) > 1.0 else float(low or 0.0), 0.0, 1.0)
        mid_f = _clamp(float(mid or 0.0) / 100.0 if float(mid or 0.0) > 1.0 else float(mid or 0.0), 0.0, 1.0)
        high_f = _clamp(float(high or 0.0) / 100.0 if float(high or 0.0) > 1.0 else float(high or 0.0), 0.0, 1.0)
        clear = (1.0 - min(1.0, low_f * 1.35)) * (1.0 - mid_f * 0.70) * (1.0 - high_f * 0.25)
        return _clamp((1.0 - clear) * 100.0, 0.0, 100.0)
    cloud = _feature(features, "cloud_cover", "cloud_cover_pct", "cloudcover", "metar_effective_cloud_cover_pct")
    if cloud is None:
        return None
    return _clamp(float(cloud) if float(cloud) > 1.0 else float(cloud) * 100.0, 0.0, 100.0)


def _precip_rate_mm_per_h(features: Mapping[str, Any]) -> Optional[float]:
    profile = precipitation_evidence_profile(features)
    if not profile.get("available_strong"):
        return None
    value = _to_float(profile.get("rate_mm_per_h"))
    if value is None:
        return None
    return max(0.0, float(value))


def _observed_today_max_is_strong(features: Mapping[str, Any]) -> bool:
    source = str(
        features.get("observed_today_max_source")
        or features.get("observed_temp_source")
        or ""
    ).strip().lower()
    if ("wu" in source or "twc" in source or "wunderground" in source or "weather_com" in source) and (
        _wu_twc_intraday_strong_observation_demote_reason(features)
    ):
        return False
    weak_source_tokens = ("open_meteo", "historical_forecast", "forecast", "reanalysis", "dry_run")
    if source and any(token in source for token in weak_source_tokens):
        return False
    explicit = _feature_bool(
        features,
        "observed_today_max_trusted",
        "observed_today_max_is_official",
        "observed_today_max_is_station_observed",
    )
    if explicit is not None:
        return bool(explicit)
    final = _feature_bool(
        features,
        "temperature_observation_final_daily_max",
        "observed_today_max_is_final",
        "daily_max_observation_complete",
    )
    if final:
        return True
    strong_source_tokens = ("metar", "nws", "weather_com", "wunderground", "station")
    return bool(source and any(token in source for token in strong_source_tokens))


def _observed_today_max_for_peak_estimator(features: Mapping[str, Any]) -> Tuple[Optional[float], Dict[str, Any]]:
    """Return the intraday max allowed to lock the peak estimator.

    WU/TWC intraday can be a useful warning, but when it has deformation/date
    mismatch alerts it must not become the hard lock for trajectory peak.
    """

    raw_observed_max = _feature(features, "observed_today_max_c", "daily_max_observed_c")
    raw_source = str(
        features.get("observed_today_max_source")
        or features.get("observed_temp_source")
        or ""
    ).strip()
    demote_reason = ""
    raw_source_l = raw_source.lower()
    if any(token in raw_source_l for token in ("wu", "twc", "wunderground", "weather_com")):
        demote_reason = _wu_twc_intraday_strong_observation_demote_reason(features)

    candidates: List[Tuple[float, str]] = []
    if raw_observed_max is not None and not demote_reason:
        candidates.append((float(raw_observed_max), raw_source or "observed_today_max_c"))

    for key in (
        "zuuu_metar_raw_amsc_today_max_c",
        "zuuu_metar_raw_awc_today_max_c",
        "awc_raw_metar_today_max_c",
        "awc_metar_today_max_c",
        "metar_today_max_c",
        "station_observed_today_max_c",
    ):
        value = _feature(features, key)
        if value is not None:
            candidates.append((float(value), key))

    if not candidates:
        return None, {
            "raw_observed_today_max_c": raw_observed_max,
            "raw_observed_today_max_source": raw_source,
            "demoted_reason": demote_reason,
            "used_source": "",
        }

    selected_value, selected_source = max(candidates, key=lambda item: item[0])
    return selected_value, {
        "raw_observed_today_max_c": raw_observed_max,
        "raw_observed_today_max_source": raw_source,
        "demoted_reason": demote_reason,
        "used_source": selected_source,
    }


def _mapping_feature(features: Mapping[str, Any], *keys: str) -> Dict[str, Any]:
    for key in keys:
        value = features.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _high_temperature_platform_recoil_meta(
    features: Mapping[str, Any],
    *,
    observed_c: Optional[float],
    observed_today_max_c: Optional[float],
    hour_local: Optional[float],
    peak_window_end_hour_local: Optional[float],
) -> Dict[str, Any]:
    if observed_c is None or observed_today_max_c is None or hour_local is None:
        return {
            "active": False,
            "reason": "missing_observed_or_hour",
        }
    rolling = _mapping_feature(
        features,
        "zuuu_metar_raw_amsc_rolling_temperature_summary",
        "zuuu_metar_raw_awc_rolling_temperature_summary",
        "rolling_temperature_observation_summary",
    )
    source_text = str(
        features.get("temperature_momentum_source")
        or features.get("observed_today_max_source")
        or features.get("observed_temp_source")
        or ""
    ).strip().lower()
    hard_source = bool(
        "metar" in source_text
        or "amsc" in source_text
        or "awc" in source_text
        or features.get("zuuu_metar_raw_amsc_latest_temp_c") not in (None, "")
        or features.get("zuuu_metar_raw_awc_latest_temp_c") not in (None, "")
    )
    if not hard_source:
        return {
            "active": False,
            "reason": "hard_metar_source_missing",
        }
    latest_drop = _to_float(rolling.get("latest_drop_from_rolling_max_c"))
    if latest_drop is None:
        latest_drop = max(0.0, float(observed_today_max_c) - float(observed_c))
    max_allowed_drop = max(0.0, _env_float("PM_BAYES_PLATFORM_RECOIL_MAX_DROP_C", 1.0))
    near_high = bool(float(latest_drop) <= float(max_allowed_drop))
    if not near_high:
        return {
            "active": False,
            "reason": "latest_temperature_too_far_below_platform_high",
            "latest_drop_from_high_c": float(latest_drop),
            "max_allowed_drop_c": float(max_allowed_drop),
        }
    grace = max(0.0, _env_float("PM_BAYES_PLATFORM_RECOIL_PEAK_WINDOW_GRACE_HOURS", 0.25))
    peak_open = bool(
        peak_window_end_hour_local is None
        or float(hour_local) <= float(peak_window_end_hour_local) + float(grace)
    )
    if not peak_open:
        return {
            "active": False,
            "reason": "peak_window_already_closed",
            "hour_local": float(hour_local),
            "peak_window_end_hour_local": peak_window_end_hour_local,
        }
    raw_weather = " ".join(
        str(features.get(key) or "")
        for key in (
            "weather_text",
            "cloud_text",
            "zuuu_metar_raw_amsc_latest_weather_text",
            "zuuu_metar_raw_amsc_latest_cloud_text",
            "zuuu_metar_raw_awc_latest_weather_text",
            "zuuu_metar_raw_awc_latest_cloud_text",
            "raw_metar",
            "zuuu_metar_raw_amsc_latest_raw",
            "zuuu_metar_raw_awc_latest_raw",
        )
    ).upper()
    cutoff_tokens = ("TSRA", "SHRA", "+RA", " CB", "VCB")
    if any(token in raw_weather for token in cutoff_tokens):
        return {
            "active": False,
            "reason": "convective_or_rain_cutoff_observed",
            "latest_drop_from_high_c": float(latest_drop),
        }
    rolling_state = str(
        rolling.get("state")
        or features.get("temperature_trajectory_realization_state")
        or features.get("temperature_momentum_realization_state")
        or ""
    ).strip().lower()
    platform_like = rolling_state in {"platform", "flat", "mixed", "weak_warming", "sustained_warming", ""}
    if not platform_like:
        return {
            "active": False,
            "reason": "trajectory_state_not_platform_like",
            "trajectory_state": rolling_state,
            "latest_drop_from_high_c": float(latest_drop),
        }
    residual_c = max(0.0, _env_float("PM_BAYES_PLATFORM_RECOIL_RESIDUAL_C", 0.55))
    return {
        "active": True,
        "reason": "hard_metar_near_high_platform_before_peak_window_end",
        "residual_c": float(residual_c),
        "latest_drop_from_high_c": float(latest_drop),
        "max_allowed_drop_c": float(max_allowed_drop),
        "hour_local": float(hour_local),
        "peak_window_end_hour_local": peak_window_end_hour_local,
        "trajectory_state": rolling_state or "unknown",
        "source": "hard_metar_platform_recoil",
    }


def _wet_bulb_c(temp_c: Optional[float], dewpoint_c: Optional[float]) -> Optional[float]:
    if temp_c is None or dewpoint_c is None:
        return None
    # Stull-style approximation via RH reconstructed from T/Td. Good enough for a
    # likelihood constraint; not used as settlement fact.
    t = float(temp_c)
    td = float(dewpoint_c)
    es = math.exp((17.625 * t) / (243.04 + t))
    e = math.exp((17.625 * td) / (243.04 + td))
    rh = _clamp(100.0 * e / max(1e-9, es), 1.0, 100.0)
    return (
        t * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(t + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
        - 4.686035
    )


def _group_templates(features: Mapping[str, Any], config: DynamicLikelihoodConfig) -> Dict[str, Dict[str, Any]]:
    observed_c = _observed_temp_c(features)
    forecast_c = _forecast_peak_c(features)
    hour = _observation_hour_local_from_features(features)
    cti = _clear_sky_ratio(features)
    cloud = _effective_cloud_cover_pct(features)
    precip_profile = precipitation_evidence_profile(features)
    precip_rate = _precip_rate_mm_per_h(features)
    wind_speed = _feature(features, "wind_speed_mps", "wind_speed_10m_mps")
    pressure_trend = _feature(features, "pressure_tendency_hpa_3h", "pressure_trend_hpa_3h", "pressure_change_hpa_3h")
    dew_c = _feature(features, "dewpoint_c", "dew_point_c", "dewpoint_2m_c", "dew_point_2m_c", "wet_bulb_c")
    values = _temperature_values_from_features(features)
    templates: Dict[str, Dict[str, Any]] = {
        "temperature_trajectory": {
            "available": observed_c is not None and hour is not None and forecast_c is not None,
            "na_reason": "" if observed_c is not None and hour is not None and forecast_c is not None else "observed_temp_hour_or_forecast_missing",
            "weight": float(config.observed_temp_weight + config.peak_estimator_weight),
        },
        "radiation": {
            "available": cti is not None,
            "na_reason": "" if cti is not None else "observed_or_clear_sky_radiation_missing",
            "weight": float(config.radiation_weight),
        },
        "cloud_shading": {
            "available": cloud is not None,
            "na_reason": "" if cloud is not None else "low_mid_total_cloud_missing",
            "weight": float(config.cloud_weight),
        },
        "precipitation_evaporative_cooling": {
            "available": precip_rate is not None,
            "na_reason": "" if precip_rate is not None else str(precip_profile.get("na_reason") or "radar_qpe_or_observed_precip_missing"),
            "weight": float(config.precipitation_weight),
            "source_profile": dict(precip_profile),
            "evidence_state": str(precip_profile.get("evidence_state") or "missing"),
        },
        "airmass_mixing": {
            "available": wind_speed is not None or pressure_trend is not None,
            "na_reason": "" if wind_speed is not None or pressure_trend is not None else "wind_airmass_or_pressure_trend_missing",
            "weight": float(config.wind_weight + config.pressure_weight),
        },
        "wet_bulb_constraint": {
            "available": dew_c is not None,
            "na_reason": "" if dew_c is not None else "dewpoint_or_wet_bulb_missing",
            "weight": float(config.dewpoint_weight + config.wet_bulb_weight),
        },
        "residual_correction": {
            "available": bool(values) or forecast_c is not None,
            "na_reason": "" if bool(values) or forecast_c is not None else "model_distribution_or_forecast_missing",
            "weight": float(config.ge_tail_support_weight),
        },
    }
    for key, entry in templates.items():
        entry["log_likelihood"] = 0.0
        entry["components"] = {}
        entry[f"{key}_na_reason"] = entry["na_reason"]
    return templates


def _record_group_component(
    components: Dict[str, float],
    component_groups: Dict[str, Dict[str, Any]],
    *,
    group: str,
    name: str,
    factor: float,
    weight: float,
) -> None:
    log_val = _log_factor(factor, weight)
    components[name] = log_val
    entry = component_groups.setdefault(group, {"available": True, "na_reason": "", "weight": float(weight), "log_likelihood": 0.0, "components": {}})
    entry["available"] = True
    entry["na_reason"] = ""
    entry[f"{group}_na_reason"] = ""
    entry["log_likelihood"] = float(entry.get("log_likelihood") or 0.0) + float(log_val)
    comps = entry.setdefault("components", {})
    if isinstance(comps, dict):
        comps[name] = {
            "factor": float(_clamp(float(factor), EPS, 10.0)),
            "weight": float(max(0.0, weight)),
            "log_likelihood": float(log_val),
        }


def _summarize_component_groups(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    totals = {group: 0.0 for group in EVIDENCE_GROUPS}
    availability: Dict[str, Dict[str, Any]] = {}
    row_count = max(1, len(rows))
    for row in rows:
        groups = row.get("component_groups") if isinstance(row, Mapping) else None
        if not isinstance(groups, Mapping):
            continue
        for group in EVIDENCE_GROUPS:
            entry = groups.get(group)
            if not isinstance(entry, Mapping):
                continue
            totals[group] += abs(float(entry.get("log_likelihood") or 0.0))
            if group not in availability:
                availability[group] = {
                    "available": bool(entry.get("available")),
                    "na_reason": str(entry.get("na_reason") or ""),
                    f"{group}_na_reason": str(entry.get("na_reason") or ""),
                    "weight": float(entry.get("weight") or 0.0),
                }
                if isinstance(entry.get("source_profile"), Mapping):
                    availability[group]["source_profile"] = dict(entry.get("source_profile") or {})
                if entry.get("evidence_state") is not None:
                    availability[group]["evidence_state"] = str(entry.get("evidence_state") or "")
    avg = {group: totals[group] / float(row_count) for group in EVIDENCE_GROUPS}
    denom = sum(avg.values())
    shares = {group: (avg[group] / denom if denom > EPS else 0.0) for group in EVIDENCE_GROUPS}
    core = (
        "temperature_trajectory",
        "radiation",
        "cloud_shading",
        "precipitation_evaporative_cooling",
    )
    return {
        "average_abs_log_likelihood": avg,
        "contribution_share": shares,
        "core_evidence_share": sum(shares[group] for group in core),
        "availability": {group: availability.get(group, {}) for group in EVIDENCE_GROUPS},
    }


def peak_kalman_gain(hour_local: Optional[float]) -> float:
    hour = _to_float(hour_local)
    if hour is None:
        return 0.0
    gain = 0.90 / (1.0 + math.exp(-0.55 * (float(hour) - 10.5)))
    if hour < 6.0:
        gain = min(gain, 0.20)
    return _clamp(gain, 0.0, 0.90)


def _default_diurnal_rise_c(month: Optional[float], hour_local: float) -> Tuple[float, float]:
    h = _clamp(float(hour_local), 0.0, 24.0)
    curve = [
        (5.0, 7.2, 3.0),
        (6.0, 6.4, 2.8),
        (7.0, 5.4, 2.5),
        (8.0, 4.5, 2.2),
        (9.0, 3.6, 1.9),
        (10.0, 2.8, 1.6),
        (11.0, 2.0, 1.3),
        (12.0, 1.2, 1.0),
        (13.0, 0.6, 0.7),
        (14.0, 0.2, 0.5),
    ]
    if h <= curve[0][0]:
        mu, sigma = curve[0][1], curve[0][2]
    elif h >= curve[-1][0]:
        mu, sigma = curve[-1][1], curve[-1][2]
    else:
        mu, sigma = curve[-1][1], curve[-1][2]
        for (h0, mu0, sig0), (h1, mu1, sig1) in zip(curve, curve[1:]):
            if h0 <= h <= h1:
                t = (h - h0) / max(1e-9, h1 - h0)
                mu = mu0 + (mu1 - mu0) * t
                sigma = sig0 + (sig1 - sig0) * t
                break
    m = _to_float(month)
    if m is not None and int(m) in {6, 7, 8}:
        mu *= 1.08
    elif m is not None and int(m) in {12, 1, 2}:
        mu *= 0.85
    return max(0.0, float(mu)), max(0.3, float(sigma))


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    sig = max(1e-6, float(sigma))
    return 0.5 * (1.0 + math.erf((float(x) - float(mu)) / (sig * math.sqrt(2.0))))


def _normal_bucket_mass(
    bucket: OutcomeBucket,
    *,
    mu_c: float,
    sigma_c: float,
    eq_width_c: float = 1.0,
) -> float:
    low_c, high_c, rep_c = _bucket_bounds_c(bucket)
    width = max(0.1, float(eq_width_c))
    if bucket.kind == "le" and high_c is not None:
        return _clamp(_normal_cdf(high_c + width / 2.0, mu_c, sigma_c), EPS, 1.0)
    if bucket.kind == "ge" and low_c is not None:
        return _clamp(1.0 - _normal_cdf(low_c - width / 2.0, mu_c, sigma_c), EPS, 1.0)
    if bucket.kind == "range" and low_c is not None and high_c is not None:
        lo = low_c - width / 2.0
        hi = high_c + width / 2.0
        return _clamp(_normal_cdf(hi, mu_c, sigma_c) - _normal_cdf(lo, mu_c, sigma_c), EPS, 1.0)
    lo = rep_c - width / 2.0
    hi = rep_c + width / 2.0
    return _clamp(_normal_cdf(hi, mu_c, sigma_c) - _normal_cdf(lo, mu_c, sigma_c), EPS, 1.0)


def estimate_peak_temperature_from_features(features: Mapping[str, Any]) -> Dict[str, Any]:
    """Realtime highest-temp extrapolator using a compact energy-balance nowcast."""

    if not features:
        return {"enabled": False, "source": "missing_features"}
    observed_c = _observed_temp_c(features)
    forecast_c = _forecast_peak_c(features)
    values = _temperature_values_from_features(features)
    forecast_sigma = _feature(features, "forecast_sigma_c", "model_sigma_c", "temperature_sigma_c")
    if forecast_sigma is None and len(values) >= 2:
        mean = sum(values) / len(values)
        forecast_sigma = math.sqrt(sum((v - mean) ** 2 for v in values) / max(1, len(values) - 1))
    if forecast_sigma is None:
        forecast_sigma = _env_float("PM_BAYES_PEAK_ESTIMATOR_FORECAST_SIGMA_C", 1.5)
    hour = _observation_hour_local_from_features(features)
    if hour is None or observed_c is None or forecast_c is None:
        return {
            "enabled": False,
            "source": "insufficient_peak_estimator_features",
            "has_observed_temp": observed_c is not None,
            "has_forecast_peak": forecast_c is not None,
            "has_hour_local": hour is not None,
        }
    month = _feature(features, "observation_month", "target_month", "month")
    observed_today_max, observed_today_max_meta = _observed_today_max_for_peak_estimator(features)
    lock_hour = _env_float("PM_BAYES_PEAK_ESTIMATOR_LOCK_HOUR", 14.0)
    min_hour = _env_float("PM_BAYES_PEAK_ESTIMATOR_MIN_USEFUL_HOUR", 5.0)
    peak_hour = _forecast_peak_hour_local_from_features(features)
    explicit_peak_hour = peak_hour is not None
    if peak_hour is None:
        peak_hour = _env_float("PM_BAYES_LIKELIHOOD_REFRESH_PEAK_HOUR", 16.0)
    peak_window = _peak_window_from_features(
        features,
        peak_hour=float(peak_hour) if peak_hour is not None else None,
        explicit_peak_hour=explicit_peak_hour,
    )
    peak_window_end = _to_float(peak_window.get("end_hour_local"))
    peak_window_start = _to_float(peak_window.get("start_hour_local"))
    platform_recoil = _high_temperature_platform_recoil_meta(
        features,
        observed_c=observed_c,
        observed_today_max_c=observed_today_max,
        hour_local=hour,
        peak_window_end_hour_local=peak_window_end,
    )
    hours_to_peak_center = max(0.0, float(peak_hour) - float(hour))
    hours_to_peak = max(0.0, float((peak_window_end if peak_window_end is not None else peak_hour)) - float(hour))
    rise_rate = _feature(
        features,
        "temperature_rise_rate_c_per_hour",
        "observed_temp_rise_rate_c_per_hour",
        "metar_temp_rise_rate_c_per_hour",
    )
    rise_rate_observation_only = _temperature_rise_rate_observation_only(features)
    rise_rate_window = _temperature_rise_rate_window(features)
    trajectory_rise_rate_candidate = None if rise_rate_observation_only else rise_rate
    momentum = _temperature_momentum_profile(features, fallback_rise_rate=trajectory_rise_rate_candidate)
    momentum_rate = _to_float(momentum.get("effective_rate_c_per_hour")) if isinstance(momentum, Mapping) else None
    momentum_context_rate = _to_float(momentum.get("context_effective_rate_c_per_hour")) if isinstance(momentum, Mapping) else None
    momentum_state = str(momentum.get("state") or "").strip() if isinstance(momentum, Mapping) else ""
    context_warming_watch = bool(isinstance(momentum, Mapping) and momentum.get("context_warming_watch"))
    confirmed_peak_rate_windows = _confirmed_peak_rate_windows(momentum) if isinstance(momentum, Mapping) else []
    peak_rate_confirmed = bool(confirmed_peak_rate_windows)
    trajectory_rise_rate = trajectory_rise_rate_candidate
    if (
        trajectory_rise_rate is not None
        and rise_rate_window in TEMPERATURE_MOMENTUM_PEAK_RATE_CONFIRM_WINDOWS
        and not peak_rate_confirmed
    ):
        trajectory_rise_rate = None
    use_confirmed_momentum_for_projection = bool(
        momentum_rate is not None
        and peak_rate_confirmed
        and momentum_state in {"sustained_warming", "emerging_warming", "weak_warming"}
    )
    breakout_rate = momentum_rate if use_confirmed_momentum_for_projection else trajectory_rise_rate
    if (
        context_warming_watch
        and momentum_context_rate is not None
        and (breakout_rate is None or float(momentum_context_rate) > float(breakout_rate))
    ):
        breakout_rate = float(momentum_context_rate)
    lock_threshold_hour = max(float(lock_hour), float(peak_window_end)) if peak_window_end is not None else float(lock_hour)
    post_prediction_window_warming_watch = bool(
        _env_bool("PM_BAYES_PEAK_ESTIMATOR_BREAKOUT_AFTER_LOCK_ENABLED", True)
        and hours_to_peak > _env_float("PM_BAYES_PEAK_ESTIMATOR_BREAKOUT_MIN_HOURS_TO_PEAK", 0.25)
        and breakout_rate is not None
        and float(breakout_rate) >= _env_float("PM_BAYES_PEAK_ESTIMATOR_BREAKOUT_MIN_RATE_C_PER_H", 0.50)
        and momentum_state in {"sustained_warming", "emerging_warming", "weak_warming", "context_warming_watch"}
    )
    explicit_final_daily_max = _explicit_final_daily_max_evidence(features)
    if explicit_final_daily_max:
        locked = max(float(observed_c), float(observed_today_max) if observed_today_max is not None else float(observed_c))
        selected_source_text = str(observed_today_max_meta.get("used_source") or "").lower()
        trusted_observed_lock = bool(
            _observed_today_max_is_strong(features)
            or any(token in selected_source_text for token in ("metar", "awc", "station", "nws"))
        )
        source = "locked_trusted_observed" if trusted_observed_lock else "locked_observed"
        return {
            "enabled": True,
            "source": source,
            "mu_c": float(locked),
            "sigma_c": _env_float("PM_BAYES_PEAK_ESTIMATOR_LOCKED_SIGMA_C", 0.3),
            "extrapolation_c": float(locked),
            "kalman_gain": 1.0,
            "observed_temp_c": float(observed_c),
            "observed_today_max_c": observed_today_max,
            "observed_today_max_source": observed_today_max_meta.get("used_source") or "",
            "raw_observed_today_max_c": observed_today_max_meta.get("raw_observed_today_max_c"),
            "raw_observed_today_max_source": observed_today_max_meta.get("raw_observed_today_max_source") or "",
            "raw_observed_today_max_demoted_reason": observed_today_max_meta.get("demoted_reason") or "",
            "forecast_peak_temp_c": float(forecast_c),
            "hour_local": float(hour),
            "peak_hour_local": float(peak_hour),
            "peak_window_start_hour_local": peak_window_start,
            "peak_window_end_hour_local": peak_window_end,
            "hours_to_peak_window": float(hours_to_peak),
            "hours_to_peak_center": float(hours_to_peak_center),
            "lock_threshold_hour_local": float(lock_threshold_hour),
            "observed_peak_finalization_state": "final",
            "prediction_window_lock_forbidden": True,
        }
    if float(hour) < float(min_hour):
        return {
            "enabled": True,
            "source": "forecast_only",
            "mu_c": float(forecast_c),
            "sigma_c": float(forecast_sigma),
            "extrapolation_c": float(forecast_c),
            "kalman_gain": 0.0,
            "observed_temp_c": float(observed_c),
            "forecast_peak_temp_c": float(forecast_c),
            "hour_local": float(hour),
        }
    delta_mu, delta_sigma = _default_diurnal_rise_c(month, float(hour))
    high_temp_guard = _high_temp_amplification_guard_meta(
        features,
        observed_c=observed_c,
        forecast_c=forecast_c,
        rise_rate=rise_rate,
    )
    cloud = _effective_cloud_cover_pct(features)
    wind = _feature(features, "wind_speed_mps", "wind_speed_10m_mps")
    if wind is None:
        mph = _feature(features, "wind_speed_mph")
        if mph is not None:
            wind = mph * 0.44704
    cd = cloud_diurnal_discount(cloud)
    wd = wind_diurnal_discount(wind)
    cti = _clear_sky_ratio(features)
    if cti is None:
        radiation_factor = 1.0
    else:
        radiation_factor = _clamp(0.35 + 0.75 * float(cti), 0.20, 1.18)
    precip_rate = _precip_rate_mm_per_h(features)
    if precip_rate is None:
        precip_cooling_c = 0.0
    elif float(precip_rate) <= EPS:
        precip_cooling_c = 0.0
    else:
        precip_cooling_c = 0.35 + 2.65 * _clamp(math.log1p(float(precip_rate)) / math.log(9.0), 0.0, 1.25)
    dew_c = _feature(features, "dewpoint_c", "dew_point_c", "dewpoint_2m_c", "dew_point_2m_c")
    wet_bulb_c = _feature(features, "wet_bulb_c")
    if wet_bulb_c is None:
        wet_bulb_c = _wet_bulb_c(observed_c, dew_c)
    if wet_bulb_c is None:
        wet_bulb_penalty_c = 0.0
    else:
        depression = max(0.0, float(observed_c) - float(wet_bulb_c))
        wet_bulb_penalty_c = 0.65 * _sigmoid((2.8 - depression) / 1.3)
    trajectory_delta_c = None
    momentum_rate = _to_float(momentum.get("effective_rate_c_per_hour")) if isinstance(momentum, Mapping) else None
    momentum_context_rate = _to_float(momentum.get("context_effective_rate_c_per_hour")) if isinstance(momentum, Mapping) else None
    context_warming_watch = bool(isinstance(momentum, Mapping) and momentum.get("context_warming_watch"))
    explicit_momentum_gate = _feature_bool(features, "temperature_momentum_posterior_gate_allowed")
    use_momentum_rate = bool(
        momentum_rate is not None
        and peak_rate_confirmed
        and (
            explicit_momentum_gate is True
            or str(momentum.get("state") or "") in {"sustained_warming", "emerging_warming", "weak_warming"}
        )
    )
    use_context_rate = bool(
        context_warming_watch
        and momentum_context_rate is not None
        and math.isfinite(float(momentum_context_rate))
        and (momentum_rate is None or float(momentum_context_rate) > float(momentum_rate))
    )
    trajectory_rate = (
        float(momentum_context_rate)
        if use_context_rate
        else (momentum_rate if use_momentum_rate else trajectory_rise_rate)
    )
    short_window_suppressed = bool((rise_rate_observation_only and rise_rate is not None) or (momentum_rate is not None and not peak_rate_confirmed))
    trajectory_suppressed_reason = ""
    if short_window_suppressed and not use_context_rate and not use_momentum_rate and trajectory_rise_rate is None:
        trajectory_suppressed_reason = "short_window_requires_30m_45m_1h_confirmation"
    trajectory_regime_v1 = _observed_temperature_trajectory_regime_v1(
        features,
        observed_c=float(observed_c),
        hour_local=float(hour),
        peak_hour=float(peak_hour) if peak_hour is not None else None,
        peak_window_start=peak_window_start,
        peak_window_end=peak_window_end,
        hours_to_peak=float(hours_to_peak),
        momentum=momentum if isinstance(momentum, Mapping) else {},
        base_rate_c_per_hour=trajectory_rate,
        default_delta_mu_c=float(delta_mu),
        default_delta_sigma_c=float(delta_sigma),
        radiation_factor=float(radiation_factor),
        cloud_discount=float(cd),
        precip_cooling_c=float(precip_cooling_c),
        wet_bulb_penalty_c=float(wet_bulb_penalty_c),
    )
    trajectory_source_component_enabled = _env_bool("PM_BAYES_TRAJECTORY_REGIME_V1_SOURCE_COMPONENT_ENABLED", True)
    legacy_peak_trajectory_enabled = _env_bool(
        "PM_BAYES_LEGACY_TRAJECTORY_EMBED_IN_PEAK_ESTIMATOR",
        not trajectory_source_component_enabled,
    )
    trajectory_regime_live_enabled = _env_bool("PM_BAYES_TRAJECTORY_REGIME_V1_EMBED_IN_PEAK_ESTIMATOR", False)
    trajectory_regime_applied = bool(
        trajectory_regime_live_enabled
        and legacy_peak_trajectory_enabled
        and trajectory_regime_v1.get("available")
        and _to_float(trajectory_regime_v1.get("path_delta_c")) is not None
        and hours_to_peak > 0
    )
    if trajectory_regime_applied:
        trajectory_delta_c = max(0.0, float(_to_float(trajectory_regime_v1.get("path_delta_c")) or 0.0))
        trajectory_rate = _to_float(trajectory_regime_v1.get("rate_hat_c_per_hour"))
    elif legacy_peak_trajectory_enabled and trajectory_rate is not None and math.isfinite(float(trajectory_rate)) and hours_to_peak > 0:
        trajectory_delta_c = max(0.0, float(trajectory_rate) * min(hours_to_peak, 3.5))
        if use_momentum_rate or use_context_rate:
            trajectory_delta_c *= float(_to_float(momentum.get("realization_scale")) or 0.0)
    combined_discount = cd * wd * radiation_factor
    adjusted_delta_mu = delta_mu * combined_discount
    if trajectory_delta_c is not None:
        trajectory_blend = _clamp(peak_kalman_gain(float(hour)) + 0.15, 0.20, 0.95)
        adjusted_delta_mu = trajectory_blend * trajectory_delta_c + (1.0 - trajectory_blend) * adjusted_delta_mu
    adjusted_delta_mu = max(0.0, adjusted_delta_mu - precip_cooling_c - wet_bulb_penalty_c)
    cooling_uncertainty = 0.20 * precip_cooling_c + 0.15 * wet_bulb_penalty_c
    adjusted_delta_sigma = math.sqrt(
        delta_sigma**2
        + (delta_mu * max(0.0, 1.0 - min(1.0, combined_discount)) * 0.2) ** 2
        + cooling_uncertainty**2
    )
    extrapolation_mu = float(observed_c) + adjusted_delta_mu
    if observed_today_max is not None:
        extrapolation_mu = max(extrapolation_mu, float(observed_today_max))
    if platform_recoil.get("active") and observed_today_max is not None:
        residual_floor = float(observed_today_max) + float(platform_recoil.get("residual_c") or 0.0)
        if extrapolation_mu < residual_floor:
            extrapolation_mu = residual_floor
            adjusted_delta_mu = max(0.0, float(extrapolation_mu) - float(observed_c))
    if high_temp_guard.get("active"):
        residual_cap_c = max(0.0, _env_float("PM_BAYES_HIGH_TEMP_GUARD_MAX_RESIDUAL_C", 0.8))
        guard_base_c = max(float(observed_c), float(observed_today_max) if observed_today_max is not None else float(observed_c))
        guard_cap_c = guard_base_c + residual_cap_c
        if extrapolation_mu > guard_cap_c:
            extrapolation_mu = guard_cap_c
            adjusted_delta_mu = max(0.0, float(extrapolation_mu) - float(observed_c))
    gain = peak_kalman_gain(float(hour))
    if hours_to_peak <= 2.0:
        gain = max(gain, 0.72)
    if hours_to_peak <= 0.5:
        gain = max(gain, 0.86)
    if high_temp_guard.get("active"):
        gain = max(gain, _clamp(_env_float("PM_BAYES_HIGH_TEMP_GUARD_MIN_OBS_GAIN", 0.86), 0.0, 1.0))
    rho = _clamp(_env_float("PM_BAYES_PEAK_ESTIMATOR_CORRELATION", 0.3), 0.0, 0.95)
    blended_mu = gain * extrapolation_mu + (1.0 - gain) * float(forecast_c)
    # A selected observed-to-date maximum is a physical lower bound on the
    # final maximum even when another intraday source was demoted.
    if observed_today_max is not None:
        blended_mu = max(float(blended_mu), float(observed_today_max))
    blended_var = (
        gain**2 * adjusted_delta_sigma**2
        + (1.0 - gain) ** 2 * float(forecast_sigma) ** 2
        + 2.0 * gain * (1.0 - gain) * rho * adjusted_delta_sigma * float(forecast_sigma)
    )
    return {
        "enabled": True,
        "source": "energy_balance_nowcast",
        "legacy_source": "kalman_blend",
        "mu_c": float(blended_mu),
        "sigma_c": max(0.25, math.sqrt(max(0.0, blended_var))),
        "extrapolation_c": float(extrapolation_mu),
        "extrapolation_sigma_c": float(adjusted_delta_sigma),
        "kalman_gain": float(gain),
        "observed_temp_c": float(observed_c),
        "observed_today_max_c": observed_today_max,
        "observed_today_max_source": observed_today_max_meta.get("used_source") or "",
        "raw_observed_today_max_c": observed_today_max_meta.get("raw_observed_today_max_c"),
        "raw_observed_today_max_source": observed_today_max_meta.get("raw_observed_today_max_source") or "",
        "raw_observed_today_max_demoted_reason": observed_today_max_meta.get("demoted_reason") or "",
        "forecast_peak_temp_c": float(forecast_c),
        "hour_local": float(hour),
        "peak_hour_local": float(peak_hour),
        "peak_window_start_hour_local": peak_window_start,
        "peak_window_end_hour_local": peak_window_end,
        "hours_to_peak_window": float(hours_to_peak),
        "hours_to_peak_center": float(hours_to_peak_center),
        "lock_threshold_hour_local": float(lock_threshold_hour),
        "observed_peak_finalization_state": "provisional",
        "prediction_window_lock_forbidden": True,
        "forecast_peak_window_used_as_weak_context_only": True,
        "delta_mu_c": float(delta_mu),
        "delta_sigma_c": float(delta_sigma),
        "adjusted_delta_mu_c": float(adjusted_delta_mu),
        "cloud_discount": float(cd),
        "wind_discount": float(wd),
        "radiation_factor": float(radiation_factor),
        "precip_evaporative_cooling_c": float(precip_cooling_c),
        "wet_bulb_penalty_c": float(wet_bulb_penalty_c),
        "combined_discount": float(combined_discount),
        "post_prediction_window_warming_watch": bool(post_prediction_window_warming_watch),
        "post_lock_breakout_projection": False,
        "post_lock_breakout_rate_c_per_hour": float(breakout_rate) if breakout_rate is not None else None,
        "platform_recoil_watch": platform_recoil,
        "platform_recoil_active": bool(platform_recoil.get("active")),
        "high_temp_amplification_guard_active": bool(high_temp_guard.get("active")),
        "high_temp_amplification_guard": high_temp_guard,
        "high_temp_guard_residual_cap_c": _env_float("PM_BAYES_HIGH_TEMP_GUARD_MAX_RESIDUAL_C", 0.8),
        "temperature_momentum": momentum,
        "temperature_momentum_realization_state": momentum.get("state") if isinstance(momentum, Mapping) else "missing",
        "temperature_momentum_effective_c_per_hour": momentum_rate,
        "temperature_momentum_context_effective_c_per_hour": momentum_context_rate,
        "temperature_momentum_context_warming_watch": bool(context_warming_watch),
        "observed_temperature_trajectory_regime_v1": trajectory_regime_v1,
        "observed_temperature_trajectory_regime_v1_applied_to_mu": bool(trajectory_regime_applied),
        "energy_balance_terms": {
            "temperature_trajectory_delta_c": trajectory_delta_c,
            "temperature_trajectory_rate_c_per_hour": trajectory_rate,
            "observed_temperature_trajectory_regime_v1": trajectory_regime_v1,
            "observed_temperature_trajectory_regime_v1_applied_to_mu": bool(trajectory_regime_applied),
            "observed_temperature_trajectory_regime_v1_live_enabled": bool(trajectory_regime_live_enabled),
            "observed_temperature_trajectory_regime_v1_source_component_enabled": bool(trajectory_source_component_enabled),
            "legacy_peak_trajectory_enabled": bool(legacy_peak_trajectory_enabled),
            "temperature_trajectory_uses_context_momentum": bool(use_context_rate),
            "temperature_trajectory_uses_confirmed_momentum": bool(use_momentum_rate),
            "temperature_trajectory_confirmed_momentum_windows": ",".join(confirmed_peak_rate_windows),
            "temperature_trajectory_confirmed_momentum_min_rate_c_per_hour": float(TEMPERATURE_MOMENTUM_PEAK_RATE_CONFIRM_MIN_C_PER_HOUR),
            "temperature_trajectory_momentum_gate_allowed": explicit_momentum_gate,
            "temperature_trajectory_short_window_suppressed": bool(short_window_suppressed),
            "temperature_trajectory_suppressed_reason": trajectory_suppressed_reason,
            "temperature_rise_rate_window": _temperature_rise_rate_window(features),
            "temperature_momentum_realization_scale": momentum.get("realization_scale") if isinstance(momentum, Mapping) else None,
            "default_diurnal_delta_c": float(delta_mu),
            "radiation_factor": float(radiation_factor),
            "cloud_discount": float(cd),
            "wind_discount": float(wd),
            "precip_evaporative_cooling_c": float(precip_cooling_c),
            "wet_bulb_penalty_c": float(wet_bulb_penalty_c),
            "hours_to_peak_window": float(hours_to_peak),
            "hours_to_peak_center": float(hours_to_peak_center),
            "post_prediction_window_warming_watch": bool(post_prediction_window_warming_watch),
            "post_lock_breakout_projection": False,
            "platform_recoil_active": bool(platform_recoil.get("active")),
            "platform_recoil_watch": platform_recoil,
            "high_temp_amplification_guard_active": bool(high_temp_guard.get("active")),
        },
    }


def compute_bucket_log_likelihood(
    bucket: OutcomeBucket,
    features: Mapping[str, Any],
    config: DynamicLikelihoodConfig,
) -> Dict[str, Any]:
    low_c, high_c, rep_c = _bucket_bounds_c(bucket)
    ref_c = _feature(
        features,
        "forecast_peak_temp_c",
        "model_peak_temp_c",
        "expected_temp_c",
        "temperature_2m_max_c",
        "settlement_source_forecast_peak_c",
        "wu_forecast_peak_c",
    )
    if ref_c is None:
        ref_raw = _feature(features, "forecast_peak_temp", "model_peak_temp", "expected_temp", "temperature_2m_max")
        if ref_raw is not None:
            ref_c = _convert_temp(ref_raw, _feature_unit(features), "C")
    if ref_c is None:
        ref_c = rep_c

    components: Dict[str, float] = {}
    component_groups = _group_templates(features, config)
    hard_zero = False
    hard_reasons: List[str] = []

    dew_c = _feature(features, "dewpoint_c", "dew_point_c", "dewpoint_2m_c", "dew_point_2m_c")
    if dew_c is None:
        dew_raw = _feature(features, "dewpoint", "dew_point", "dewpoint_2m", "dew_point_2m")
        if dew_raw is not None:
            dew_c = _convert_temp(dew_raw, _feature_unit(features), "C")
    if dew_c is not None:
        impossible = False
        if bucket.kind in {"eq", "range", "le"} and high_c is not None and high_c < dew_c - 1e-9:
            impossible = True
        if bucket.kind == "eq" and rep_c < dew_c - 1e-9:
            impossible = True
        if impossible:
            hard_zero = True
            hard_reasons.append("dewpoint_above_bucket")
            _record_group_component(
                components,
                component_groups,
                group="wet_bulb_constraint",
                name="dewpoint",
                factor=EPS,
                weight=config.dewpoint_weight,
            )
        else:
            margin = max(0.0, rep_c - dew_c)
            factor = 0.35 + 0.65 * _sigmoid((margin - 1.0) / 1.2)
            _record_group_component(
                components,
                component_groups,
                group="wet_bulb_constraint",
                name="dewpoint",
                factor=factor,
                weight=config.dewpoint_weight,
            )

    obs_c = _feature(features, "observed_temp_c", "current_temp_c", "temperature_now_c")
    if obs_c is None:
        obs_raw = _feature(features, "observed_temp", "current_temp", "temperature_now")
        if obs_raw is not None:
            obs_c = _convert_temp(obs_raw, _feature_unit(features), "C")
    if obs_c is not None:
        if high_c is not None and high_c < obs_c - 0.25:
            _record_group_component(
                components,
                component_groups,
                group="temperature_trajectory",
                name="observed_temp",
                factor=0.03,
                weight=config.observed_temp_weight,
            )
        else:
            factor = 0.55 + 0.45 * _sigmoid((rep_c - obs_c + 0.5) / 1.5)
            _record_group_component(
                components,
                component_groups,
                group="temperature_trajectory",
                name="observed_temp",
                factor=factor,
                weight=config.observed_temp_weight,
            )

    observed_today_max_c, observed_today_max_meta = _observed_today_max_for_peak_estimator(features)
    observed_today_max_source = str(observed_today_max_meta.get("used_source") or "")
    observed_today_max_strong = bool(
        observed_today_max_c is not None
        and (
            any(token in observed_today_max_source.lower() for token in ("metar", "station", "awc", "nws"))
            or _observed_today_max_is_strong(features)
        )
    )
    if observed_today_max_c is not None and observed_today_max_strong:
        if high_c is not None and high_c < float(observed_today_max_c) - 0.25:
            hard_zero = True
            hard_reasons.append("observed_today_max_above_bucket")
            _record_group_component(
                components,
                component_groups,
                group="temperature_trajectory",
                name="observed_today_max_lower_bound",
                factor=EPS,
                weight=config.observed_temp_weight,
            )
        elif _feature_bool(
            features,
            "temperature_observation_final_daily_max",
            "observed_today_max_is_final",
            "daily_max_observation_complete",
        ):
            final_sigma_c = _env_float("PM_BAYES_FINAL_OBSERVED_MAX_SIGMA_C", 0.45)
            final_mass = _normal_bucket_mass(
                bucket,
                mu_c=float(observed_today_max_c),
                sigma_c=max(0.15, float(final_sigma_c)),
                eq_width_c=_env_float("PM_BAYES_FINAL_OBSERVED_MAX_EQ_WIDTH_C", 1.0),
            )
            _record_group_component(
                components,
                component_groups,
                group="temperature_trajectory",
                name="observed_today_max_final",
                factor=max(EPS, final_mass),
                weight=config.final_observed_max_weight,
            )

    peak_meta = features.get("peak_estimator") if isinstance(features.get("peak_estimator"), Mapping) else None
    if not isinstance(peak_meta, Mapping):
        peak_meta = estimate_peak_temperature_from_features(features)
    peak_mu = _to_float(peak_meta.get("mu_c")) if isinstance(peak_meta, Mapping) else None
    peak_sigma = _to_float(peak_meta.get("sigma_c")) if isinstance(peak_meta, Mapping) else None
    if (
        isinstance(peak_meta, Mapping)
        and peak_meta.get("enabled")
        and str(peak_meta.get("source") or "") != "forecast_only"
        and peak_mu is not None
        and peak_sigma is not None
    ):
        peak_mass = _normal_bucket_mass(
            bucket,
            mu_c=float(peak_mu),
            sigma_c=max(0.25, float(peak_sigma)),
            eq_width_c=_env_float("PM_BAYES_PEAK_ESTIMATOR_EQ_WIDTH_C", 1.0),
        )
        _record_group_component(
            components,
            component_groups,
            group="temperature_trajectory",
            name="peak_estimator",
            factor=peak_mass,
            weight=config.peak_estimator_weight,
        )

    guard_meta = (
        peak_meta.get("high_temp_amplification_guard")
        if isinstance(peak_meta, Mapping) and isinstance(peak_meta.get("high_temp_amplification_guard"), Mapping)
        else _high_temp_amplification_guard_meta(
            features,
            observed_c=obs_c,
            forecast_c=ref_c,
            rise_rate=_feature(
                features,
                "temperature_rise_rate_c_per_hour",
                "observed_temp_rise_rate_c_per_hour",
                "metar_temp_rise_rate_c_per_hour",
            ),
        )
    )
    high_temp_guard_active = bool(guard_meta.get("active")) if isinstance(guard_meta, Mapping) else False
    guard_obs_c = _to_float(guard_meta.get("observed_temp_c")) if isinstance(guard_meta, Mapping) else None
    guard_block_margin_c = max(0.0, _env_float("PM_BAYES_HIGH_TEMP_GUARD_BUCKET_MARGIN_C", 1.5))
    guard_blocks_bucket = bool(
        high_temp_guard_active
        and bucket.kind != "le"
        and guard_obs_c is not None
        and rep_c >= float(guard_obs_c) + guard_block_margin_c
    )
    if guard_blocks_bucket:
        factor = _clamp(_env_float("PM_BAYES_HIGH_TEMP_GUARD_BUCKET_FACTOR", 0.65), EPS, 1.0)
        _record_group_component(
            components,
            component_groups,
            group="temperature_trajectory",
            name="high_temp_amplification_guard",
            factor=factor,
            weight=config.peak_estimator_weight,
        )

    settlement_forecast_c = _feature(features, "settlement_source_forecast_peak_c", "wu_forecast_peak_c")
    if settlement_forecast_c is not None and config.settlement_source_forecast_weight > 0:
        forecast_sigma = max(
            0.35,
            _env_float("PM_BAYES_SETTLEMENT_SOURCE_FORECAST_SIGMA_C", 1.1),
        )
        forecast_mass = _normal_bucket_mass(
            bucket,
            mu_c=float(settlement_forecast_c),
            sigma_c=forecast_sigma,
            eq_width_c=_env_float("PM_BAYES_SETTLEMENT_SOURCE_FORECAST_EQ_WIDTH_C", 1.0),
        )
        _record_group_component(
            components,
            component_groups,
            group="residual_correction",
            name="settlement_source_forecast_peak",
            factor=max(EPS, forecast_mass),
            weight=config.settlement_source_forecast_weight,
        )

    tail_support_max_c = _estimate_tail_support_max_c(
        bucket,
        features,
        peak_mu=peak_mu,
        peak_sigma=peak_sigma,
        quantile=config.ge_tail_support_quantile,
        zscore=config.ge_tail_support_zscore,
    )
    if tail_support_max_c is not None and bucket.kind == "ge":
        support_gap = float(tail_support_max_c) - rep_c
        if support_gap <= -max(0.0, float(config.ge_tail_hard_threshold_c)):
            hard_zero = True
            hard_reasons.append("tail_support_below_bucket")
            _record_group_component(
                components,
                component_groups,
                group="residual_correction",
                name="tail_support",
                factor=EPS,
                weight=config.ge_tail_support_weight,
            )
        else:
            hard_width = max(0.2, float(config.ge_tail_soft_width_c))
            raw_factor = _sigmoid((support_gap + float(config.ge_tail_soft_margin_c)) / hard_width)
            factor = _clamp(raw_factor, float(config.ge_tail_soft_min_factor), 1.0)
            _record_group_component(
                components,
                component_groups,
                group="residual_correction",
                name="tail_support",
                factor=factor,
                weight=config.ge_tail_support_weight,
            )

    cti = _clear_sky_ratio(features)
    if cti is not None:
        cti = _clamp(cti, 0.0, 1.5)
        hot_delta = rep_c - float(ref_c)
        if cti <= config.low_cti:
            factor = 1.0 - (0.65 * _sigmoid((hot_delta - config.high_temp_delta_c) / 1.2))
        elif cti >= config.high_cti:
            factor = 0.85 + (0.30 * _sigmoid((hot_delta - 0.5) / 1.5))
        else:
            factor = 0.85 + 0.15 * _sigmoid(-abs(hot_delta) / 2.0)
        if guard_blocks_bucket:
            factor = min(1.0, factor)
        _record_group_component(
            components,
            component_groups,
            group="radiation",
            name="radiation_cti",
            factor=factor,
            weight=config.radiation_weight,
        )

    cloud = _effective_cloud_cover_pct(features)
    if cloud is not None:
        cloud_pct = cloud / 100.0 if cloud > 1.0 else cloud
        hot_delta = rep_c - float(ref_c)
        low_cloud = _feature(features, "cloud_cover_low_pct", "low_cloud_cover_pct", "cloud_low_pct")
        low_cloud_pct = _clamp(float(low_cloud or 0.0) / 100.0 if float(low_cloud or 0.0) > 1.0 else float(low_cloud or 0.0), 0.0, 1.0)
        low_cloud_extra = 0.18 * low_cloud_pct
        low_factor = max(0.05, 1.0 - _clamp(cloud_pct, 0.0, 1.0) * (0.55 + config.cloud_low_pct + low_cloud_extra))
        factor = low_factor if hot_delta > config.high_temp_delta_c else 0.85 + 0.15 * (1.0 - _clamp(cloud_pct, 0.0, 1.0))
        _record_group_component(
            components,
            component_groups,
            group="cloud_shading",
            name="cloud",
            factor=factor,
            weight=config.cloud_weight,
        )

    precip_profile = precipitation_evidence_profile(features)
    precip_rate = _precip_rate_mm_per_h(features)
    if precip_rate is not None:
        intensity = _clamp(math.log1p(float(precip_rate)) / math.log(9.0), 0.0, 1.5)
        raining = bool(
            _feature_bool(features, "metar_rain_observed", "radar_precip_observed")
            or (
                _feature_bool(features, "is_raining_observed")
                and precip_profile.get("available_strong")
            )
        )
        if raining:
            intensity = max(intensity, 0.35)
        hot_delta = rep_c - float(ref_c)
        if precip_profile.get("no_precip_confirmed") and precip_rate <= EPS and not raining:
            factor = 1.0
        elif hot_delta > 0.0:
            factor = 1.0 - min(0.88, (0.22 + 0.48 * intensity) * _sigmoid((hot_delta - 0.3) / 1.0))
        else:
            factor = 0.96 + 0.16 * intensity * _sigmoid((-hot_delta + 0.5) / 1.2)
        _record_group_component(
            components,
            component_groups,
            group="precipitation_evaporative_cooling",
            name="precip_evaporative_cooling",
            factor=factor,
            weight=config.precipitation_weight,
        )
        precip_group = component_groups.get("precipitation_evaporative_cooling")
        if isinstance(precip_group, dict):
            precip_group["source_profile"] = dict(precip_profile)
            precip_group["evidence_state"] = str(precip_profile.get("evidence_state") or "missing")

    wet_bulb_c = _feature(features, "wet_bulb_c")
    if wet_bulb_c is None:
        wet_bulb_c = _wet_bulb_c(obs_c, dew_c)
    if wet_bulb_c is not None:
        hot_delta = rep_c - float(ref_c)
        depression = max(0.0, rep_c - float(wet_bulb_c))
        humid_cap = 0.50 + 0.50 * _sigmoid((depression - 5.5) / 1.6)
        factor = 1.0 - (1.0 - humid_cap) * _sigmoid((hot_delta - 0.5) / 1.4)
        _record_group_component(
            components,
            component_groups,
            group="wet_bulb_constraint",
            name="wet_bulb",
            factor=factor,
            weight=config.wet_bulb_weight,
        )

    wind_speed = _feature(features, "wind_speed_mps", "wind_speed_10m_mps")
    if wind_speed is None:
        mph = _feature(features, "wind_speed_mph")
        if mph is not None:
            wind_speed = mph * 0.44704
    wind_dir = _feature(features, "wind_direction_deg", "wind_direction_10m_deg")
    if wind_speed is not None and wind_dir is not None:
        warm = _parse_sectors(features.get("warm_wind_dirs"), [(180.0, 260.0)])
        cool = _parse_sectors(features.get("cool_wind_dirs"), [(300.0, 60.0)])
        marine = _parse_sectors(features.get("marine_wind_dirs"), [(90.0, 160.0)])
        speed_scale = _clamp(wind_speed / max(0.1, config.wind_strong_mps), 0.0, 1.5)
        hot_delta = rep_c - float(ref_c)
        if _in_direction(wind_dir, cool) or _in_direction(wind_dir, marine):
            factor = 1.0 - 0.42 * speed_scale * _sigmoid((hot_delta - 0.5) / 1.5)
        elif _in_direction(wind_dir, warm):
            factor = 0.85 + 0.35 * speed_scale * _sigmoid((hot_delta - 0.5) / 1.5)
        else:
            factor = 1.0 - 0.12 * speed_scale
        _record_group_component(
            components,
            component_groups,
            group="airmass_mixing",
            name="wind_airmass",
            factor=factor,
            weight=config.wind_weight,
        )

    pressure_trend = _feature(features, "pressure_tendency_hpa_3h", "pressure_trend_hpa_3h", "pressure_change_hpa_3h")
    if pressure_trend is not None:
        hot_delta = rep_c - float(ref_c)
        if pressure_trend >= 1.0:
            factor = 1.0 - 0.28 * _sigmoid((hot_delta - 0.5) / 1.5)
        elif pressure_trend <= -1.0:
            factor = 0.90 + 0.24 * _sigmoid((hot_delta - 0.5) / 1.5)
        else:
            factor = 1.0
        _record_group_component(
            components,
            component_groups,
            group="airmass_mixing",
            name="pressure_trend",
            factor=factor,
            weight=config.pressure_weight,
        )

    log_likelihood = sum(components.values())
    if hard_zero:
        log_likelihood = math.log(EPS)
    return {
        "label": bucket.label,
        "log_likelihood": log_likelihood,
        "likelihood": math.exp(max(math.log(EPS), min(math.log(10.0), log_likelihood))),
        "components": components,
        "hard_zero": hard_zero,
        "hard_reasons": hard_reasons,
        "representative_temp_c": rep_c,
        "reference_temp_c": ref_c,
        "tail_support_max_c": tail_support_max_c,
        "peak_estimator": peak_meta,
        "component_groups": component_groups,
    }


def compute_weather_likelihood_vector(
    labels: Sequence[str],
    features: Optional[Mapping[str, Any]],
    *,
    hours_to_event: Optional[float] = None,
    base_probs: Optional[Sequence[float]] = None,
) -> Dict[str, Any]:
    if not features:
        return {"enabled": False, "source": "missing_features", "likelihood_probs": []}
    config = dynamic_likelihood_config(hours_to_event)
    buckets = [parse_temperature_bucket(label) for label in labels]
    if not buckets or any(bucket is None for bucket in buckets):
        return {"enabled": False, "source": "unsupported_labels", "likelihood_probs": []}

    rows = [compute_bucket_log_likelihood(bucket, features, config) for bucket in buckets if bucket is not None]
    active_evidence_count = sum(
        1
        for row in rows
        if row.get("hard_zero") or bool(row.get("components"))
    )
    max_log = max(float(row["log_likelihood"]) for row in rows) if rows else 0.0
    likelihoods = [math.exp(float(row["log_likelihood"]) - max_log) for row in rows]
    likelihood_probs = _normalize(likelihoods)

    adjusted_probs: List[float] = []
    if base_probs is not None and len(base_probs) == len(likelihood_probs):
        adjusted_probs = _normalize([max(EPS, float(p)) * likelihood_probs[idx] for idx, p in enumerate(base_probs)])

    values = _temperature_values_from_features(features)
    bimodal = detect_bimodal_temperature_distribution(
        values,
        min_separation_c=config.bimodal_min_separation_c,
        valley_ratio=config.bimodal_valley_ratio,
    )
    # Multi-modality is uncertainty structure, not an independent legacy
    # sizing gate.  The canonical joint weak mixture preserves the scenarios;
    # confidence/position capacity must come from its approved information
    # surface and posterior, never from this binary old special case.
    risk_size_scale = 1.0
    physical_evidence = _summarize_component_groups(rows)
    return {
        "enabled": True,
        "source": "weather_physical_likelihood",
        "probability_evidence_enabled": active_evidence_count > 0,
        "active_evidence_count": active_evidence_count,
        "likelihood_probs": likelihood_probs,
        "adjusted_probs": adjusted_probs,
        "rows": rows,
        "config": {
            "dewpoint_weight": config.dewpoint_weight,
            "observed_temp_weight": config.observed_temp_weight,
            "peak_estimator_weight": config.peak_estimator_weight,
            "radiation_weight": config.radiation_weight,
            "cloud_weight": config.cloud_weight,
            "precipitation_weight": config.precipitation_weight,
            "wind_weight": config.wind_weight,
            "pressure_weight": config.pressure_weight,
            "wet_bulb_weight": config.wet_bulb_weight,
            "cloud_low_pct": config.cloud_low_pct,
            "wind_strong_mps": config.wind_strong_mps,
            "ge_tail_hard_threshold_c": config.ge_tail_hard_threshold_c,
            "ge_tail_soft_width_c": config.ge_tail_soft_width_c,
            "ge_tail_soft_margin_c": config.ge_tail_soft_margin_c,
            "ge_tail_soft_min_factor": config.ge_tail_soft_min_factor,
            "ge_tail_support_weight": config.ge_tail_support_weight,
            "ge_tail_support_quantile": config.ge_tail_support_quantile,
            "ge_tail_support_zscore": config.ge_tail_support_zscore,
            "final_observed_max_weight": config.final_observed_max_weight,
            "settlement_source_forecast_weight": config.settlement_source_forecast_weight,
        },
        "bimodal": bimodal,
        "risk_size_scale": risk_size_scale,
        "physical_evidence": physical_evidence,
    }
