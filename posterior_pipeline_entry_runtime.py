from __future__ import annotations

import math
import os
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import climo_regime_runtime as climo_runtime
from bayesian_likelihood_runtime import (
    compute_weather_likelihood_vector,
    estimate_peak_temperature_from_features,
    precipitation_evidence_profile,
)
from climo_regime_runtime import (
    cap_climo_evidence_by_pp_delta,
    climo_regime_bucket_probabilities,
    climo_regime_explanation,
    climo_regime_pp_cap,
)
from lcb_stage_runtime import DEFAULT_BAYES_STAGE_Z, resolve_lcb_stage_z
from hierarchical_posterior_pipeline_runtime import (
    SCHEMA_VERSION as HIERARCHICAL_PIPELINE_VERSION,
    build_hierarchical_posterior_pipeline,
)
from sequential_remaining_warming_bayes_runtime import (
    SCHEMA_VERSION as HIERARCHICAL_POSTERIOR_VERSION,
)
from source_observation_runtime import SOURCE_CLASSES, build_source_observation
from temperature_posterior_runtime import (
    VERSION as CTP_VERSION,
    bucket_probabilities_by_label,
    build_temperature_posterior_distribution,
)
from weather_source_acquisition_manager import (
    build_source_acquisition_trace,
    source_payloads_from_fact_payload,
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


def _physical_likelihood_evidence_scale(*args: Any, **kwargs: Any) -> Any:
    from intraday_likelihood_runtime import physical_likelihood_quality_scale

    return physical_likelihood_quality_scale(*args, **kwargs)


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


def _temperature_trajectory_channel_probs(*args: Any, **kwargs: Any) -> Any:
    from intraday_likelihood_runtime import temperature_trajectory_realization_probs

    return temperature_trajectory_realization_probs(*args, **kwargs)


def _post_cool_rebound_risk_payload(*args: Any, **kwargs: Any) -> Any:
    from intraday_likelihood_runtime import post_cool_rebound_risk_payload

    return post_cool_rebound_risk_payload(*args, **kwargs)


def _post_cool_rebound_channel_payload(*args: Any, **kwargs: Any) -> Any:
    from intraday_likelihood_runtime import post_cool_rebound_channel_payload

    return post_cool_rebound_channel_payload(*args, **kwargs)


def _post_cool_rebound_channel_probs(*args: Any, **kwargs: Any) -> Any:
    from intraday_likelihood_runtime import post_cool_rebound_channel_probs

    return post_cool_rebound_channel_probs(*args, **kwargs)


def _post_cool_rebound_risk_channel_probs(*args: Any, **kwargs: Any) -> Any:
    from intraday_likelihood_runtime import post_cool_rebound_risk_channel_probs

    return post_cool_rebound_risk_channel_probs(*args, **kwargs)


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


def _metar_post_peak_final_lock_state(
    features: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> Dict[str, Any]:
    source_key = str(candidate.get("source") or "").strip().lower()
    if source_key not in {"zuuu_metar_raw_amsc", "zuuu_metar_raw_awc"}:
        return {"final": False, "reason": "not_primary_metar_source"}
    value_c = _to_float(candidate.get("value_c"))
    latest_temp = _feature_float(
        features,
        f"{source_key}_latest_temp_c",
        "observed_temp_c",
        "observed_current_temp_c",
        "metar_temperature_c",
    )
    latest_at = _parse_iso_datetime(candidate.get("latest_at") or features.get(f"{source_key}_latest_at"))
    today_max_at = _parse_iso_datetime(candidate.get("value_at") or features.get(f"{source_key}_today_max_at"))
    temp_drop_c = (
        float(value_c) - float(latest_temp)
        if value_c is not None and latest_temp is not None
        else None
    )
    elapsed_min = (
        (latest_at.astimezone(timezone.utc) - today_max_at.astimezone(timezone.utc)).total_seconds() / 60.0
        if latest_at is not None and today_max_at is not None
        else None
    )
    return {
        "final": False,
        "reason": "metar_post_peak_cooling_is_probabilistic_not_final",
        "source": source_key,
        "today_max_c": float(value_c) if value_c is not None else None,
        "today_max_at": today_max_at.astimezone(timezone.utc).isoformat() if today_max_at is not None else "",
        "latest_temp_c": float(latest_temp) if latest_temp is not None else None,
        "latest_at": latest_at.astimezone(timezone.utc).isoformat() if latest_at is not None else "",
        "temp_drop_c": temp_drop_c,
        "elapsed_min": elapsed_min,
        "prediction_window_cannot_emit_final_lock": True,
        "required_final_evidence": "event_end_complete_day_or_official_settlement",
    }


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
    amsc_mismatch = _feature_bool(features, "zuuu_metar_raw_amsc_date_mismatch")
    amsc_value = _feature_float(features, "zuuu_metar_raw_amsc_today_max_c")
    if amsc_value is None:
        source_text = _normalize_text(features.get("source"))
        observed_source = _normalize_text(features.get("observed_today_max_source"))
        if (
            "zuuu_metar_raw_amsc" in source_text
            or "zuuu_metar_raw_amsc" in observed_source
        ) and _feature_bool(features, "observed_today_max_trusted"):
            amsc_value = _feature_float(features, "observed_today_max_c")
    if amsc_value is not None and not amsc_mismatch:
        candidates.append(
            {
                "source": "zuuu_metar_raw_amsc",
                "value_c": float(amsc_value),
                "value_at": _feature_text(features, "zuuu_metar_raw_amsc_today_max_at", "zuuu_metar_raw_amsc_latest_at"),
                "latest_at": _feature_text(features, "zuuu_metar_raw_amsc_latest_at"),
                "fetched_at": _feature_text(features, "zuuu_metar_raw_amsc_fetched_at"),
                "event_date": _feature_date_text(features, "zuuu_metar_raw_amsc_target_event_date", "event_date", "run_date"),
                "priority": 30,
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
    metar_final = _metar_post_peak_final_lock_state(features, candidate)
    if bool(metar_final.get("final")):
        return {
            "final_lock": True,
            "reason": str(metar_final.get("reason") or "metar_post_peak_final_lock"),
            "final_lock_state": metar_final,
        }
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
        row["edge_min_exit_codes_yes"] = ["target_realization_yes_target_invalidated"]
        row["target_adjustment_force_exit"] = True
        row["target_adjustment_exit_reason_code"] = "target_realization_yes_target_invalidated"
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


def _build_physical_d0_explanation(*args: Any, **kwargs: Any) -> Any:
    from intraday_likelihood_runtime import build_physical_d0_explanation

    old_strength_key = "physical_likelihood" + "_evidence"
    if old_strength_key in kwargs and "physical_layer_strength" not in kwargs:
        kwargs = dict(kwargs)
        kwargs["physical_layer_strength"] = kwargs.pop(old_strength_key)
    return build_physical_d0_explanation(*args, **kwargs)


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


def _hierarchical_production_enabled() -> bool:
    return True


def _bayes_item_labels(pre_items: List[Dict[str, Any]]) -> List[str]:
    labels: List[str] = []
    for item in pre_items:
        nested = item.get("item") if isinstance(item.get("item"), Mapping) else {}
        labels.append(str(item.get("label") or nested.get("label") or "").strip())
    return labels


def _bayes_item_rounding_rule(pre_items: List[Dict[str, Any]]) -> str:
    for item in pre_items:
        text = str(item.get("rounding_rule") or "").strip()
        if text:
            return text
    return "rounded_degrees"


def _root_prior_distribution(
    *,
    labels: List[str],
    historical_context: Optional[Mapping[str, Any]],
) -> Dict[str, float]:
    history = historical_context if isinstance(historical_context, Mapping) else {}
    historical_prior = history.get("prior_probs") if isinstance(history.get("prior_probs"), list) else None
    if not labels:
        return {}
    neutral_probs = [1.0 / len(labels) for _label in labels]
    if historical_prior and len(historical_prior) == len(labels):
        history_probs = _normalize([float(v) for v in historical_prior])
        sample_scale = _clamp01(_to_float(history.get("prior_sample_scale")))
        if sample_scale is None:
            sample_scale = 1.0
        values = _normalize(
            [
                float(sample_scale) * history_probs[idx] + (1.0 - float(sample_scale)) * neutral_probs[idx]
                for idx in range(len(labels))
            ]
        )
    else:
        values = neutral_probs
    return {label: float(values[idx]) for idx, label in enumerate(labels)}


def _station_timezone_from_features(features: Mapping[str, Any]) -> str:
    return (
        _feature_text(features, "station_timezone", "timezone", "tz")
        or os.environ.get("PM_WEATHER_STATION_TIMEZONE")
        or "Asia/Shanghai"
    )


def _event_date_for_hierarchical_pipeline(
    *,
    features: Mapping[str, Any],
    station_timezone: str,
    days_to_event: Optional[float],
) -> str:
    direct = _feature_date_text(features, "event_date", "target_event_date", "run_date", "market_event_date")
    if direct:
        return direct
    tz = _timezone_for_name(station_timezone)
    local_today = datetime.now(tz).date()
    days = _to_float(days_to_event)
    if days is not None and days > 0:
        local_today = local_today + timedelta(days=int(round(days)))
    return local_today.isoformat()


def _as_of_for_hierarchical_pipeline(
    features: Mapping[str, Any],
    *,
    event_date: str = "",
    station_timezone: str = "Asia/Shanghai",
) -> str:
    direct = _feature_text(
        features,
        "as_of",
        "latest_at",
        "valid_time",
        "valid_time_local",
        "observation_time",
        "zuuu_metar_raw_awc_latest_at",
        "zuuu_metar_raw_awc_fetched_at",
        "fetched_at",
        "updated_at",
    )
    if direct:
        return direct
    hour = _feature_float(features, "observation_hour_local", "hour_local", "local_hour")
    date_text = str(event_date or _feature_date_text(features, "event_date", "target_event_date", "run_date", "market_event_date") or "").strip()
    if hour is None or not date_text:
        return ""
    try:
        tz = _timezone_for_name(station_timezone)
        whole_hour = int(float(hour))
        minute = int(round((float(hour) - whole_hour) * 60.0))
        if minute >= 60:
            whole_hour += 1
            minute -= 60
        whole_hour = max(0, min(23, whole_hour))
        minute = max(0, min(59, minute))
        local_dt = datetime.fromisoformat(date_text[:10]).replace(
            hour=whole_hour,
            minute=minute,
            second=0,
            microsecond=0,
            tzinfo=tz,
        )
        return local_dt.isoformat()
    except Exception:
        return ""


def _forecast_observation_row(
    *,
    source_id: str,
    event_date: str,
    station_id: str,
    forecast_peak_c: Optional[float] = None,
    distribution: Optional[Mapping[str, float]] = None,
    forecast_group: str = "",
    confidence: Optional[float] = None,
    precision: Optional[float] = None,
    valid_time: str = "",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "forecast_group": forecast_group,
        "distribution": dict(distribution or {}),
        "confidence": confidence,
        "precision": precision,
        "adapter": "posterior_pipeline_entry_runtime_v1",
    }
    return build_source_observation(
        source_id=source_id,
        station_id=station_id,
        event_date=event_date,
        valid_time=valid_time,
        forecast_peak_c=forecast_peak_c,
        payload=payload,
        overrides={
            "source_class": SOURCE_CLASSES["forecast_weak"],
            "role": "forecast_weak_likelihood_adapter",
            "hard_eligible": False,
            "demotion_reason": "forecast_source_not_hard_observation",
        },
    ).as_dict(include_payload=True)


SOFT_FORECAST_MANAGER_SOURCE_IDS = {
    "wu_forecast_high",
    "weather_com_forecast_peak",
    "twc_forecast_peak",
    "twc_forecast_hourly_peak",
    "zuuu_taf_forecast_peak",
}


def _source_manager_observations_from_features(
    *,
    features: Mapping[str, Any],
    event_date: str,
    station_id: str,
    fact_as_of: str,
    source_classes: Tuple[str, ...],
    soft_forecast_allowed: bool = True,
    hard_observation: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    source_payloads = source_payloads_from_fact_payload(
        {"weather_likelihood_features": dict(features or {})},
        station_id=station_id,
        event_date=event_date,
        fact_as_of=fact_as_of,
        hard_observation=hard_observation,
    )
    filtered_payloads: List[Mapping[str, Any]] = []
    for source in source_payloads:
        source_id = str(source.get("source_id") or source.get("source") or "").strip()
        if not soft_forecast_allowed and source_id in SOFT_FORECAST_MANAGER_SOURCE_IDS:
            continue
        filtered_payloads.append(source)
    trace = build_source_acquisition_trace(
        sources=filtered_payloads,
        station_id=station_id,
        event_date=event_date,
        fetched_at=fact_as_of,
        include_payload=True,
    )
    allowed_classes = set(source_classes)
    return [
        dict(row)
        for row in trace.get("source_observations") or []
        if isinstance(row, Mapping) and str(row.get("source_class") or "") in allowed_classes
    ]


def _forecast_weak_source_observations(
    *,
    pre_items: List[Dict[str, Any]],
    labels: List[str],
    features: Mapping[str, Any],
    weights: Mapping[str, Any],
    model_cache_freshness: Optional[float],
    historical_context: Optional[Mapping[str, Any]],
    event_date: str,
    as_of: str = "",
) -> List[Dict[str, Any]]:
    station_id = _feature_text(features, "station_id", "weather_station_id", "icao", "airport") or "ZUUU"
    observations: List[Dict[str, Any]] = []
    history = historical_context if isinstance(historical_context, Mapping) else {}
    model_weight_multipliers = (
        history.get("model_weight_multipliers") if isinstance(history.get("model_weight_multipliers"), Mapping) else {}
    )
    soft_scope = _soft_forecast_scope_meta(features)
    soft_forecast_allowed = bool(soft_scope.get("allowed"))
    freshness = _clamp01(model_cache_freshness)
    if freshness is None:
        freshness = 1.0

    active_models: List[str] = []
    for item in pre_items:
        for model in list(item.get("active_models") or []):
            text = str(model or "").strip()
            if text and text not in active_models:
                active_models.append(text)

    for model in active_models:
        distribution_values: List[float] = []
        has_value = False
        for item in pre_items:
            per_model = item.get("per_model_q") if isinstance(item.get("per_model_q"), Mapping) else {}
            band = per_model.get(model) if isinstance(per_model.get(model), Mapping) else {}
            q_val = _clamp01(_to_float(band.get("q_round") or band.get("q_raw")))
            if q_val is None:
                distribution_values.append(0.0)
                continue
            has_value = True
            distribution_values.append(float(q_val))
        if not has_value or sum(distribution_values) <= EPS:
            continue
        raw_weight = _to_float(weights.get(model))
        weight = float(raw_weight) if raw_weight is not None and raw_weight > 0 else 1.0
        mult = _to_float(model_weight_multipliers.get(model))
        if mult is not None and mult > 0:
            weight *= float(mult)
        normalized = _normalize(distribution_values)
        observations.append(
            _forecast_observation_row(
                source_id=f"model_distribution:{model}",
                event_date=event_date,
                station_id=station_id,
                distribution={label: normalized[idx] for idx, label in enumerate(labels)},
                forecast_group="model",
                confidence=max(0.05, min(1.0, float(freshness))),
                precision=max(0.0, float(weight) * max(0.25, float(freshness))),
            )
        )

    observations.extend(
        _source_manager_observations_from_features(
            features=features,
            event_date=event_date,
            station_id=station_id,
            fact_as_of=as_of,
            source_classes=(SOURCE_CLASSES["forecast_weak"], SOURCE_CLASSES["diagnostic_current"]),
            soft_forecast_allowed=soft_forecast_allowed,
        )
    )
    return observations


def _hierarchical_observation_inputs(features: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    def put_float(target: str, *keys: str) -> None:
        value = _feature_float(features, *keys)
        if value is not None:
            out[target] = value

    def put_text(target: str, *keys: str) -> None:
        value = _feature_text(features, *keys)
        if value:
            out[target] = value

    def put_bool(target: str, *keys: str) -> None:
        for key in keys:
            if key not in features:
                continue
            value = _bool_feature(features.get(key))
            if value is not None:
                out[target] = bool(value)
                return

    put_float(
        "current_temp_c",
        "current_temp_c",
        "observed_current_temp_c",
        "observed_temp_c",
        "metar_temperature_c",
        "temperature_c",
        "zuuu_metar_raw_awc_latest_temp_c",
    )
    put_float(
        "observed_today_max_c",
        "observed_today_max_c",
        "today_max_c",
        "daily_max_observed_c",
        "zuuu_metar_raw_awc_today_max_c",
    )
    put_float("dewpoint_c", "dewpoint_c", "metar_dewpoint_c", "dew_point_c", "zuuu_metar_raw_awc_latest_dewpoint_c")
    put_float("visibility_m", "visibility_m", "metar_visibility_m", "visibility")
    put_float("cloud_cover_pct", "cloud_cover_pct", "cloud_pct", "metar_cloud_cover_pct")
    put_float("low_cloud_cover_pct", "low_cloud_cover_pct", "low_cloud_pct")
    put_float("wind_speed_mps", "wind_speed_mps", "metar_wind_speed_mps")
    put_float("pressure_hpa", "pressure_hpa", "altimeter_hpa", "metar_pressure_hpa")
    put_float("dewpoint_trend_c", "dewpoint_trend_c", "dewpoint_trend_c_per_hour")
    put_float(
        "momentum_rate_c_per_hour",
        "momentum_rate_c_per_hour",
        "temperature_rate_c_per_hour",
        "temperature_rise_rate_c_per_hour",
        "observed_temp_rise_rate_c_per_hour",
        "metar_temp_rise_rate_c_per_hour",
        "temperature_momentum_posterior_effective_c_per_hour",
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_1h_c_per_hour",
    )
    for window in TEMPERATURE_MOMENTUM_WINDOWS:
        put_float(
            f"temperature_momentum_{window}_c_per_hour",
            f"temperature_momentum_{window}_c_per_hour",
            f"temperature_rise_rate_{window}_c_per_hour",
            f"zuuu_metar_raw_awc_temperature_momentum_{window}_c_per_hour",
        )
    put_float(
        "temperature_trajectory_stable_rate_c_per_hour",
        "temperature_trajectory_stable_rate_c_per_hour",
        "temperature_momentum_stable_c_per_hour",
    )
    put_float(
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_posterior_effective_c_per_hour",
    )
    put_float("local_hour", "observation_hour_local", "local_hour", "hour_local")
    put_text(
        "temperature_trajectory_realization_state",
        "temperature_trajectory_realization_state",
        "temperature_momentum_realization_state",
    )
    put_text("weather_text", "weather_text", "metar_weather", "weather", "wx", "zuuu_metar_raw_awc_latest_raw")
    put_text("cloud_text", "cloud_text", "clouds", "metar_cloud_text", "zuuu_metar_raw_awc_latest_raw")
    put_bool(
        "hard_source_final_daily_max",
        "hard_source_final_daily_max",
        "hard_source_settlement_final",
        "observed_today_max_final",
        "observed_today_max_is_final",
        "observed_today_max_is_official",
        "observed_today_max_trusted_final",
        "temperature_observation_final_daily_max",
        "no_more_warming_possible",
    )
    return out


def _hard_observation_for_source_manager(
    *,
    features: Mapping[str, Any],
    observation_inputs: Mapping[str, Any],
) -> Dict[str, Any]:
    current_temp_c = _to_float(observation_inputs.get("current_temp_c"))
    observed_today_max_c = _to_float(observation_inputs.get("observed_today_max_c"))
    if current_temp_c is None and observed_today_max_c is None:
        return {}
    observed_at = _feature_text(
        features,
        "zuuu_metar_raw_awc_latest_at",
        "valid_time",
        "valid_time_local",
        "observation_time",
    )
    fetched_at = _feature_text(features, "zuuu_metar_raw_awc_fetched_at", "fetched_at", "response_completed_at")
    raw = _feature_text(
        features,
        "zuuu_metar_raw_awc_latest_raw",
        "metar_raw",
        "raw_metar",
        "zuuu_metar_raw_awc_source_url",
    )
    return {
        "ok": True,
        "source_id": _feature_text(features, "hard_observation_source_id", "metar_source_id") or "zuuu_metar_raw_awc",
        "observed_at": observed_at,
        "fetched_at": fetched_at,
        "fetch_duration_ms": _feature_float(
            features,
            "zuuu_metar_raw_awc_fetch_duration_ms",
            "fetch_duration_ms",
            "response_duration_ms",
        ),
        "temperature_c": current_temp_c,
        "dewpoint_c": _to_float(observation_inputs.get("dewpoint_c")),
        "observed_today_max_c": observed_today_max_c,
        "value_c": observed_today_max_c if observed_today_max_c is not None else current_temp_c,
        "raw": raw,
        "final_bucket_lock": bool(observation_inputs.get("hard_source_final_daily_max")),
    }


def _hard_observation_source_observations(
    *,
    features: Mapping[str, Any],
    event_date: str,
    as_of: str = "",
) -> List[Dict[str, Any]]:
    observation_inputs = _hierarchical_observation_inputs(features)
    if not observation_inputs:
        return []
    station_id = _feature_text(features, "station_id", "weather_station_id", "icao", "airport") or "ZUUU"
    hard_observation = _hard_observation_for_source_manager(
        features=features,
        observation_inputs=observation_inputs,
    )
    if not hard_observation:
        return []
    return _source_manager_observations_from_features(
        features=features,
        event_date=event_date,
        station_id=station_id,
        fact_as_of=as_of,
        source_classes=(SOURCE_CLASSES["hard_observation"],),
        hard_observation=hard_observation,
    )


def _posterior_distribution_map(pipeline: Mapping[str, Any], labels: List[str]) -> Dict[str, float]:
    raw = pipeline.get("posterior_temperature_distribution_map")
    if isinstance(raw, Mapping):
        values = [_clamp01(_to_float(raw.get(label))) or 0.0 for label in labels]
        normalized = _normalize(values)
        return {label: float(normalized[idx]) for idx, label in enumerate(labels)}
    rows = pipeline.get("posterior_temperature_distribution")
    if isinstance(rows, list):
        by_label: Dict[str, float] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            label = str(row.get("bucket") or row.get("label") or "").strip()
            value = _clamp01(_to_float(row.get("posterior")))
            if label and value is not None:
                by_label[label] = float(value)
        values = [by_label.get(label, 0.0) for label in labels]
        normalized = _normalize(values)
        return {label: float(normalized[idx]) for idx, label in enumerate(labels)}
    return {label: value for label, value in zip(labels, _normalize([1.0 for _ in labels]))}


def _hierarchical_alpha_total(
    pipeline: Mapping[str, Any],
    *,
    labels: List[str],
    prior_strength: float,
) -> float:
    runtime_outputs = pipeline.get("runtime_outputs") if isinstance(pipeline.get("runtime_outputs"), Mapping) else {}
    forecast_output = (
        runtime_outputs.get("forecast_weak_likelihood")
        if isinstance(runtime_outputs.get("forecast_weak_likelihood"), Mapping)
        else {}
    )
    precision_cap = (
        forecast_output.get("precision_cap")
        if isinstance(forecast_output.get("precision_cap"), Mapping)
        else {}
    )
    forecast_precision = _to_float(precision_cap.get("total_capped_precision")) or 0.0
    beta_weak = _to_float(pipeline.get("beta_weak")) or 0.0
    beta_strong = _to_float(pipeline.get("beta_strong")) or 0.0
    strong_proxy = max(1.0, float(len(labels) or 1))
    return max(1.0, float(prior_strength) + beta_weak * forecast_precision + beta_strong * strong_proxy)


def _write_hierarchical_posterior_to_items(
    *,
    pre_items: List[Dict[str, Any]],
    labels: List[str],
    pipeline: Mapping[str, Any],
    z_value: float,
    z_meta: Mapping[str, Any],
    prior_strength: float,
) -> Dict[str, Any]:
    posterior_map = _posterior_distribution_map(pipeline, labels)
    posterior_probs = [float(posterior_map.get(label, 0.0) or 0.0) for label in labels]
    alpha_total = _hierarchical_alpha_total(pipeline, labels=labels, prior_strength=prior_strength)
    alphas = [max(0.0, p * alpha_total) for p in posterior_probs]
    layer_contributions = (
        pipeline.get("layer_contributions")
        if isinstance(pipeline.get("layer_contributions"), Mapping)
        else {}
    )
    log_trace_by_bucket = {}
    runtime_outputs = pipeline.get("runtime_outputs") if isinstance(pipeline.get("runtime_outputs"), Mapping) else {}
    posterior_output = (
        runtime_outputs.get("hierarchical_temperature_posterior")
        if isinstance(runtime_outputs.get("hierarchical_temperature_posterior"), Mapping)
        else {}
    )
    log_trace = posterior_output.get("log_score_trace") if isinstance(posterior_output.get("log_score_trace"), list) else []
    for row in log_trace:
        if isinstance(row, Mapping):
            log_trace_by_bucket[str(row.get("bucket") or "")] = dict(row)

    for idx, item in enumerate(pre_items):
        q_obj = item.get("q")
        if not isinstance(q_obj, dict) or idx >= len(labels):
            continue
        label = labels[idx]
        posterior = float(posterior_probs[idx])
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
        item["q_temperature_bucket_posterior"] = posterior
        item["q_temperature_bucket_probability_mode"] = "hierarchical_joint_likelihood"
        item["q_temperature_bucket_label"] = label
        item["q_contract_payout_posterior"] = posterior
        log_row = log_trace_by_bucket.get(label, {})
        item["q_bayes_meta"] = {
            "enabled": True,
            "source": HIERARCHICAL_PIPELINE_VERSION,
            "posterior_engine": HIERARCHICAL_POSTERIOR_VERSION,
            "posterior_temperature_distribution_version": HIERARCHICAL_POSTERIOR_VERSION,
            "posterior_temperature_distribution_status": "ok" if pipeline.get("ok") else "not_ok",
            "z": float(z_value),
            "z_stage": z_meta.get("stage"),
            "z_source": z_meta.get("source"),
            "prior_strength": prior_strength,
            "phase": pipeline.get("phase"),
            "beta_weak": pipeline.get("beta_weak"),
            "beta_strong": pipeline.get("beta_strong"),
            "phase_reason": pipeline.get("phase_reason"),
            "hard_mask_reason": pipeline.get("hard_mask_reason"),
            "score_components": {
                "prior": log_row.get("prior_log"),
                "forecast_weak_likelihood": log_row.get("forecast_weak_log"),
                "metar_strong_likelihood": log_row.get("metar_strong_log"),
                "hard_mask": log_row.get("hard_mask_log"),
            },
            "layer_contributions": layer_contributions,
            "hierarchical_posterior_pipeline_schema": pipeline.get("schema"),
            "temperature_bucket_posterior": posterior,
            "temperature_bucket_probability_mode": "hierarchical_joint_likelihood",
            "temperature_bucket_label": label,
        }
    return {
        "posterior_probs": posterior_probs,
        "alpha_total": alpha_total,
    }


def _apply_hierarchical_bayesian_probability_update(
    pre_items: List[Dict[str, Any]],
    *,
    weights: Mapping[str, Any],
    model_cache_freshness: Optional[float] = None,
    days_to_event: Optional[float] = None,
    lcb_stage_tz: Optional[str] = None,
    historical_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    labels = _bayes_item_labels(pre_items)
    history = historical_context if isinstance(historical_context, Mapping) else {}
    likelihood_features = _extract_likelihood_features(pre_items, history)
    station_timezone = _station_timezone_from_features(likelihood_features)
    event_date = _event_date_for_hierarchical_pipeline(
        features=likelihood_features,
        station_timezone=station_timezone,
        days_to_event=days_to_event,
    )
    as_of = _as_of_for_hierarchical_pipeline(
        likelihood_features,
        event_date=event_date,
        station_timezone=station_timezone,
    )
    prior_distribution = _root_prior_distribution(
        labels=labels,
        historical_context=history,
    )
    source_observations = _forecast_weak_source_observations(
        pre_items=pre_items,
        labels=labels,
        features=likelihood_features,
        weights=weights,
        model_cache_freshness=model_cache_freshness,
        historical_context=history,
        event_date=event_date,
        as_of=as_of,
    )
    source_observations.extend(
        _hard_observation_source_observations(
            features=likelihood_features,
            event_date=event_date,
            as_of=as_of,
        )
    )
    observation_inputs = _hierarchical_observation_inputs(likelihood_features)
    prior_strength = max(0.0, _env_float("PM_BAYES_PROBABILITY_PRIOR_STRENGTH", 12.0))
    z_meta = resolve_lcb_stage_z(
        "PM_BAYES_PROBABILITY_Z",
        default=1.28,
        days_to_event=days_to_event,
        tz_name=lcb_stage_tz,
        stage_defaults=DEFAULT_BAYES_STAGE_Z,
    )
    z_value = max(0.0, _to_float(z_meta.get("z")) or 1.28)
    pipeline = build_hierarchical_posterior_pipeline(
        buckets=labels,
        prior_distribution=prior_distribution,
        event_date=event_date,
        as_of=as_of,
        station_timezone=station_timezone,
        source_observations=source_observations,
        observation_inputs=observation_inputs,
        rounding_rule=_bayes_item_rounding_rule(pre_items),
        posterior_run_id=_feature_text(likelihood_features, "posterior_run_id", "run_id"),
        fact_snapshot_id=_feature_text(likelihood_features, "fact_snapshot_id", "snapshot_id"),
    )
    writeback = _write_hierarchical_posterior_to_items(
        pre_items=pre_items,
        labels=labels,
        pipeline=pipeline,
        z_value=z_value,
        z_meta=z_meta,
        prior_strength=prior_strength,
    )
    return {
        "enabled": True,
        "source": HIERARCHICAL_PIPELINE_VERSION,
        "posterior_engine": HIERARCHICAL_POSTERIOR_VERSION,
        "hierarchical_posterior_pipeline_schema": pipeline.get("schema"),
        "posterior_temperature_distribution": pipeline.get("posterior_temperature_distribution"),
        "posterior_temperature_distribution_map": pipeline.get("posterior_temperature_distribution_map"),
        "layer_contributions": pipeline.get("layer_contributions"),
        "hard_constraints": pipeline.get("hard_constraints"),
        "phase": pipeline.get("phase"),
        "beta_weak": pipeline.get("beta_weak"),
        "beta_strong": pipeline.get("beta_strong"),
        "phase_reason": pipeline.get("phase_reason"),
        "hard_mask_reason": pipeline.get("hard_mask_reason"),
        "source_lineage": pipeline.get("source_lineage"),
        "na_reasons": pipeline.get("na_reasons"),
        "normalization": pipeline.get("normalization"),
        "hierarchical_posterior_pipeline": pipeline,
        "posterior_probs": writeback.get("posterior_probs"),
        "alpha_total": writeback.get("alpha_total"),
        "prior_strength": prior_strength,
        "z": float(z_value),
        "z_stage": z_meta.get("stage"),
        "z_source": z_meta.get("source"),
    }


def apply_bayesian_probability_update(
    pre_items: List[Dict[str, Any]],
    *,
    weights: Mapping[str, Any],
    model_cache_freshness: Optional[float] = None,
    days_to_event: Optional[float] = None,
    lcb_stage_tz: Optional[str] = None,
    historical_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply the single production Bayesian posterior entry.

    The production path is the hierarchical joint likelihood pipeline: root
    prior, forecast weak likelihood, METAR strong likelihood, D1/D0 beta
    weights, and hard mask.
    """

    if not _to_bool(os.environ.get("PM_BAYES_PROBABILITY_ENABLED"), default=True):
        return {"enabled": False, "source": "disabled"}
    if not pre_items:
        return {"enabled": True, "source": "empty"}
    try:
        return _apply_hierarchical_bayesian_probability_update(
            pre_items,
            weights=weights,
            model_cache_freshness=model_cache_freshness,
            days_to_event=days_to_event,
            lcb_stage_tz=lcb_stage_tz,
            historical_context=historical_context,
        )
    except Exception as exc:
        return {
            "enabled": True,
            "source": "hierarchical_posterior_pipeline_error",
            "posterior_engine": HIERARCHICAL_POSTERIOR_VERSION,
            "error": str(exc),
        }
