from __future__ import annotations

import math
import os
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from bayesian_likelihood_runtime import (
    compute_weather_likelihood_vector,
    estimate_peak_temperature_from_features,
    precipitation_evidence_profile,
)
from climo_regime_runtime import (
    cap_climo_evidence_by_pp_delta,
    climo_regime_bucket_probabilities,
    climo_regime_evidence_scale,
    climo_regime_explanation,
    climo_regime_pp_cap,
)
from lcb_stage_runtime import DEFAULT_BAYES_STAGE_Z, resolve_lcb_stage_z
from temperature_posterior_runtime import (
    VERSION as CTP_VERSION,
    bucket_probabilities_by_label,
    build_temperature_posterior_distribution,
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python without zoneinfo
    ZoneInfo = None  # type: ignore[assignment]


EPS = 1e-12
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
TEMPERATURE_MOMENTUM_RECENT_WINDOWS: Tuple[str, ...] = ("1m", "2m", "3m", "5m", "10m", "15m", "30m", "45m", "1h")
TEMPERATURE_MOMENTUM_RATE_ABS_MAX = 6.0
TEMPERATURE_MOMENTUM_POSTERIOR_CONFIRM_WINDOWS: Tuple[str, ...] = ("30m", "45m", "1h", "2h", "3h", "6h")


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bool_feature(value: Any) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _env_float(key: str, default: float) -> float:
    val = _to_float(os.environ.get(key))
    if val is None:
        return float(default)
    return float(val)


def _clamp01(value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return max(0.0, min(1.0, float(value)))


def _normalize(values: List[float]) -> List[float]:
    cleaned = [max(0.0, float(v)) if math.isfinite(float(v)) else 0.0 for v in values]
    total = sum(cleaned)
    if total <= EPS:
        n = len(cleaned)
        return [1.0 / n for _ in cleaned] if n else []
    return [v / total for v in cleaned]


def _feature_float(features: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        val = _to_float(features.get(key))
        if val is not None:
            return val
    return None


def _observed_path_temp_from_features(features: Mapping[str, Any]) -> Optional[float]:
    current = _feature_float(
        features,
        "observed_path_temp_c",
        "observed_temp_c",
        "current_temp_c",
        "temperature_now_c",
        "observed_temp",
        "current_temp",
        "temperature_now",
    )
    today_max = _feature_float(features, "observed_today_max_c", "daily_max_observed_c")
    if current is not None and today_max is not None:
        return max(float(current), float(today_max))
    if today_max is not None:
        return float(today_max)
    return current


def _feature_bool(features: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        val = features.get(key)
        if isinstance(val, bool):
            if val:
                return True
            continue
        if str(val or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _physical_likelihood_evidence_scale(
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


def _parse_event_date(value: Any) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _csv_tokens(value: Any) -> List[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _feature_text(features: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        val = features.get(key)
        text = str(val or "").strip()
        if text:
            return text
    return ""


def _normalize_station_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())


def _soft_forecast_scope_meta(features: Mapping[str, Any]) -> Dict[str, Any]:
    city_allow = {_normalize_text(item) for item in _csv_tokens(os.environ.get("PM_BAYES_SOFT_FORECAST_CITY_ALLOWLIST"))}
    station_allow = {
        _normalize_station_text(item)
        for item in _csv_tokens(os.environ.get("PM_BAYES_SOFT_FORECAST_STATION_ALLOWLIST"))
    }
    slug_allow = {_normalize_text(item) for item in _csv_tokens(os.environ.get("PM_BAYES_SOFT_FORECAST_SLUG_ALLOWLIST"))}
    city_allow.discard("")
    station_allow.discard("")
    slug_allow.discard("")
    station = _normalize_station_text(
        _feature_text(
            features,
            "soft_forecast_scope_station_id",
            "station_id",
            "source_station_id",
            "taf_forecast_station_id",
        )
    )
    city = _normalize_text(
        _feature_text(
            features,
            "soft_forecast_scope_city",
            "city",
            "city_key",
            "location_city",
        )
    )
    slug = _normalize_text(
        _feature_text(
            features,
            "soft_forecast_scope_slug",
            "slug",
            "event_slug",
            "market_slug",
        )
    )
    title = _normalize_text(_feature_text(features, "soft_forecast_scope_title", "title", "event_title"))
    source = _normalize_text(
        _feature_text(
            features,
            "soft_forecast_scope_resolution_source",
            "resolution_source",
            "wu_settlement_page_url",
        )
    )
    if not city_allow and not station_allow and not slug_allow:
        return {
            "allowed": True,
            "reason": "no_allowlist",
            "city": city,
            "station_id": station,
            "slug": slug,
        }
    if station_allow and station in station_allow:
        return {
            "allowed": True,
            "reason": "station_allowlist",
            "city": city,
            "station_id": station,
            "slug": slug,
        }
    if slug_allow and slug in slug_allow:
        return {
            "allowed": True,
            "reason": "slug_allowlist",
            "city": city,
            "station_id": station,
            "slug": slug,
        }
    if city_allow:
        if city in city_allow:
            return {
                "allowed": True,
                "reason": "city_allowlist",
                "city": city,
                "station_id": station,
                "slug": slug,
            }
        if "chengdu" in city_allow and (
            "chengdu" in slug
            or "chengdu" in title
            or station == "ZUUU"
            or "chengdu/zuuu" in source
        ):
            return {
                "allowed": True,
                "reason": "chengdu_scope_alias",
                "city": city,
                "station_id": station,
                "slug": slug,
            }
    return {
        "allowed": False,
        "reason": "scope_not_allowed",
        "city": city,
        "station_id": station,
        "slug": slug,
    }


def _physical_likelihood_scope_meta(features: Mapping[str, Any]) -> Dict[str, Any]:
    city_allow = {_normalize_text(item) for item in _csv_tokens(os.environ.get("PM_BAYES_LIKELIHOOD_CITY_ALLOWLIST"))}
    station_allow = {
        _normalize_station_text(item)
        for item in _csv_tokens(os.environ.get("PM_BAYES_LIKELIHOOD_STATION_ALLOWLIST"))
    }
    slug_allow = {_normalize_text(item) for item in _csv_tokens(os.environ.get("PM_BAYES_LIKELIHOOD_SLUG_ALLOWLIST"))}
    city_allow.discard("")
    station_allow.discard("")
    slug_allow.discard("")
    station = _normalize_station_text(
        _feature_text(
            features,
            "physical_likelihood_scope_station_id",
            "station_id",
            "source_station_id",
            "taf_forecast_station_id",
        )
    )
    city = _normalize_text(
        _feature_text(
            features,
            "physical_likelihood_scope_city",
            "city",
            "city_key",
            "location_city",
        )
    )
    slug = _normalize_text(
        _feature_text(
            features,
            "physical_likelihood_scope_slug",
            "slug",
            "event_slug",
            "market_slug",
        )
    )
    title = _normalize_text(_feature_text(features, "physical_likelihood_scope_title", "title", "event_title"))
    source = _normalize_text(
        _feature_text(
            features,
            "physical_likelihood_scope_resolution_source",
            "resolution_source",
            "wu_settlement_page_url",
        )
    )
    if not city_allow and not station_allow and not slug_allow:
        return {
            "allowed": True,
            "reason": "no_allowlist",
            "city": city,
            "station_id": station,
            "slug": slug,
        }
    if station_allow and station in station_allow:
        return {
            "allowed": True,
            "reason": "station_allowlist",
            "city": city,
            "station_id": station,
            "slug": slug,
        }
    if slug_allow and slug in slug_allow:
        return {
            "allowed": True,
            "reason": "slug_allowlist",
            "city": city,
            "station_id": station,
            "slug": slug,
        }
    if city_allow:
        if city in city_allow:
            return {
                "allowed": True,
                "reason": "city_allowlist",
                "city": city,
                "station_id": station,
                "slug": slug,
            }
        if "chengdu" in city_allow and (
            "chengdu" in slug
            or "chengdu" in title
            or station == "ZUUU"
            or "chengdu/zuuu" in source
        ):
            return {
                "allowed": True,
                "reason": "chengdu_scope_alias",
                "city": city,
                "station_id": station,
                "slug": slug,
            }
    return {
        "allowed": False,
        "reason": "scope_not_allowed",
        "city": city,
        "station_id": station,
        "slug": slug,
    }


def _normalize_unit(value: Any) -> str:
    raw = str(value or "").strip().upper().replace("°", "")
    return raw or ""


def _label_unit(label: str) -> str:
    m = re.search(r"([A-Za-z°/%]+)\s*$", str(label or "").strip())
    return _normalize_unit(m.group(1)) if m else ""


def _bucket_hit(actual: float, label: str, rounding_rule: Optional[str]) -> Optional[bool]:
    raw = str(label or "").strip().replace("–", "-")
    if not raw:
        return None
    num = r"(-?\d+(?:\.\d+)?)"
    unit = r"([A-Za-z°/%]+)?"
    value = float(actual)
    if str(rounding_rule or "").strip().lower() == "whole_degrees" and _label_unit(raw) in {"C", "F"}:
        # WU/TWC settlement pages expose whole-degree display values. Decimal API
        # readings are not a floor-to-bucket rule; 27.78C has already exceeded
        # the <=27C bucket and displays as 28C.
        value = float(math.floor(value + 0.5))

    m = re.match(rf"^{num}\s*-\s*{num}\s*{unit}$", raw)
    if not m:
        m = re.match(rf"^{num}\s+to\s+{num}\s*{unit}$", raw, flags=re.I)
    if m:
        low = min(float(m.group(1)), float(m.group(2)))
        high = max(float(m.group(1)), float(m.group(2)))
        return low <= value <= high

    m = re.match(rf"^{num}\s*{unit}\s+or\s+below$", raw, flags=re.I)
    if m:
        return value <= float(m.group(1))
    m = re.match(rf"^{num}\s*{unit}\s+or\s+(higher|above|more)$", raw, flags=re.I)
    if m:
        return value >= float(m.group(1))
    m = re.match(rf"^{num}\s*{unit}$", raw)
    if m:
        return abs(value - float(m.group(1))) <= 1e-9
    return None


def _temperature_bucket_bounds(label: str, rounding_rule: Optional[str]) -> Optional[Tuple[float, float]]:
    raw = str(label or "").strip().replace("–", "-")
    if not raw:
        return None
    num = r"(-?\d+(?:\.\d+)?)"
    unit = r"([A-Za-z°/%]+)?"
    whole_degrees = str(rounding_rule or "").strip().lower() == "whole_degrees" and _label_unit(raw) in {"C", "F"}
    rounded_degrees = str(rounding_rule or "").strip().lower() == "rounded_degrees" and _label_unit(raw) in {"C", "F"}

    def eq_bounds(value: float) -> Tuple[float, float]:
        if whole_degrees or rounded_degrees:
            return float(value) - 0.5, float(value) + 0.5
        return float(value) - 0.5, float(value) + 0.5

    m = re.match(rf"^{num}\s*-\s*{num}\s*{unit}$", raw)
    if not m:
        m = re.match(rf"^{num}\s+to\s+{num}\s*{unit}$", raw, flags=re.I)
    if m:
        low = min(float(m.group(1)), float(m.group(2)))
        high = max(float(m.group(1)), float(m.group(2)))
        if rounded_degrees or whole_degrees:
            return low - 0.5, high + 0.5
        return (low, high)

    m = re.match(rf"^{num}\s*{unit}\s+or\s+below$", raw, flags=re.I)
    if m:
        k = float(m.group(1))
        if rounded_degrees or whole_degrees:
            return float("-inf"), k + 0.5
        return float("-inf"), k
    m = re.match(rf"^{num}\s*{unit}\s+or\s+(higher|above|more)$", raw, flags=re.I)
    if m:
        k = float(m.group(1))
        return (k - 0.5 if (rounded_degrees or whole_degrees) else k), float("inf")
    m = re.match(rf"^{num}\s*{unit}$", raw)
    if m:
        return eq_bounds(float(m.group(1)))
    return None


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    if math.isinf(x):
        return 0.0 if x < 0 else 1.0
    z = (float(x) - float(mu)) / max(EPS, float(sigma))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _normal_bucket_probs(
    labels: List[str],
    *,
    mu: float,
    sigma: float,
    rounding_rule: Optional[str],
) -> List[float]:
    probs: List[float] = []
    for label in labels:
        bounds = _temperature_bucket_bounds(label, rounding_rule)
        if bounds is None:
            probs.append(0.0)
            continue
        low, high = bounds
        p = _normal_cdf(float(high), float(mu), float(sigma)) - _normal_cdf(float(low), float(mu), float(sigma))
        probs.append(max(0.0, float(p)))
    return _normalize(probs) if any(v > EPS for v in probs) else [0.0] * len(labels)


def _temperature_momentum_posterior_gate(features: Mapping[str, Any], *, state: str, rate: Optional[float]) -> Dict[str, Any]:
    explicit = _bool_feature(features.get("temperature_momentum_posterior_gate_allowed"))
    confirmed_raw = str(features.get("temperature_momentum_posterior_confirmed_windows") or "").strip()
    confirmed_windows = [part.strip() for part in confirmed_raw.split(",") if part.strip()]
    if not confirmed_windows:
        confirmed_windows = [
            label
            for label in TEMPERATURE_MOMENTUM_POSTERIOR_CONFIRM_WINDOWS
            if _to_float(features.get(f"temperature_momentum_{label}_c_per_hour")) is not None
            and float(features[f"temperature_momentum_{label}_c_per_hour"]) >= 0.25
        ]
    positive_short = [
        label
        for label in ("1m", "2m", "3m", "5m", "10m", "15m")
        if _to_float(features.get(f"temperature_momentum_{label}_c_per_hour")) is not None
        and float(features[f"temperature_momentum_{label}_c_per_hour"]) >= 0.35
    ]
    hard_source_text = " ".join(
        str(features.get(key) or "")
        for key in (
            "observation_path_primary_source",
            "observed_today_max_source",
            "observed_temp_source",
            "temperature_momentum_source",
            "temperature_rise_rate_source",
            "temperature_trajectory_stable_rate_source",
        )
    ).lower()
    hard_observation_source = any(token in hard_source_text for token in ("zuuu", "metar", "awc", "airport"))
    one_hour_rate = _feature_float(
        features,
        "temperature_momentum_1h_c_per_hour",
        "zuuu_metar_raw_awc_temperature_momentum_1h_c_per_hour",
    )
    one_hour_context_positive = [
        label
        for label in ("2h", "3h", "6h")
        if _feature_float(
            features,
            f"temperature_momentum_{label}_c_per_hour",
            f"zuuu_metar_raw_awc_temperature_momentum_{label}_c_per_hour",
        )
        is not None
        and float(
            _feature_float(
                features,
                f"temperature_momentum_{label}_c_per_hour",
                f"zuuu_metar_raw_awc_temperature_momentum_{label}_c_per_hour",
            )
        )
        >= 0.35
    ]
    one_hour_threshold = max(
        0.0,
        _env_float("PM_TEMPERATURE_MOMENTUM_ONE_HOUR_POSTERIOR_GATE_MIN_C_PER_HOUR", 0.75),
    )
    one_hour_allowed = bool(
        hard_observation_source
        and rate is not None
        and float(rate) >= one_hour_threshold
        and one_hour_rate is not None
        and float(one_hour_rate) >= one_hour_threshold
        and bool(one_hour_context_positive)
        and state in {"sustained_warming", "emerging_warming", "weak_warming"}
    )
    context_watch_allowed = bool(
        hard_observation_source
        and state == "context_warming_watch"
        and rate is not None
        and float(rate) >= _env_float("PM_TEMPERATURE_MOMENTUM_CONTEXT_POSTERIOR_GATE_MIN_C_PER_HOUR", 0.45)
        and (one_hour_rate is None or float(one_hour_rate) >= -0.05)
    )
    explicit_reason = str(features.get("temperature_momentum_posterior_gate_reason") or "").strip()
    if explicit is not None:
        if explicit and explicit_reason in {"one_hour_hard_momentum_confirmed", "hard_observation_one_hour_momentum_confirmed"} and not one_hour_context_positive:
            allowed = False
        else:
            allowed = explicit
    else:
        allowed = bool(
            (
                rate is not None
                and float(rate) >= 0.25
                and len(confirmed_windows) >= 2
                and state in {"sustained_warming", "emerging_warming", "weak_warming"}
            )
            or one_hour_allowed
            or context_watch_allowed
        )
    if allowed:
        gate_state = "confirmed"
        if explicit is not None and explicit_reason:
            reason = explicit_reason
        elif one_hour_allowed:
            reason = "one_hour_context_confirmed"
        elif context_watch_allowed:
            reason = "hard_observation_context_warming_watch"
        else:
            reason = "multi_window_confirmed"
    elif positive_short and len(confirmed_windows) < 2:
        gate_state = "short_window_watch"
        reason = "short_window_warming_requires_30m_1h_confirmation"
    elif state in {"cooling", "not_realized", "flat"}:
        gate_state = "not_warming"
        reason = f"momentum_state_{state}"
    else:
        gate_state = "unconfirmed"
        reason = "multi_window_confirmation_missing"
    return {
        "allowed": bool(allowed),
        "state": gate_state,
        "reason": reason,
        "confirmed_windows": confirmed_windows,
        "confirmed_positive_window_count": len(confirmed_windows),
        "positive_short_windows": positive_short,
    }


def _temperature_trajectory_channel_probs(
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


def _post_cool_rebound_risk_payload(
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


def _post_cool_rebound_channel_payload(
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
        risk_payload = _post_cool_rebound_risk_payload(history, features)
        if isinstance(risk_payload.get(child_key), Mapping):
            return dict(risk_payload.get(child_key) or {})
        nested_features = risk_payload.get("features") if isinstance(risk_payload.get("features"), Mapping) else {}
        if isinstance(nested_features, Mapping) and isinstance(nested_features.get(child_key), Mapping):
            return dict(nested_features.get(child_key) or {})
    if channel == "intraday_likelihood":
        return _post_cool_rebound_risk_payload(history, features)
    return {}


def _post_cool_rebound_channel_probs(
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
    payload = _post_cool_rebound_channel_payload(history, features, channel=channel)
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


def _post_cool_rebound_risk_channel_probs(
    labels: List[str],
    history: Mapping[str, Any],
    features: Mapping[str, Any],
    *,
    rounding_rule: Optional[str],
) -> Tuple[List[float], List[float], Dict[str, Any]]:
    return _post_cool_rebound_channel_probs(
        labels,
        history,
        features,
        rounding_rule=rounding_rule,
        channel="intraday_likelihood",
    )


def _format_bucket_number(value: float) -> str:
    if abs(float(value) - round(float(value))) <= 1e-9:
        return str(int(round(float(value))))
    return ("%g" % float(value))


def _atomic_temperature_bucket_label(label: str) -> Tuple[str, bool]:
    raw = str(label or "").strip().replace("–", "-")
    if not raw:
        return raw, False
    num = r"(-?\d+(?:\.\d+)?)"
    unit = r"([A-Za-z°/%]+)?"
    match = re.match(rf"^{num}\s*{unit}\s+or\s+(below|higher|above|more)$", raw, flags=re.I)
    if not match:
        return raw, False
    unit_norm = _normalize_unit(match.group(2))
    if unit_norm not in {"C", "F"}:
        return raw, False
    suffix = "°C" if unit_norm == "C" else "°F"
    return f"{_format_bucket_number(float(match.group(1)))}{suffix}", True


def _atomic_temperature_bucket_labels(labels: List[str]) -> Tuple[List[str], bool]:
    out: List[str] = []
    changed = False
    for label in labels:
        atomic, is_changed = _atomic_temperature_bucket_label(label)
        out.append(atomic)
        changed = changed or is_changed
    return out, changed


def _extract_likelihood_features(
    pre_items: List[Dict[str, Any]],
    history: Mapping[str, Any],
) -> Dict[str, Any]:
    if isinstance(history.get("likelihood_features"), Mapping):
        return dict(history.get("likelihood_features") or {})
    if isinstance(history.get("weather_likelihood_features"), Mapping):
        return dict(history.get("weather_likelihood_features") or {})
    for item in pre_items:
        for key in ("bayes_likelihood_features", "likelihood_features", "weather_likelihood_features"):
            val = item.get(key)
            if isinstance(val, Mapping) and val:
                return dict(val)
    return {}


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _feature_date_text(features: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        raw = str(features.get(key) or "").strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            return raw
        if raw:
            parsed = _parse_iso_datetime(raw)
            if parsed is not None:
                return parsed.date().isoformat()
    return ""


def _timezone_for_name(name: Any) -> timezone:
    text = str(name or "").strip()
    if text and ZoneInfo is not None:
        try:
            return ZoneInfo(text)  # type: ignore[return-value]
        except Exception:
            pass
    return timezone.utc


def _parse_local_datetime(value: Any, zone: timezone) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def _peak_hour_distribution_meta_from_features(
    features: Mapping[str, Any],
    *,
    zone: timezone,
    event_date: str = "",
) -> Tuple[Optional[float], Dict[str, Any]]:
    raw = features.get("expected_peak_hour_distribution") or features.get("peak_hour_distribution")
    items: List[Tuple[float, float]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            hour = _to_float(key)
            prob = _to_float(value)
            if hour is not None and prob is not None and float(prob) > 0:
                items.append((max(0.0, min(23.99, float(hour))), float(prob)))
    elif isinstance(raw, list):
        for row in raw:
            if not isinstance(row, Mapping):
                continue
            valid_local = str(row.get("valid_time_local") or row.get("expected_peak_at_local") or "").strip()
            if event_date and valid_local:
                local_dt = _parse_local_datetime(valid_local, zone)
                if local_dt is not None and local_dt.date().isoformat() != event_date:
                    continue
            hour = _to_float(row.get("hour_local") or row.get("hour") or row.get("peak_hour"))
            prob = _to_float(row.get("prob") or row.get("probability") or row.get("weight"))
            if hour is not None and prob is not None and float(prob) > 0:
                items.append((max(0.0, min(23.99, float(hour))), float(prob)))
    total = sum(prob for _, prob in items)
    if not items or total <= 0:
        return None, {}
    center = sum(hour * prob for hour, prob in items) / total
    start = _to_float(features.get("expected_peak_window_start_hour_local") or features.get("peak_window_start_hour_local"))
    end = _to_float(features.get("expected_peak_window_end_hour_local") or features.get("peak_window_end_hour_local"))
    if start is None:
        start = min(hour for hour, _ in items)
    if end is None:
        end = max(hour for hour, _ in items)
    return max(0.0, min(23.99, float(center))), {
        "expected_peak_hour_source": str(features.get("expected_peak_hour_source") or "expected_peak_hour_distribution"),
        "expected_peak_hour_local": float(center),
        "expected_peak_at_local": str(features.get("expected_peak_at_local") or ""),
        "expected_peak_window_start_hour_local": float(start),
        "expected_peak_window_end_hour_local": float(end),
        "expected_peak_window_source": str(features.get("expected_peak_window_source") or "expected_peak_hour_distribution"),
    }


def _wu_forecast_peak_hour_from_features(
    features: Mapping[str, Any],
    *,
    zone: timezone,
    event_date: str = "",
) -> Tuple[Optional[float], Dict[str, Any]]:
    dist_hour, dist_meta = _peak_hour_distribution_meta_from_features(
        features,
        zone=zone,
        event_date=event_date,
    )
    if dist_hour is not None:
        return dist_hour, dist_meta
    for key in (
        "wu_forecast_peak_at",
        "wu_forecast_peak_valid_local",
        "weather_com_forecast_peak_at",
        "twc_forecast_peak_at",
    ):
        local_dt = _parse_local_datetime(features.get(key), zone)
        if local_dt is None:
            continue
        if event_date and local_dt.date().isoformat() != event_date:
            continue
        hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0
        return max(0.0, min(23.99, float(hour))), {
            "expected_peak_hour_source": key,
            "expected_peak_hour_local": float(hour),
            "expected_peak_at_local": local_dt.isoformat(),
        }
    return None, {"expected_peak_hour_source": "wu_forecast_peak_time_missing"}


def _hard_source_window_local_dt_override(
    *,
    zone: timezone,
    event_key: str = "",
) -> Optional[datetime]:
    hour_raw = (
        os.environ.get("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_HOUR_OVERRIDE")
        or os.environ.get("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE")
    )
    if hour_raw in (None, ""):
        return None
    try:
        hour = float(str(hour_raw).strip())
    except Exception:
        return None
    if not math.isfinite(hour):
        return None
    date_raw = (
        os.environ.get("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_DATE_OVERRIDE")
        or os.environ.get("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE")
        or event_key
    )
    date_key = _normalize_date_key(date_raw)
    if not date_key:
        return None
    try:
        base_date = datetime.fromisoformat(date_key).date()
    except Exception:
        return None
    hour = max(0.0, min(23.999, float(hour)))
    whole_hour = int(hour)
    minute_float = (hour - whole_hour) * 60.0
    minute = int(minute_float)
    second = int(round((minute_float - minute) * 60.0))
    if second >= 60:
        minute += 1
        second = 0
    if minute >= 60:
        whole_hour = min(23, whole_hour + 1)
        minute = 0
    return datetime(
        base_date.year,
        base_date.month,
        base_date.day,
        whole_hour,
        minute,
        second,
        tzinfo=zone,
    )


def _fahrenheit_to_celsius(value: float) -> float:
    return (float(value) - 32.0) * 5.0 / 9.0


def _celsius_to_fahrenheit(value: float) -> float:
    return float(value) * 9.0 / 5.0 + 32.0


def _integer_fahrenheit_boundary_profile_from_c(
    value_c: Any,
    *,
    rounding_rule: Optional[str],
) -> Dict[str, Any]:
    value = _to_float(value_c)
    if value is None:
        return {"boundary_ambiguous": False, "boundary_status": "missing_value"}
    f_value = _celsius_to_fahrenheit(float(value))
    f_integer = int(round(float(f_value)))
    maps_to_f_integer = abs(float(f_value) - float(f_integer)) <= 1e-6
    actual_is_integer_c = abs(float(value) - round(float(value))) <= 1e-6
    profile: Dict[str, Any] = {
        "source": "reverse_fahrenheit_integer_display",
        "native_value": f_integer,
        "native_unit": "F",
        "native_precision": "integer_display",
        "display_value": float(value),
        "display_unit": "C",
        "settlement_unit": "C",
        "settlement_policy": str(rounding_rule or "whole_degrees"),
        "converted_value_c": float(value),
        "reverse_f_value": float(f_value),
        "maps_to_f_integer": bool(maps_to_f_integer),
        "actual_is_integer_c": bool(actual_is_integer_c),
        "boundary_ambiguous": False,
        "boundary_status": "ok",
        "ambiguous_reason": "",
    }
    if not maps_to_f_integer or actual_is_integer_c:
        return profile
    lo_c = _fahrenheit_to_celsius(float(f_integer) - 0.5)
    hi_c = _fahrenheit_to_celsius(float(f_integer) + 0.5)
    profile["native_rounding_interval"] = [float(f_integer) - 0.5, float(f_integer) + 0.5]
    profile["settlement_interval_c"] = [float(lo_c), float(hi_c)]
    policy = str(rounding_rule or "whole_degrees").strip().lower()
    if "floor" in policy:
        lo_bucket = math.floor(float(lo_c))
        hi_bucket = math.floor(float(hi_c) - 1e-12)
        if abs(float(hi_c) - round(float(hi_c))) > 1e-9:
            hi_bucket = math.floor(float(hi_c))
        profile["settlement_interval_floor_buckets"] = [int(lo_bucket), int(hi_bucket)]
        if lo_bucket != hi_bucket:
            profile["boundary_ambiguous"] = True
            profile["boundary_status"] = "boundary_ambiguous"
            profile["ambiguous_reason"] = "reverse_f_integer_display_crosses_c_floor_bucket"
    elif "whole" in policy or "round" in policy:
        lo_bucket = math.floor(float(lo_c) + 0.5)
        hi_bucket = math.floor(float(hi_c) - 1e-12 + 0.5)
        profile["settlement_interval_rounded_buckets"] = [int(lo_bucket), int(hi_bucket)]
        if lo_bucket != hi_bucket:
            profile["boundary_ambiguous"] = True
            profile["boundary_status"] = "boundary_ambiguous"
            profile["ambiguous_reason"] = "reverse_f_integer_display_crosses_c_rounded_bucket"
    return profile


def _hard_source_value_c(value: Any, unit: Any = "C") -> Optional[float]:
    raw = _to_float(value)
    if raw is None:
        return None
    unit_norm = _normalize_unit(unit or "C")
    if unit_norm in {"", "C"}:
        return float(raw)
    if unit_norm == "F":
        return _fahrenheit_to_celsius(float(raw))
    return None


def _mapping_from_feature(features: Mapping[str, Any], key: str) -> Dict[str, Any]:
    value = features.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _is_settlement_actual_source(source: Any) -> bool:
    text = str(source or "").strip().lower()
    return bool(
        text in {"settlement_actual", "event_runs.settlement_actual"}
        or "settlement_actual" in text
        or "final_settlement" in text
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


def _wu_twc_intraday_hard_source_demotion_reason(features: Mapping[str, Any]) -> str:
    if _feature_float(features, "settlement_actual", "settlement_actual_c") is not None:
        return ""
    if _feature_bool(
        features,
        "observed_today_max_final",
        "hard_source_final_daily_max",
        "hard_source_settlement_final",
        "temperature_observation_final_daily_max",
    ):
        return ""

    alert = _mapping_from_feature(features, "weather_source_delay_deformation_alert")
    alert_tokens = _flatten_feature_tokens(alert)
    alert_text = " ".join(alert_tokens)
    if _to_bool(alert.get("alert"), default=False) and (
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
    if _feature_bool(
        features,
        "wu_twc_observed_intraday_date_mismatch",
        "wu_twc_observed_intraday_event_date_mismatch",
        "wu_twc_observed_intraday_current_observation_event_date_mismatch",
    ):
        return "wu_twc_intraday_event_date_mismatch"
    return ""


def _hard_source_candidate_boundary_profile(
    features: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    rounding_rule: Optional[str],
) -> Dict[str, Any]:
    source = str(candidate.get("source") or "").strip().lower()
    profile: Dict[str, Any] = {}
    if source == "wu_twc_observed_intraday":
        profile = _mapping_from_feature(features, "wu_twc_observed_intraday_settlement_precision")
        if not profile and _feature_bool(features, "wu_twc_observed_intraday_boundary_ambiguous"):
            profile = {
                "boundary_ambiguous": True,
                "ambiguous_reason": _feature_text(
                    features,
                    "wu_twc_observed_intraday_boundary_ambiguous_reason",
                )
                or "wu_twc_observed_intraday_boundary_ambiguous",
            }
    if not profile and _is_settlement_actual_source(source):
        profile = _mapping_from_feature(features, "settlement_actual_precision")
        if not profile and _feature_bool(features, "settlement_actual_boundary_ambiguous"):
            profile = {
                "boundary_ambiguous": True,
                "ambiguous_reason": _feature_text(features, "settlement_actual_boundary_ambiguous_reason")
                or "settlement_actual_boundary_ambiguous",
            }
        if not profile and _to_bool(os.environ.get("PM_BAYES_BLOCK_REVERSE_F_SETTLEMENT_BOUNDARY"), default=True):
            profile = _integer_fahrenheit_boundary_profile_from_c(
                candidate.get("value_c"),
                rounding_rule=rounding_rule,
            )
        awc_conflict = _settlement_below_awc_hard_observation_profile(
            features,
            candidate,
            rounding_rule=rounding_rule,
        )
        if awc_conflict:
            if profile:
                profile = {**profile, **awc_conflict}
            else:
                profile = awc_conflict
        deformation_conflict = _settlement_actual_intraday_deformation_lock_reason(
            features,
            candidate,
            rounding_rule=rounding_rule,
        )
        if deformation_conflict:
            conflict_profile = {
                "boundary_ambiguous": True,
                "boundary_status": "source_conflict",
                "ambiguous_reason": deformation_conflict,
                "settlement_actual_value_c": _to_float(candidate.get("value_c")),
            }
            if profile:
                profile = {**profile, **conflict_profile}
            else:
                profile = conflict_profile
    return profile


def _settlement_bucket_for_policy(value: Any, rounding_rule: Optional[str]) -> Optional[int]:
    raw = _to_float(value)
    if raw is None:
        return None
    policy = str(rounding_rule or "whole_degrees").strip().lower()
    if "round" in policy and "floor" not in policy:
        return int(round(float(raw)))
    if "whole" in policy and "floor" not in policy:
        return int(math.floor(float(raw) + 0.5))
    return int(math.floor(float(raw)))


def _awc_hard_observation_value_c(features: Mapping[str, Any]) -> Optional[float]:
    if _feature_bool(features, "zuuu_metar_raw_awc_date_mismatch"):
        return None
    awc_value = _feature_float(features, "zuuu_metar_raw_awc_today_max_c")
    if awc_value is not None:
        return float(awc_value)
    source_text = _normalize_text(features.get("source"))
    observed_source = _normalize_text(features.get("observed_today_max_source"))
    if (
        "zuuu_metar_raw_awc" in source_text
        or "zuuu_metar_raw_awc" in observed_source
    ) and _feature_bool(features, "observed_today_max_trusted"):
        observed_value = _feature_float(features, "observed_today_max_c")
        if observed_value is not None:
            return float(observed_value)
    return None


def _settlement_below_awc_hard_observation_profile(
    features: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    rounding_rule: Optional[str],
) -> Dict[str, Any]:
    if not _to_bool(os.environ.get("PM_BAYES_BLOCK_SETTLEMENT_BELOW_AWC_OBSERVED"), default=True):
        return {}
    source = str(candidate.get("source") or "").strip().lower()
    if not _is_settlement_actual_source(source):
        return {}
    settlement_bucket = _settlement_bucket_for_policy(candidate.get("value_c"), rounding_rule)
    awc_value = _awc_hard_observation_value_c(features)
    awc_bucket = _settlement_bucket_for_policy(awc_value, rounding_rule)
    if settlement_bucket is None or awc_bucket is None or int(settlement_bucket) >= int(awc_bucket):
        return {}
    return {
        "boundary_ambiguous": True,
        "boundary_status": "source_conflict",
        "ambiguous_reason": "settlement_actual_below_awc_hard_observation",
        "settlement_actual_bucket": int(settlement_bucket),
        "awc_hard_observation_bucket": int(awc_bucket),
        "settlement_actual_value_c": _to_float(candidate.get("value_c")),
        "awc_hard_observation_value_c": float(awc_value) if awc_value is not None else None,
    }


def _nested_float(mapping: Mapping[str, Any], path: Sequence[str]) -> Optional[float]:
    cur: Any = mapping
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return _to_float(cur)


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        parsed = _to_float(value)
        if parsed is not None:
            return parsed
    return None


def _settlement_actual_intraday_deformation_lock_reason(
    features: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    rounding_rule: Optional[str],
) -> str:
    if not _to_bool(os.environ.get("PM_BAYES_BLOCK_DEFORMED_WU_INTRADAY_SETTLEMENT_LOCK"), default=True):
        return ""
    source = str(candidate.get("source") or "").strip().lower()
    if not _is_settlement_actual_source(source):
        return ""
    comparison = _mapping_from_feature(features, "source_observation_comparison")
    if not comparison:
        return ""
    alert = _mapping_from_feature(features, "weather_source_delay_deformation_alert")
    alert_tokens = _flatten_feature_tokens(alert)
    alert_text = " ".join(alert_tokens)
    alert_on = bool(
        _to_bool(alert.get("alert"), default=False)
        and (
            "wu" in alert_text
            or "twc" in alert_text
            or "temperature_deformation" in alert_text
            or "temperature_mismatch" in alert_text
            or "delay" in alert_text
        )
    )
    if not alert_on:
        return ""
    settlement_bucket = _settlement_bucket_for_policy(candidate.get("value_c"), rounding_rule)
    awc = comparison.get("awc") if isinstance(comparison.get("awc"), Mapping) else {}
    wu_twc = comparison.get("wu_twc") if isinstance(comparison.get("wu_twc"), Mapping) else {}
    awc_value = _nested_float(awc, ("raw_observations", "today_max_metar", "temperature_raw_value"))
    if awc_value is None:
        awc_value = _first_float(awc.get("today_max_c"), awc.get("bucket_basis_value_c"))
    awc_bucket = _settlement_bucket_for_policy(awc_value, rounding_rule)
    wu_current_value = _nested_float(wu_twc, ("raw_observations", "current_metric", "max_since7am_raw_value"))
    if wu_current_value is None:
        wu_current_value = _first_float(wu_twc.get("today_max_c"), wu_twc.get("bucket_basis_value_c"))
    wu_current_bucket = _settlement_bucket_for_policy(wu_current_value, rounding_rule)
    wu_history_value = _nested_float(wu_twc, ("raw_observations", "history_metric", "raw_value"))
    wu_history_bucket = _settlement_bucket_for_policy(wu_history_value, rounding_rule)
    if (
        settlement_bucket is None
        or awc_bucket is None
        or wu_current_bucket is None
        or int(settlement_bucket) == int(awc_bucket)
    ):
        return ""
    if int(settlement_bucket) != int(wu_current_bucket):
        return ""
    if wu_history_bucket is not None and int(wu_history_bucket) != int(awc_bucket):
        return ""
    return "wu_twc_intraday_deformation_not_final_settlement"


def _hard_source_candidate_dt(candidate: Mapping[str, Any]) -> Optional[datetime]:
    for key in ("value_at", "latest_at", "fetched_at"):
        dt = _parse_iso_datetime(candidate.get(key))
        if dt is not None:
            return dt
    return None


def _hard_source_posterior_override_candidates(
    features: Mapping[str, Any],
    *,
    rounding_rule: Optional[str] = None,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    settlement_value = _feature_float(features, "settlement_actual", "settlement_actual_c")
    if settlement_value is not None:
        candidates.append(
            {
                "source": str(features.get("settlement_actual_source") or "settlement_actual"),
                "value_c": float(settlement_value),
                "value_at": _feature_text(features, "settlement_actual_at", "settlement_final_at", "observed_today_max_at"),
                "latest_at": _feature_text(features, "settlement_actual_at", "settlement_final_at"),
                "fetched_at": _feature_text(features, "settlement_actual_fetched_at"),
                "event_date": _feature_date_text(features, "settlement_actual_event_date", "event_date", "run_date"),
                "priority": 1000,
            }
        )
    awc_mismatch = _feature_bool(features, "zuuu_metar_raw_awc_date_mismatch")
    awc_value = _feature_float(features, "zuuu_metar_raw_awc_today_max_c")
    if awc_value is None:
        source_text = _normalize_text(features.get("source"))
        observed_source = _normalize_text(features.get("observed_today_max_source"))
        if (
            "zuuu_metar_raw_awc" in source_text
            or "zuuu_metar_raw_awc" in observed_source
        ) and _feature_bool(features, "observed_today_max_trusted"):
            awc_value = _feature_float(features, "observed_today_max_c")
    if awc_value is not None and not awc_mismatch:
        candidates.append(
            {
                "source": "zuuu_metar_raw_awc",
                "value_c": float(awc_value),
                "value_at": _feature_text(features, "zuuu_metar_raw_awc_today_max_at", "zuuu_metar_raw_awc_latest_at"),
                "latest_at": _feature_text(features, "zuuu_metar_raw_awc_latest_at"),
                "fetched_at": _feature_text(features, "zuuu_metar_raw_awc_fetched_at"),
                "event_date": _feature_date_text(features, "zuuu_metar_raw_awc_target_event_date", "event_date", "run_date"),
                "priority": 20,
            }
        )
    wu_twc_demote_reason = _wu_twc_intraday_hard_source_demotion_reason(features)
    if not _feature_bool(features, "wu_twc_observed_intraday_stale") and not wu_twc_demote_reason:
        wu_today_max = _feature_float(features, "wu_twc_observed_intraday_today_max_c")
        wu_current = _feature_float(features, "wu_twc_observed_intraday_current_temp_c")
        wu_value = None
        wu_value_at = ""
        if wu_today_max is not None and wu_current is not None:
            if float(wu_today_max) >= float(wu_current):
                wu_value = float(wu_today_max)
                wu_value_at = _feature_text(
                    features,
                    "wu_twc_observed_intraday_today_max_at",
                    "wu_twc_observed_intraday_current_at",
                )
            else:
                wu_value = float(wu_current)
                wu_value_at = _feature_text(
                    features,
                    "wu_twc_observed_intraday_current_at",
                    "wu_twc_observed_intraday_today_max_at",
                )
        elif wu_today_max is not None:
            wu_value = float(wu_today_max)
            wu_value_at = _feature_text(
                features,
                "wu_twc_observed_intraday_today_max_at",
                "wu_twc_observed_intraday_current_at",
            )
        elif wu_current is not None:
            wu_value = float(wu_current)
            wu_value_at = _feature_text(
                features,
                "wu_twc_observed_intraday_current_at",
                "wu_twc_observed_intraday_today_max_at",
            )
        if wu_value is not None:
            candidates.append(
                {
                    "source": "wu_twc_observed_intraday",
                    "value_c": float(wu_value),
                    "value_at": wu_value_at,
                    "latest_at": _feature_text(
                        features,
                        "wu_twc_observed_intraday_current_at",
                        "wu_twc_observed_intraday_today_max_at",
                    ),
                    "fetched_at": _feature_text(features, "wu_twc_observed_intraday_fetched_at"),
                    "event_date": _feature_date_text(
                        features,
                        "wu_twc_observed_intraday_target_event_date",
                        "event_date",
                        "run_date",
                    ),
                    "priority": 18,
                }
            )
    observed_source = _normalize_text(features.get("observed_today_max_source"))
    station_value = _feature_float(features, "observed_today_max_c")
    station_value_at = _feature_text(
        features,
        "station_observation_observed_at",
        "observed_today_max_at",
        "station_observation_created_at",
    )
    station_fetched_at = _feature_text(
        features,
        "station_observation_created_at",
        "weather_likelihood_refreshed_at",
    )
    if (
        station_value is not None
        and observed_source.startswith("station_observations.")
        and "pws" not in observed_source
        and _feature_bool(features, "observed_today_max_trusted", "station_observation_triggered")
    ):
        candidates.append(
            {
                "source": str(features.get("observed_today_max_source") or "station_observations"),
                "value_c": float(station_value),
                "value_at": station_value_at,
                "latest_at": station_value_at,
                "fetched_at": station_fetched_at,
                "event_date": _feature_date_text(features, "event_date", "run_date"),
                "priority": 12,
            }
        )

    for candidate in candidates:
        profile = _hard_source_candidate_boundary_profile(
            features,
            candidate,
            rounding_rule=rounding_rule,
        )
        if profile:
            candidate["settlement_precision"] = profile
            candidate["boundary_ambiguous"] = bool(profile.get("boundary_ambiguous"))
            if profile.get("ambiguous_reason"):
                candidate["boundary_ambiguous_reason"] = str(profile.get("ambiguous_reason") or "")
    return candidates


def _hard_source_override_window_state(
    features: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    event_tz: Optional[str] = None,
    event_date: Optional[str] = None,
    now_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    zone_name = (
        str(event_tz or "").strip()
        or _feature_text(features, "event_tz", "tz", "timezone")
        or "Asia/Shanghai"
    )
    zone = _timezone_for_name(zone_name)
    event_key = (
        _normalize_date_key(event_date)
        or _feature_date_text(features, "event_date", "target_event_date", "run_date")
        or str(candidate.get("event_date") or "").strip()
    )
    decision_dt = _hard_source_window_local_dt_override(
        zone=zone,
        event_key=event_key,
    ) or _hard_source_candidate_dt(candidate)
    if decision_dt is None:
        decision_dt = now_dt if isinstance(now_dt, datetime) else datetime.now(timezone.utc)
        if decision_dt.tzinfo is None:
            decision_dt = decision_dt.replace(tzinfo=timezone.utc)
    local_dt = decision_dt.astimezone(zone)
    local_hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0
    candidate_event_key = str(candidate.get("event_date") or "").strip()
    if event_key and candidate_event_key and event_key != candidate_event_key:
        return {
            "allowed": False,
            "reason": "candidate_event_date_mismatch",
            "event_date": event_key,
            "candidate_event_date": candidate_event_key,
            "local_time": local_dt.isoformat(),
            "timezone": zone_name,
        }
    if event_key and local_dt.date().isoformat() != event_key:
        return {
            "allowed": False,
            "reason": "outside_event_local_date",
            "event_date": event_key,
            "local_date": local_dt.date().isoformat(),
            "local_time": local_dt.isoformat(),
            "timezone": zone_name,
        }
    mode = str(os.environ.get("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_MODE") or "wu_forecast").strip().lower()
    peak_hour = None
    peak_meta: Dict[str, Any] = {}
    lead = None
    post = None
    if mode == "fixed":
        start_hour = _env_float(
            "PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_START_LOCAL_HOUR",
            _env_float("PM_HARD_SOURCE_DIRECT_DECISION_START_LOCAL_HOUR", 14.0),
        )
        end_hour = _env_float(
            "PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_END_LOCAL_HOUR",
            _env_float("PM_HARD_SOURCE_DIRECT_DECISION_END_LOCAL_HOUR", 17.5),
        )
    else:
        if mode in {"wu_forecast", "wu", "twc", "weather_com"}:
            peak_hour, peak_meta = _wu_forecast_peak_hour_from_features(
                features,
                zone=zone,
                event_date=event_key,
            )
        if peak_hour is None:
            source = str(peak_meta.get("expected_peak_hour_source") or "wu_forecast_peak_time_missing")
            return {
                "allowed": False,
                "reason": "wu_forecast_peak_time_missing",
                "mode": mode or "wu_forecast",
                "event_date": event_key,
                "local_time": local_dt.isoformat(),
                "local_hour": float(local_hour),
                "timezone": zone_name,
                "peak_hour_local": None,
                "peak_hour_source": source,
                "peak_at_local": "",
                "start_hour_local": None,
                "end_hour_local": None,
                "lead_hours": None,
                "post_hours": None,
            }
        lead = _env_float(
            "PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_PEAK_LEAD_HOURS",
            _env_float("PM_HARD_SOURCE_DIRECT_PEAK_LEAD_HOURS", 1.0),
        )
        post = _env_float(
            "PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_PEAK_POST_HOURS",
            _env_float("PM_HARD_SOURCE_DIRECT_PEAK_POST_HOURS", 1.5),
        )
        peak_window_start = _to_float(peak_meta.get("expected_peak_window_start_hour_local"))
        peak_window_end = _to_float(peak_meta.get("expected_peak_window_end_hour_local"))
        if peak_window_start is not None and peak_window_end is not None:
            start_hour = max(0.0, float(peak_window_start) - max(0.0, float(lead)))
            end_hour = min(23.99, float(peak_window_end) + max(0.0, float(post)))
        else:
            start_hour = max(0.0, float(peak_hour) - max(0.0, float(lead)))
            end_hour = min(23.99, float(peak_hour) + max(0.0, float(post)))
    allowed = start_hour <= float(local_hour) <= end_hour
    return {
        "allowed": bool(allowed),
        "reason": "inside_peak_window" if allowed else "outside_peak_window",
        "mode": mode or "wu_forecast",
        "event_date": event_key,
        "local_time": local_dt.isoformat(),
        "local_hour": float(local_hour),
        "timezone": zone_name,
        "peak_hour_local": float(peak_hour) if peak_hour is not None else None,
        "peak_hour_source": str(peak_meta.get("expected_peak_hour_source") or ""),
        "peak_at_local": str(peak_meta.get("expected_peak_at_local") or ""),
        "peak_window_start_hour_local": peak_meta.get("expected_peak_window_start_hour_local"),
        "peak_window_end_hour_local": peak_meta.get("expected_peak_window_end_hour_local"),
        "peak_window_source": str(peak_meta.get("expected_peak_window_source") or ""),
        "start_hour_local": float(start_hour),
        "end_hour_local": float(end_hour),
        "lead_hours": float(lead) if lead is not None else None,
        "post_hours": float(post) if post is not None else None,
    }


def _hard_source_final_lock_state(
    features: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> Dict[str, Any]:
    source = str(candidate.get("source") or "").strip()
    source_key = source.lower()
    if _is_settlement_actual_source(source_key) and _feature_float(features, "settlement_actual", "settlement_actual_c") is not None:
        demotion_reason = _settlement_actual_intraday_deformation_lock_reason(
            features,
            candidate,
            rounding_rule=None,
        )
        if demotion_reason:
            return {"final_lock": False, "reason": demotion_reason}
        return {"final_lock": True, "reason": "settlement_actual_known"}
    source_final_keys: List[str] = []
    if source:
        source_final_keys.extend(
            [
                f"{source}_final_daily_max",
                f"{source}_settlement_final",
                f"{source}_no_more_warming_possible",
            ]
        )
    if _feature_bool(
        features,
        "hard_source_final_daily_max",
        "hard_source_settlement_final",
        "observed_today_max_final",
        "temperature_observation_final_daily_max",
        "no_more_warming_possible",
        *source_final_keys,
    ):
        return {"final_lock": True, "reason": "hard_source_final_flag"}
    return {"final_lock": False, "reason": "hard_source_lower_bound_only"}


def _hard_source_candidate_bypasses_peak_window(candidate: Mapping[str, Any]) -> bool:
    source = str(candidate.get("source") or "").strip().lower()
    if not source:
        return False
    if source in {
        "zuuu_metar_raw_awc",
        "awc_raw_metar",
        "raw_metar",
        "wu_twc_observed_intraday",
        "station_observations",
    }:
        return True
    return (
        ("metar" in source or "awc" in source)
        and "forecast" not in source
    )


def _hard_source_constrained_probs(
    labels: List[str],
    *,
    target_label: str,
    target_index: int,
    base_probs: Optional[List[float]],
    rounding_rule: Optional[str],
    final_lock: bool,
) -> Tuple[List[float], List[bool], str]:
    if final_lock:
        probs = [0.0 for _ in labels]
        if 0 <= int(target_index) < len(probs):
            probs[int(target_index)] = 1.0
        return probs, [idx == int(target_index) for idx in range(len(labels))], "final_bucket_lock"

    target_bounds = _temperature_bucket_bounds(target_label, rounding_rule)
    if target_bounds is None:
        eligible = [idx >= int(target_index) for idx in range(len(labels))]
    else:
        target_low = float(target_bounds[0])
        eligible = []
        for label in labels:
            bounds = _temperature_bucket_bounds(label, rounding_rule)
            if bounds is None:
                eligible.append(False)
                continue
            _low, high = bounds
            eligible.append(not (math.isfinite(float(high)) and float(high) <= target_low + 1e-9))

    if not any(eligible) and 0 <= int(target_index) < len(labels):
        eligible[int(target_index)] = True

    source_probs = list(base_probs or [])
    if len(source_probs) != len(labels):
        source_probs = [1.0 if idx == int(target_index) else 0.0 for idx in range(len(labels))]
    constrained = [
        max(0.0, float(source_probs[idx])) if eligible[idx] and math.isfinite(float(source_probs[idx])) else 0.0
        for idx in range(len(labels))
    ]
    if sum(constrained) <= EPS and 0 <= int(target_index) < len(constrained):
        constrained[int(target_index)] = 1.0
    return _normalize(constrained), eligible, "hard_source_lower_bound_constraint"


def _normalize_date_key(value: Any) -> str:
    raw = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    parsed = _parse_iso_datetime(raw)
    return parsed.date().isoformat() if parsed is not None else ""


def _bucket_width_for_selection(label: str, rounding_rule: Optional[str]) -> float:
    bounds = _temperature_bucket_bounds(label, rounding_rule)
    if bounds is None:
        return float("inf")
    low, high = bounds
    if math.isfinite(low) and math.isfinite(high):
        return max(0.0, float(high) - float(low))
    return float("inf")


def resolve_hard_source_posterior_override(
    labels: List[str],
    features: Mapping[str, Any],
    *,
    base_probs: Optional[List[float]] = None,
    rounding_rule: Optional[str] = None,
    market_var: Optional[str] = None,
    event_tz: Optional[str] = None,
    event_date: Optional[str] = None,
    now_dt: Optional[datetime] = None,
    ignore_window: bool = False,
) -> Dict[str, Any]:
    if not _to_bool(os.environ.get("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"), default=True):
        return {"enabled": False, "applied": False, "status": "disabled"}
    if str(market_var or "temperature_2m_max").strip() and not str(market_var or "temperature_2m_max").startswith("temperature"):
        return {"enabled": True, "applied": False, "status": "non_temperature_market"}
    if not labels:
        return {"enabled": True, "applied": False, "status": "missing_labels"}
    candidates_all = _hard_source_posterior_override_candidates(
        features if isinstance(features, Mapping) else {},
        rounding_rule=rounding_rule,
    )
    blocked_boundary_candidates = [
        cand for cand in candidates_all if bool(cand.get("boundary_ambiguous"))
    ]
    candidates = [
        cand for cand in candidates_all if not bool(cand.get("boundary_ambiguous"))
    ]
    if not candidates:
        if blocked_boundary_candidates:
            blocked = blocked_boundary_candidates[0]
            return {
                "enabled": True,
                "applied": False,
                "status": "hard_source_boundary_ambiguous",
                "source": blocked.get("source"),
                "value_c": blocked.get("value_c"),
                "settlement_precision": blocked.get("settlement_precision"),
                "boundary_ambiguous": True,
                "boundary_ambiguous_reason": blocked.get("boundary_ambiguous_reason") or "boundary_ambiguous",
                "candidate_count": len(candidates_all),
                "blocked_candidate_count": len(blocked_boundary_candidates),
            }
        return {"enabled": True, "applied": False, "status": "hard_source_missing"}
    candidates.sort(
        key=lambda cand: (
            float(cand.get("value_c") or float("-inf")),
            int(cand.get("priority") or 0),
            (_hard_source_candidate_dt(cand) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        ),
        reverse=True,
    )
    candidate = candidates[0]
    window_enabled = _to_bool(
        os.environ.get("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"),
        default=True,
    )
    window = _hard_source_override_window_state(
        features,
        candidate,
        event_tz=event_tz,
        event_date=event_date,
        now_dt=now_dt,
    )
    value_c = _to_float(candidate.get("value_c"))
    if value_c is None:
        return {"enabled": True, "applied": False, "status": "hard_source_value_invalid"}
    hits: List[Tuple[float, int, str]] = []
    for idx, label in enumerate(labels):
        hit = _bucket_hit(float(value_c), label, rounding_rule)
        if hit is True:
            hits.append((_bucket_width_for_selection(label, rounding_rule), idx, label))
    if not hits:
        return {
            "enabled": True,
            "applied": False,
            "status": "hard_source_bucket_not_listed",
            "source": candidate.get("source"),
            "value_c": float(value_c),
            "window_enabled": bool(window_enabled),
            "window": window,
            "candidate_count": len(candidates_all),
            "blocked_candidate_count": len(blocked_boundary_candidates),
        }
    hits.sort(key=lambda item: (item[0], item[1]))
    _width, target_idx, target_label = hits[0]
    final_lock_state = _hard_source_final_lock_state(features, candidate)
    window_bypassed_by_strong_observation = _hard_source_candidate_bypasses_peak_window(candidate)
    if (
        window_enabled
        and not bool(ignore_window)
        and not bool(window_bypassed_by_strong_observation)
        and not bool(final_lock_state.get("final_lock"))
        and not bool(window.get("allowed"))
    ):
        return {
            "enabled": True,
            "applied": False,
            "status": str(window.get("reason") or "outside_peak_window"),
            "source": candidate.get("source"),
            "value_c": candidate.get("value_c"),
            "window_enabled": True,
            "window": window,
            "candidate_count": len(candidates_all),
            "blocked_candidate_count": len(blocked_boundary_candidates),
        }
    probs, eligible_mask, constraint_mode = _hard_source_constrained_probs(
        labels,
        target_label=target_label,
        target_index=target_idx,
        base_probs=base_probs,
        rounding_rule=rounding_rule,
        final_lock=bool(final_lock_state.get("final_lock")),
    )
    return {
        "enabled": True,
        "applied": True,
        "status": "applied",
        "source": str(candidate.get("source") or ""),
        "value_c": float(value_c),
        "value_at": str(candidate.get("value_at") or ""),
        "latest_at": str(candidate.get("latest_at") or ""),
        "fetched_at": str(candidate.get("fetched_at") or ""),
        "target_bucket": target_label,
        "target_index": int(target_idx),
        "posterior_probs": probs,
        "eligible_mask": eligible_mask,
        "constraint_mode": constraint_mode,
        "final_bucket_lock": bool(final_lock_state.get("final_lock")),
        "final_lock_reason": str(final_lock_state.get("reason") or ""),
        "window_enabled": bool(window_enabled),
        "window_ignored": bool(ignore_window),
        "window_bypassed_by_strong_observation": bool(window_bypassed_by_strong_observation),
        "window": window,
        "candidate_count": len(candidates_all),
        "blocked_candidate_count": len(blocked_boundary_candidates),
        "settlement_precision": candidate.get("settlement_precision"),
        "boundary_ambiguous": False,
        "decision_basis": constraint_mode,
        "target_adjustment_action": "HARD_SOURCE_BUCKET_OVERRIDE",
    }


def _set_row_probability_fields_for_hard_source_override(
    row: Dict[str, Any],
    *,
    probability: float,
    meta: Mapping[str, Any],
    label: str,
    eligible: bool = True,
) -> None:
    p = max(0.0, min(1.0, float(probability)))
    no_p = 1.0 - p
    prior_q = _clamp01(
        _to_float(
            row.get("q_bayes_posterior")
            if row.get("q_bayes_posterior") is not None
            else row.get("q_raw")
            if row.get("q_raw") is not None
            else row.get("q_round")
        )
    )
    if prior_q is not None:
        row.setdefault("q_bayes_posterior_pre_hard_source_override", float(prior_q))
        row.setdefault("q_raw_pre_hard_source_override", float(prior_q))
    prior_atomic = _clamp01(_to_float(row.get("q_temperature_bucket_posterior")))
    if prior_atomic is not None:
        row.setdefault("q_temperature_bucket_posterior_pre_hard_source_override", float(prior_atomic))
    if row.get("q_raw_static") is None and row.get("q_raw") is not None:
        row["q_raw_static"] = row.get("q_raw")
    if row.get("q_round_static") is None and row.get("q_round") is not None:
        row["q_round_static"] = row.get("q_round")
    for key in (
        "q_raw",
        "q_round",
        "q_bayes_posterior",
        "q_bayes_lcb",
        "q_bayes_ucb",
        "q_entry",
        "q_calibrated",
        "q_calibrated_lcb",
        "q_calibrated_ucb",
        "q_quote",
        "q_quote_lcb",
        "q_quote_ucb",
        "q_temperature_bucket_posterior",
        "q_contract_payout_posterior",
    ):
        row[key] = p
    for key in (
        "q_raw_no",
        "q_round_no",
        "q_bayes_posterior_no",
        "q_bayes_lcb_no",
        "q_bayes_ucb_no",
        "q_entry_no",
        "q_calibrated_no",
        "q_calibrated_lcb_no",
        "q_calibrated_ucb_no",
        "q_quote_no",
        "q_quote_lcb_no",
        "q_quote_ucb_no",
    ):
        row[key] = no_p
    for key in ("q_bayes_sigma", "q_bayes_sigma_no", "q_calibrated_sigma", "q_quote_sigma", "q_quote_sigma_no"):
        row[key] = 0.0
    row["q_bayes_enabled"] = True
    row["q_bayes_source"] = f"{CTP_VERSION}_hard_source_override"
    row["q_calibration_source"] = "hard_source_posterior_override"
    row["q_quote_source"] = "hard_source_posterior_override"
    row["q_quote_source_no"] = "hard_source_posterior_override"
    row["q_entry_mode"] = "hard_source_posterior_override"
    row["q_temperature_bucket_probability_mode"] = "hard_source_posterior_override"
    row["q_temperature_bucket_label"] = label
    row["hard_source_posterior_override"] = True
    row["hard_source_posterior_override_applied"] = True
    row["hard_source_posterior_override_source"] = meta.get("source")
    row["hard_source_posterior_override_value_c"] = meta.get("value_c")
    row["hard_source_posterior_override_target_bucket"] = meta.get("target_bucket")
    row["hard_source_posterior_override_status"] = meta.get("status")
    row["hard_source_posterior_override_window"] = meta.get("window")
    row["hard_source_posterior_override_constraint_mode"] = meta.get("constraint_mode")
    row["hard_source_posterior_override_final_bucket_lock"] = bool(meta.get("final_bucket_lock"))
    row["hard_source_posterior_override_final_lock_reason"] = meta.get("final_lock_reason")
    row["target_adjustment_action"] = meta.get("target_adjustment_action")
    row["target_adjustment_reason_codes"] = "hard_source_posterior_override"
    if not bool(eligible) and p <= EPS:
        hard_zero_reasons = [
            "observed_today_max_above_bucket",
            str(meta.get("constraint_mode") or "hard_source_lower_bound_constraint"),
        ]
        if bool(meta.get("final_bucket_lock")):
            hard_zero_reasons.append("hard_source_final_bucket_lock")
        row["q_posterior_observed_constraints_applied"] = True
        row["q_posterior_observed_constraints_source"] = "hard_source_posterior_override"
        if prior_q is not None:
            row["q_posterior_observed_constraints_pre_prob"] = float(prior_q)
        row["q_posterior_observed_constraints_post_prob"] = 0.0
        row["q_posterior_observed_constraints_hard_zero"] = True
        row["q_posterior_observed_constraints_reason_codes"] = list(hard_zero_reasons)
        row["q_posterior_observed_constraints_hard_zero_reasons"] = list(hard_zero_reasons)
        row["posterior_settlement_candidate_bucket_hard_zero"] = True
        row["posterior_settlement_candidate_bucket_hard_zero_reasons"] = list(hard_zero_reasons)
        row["hard_reasons"] = list(hard_zero_reasons)
        row["edge_min_exit_yes"] = True
        row["edge_min_exit_reasons_yes"] = ["硬源已突破该温度桶，YES 不可兑现"]
        row["edge_min_exit_codes_yes"] = ["target_realization_exit_clear"]
        row["target_adjustment_force_exit"] = True
        row["target_adjustment_exit_reason_code"] = "target_realization_exit_clear"
        row["target_adjustment_exit_reason"] = "硬源已突破该温度桶，YES 不可兑现"
    else:
        row["q_posterior_observed_constraints_applied"] = True
        row["q_posterior_observed_constraints_source"] = "hard_source_posterior_override"
        if prior_q is not None:
            row["q_posterior_observed_constraints_pre_prob"] = float(prior_q)
        row["q_posterior_observed_constraints_post_prob"] = p
        row["q_posterior_observed_constraints_hard_zero"] = False
        row["q_posterior_observed_constraints_reason_codes"] = []
        row["q_posterior_observed_constraints_hard_zero_reasons"] = []
        row["posterior_settlement_candidate_bucket_hard_zero"] = False
        row["posterior_settlement_candidate_bucket_hard_zero_reasons"] = []
        stale_hard_reasons = row.get("hard_reasons")
        if isinstance(stale_hard_reasons, list) and set(str(reason) for reason in stale_hard_reasons).issubset(
            {
                "observed_today_max_above_bucket",
                "hard_source_lower_bound_constraint",
                "final_bucket_lock",
                "hard_source_final_bucket_lock",
            }
        ):
            row["hard_reasons"] = []
        row["edge_min_exit_yes"] = False
        row["edge_min_exit_reasons_yes"] = []
        row["edge_min_exit_codes_yes"] = []
        row["target_adjustment_force_exit"] = False
        row.pop("target_adjustment_exit_reason_code", None)
        row.pop("target_adjustment_exit_reason", None)


def apply_hard_source_posterior_override_to_distribution(
    rows: List[Dict[str, Any]],
    features: Mapping[str, Any],
    *,
    base_probs: Optional[List[float]] = None,
    market_var: Optional[str] = None,
    rounding_rule: Optional[str] = None,
    event_tz: Optional[str] = None,
    event_date: Optional[str] = None,
    now_dt: Optional[datetime] = None,
    ignore_window: bool = False,
) -> Dict[str, Any]:
    labels = [
        str(row.get("outcome") or row.get("label") or "")
        for row in rows
        if isinstance(row, Mapping)
    ]
    meta = resolve_hard_source_posterior_override(
        labels,
        features if isinstance(features, Mapping) else {},
        base_probs=base_probs,
        rounding_rule=rounding_rule,
        market_var=market_var,
        event_tz=event_tz,
        event_date=event_date,
        now_dt=now_dt,
        ignore_window=ignore_window,
    )
    if not bool(meta.get("applied")):
        return meta
    probs = meta.get("posterior_probs") if isinstance(meta.get("posterior_probs"), list) else []
    eligible_mask = meta.get("eligible_mask") if isinstance(meta.get("eligible_mask"), list) else []
    idx = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("outcome") or row.get("label") or "")
        prob = float(probs[idx]) if idx < len(probs) else 0.0
        eligible = bool(eligible_mask[idx]) if idx < len(eligible_mask) else prob > EPS
        _set_row_probability_fields_for_hard_source_override(
            row,
            probability=prob,
            meta=meta,
            label=label,
            eligible=eligible,
        )
        idx += 1
    return meta


def _bucket_center_for_explanation(label: str, rounding_rule: Optional[str]) -> Optional[float]:
    bounds = _temperature_bucket_bounds(label, rounding_rule)
    if bounds is None:
        return None
    low, high = bounds
    if math.isfinite(low) and math.isfinite(high):
        return (float(low) + float(high)) / 2.0
    if math.isfinite(low):
        return float(low) + 0.5
    if math.isfinite(high):
        return float(high) - 0.5
    return None


def _weighted_bucket_mean(
    labels: List[str],
    probs: List[float],
    rounding_rule: Optional[str],
) -> Optional[float]:
    total = 0.0
    weighted = 0.0
    for label, prob in zip(labels, probs):
        center = _bucket_center_for_explanation(label, rounding_rule)
        p = _to_float(prob)
        if center is None or p is None or p <= 0:
            continue
        weighted += float(center) * float(p)
        total += float(p)
    if total <= EPS:
        return None
    return weighted / total


def _d0_physical_time_weight(features: Mapping[str, Any]) -> Dict[str, Any]:
    hour = _feature_float(features, "observation_hour_local", "hour_local", "local_hour")
    if hour is None:
        return {"window": "unknown", "weight": 0.55, "hour_local": None}
    h = float(hour)
    if h < 10.0:
        window, weight = "before_10", 0.35
    elif h < 11.0:
        window, weight = "10", 0.45
    elif h < 12.0:
        window, weight = "11", 0.58
    elif h < 13.0:
        window, weight = "12", 0.72
    elif h < 14.0:
        window, weight = "13", 0.88
    elif h < 15.0:
        window, weight = "14", 1.00
    else:
        window, weight = "15_after", 1.12
    return {"window": window, "weight": weight, "hour_local": h}


def _first_feature_number(
    features: Mapping[str, Any],
    keys: List[str],
) -> Tuple[Optional[float], str]:
    for key in keys:
        val = _feature_float(features, key)
        if val is not None:
            return float(val), key
    return None, ""


def _first_feature_bool(
    features: Mapping[str, Any],
    keys: List[str],
) -> Tuple[Optional[bool], str]:
    for key in keys:
        if key not in features:
            continue
        val = features.get(key)
        if isinstance(val, bool):
            return bool(val), key
        text = str(val or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True, key
        if text in {"0", "false", "no", "off"}:
            return False, key
    return None, ""


def _group_log_likelihoods(
    rows: List[Any],
    group_key: str,
    count: int,
) -> Tuple[List[float], bool, str, Optional[float]]:
    out: List[float] = []
    available = False
    na_reason = ""
    weight: Optional[float] = None
    for idx in range(count):
        row = rows[idx] if idx < len(rows) and isinstance(rows[idx], Mapping) else {}
        groups = row.get("component_groups") if isinstance(row, Mapping) else None
        group = groups.get(group_key) if isinstance(groups, Mapping) else None
        if isinstance(group, Mapping):
            if bool(group.get("available")):
                available = True
            if not na_reason:
                na_reason = str(group.get("na_reason") or group.get(f"{group_key}_na_reason") or "")
            if weight is None:
                weight = _to_float(group.get("weight"))
            ll = _to_float(group.get("log_likelihood"))
            out.append(float(ll) if ll is not None else 0.0)
        else:
            out.append(0.0)
    return out, available, na_reason, weight


def _probability_shift_from_group(
    labels: List[str],
    base_probs: List[float],
    group_ll: List[float],
    *,
    rounding_rule: Optional[str],
    time_weight: float,
) -> Dict[str, Any]:
    if len(labels) != len(base_probs) or len(labels) != len(group_ll) or not labels:
        return {"warming_pp": 0.0, "cooling_pp": 0.0, "net_pp": 0.0, "target_buckets": []}
    ref_mean = _weighted_bucket_mean(labels, base_probs, rounding_rule)
    if ref_mean is None:
        centers = [
            _bucket_center_for_explanation(label, rounding_rule)
            for label in labels
        ]
        finite_centers = [float(x) for x in centers if x is not None]
        ref_mean = sum(finite_centers) / len(finite_centers) if finite_centers else 0.0
    scaled_ll = [float(v) * max(0.0, float(time_weight)) for v in group_ll]
    max_ll = max(scaled_ll) if scaled_ll else 0.0
    group_shape = _normalize([math.exp(max(-40.0, min(40.0, v - max_ll))) for v in scaled_ll])
    shifted = _normalize([
        max(EPS, float(base_probs[idx])) * max(EPS, group_shape[idx])
        for idx in range(len(labels))
    ])
    deltas = [shifted[idx] - float(base_probs[idx]) for idx in range(len(labels))]
    warming = 0.0
    cooling = 0.0
    bucket_impacts: List[Dict[str, Any]] = []
    for idx, label in enumerate(labels):
        center = _bucket_center_for_explanation(label, rounding_rule)
        if center is None:
            continue
        delta = deltas[idx]
        if center > ref_mean + 0.05 and delta > 0:
            warming += delta
        if center < ref_mean - 0.05 and delta > 0:
            cooling += delta
        bucket_impacts.append(
            {
                "label": label,
                "center_c": float(center),
                "delta_pp": float(delta * 100.0),
                "side": "higher" if center > ref_mean + 0.05 else ("lower" if center < ref_mean - 0.05 else "center"),
            }
        )
    net = warming - cooling
    if abs(net) < 0.0005:
        target = sorted(bucket_impacts, key=lambda item: abs(float(item["delta_pp"])), reverse=True)
    elif net > 0:
        target = sorted(
            [item for item in bucket_impacts if item["side"] == "higher" and item["delta_pp"] > 0],
            key=lambda item: float(item["delta_pp"]),
            reverse=True,
        )
    else:
        target = sorted(
            [item for item in bucket_impacts if item["side"] == "lower" and item["delta_pp"] > 0],
            key=lambda item: float(item["delta_pp"]),
            reverse=True,
        )
    return {
        "warming_pp": float(warming * 100.0),
        "cooling_pp": float(cooling * 100.0),
        "net_pp": float(net * 100.0),
        "reference_mean_bucket_c": float(ref_mean),
        "target_buckets": [str(item["label"]) for item in target[:3]],
        "bucket_impacts": bucket_impacts,
    }


def _compact_physical_likelihood_components(value: Any, *, max_components: int = 12) -> Dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, float] = {}
    for key, raw_value in value.items():
        if len(out) >= max_components:
            break
        name = str(key or "").strip()
        if not name:
            continue
        num = _to_float(raw_value)
        if num is None and isinstance(raw_value, Mapping):
            num = _to_float(raw_value.get("log_likelihood"))
        if num is None:
            continue
        out[name] = float(num)
    return out


def _compact_physical_likelihood_group_components(
    value: Any,
    *,
    max_components: int = 6,
) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, raw_value in value.items():
        if len(out) >= max_components:
            break
        name = str(key or "").strip()
        if not name or not isinstance(raw_value, Mapping):
            continue
        entry: Dict[str, Any] = {}
        for num_key in ("factor", "weight", "log_likelihood"):
            num = _to_float(raw_value.get(num_key))
            if num is not None:
                entry[num_key] = float(num)
        if entry:
            out[name] = entry
    return out


def _compact_physical_likelihood_component_groups(
    value: Any,
    *,
    max_groups: int = 10,
) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, raw_group in value.items():
        if len(out) >= max_groups:
            break
        group_key = str(key or "").strip()
        if not group_key or not isinstance(raw_group, Mapping):
            continue
        group: Dict[str, Any] = {}
        if "available" in raw_group:
            group["available"] = bool(raw_group.get("available"))
        for text_key in ("na_reason", f"{group_key}_na_reason", "evidence_state"):
            text = str(raw_group.get(text_key) or "").strip()
            if text:
                group[text_key] = text[:160]
        for num_key in ("weight", "log_likelihood"):
            num = _to_float(raw_group.get(num_key))
            if num is not None:
                group[num_key] = float(num)
        source_profile = raw_group.get("source_profile")
        if isinstance(source_profile, Mapping):
            compact_source: Dict[str, Any] = {}
            for text_key in ("evidence_state", "source_class", "source_field", "source", "na_reason"):
                text = str(source_profile.get(text_key) or "").strip()
                if text:
                    compact_source[text_key] = text[:160]
            for bool_key in ("precip_present", "no_precip_confirmed", "is_real_radar_or_metar", "is_proxy_only"):
                if bool_key in source_profile:
                    compact_source[bool_key] = bool(source_profile.get(bool_key))
            rate = _to_float(source_profile.get("rate_mm_per_h"))
            if rate is not None:
                compact_source["rate_mm_per_h"] = float(rate)
            if compact_source:
                group["source_profile"] = compact_source
        components = _compact_physical_likelihood_group_components(raw_group.get("components"))
        if components:
            group["components"] = components
        if group:
            out[group_key] = group
    return out


def _target_bucket_text(buckets: List[str], direction: str) -> str:
    labels = [
        str(x).replace("°C", "").replace("°F", "").strip()
        for x in buckets
        if str(x).strip() and not re.search(r"\bor\s+(?:below|higher|above|more)\b", str(x), re.IGNORECASE)
    ]
    if not labels:
        return ""
    action = "上调" if direction in {"warming", "cooling"} else "观察"
    return f"{'/'.join(labels[:3])} {action}"


def _physical_variable_current_value(
    features: Mapping[str, Any],
    key: str,
) -> Dict[str, Any]:
    if key == "cloud_cover":
        value, source = _first_feature_number(
            features,
            [
                "cloud_cover_low_pct",
                "cloud_cover_mid_pct",
                "cloud_cover_pct",
                "metar_effective_cloud_cover_pct",
                "nws_effective_cloud_cover_pct",
            ],
        )
        return {"value": value, "unit": "%", "source_field": source}
    if key == "cloud_growth":
        value, source = _first_feature_number(
            features,
            [
                "cloud_cover_growth_pct_per_h",
                "cloud_cover_trend_pct_per_h",
                "cloud_cover_change_pct_1h",
                "low_cloud_cover_growth_pct_per_h",
                "cloud_growth_pct_per_h",
            ],
        )
        return {"value": value, "unit": "pp/h", "source_field": source}
    if key == "radar_echo":
        profile = precipitation_evidence_profile(features)
        if profile.get("no_precip_confirmed"):
            return {
                "value": 0.0,
                "unit": "mm/h",
                "source_field": str(profile.get("source_field") or "verified_no_precip"),
            }
        if profile.get("available_strong"):
            value = _to_float(profile.get("rate_mm_per_h"))
            return {
                "value": float(value) if value is not None else None,
                "unit": "mm/h",
                "source_field": str(profile.get("source_field") or "verified_precip"),
            }
        if profile.get("is_proxy_only"):
            return {
                "value": None,
                "unit": "mm/h",
                "source_field": str(profile.get("source_field") or "proxy_precip_ignored"),
            }
        value, source = _first_feature_number(
            features,
            [
                "radar_precip_rate_mm_per_h",
                "qpe_precip_rate_mm_per_h",
                "observed_precip_rate_mm_per_h",
                "precipitation_rate_mm_per_h",
                "rain_rate_mm_per_h",
            ],
        )
        if value is None:
            raining, bool_source = _first_feature_bool(
                features,
                ["is_raining_observed", "radar_precip_observed", "metar_rain_observed", "is_raining"],
            )
            if raining is not None:
                return {"value": 1.0 if raining else 0.0, "unit": "bool", "source_field": bool_source}
        return {"value": value, "unit": "mm/h", "source_field": source}
    value, source = _first_feature_number(
        features,
        ["dewpoint_c", "dew_point_c", "dewpoint_2m_c", "dew_point_2m_c", "wet_bulb_c"],
    )
    return {"value": value, "unit": "C", "source_field": source}


def _radar_echo_source_profile(features: Mapping[str, Any], current: Mapping[str, Any]) -> Dict[str, Any]:
    profile = precipitation_evidence_profile(features)
    if not profile.get("source_field") and current.get("source_field"):
        profile = dict(profile)
        profile["source_field"] = str(current.get("source_field") or "")
    return dict(profile)


def _physical_variable_trend(
    features: Mapping[str, Any],
    key: str,
) -> Dict[str, Any]:
    if key == "cloud_growth":
        value = _physical_variable_current_value(features, key)
    elif key == "radar_echo":
        profile = precipitation_evidence_profile(features)
        if profile.get("no_precip_confirmed"):
            value = {"value": 0.0, "unit": "mm/h2", "source_field": str(profile.get("source_field") or "verified_no_precip")}
        elif profile.get("is_proxy_only"):
            value = {"value": None, "unit": "mm/h2", "source_field": str(profile.get("source_field") or "proxy_precip_ignored")}
        else:
            trend, source = _first_feature_number(
                features,
                ["radar_precip_rate_trend_mm_per_h2", "precip_rate_change_mm_per_h_1h", "rain_rate_trend_mm_per_h2"],
            )
            value = {"value": trend, "unit": "mm/h2", "source_field": source}
    elif key == "dew_point":
        trend, source = _first_feature_number(
            features,
            ["dewpoint_trend_c_per_h", "dew_point_trend_c_per_h", "dewpoint_change_c_1h"],
        )
        value = {"value": trend, "unit": "C/h", "source_field": source}
    else:
        trend, source = _first_feature_number(
            features,
            ["cloud_cover_trend_pct_per_h", "cloud_cover_change_pct_1h", "low_cloud_cover_growth_pct_per_h"],
        )
        value = {"value": trend, "unit": "pp/h", "source_field": source}
    raw = _to_float(value.get("value"))
    if raw is None:
        return {"value": None, "unit": value.get("unit"), "source_field": "", "direction": "unknown"}
    direction = "rising" if raw > 0.05 else ("falling" if raw < -0.05 else "flat")
    return {
        "value": float(raw),
        "unit": value.get("unit"),
        "source_field": value.get("source_field") or "",
        "direction": direction,
    }


def _temperature_momentum_variable(features: Mapping[str, Any]) -> Dict[str, Any]:
    raw_rates: Dict[str, float] = {}
    for label in TEMPERATURE_MOMENTUM_WINDOWS:
        rate, _ = _first_feature_number(
            features,
            [
                f"temperature_momentum_{label}_c_per_hour",
                f"temperature_rise_rate_{label}_c_per_hour",
            ],
        )
        if rate is not None:
            raw_rates[label] = float(rate)
    if "1h" not in raw_rates:
        fallback, _ = _first_feature_number(
            features,
            ["temperature_rise_rate_c_per_hour", "observed_temp_rise_rate_c_per_hour", "metar_temp_rise_rate_c_per_hour"],
        )
        if fallback is not None:
            raw_rates["1h"] = float(fallback)
    rates = {
        label: rate
        for label, rate in raw_rates.items()
        if math.isfinite(float(rate)) and abs(float(rate)) <= TEMPERATURE_MOMENTUM_RATE_ABS_MAX
    }
    suppressed_rates = {
        label: rate
        for label, rate in raw_rates.items()
        if label not in rates
    }
    effective, source = _first_feature_number(features, ["temperature_momentum_effective_c_per_hour"])
    if effective is not None and abs(float(effective)) > TEMPERATURE_MOMENTUM_RATE_ABS_MAX:
        effective = None
        source = "suppressed_untrusted_temperature_momentum_effective"
    if effective is None and rates:
        denom = sum(TEMPERATURE_MOMENTUM_WEIGHTS.get(key, 0.0) for key in rates) or float(len(rates))
        effective = sum(rate * TEMPERATURE_MOMENTUM_WEIGHTS.get(key, 1.0) for key, rate in rates.items()) / max(EPS, denom)
        source = "computed_from_temperature_momentum_rates"
    state = str(features.get("temperature_momentum_realization_state") or "").strip()
    if suppressed_rates or source == "suppressed_untrusted_temperature_momentum_effective":
        state = ""
    if not state:
        recent = [rates[key] for key in TEMPERATURE_MOMENTUM_RECENT_WINDOWS if key in rates]
        short = sum(recent) / max(1, len(recent)) if recent else rates.get("1h")
        positive_count = sum(1 for rate in rates.values() if rate >= 0.25)
        nonpositive_count = sum(1 for rate in rates.values() if rate <= 0.0)
        eff = _to_float(effective)
        if not rates:
            state = "missing"
        elif short is not None and short <= -0.20 and eff is not None and eff <= 0.0:
            state = "cooling"
        elif short is not None and short <= 0.0 and eff is not None and eff <= 0.10:
            state = "not_realized"
        elif len(rates) >= 2 and short is not None and short >= 0.35 and positive_count >= 2 and eff is not None and eff >= 0.25:
            state = "sustained_warming"
        elif short is not None and short >= 0.35 and eff is not None and eff >= 0.10:
            state = "emerging_warming"
        elif eff is not None and eff >= 0.15 and nonpositive_count == 0:
            state = "weak_warming"
        elif eff is not None and abs(eff) < 0.15:
            state = "flat"
        else:
            state = "mixed"
    if state in {"sustained_warming", "emerging_warming", "weak_warming"}:
        direction = "warming"
    elif state in {"cooling", "not_realized", "flat"}:
        direction = "cooling"
    elif state == "missing":
        direction = "missing"
    else:
        direction = "neutral"
    momentum_gate = _temperature_momentum_posterior_gate(features, state=state, rate=effective)
    if direction == "warming" and not momentum_gate["allowed"]:
        direction = "neutral"
    conclusion_by_state = {
        "sustained_warming": "分钟级到小时级温度动量显示升温正在持续兑现。",
        "emerging_warming": "短窗口升温较强，但仍需中长窗口确认持续性。",
        "weak_warming": "多周期动量偏正但不强，只能小幅确认升温潜力。",
        "cooling": "温度动量转向降温，升温潜力没有兑现。",
        "not_realized": "当前动量不正，晴空/辐射潜力不得单独放大高温桶。",
        "flat": "温度动量接近走平，升温潜力暂未强兑现。",
        "mixed": "多窗口动量方向分歧，暂不按强持续升温处理。",
        "missing": "缺少 24h/12h/6h/3h/2h/1h/45m/30m/15m/10m/5m/3m/2m/1m 温度动量，不能确认升温潜力是否兑现。",
    }
    return {
        "key": "temperature_momentum",
        "label": "Temperature Momentum 温度动量",
        "current": {"value": effective, "unit": "°C/h", "source_field": source or "temperature_momentum"},
        "trend": {"value": effective, "unit": "°C/h", "source_field": source or "", "direction": "rising" if direction == "warming" else ("falling" if direction == "cooling" else "flat")},
        "evidence_state": "observed_realization" if rates else "missing",
        "na_reason": "" if rates else "temperature_momentum_missing",
        "evidence_class": "observed_realization",
        "role": "realization_gate_not_physical_potential",
        "direction": direction,
        "warming_pp": 0.0,
        "cooling_pp": 0.0,
        "observed_warming_pp": 0.0,
        "observed_cooling_pp": 0.0,
        "neutral": bool(direction in {"neutral", "missing"}),
        "target_buckets": [],
        "target_bucket_text": "",
        "reason": (
            "短窗口升温只作为风险提示；缺少 30m/1h 等多窗口确认，暂不进入主后验。"
            if momentum_gate["state"] == "short_window_watch"
            else conclusion_by_state.get(state, conclusion_by_state["mixed"])
        ),
        "metrics": {
            **{f"momentum_{label}_c_per_hour": rates.get(label) for label in TEMPERATURE_MOMENTUM_WINDOWS},
            "momentum_rates_c_per_hour": {label: rates[label] for label in TEMPERATURE_MOMENTUM_WINDOWS if label in rates},
            "suppressed_untrusted_rates_c_per_hour": {
                label: suppressed_rates[label]
                for label in TEMPERATURE_MOMENTUM_WINDOWS
                if label in suppressed_rates
            },
            "effective_rate_c_per_hour": effective,
            "realization_state": state,
            "sustained_warming": bool(state == "sustained_warming"),
            "posterior_gate_allowed": bool(momentum_gate["allowed"]),
            "posterior_gate_state": str(momentum_gate["state"]),
            "posterior_gate_reason": str(momentum_gate["reason"]),
            "posterior_confirmed_windows": list(momentum_gate["confirmed_windows"]),
            "posterior_confirmed_window_count": int(momentum_gate["confirmed_positive_window_count"]),
            "positive_short_windows": list(momentum_gate["positive_short_windows"]),
        },
    }


def _build_physical_d0_explanation(
    *,
    labels: List[str],
    rounding_rule: Optional[str],
    features: Mapping[str, Any],
    physical_likelihood_meta: Mapping[str, Any],
    base_no_physical_probs: List[float],
    physical_likelihood_evidence: float,
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
    if alpha_total > EPS and physical_likelihood_evidence > 0:
        posterior_scale = min(1.0, max(0.0, float(physical_likelihood_evidence) / float(alpha_total)))
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
        "physical_evidence": float(physical_likelihood_evidence),
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


def _taf_time_scale(days_to_event: Optional[float]) -> float:
    days = _to_float(days_to_event)
    if days is None:
        return 1.0
    if days <= 0.5:
        return max(0.0, _env_float("PM_BAYES_TAF_PEAK_D0_SCALE", 1.0))
    if days <= 1.5:
        return max(0.0, _env_float("PM_BAYES_TAF_PEAK_D1_SCALE", 0.65))
    return max(0.0, _env_float("PM_BAYES_TAF_PEAK_FAR_SCALE", 0.0))


def _taf_walk_forward_calibrated(features: Mapping[str, Any]) -> bool:
    if _feature_bool(
        features,
        "taf_forecast_peak_created_at_walk_forward_calibrated",
        "taf_forecast_peak_walk_forward_calibrated",
        "taf_forecast_peak_weight_calibrated",
    ):
        return True
    return _to_bool(
        os.environ.get("PM_BAYES_TAF_PEAK_WALK_FORWARD_CALIBRATED")
        or os.environ.get("PM_TAF_FORECAST_PEAK_WALK_FORWARD_CALIBRATED"),
        default=False,
    )


def _taf_main_posterior_enabled(features: Mapping[str, Any]) -> bool:
    if _feature_bool(
        features,
        "taf_forecast_peak_main_posterior_enabled",
        "taf_forecast_peak_allow_main_posterior",
    ):
        return True
    return _to_bool(
        os.environ.get("PM_BAYES_TAF_PEAK_MAIN_POSTERIOR_ENABLED")
        or os.environ.get("PM_TAF_FORECAST_PEAK_MAIN_POSTERIOR_ENABLED"),
        default=False,
    )


def _wu_forecast_time_scale(days_to_event: Optional[float]) -> float:
    days = _to_float(days_to_event)
    if days is None:
        return 1.0
    if days <= 0.5:
        return max(0.0, _env_float("PM_BAYES_WU_FORECAST_D0_SCALE", 1.0))
    if days <= 1.5:
        return max(0.0, _env_float("PM_BAYES_WU_FORECAST_D1_SCALE", 0.65))
    return max(0.0, _env_float("PM_BAYES_WU_FORECAST_FAR_SCALE", 0.0))


def _weather_com_forecast_time_scale(days_to_event: Optional[float]) -> float:
    days = _to_float(days_to_event)
    if days is None:
        return 1.0
    if days <= 0.5:
        return max(0.0, _env_float("PM_BAYES_WEATHER_COM_FORECAST_D0_SCALE", 1.0))
    if days <= 1.5:
        return max(0.0, _env_float("PM_BAYES_WEATHER_COM_FORECAST_D1_SCALE", 0.65))
    return max(0.0, _env_float("PM_BAYES_WEATHER_COM_FORECAST_FAR_SCALE", 0.0))


def _latest_rows_by_event(rows: List[sqlite3.Row]) -> List[sqlite3.Row]:
    latest: Dict[str, sqlite3.Row] = {}
    for row in rows:
        key = str(row["slug"] or "") or f"{row['title'] or ''}|{row['event_date'] or ''}"
        old = latest.get(key)
        if old is None or str(row["created_at"] or "") > str(old["created_at"] or ""):
            latest[key] = row
    return list(latest.values())


def build_historical_bayesian_context(
    *,
    db_path: Any,
    pre_items: List[Dict[str, Any]],
    city: Optional[str] = None,
    station_id: Optional[str] = None,
    market_var: Optional[str] = None,
    market_unit: Optional[str] = None,
    run_date: Optional[str] = None,
    active_models: Optional[List[str]] = None,
    days: Optional[int] = None,
    min_samples: Optional[int] = None,
) -> Dict[str, Any]:
    """Build read-only historical prior and model reliability context."""

    if not _to_bool(os.environ.get("PM_BAYES_HISTORICAL_PRIOR_ENABLED"), default=True):
        return {"enabled": False, "source": "disabled"}
    labels = [str(item.get("label") or "") for item in pre_items]
    if not labels:
        return {"enabled": True, "source": "empty", "sample_count": 0}

    path = Path(str(db_path or ""))
    if not path.exists():
        return {"enabled": True, "source": "missing_db", "sample_count": 0}
    days_val = days if days is not None else int(os.environ.get("PM_BAYES_HISTORICAL_DAYS", "180") or 180)
    min_val = min_samples if min_samples is not None else int(os.environ.get("PM_BAYES_HISTORICAL_MIN_SAMPLES", "3") or 3)
    end_dt = _parse_event_date(run_date) or datetime.today().date()
    start_dt = end_dt - timedelta(days=max(1, int(days_val)))
    city_norm = _normalize_text(city)
    station_norm = _normalize_text(station_id)
    unit_norm = _normalize_unit(market_unit)
    model_set = {str(m) for m in (active_models or []) if str(m or "").strip()}

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT city, station_id, market_var, market_unit, rounding_rule,
                   settlement_actual, payload_json, slug, title, event_date, created_at
            FROM event_runs
            WHERE settlement_actual IS NOT NULL
              AND payload_json IS NOT NULL
              AND payload_json != ''
              AND event_date >= ?
              AND event_date <= ?
              AND (? = '' OR market_var = ?)
            ORDER BY created_at ASC
            """,
            (start_dt.isoformat(), end_dt.isoformat(), str(market_var or ""), str(market_var or "")),
        ).fetchall()
        conn.close()
    except Exception as exc:
        return {"enabled": True, "source": "history_error", "sample_count": 0, "error": str(exc)[:160]}

    candidates = []
    for row in _latest_rows_by_event(list(rows)):
        row_unit = _normalize_unit(row["market_unit"])
        if unit_norm and row_unit and row_unit != unit_norm:
            continue
        row_city = _normalize_text(row["city"])
        row_station = _normalize_text(row["station_id"])
        scope = "global"
        if station_norm and row_station and row_station == station_norm:
            scope = "station"
        elif city_norm and row_city and row_city == city_norm:
            scope = "city"
        elif city_norm or station_norm:
            scope = "fallback"
        candidates.append((scope, row))

    preferred = [row for scope, row in candidates if scope == "station"]
    source = "station_history"
    if not preferred:
        preferred = [row for scope, row in candidates if scope == "city"]
        source = "city_history"
    if not preferred:
        preferred = [row for scope, row in candidates if scope in {"global", "fallback"}]
        source = "market_history"

    counts = [0.0 for _ in labels]
    hit_events = 0
    model_stats: Dict[str, Dict[str, float]] = {}
    for row in preferred:
        actual = _to_float(row["settlement_actual"])
        if actual is None:
            continue
        rounding_rule = str(row["rounding_rule"] or "")
        matched = False
        for idx, label in enumerate(labels):
            hit = _bucket_hit(float(actual), label, rounding_rule)
            if hit is None:
                continue
            if hit:
                counts[idx] += 1.0
                matched = True
        if matched:
            hit_events += 1
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            continue
        for bucket in payload.get("rows") or []:
            if not isinstance(bucket, Mapping):
                continue
            outcome = str(bucket.get("outcome") or "")
            actual_hit = _bucket_hit(float(actual), outcome, rounding_rule)
            if actual_hit is None:
                continue
            per_model_q = bucket.get("per_model_q") if isinstance(bucket.get("per_model_q"), Mapping) else {}
            for model, band in per_model_q.items():
                if model_set and str(model) not in model_set:
                    continue
                if not isinstance(band, Mapping):
                    continue
                q_val = _clamp01(_to_float(band.get("q_round") or band.get("q_raw")))
                if q_val is None:
                    continue
                target = 1.0 if actual_hit else 0.0
                entry = model_stats.setdefault(str(model), {"count": 0.0, "brier_sum": 0.0})
                entry["count"] += 1.0
                entry["brier_sum"] += (q_val - target) ** 2

    prior_probs: Optional[List[float]] = None
    prior_sample_scale = 0.0
    if hit_events > 0:
        smoothing = max(0.0, _env_float("PM_BAYES_HISTORICAL_PRIOR_SMOOTHING", 0.5))
        prior_probs = _normalize([c + smoothing for c in counts])
        prior_sample_scale = min(1.0, float(hit_events) / float(max(1, int(min_val))))
    multipliers: Dict[str, float] = {}
    raw_scores: Dict[str, float] = {}
    for model, entry in model_stats.items():
        count = entry.get("count") or 0.0
        if count < max(1, int(min_val)):
            continue
        brier = (entry.get("brier_sum") or 0.0) / count
        raw_scores[model] = 1.0 / max(0.03, float(brier))
    if raw_scores:
        mean_score = sum(raw_scores.values()) / len(raw_scores)
        for model, score in raw_scores.items():
            mult = score / max(EPS, mean_score)
            multipliers[model] = max(0.25, min(2.5, float(mult)))

    return {
        "enabled": True,
        "source": source if prior_sample_scale >= 1.0 else (f"{source}_low_sample" if prior_probs is not None else "history_insufficient"),
        "sample_count": hit_events,
        "min_samples": int(min_val),
        "prior_sample_scale": prior_sample_scale,
        "days": int(days_val),
        "prior_probs": prior_probs,
        "bucket_counts": counts,
        "model_weight_multipliers": multipliers,
        "model_reliability_samples": {m: v.get("count", 0.0) for m, v in model_stats.items()},
    }


def _weighted_model_probability(
    per_model_q: Mapping[str, Mapping[str, Any]],
    active_models: List[str],
    weights: Mapping[str, Any],
    model_weight_multipliers: Optional[Mapping[str, Any]] = None,
    *,
    q_key: str = "q_round",
) -> Tuple[Optional[float], float, int]:
    total_weight = 0.0
    weighted_q = 0.0
    model_count = 0
    for model in active_models or []:
        band = per_model_q.get(model)
        if not isinstance(band, Mapping):
            continue
        q_val = _clamp01(_to_float(band.get(q_key)))
        if q_val is None:
            continue
        raw_weight = _to_float(weights.get(model))
        weight = float(raw_weight) if raw_weight is not None and raw_weight > 0 else 1.0
        mult = _to_float((model_weight_multipliers or {}).get(model))
        if mult is not None and mult > 0:
            weight *= float(mult)
        weighted_q += weight * q_val
        total_weight += weight
        model_count += 1
    if total_weight <= EPS or model_count <= 0:
        return None, 0.0, 0
    return weighted_q / total_weight, total_weight, model_count


def apply_bayesian_probability_update(
    pre_items: List[Dict[str, Any]],
    *,
    weights: Mapping[str, Any],
    model_cache_freshness: Optional[float] = None,
    days_to_event: Optional[float] = None,
    lcb_stage_tz: Optional[str] = None,
    historical_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Update bucket probabilities using a lightweight Dirichlet posterior.

    This keeps the existing model/scenario weighting as the likelihood builder,
    then converts the full bucket vector into one posterior snapshot. The updated
    posterior mean is written back to q_raw/q_round so downstream q calibration,
    LCB, realizable edge, Kelly, and laddering use the Bayesian probability.
    """

    if not _to_bool(os.environ.get("PM_BAYES_PROBABILITY_ENABLED"), default=True):
        return {"enabled": False, "source": "disabled"}
    if not pre_items:
        return {"enabled": True, "source": "empty"}

    prior_strength = max(0.0, _env_float("PM_BAYES_PROBABILITY_PRIOR_STRENGTH", 12.0))
    model_strength = max(0.0, _env_float("PM_BAYES_PROBABILITY_MODEL_STRENGTH", 36.0))
    nowcast_strength = max(0.0, _env_float("PM_BAYES_PROBABILITY_NOWCAST_STRENGTH", 0.0))
    z_meta = resolve_lcb_stage_z(
        "PM_BAYES_PROBABILITY_Z",
        default=1.28,
        days_to_event=days_to_event,
        tz_name=lcb_stage_tz,
        stage_defaults=DEFAULT_BAYES_STAGE_Z,
    )
    z_value = max(0.0, _to_float(z_meta.get("z")) or 1.28)
    freshness = _clamp01(model_cache_freshness)
    if freshness is None:
        freshness = 1.0

    labels = []
    for item in pre_items:
        nested = item.get("item") if isinstance(item.get("item"), Mapping) else {}
        labels.append(str(item.get("label") or nested.get("label") or ""))
    first_rounding_rule = None
    for item in pre_items:
        if item.get("rounding_rule"):
            first_rounding_rule = str(item.get("rounding_rule") or "")
            break
    atomic_temperature_labels, atomic_temperature_changed = _atomic_temperature_bucket_labels(labels)
    atomic_temperature_display_enabled = (
        bool(atomic_temperature_changed)
        and _to_bool(
            os.environ.get("PM_BAYES_ATOMIC_TEMPERATURE_BUCKET_DISPLAY_ENABLED"),
            default=True,
        )
    )
    raw_probs: List[float] = []
    model_probs: List[float] = []
    atomic_raw_probs: List[float] = []
    atomic_model_probs: List[float] = []
    nowcast_probs: List[float] = []
    atomic_nowcast_probs: List[float] = []
    history = historical_context if isinstance(historical_context, Mapping) else {}
    historical_prior = history.get("prior_probs") if isinstance(history.get("prior_probs"), list) else None
    model_weight_multipliers = (
        history.get("model_weight_multipliers") if isinstance(history.get("model_weight_multipliers"), Mapping) else {}
    )
    model_weight_sum = 0.0
    model_count_max = 0
    nowcast_share_max = 0.0

    for item in pre_items:
        q_obj = item.get("q") if isinstance(item.get("q"), Mapping) else {}
        raw_probs.append(_clamp01(_to_float(q_obj.get("q_raw") or q_obj.get("q_round"))) or 0.0)
        model_prob, model_weight, model_count = _weighted_model_probability(
            item.get("per_model_q") if isinstance(item.get("per_model_q"), Mapping) else {},
            list(item.get("active_models") or []),
            weights,
            model_weight_multipliers,
        )
        model_probs.append(model_prob if model_prob is not None else raw_probs[-1])
        atomic_model_prob = None
        atomic_per_model = item.get("per_model_q_atomic") if isinstance(item.get("per_model_q_atomic"), Mapping) else {}
        if atomic_temperature_display_enabled and atomic_per_model:
            atomic_model_prob, _, _ = _weighted_model_probability(
                atomic_per_model,
                list(item.get("active_models") or []),
                weights,
                model_weight_multipliers,
            )
        atomic_q_obj = item.get("q_atomic") if isinstance(item.get("q_atomic"), Mapping) else {}
        atomic_raw = _clamp01(
            _to_float(
                atomic_q_obj.get("q_raw")
                or atomic_q_obj.get("q_round")
                or item.get("q_model_atomic_round")
            )
        )
        if atomic_raw is None:
            atomic_raw = atomic_model_prob
        atomic_raw_probs.append(float(atomic_raw) if atomic_raw is not None else raw_probs[-1])
        atomic_model_probs.append(
            float(atomic_model_prob)
            if atomic_model_prob is not None
            else float(atomic_raw_probs[-1])
        )
        model_weight_sum = max(model_weight_sum, float(model_weight))
        model_count_max = max(model_count_max, int(model_count))
        nowcast_q = _clamp01(_to_float(item.get("q_observed_nowcast")))
        nowcast_probs.append(nowcast_q if nowcast_q is not None else 0.0)
        atomic_nowcast_q = _clamp01(_to_float(item.get("q_observed_nowcast_atomic")))
        atomic_nowcast_probs.append(atomic_nowcast_q if atomic_nowcast_q is not None else 0.0)
        nowcast_share = _clamp01(_to_float((q_obj or {}).get("observed_nowcast_share")))
        if nowcast_share is None:
            nowcast_share = _clamp01(_to_float(item.get("observed_nowcast_share"))) or 0.0
        nowcast_share_max = max(nowcast_share_max, float(nowcast_share))

    if historical_prior and len(historical_prior) == len(pre_items):
        history_probs = _normalize([float(v) for v in historical_prior])
        current_probs = _normalize(raw_probs)
        sample_scale = _clamp01(_to_float(history.get("prior_sample_scale")))
        if sample_scale is None:
            sample_scale = 1.0
        prior_probs = _normalize(
            [
                float(sample_scale) * history_probs[idx] + (1.0 - float(sample_scale)) * current_probs[idx]
                for idx in range(len(pre_items))
            ]
        )
        prior_source = str(history.get("source") or "historical")
    else:
        prior_probs = _normalize(raw_probs)
        prior_source = "current_distribution"
    likelihood_probs = _normalize(model_probs)
    nowcast_probs_norm = _normalize(nowcast_probs) if any(v > EPS for v in nowcast_probs) else [0.0] * len(pre_items)
    atomic_prior_probs = _normalize(atomic_raw_probs) if any(v > EPS for v in atomic_raw_probs) else [0.0] * len(pre_items)
    atomic_likelihood_probs = _normalize(atomic_model_probs) if any(v > EPS for v in atomic_model_probs) else [0.0] * len(pre_items)
    atomic_nowcast_probs_norm = (
        _normalize(atomic_nowcast_probs)
        if any(v > EPS for v in atomic_nowcast_probs)
        else [0.0] * len(pre_items)
    )
    likelihood_features = _extract_likelihood_features(pre_items, history)
    soft_forecast_scope = _soft_forecast_scope_meta(likelihood_features)
    soft_forecast_scope_allowed = bool(soft_forecast_scope.get("allowed"))
    physical_likelihood_scope = _physical_likelihood_scope_meta(likelihood_features)
    physical_likelihood_scope_allowed = bool(physical_likelihood_scope.get("allowed"))
    soft_forecast_manual_override = _to_bool(
        os.environ.get("PM_BAYES_SOFT_FORECAST_MANUAL_OVERRIDE_ENABLED"),
        default=False,
    )
    ctp_soft_forecast_precision_scale = max(
        0.0,
        _env_float("PM_BAYES_CTP_SOFT_FORECAST_PRECISION_SCALE", 1.0),
    )
    ctp_model_ensemble_precision_scale = max(
        0.0,
        _env_float("PM_BAYES_CTP_MODEL_ENSEMBLE_PRECISION_SCALE", ctp_soft_forecast_precision_scale),
    )
    ctp_wu_forecast_precision_scale = max(
        0.0,
        _env_float("PM_BAYES_CTP_WU_FORECAST_PRECISION_SCALE", ctp_soft_forecast_precision_scale),
    )
    ctp_weather_com_forecast_precision_scale = max(
        0.0,
        _env_float("PM_BAYES_CTP_WEATHER_COM_FORECAST_PRECISION_SCALE", ctp_soft_forecast_precision_scale),
    )
    ctp_taf_peak_precision_scale = max(
        0.0,
        _env_float("PM_BAYES_CTP_TAF_PEAK_PRECISION_SCALE", ctp_soft_forecast_precision_scale),
    )

    days = _to_float(days_to_event)
    physical_likelihood_d0_only = _to_bool(
        os.environ.get("PM_BAYES_LIKELIHOOD_D0_ONLY"),
        default=False,
    )
    physical_likelihood_d0_max_days = max(0.0, _env_float("PM_BAYES_LIKELIHOOD_D0_MAX_DAYS", 0.75))
    physical_likelihood_stage_allowed = True
    physical_likelihood_stage_reason = "all_stages_allowed"
    if physical_likelihood_d0_only:
        if days is None:
            physical_likelihood_stage_allowed = False
            physical_likelihood_stage_reason = "days_to_event_missing"
        elif float(days) > physical_likelihood_d0_max_days:
            physical_likelihood_stage_allowed = False
            physical_likelihood_stage_reason = "outside_d0_window"
        else:
            physical_likelihood_stage_reason = "d0_window"
    time_mult = 1.0
    if days is not None:
        if days <= 0.5:
            time_mult = 1.35
        elif days <= 1.5:
            time_mult = 1.15
        elif days >= 5.0:
            time_mult = 0.75
    model_evidence = model_strength * max(0.25, freshness) * time_mult
    if model_count_max > 0:
        model_evidence *= min(1.5, max(0.5, model_count_max / 3.0))
    nowcast_evidence = nowcast_strength * max(0.0, min(1.0, nowcast_share_max))
    wu_forecast_evidence = 0.0
    wu_forecast_evidence_base = 0.0
    wu_forecast_evidence_scale = 0.0
    wu_forecast_probs = [0.0] * len(pre_items)
    wu_forecast_atomic_probs = [0.0] * len(pre_items)
    wu_forecast_meta: Dict[str, Any] = {"enabled": False, "status": "disabled"}
    if _to_bool(os.environ.get("PM_BAYES_WU_FORECAST_ENABLED"), default=True):
        wu_forecast_c = _feature_float(likelihood_features, "wu_forecast_peak_c", "settlement_source_forecast_peak_c")
        wu_forecast_soft = _feature_bool(
            likelihood_features,
            "wu_forecast_peak_soft",
            "settlement_source_forecast_peak_soft",
            "twc_forecast_peak_soft",
        )
        wu_forecast_settlement_usable = _feature_bool(
            likelihood_features,
            "wu_forecast_peak_usable_for_settlement",
            "settlement_source_forecast_peak_usable_for_settlement",
        )
        wu_forecast_observed_nowcast = _feature_bool(
            likelihood_features,
            "wu_forecast_peak_observed_nowcast",
            "settlement_source_forecast_peak_observed_nowcast",
        )
        if wu_forecast_c is None:
            wu_forecast_meta = {"enabled": False, "status": "missing_wu_forecast_peak_c"}
        elif not all(labels):
            wu_forecast_meta = {"enabled": False, "status": "missing_labels"}
        elif wu_forecast_settlement_usable or wu_forecast_observed_nowcast:
            wu_forecast_meta = {"enabled": False, "status": "wu_forecast_role_boundary_violation"}
        elif not wu_forecast_soft:
            wu_forecast_meta = {"enabled": False, "status": "wu_forecast_not_marked_soft", "peak_c": float(wu_forecast_c)}
        elif not soft_forecast_scope_allowed:
            wu_forecast_meta = {
                "enabled": False,
                "status": "scope_not_allowed",
                "peak_c": float(wu_forecast_c),
                "scope": dict(soft_forecast_scope),
            }
        else:
            wu_forecast_sigma = _feature_float(likelihood_features, "wu_forecast_peak_sigma_c", "settlement_source_forecast_peak_sigma_c")
            if wu_forecast_sigma is None or wu_forecast_sigma <= 0:
                wu_forecast_sigma = max(0.05, _env_float("PM_BAYES_WU_FORECAST_SIGMA_C", 1.1))
            wu_forecast_probs = _normal_bucket_probs(
                labels,
                mu=float(wu_forecast_c),
                sigma=float(wu_forecast_sigma),
                rounding_rule=str(os.environ.get("PM_BAYES_WU_FORECAST_BUCKET_RULE") or "rounded_degrees"),
            )
            if atomic_temperature_display_enabled:
                wu_forecast_atomic_probs = _normal_bucket_probs(
                    atomic_temperature_labels,
                    mu=float(wu_forecast_c),
                    sigma=float(wu_forecast_sigma),
                    rounding_rule=str(os.environ.get("PM_BAYES_WU_FORECAST_BUCKET_RULE") or "rounded_degrees"),
                )
            if any(v > EPS for v in wu_forecast_probs):
                wu_forecast_evidence_base = max(0.0, _env_float("PM_BAYES_WU_FORECAST_EVIDENCE", 0.0))
                wu_forecast_confidence = _feature_float(likelihood_features, "wu_forecast_peak_confidence")
                if wu_forecast_confidence is None:
                    wu_forecast_confidence = 1.0
                wu_forecast_evidence_scale = max(0.0, min(1.0, float(wu_forecast_confidence))) * _wu_forecast_time_scale(days)
                wu_forecast_evidence = wu_forecast_evidence_base * wu_forecast_evidence_scale
                wu_forecast_status = "enabled_manual_override" if soft_forecast_manual_override and wu_forecast_evidence > 0 else "enabled"
                wu_forecast_meta = {
                    "enabled": bool(wu_forecast_evidence > 0),
                    "status": wu_forecast_status if wu_forecast_evidence > 0 else "zero_scaled",
                    "source": str(likelihood_features.get("wu_forecast_peak_source") or "wunderground_forecast_daily_peak"),
                    "peak_c": float(wu_forecast_c),
                    "sigma_c": float(wu_forecast_sigma),
                    "evidence_base": wu_forecast_evidence_base,
                    "evidence_scale": wu_forecast_evidence_scale,
                    "evidence": wu_forecast_evidence,
                    "time_scale": _wu_forecast_time_scale(days),
                    "manual_override": bool(soft_forecast_manual_override),
                    "scope": dict(soft_forecast_scope),
                }
            else:
                wu_forecast_meta = {"enabled": False, "status": "wu_forecast_bucket_probs_empty", "peak_c": float(wu_forecast_c)}
    weather_com_forecast_evidence = 0.0
    weather_com_forecast_evidence_raw = 0.0
    weather_com_forecast_evidence_base = 0.0
    weather_com_forecast_evidence_scale = 0.0
    weather_com_forecast_cap_meta: Dict[str, Any] = {}
    weather_com_forecast_probs = [0.0] * len(pre_items)
    weather_com_forecast_atomic_probs = [0.0] * len(pre_items)
    weather_com_forecast_meta: Dict[str, Any] = {"enabled": False, "status": "disabled"}
    if _to_bool(os.environ.get("PM_BAYES_WEATHER_COM_FORECAST_ENABLED"), default=False):
        weather_com_forecast_c = _feature_float(
            likelihood_features,
            "weather_com_forecast_peak_c",
            "twc_forecast_peak_c",
        )
        weather_com_forecast_soft = _feature_bool(
            likelihood_features,
            "weather_com_forecast_peak_soft",
            "twc_forecast_peak_soft",
        )
        weather_com_settlement_usable = _feature_bool(
            likelihood_features,
            "weather_com_forecast_peak_usable_for_settlement",
            "twc_forecast_peak_usable_for_settlement",
        )
        weather_com_observed_nowcast = _feature_bool(
            likelihood_features,
            "weather_com_forecast_peak_observed_nowcast",
            "twc_forecast_peak_observed_nowcast",
        )
        if weather_com_forecast_c is None:
            weather_com_forecast_meta = {"enabled": False, "status": "missing_weather_com_forecast_peak_c"}
        elif not all(labels):
            weather_com_forecast_meta = {"enabled": False, "status": "missing_labels"}
        elif weather_com_settlement_usable or weather_com_observed_nowcast:
            weather_com_forecast_meta = {
                "enabled": False,
                "status": "weather_com_role_boundary_violation",
                "peak_c": float(weather_com_forecast_c),
            }
        elif not weather_com_forecast_soft:
            weather_com_forecast_meta = {
                "enabled": False,
                "status": "weather_com_forecast_not_marked_soft",
                "peak_c": float(weather_com_forecast_c),
            }
        elif not soft_forecast_scope_allowed:
            weather_com_forecast_meta = {
                "enabled": False,
                "status": "scope_not_allowed",
                "peak_c": float(weather_com_forecast_c),
                "scope": dict(soft_forecast_scope),
            }
        else:
            weather_com_sigma = _feature_float(
                likelihood_features,
                "weather_com_forecast_peak_sigma_c",
                "twc_forecast_peak_sigma_c",
            )
            if weather_com_sigma is None or weather_com_sigma <= 0:
                weather_com_sigma = max(0.05, _env_float("PM_BAYES_WEATHER_COM_FORECAST_SIGMA_C", 0.8))
            weather_com_forecast_probs = _normal_bucket_probs(
                labels,
                mu=float(weather_com_forecast_c),
                sigma=float(weather_com_sigma),
                rounding_rule=str(os.environ.get("PM_BAYES_WEATHER_COM_FORECAST_BUCKET_RULE") or "rounded_degrees"),
            )
            if atomic_temperature_display_enabled:
                weather_com_forecast_atomic_probs = _normal_bucket_probs(
                    atomic_temperature_labels,
                    mu=float(weather_com_forecast_c),
                    sigma=float(weather_com_sigma),
                    rounding_rule=str(os.environ.get("PM_BAYES_WEATHER_COM_FORECAST_BUCKET_RULE") or "rounded_degrees"),
                )
            if any(v > EPS for v in weather_com_forecast_probs):
                weather_com_forecast_evidence_base = max(
                    0.0,
                    _env_float("PM_BAYES_WEATHER_COM_FORECAST_EVIDENCE", 0.0),
                )
                weather_com_confidence = _feature_float(
                    likelihood_features,
                    "weather_com_forecast_peak_confidence",
                    "twc_forecast_peak_confidence",
                )
                if weather_com_confidence is None:
                    weather_com_confidence = 1.0
                weather_com_forecast_evidence_scale = (
                    max(0.0, min(1.0, float(weather_com_confidence)))
                    * _weather_com_forecast_time_scale(days)
                )
                weather_com_forecast_evidence_raw = (
                    weather_com_forecast_evidence_base * weather_com_forecast_evidence_scale
                )
                weather_com_status = (
                    "enabled_manual_override"
                    if soft_forecast_manual_override and weather_com_forecast_evidence_raw > 0
                    else "enabled"
                )
                weather_com_forecast_meta = {
                    "enabled": bool(weather_com_forecast_evidence_raw > 0),
                    "status": weather_com_status if weather_com_forecast_evidence_raw > 0 else "zero_scaled",
                    "source": str(
                        likelihood_features.get("weather_com_forecast_peak_source")
                        or likelihood_features.get("twc_forecast_peak_source")
                        or "weather_com_forecast_daily_peak"
                    ),
                    "peak_c": float(weather_com_forecast_c),
                    "sigma_c": float(weather_com_sigma),
                    "evidence_base": weather_com_forecast_evidence_base,
                    "evidence_scale": weather_com_forecast_evidence_scale,
                    "evidence_raw": weather_com_forecast_evidence_raw,
                    "evidence": weather_com_forecast_evidence_raw,
                    "time_scale": _weather_com_forecast_time_scale(days),
                    "manual_override": bool(soft_forecast_manual_override),
                    "scope": dict(soft_forecast_scope),
                }
            else:
                weather_com_forecast_meta = {
                    "enabled": False,
                    "status": "weather_com_bucket_probs_empty",
                    "peak_c": float(weather_com_forecast_c),
                }
    taf_peak_evidence = 0.0
    taf_peak_evidence_base = 0.0
    taf_peak_evidence_scale = 0.0
    taf_peak_probs = [0.0] * len(pre_items)
    taf_peak_atomic_probs = [0.0] * len(pre_items)
    taf_peak_meta: Dict[str, Any] = {"enabled": False, "status": "disabled"}
    if _to_bool(os.environ.get("PM_BAYES_TAF_PEAK_ENABLED"), default=False):
        taf_peak_c = _feature_float(likelihood_features, "taf_forecast_peak_c")
        taf_usable = _feature_bool(
            likelihood_features,
            "taf_forecast_peak_usable_for_bayes",
            "taf_forecast_peak_strong_soft",
        )
        taf_calibrated = _taf_walk_forward_calibrated(likelihood_features)
        taf_main_posterior_enabled = _taf_main_posterior_enabled(likelihood_features)
        taf_settlement_usable = _feature_bool(likelihood_features, "taf_forecast_peak_usable_for_settlement")
        taf_observed_nowcast = _feature_bool(likelihood_features, "taf_forecast_peak_observed_nowcast")
        if taf_peak_c is None:
            taf_peak_meta = {"enabled": False, "status": "missing_taf_forecast_peak_c"}
        elif not all(labels):
            taf_peak_meta = {"enabled": False, "status": "missing_labels"}
        elif taf_settlement_usable or taf_observed_nowcast:
            taf_peak_meta = {"enabled": False, "status": "taf_role_boundary_violation"}
        elif not taf_usable:
            taf_peak_meta = {"enabled": False, "status": "taf_not_marked_usable_for_bayes"}
        elif not taf_calibrated:
            taf_peak_meta = {
                "enabled": False,
                "status": "taf_pending_created_at_walk_forward_calibration",
                "peak_c": float(taf_peak_c),
                "source": str(likelihood_features.get("taf_forecast_peak_source") or "zuuu_taf_awc_tx_peak"),
                "calibrated": False,
                "requested_for_bayes": bool(taf_usable),
            }
        elif not soft_forecast_scope_allowed:
            taf_peak_meta = {
                "enabled": False,
                "status": "scope_not_allowed",
                "peak_c": float(taf_peak_c),
                "scope": dict(soft_forecast_scope),
            }
        elif not taf_main_posterior_enabled:
            taf_peak_meta = {
                "enabled": False,
                "status": "taf_forecast_reference_only",
                "reason": "taf_is_forecast_not_hard_observation",
                "peak_c": float(taf_peak_c),
                "source": str(likelihood_features.get("taf_forecast_peak_source") or "zuuu_taf_awc_tx_peak"),
                "calibrated": bool(taf_calibrated),
                "requested_for_bayes": bool(taf_usable),
                "manual_override_ignored": bool(soft_forecast_manual_override),
            }
        else:
            taf_sigma = _feature_float(likelihood_features, "taf_forecast_peak_sigma_c")
            if taf_sigma is None or taf_sigma <= 0:
                taf_sigma = max(0.05, _env_float("PM_BAYES_TAF_PEAK_SIGMA_C", 0.8))
            taf_peak_probs = _normal_bucket_probs(
                labels,
                mu=float(taf_peak_c),
                sigma=float(taf_sigma),
                rounding_rule=str(os.environ.get("PM_BAYES_TAF_PEAK_BUCKET_RULE") or "rounded_degrees"),
            )
            if atomic_temperature_display_enabled:
                taf_peak_atomic_probs = _normal_bucket_probs(
                    atomic_temperature_labels,
                    mu=float(taf_peak_c),
                    sigma=float(taf_sigma),
                    rounding_rule=str(os.environ.get("PM_BAYES_TAF_PEAK_BUCKET_RULE") or "rounded_degrees"),
                )
            if any(v > EPS for v in taf_peak_probs):
                taf_peak_evidence_base = max(0.0, _env_float("PM_BAYES_TAF_PEAK_EVIDENCE", 0.0))
                taf_confidence = _feature_float(likelihood_features, "taf_forecast_peak_confidence")
                if taf_confidence is None:
                    taf_confidence = 1.0
                taf_peak_evidence_scale = max(0.0, min(1.0, float(taf_confidence))) * _taf_time_scale(days)
                taf_peak_evidence = taf_peak_evidence_base * taf_peak_evidence_scale
                taf_peak_meta = {
                    "enabled": bool(taf_peak_evidence > 0),
                    "status": "enabled" if taf_peak_evidence > 0 else "zero_scaled",
                    "source": str(likelihood_features.get("taf_forecast_peak_source") or "zuuu_taf_awc_tx_peak"),
                    "peak_c": float(taf_peak_c),
                    "sigma_c": float(taf_sigma),
                    "evidence_base": taf_peak_evidence_base,
                    "evidence_scale": taf_peak_evidence_scale,
                    "evidence": taf_peak_evidence,
                    "time_scale": _taf_time_scale(days),
                    "manual_override": False,
                    "manual_override_ignored": bool(soft_forecast_manual_override),
                    "calibrated": True,
                    "main_posterior_enabled": True,
                    "scope": dict(soft_forecast_scope),
                }
            else:
                taf_peak_meta = {"enabled": False, "status": "taf_bucket_probs_empty", "peak_c": float(taf_peak_c)}
    climo_regime_evidence = 0.0
    climo_regime_evidence_raw = 0.0
    climo_regime_evidence_base = 0.0
    climo_regime_evidence_scale_value = 0.0
    climo_regime_pp_cap_meta: Dict[str, Any] = {}
    climo_regime_cap_meta: Dict[str, Any] = {}
    climo_regime_no_climo_probs: List[float] = []
    climo_regime_probs = [0.0] * len(pre_items)
    climo_regime_atomic_probs = [0.0] * len(pre_items)
    climo_regime_meta: Dict[str, Any] = {"enabled": False, "status": "disabled"}
    climo_payload = history.get("climo_regime") if isinstance(history.get("climo_regime"), Mapping) else {}
    if isinstance(climo_payload, Mapping) and isinstance(climo_payload.get("features"), Mapping):
        climo_features: Mapping[str, Any] = climo_payload.get("features")  # type: ignore[assignment]
    elif isinstance(history.get("climo_regime_features"), Mapping):
        climo_features = history.get("climo_regime_features")  # type: ignore[assignment]
    else:
        climo_features = {}
    if isinstance(climo_payload, Mapping) and isinstance(climo_payload.get("bucket_probabilities"), Mapping):
        climo_probs_map: Mapping[str, Any] = climo_payload.get("bucket_probabilities")  # type: ignore[assignment]
    elif isinstance(history.get("climo_regime_bucket_probabilities"), Mapping):
        climo_probs_map = history.get("climo_regime_bucket_probabilities")  # type: ignore[assignment]
    else:
        climo_probs_map = {}
    if _to_bool(os.environ.get("PM_BAYES_CLIMO_REGIME_ENABLED"), default=True):
        if not all(labels):
            climo_regime_meta = {"enabled": False, "status": "missing_labels"}
        elif not isinstance(climo_features, Mapping) or not climo_features:
            climo_regime_meta = {"enabled": False, "status": "missing_climo_regime_features"}
        else:
            if isinstance(climo_probs_map, Mapping) and climo_probs_map:
                climo_regime_probs = _normalize(
                    [float(_clamp01(_to_float(climo_probs_map.get(label))) or 0.0) for label in labels]
                )
            else:
                computed_probs = climo_regime_bucket_probabilities(
                    labels,
                    climo_features,
                    rounding_rule=str(os.environ.get("PM_BAYES_CLIMO_REGIME_BUCKET_RULE") or "whole_degrees"),
                )
                if computed_probs:
                    climo_regime_probs = _normalize([float(computed_probs.get(label, 0.0) or 0.0) for label in labels])
            if atomic_temperature_display_enabled:
                computed_atomic_probs = climo_regime_bucket_probabilities(
                    atomic_temperature_labels,
                    climo_features,
                    rounding_rule=str(os.environ.get("PM_BAYES_CLIMO_REGIME_BUCKET_RULE") or "whole_degrees"),
                )
                if computed_atomic_probs:
                    climo_regime_atomic_probs = _normalize(
                        [float(computed_atomic_probs.get(label, 0.0) or 0.0) for label in atomic_temperature_labels]
                    )
            if any(v > EPS for v in climo_regime_probs):
                climo_regime_evidence_base = max(0.0, _env_float("PM_BAYES_CLIMO_REGIME_EVIDENCE", 2.0))
                scale_meta = climo_regime_evidence_scale(days_to_event=days, features=climo_features)
                climo_regime_evidence_scale_value = max(
                    0.0,
                    min(1.0, _to_float(scale_meta.get("scale")) or 0.0),
                )
                climo_regime_evidence_raw = climo_regime_evidence_base * climo_regime_evidence_scale_value
                climo_regime_evidence = climo_regime_evidence_raw
                climo_regime_meta = {
                    "enabled": bool(climo_regime_evidence > 0),
                    "status": "enabled" if climo_regime_evidence > 0 else "zero_scaled",
                    "source": str(climo_features.get("source") or "event_daily_temps_visible_history"),
                    "evidence_base": climo_regime_evidence_base,
                    "evidence_scale": climo_regime_evidence_scale_value,
                    "evidence": climo_regime_evidence,
                    "evidence_raw": climo_regime_evidence_raw,
                    "scale": dict(scale_meta),
                    "features": dict(climo_features),
                    "explanation": climo_regime_explanation(climo_features),
                }
            else:
                climo_regime_meta = {
                    "enabled": False,
                    "status": "climo_regime_bucket_probs_empty",
                    "features": dict(climo_features),
                }
    physical_likelihood_evidence = 0.0
    physical_likelihood_evidence_base = 0.0
    physical_likelihood_evidence_scale = 1.0
    physical_likelihood_evidence_scale_reason = ""
    physical_likelihood_evidence_scale_details: Dict[str, Any] = {}
    physical_likelihood_probs = [0.0] * len(pre_items)
    physical_likelihood_atomic_probs = [0.0] * len(pre_items)
    physical_likelihood_meta: Dict[str, Any] = {}
    physical_likelihood_atomic_meta: Dict[str, Any] = {}
    if _to_bool(os.environ.get("PM_BAYES_LIKELIHOOD_ENABLED"), default=True):
        if (
            physical_likelihood_scope_allowed
            and physical_likelihood_stage_allowed
            and likelihood_features
            and all(labels)
        ):
            hours_to_event = float(days) * 24.0 if days is not None else None
            physical_likelihood_meta = compute_weather_likelihood_vector(
                labels,
                likelihood_features,
                hours_to_event=hours_to_event,
                base_probs=likelihood_probs,
            )
            probs = physical_likelihood_meta.get("likelihood_probs")
            if (
                physical_likelihood_meta.get("enabled")
                and physical_likelihood_meta.get("probability_evidence_enabled", True)
                and isinstance(probs, list)
                and len(probs) == len(pre_items)
            ):
                physical_likelihood_probs = _normalize([float(v) for v in probs])
                physical_likelihood_evidence_base = max(
                    0.0,
                    _env_float("PM_BAYES_LIKELIHOOD_EVIDENCE", 0.0),
                )
                (
                    physical_likelihood_evidence_scale,
                    physical_likelihood_evidence_scale_reason,
                    physical_likelihood_evidence_scale_details,
                ) = _physical_likelihood_evidence_scale(
                    likelihood_features,
                    physical_likelihood_meta,
                    physical_likelihood_probs,
                )
                physical_likelihood_evidence = physical_likelihood_evidence_base * physical_likelihood_evidence_scale
                if atomic_temperature_display_enabled:
                    physical_likelihood_atomic_meta = compute_weather_likelihood_vector(
                        atomic_temperature_labels,
                        likelihood_features,
                        hours_to_event=hours_to_event,
                        base_probs=atomic_likelihood_probs,
                    )
                    atomic_probs = physical_likelihood_atomic_meta.get("likelihood_probs")
                    if (
                        physical_likelihood_atomic_meta.get("enabled")
                        and physical_likelihood_atomic_meta.get("probability_evidence_enabled", True)
                        and isinstance(atomic_probs, list)
                        and len(atomic_probs) == len(pre_items)
                    ):
                        physical_likelihood_atomic_probs = _normalize([float(v) for v in atomic_probs])
        elif not physical_likelihood_scope_allowed:
            physical_likelihood_meta = {
                "enabled": False,
                "status": "scope_not_allowed",
                "scope": dict(physical_likelihood_scope),
            }
        elif not physical_likelihood_stage_allowed:
            physical_likelihood_meta = {
                "enabled": False,
                "status": physical_likelihood_stage_reason,
                "scope": dict(physical_likelihood_scope),
            }

    temperature_trajectory_evidence = 0.0
    temperature_trajectory_evidence_base = 0.0
    temperature_trajectory_evidence_scale = 0.0
    temperature_trajectory_probs = [0.0] * len(pre_items)
    temperature_trajectory_atomic_probs = [0.0] * len(pre_items)
    temperature_trajectory_meta: Dict[str, Any] = {"enabled": False, "status": "disabled"}
    temperature_trajectory_atomic_meta: Dict[str, Any] = {}
    temperature_trajectory_uses_regime_v1_source_component = False
    temperature_trajectory_v1_source_component_enabled = _to_bool(
        os.environ.get("PM_BAYES_TRAJECTORY_REGIME_V1_SOURCE_COMPONENT_ENABLED"),
        default=True,
    )
    if likelihood_features and all(labels):
        if temperature_trajectory_v1_source_component_enabled:
            peak_meta: Dict[str, Any] = {}
            raw_peak_meta = likelihood_features.get("peak_estimator")
            if isinstance(raw_peak_meta, Mapping) and raw_peak_meta.get("enabled"):
                peak_meta = dict(raw_peak_meta)
            else:
                peak_meta = estimate_peak_temperature_from_features(likelihood_features)
            regime_v1 = (
                peak_meta.get("observed_temperature_trajectory_regime_v1")
                if isinstance(peak_meta.get("observed_temperature_trajectory_regime_v1"), Mapping)
                else {}
            )
            path_mu = _to_float(regime_v1.get("path_mu_c") if isinstance(regime_v1, Mapping) else None)
            path_sigma = _to_float(regime_v1.get("path_sigma_c") if isinstance(regime_v1, Mapping) else None)
            if (
                isinstance(regime_v1, Mapping)
                and bool(regime_v1.get("available"))
                and path_mu is not None
                and path_sigma is not None
                and path_sigma > 0.0
            ):
                path_sigma = max(0.25, float(path_sigma))
                precision_cap = max(0.0, _env_float("PM_BAYES_TRAJECTORY_REGIME_V1_PRECISION_CAP", 8.0))
                raw_precision = _to_float(
                    regime_v1.get("precision")
                    or regime_v1.get("effective_evidence")
                    or regime_v1.get("evidence")
                )
                if raw_precision is not None and raw_precision > 0:
                    precision = min(float(precision_cap), float(raw_precision))
                else:
                    precision = min(float(precision_cap), 1.0 / max(0.25, path_sigma) ** 2)
                evidence_base = _to_float(regime_v1.get("evidence_base"))
                if evidence_base is None or evidence_base <= 0:
                    evidence_base = 1.0 / max(0.25, path_sigma) ** 2
                evidence_scale = _to_float(regime_v1.get("evidence_scale"))
                if evidence_scale is None or evidence_scale < 0:
                    evidence_scale = precision / max(EPS, float(evidence_base))
                temperature_trajectory_probs = _normal_bucket_probs(
                    labels,
                    mu=float(path_mu),
                    sigma=float(path_sigma),
                    rounding_rule=first_rounding_rule or "rounded_degrees",
                )
                if any(v > EPS for v in temperature_trajectory_probs) and precision > 0.0:
                    temperature_trajectory_evidence = float(precision)
                    temperature_trajectory_evidence_base = float(evidence_base)
                    temperature_trajectory_evidence_scale = float(evidence_scale)
                    temperature_trajectory_uses_regime_v1_source_component = True
                    temperature_trajectory_meta = dict(regime_v1)
                    temperature_trajectory_meta.update(
                        {
                            "enabled": True,
                            "available": True,
                            "status": "enabled",
                            "source": "observed_temperature_trajectory_regime_v1",
                            "source_id": "temperature_trajectory",
                            "group": "temperature_trajectory",
                            "role": "observed_realization",
                            "source_class": "airport_temperature_path",
                            "mean_c": float(path_mu),
                            "mu_c": float(path_mu),
                            "sigma_c": float(path_sigma),
                            "precision": float(precision),
                            "evidence": float(precision),
                            "evidence_base": float(evidence_base),
                            "evidence_scale": float(evidence_scale),
                            "weaker_than_hard_source": True,
                            "source_component_enabled": True,
                            "peak_estimator": dict(peak_meta),
                            "rate_c_per_hour": regime_v1.get("rate_hat_c_per_hour"),
                            "realization_state": peak_meta.get("temperature_momentum_realization_state")
                            or likelihood_features.get("temperature_momentum_realization_state"),
                            "hours_to_peak_window": peak_meta.get("hours_to_peak_window")
                            or likelihood_features.get("hours_to_peak_window"),
                        }
                    )
                    if atomic_temperature_display_enabled:
                        temperature_trajectory_atomic_probs = _normal_bucket_probs(
                            atomic_temperature_labels,
                            mu=float(path_mu),
                            sigma=float(path_sigma),
                            rounding_rule=first_rounding_rule or "rounded_degrees",
                        )
                        temperature_trajectory_atomic_meta = dict(temperature_trajectory_meta)
                else:
                    temperature_trajectory_meta = {
                        "enabled": False,
                        "status": "regime_v1_bucket_probs_empty",
                        "source": "observed_temperature_trajectory_regime_v1",
                        "source_component_enabled": True,
                        "peak_estimator": dict(peak_meta),
                    }
            else:
                temperature_trajectory_meta = {
                    "enabled": False,
                    "status": str(
                        (regime_v1.get("na_reason") if isinstance(regime_v1, Mapping) else "")
                        or "regime_v1_unavailable"
                    ),
                    "source": "observed_temperature_trajectory_regime_v1",
                    "source_component_enabled": True,
                    "peak_estimator": dict(peak_meta),
                }
        else:
            (
                temperature_trajectory_probs,
                _unused_temperature_trajectory_atomic_probs,
                temperature_trajectory_meta,
            ) = _temperature_trajectory_channel_probs(
                labels,
                likelihood_features,
                rounding_rule=first_rounding_rule or "rounded_degrees",
            )
            temperature_trajectory_evidence = max(0.0, float(temperature_trajectory_meta.get("evidence") or 0.0))
            temperature_trajectory_evidence_base = max(
                0.0,
                float(temperature_trajectory_meta.get("evidence_base") or 0.0),
            )
            temperature_trajectory_evidence_scale = max(
                0.0,
                float(temperature_trajectory_meta.get("evidence_scale") or 0.0),
            )
            if atomic_temperature_display_enabled:
                (
                    temperature_trajectory_atomic_probs,
                    _unused_temperature_trajectory_atomic_probs_2,
                    temperature_trajectory_atomic_meta,
                ) = _temperature_trajectory_channel_probs(
                    atomic_temperature_labels,
                    likelihood_features,
                    rounding_rule=first_rounding_rule or "rounded_degrees",
                )
            if not any(v > EPS for v in temperature_trajectory_probs):
                temperature_trajectory_evidence = 0.0
                if isinstance(temperature_trajectory_meta, dict):
                    temperature_trajectory_meta["enabled"] = False
                    temperature_trajectory_meta["evidence"] = 0.0

    post_cool_rebound_regime_prior_evidence = 0.0
    post_cool_rebound_regime_prior_evidence_raw = 0.0
    post_cool_rebound_regime_prior_evidence_base = 0.0
    post_cool_rebound_regime_prior_evidence_scale = 0.0
    post_cool_rebound_regime_prior_probs = [0.0] * len(pre_items)
    post_cool_rebound_regime_prior_atomic_probs = [0.0] * len(pre_items)
    post_cool_rebound_regime_prior_no_prior_probs: List[float] = []
    post_cool_rebound_regime_prior_cap_meta: Dict[str, Any] = {}
    post_cool_rebound_regime_prior_meta: Dict[str, Any] = {"enabled": False, "status": "disabled"}
    if all(labels):
        (
            post_cool_rebound_regime_prior_probs,
            _unused_post_cool_regime_prior_atomic_probs,
            post_cool_rebound_regime_prior_meta,
        ) = _post_cool_rebound_channel_probs(
            labels,
            history,
            likelihood_features,
            rounding_rule=first_rounding_rule or "rounded_degrees",
            channel="regime_prior",
        )
        post_cool_rebound_regime_prior_evidence_raw = max(
            0.0,
            float(
                post_cool_rebound_regime_prior_meta.get("evidence_raw")
                or post_cool_rebound_regime_prior_meta.get("evidence")
                or 0.0
            ),
        )
        post_cool_rebound_regime_prior_evidence = post_cool_rebound_regime_prior_evidence_raw
        post_cool_rebound_regime_prior_evidence_base = max(
            0.0,
            float(post_cool_rebound_regime_prior_meta.get("evidence_base") or 0.0),
        )
        post_cool_rebound_regime_prior_evidence_scale = max(
            0.0,
            float(post_cool_rebound_regime_prior_meta.get("evidence_scale") or 0.0),
        )
        if atomic_temperature_display_enabled:
            (
                post_cool_rebound_regime_prior_atomic_probs,
                _unused_post_cool_regime_prior_atomic_probs_2,
                _unused_post_cool_regime_prior_atomic_meta,
            ) = _post_cool_rebound_channel_probs(
                atomic_temperature_labels,
                history,
                likelihood_features,
                rounding_rule=first_rounding_rule or "rounded_degrees",
                channel="regime_prior",
            )
        if not any(v > EPS for v in post_cool_rebound_regime_prior_probs):
            post_cool_rebound_regime_prior_evidence = 0.0
            post_cool_rebound_regime_prior_evidence_raw = 0.0
            if isinstance(post_cool_rebound_regime_prior_meta, dict):
                post_cool_rebound_regime_prior_meta["enabled"] = False
                post_cool_rebound_regime_prior_meta["evidence"] = 0.0
                post_cool_rebound_regime_prior_meta["evidence_raw"] = 0.0

    post_cool_rebound_intraday_likelihood_evidence = 0.0
    post_cool_rebound_intraday_likelihood_evidence_raw = 0.0
    post_cool_rebound_intraday_likelihood_evidence_base = 0.0
    post_cool_rebound_intraday_likelihood_evidence_scale = 0.0
    post_cool_rebound_intraday_likelihood_probs = [0.0] * len(pre_items)
    post_cool_rebound_intraday_likelihood_atomic_probs = [0.0] * len(pre_items)
    post_cool_rebound_intraday_likelihood_no_likelihood_probs: List[float] = []
    post_cool_rebound_intraday_likelihood_cap_meta: Dict[str, Any] = {}
    post_cool_rebound_intraday_likelihood_meta: Dict[str, Any] = {"enabled": False, "status": "disabled"}
    post_cool_rebound_risk_evidence = 0.0
    post_cool_rebound_risk_evidence_raw = 0.0
    post_cool_rebound_risk_evidence_base = 0.0
    post_cool_rebound_risk_evidence_scale = 0.0
    post_cool_rebound_risk_probs = [0.0] * len(pre_items)
    post_cool_rebound_risk_atomic_probs = [0.0] * len(pre_items)
    post_cool_rebound_risk_no_risk_probs: List[float] = []
    post_cool_rebound_risk_cap_meta: Dict[str, Any] = {}
    post_cool_rebound_risk_meta: Dict[str, Any] = {"enabled": False, "status": "disabled"}
    if all(labels):
        (
            post_cool_rebound_intraday_likelihood_probs,
            _unused_post_cool_atomic_probs,
            post_cool_rebound_intraday_likelihood_meta,
        ) = _post_cool_rebound_channel_probs(
            labels,
            history,
            likelihood_features,
            rounding_rule=first_rounding_rule or "rounded_degrees",
            channel="intraday_likelihood",
        )
        post_cool_rebound_intraday_likelihood_evidence_raw = max(
            0.0,
            float(
                post_cool_rebound_intraday_likelihood_meta.get("evidence_raw")
                or post_cool_rebound_intraday_likelihood_meta.get("evidence")
                or 0.0
            ),
        )
        post_cool_rebound_intraday_likelihood_evidence = post_cool_rebound_intraday_likelihood_evidence_raw
        post_cool_rebound_intraday_likelihood_evidence_base = max(
            0.0,
            float(post_cool_rebound_intraday_likelihood_meta.get("evidence_base") or 0.0),
        )
        post_cool_rebound_intraday_likelihood_evidence_scale = max(
            0.0,
            float(post_cool_rebound_intraday_likelihood_meta.get("evidence_scale") or 0.0),
        )
        if atomic_temperature_display_enabled:
            (
                post_cool_rebound_intraday_likelihood_atomic_probs,
                _unused_post_cool_atomic_probs_2,
                _unused_post_cool_atomic_meta,
            ) = _post_cool_rebound_channel_probs(
                atomic_temperature_labels,
                history,
                likelihood_features,
                rounding_rule=first_rounding_rule or "rounded_degrees",
                channel="intraday_likelihood",
            )
        if not any(v > EPS for v in post_cool_rebound_intraday_likelihood_probs):
            post_cool_rebound_intraday_likelihood_evidence = 0.0
            post_cool_rebound_intraday_likelihood_evidence_raw = 0.0
            if isinstance(post_cool_rebound_intraday_likelihood_meta, dict):
                post_cool_rebound_intraday_likelihood_meta["enabled"] = False
                post_cool_rebound_intraday_likelihood_meta["evidence"] = 0.0
                post_cool_rebound_intraday_likelihood_meta["evidence_raw"] = 0.0

    base_without_climo_scores = [
        max(
            EPS,
            prior_strength * prior_probs[idx]
            + model_evidence * likelihood_probs[idx]
            + wu_forecast_evidence * wu_forecast_probs[idx]
            + weather_com_forecast_evidence * weather_com_forecast_probs[idx]
            + nowcast_evidence * nowcast_probs_norm[idx]
            + taf_peak_evidence * taf_peak_probs[idx]
            + temperature_trajectory_evidence * temperature_trajectory_probs[idx]
            + physical_likelihood_evidence * physical_likelihood_probs[idx],
        )
        for idx in range(len(pre_items))
    ]
    if weather_com_forecast_evidence_raw > 0 and any(v > EPS for v in weather_com_forecast_probs):
        base_without_weather_com_scores = [
            max(
                EPS,
                prior_strength * prior_probs[idx]
                + model_evidence * likelihood_probs[idx]
                + wu_forecast_evidence * wu_forecast_probs[idx]
                + nowcast_evidence * nowcast_probs_norm[idx]
                + taf_peak_evidence * taf_peak_probs[idx]
                + temperature_trajectory_evidence * temperature_trajectory_probs[idx]
                + physical_likelihood_evidence * physical_likelihood_probs[idx],
            )
            for idx in range(len(pre_items))
        ]
        weather_com_forecast_cap_meta = cap_climo_evidence_by_pp_delta(
            base_scores=base_without_weather_com_scores,
            climo_probs=weather_com_forecast_probs,
            raw_evidence=weather_com_forecast_evidence_raw,
            max_abs_pp=max(0.0, _env_float("PM_BAYES_WEATHER_COM_FORECAST_PP_CAP", 0.10)),
        )
        weather_com_forecast_evidence = max(
            0.0,
            float(weather_com_forecast_cap_meta.get("evidence") or 0.0),
        )
        weather_com_status = "enabled" if weather_com_forecast_evidence > 0.0 else "zero_scaled"
        if bool(weather_com_forecast_cap_meta.get("cap_hit")):
            weather_com_status = "pp_capped" if weather_com_forecast_evidence > 0.0 else "pp_cap_zero_weight"
        if isinstance(weather_com_forecast_meta, dict):
            weather_com_forecast_meta.update(
                {
                    "enabled": bool(weather_com_forecast_evidence > 0.0),
                    "status": weather_com_status,
                    "evidence": weather_com_forecast_evidence,
                    "evidence_raw": weather_com_forecast_evidence_raw,
                    "pp_cap": dict(weather_com_forecast_cap_meta),
                    "pp_cap_max_abs": weather_com_forecast_cap_meta.get("max_abs_pp"),
                    "pp_max_delta": weather_com_forecast_cap_meta.get("max_abs_delta"),
                    "pp_raw_max_delta": weather_com_forecast_cap_meta.get("raw_max_abs_delta"),
                    "pp_cap_hit": bool(weather_com_forecast_cap_meta.get("cap_hit")),
                }
            )
        base_without_climo_scores = [
            max(
                EPS,
                prior_strength * prior_probs[idx]
                + model_evidence * likelihood_probs[idx]
                + wu_forecast_evidence * wu_forecast_probs[idx]
                + weather_com_forecast_evidence * weather_com_forecast_probs[idx]
                + nowcast_evidence * nowcast_probs_norm[idx]
                + taf_peak_evidence * taf_peak_probs[idx]
                + temperature_trajectory_evidence * temperature_trajectory_probs[idx]
                + physical_likelihood_evidence * physical_likelihood_probs[idx],
            )
            for idx in range(len(pre_items))
        ]
    elif isinstance(weather_com_forecast_meta, dict) and weather_com_forecast_meta.get("enabled"):
        weather_com_forecast_meta.update(
            {
                "enabled": False,
                "status": "zero_scaled",
                "evidence": 0.0,
                "evidence_raw": weather_com_forecast_evidence_raw,
            }
        )
    climo_regime_no_climo_probs = _normalize(base_without_climo_scores)
    if any(v > EPS for v in climo_regime_probs):
        climo_regime_pp_cap_meta = climo_regime_pp_cap(days_to_event=days, features=climo_features)
        climo_regime_cap_meta = cap_climo_evidence_by_pp_delta(
            base_scores=base_without_climo_scores,
            climo_probs=climo_regime_probs,
            raw_evidence=climo_regime_evidence_raw,
            max_abs_pp=float(_to_float(climo_regime_pp_cap_meta.get("max_abs_pp")) or 0.0),
        )
        climo_regime_evidence = max(0.0, float(climo_regime_cap_meta.get("evidence") or 0.0))
        climo_status = "enabled" if climo_regime_evidence > 0.0 else "zero_scaled"
        if bool(climo_regime_cap_meta.get("cap_hit")):
            climo_status = "pp_capped" if climo_regime_evidence > 0.0 else "pp_cap_zero_weight"
        climo_regime_meta.update(
            {
                "enabled": bool(climo_regime_evidence > 0.0),
                "status": climo_status,
                "evidence": climo_regime_evidence,
                "evidence_raw": climo_regime_evidence_raw,
                "pp_cap": climo_regime_pp_cap_meta,
                "pp_cap_max_abs": climo_regime_pp_cap_meta.get("max_abs_pp"),
                "pp_max_delta": climo_regime_cap_meta.get("max_abs_delta"),
                "pp_raw_max_delta": climo_regime_cap_meta.get("raw_max_abs_delta"),
                "pp_cap_hit": bool(climo_regime_cap_meta.get("cap_hit")),
                "temperature_trajectory_priority": True,
            }
        )
    elif isinstance(climo_regime_meta, dict) and climo_regime_meta.get("enabled"):
        climo_regime_meta.update(
            {
                "enabled": False,
                "status": "zero_scaled",
                "evidence": 0.0,
                "evidence_raw": climo_regime_evidence_raw,
                "temperature_trajectory_priority": True,
            }
        )

    base_without_post_cool_regime_prior_scores = [
        max(
            EPS,
            prior_strength * prior_probs[idx]
            + model_evidence * likelihood_probs[idx]
            + wu_forecast_evidence * wu_forecast_probs[idx]
            + weather_com_forecast_evidence * weather_com_forecast_probs[idx]
            + nowcast_evidence * nowcast_probs_norm[idx]
            + taf_peak_evidence * taf_peak_probs[idx]
            + climo_regime_evidence * climo_regime_probs[idx]
            + temperature_trajectory_evidence * temperature_trajectory_probs[idx]
            + physical_likelihood_evidence * physical_likelihood_probs[idx],
        )
        for idx in range(len(pre_items))
    ]
    post_cool_rebound_regime_prior_no_prior_probs = _normalize(base_without_post_cool_regime_prior_scores)
    post_cool_rebound_regime_prior_with_prior_probs = list(post_cool_rebound_regime_prior_no_prior_probs)
    post_cool_rebound_regime_prior_pp_deltas = [0.0] * len(pre_items)
    if any(v > EPS for v in post_cool_rebound_regime_prior_probs):
        prior_features = (
            post_cool_rebound_regime_prior_meta.get("features")
            if isinstance(post_cool_rebound_regime_prior_meta.get("features"), Mapping)
            else {}
        )
        configured_prior_pp_cap = _to_float(post_cool_rebound_regime_prior_meta.get("posterior_lift_pp_cap"))
        if configured_prior_pp_cap is None and isinstance(prior_features, Mapping):
            configured_prior_pp_cap = _to_float(prior_features.get("posterior_lift_pp_cap"))
        if configured_prior_pp_cap is None:
            configured_prior_pp_cap = _env_float("PM_BAYES_POST_COOL_REBOUND_REGIME_PRIOR_PP_CAP", 0.02)
        post_cool_rebound_regime_prior_cap_meta = cap_climo_evidence_by_pp_delta(
            base_scores=base_without_post_cool_regime_prior_scores,
            climo_probs=post_cool_rebound_regime_prior_probs,
            raw_evidence=post_cool_rebound_regime_prior_evidence_raw,
            max_abs_pp=max(0.0, float(configured_prior_pp_cap)),
        )
        post_cool_rebound_regime_prior_evidence = max(
            0.0,
            float(post_cool_rebound_regime_prior_cap_meta.get("evidence") or 0.0),
        )
        if post_cool_rebound_regime_prior_evidence > 0.0:
            post_cool_rebound_regime_prior_with_prior_probs = _normalize(
                [
                    max(
                        EPS,
                        base_without_post_cool_regime_prior_scores[idx]
                        + post_cool_rebound_regime_prior_evidence
                        * post_cool_rebound_regime_prior_probs[idx],
                    )
                    for idx in range(len(pre_items))
                ]
            )
            post_cool_rebound_regime_prior_pp_deltas = [
                post_cool_rebound_regime_prior_with_prior_probs[idx]
                - post_cool_rebound_regime_prior_no_prior_probs[idx]
                for idx in range(len(pre_items))
            ]
        prior_status = "enabled" if post_cool_rebound_regime_prior_evidence > 0.0 else "zero_scaled"
        if bool(post_cool_rebound_regime_prior_cap_meta.get("cap_hit")):
            prior_status = "pp_capped" if post_cool_rebound_regime_prior_evidence > 0.0 else "pp_cap_zero_weight"
        post_cool_rebound_regime_prior_meta.update(
            {
                "enabled": bool(post_cool_rebound_regime_prior_evidence > 0.0),
                "status": prior_status,
                "evidence": post_cool_rebound_regime_prior_evidence,
                "evidence_raw": post_cool_rebound_regime_prior_evidence_raw,
                "pp_cap": dict(post_cool_rebound_regime_prior_cap_meta),
                "pp_cap_max_abs": post_cool_rebound_regime_prior_cap_meta.get("max_abs_pp"),
                "pp_max_delta": post_cool_rebound_regime_prior_cap_meta.get("max_abs_delta"),
                "pp_raw_max_delta": post_cool_rebound_regime_prior_cap_meta.get("raw_max_abs_delta"),
                "pp_cap_hit": bool(post_cool_rebound_regime_prior_cap_meta.get("cap_hit")),
                "hard_source_priority": True,
            }
        )
    elif isinstance(post_cool_rebound_regime_prior_meta, dict) and post_cool_rebound_regime_prior_meta.get("enabled"):
        post_cool_rebound_regime_prior_meta.update(
            {
                "enabled": False,
                "status": "zero_scaled",
                "evidence": 0.0,
                "evidence_raw": post_cool_rebound_regime_prior_evidence_raw,
                "hard_source_priority": True,
            }
        )

    base_without_post_cool_rebound_risk_scores = [
        max(
            EPS,
            prior_strength * prior_probs[idx]
            + model_evidence * likelihood_probs[idx]
            + wu_forecast_evidence * wu_forecast_probs[idx]
            + weather_com_forecast_evidence * weather_com_forecast_probs[idx]
            + nowcast_evidence * nowcast_probs_norm[idx]
            + taf_peak_evidence * taf_peak_probs[idx]
            + climo_regime_evidence * climo_regime_probs[idx]
            + post_cool_rebound_regime_prior_evidence * post_cool_rebound_regime_prior_probs[idx]
            + temperature_trajectory_evidence * temperature_trajectory_probs[idx]
            + physical_likelihood_evidence * physical_likelihood_probs[idx],
        )
        for idx in range(len(pre_items))
    ]
    post_cool_rebound_risk_no_risk_probs = _normalize(base_without_post_cool_rebound_risk_scores)
    post_cool_rebound_intraday_likelihood_no_likelihood_probs = post_cool_rebound_risk_no_risk_probs
    post_cool_rebound_intraday_likelihood_with_likelihood_probs = list(
        post_cool_rebound_intraday_likelihood_no_likelihood_probs
    )
    post_cool_rebound_intraday_likelihood_pp_deltas = [0.0] * len(pre_items)
    if any(v > EPS for v in post_cool_rebound_intraday_likelihood_probs):
        risk_features = (
            post_cool_rebound_intraday_likelihood_meta.get("features")
            if isinstance(post_cool_rebound_intraday_likelihood_meta.get("features"), Mapping)
            else {}
        )
        configured_pp_cap = _to_float(post_cool_rebound_intraday_likelihood_meta.get("posterior_lift_pp_cap"))
        if configured_pp_cap is None and isinstance(risk_features, Mapping):
            configured_pp_cap = _to_float(risk_features.get("posterior_lift_pp_cap"))
        if configured_pp_cap is None:
            configured_pp_cap = _env_float("PM_BAYES_POST_COOL_REBOUND_INTRADAY_LIKELIHOOD_PP_CAP", 0.08)
        post_cool_rebound_intraday_likelihood_cap_meta = cap_climo_evidence_by_pp_delta(
            base_scores=base_without_post_cool_rebound_risk_scores,
            climo_probs=post_cool_rebound_intraday_likelihood_probs,
            raw_evidence=post_cool_rebound_intraday_likelihood_evidence_raw,
            max_abs_pp=max(0.0, float(configured_pp_cap)),
        )
        post_cool_rebound_intraday_likelihood_evidence = max(
            0.0,
            float(post_cool_rebound_intraday_likelihood_cap_meta.get("evidence") or 0.0),
        )
        if post_cool_rebound_intraday_likelihood_evidence > 0.0:
            post_cool_rebound_intraday_likelihood_with_likelihood_probs = _normalize(
                [
                    max(
                        EPS,
                        base_without_post_cool_rebound_risk_scores[idx]
                        + post_cool_rebound_intraday_likelihood_evidence
                        * post_cool_rebound_intraday_likelihood_probs[idx],
                    )
                    for idx in range(len(pre_items))
                ]
            )
            post_cool_rebound_intraday_likelihood_pp_deltas = [
                post_cool_rebound_intraday_likelihood_with_likelihood_probs[idx]
                - post_cool_rebound_intraday_likelihood_no_likelihood_probs[idx]
                for idx in range(len(pre_items))
            ]
        risk_status = "enabled" if post_cool_rebound_intraday_likelihood_evidence > 0.0 else "zero_scaled"
        if bool(post_cool_rebound_intraday_likelihood_cap_meta.get("cap_hit")):
            risk_status = "pp_capped" if post_cool_rebound_intraday_likelihood_evidence > 0.0 else "pp_cap_zero_weight"
        post_cool_rebound_intraday_likelihood_meta.update(
            {
                "enabled": bool(post_cool_rebound_intraday_likelihood_evidence > 0.0),
                "status": risk_status,
                "evidence": post_cool_rebound_intraday_likelihood_evidence,
                "evidence_raw": post_cool_rebound_intraday_likelihood_evidence_raw,
                "pp_cap": dict(post_cool_rebound_intraday_likelihood_cap_meta),
                "pp_cap_max_abs": post_cool_rebound_intraday_likelihood_cap_meta.get("max_abs_pp"),
                "pp_max_delta": post_cool_rebound_intraday_likelihood_cap_meta.get("max_abs_delta"),
                "pp_raw_max_delta": post_cool_rebound_intraday_likelihood_cap_meta.get("raw_max_abs_delta"),
                "pp_cap_hit": bool(post_cool_rebound_intraday_likelihood_cap_meta.get("cap_hit")),
                "hard_source_priority": True,
            }
        )
    elif isinstance(post_cool_rebound_intraday_likelihood_meta, dict) and post_cool_rebound_intraday_likelihood_meta.get("enabled"):
        post_cool_rebound_intraday_likelihood_meta.update(
            {
                "enabled": False,
                "status": "zero_scaled",
                "evidence": 0.0,
                "evidence_raw": post_cool_rebound_intraday_likelihood_evidence_raw,
                "hard_source_priority": True,
            }
        )
    post_cool_rebound_risk_evidence = post_cool_rebound_intraday_likelihood_evidence
    post_cool_rebound_risk_evidence_raw = post_cool_rebound_intraday_likelihood_evidence_raw
    post_cool_rebound_risk_evidence_base = post_cool_rebound_intraday_likelihood_evidence_base
    post_cool_rebound_risk_evidence_scale = post_cool_rebound_intraday_likelihood_evidence_scale
    post_cool_rebound_risk_probs = post_cool_rebound_intraday_likelihood_probs
    post_cool_rebound_risk_atomic_probs = post_cool_rebound_intraday_likelihood_atomic_probs
    post_cool_rebound_risk_cap_meta = post_cool_rebound_intraday_likelihood_cap_meta
    post_cool_rebound_risk_meta = post_cool_rebound_intraday_likelihood_meta

    posterior_probs = list(likelihood_probs)
    posterior_probs_pre_hard_source_override = list(posterior_probs)
    alpha_total = 1.0
    alphas: List[float] = [max(EPS, float(prob)) for prob in posterior_probs]
    base_no_physical_probs = _normalize(likelihood_probs)
    physical_d0_explanation = _build_physical_d0_explanation(
        labels=labels,
        rounding_rule=first_rounding_rule,
        features=likelihood_features,
        physical_likelihood_meta=physical_likelihood_meta,
        base_no_physical_probs=base_no_physical_probs,
        physical_likelihood_evidence=float(physical_likelihood_evidence),
        alpha_total=float(alpha_total),
        z_stage=z_meta.get("stage"),
        days_to_event=days_to_event,
    )
    atomic_temperature_alphas: List[float] = []
    atomic_temperature_posterior_probs: List[Optional[float]] = [None] * len(pre_items)
    atomic_temperature_alpha_total = 0.0
    if atomic_temperature_display_enabled:
        atomic_temperature_posterior_probs = list(atomic_likelihood_probs or atomic_prior_probs)
        atomic_temperature_alphas = [max(EPS, float(prob)) for prob in atomic_temperature_posterior_probs]
        atomic_temperature_alpha_total = max(1.0, float(sum(atomic_temperature_alphas)))
    atomic_temperature_posterior_pre_hard_source_override = list(atomic_temperature_posterior_probs)

    hard_source_override_meta = resolve_hard_source_posterior_override(
        labels,
        likelihood_features,
        base_probs=list(posterior_probs),
        rounding_rule=first_rounding_rule,
        market_var=_feature_text(likelihood_features, "market_var"),
        event_tz=lcb_stage_tz or _feature_text(likelihood_features, "event_tz", "tz", "timezone"),
        event_date=_feature_date_text(likelihood_features, "event_date", "target_event_date", "run_date"),
    )
    hard_source_override_applied = bool(hard_source_override_meta.get("applied"))
    if hard_source_override_applied:
        override_probs = hard_source_override_meta.get("posterior_probs")
        if isinstance(override_probs, list) and len(override_probs) == len(pre_items):
            posterior_probs = [float(v) for v in override_probs]
            alphas = [float(v) for v in posterior_probs]
            alpha_total = 1.0
            if atomic_temperature_display_enabled:
                if bool(hard_source_override_meta.get("final_bucket_lock")):
                    target_idx = int(hard_source_override_meta.get("target_index") or 0)
                    atomic_temperature_posterior_probs = [
                        1.0 if idx == target_idx else 0.0
                        for idx in range(len(pre_items))
                    ]
                else:
                    atomic_temperature_posterior_probs = [float(v) for v in override_probs]
                atomic_temperature_alphas = [float(v) for v in atomic_temperature_posterior_probs]
                atomic_temperature_alpha_total = 1.0

    observed_today_max_c, observed_today_max_source = _first_feature_number(
        likelihood_features,
        [
            "observed_today_max_c",
            "daily_max_observed_c",
            "zuuu_metar_raw_awc_today_max_c",
            "wu_observed_today_max_c",
            "twc_observed_today_max_c",
            "settlement_source_observed_today_max_c",
        ],
    )
    hard_source_value_c = _to_float(hard_source_override_meta.get("value_c"))
    if hard_source_override_applied and hard_source_value_c is not None:
        observed_today_max_c = (
            max(float(observed_today_max_c), float(hard_source_value_c))
            if observed_today_max_c is not None
            else float(hard_source_value_c)
        )
        observed_today_max_source = str(hard_source_override_meta.get("source") or observed_today_max_source)

    ctp_components: List[Dict[str, Any]] = []
    ctp_likelihood_updates: List[Dict[str, Any]] = []

    def add_ctp_component(component: Dict[str, Any]) -> None:
        ctp_components.append(component)

    def add_ctp_likelihood_update(update: Dict[str, Any]) -> None:
        ctp_likelihood_updates.append(update)

    def ctp_component_precision_from_sigma(
        *,
        evidence: float,
        raw_evidence: Optional[float],
        sigma_c: Optional[Any],
        fallback_precision: float = 0.5,
    ) -> float:
        evidence_value = max(0.0, float(evidence or 0.0))
        if evidence_value <= 0.0:
            return 0.0
        raw_value = max(0.0, float(raw_evidence if raw_evidence is not None else evidence_value))
        shrink = min(1.0, evidence_value / raw_value) if raw_value > 0.0 else 1.0
        if raw_value > evidence_value > 0.0:
            shrink *= shrink
        sigma_value = _to_float(sigma_c)
        if sigma_value is not None and sigma_value > 0.0:
            base_precision = 1.0 / max(0.05, float(sigma_value)) ** 2
        else:
            base_precision = max(0.0, float(fallback_precision))
        return max(0.0, min(8.0, base_precision) * shrink)

    if prior_strength > 0.0:
        add_ctp_component(
            {
                "source_id": "climate_prior",
                "group": "climate_prior",
                "role": "prior",
                "bucket_probs": list(prior_probs),
                "precision": max(
                    0.05,
                    min(
                        2.0,
                        (float(history.get("prior_sample_scale") or 0.5) if history else 0.5)
                        * min(1.0, prior_strength / 12.0),
                    ),
                ),
                "source": prior_source,
            }
        )
    if model_evidence > 0.0:
        add_ctp_component(
            {
                "source_id": "model_ensemble",
                "group": "soft_forecast",
                "role": "calibrated_forecast_likelihood",
                "bucket_probs": list(likelihood_probs),
                "precision": max(
                    0.10,
                    float(freshness)
                    * max(1.0, float(model_count_max or 1))
                    * min(1.0, model_evidence / max(1.0, model_strength)),
                )
                * ctp_model_ensemble_precision_scale,
                "source": "model_ensemble_weighted_probability",
                "confidence": freshness,
            }
        )
    if nowcast_evidence > 0.0 and any(v > EPS for v in nowcast_probs_norm):
        add_ctp_component(
            {
                "source_id": "observed_nowcast",
                "group": "observation_nowcast",
                "role": "intraday_observation_update",
                "bucket_probs": list(nowcast_probs_norm),
                "precision": max(0.10, min(2.0, 0.5 + nowcast_share_max))
                * min(1.0, nowcast_evidence / max(1.0, nowcast_strength)),
                "source": "observed_nowcast",
            }
        )

    invalid_component_status_prefixes = (
        "missing_",
        "scope_not_allowed",
        "disabled",
        "taf_forecast_reference_only",
        "taf_pending",
    )

    def valid_soft_status(meta: Mapping[str, Any]) -> bool:
        status = str(meta.get("status") or "")
        if not status:
            return False
        return not any(status.startswith(prefix) for prefix in invalid_component_status_prefixes)

    if wu_forecast_evidence > 0.0 and valid_soft_status(wu_forecast_meta) and wu_forecast_meta.get("peak_c") is not None:
        add_ctp_component(
            {
                "source_id": "wu_forecast",
                "group": "soft_forecast",
                "role": "calibrated_forecast_likelihood",
                "bucket_probs": list(wu_forecast_probs),
                "precision": ctp_component_precision_from_sigma(
                    evidence=wu_forecast_evidence,
                    raw_evidence=wu_forecast_evidence,
                    sigma_c=wu_forecast_meta.get("sigma_c"),
                    fallback_precision=0.5,
                )
                * ctp_wu_forecast_precision_scale,
                "source": wu_forecast_meta.get("source"),
                "calibrated_peak_c": wu_forecast_meta.get("peak_c"),
                "sigma_c": wu_forecast_meta.get("sigma_c"),
            }
        )
    if (
        weather_com_forecast_evidence > 0.0
        and valid_soft_status(weather_com_forecast_meta)
        and weather_com_forecast_meta.get("peak_c") is not None
    ):
        add_ctp_component(
            {
                "source_id": "weather_com_forecast",
                "group": "soft_forecast",
                "role": "calibrated_forecast_likelihood",
                "bucket_probs": list(weather_com_forecast_probs),
                "precision": ctp_component_precision_from_sigma(
                    evidence=weather_com_forecast_evidence,
                    raw_evidence=weather_com_forecast_evidence_raw,
                    sigma_c=weather_com_forecast_meta.get("sigma_c"),
                    fallback_precision=0.5,
                )
                * ctp_weather_com_forecast_precision_scale,
                "source": weather_com_forecast_meta.get("source"),
                "calibrated_peak_c": weather_com_forecast_meta.get("peak_c"),
                "sigma_c": weather_com_forecast_meta.get("sigma_c"),
            }
        )
    if taf_peak_evidence > 0.0 and valid_soft_status(taf_peak_meta) and taf_peak_meta.get("peak_c") is not None:
        add_ctp_component(
            {
                "source_id": "taf_peak",
                "group": "soft_forecast",
                "role": "calibrated_forecast_likelihood",
                "bucket_probs": list(taf_peak_probs),
                "precision": ctp_component_precision_from_sigma(
                    evidence=taf_peak_evidence,
                    raw_evidence=taf_peak_evidence,
                    sigma_c=taf_peak_meta.get("sigma_c"),
                    fallback_precision=0.5,
                )
                * ctp_taf_peak_precision_scale,
                "source": taf_peak_meta.get("source"),
                "calibrated_peak_c": taf_peak_meta.get("peak_c"),
                "sigma_c": taf_peak_meta.get("sigma_c"),
            }
        )
    if climo_regime_evidence > 0.0 and any(v > EPS for v in climo_regime_probs):
        add_ctp_component(
            {
                "source_id": "climo_regime",
                "group": "climate_prior",
                "role": "conditional_climate_prior",
                "bucket_probs": list(climo_regime_probs),
                "precision": 0.5,
                "source": climo_regime_meta.get("source"),
            }
        )
    if temperature_trajectory_evidence > 0.0 and any(v > EPS for v in temperature_trajectory_probs):
        if temperature_trajectory_uses_regime_v1_source_component:
            add_ctp_component(
                {
                    "source_id": "temperature_trajectory",
                    "group": "temperature_trajectory",
                    "role": "observed_realization",
                    "mean_c": temperature_trajectory_meta.get("path_mu_c")
                    or temperature_trajectory_meta.get("mean_c")
                    or temperature_trajectory_meta.get("mu_c"),
                    "sigma_c": temperature_trajectory_meta.get("path_sigma_c")
                    or temperature_trajectory_meta.get("sigma_c"),
                    "precision": float(temperature_trajectory_evidence),
                    "source": "observed_temperature_trajectory_regime_v1",
                    "path_mu_c": temperature_trajectory_meta.get("path_mu_c"),
                    "path_sigma_c": temperature_trajectory_meta.get("path_sigma_c"),
                    "evidence": temperature_trajectory_meta.get("evidence"),
                    "evidence_base": temperature_trajectory_meta.get("evidence_base"),
                    "evidence_scale": temperature_trajectory_meta.get("evidence_scale"),
                    "momentum_evidence_scale": temperature_trajectory_meta.get("momentum_evidence_scale"),
                    "confidence_evidence_scale": temperature_trajectory_meta.get("confidence_evidence_scale"),
                    "forcing_evidence_scale": temperature_trajectory_meta.get("forcing_evidence_scale"),
                    "kinematic_baseline_mu": temperature_trajectory_meta.get("kinematic_baseline_mu"),
                    "regime_probs": temperature_trajectory_meta.get("regime_probs"),
                    "rate_hat_c_per_hour": temperature_trajectory_meta.get("rate_hat_c_per_hour"),
                    "accel_hat_c_per_hour2": temperature_trajectory_meta.get("accel_hat_c_per_hour2"),
                    "strong_warming_confirmed": temperature_trajectory_meta.get("strong_warming_confirmed"),
                    "one_hour_context_confirmed": temperature_trajectory_meta.get("one_hour_context_confirmed"),
                    "warming_acceleration_confirmed_by_context": temperature_trajectory_meta.get(
                        "warming_acceleration_confirmed_by_context"
                    ),
                    "positive_context_window_count": temperature_trajectory_meta.get("positive_context_window_count"),
                    "context_confirmed_windows": temperature_trajectory_meta.get("context_confirmed_windows"),
                    "forcing_turn_probability": temperature_trajectory_meta.get("forcing_turn_probability"),
                    "dedupe_policy": temperature_trajectory_meta.get("dedupe_policy"),
                    "regime_model_status": temperature_trajectory_meta.get("regime_model_status"),
                }
            )
        else:
            trajectory_precision = ctp_component_precision_from_sigma(
                evidence=temperature_trajectory_evidence,
                raw_evidence=temperature_trajectory_evidence_base,
                sigma_c=temperature_trajectory_meta.get("sigma_c"),
                fallback_precision=0.75,
            )
            trajectory_strength = max(
                0.05,
                min(1.5, _to_float(temperature_trajectory_meta.get("evidence_scale")) or 0.0),
            )
            add_ctp_likelihood_update(
                {
                    "source_id": "temperature_trajectory",
                    "group": "intraday_path",
                    "role": "intraday_path_likelihood_update",
                    "kind": "trajectory_reachability",
                    "strength": trajectory_strength,
                    "precision": trajectory_precision,
                    "source": temperature_trajectory_meta.get("source"),
                    "observed_temp_c": temperature_trajectory_meta.get("observed_temp_c"),
                    "observed_today_max_c": observed_today_max_c,
                    "rate_c_per_hour": temperature_trajectory_meta.get("rate_c_per_hour"),
                    "hours_to_peak_window": temperature_trajectory_meta.get("hours_to_peak_window"),
                    "rate_sigma_c_per_hour": _env_float("PM_BAYES_CTP_TRAJECTORY_RATE_SIGMA_C_PER_H", 0.35),
                    "min_projection_hours": _env_float("PM_BAYES_CTP_TRAJECTORY_MIN_PROJECTION_HOURS", 0.25),
                    "max_projection_hours": _env_float("PM_BAYES_CTP_TRAJECTORY_MAX_PROJECTION_HOURS", 3.5),
                    "evidence": temperature_trajectory_evidence,
                    "evidence_base": temperature_trajectory_evidence_base,
                    "evidence_scale": temperature_trajectory_evidence_scale,
                    "legacy_bucket_probs": list(temperature_trajectory_probs),
                    "legacy_mu_c": temperature_trajectory_meta.get("mu_c"),
                    "legacy_sigma_c": temperature_trajectory_meta.get("sigma_c"),
                    "realization_state": temperature_trajectory_meta.get("realization_state"),
                }
            )
    if post_cool_rebound_regime_prior_evidence > 0.0 and any(v > EPS for v in post_cool_rebound_regime_prior_probs):
        add_ctp_component(
            {
                "source_id": "post_cool_rebound_regime_prior",
                "group": "climate_prior",
                "role": "conditional_climate_prior",
                "bucket_probs": list(post_cool_rebound_regime_prior_probs),
                "mean_c": post_cool_rebound_regime_prior_meta.get("mu_c")
                or post_cool_rebound_regime_prior_meta.get("target_peak_c"),
                "sigma_c": post_cool_rebound_regime_prior_meta.get("sigma_c"),
                "precision": 0.5,
                "source": post_cool_rebound_regime_prior_meta.get("source"),
            }
        )
    if (
        post_cool_rebound_intraday_likelihood_evidence > 0.0
        and any(v > EPS for v in post_cool_rebound_intraday_likelihood_probs)
    ):
        add_ctp_component(
            {
                "source_id": "post_cool_rebound_intraday_likelihood",
                "group": "intraday_path",
                "role": "intraday_likelihood",
                "bucket_probs": list(post_cool_rebound_intraday_likelihood_probs),
                "mean_c": post_cool_rebound_intraday_likelihood_meta.get("mu_c")
                or post_cool_rebound_intraday_likelihood_meta.get("target_peak_c"),
                "sigma_c": post_cool_rebound_intraday_likelihood_meta.get("sigma_c"),
                "precision": ctp_component_precision_from_sigma(
                    evidence=post_cool_rebound_intraday_likelihood_evidence,
                    raw_evidence=post_cool_rebound_intraday_likelihood_evidence_raw,
                    sigma_c=post_cool_rebound_intraday_likelihood_meta.get("sigma_c"),
                    fallback_precision=0.75,
                ),
                "source": post_cool_rebound_intraday_likelihood_meta.get("source"),
            }
        )
    if physical_likelihood_evidence > 0.0 and any(v > EPS for v in physical_likelihood_probs):
        physical_strength = max(0.05, min(1.5, float(physical_likelihood_evidence_scale or 0.0)))
        add_ctp_likelihood_update(
            {
                "source_id": "independent_physics_residual",
                "group": "physical_residual",
                "role": "physical_residual_likelihood_update",
                "kind": "bucket_likelihood",
                "labels": list(labels),
                "probabilities": list(physical_likelihood_probs),
                "strength": physical_strength,
                "precision": 0.75,
                "source": physical_likelihood_meta.get("source"),
                "evidence": physical_likelihood_evidence,
                "evidence_base": physical_likelihood_evidence_base,
                "evidence_scale": physical_likelihood_evidence_scale,
            }
        )

    ctp_source_group_precision_cap = max(
        0.0,
        _env_float("PM_BAYES_CTP_SOURCE_GROUP_PRECISION_CAP", 8.0),
    )

    ctp_pre_constraints = build_temperature_posterior_distribution(
        labels,
        rounding_rule=first_rounding_rule,
        source_components=ctp_components,
        likelihood_updates=ctp_likelihood_updates,
        observed_today_max_c=None,
        precision_cap=ctp_source_group_precision_cap,
        event_key={
            "event_date": _feature_date_text(likelihood_features, "event_date", "target_event_date", "run_date"),
            "station_id": _feature_text(likelihood_features, "station_id", "weather_station_id"),
            "slug": _feature_text(likelihood_features, "slug", "event_slug"),
        },
    )
    ctp_posterior = build_temperature_posterior_distribution(
        labels,
        rounding_rule=first_rounding_rule,
        source_components=ctp_components,
        likelihood_updates=ctp_likelihood_updates,
        observed_today_max_c=observed_today_max_c,
        precision_cap=ctp_source_group_precision_cap,
        hard_lock_bucket=(
            str(hard_source_override_meta.get("target_bucket") or "")
            if bool(hard_source_override_meta.get("final_bucket_lock"))
            else None
        ),
        hard_lock_temp_c=(
            hard_source_override_meta.get("value_c")
            if bool(hard_source_override_meta.get("final_bucket_lock"))
            else None
        ),
        event_key={
            "event_date": _feature_date_text(likelihood_features, "event_date", "target_event_date", "run_date"),
            "station_id": _feature_text(likelihood_features, "station_id", "weather_station_id"),
            "slug": _feature_text(likelihood_features, "slug", "event_slug"),
        },
    )
    ctp_probs_by_label = bucket_probabilities_by_label(ctp_posterior)
    ctp_probs = _normalize([float(ctp_probs_by_label.get(label, 0.0) or 0.0) for label in labels])
    ctp_pre_probs_by_label = bucket_probabilities_by_label(ctp_pre_constraints)
    ctp_pre_probs = _normalize([float(ctp_pre_probs_by_label.get(label, 0.0) or 0.0) for label in labels])
    posterior_probs = list(ctp_probs)
    posterior_probs_pre_hard_source_override = list(ctp_pre_probs)
    ctp_group_rows = (
        (ctp_posterior.get("source_components") or {}).get("groups")
        if isinstance(ctp_posterior.get("source_components"), Mapping)
        else []
    )
    ctp_effective_precision = 0.0
    ctp_component_precisions: Dict[str, float] = {
        "history": 0.0,
        "model": 0.0,
        "wu_forecast": 0.0,
        "weather_com_forecast": 0.0,
        "taf_peak": 0.0,
        "climo_regime": 0.0,
        "post_cool_rebound_regime_prior": 0.0,
        "observed_nowcast": 0.0,
        "temperature_trajectory": 0.0,
        "post_cool_rebound_intraday_likelihood": 0.0,
        "independent_physics_residual": 0.0,
    }
    ctp_component_aliases = {
        "climate_prior": "history",
        "model_ensemble": "model",
    }
    if isinstance(ctp_group_rows, list):
        for row in ctp_group_rows:
            if isinstance(row, Mapping):
                group_effective_precision = max(0.0, _to_float(row.get("effective_precision")) or 0.0)
                ctp_effective_precision += group_effective_precision
                group_raw_precision = max(0.0, _to_float(row.get("raw_precision")) or 0.0)
                members = row.get("members")
                if not isinstance(members, list) or group_effective_precision <= 0.0 or group_raw_precision <= 0.0:
                    continue
                for member in members:
                    if not isinstance(member, Mapping):
                        continue
                    source_id = str(member.get("source_id") or "").strip()
                    if not source_id:
                        continue
                    key = ctp_component_aliases.get(source_id, source_id)
                    member_precision = max(0.0, _to_float(member.get("precision")) or 0.0)
                    ctp_component_precisions[key] = ctp_component_precisions.get(key, 0.0) + (
                        group_effective_precision * member_precision / group_raw_precision
                    )
    ctp_update_rows = (
        (ctp_posterior.get("likelihood_updates") or {}).get("updates")
        if isinstance(ctp_posterior.get("likelihood_updates"), Mapping)
        else []
    )
    if isinstance(ctp_update_rows, list):
        for row in ctp_update_rows:
            if not isinstance(row, Mapping):
                continue
            update_precision = max(0.0, _to_float(row.get("effective_precision")) or 0.0)
            if update_precision <= 0.0:
                continue
            source_id = str(row.get("source_id") or "").strip()
            if not source_id:
                continue
            key = ctp_component_aliases.get(source_id, source_id)
            ctp_effective_precision += update_precision
            ctp_component_precisions[key] = ctp_component_precisions.get(key, 0.0) + update_precision
    alpha_total = max(1.0, float(ctp_effective_precision))
    alphas = [max(0.0, p * alpha_total) for p in posterior_probs]
    temperature_trajectory_path_mu_c = _to_float(
        temperature_trajectory_meta.get("path_mu_c")
        if temperature_trajectory_meta.get("path_mu_c") is not None
        else temperature_trajectory_meta.get("mu_c")
    )
    temperature_trajectory_path_sigma_c = _to_float(
        temperature_trajectory_meta.get("path_sigma_c")
        if temperature_trajectory_meta.get("path_sigma_c") is not None
        else temperature_trajectory_meta.get("sigma_c")
    )
    temperature_trajectory_rate_hat_c_per_hour = _to_float(
        temperature_trajectory_meta.get("rate_hat_c_per_hour")
        if temperature_trajectory_meta.get("rate_hat_c_per_hour") is not None
        else temperature_trajectory_meta.get("rate_c_per_hour")
    )
    temperature_trajectory_accel_hat_c_per_hour2 = _to_float(
        temperature_trajectory_meta.get("accel_hat_c_per_hour2")
    )
    temperature_trajectory_component_detail = {
        "source": str(temperature_trajectory_meta.get("source") or ""),
        "status": str(temperature_trajectory_meta.get("status") or ""),
        "path_mu_c": temperature_trajectory_path_mu_c,
        "path_sigma_c": temperature_trajectory_path_sigma_c,
        "evidence": temperature_trajectory_evidence,
        "evidence_base": temperature_trajectory_evidence_base,
        "evidence_scale": temperature_trajectory_evidence_scale,
        "rate_hat_c_per_hour": temperature_trajectory_rate_hat_c_per_hour,
        "rate_c_per_hour": temperature_trajectory_rate_hat_c_per_hour,
        "accel_hat_c_per_hour2": temperature_trajectory_accel_hat_c_per_hour2,
        "realization_state": temperature_trajectory_meta.get("realization_state"),
        "hours_to_peak_window": temperature_trajectory_meta.get("hours_to_peak_window"),
        "regime_model_status": temperature_trajectory_meta.get("regime_model_status"),
        "strong_warming_confirmed": temperature_trajectory_meta.get("strong_warming_confirmed"),
        "one_hour_context_confirmed": temperature_trajectory_meta.get("one_hour_context_confirmed"),
        "positive_context_window_count": temperature_trajectory_meta.get("positive_context_window_count"),
        "context_confirmed_windows": temperature_trajectory_meta.get("context_confirmed_windows"),
        "forcing_turn_probability": temperature_trajectory_meta.get("forcing_turn_probability"),
        "forcing_onset_probs": temperature_trajectory_meta.get("forcing_onset_probs"),
        "forcing_contributors": temperature_trajectory_meta.get("forcing_contributors"),
        "regime_probs": temperature_trajectory_meta.get("regime_probs"),
    }
    if atomic_temperature_display_enabled:
        ctp_atomic = build_temperature_posterior_distribution(
            atomic_temperature_labels,
            rounding_rule=first_rounding_rule,
            source_components=ctp_components,
            likelihood_updates=ctp_likelihood_updates,
            observed_today_max_c=observed_today_max_c,
            precision_cap=ctp_source_group_precision_cap,
            hard_lock_bucket=(
                str(hard_source_override_meta.get("target_bucket") or "")
                if bool(hard_source_override_meta.get("final_bucket_lock"))
                else None
            ),
            hard_lock_temp_c=(
                hard_source_override_meta.get("value_c")
                if bool(hard_source_override_meta.get("final_bucket_lock"))
                else None
            ),
        )
        ctp_atomic_probs_by_label = bucket_probabilities_by_label(ctp_atomic)
        atomic_temperature_posterior_probs = [
            float(ctp_atomic_probs_by_label.get(label, posterior_probs[idx]) or 0.0)
            for idx, label in enumerate(atomic_temperature_labels)
        ]
        atomic_temperature_posterior_pre_hard_source_override = list(atomic_temperature_posterior_probs)
        atomic_temperature_alphas = [
            max(0.0, float(v) * alpha_total)
            for v in atomic_temperature_posterior_probs
        ]
        atomic_temperature_alpha_total = alpha_total

    for idx, item in enumerate(pre_items):
        q_obj = item.get("q")
        if not isinstance(q_obj, dict):
            continue
        posterior = float(posterior_probs[idx])
        if bool(hard_source_override_meta.get("final_bucket_lock")):
            std = 0.0
            lcb = posterior
            ucb = posterior
        else:
            std = math.sqrt(max(0.0, posterior * (1.0 - posterior) / (alpha_total + 1.0)))
            lcb = max(0.0, posterior - z_value * std)
            ucb = min(1.0, posterior + z_value * std)
        q_obj["q_raw_static"] = q_obj.get("q_raw")
        q_obj["q_round_static"] = q_obj.get("q_round")
        if q_obj.get("q_raw_static") is not None:
            q_raw_static = _clamp01(_to_float(q_obj.get("q_raw_static")))
            if q_raw_static is not None:
                q_obj["q_raw_static_no"] = 1.0 - q_raw_static
        q_obj["q_raw"] = posterior
        q_obj["q_round"] = posterior
        q_obj["q_raw_no"] = 1.0 - posterior
        q_obj["q_round_no"] = 1.0 - posterior
        q_obj["q_bayes_posterior"] = posterior
        q_obj["q_bayes_lcb"] = lcb
        q_obj["q_bayes_ucb"] = ucb
        q_obj["q_bayes_sigma"] = std
        q_obj["q_bayes_alpha"] = alphas[idx]
        q_obj["q_bayes_alpha_total"] = alpha_total
        q_obj["q_bayes_posterior_no"] = 1.0 - posterior
        q_obj["q_bayes_lcb_no"] = max(0.0, 1.0 - ucb)
        q_obj["q_bayes_ucb_no"] = min(1.0, 1.0 - lcb)
        q_obj["q_bayes_sigma_no"] = std
        if hard_source_override_applied:
            q_obj["hard_source_posterior_override"] = True
            q_obj["hard_source_posterior_override_applied"] = True
            q_obj["hard_source_posterior_override_source"] = hard_source_override_meta.get("source")
            q_obj["hard_source_posterior_override_value_c"] = hard_source_override_meta.get("value_c")
            q_obj["hard_source_posterior_override_target_bucket"] = hard_source_override_meta.get("target_bucket")
            q_obj["hard_source_posterior_override_constraint_mode"] = hard_source_override_meta.get("constraint_mode")
            q_obj["hard_source_posterior_override_final_bucket_lock"] = bool(hard_source_override_meta.get("final_bucket_lock"))
            q_obj["q_bayes_posterior_pre_hard_source_override"] = (
                posterior_probs_pre_hard_source_override[idx]
                if idx < len(posterior_probs_pre_hard_source_override)
                else None
            )
        if wu_forecast_evidence > 0 and idx < len(wu_forecast_probs):
            item["q_wu_forecast_peak"] = wu_forecast_probs[idx]
        if weather_com_forecast_evidence > 0 and idx < len(weather_com_forecast_probs):
            item["q_weather_com_forecast_peak"] = weather_com_forecast_probs[idx]
        if taf_peak_evidence > 0 and idx < len(taf_peak_probs):
            item["q_taf_peak"] = taf_peak_probs[idx]
        if temperature_trajectory_evidence > 0 and idx < len(temperature_trajectory_probs):
            item["q_temperature_trajectory"] = temperature_trajectory_probs[idx]
        if post_cool_rebound_regime_prior_evidence > 0 and idx < len(post_cool_rebound_regime_prior_probs):
            item["q_post_cool_rebound_regime_prior"] = post_cool_rebound_regime_prior_probs[idx]
        if (
            post_cool_rebound_intraday_likelihood_evidence > 0
            and idx < len(post_cool_rebound_intraday_likelihood_probs)
        ):
            item["q_post_cool_rebound_intraday_likelihood"] = post_cool_rebound_intraday_likelihood_probs[idx]
        if post_cool_rebound_risk_evidence > 0 and idx < len(post_cool_rebound_risk_probs):
            item["q_post_cool_rebound_risk"] = post_cool_rebound_risk_probs[idx]
        atomic_temperature_posterior = atomic_temperature_posterior_probs[idx]
        if atomic_temperature_posterior is not None:
            item["q_temperature_bucket_posterior"] = atomic_temperature_posterior
            item["q_temperature_bucket_probability_mode"] = "listed_atomic_temperature_conditional"
            item["q_temperature_bucket_label"] = atomic_temperature_labels[idx]
            item["q_contract_payout_posterior"] = posterior
            if hard_source_override_applied:
                item["q_temperature_bucket_probability_mode"] = "hard_source_posterior_override"
                item["q_temperature_bucket_posterior_pre_hard_source_override"] = (
                    atomic_temperature_posterior_pre_hard_source_override[idx]
                    if idx < len(atomic_temperature_posterior_pre_hard_source_override)
                    else None
                )
        score_components = {
            key: max(0.0, float(effective_precision)) * posterior
            for key, effective_precision in ctp_component_precisions.items()
        }
        score_component_details = {
            "temperature_trajectory": {
                **temperature_trajectory_component_detail,
                "prob": (
                    temperature_trajectory_probs[idx]
                    if temperature_trajectory_evidence > 0 and idx < len(temperature_trajectory_probs)
                    else None
                ),
                "component": score_components.get("temperature_trajectory"),
            }
        }
        physical_likelihood_rows = (
            physical_likelihood_meta.get("rows")
            if isinstance(physical_likelihood_meta, Mapping)
            and isinstance(physical_likelihood_meta.get("rows"), list)
            else []
        )
        physical_likelihood_row = (
            physical_likelihood_rows[idx]
            if idx < len(physical_likelihood_rows)
            and isinstance(physical_likelihood_rows[idx], Mapping)
            else None
        )
        physical_likelihood_observed_prob = (
            physical_likelihood_probs[idx]
            if idx < len(physical_likelihood_probs)
            else None
        )
        physical_likelihood_component_groups = (
            _compact_physical_likelihood_component_groups(physical_likelihood_row.get("component_groups"))
            if isinstance(physical_likelihood_row, Mapping)
            else {}
        )
        physical_likelihood_components = (
            _compact_physical_likelihood_components(physical_likelihood_row.get("components"))
            if isinstance(physical_likelihood_row, Mapping)
            else {}
        )
        physical_likelihood_hard_zero = bool(
            physical_likelihood_row.get("hard_zero")
            if isinstance(physical_likelihood_row, Mapping)
            else False
        )
        physical_likelihood_hard_reasons = (
            [
                str(reason or "").strip()
                for reason in physical_likelihood_row.get("hard_reasons", [])
                if str(reason or "").strip()
            ]
            if isinstance(physical_likelihood_row, Mapping)
            and isinstance(physical_likelihood_row.get("hard_reasons"), list)
            else []
        )
        item["q_bayes_meta"] = {
            "enabled": True,
            "source": f"{CTP_VERSION}_hard_source_override" if hard_source_override_applied else CTP_VERSION,
            "posterior_engine": CTP_VERSION,
            "posterior_temperature_distribution_version": CTP_VERSION,
            "posterior_temperature_distribution_status": ctp_posterior.get("status"),
            "posterior_temperature_distribution_observed_today_max_c": observed_today_max_c,
            "posterior_temperature_distribution_observed_today_max_source": observed_today_max_source,
            "posterior_temperature_distribution_d0_violation_count": (
                (ctp_posterior.get("observation_constraints") or {}).get("d0_violation_count")
                if isinstance(ctp_posterior.get("observation_constraints"), Mapping)
                else None
            ),
            "posterior_temperature_distribution_mean_c": ctp_posterior.get("mean_c"),
            "posterior_temperature_distribution_sigma_c": ctp_posterior.get("sigma_c"),
            "z": float(z_value),
            "z_stage": z_meta.get("stage"),
            "z_source": z_meta.get("source"),
            "prior_strength": prior_strength,
            "model_evidence": model_evidence,
            "nowcast_evidence": nowcast_evidence,
            "observed_nowcast_evidence": nowcast_evidence,
            "wu_forecast_evidence": wu_forecast_evidence,
            "wu_forecast_evidence_base": wu_forecast_evidence_base,
            "wu_forecast_evidence_scale": wu_forecast_evidence_scale,
            "wu_forecast_enabled": bool(wu_forecast_evidence > 0),
            "wu_forecast_status": str(wu_forecast_meta.get("status") or ""),
            "wu_forecast_source": str(wu_forecast_meta.get("source") or ""),
            "wu_forecast_prob": (
                wu_forecast_probs[idx]
                if wu_forecast_evidence > 0 and idx < len(wu_forecast_probs)
                else None
            ),
            "wu_forecast_peak_c": wu_forecast_meta.get("peak_c"),
            "wu_forecast_sigma_c": wu_forecast_meta.get("sigma_c"),
            "weather_com_forecast_evidence": weather_com_forecast_evidence,
            "weather_com_forecast_evidence_raw": weather_com_forecast_evidence_raw,
            "weather_com_forecast_evidence_base": weather_com_forecast_evidence_base,
            "weather_com_forecast_evidence_scale": weather_com_forecast_evidence_scale,
            "weather_com_forecast_enabled": bool(weather_com_forecast_evidence > 0),
            "weather_com_forecast_status": str(weather_com_forecast_meta.get("status") or ""),
            "weather_com_forecast_source": str(weather_com_forecast_meta.get("source") or ""),
            "weather_com_forecast_prob": (
                weather_com_forecast_probs[idx]
                if weather_com_forecast_evidence > 0 and idx < len(weather_com_forecast_probs)
                else None
            ),
            "weather_com_forecast_peak_c": weather_com_forecast_meta.get("peak_c"),
            "weather_com_forecast_sigma_c": weather_com_forecast_meta.get("sigma_c"),
            "weather_com_forecast_pp_cap": weather_com_forecast_meta.get("pp_cap_max_abs"),
            "weather_com_forecast_pp_max_delta": weather_com_forecast_meta.get("pp_max_delta"),
            "weather_com_forecast_pp_raw_max_delta": weather_com_forecast_meta.get("pp_raw_max_delta"),
            "weather_com_forecast_pp_cap_hit": bool(weather_com_forecast_meta.get("pp_cap_hit")),
            "taf_peak_evidence": taf_peak_evidence,
            "taf_peak_evidence_base": taf_peak_evidence_base,
            "taf_peak_evidence_scale": taf_peak_evidence_scale,
            "taf_peak_enabled": bool(taf_peak_evidence > 0),
            "taf_peak_status": str(taf_peak_meta.get("status") or ""),
            "taf_peak_source": str(taf_peak_meta.get("source") or ""),
            "taf_peak_prob": taf_peak_probs[idx] if taf_peak_evidence > 0 and idx < len(taf_peak_probs) else None,
            "taf_peak_c": taf_peak_meta.get("peak_c"),
            "taf_peak_sigma_c": taf_peak_meta.get("sigma_c"),
            "temperature_trajectory_evidence": temperature_trajectory_evidence,
            "temperature_trajectory_evidence_base": temperature_trajectory_evidence_base,
            "temperature_trajectory_evidence_scale": temperature_trajectory_evidence_scale,
            "temperature_trajectory_posterior_enabled": bool(temperature_trajectory_evidence > 0),
            "temperature_trajectory_posterior_status": str(temperature_trajectory_meta.get("status") or ""),
            "temperature_trajectory_posterior_source": str(temperature_trajectory_meta.get("source") or ""),
            "temperature_trajectory_role": str(
                temperature_trajectory_meta.get("role") or "intraday_temperature_trajectory_likelihood"
            ),
            "temperature_trajectory_source_class": str(
                temperature_trajectory_meta.get("source_class") or "airport_temperature_path"
            ),
            "temperature_trajectory_derived_from": list(temperature_trajectory_meta.get("derived_from") or []),
            "temperature_trajectory_dedupe_policy": str(temperature_trajectory_meta.get("dedupe_policy") or ""),
            "temperature_trajectory_weaker_than_hard_source": bool(
                temperature_trajectory_meta.get("weaker_than_hard_source", True)
            ),
            "temperature_trajectory_prob": (
                temperature_trajectory_probs[idx]
                if temperature_trajectory_evidence > 0 and idx < len(temperature_trajectory_probs)
                else None
            ),
            "temperature_trajectory_mu_c": (
                temperature_trajectory_path_mu_c
                if temperature_trajectory_path_mu_c is not None
                else temperature_trajectory_meta.get("mu_c")
            ),
            "temperature_trajectory_sigma_c": (
                temperature_trajectory_path_sigma_c
                if temperature_trajectory_path_sigma_c is not None
                else temperature_trajectory_meta.get("sigma_c")
            ),
            "temperature_trajectory_path_mu_c": temperature_trajectory_path_mu_c,
            "temperature_trajectory_path_sigma_c": temperature_trajectory_path_sigma_c,
            "temperature_trajectory_extrapolation_c": temperature_trajectory_meta.get("extrapolation_c"),
            "temperature_trajectory_blended_mu_c": temperature_trajectory_meta.get("blended_mu_c"),
            "temperature_trajectory_rate_c_per_hour": temperature_trajectory_rate_hat_c_per_hour,
            "temperature_trajectory_rate_hat_c_per_hour": temperature_trajectory_rate_hat_c_per_hour,
            "temperature_trajectory_accel_hat_c_per_hour2": temperature_trajectory_accel_hat_c_per_hour2,
            "temperature_trajectory_realization_state": temperature_trajectory_meta.get("realization_state"),
            "temperature_momentum_posterior_gate": temperature_trajectory_meta.get("momentum_posterior_gate"),
            "temperature_trajectory_hours_to_peak_window": temperature_trajectory_meta.get("hours_to_peak_window"),
            "temperature_trajectory_component_detail": dict(temperature_trajectory_component_detail),
            "temperature_trajectory_peak_estimator": (
                dict(temperature_trajectory_meta.get("peak_estimator"))
                if isinstance(temperature_trajectory_meta.get("peak_estimator"), Mapping)
                else None
            ),
            "post_cool_rebound_regime_prior_evidence": post_cool_rebound_regime_prior_evidence,
            "post_cool_rebound_regime_prior_evidence_raw": post_cool_rebound_regime_prior_evidence_raw,
            "post_cool_rebound_regime_prior_evidence_base": post_cool_rebound_regime_prior_evidence_base,
            "post_cool_rebound_regime_prior_evidence_scale": post_cool_rebound_regime_prior_evidence_scale,
            "post_cool_rebound_regime_prior_enabled": bool(post_cool_rebound_regime_prior_evidence > 0),
            "post_cool_rebound_regime_prior_status": str(post_cool_rebound_regime_prior_meta.get("status") or ""),
            "post_cool_rebound_regime_prior_state": str(post_cool_rebound_regime_prior_meta.get("state") or ""),
            "post_cool_rebound_regime_prior_source": str(post_cool_rebound_regime_prior_meta.get("source") or ""),
            "post_cool_rebound_regime_prior_role": str(
                post_cool_rebound_regime_prior_meta.get("role") or "conditional_climate_prior"
            ),
            "post_cool_rebound_regime_prior_source_class": str(
                post_cool_rebound_regime_prior_meta.get("source_class") or "conditional_climate_prior"
            ),
            "post_cool_rebound_regime_prior_derived_from": list(
                post_cool_rebound_regime_prior_meta.get("derived_from") or []
            ),
            "post_cool_rebound_regime_prior_dedupe_policy": str(
                post_cool_rebound_regime_prior_meta.get("dedupe_policy") or ""
            ),
            "post_cool_rebound_regime_prior_weaker_than_hard_source": bool(
                post_cool_rebound_regime_prior_meta.get("weaker_than_hard_source", True)
            ),
            "post_cool_rebound_regime_prior_prob": (
                post_cool_rebound_regime_prior_probs[idx]
                if post_cool_rebound_regime_prior_evidence > 0 and idx < len(post_cool_rebound_regime_prior_probs)
                else None
            ),
            "post_cool_rebound_regime_prior_target_peak_c": post_cool_rebound_regime_prior_meta.get("target_peak_c"),
            "post_cool_rebound_regime_prior_mu_c": post_cool_rebound_regime_prior_meta.get("mu_c"),
            "post_cool_rebound_regime_prior_sigma_c": post_cool_rebound_regime_prior_meta.get("sigma_c"),
            "post_cool_rebound_regime_prior_pp_delta": (
                post_cool_rebound_regime_prior_pp_deltas[idx]
                if idx < len(post_cool_rebound_regime_prior_pp_deltas)
                else None
            ),
            "post_cool_rebound_regime_prior_pp_cap": post_cool_rebound_regime_prior_meta.get("pp_cap_max_abs"),
            "post_cool_rebound_regime_prior_pp_max_delta": post_cool_rebound_regime_prior_meta.get("pp_max_delta"),
            "post_cool_rebound_regime_prior_pp_raw_max_delta": post_cool_rebound_regime_prior_meta.get("pp_raw_max_delta"),
            "post_cool_rebound_regime_prior_pp_cap_hit": bool(post_cool_rebound_regime_prior_meta.get("pp_cap_hit")),
            "post_cool_rebound_regime_prior_features": (
                dict(post_cool_rebound_regime_prior_meta.get("features"))
                if isinstance(post_cool_rebound_regime_prior_meta.get("features"), Mapping)
                else {}
            ),
            "post_cool_rebound_intraday_likelihood_evidence": post_cool_rebound_intraday_likelihood_evidence,
            "post_cool_rebound_intraday_likelihood_evidence_raw": post_cool_rebound_intraday_likelihood_evidence_raw,
            "post_cool_rebound_intraday_likelihood_evidence_base": post_cool_rebound_intraday_likelihood_evidence_base,
            "post_cool_rebound_intraday_likelihood_evidence_scale": post_cool_rebound_intraday_likelihood_evidence_scale,
            "post_cool_rebound_intraday_likelihood_enabled": bool(post_cool_rebound_intraday_likelihood_evidence > 0),
            "post_cool_rebound_intraday_likelihood_status": str(post_cool_rebound_intraday_likelihood_meta.get("status") or ""),
            "post_cool_rebound_intraday_likelihood_state": str(post_cool_rebound_intraday_likelihood_meta.get("state") or ""),
            "post_cool_rebound_intraday_likelihood_source": str(post_cool_rebound_intraday_likelihood_meta.get("source") or ""),
            "post_cool_rebound_intraday_likelihood_role": str(
                post_cool_rebound_intraday_likelihood_meta.get("role") or "intraday_likelihood"
            ),
            "post_cool_rebound_intraday_likelihood_source_class": str(
                post_cool_rebound_intraday_likelihood_meta.get("source_class") or "derived_intraday_likelihood"
            ),
            "post_cool_rebound_intraday_likelihood_derived_from": list(
                post_cool_rebound_intraday_likelihood_meta.get("derived_from") or []
            ),
            "post_cool_rebound_intraday_likelihood_dedupe_policy": str(
                post_cool_rebound_intraday_likelihood_meta.get("dedupe_policy") or ""
            ),
            "post_cool_rebound_intraday_likelihood_weaker_than_hard_source": bool(
                post_cool_rebound_intraday_likelihood_meta.get("weaker_than_hard_source", True)
            ),
            "post_cool_rebound_intraday_likelihood_prob": (
                post_cool_rebound_intraday_likelihood_probs[idx]
                if post_cool_rebound_intraday_likelihood_evidence > 0
                and idx < len(post_cool_rebound_intraday_likelihood_probs)
                else None
            ),
            "post_cool_rebound_intraday_likelihood_target_peak_c": post_cool_rebound_intraday_likelihood_meta.get("target_peak_c"),
            "post_cool_rebound_intraday_likelihood_mu_c": post_cool_rebound_intraday_likelihood_meta.get("mu_c"),
            "post_cool_rebound_intraday_likelihood_sigma_c": post_cool_rebound_intraday_likelihood_meta.get("sigma_c"),
            "post_cool_rebound_intraday_likelihood_pp_delta": (
                post_cool_rebound_intraday_likelihood_pp_deltas[idx]
                if idx < len(post_cool_rebound_intraday_likelihood_pp_deltas)
                else None
            ),
            "post_cool_rebound_intraday_likelihood_pp_cap": post_cool_rebound_intraday_likelihood_meta.get("pp_cap_max_abs"),
            "post_cool_rebound_intraday_likelihood_pp_max_delta": post_cool_rebound_intraday_likelihood_meta.get("pp_max_delta"),
            "post_cool_rebound_intraday_likelihood_pp_raw_max_delta": post_cool_rebound_intraday_likelihood_meta.get("pp_raw_max_delta"),
            "post_cool_rebound_intraday_likelihood_pp_cap_hit": bool(post_cool_rebound_intraday_likelihood_meta.get("pp_cap_hit")),
            "post_cool_rebound_intraday_likelihood_features": (
                dict(post_cool_rebound_intraday_likelihood_meta.get("features"))
                if isinstance(post_cool_rebound_intraday_likelihood_meta.get("features"), Mapping)
                else {}
            ),
            "post_cool_rebound_risk_evidence": post_cool_rebound_risk_evidence,
            "post_cool_rebound_risk_evidence_raw": post_cool_rebound_risk_evidence_raw,
            "post_cool_rebound_risk_evidence_base": post_cool_rebound_risk_evidence_base,
            "post_cool_rebound_risk_evidence_scale": post_cool_rebound_risk_evidence_scale,
            "post_cool_rebound_risk_enabled": bool(post_cool_rebound_risk_evidence > 0),
            "post_cool_rebound_risk_status": str(post_cool_rebound_risk_meta.get("status") or ""),
            "post_cool_rebound_risk_state": str(post_cool_rebound_risk_meta.get("state") or ""),
            "post_cool_rebound_risk_source": str(post_cool_rebound_risk_meta.get("source") or ""),
            "post_cool_rebound_risk_prob": (
                post_cool_rebound_risk_probs[idx]
                if post_cool_rebound_risk_evidence > 0 and idx < len(post_cool_rebound_risk_probs)
                else None
            ),
            "post_cool_rebound_risk_target_peak_c": post_cool_rebound_risk_meta.get("target_peak_c"),
            "post_cool_rebound_risk_mu_c": post_cool_rebound_risk_meta.get("mu_c"),
            "post_cool_rebound_risk_sigma_c": post_cool_rebound_risk_meta.get("sigma_c"),
            "post_cool_rebound_risk_pp_delta": (
                post_cool_rebound_intraday_likelihood_pp_deltas[idx]
                if idx < len(post_cool_rebound_intraday_likelihood_pp_deltas)
                else None
            ),
            "post_cool_rebound_risk_pp_cap": post_cool_rebound_risk_meta.get("pp_cap_max_abs"),
            "post_cool_rebound_risk_pp_max_delta": post_cool_rebound_risk_meta.get("pp_max_delta"),
            "post_cool_rebound_risk_pp_raw_max_delta": post_cool_rebound_risk_meta.get("pp_raw_max_delta"),
            "post_cool_rebound_risk_pp_cap_hit": bool(post_cool_rebound_risk_meta.get("pp_cap_hit")),
            "post_cool_rebound_risk_features": (
                dict(post_cool_rebound_risk_meta.get("features"))
                if isinstance(post_cool_rebound_risk_meta.get("features"), Mapping)
                else {}
            ),
            "climo_regime_evidence": climo_regime_evidence,
            "climo_regime_evidence_raw": climo_regime_evidence_raw,
            "climo_regime_evidence_base": climo_regime_evidence_base,
            "climo_regime_evidence_scale": climo_regime_evidence_scale_value,
            "climo_regime_enabled": bool(climo_regime_evidence > 0),
            "climo_regime_status": str(climo_regime_meta.get("status") or ""),
            "climo_regime_source": str(climo_regime_meta.get("source") or ""),
            "climo_regime_prob": climo_regime_probs[idx] if idx < len(climo_regime_probs) else None,
            "climo_regime_pp_delta": (
                posterior - climo_regime_no_climo_probs[idx]
                if idx < len(climo_regime_no_climo_probs)
                else None
            ),
            "climo_regime_pp_cap": climo_regime_meta.get("pp_cap_max_abs"),
            "climo_regime_pp_max_delta": climo_regime_meta.get("pp_max_delta"),
            "climo_regime_pp_cap_stage": (
                (climo_regime_meta.get("pp_cap") or {}).get("stage")
                if isinstance(climo_regime_meta.get("pp_cap"), Mapping)
                else None
            ),
            "climo_regime_pp_cap_hit": bool(climo_regime_meta.get("pp_cap_hit")),
            "climo_regime_primary_regime": (
                climo_features.get("primary_regime_label")
                if isinstance(climo_features, Mapping)
                else None
            ),
            "climo_regime_high_temp_gate": (
                climo_features.get("high_temp_potential_gate")
                if isinstance(climo_features, Mapping)
                else None
            ),
            "temperature_trajectory_priority": True,
            "climo_regime_features": dict(climo_features) if isinstance(climo_features, Mapping) else {},
            "climo_regime_explanation": (
                climo_regime_meta.get("explanation")
                if isinstance(climo_regime_meta, Mapping)
                else None
            ),
            "temperature_bucket_posterior": atomic_temperature_posterior,
            "temperature_bucket_label": (
                atomic_temperature_labels[idx]
                if atomic_temperature_posterior is not None and idx < len(atomic_temperature_labels)
                else None
            ),
            "temperature_bucket_probability_mode": (
                "hard_source_posterior_override"
                if hard_source_override_applied
                else (
                    "listed_atomic_temperature_conditional"
                    if atomic_temperature_posterior is not None
                    else "contract_payout"
                )
            ),
            "contract_payout_posterior": posterior,
            "atomic_temperature_alpha": (
                atomic_temperature_alphas[idx]
                if atomic_temperature_posterior is not None and idx < len(atomic_temperature_alphas)
                else None
            ),
            "atomic_temperature_alpha_total": (
                atomic_temperature_alpha_total
                if atomic_temperature_posterior is not None
                else None
            ),
            "atomic_temperature_score_components": (
                dict(score_components)
                if atomic_temperature_posterior is not None
                else None
            ),
            "soft_forecast_scope_allowed": bool(soft_forecast_scope_allowed),
            "soft_forecast_scope_reason": str(soft_forecast_scope.get("reason") or ""),
            "soft_forecast_scope": dict(soft_forecast_scope),
            "soft_forecast_manual_override_enabled": bool(soft_forecast_manual_override),
            "ctp_soft_forecast_precision_scale": ctp_soft_forecast_precision_scale,
            "ctp_model_ensemble_precision_scale": ctp_model_ensemble_precision_scale,
            "ctp_wu_forecast_precision_scale": ctp_wu_forecast_precision_scale,
            "ctp_weather_com_forecast_precision_scale": ctp_weather_com_forecast_precision_scale,
            "ctp_taf_peak_precision_scale": ctp_taf_peak_precision_scale,
            "physical_likelihood_scope_allowed": bool(physical_likelihood_scope_allowed),
            "physical_likelihood_scope_reason": str(physical_likelihood_scope.get("reason") or ""),
            "physical_likelihood_scope": dict(physical_likelihood_scope),
            "physical_likelihood_d0_only": bool(physical_likelihood_d0_only),
            "physical_likelihood_stage_allowed": bool(physical_likelihood_stage_allowed),
            "physical_likelihood_stage_reason": str(physical_likelihood_stage_reason),
            "physical_likelihood_evidence": physical_likelihood_evidence,
            "independent_physics_residual_evidence": physical_likelihood_evidence,
            "physical_likelihood_evidence_base": physical_likelihood_evidence_base,
            "independent_physics_residual_evidence_base": physical_likelihood_evidence_base,
            "physical_likelihood_evidence_scale": physical_likelihood_evidence_scale,
            "independent_physics_residual_evidence_scale": physical_likelihood_evidence_scale,
            "physical_likelihood_evidence_scale_reason": physical_likelihood_evidence_scale_reason,
            "physical_likelihood_evidence_scale_details": dict(physical_likelihood_evidence_scale_details),
            "physical_likelihood_enabled": bool(physical_likelihood_evidence > 0),
            "independent_physics_residual_enabled": bool(physical_likelihood_evidence > 0),
            "physical_likelihood_source": str(physical_likelihood_meta.get("source") or ""),
            "independent_physics_residual_source": str(physical_likelihood_meta.get("source") or ""),
            "independent_physics_residual_role": "independent_physical_residual_likelihood",
            "independent_physics_residual_source_class": "physical_residual",
            "independent_physics_residual_dedupe_policy": (
                "must not reuse temperature trajectory, METAR hard observation, final settlement, "
                "or model-QM evidence without downweighting"
            ),
            "independent_physics_residual_weaker_than_hard_source": True,
            "physical_likelihood_prob": (
                physical_likelihood_probs[idx]
                if physical_likelihood_evidence > 0 and idx < len(physical_likelihood_probs)
                else None
            ),
            "physical_likelihood_observed_prob": physical_likelihood_observed_prob,
            "independent_physics_residual_prob": (
                physical_likelihood_probs[idx]
                if physical_likelihood_evidence > 0 and idx < len(physical_likelihood_probs)
                else None
            ),
            "independent_physics_residual_observed_prob": physical_likelihood_observed_prob,
            "physical_likelihood_row": (
                physical_likelihood_row
                if physical_likelihood_evidence > 0
                else None
            ),
            "physical_likelihood_observed_row": physical_likelihood_row,
            "physical_likelihood_component_groups": (
                physical_likelihood_component_groups
                if physical_likelihood_evidence > 0
                and physical_likelihood_component_groups
                else None
            ),
            "physical_likelihood_observed_component_groups": (
                physical_likelihood_component_groups
                if physical_likelihood_component_groups
                else None
            ),
            "physical_d0_explanation": physical_d0_explanation,
            "bimodal_risk": bool(
                ((physical_likelihood_meta.get("bimodal") or {}) if isinstance(physical_likelihood_meta, Mapping) else {}).get("bimodal")
            ),
            "bimodal_size_scale": physical_likelihood_meta.get("risk_size_scale"),
            "posterior": posterior,
            "posterior_pre_hard_source_override": (
                posterior_probs_pre_hard_source_override[idx]
                if hard_source_override_applied and idx < len(posterior_probs_pre_hard_source_override)
                else None
            ),
            "temperature_bucket_posterior_pre_hard_source_override": (
                atomic_temperature_posterior_pre_hard_source_override[idx]
                if hard_source_override_applied and idx < len(atomic_temperature_posterior_pre_hard_source_override)
                else None
            ),
            "hard_source_posterior_override": bool(hard_source_override_applied),
            "hard_source_posterior_override_applied": bool(hard_source_override_applied),
            "hard_source_posterior_override_status": str(hard_source_override_meta.get("status") or ""),
            "hard_source_posterior_override_source": hard_source_override_meta.get("source"),
            "hard_source_posterior_override_value_c": hard_source_override_meta.get("value_c"),
            "hard_source_posterior_override_target_bucket": hard_source_override_meta.get("target_bucket"),
            "hard_source_posterior_override_window": hard_source_override_meta.get("window"),
            "hard_source_posterior_override_constraint_mode": hard_source_override_meta.get("constraint_mode"),
            "hard_source_posterior_override_final_bucket_lock": bool(hard_source_override_meta.get("final_bucket_lock")),
            "hard_source_posterior_override_final_lock_reason": hard_source_override_meta.get("final_lock_reason"),
            "target_adjustment_action": hard_source_override_meta.get("target_adjustment_action"),
            "target_adjustment_reason_codes": (
                ["hard_source_posterior_override"] if hard_source_override_applied else []
            ),
            "lcb": lcb,
            "ucb": ucb,
            "sigma": std,
            "alpha": alphas[idx],
            "alpha_total": alpha_total,
            "score_components": score_components,
            "score_component_details": score_component_details,
            "model_count": model_count_max,
            "model_cache_freshness": freshness,
            "prior_source": prior_source,
            "historical_samples": history.get("sample_count", 0) if history else 0,
            "historical_min_samples": history.get("min_samples") if history else None,
            "historical_prior_sample_scale": history.get("prior_sample_scale") if history else None,
            "historical_days": history.get("days") if history else None,
            "historical_bucket_count": (history.get("bucket_counts") or [None] * len(pre_items))[idx] if history else None,
            "bias_correction_enabled": bool(model_weight_multipliers),
            "model_weight_multipliers": dict(model_weight_multipliers or {}),
        }
        physical_likelihood_row_fields: Dict[str, Any] = {
            "q_bayes_physical_likelihood_prob": (
                physical_likelihood_probs[idx]
                if physical_likelihood_evidence > 0 and idx < len(physical_likelihood_probs)
                else None
            ),
            "q_bayes_physical_likelihood_observed_prob": physical_likelihood_observed_prob,
            "q_bayes_physical_likelihood_evidence": physical_likelihood_evidence,
            "q_bayes_physical_likelihood_evidence_base": physical_likelihood_evidence_base,
            "q_bayes_physical_likelihood_evidence_scale": physical_likelihood_evidence_scale,
            "q_bayes_physical_likelihood_evidence_scale_reason": physical_likelihood_evidence_scale_reason,
            "q_bayes_physical_likelihood_source": str(physical_likelihood_meta.get("source") or ""),
            "q_bayes_physical_likelihood_enabled": bool(physical_likelihood_evidence > 0),
            "q_bayes_physical_likelihood_hard_zero": physical_likelihood_hard_zero,
            "q_bayes_physical_likelihood_hard_reasons": physical_likelihood_hard_reasons,
        }
        if physical_likelihood_evidence_scale_details:
            physical_likelihood_row_fields["q_bayes_physical_likelihood_evidence_scale_details"] = dict(
                physical_likelihood_evidence_scale_details
            )
        if physical_likelihood_component_groups:
            physical_likelihood_row_fields["q_bayes_physical_likelihood_component_groups"] = physical_likelihood_component_groups
        if physical_likelihood_components:
            physical_likelihood_row_fields["q_bayes_physical_likelihood_components"] = physical_likelihood_components
        for target in (item, q_obj):
            for key, value in physical_likelihood_row_fields.items():
                if value not in (None, "", [], {}):
                    target[key] = value

    return {
        "enabled": True,
        "source": f"{CTP_VERSION}_hard_source_override" if hard_source_override_applied else CTP_VERSION,
        "posterior_engine": CTP_VERSION,
        "posterior_temperature_distribution": ctp_posterior,
        "posterior_temperature_distribution_pre_observed_constraints": ctp_pre_constraints,
        "z": float(z_value),
        "z_stage": z_meta.get("stage"),
        "z_source": z_meta.get("source"),
        "bucket_count": len(pre_items),
        "prior_strength": prior_strength,
        "model_evidence": model_evidence,
        "ctp_soft_forecast_precision_scale": ctp_soft_forecast_precision_scale,
        "ctp_model_ensemble_precision_scale": ctp_model_ensemble_precision_scale,
        "ctp_wu_forecast_precision_scale": ctp_wu_forecast_precision_scale,
        "ctp_weather_com_forecast_precision_scale": ctp_weather_com_forecast_precision_scale,
        "ctp_taf_peak_precision_scale": ctp_taf_peak_precision_scale,
        "nowcast_evidence": nowcast_evidence,
        "observed_nowcast_evidence": nowcast_evidence,
        "wu_forecast_evidence": wu_forecast_evidence,
        "wu_forecast_evidence_base": wu_forecast_evidence_base,
        "wu_forecast_evidence_scale": wu_forecast_evidence_scale,
        "wu_forecast_enabled": bool(wu_forecast_evidence > 0),
        "wu_forecast_status": str(wu_forecast_meta.get("status") or ""),
        "wu_forecast_source": str(wu_forecast_meta.get("source") or ""),
        "wu_forecast_peak_c": wu_forecast_meta.get("peak_c"),
        "wu_forecast_sigma_c": wu_forecast_meta.get("sigma_c"),
        "weather_com_forecast_evidence": weather_com_forecast_evidence,
        "weather_com_forecast_evidence_raw": weather_com_forecast_evidence_raw,
        "weather_com_forecast_evidence_base": weather_com_forecast_evidence_base,
        "weather_com_forecast_evidence_scale": weather_com_forecast_evidence_scale,
        "weather_com_forecast_enabled": bool(weather_com_forecast_evidence > 0),
        "weather_com_forecast_status": str(weather_com_forecast_meta.get("status") or ""),
        "weather_com_forecast_source": str(weather_com_forecast_meta.get("source") or ""),
        "weather_com_forecast_peak_c": weather_com_forecast_meta.get("peak_c"),
        "weather_com_forecast_sigma_c": weather_com_forecast_meta.get("sigma_c"),
        "weather_com_forecast_pp_cap": weather_com_forecast_meta.get("pp_cap_max_abs"),
        "weather_com_forecast_pp_max_delta": weather_com_forecast_meta.get("pp_max_delta"),
        "weather_com_forecast_pp_raw_max_delta": weather_com_forecast_meta.get("pp_raw_max_delta"),
        "weather_com_forecast_pp_cap_hit": bool(weather_com_forecast_meta.get("pp_cap_hit")),
        "taf_peak_evidence": taf_peak_evidence,
        "taf_peak_evidence_base": taf_peak_evidence_base,
        "taf_peak_evidence_scale": taf_peak_evidence_scale,
        "taf_peak_enabled": bool(taf_peak_evidence > 0),
        "taf_peak_status": str(taf_peak_meta.get("status") or ""),
        "taf_peak_source": str(taf_peak_meta.get("source") or ""),
        "taf_peak_c": taf_peak_meta.get("peak_c"),
        "taf_peak_sigma_c": taf_peak_meta.get("sigma_c"),
        "temperature_trajectory_evidence": temperature_trajectory_evidence,
        "temperature_trajectory_evidence_base": temperature_trajectory_evidence_base,
        "temperature_trajectory_evidence_scale": temperature_trajectory_evidence_scale,
        "temperature_trajectory_posterior_enabled": bool(temperature_trajectory_evidence > 0),
        "temperature_trajectory_posterior_status": str(temperature_trajectory_meta.get("status") or ""),
        "temperature_trajectory_posterior_source": str(temperature_trajectory_meta.get("source") or ""),
        "temperature_trajectory_role": str(
            temperature_trajectory_meta.get("role") or "intraday_temperature_trajectory_likelihood"
        ),
        "temperature_trajectory_source_class": str(
            temperature_trajectory_meta.get("source_class") or "airport_temperature_path"
        ),
        "temperature_trajectory_derived_from": list(temperature_trajectory_meta.get("derived_from") or []),
        "temperature_trajectory_dedupe_policy": str(temperature_trajectory_meta.get("dedupe_policy") or ""),
        "temperature_trajectory_weaker_than_hard_source": bool(
            temperature_trajectory_meta.get("weaker_than_hard_source", True)
        ),
        "temperature_trajectory_mu_c": (
            temperature_trajectory_path_mu_c
            if temperature_trajectory_path_mu_c is not None
            else temperature_trajectory_meta.get("mu_c")
        ),
        "temperature_trajectory_sigma_c": (
            temperature_trajectory_path_sigma_c
            if temperature_trajectory_path_sigma_c is not None
            else temperature_trajectory_meta.get("sigma_c")
        ),
        "temperature_trajectory_path_mu_c": temperature_trajectory_path_mu_c,
        "temperature_trajectory_path_sigma_c": temperature_trajectory_path_sigma_c,
        "temperature_trajectory_extrapolation_c": temperature_trajectory_meta.get("extrapolation_c"),
        "temperature_trajectory_blended_mu_c": temperature_trajectory_meta.get("blended_mu_c"),
        "temperature_trajectory_rate_c_per_hour": temperature_trajectory_rate_hat_c_per_hour,
        "temperature_trajectory_rate_hat_c_per_hour": temperature_trajectory_rate_hat_c_per_hour,
        "temperature_trajectory_accel_hat_c_per_hour2": temperature_trajectory_accel_hat_c_per_hour2,
        "temperature_trajectory_realization_state": temperature_trajectory_meta.get("realization_state"),
        "temperature_momentum_posterior_gate": temperature_trajectory_meta.get("momentum_posterior_gate"),
        "temperature_trajectory_hours_to_peak_window": temperature_trajectory_meta.get("hours_to_peak_window"),
        "temperature_trajectory_forcing_turn_probability": temperature_trajectory_meta.get("forcing_turn_probability"),
        "temperature_trajectory_forcing_onset_probs": temperature_trajectory_meta.get("forcing_onset_probs"),
        "temperature_trajectory_forcing_contributors": temperature_trajectory_meta.get("forcing_contributors"),
        "temperature_trajectory_regime_probs": temperature_trajectory_meta.get("regime_probs"),
        "temperature_trajectory_component_detail": dict(temperature_trajectory_component_detail),
        "temperature_trajectory_peak_estimator": (
            dict(temperature_trajectory_meta.get("peak_estimator"))
            if isinstance(temperature_trajectory_meta.get("peak_estimator"), Mapping)
            else None
        ),
        "post_cool_rebound_regime_prior_evidence": post_cool_rebound_regime_prior_evidence,
        "post_cool_rebound_regime_prior_evidence_raw": post_cool_rebound_regime_prior_evidence_raw,
        "post_cool_rebound_regime_prior_evidence_base": post_cool_rebound_regime_prior_evidence_base,
        "post_cool_rebound_regime_prior_evidence_scale": post_cool_rebound_regime_prior_evidence_scale,
        "post_cool_rebound_regime_prior_enabled": bool(post_cool_rebound_regime_prior_evidence > 0),
        "post_cool_rebound_regime_prior_status": str(post_cool_rebound_regime_prior_meta.get("status") or ""),
        "post_cool_rebound_regime_prior_state": str(post_cool_rebound_regime_prior_meta.get("state") or ""),
        "post_cool_rebound_regime_prior_source": str(post_cool_rebound_regime_prior_meta.get("source") or ""),
        "post_cool_rebound_regime_prior_role": str(
            post_cool_rebound_regime_prior_meta.get("role") or "conditional_climate_prior"
        ),
        "post_cool_rebound_regime_prior_source_class": str(
            post_cool_rebound_regime_prior_meta.get("source_class") or "conditional_climate_prior"
        ),
        "post_cool_rebound_regime_prior_derived_from": list(
            post_cool_rebound_regime_prior_meta.get("derived_from") or []
        ),
        "post_cool_rebound_regime_prior_dedupe_policy": str(
            post_cool_rebound_regime_prior_meta.get("dedupe_policy") or ""
        ),
        "post_cool_rebound_regime_prior_weaker_than_hard_source": bool(
            post_cool_rebound_regime_prior_meta.get("weaker_than_hard_source", True)
        ),
        "post_cool_rebound_regime_prior_target_peak_c": post_cool_rebound_regime_prior_meta.get("target_peak_c"),
        "post_cool_rebound_regime_prior_mu_c": post_cool_rebound_regime_prior_meta.get("mu_c"),
        "post_cool_rebound_regime_prior_sigma_c": post_cool_rebound_regime_prior_meta.get("sigma_c"),
        "post_cool_rebound_regime_prior_pp_cap": post_cool_rebound_regime_prior_meta.get("pp_cap_max_abs"),
        "post_cool_rebound_regime_prior_pp_max_delta": post_cool_rebound_regime_prior_meta.get("pp_max_delta"),
        "post_cool_rebound_regime_prior_pp_raw_max_delta": post_cool_rebound_regime_prior_meta.get("pp_raw_max_delta"),
        "post_cool_rebound_regime_prior_pp_cap_hit": bool(post_cool_rebound_regime_prior_meta.get("pp_cap_hit")),
        "post_cool_rebound_regime_prior_features": (
            dict(post_cool_rebound_regime_prior_meta.get("features"))
            if isinstance(post_cool_rebound_regime_prior_meta.get("features"), Mapping)
            else {}
        ),
        "post_cool_rebound_intraday_likelihood_evidence": post_cool_rebound_intraday_likelihood_evidence,
        "post_cool_rebound_intraday_likelihood_evidence_raw": post_cool_rebound_intraday_likelihood_evidence_raw,
        "post_cool_rebound_intraday_likelihood_evidence_base": post_cool_rebound_intraday_likelihood_evidence_base,
        "post_cool_rebound_intraday_likelihood_evidence_scale": post_cool_rebound_intraday_likelihood_evidence_scale,
        "post_cool_rebound_intraday_likelihood_enabled": bool(post_cool_rebound_intraday_likelihood_evidence > 0),
        "post_cool_rebound_intraday_likelihood_status": str(post_cool_rebound_intraday_likelihood_meta.get("status") or ""),
        "post_cool_rebound_intraday_likelihood_state": str(post_cool_rebound_intraday_likelihood_meta.get("state") or ""),
        "post_cool_rebound_intraday_likelihood_source": str(post_cool_rebound_intraday_likelihood_meta.get("source") or ""),
        "post_cool_rebound_intraday_likelihood_role": str(
            post_cool_rebound_intraday_likelihood_meta.get("role") or "intraday_likelihood"
        ),
        "post_cool_rebound_intraday_likelihood_source_class": str(
            post_cool_rebound_intraday_likelihood_meta.get("source_class") or "derived_intraday_likelihood"
        ),
        "post_cool_rebound_intraday_likelihood_derived_from": list(
            post_cool_rebound_intraday_likelihood_meta.get("derived_from") or []
        ),
        "post_cool_rebound_intraday_likelihood_dedupe_policy": str(
            post_cool_rebound_intraday_likelihood_meta.get("dedupe_policy") or ""
        ),
        "post_cool_rebound_intraday_likelihood_weaker_than_hard_source": bool(
            post_cool_rebound_intraday_likelihood_meta.get("weaker_than_hard_source", True)
        ),
        "post_cool_rebound_intraday_likelihood_target_peak_c": post_cool_rebound_intraday_likelihood_meta.get("target_peak_c"),
        "post_cool_rebound_intraday_likelihood_mu_c": post_cool_rebound_intraday_likelihood_meta.get("mu_c"),
        "post_cool_rebound_intraday_likelihood_sigma_c": post_cool_rebound_intraday_likelihood_meta.get("sigma_c"),
        "post_cool_rebound_intraday_likelihood_pp_cap": post_cool_rebound_intraday_likelihood_meta.get("pp_cap_max_abs"),
        "post_cool_rebound_intraday_likelihood_pp_max_delta": post_cool_rebound_intraday_likelihood_meta.get("pp_max_delta"),
        "post_cool_rebound_intraday_likelihood_pp_raw_max_delta": post_cool_rebound_intraday_likelihood_meta.get("pp_raw_max_delta"),
        "post_cool_rebound_intraday_likelihood_pp_cap_hit": bool(post_cool_rebound_intraday_likelihood_meta.get("pp_cap_hit")),
        "post_cool_rebound_intraday_likelihood_features": (
            dict(post_cool_rebound_intraday_likelihood_meta.get("features"))
            if isinstance(post_cool_rebound_intraday_likelihood_meta.get("features"), Mapping)
            else {}
        ),
        "post_cool_rebound_risk_evidence": post_cool_rebound_risk_evidence,
        "post_cool_rebound_risk_evidence_raw": post_cool_rebound_risk_evidence_raw,
        "post_cool_rebound_risk_evidence_base": post_cool_rebound_risk_evidence_base,
        "post_cool_rebound_risk_evidence_scale": post_cool_rebound_risk_evidence_scale,
        "post_cool_rebound_risk_enabled": bool(post_cool_rebound_risk_evidence > 0),
        "post_cool_rebound_risk_status": str(post_cool_rebound_risk_meta.get("status") or ""),
        "post_cool_rebound_risk_state": str(post_cool_rebound_risk_meta.get("state") or ""),
        "post_cool_rebound_risk_source": str(post_cool_rebound_risk_meta.get("source") or ""),
        "post_cool_rebound_risk_target_peak_c": post_cool_rebound_risk_meta.get("target_peak_c"),
        "post_cool_rebound_risk_mu_c": post_cool_rebound_risk_meta.get("mu_c"),
        "post_cool_rebound_risk_sigma_c": post_cool_rebound_risk_meta.get("sigma_c"),
        "post_cool_rebound_risk_pp_cap": post_cool_rebound_risk_meta.get("pp_cap_max_abs"),
        "post_cool_rebound_risk_pp_max_delta": post_cool_rebound_risk_meta.get("pp_max_delta"),
        "post_cool_rebound_risk_pp_raw_max_delta": post_cool_rebound_risk_meta.get("pp_raw_max_delta"),
        "post_cool_rebound_risk_pp_cap_hit": bool(post_cool_rebound_risk_meta.get("pp_cap_hit")),
        "post_cool_rebound_risk_features": (
            dict(post_cool_rebound_risk_meta.get("features"))
            if isinstance(post_cool_rebound_risk_meta.get("features"), Mapping)
            else {}
        ),
        "climo_regime_evidence": climo_regime_evidence,
        "climo_regime_evidence_raw": climo_regime_evidence_raw,
        "climo_regime_evidence_base": climo_regime_evidence_base,
        "climo_regime_evidence_scale": climo_regime_evidence_scale_value,
        "climo_regime_enabled": bool(climo_regime_evidence > 0),
        "climo_regime_status": str(climo_regime_meta.get("status") or ""),
        "climo_regime_source": str(climo_regime_meta.get("source") or ""),
        "climo_regime_pp_cap": climo_regime_meta.get("pp_cap_max_abs"),
        "climo_regime_pp_max_delta": climo_regime_meta.get("pp_max_delta"),
        "climo_regime_pp_cap_stage": (
            (climo_regime_meta.get("pp_cap") or {}).get("stage")
            if isinstance(climo_regime_meta.get("pp_cap"), Mapping)
            else None
        ),
        "climo_regime_pp_cap_hit": bool(climo_regime_meta.get("pp_cap_hit")),
        "climo_regime_primary_regime": (
            climo_features.get("primary_regime_label")
            if isinstance(climo_features, Mapping)
            else None
        ),
        "climo_regime_high_temp_gate": (
            climo_features.get("high_temp_potential_gate")
            if isinstance(climo_features, Mapping)
            else None
        ),
        "temperature_trajectory_priority": True,
        "climo_regime_features": dict(climo_features) if isinstance(climo_features, Mapping) else {},
        "climo_regime_explanation": (
            climo_regime_meta.get("explanation")
            if isinstance(climo_regime_meta, Mapping)
            else None
        ),
        "atomic_temperature_bucket_display_enabled": bool(atomic_temperature_display_enabled),
        "atomic_temperature_probability_mode": (
            "hard_source_posterior_override"
            if hard_source_override_applied
            else (
                "listed_atomic_temperature_conditional"
                if atomic_temperature_display_enabled
                else "contract_payout"
            )
        ),
        "atomic_temperature_alpha_total": (
            atomic_temperature_alpha_total
            if atomic_temperature_display_enabled
            else None
        ),
        "soft_forecast_scope_allowed": bool(soft_forecast_scope_allowed),
        "soft_forecast_scope_reason": str(soft_forecast_scope.get("reason") or ""),
        "soft_forecast_scope": dict(soft_forecast_scope),
        "soft_forecast_manual_override_enabled": bool(soft_forecast_manual_override),
        "physical_likelihood_scope_allowed": bool(physical_likelihood_scope_allowed),
        "physical_likelihood_scope_reason": str(physical_likelihood_scope.get("reason") or ""),
        "physical_likelihood_scope": dict(physical_likelihood_scope),
        "physical_likelihood_d0_only": bool(physical_likelihood_d0_only),
        "physical_likelihood_stage_allowed": bool(physical_likelihood_stage_allowed),
        "physical_likelihood_stage_reason": str(physical_likelihood_stage_reason),
        "physical_likelihood_evidence": physical_likelihood_evidence,
        "independent_physics_residual_evidence": physical_likelihood_evidence,
        "physical_likelihood_evidence_base": physical_likelihood_evidence_base,
        "independent_physics_residual_evidence_base": physical_likelihood_evidence_base,
        "physical_likelihood_evidence_scale": physical_likelihood_evidence_scale,
        "independent_physics_residual_evidence_scale": physical_likelihood_evidence_scale,
        "physical_likelihood_evidence_scale_reason": physical_likelihood_evidence_scale_reason,
        "physical_likelihood_evidence_scale_details": dict(physical_likelihood_evidence_scale_details),
        "physical_likelihood_enabled": bool(physical_likelihood_evidence > 0),
        "independent_physics_residual_enabled": bool(physical_likelihood_evidence > 0),
        "physical_likelihood_source": str(physical_likelihood_meta.get("source") or ""),
        "independent_physics_residual_source": str(physical_likelihood_meta.get("source") or ""),
        "independent_physics_residual_role": "independent_physical_residual_likelihood",
        "independent_physics_residual_source_class": "physical_residual",
        "independent_physics_residual_dedupe_policy": (
            "must not reuse temperature trajectory, METAR hard observation, final settlement, "
            "or model-QM evidence without downweighting"
        ),
        "independent_physics_residual_weaker_than_hard_source": True,
        "physical_d0_explanation": physical_d0_explanation,
        "physical_likelihood_bimodal": bool(
            ((physical_likelihood_meta.get("bimodal") or {}) if isinstance(physical_likelihood_meta, Mapping) else {}).get("bimodal")
        ),
        "physical_likelihood_risk_size_scale": physical_likelihood_meta.get("risk_size_scale"),
        "alpha_total": alpha_total,
        "hard_source_posterior_override": bool(hard_source_override_applied),
        "hard_source_posterior_override_applied": bool(hard_source_override_applied),
        "hard_source_posterior_override_status": str(hard_source_override_meta.get("status") or ""),
        "hard_source_posterior_override_source": hard_source_override_meta.get("source"),
        "hard_source_posterior_override_value_c": hard_source_override_meta.get("value_c"),
        "hard_source_posterior_override_target_bucket": hard_source_override_meta.get("target_bucket"),
        "hard_source_posterior_override_window": hard_source_override_meta.get("window"),
        "hard_source_posterior_override_constraint_mode": hard_source_override_meta.get("constraint_mode"),
        "hard_source_posterior_override_final_bucket_lock": bool(hard_source_override_meta.get("final_bucket_lock")),
        "hard_source_posterior_override_final_lock_reason": hard_source_override_meta.get("final_lock_reason"),
        "model_count": model_count_max,
        "model_weight_sum": model_weight_sum,
        "model_cache_freshness": freshness,
        "prior_source": prior_source,
        "historical_samples": history.get("sample_count", 0) if history else 0,
        "bias_correction_enabled": bool(model_weight_multipliers),
    }
