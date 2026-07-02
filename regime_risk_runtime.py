from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


EPS = 1e-12


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
    if val is None:
        return float(default)
    return float(val)


def _date_key(value: Any) -> str:
    return str(value or "").split("T", 1)[0][:10]


def _parse_date(value: Any) -> Optional[datetime.date]:
    text = _date_key(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_unit(value: Any) -> str:
    return str(value or "").strip().upper().replace("°", "")


def _feature_float(features: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        val = _to_float(features.get(key))
        if val is not None:
            return float(val)
    return None


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


def _observed_path_temp_from_features(features: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    current = _feature_float(
        features,
        "observed_path_temp_c",
        "observed_temp_c",
        "current_temp_c",
        "temperature_now_c",
        "metar_temperature_c",
    )
    today_max = _feature_float(features, "observed_today_max_c", "daily_max_observed_c")
    if current is not None and today_max is not None:
        if today_max >= current:
            return float(today_max), "observed_today_max_floor"
        return float(current), "observed_current"
    if today_max is not None:
        return float(today_max), "observed_today_max"
    if current is not None:
        return float(current), "observed_current"
    return None, ""


def _confirmed_warming_windows(features: Mapping[str, Any]) -> List[str]:
    raw = str(features.get("temperature_momentum_posterior_confirmed_windows") or "").strip()
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    out: List[str] = []
    for label in ("30m", "45m", "1h", "2h", "3h", "6h"):
        rate = _to_float(features.get(f"temperature_momentum_{label}_c_per_hour"))
        if rate is not None and rate >= 0.25:
            out.append(label)
    return out


def _stable_warming_rate(features: Mapping[str, Any]) -> Optional[float]:
    for key in (
        "temperature_momentum_posterior_effective_c_per_hour",
        "temperature_trajectory_stable_rate_c_per_hour",
        "temperature_momentum_effective_c_per_hour",
        "temperature_rise_rate_c_per_hour",
        "observed_temp_rise_rate_c_per_hour",
        "metar_temp_rise_rate_c_per_hour",
    ):
        val = _to_float(features.get(key))
        if val is not None:
            return float(val)
    peak = features.get("peak_estimator")
    if isinstance(peak, Mapping):
        val = _to_float(peak.get("temperature_momentum_effective_c_per_hour"))
        if val is not None:
            return float(val)
        terms = peak.get("energy_balance_terms")
        if isinstance(terms, Mapping):
            val = _to_float(terms.get("temperature_trajectory_rate_c_per_hour"))
            if val is not None:
                return float(val)
    return None


def _peak_meta(features: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = features.get("peak_estimator")
    return meta if isinstance(meta, Mapping) else {}


def _hours_to_peak(features: Mapping[str, Any]) -> Optional[float]:
    peak = _peak_meta(features)
    val = _to_float(peak.get("hours_to_peak_window"))
    if val is not None:
        return float(val)
    val = _feature_float(features, "hours_to_peak_window", "hours_to_peak")
    if val is not None:
        return float(val)
    hour = _feature_float(features, "observation_hour_local", "hour_local", "local_hour")
    peak_hour = _feature_float(features, "expected_peak_hour_local", "local_peak_hour", "peak_hour_local")
    if hour is not None and peak_hour is not None:
        return float(peak_hour) - float(hour)
    return None


def _load_recent_settled_tmax(
    conn: sqlite3.Connection,
    *,
    city: Optional[str],
    station_id: Optional[str],
    market_var: str,
    market_unit: str,
    target_event_date: str,
    as_of: str,
) -> List[Dict[str, Any]]:
    target_day = _parse_date(target_event_date)
    if target_day is None:
        return []
    start_day = (target_day - timedelta(days=10)).isoformat()
    station = str(station_id or "").strip().upper()
    city_like = f"%{str(city or '').strip().lower()}%" if str(city or "").strip() else "%"
    rows: List[Dict[str, Any]] = []
    seen = set()
    try:
        daily_rows = conn.execute(
            """
            SELECT event_date, settlement_actual, created_at, resolution_source
            FROM event_daily_temps
            WHERE event_date >= ?
              AND event_date < ?
              AND datetime(created_at) < datetime(?)
              AND settlement_actual IS NOT NULL
              AND (? = '' OR market_var = ?)
              AND (? = '' OR market_unit = ?)
              AND (? = '' OR upper(coalesce(station_id, '')) = ? OR lower(coalesce(city, '')) LIKE ?)
            ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
            LIMIT 50
            """,
            (
                start_day,
                target_event_date,
                as_of,
                str(market_var or "").strip(),
                str(market_var or "").strip(),
                str(market_unit or "").strip(),
                str(market_unit or "").strip(),
                station,
                station,
                city_like,
            ),
        ).fetchall()
    except sqlite3.Error:
        daily_rows = []
    for row in daily_rows:
        event_date = _date_key(row["event_date"])
        actual = _to_float(row["settlement_actual"])
        if not event_date or event_date in seen or actual is None:
            continue
        seen.add(event_date)
        rows.append(
            {
                "event_date": event_date,
                "actual_c": float(actual),
                "created_at": str(row["created_at"] or ""),
                "source": str(row["resolution_source"] or "event_daily_temps"),
            }
        )
    try:
        run_rows = conn.execute(
            """
            SELECT event_date, settlement_actual, created_at
            FROM event_runs
            WHERE event_date >= ?
              AND event_date < ?
              AND datetime(created_at) < datetime(?)
              AND settlement_actual IS NOT NULL
              AND (? = '' OR market_var = ?)
              AND (? = '' OR market_unit = ?)
              AND (? = '' OR upper(coalesce(station_id, '')) = ? OR lower(coalesce(city, '')) LIKE ?)
            ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
            LIMIT 50
            """,
            (
                start_day,
                target_event_date,
                as_of,
                str(market_var or "").strip(),
                str(market_var or "").strip(),
                str(market_unit or "").strip(),
                str(market_unit or "").strip(),
                station,
                station,
                city_like,
            ),
        ).fetchall()
    except sqlite3.Error:
        run_rows = []
    for row in run_rows:
        event_date = _date_key(row["event_date"])
        actual = _to_float(row["settlement_actual"])
        if not event_date or event_date in seen or actual is None:
            continue
        seen.add(event_date)
        rows.append(
            {
                "event_date": event_date,
                "actual_c": float(actual),
                "created_at": str(row["created_at"] or ""),
                "source": "event_runs.settlement_actual",
            }
        )
    try:
        observation_rows = conn.execute(
            """
            SELECT event_date, value, unit, source_kind, created_at
            FROM station_observations
            WHERE event_date >= ?
              AND event_date < ?
              AND datetime(created_at) < datetime(?)
              AND value IS NOT NULL
              AND (? = '' OR market_var = ?)
              AND (? = '' OR upper(coalesce(station_id, '')) = ?)
            ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
            LIMIT 5000
            """,
            (
                start_day,
                target_event_date,
                as_of,
                str(market_var or "").strip(),
                str(market_var or "").strip(),
                station,
                station,
            ),
        ).fetchall()
    except sqlite3.Error:
        observation_rows = []
    observed_by_day: Dict[str, Dict[str, Any]] = {}
    for row in observation_rows:
        event_date = _date_key(row["event_date"])
        if not event_date or event_date in seen:
            continue
        value = _to_float(row["value"])
        if value is None:
            continue
        unit = _normalize_unit(row["unit"])
        value_c = (float(value) - 32.0) * 5.0 / 9.0 if unit == "F" else float(value)
        current = observed_by_day.get(event_date)
        if current is None or value_c > float(current["actual_c"]):
            observed_by_day[event_date] = {
                "event_date": event_date,
                "actual_c": value_c,
                "created_at": str(row["created_at"] or ""),
                "source": "station_observations." + str(row["source_kind"] or "observed_tmax"),
                "provisional": True,
            }
    for event_date in sorted(observed_by_day.keys(), reverse=True):
        if event_date in seen:
            continue
        seen.add(event_date)
        rows.append(observed_by_day[event_date])
    rows.sort(key=lambda item: item["event_date"], reverse=True)
    return rows


def _station_observation_fallback(
    conn: sqlite3.Connection,
    *,
    target_event_date: str,
    station_id: Optional[str],
    market_var: str,
    as_of: str,
) -> Dict[str, Any]:
    station = str(station_id or "").strip().upper()
    try:
        rows = conn.execute(
            """
            SELECT value, unit, source_kind, observed_at, published_at, created_at
            FROM station_observations
            WHERE event_date = ?
              AND (? = '' OR upper(coalesce(station_id, '')) = ?)
              AND (? = '' OR market_var = ?)
              AND datetime(created_at) <= datetime(?)
              AND value IS NOT NULL
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT 200
            """,
            (
                target_event_date,
                station,
                station,
                str(market_var or "").strip(),
                str(market_var or "").strip(),
                as_of,
            ),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    values: List[float] = []
    latest: Dict[str, Any] = {}
    for row in rows:
        value = _to_float(row["value"])
        if value is None:
            continue
        unit = _normalize_unit(row["unit"])
        value_c = (float(value) - 32.0) * 5.0 / 9.0 if unit == "F" else float(value)
        values.append(value_c)
        if not latest:
            latest = {
                "observed_temp_c": value_c,
                "observed_temp_source": str(row["source_kind"] or "station_observations"),
                "observed_at": str(row["observed_at"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
    if values:
        latest["observed_today_max_c"] = max(values)
        latest["source"] = "station_observations"
    return latest


def build_post_cool_rebound_risk_features(
    *,
    target_event_date: str,
    weather_features: Mapping[str, Any],
    recent_daily_tmax: Sequence[Mapping[str, Any]],
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    features = dict(weather_features or {})
    updated_at = as_of or _now_iso()
    event_day = _parse_date(target_event_date)
    if event_day is None:
        return {
            "status": "invalid_target_event_date",
            "state": "off",
            "enabled": False,
            "source": "post_cool_rebound_risk",
            "updated_at": updated_at,
        }
    unit = _normalize_unit(features.get("market_unit") or features.get("unit"))
    market_var = str(features.get("market_var") or "").strip()
    if market_var and market_var != "temperature_2m_max":
        return {
            "status": "not_temperature_market",
            "state": "off",
            "enabled": False,
            "source": "post_cool_rebound_risk",
            "updated_at": updated_at,
        }
    if unit and unit != "C":
        return {
            "status": "unsupported_temperature_unit",
            "state": "off",
            "enabled": False,
            "market_unit": unit,
            "source": "post_cool_rebound_risk",
            "updated_at": updated_at,
        }

    prior_days = [
        row for row in recent_daily_tmax
        if _parse_date(row.get("event_date")) is not None and _parse_date(row.get("event_date")) < event_day
    ]
    prior_days = sorted(prior_days, key=lambda row: str(row.get("event_date") or ""), reverse=True)
    prev1 = _to_float(prior_days[0].get("actual_c")) if len(prior_days) >= 1 else None
    prev2 = _to_float(prior_days[1].get("actual_c")) if len(prior_days) >= 2 else None
    prev_vals = [float(v) for v in (prev1, prev2) if v is not None]
    cool_trough = min(prev_vals) if prev_vals else None
    observed_path, observed_path_source = _observed_path_temp_from_features(features)
    forecast_peak = _feature_float(
        features,
        "forecast_peak_temp_c",
        "forecast_peak_temp",
        "peak_estimator_mu_c",
        "open_meteo_peak_temp_c",
    )
    peak = _peak_meta(features)
    peak_mu = _to_float(peak.get("mu_c"))
    extrapolation = _to_float(peak.get("extrapolation_c"))
    sigma = _to_float(peak.get("extrapolation_sigma_c")) or _to_float(peak.get("sigma_c"))
    rate = _stable_warming_rate(features)
    confirmed_windows = _confirmed_warming_windows(features)
    hour_local = _feature_float(features, "observation_hour_local", "hour_local", "local_hour")
    expected_peak_hour = _feature_float(features, "expected_peak_hour_local", "local_peak_hour", "peak_hour_local")
    hours_to_peak = _hours_to_peak(features)

    cool_min_gap = max(0.0, _env_float("PM_POST_COOL_REBOUND_MIN_EXPECTED_REBOUND_C", 2.0))
    obs_rebound_min = max(0.0, _env_float("PM_POST_COOL_REBOUND_MIN_OBS_REBOUND_C", 3.0))
    min_rate = max(0.0, _env_float("PM_POST_COOL_REBOUND_MIN_RATE_C_PER_H", 0.25))
    peak_buffer_h = max(0.0, _env_float("PM_POST_COOL_REBOUND_PEAK_BUFFER_H", 0.25))
    cool_gate = cool_trough is not None and forecast_peak is not None and (forecast_peak - cool_trough) >= cool_min_gap
    observed_rebound_c = (
        float(observed_path) - float(cool_trough)
        if observed_path is not None and cool_trough is not None
        else None
    )
    obs_gate = observed_rebound_c is not None and observed_rebound_c >= obs_rebound_min
    rate_gate = rate is not None and rate >= min_rate and len(confirmed_windows) >= 2
    after_peak_gate = hours_to_peak is not None and hours_to_peak < -peak_buffer_h
    near_model_gate = False
    if observed_path is not None and forecast_peak is not None:
        near_model_gate = observed_path >= forecast_peak - _env_float("PM_POST_COOL_REBOUND_NEAR_MODEL_BAND_C", 0.75)
    if extrapolation is not None and forecast_peak is not None:
        near_model_gate = near_model_gate or extrapolation >= forecast_peak - 0.25
    observed_breakthrough_buffer = max(
        0.0,
        _env_float("PM_POST_COOL_REBOUND_OBS_BREAKTHROUGH_BUFFER_C", 0.50),
    )
    observed_breakthrough_gate = (
        obs_gate
        and near_model_gate
        and observed_path is not None
        and forecast_peak is not None
        and observed_path >= forecast_peak + observed_breakthrough_buffer
    )

    missing: List[str] = []
    if cool_trough is None:
        missing.append("recent_settled_tmax")
    if forecast_peak is None:
        missing.append("forecast_peak")
    if observed_path is None:
        missing.append("observed_temperature")
    if rate is None and not observed_breakthrough_gate:
        missing.append("warming_rate")
    if hours_to_peak is None and not observed_breakthrough_gate:
        missing.append("hours_to_peak")

    projected_by_rate = None
    if observed_path is not None and rate is not None and hours_to_peak is not None:
        projected_by_rate = float(observed_path) + max(0.0, float(rate)) * max(0.0, float(hours_to_peak))
    prior_target_candidates = [
        value
        for value in (
            forecast_peak,
            peak_mu,
        )
        if value is not None
    ]
    prior_target_peak = max(prior_target_candidates) if prior_target_candidates else None
    target_candidates = [
        value
        for value in (
            observed_path,
            forecast_peak,
            peak_mu,
            extrapolation,
            projected_by_rate,
        )
        if value is not None
    ]
    target_peak = max(target_candidates) if target_candidates else None
    if sigma is None or sigma <= 0:
        sigma = _env_float("PM_POST_COOL_REBOUND_SIGMA_C", 0.80)
    sigma = max(0.25, float(sigma))

    rebound_scale = 0.0
    if observed_rebound_c is not None and obs_rebound_min > 0:
        rebound_scale = min(1.25, max(0.0, observed_rebound_c / obs_rebound_min))
    rate_scale = 0.0
    if rate is not None:
        full_rate = max(min_rate, _env_float("PM_POST_COOL_REBOUND_FULL_RATE_C_PER_H", 0.70))
        rate_scale = min(1.25, max(0.0, float(rate) / max(EPS, full_rate)))
    time_scale = 0.75
    if hours_to_peak is not None:
        if hours_to_peak < -peak_buffer_h:
            time_scale = 0.0
        elif hours_to_peak <= 3.5:
            time_scale = 1.0
        else:
            time_scale = 0.75
    intraday_time_scale = time_scale
    if observed_breakthrough_gate and (hours_to_peak is None or after_peak_gate):
        intraday_time_scale = max(intraday_time_scale, 0.75)
    intraday_rate_scale = rate_scale if rate_gate else 0.0
    if observed_breakthrough_gate:
        intraday_rate_scale = max(intraday_rate_scale, _env_float("PM_POST_COOL_REBOUND_OBS_BREAKTHROUGH_RATE_SCALE", 0.85))

    prior_missing: List[str] = []
    if cool_trough is None:
        prior_missing.append("recent_settled_tmax")
    if forecast_peak is None:
        prior_missing.append("forecast_peak")
    if prior_target_peak is None:
        prior_missing.append("prior_target_peak")
    prior_state = "off"
    prior_status = "off"
    if prior_missing:
        prior_status = "missing_" + ",".join(prior_missing)
    elif not cool_gate:
        prior_status = "no_post_cool_rebound_setup"
    elif after_peak_gate:
        prior_status = "after_peak_window"
    else:
        prior_state = "prior"
        prior_status = "regime_prior"
    expected_rebound_c = (
        float(forecast_peak) - float(cool_trough)
        if forecast_peak is not None and cool_trough is not None
        else None
    )
    prior_rebound_scale = 0.0
    if expected_rebound_c is not None and cool_min_gap > 0:
        prior_rebound_scale = min(1.0, max(0.0, expected_rebound_c / max(EPS, cool_min_gap * 1.5)))
    prior_time_scale = time_scale if not after_peak_gate else 0.0
    prior_evidence_scale = (
        min(1.0, max(0.0, prior_rebound_scale * prior_time_scale))
        if prior_state == "prior"
        else 0.0
    )

    state = "off"
    status = "off"
    if missing:
        status = "missing_" + ",".join(missing)
    elif not cool_gate:
        status = "no_post_cool_rebound_setup"
    elif after_peak_gate and not observed_breakthrough_gate:
        status = "after_peak_window"
    elif not obs_gate or not near_model_gate:
        state = "watch"
        status = "awaiting_intraday_rebound_confirmation"
    elif not rate_gate and not observed_breakthrough_gate:
        state = "watch"
        status = "awaiting_multi_window_warming_confirmation"
    else:
        state = "lift"
        status = "lift_observed_breakthrough" if observed_breakthrough_gate and not rate_gate else "lift"
    evidence_scale = (
        min(1.0, max(0.0, rebound_scale * intraday_rate_scale * intraday_time_scale))
        if state == "lift"
        else 0.0
    )
    prior_features = {
        "enabled": bool(prior_state == "prior" and prior_evidence_scale > 0 and prior_target_peak is not None),
        "status": prior_status,
        "state": prior_state,
        "source": "post_cool_rebound_regime_prior",
        "role": "conditional_climate_prior",
        "updated_at": updated_at,
        "target_event_date": event_day.isoformat(),
        "prev1_actual_c": prev1,
        "prev2_actual_c": prev2,
        "cool_trough_c": cool_trough,
        "forecast_peak_c": forecast_peak,
        "peak_estimator_mu_c": peak_mu,
        "expected_rebound_c": expected_rebound_c,
        "target_peak_c": prior_target_peak,
        "sigma_c": max(0.35, _env_float("PM_POST_COOL_REBOUND_REGIME_PRIOR_SIGMA_C", 1.05)),
        "hour_local": hour_local,
        "expected_peak_hour_local": expected_peak_hour,
        "hours_to_peak_window": hours_to_peak,
        "cool_gate": bool(cool_gate),
        "after_peak_gate": bool(after_peak_gate),
        "missing_fields": prior_missing,
        "evidence_scale": prior_evidence_scale,
        "posterior_lift_pp_cap": max(
            0.0,
            _env_float("PM_BAYES_POST_COOL_REBOUND_REGIME_PRIOR_PP_CAP", 0.02),
        ),
        "provenance": {
            "kind": "external_archive/history_only" if any(str(row.get("source") or "").startswith("open_meteo") for row in prior_days) else "native_live_feature",
            "recent_daily_tmax_source": "event_daily_temps/event_runs plus station_observations fallback visible before as_of",
            "weather_features_source": str(features.get("source") or features.get("auxiliary_source") or ""),
            "guards": [
                "conditional_climate_prior_only",
                "requires_recent_cool_trough",
                "requires_forecast_rebound_setup",
                "does_not_require_intraday_confirmation",
                "pp_capped_before_intraday_likelihood",
                "hard_sources_override_later_in_bayes",
            ],
        },
    }
    intraday_features = {
        "enabled": bool(state == "lift" and evidence_scale > 0 and target_peak is not None),
        "status": status,
        "state": state,
        "source": "post_cool_rebound_intraday_likelihood",
        "role": "intraday_likelihood",
        "updated_at": updated_at,
        "target_event_date": event_day.isoformat(),
        "prev1_actual_c": prev1,
        "prev2_actual_c": prev2,
        "cool_trough_c": cool_trough,
        "forecast_peak_c": forecast_peak,
        "peak_estimator_mu_c": peak_mu,
        "extrapolation_c": extrapolation,
        "projected_by_rate_c": projected_by_rate,
        "target_peak_c": target_peak,
        "sigma_c": sigma,
        "observed_path_temp_c": observed_path,
        "observed_path_temp_source": observed_path_source,
        "observed_rebound_c": observed_rebound_c,
        "rate_c_per_hour": rate,
        "confirmed_windows": confirmed_windows,
        "hour_local": hour_local,
        "expected_peak_hour_local": expected_peak_hour,
        "hours_to_peak_window": hours_to_peak,
        "cool_gate": bool(cool_gate),
        "observed_rebound_gate": bool(obs_gate),
        "near_model_gate": bool(near_model_gate),
        "observed_breakthrough_gate": bool(observed_breakthrough_gate),
        "observed_breakthrough_buffer_c": observed_breakthrough_buffer,
        "warming_rate_gate": bool(rate_gate),
        "warming_rate_gate_relaxed_by_observed_breakthrough": bool(observed_breakthrough_gate and not rate_gate),
        "after_peak_gate": bool(after_peak_gate),
        "after_peak_gate_relaxed_by_observed_breakthrough": bool(observed_breakthrough_gate and after_peak_gate),
        "missing_fields": missing,
        "evidence_scale": evidence_scale,
        "posterior_lift_pp_cap": max(0.0, _env_float("PM_BAYES_POST_COOL_REBOUND_INTRADAY_LIKELIHOOD_PP_CAP", 0.08)),
        "provenance": {
            "kind": "external_archive/history_only" if any(str(row.get("source") or "").startswith("open_meteo") for row in prior_days) else "native_live_feature",
            "recent_daily_tmax_source": "event_daily_temps/event_runs plus station_observations fallback visible before as_of",
            "weather_features_source": str(features.get("source") or features.get("auxiliary_source") or ""),
            "guards": [
                "intraday_likelihood_after_regime_prior",
                "requires_recent_cool_trough",
                "requires_intraday_observed_rebound",
                "requires_multi_window_warming_rate",
                "blocked_after_peak_window",
                "hard_sources_override_later_in_bayes",
            ],
        },
    }
    out = dict(intraday_features)
    out["source"] = "post_cool_rebound_risk"
    out["role"] = "split_regime_prior_intraday_likelihood"
    out["regime_prior"] = prior_features
    out["intraday_likelihood"] = intraday_features
    return out


def load_post_cool_rebound_risk_context(
    *,
    db_path: Any,
    target_event_date: str,
    as_of: str,
    city: Optional[str],
    station_id: Optional[str],
    market_var: str,
    market_unit: str,
    weather_features: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    features = dict(weather_features or {})
    if market_unit and "market_unit" not in features:
        features["market_unit"] = market_unit
    if market_var and "market_var" not in features:
        features["market_var"] = market_var
    recent: List[Dict[str, Any]] = []
    path = Path(str(db_path or ""))
    if path.exists():
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
            conn.row_factory = sqlite3.Row
            recent = _load_recent_settled_tmax(
                conn,
                city=city,
                station_id=station_id,
                market_var=market_var,
                market_unit=market_unit,
                target_event_date=target_event_date,
                as_of=as_of,
            )
            if _observed_path_temp_from_features(features)[0] is None:
                fallback = _station_observation_fallback(
                    conn,
                    target_event_date=target_event_date,
                    station_id=station_id,
                    market_var=market_var,
                    as_of=as_of,
                )
                for key, value in fallback.items():
                    features.setdefault(key, value)
        except Exception:
            recent = []
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    risk_features = build_post_cool_rebound_risk_features(
        target_event_date=target_event_date,
        weather_features=features,
        recent_daily_tmax=recent,
        as_of=as_of,
    )
    return {
        "enabled": bool(risk_features.get("enabled")),
        "status": str(risk_features.get("status") or ""),
        "state": str(risk_features.get("state") or "off"),
        "features": risk_features,
        "regime_prior": (
            dict(risk_features.get("regime_prior"))
            if isinstance(risk_features.get("regime_prior"), Mapping)
            else {}
        ),
        "intraday_likelihood": (
            dict(risk_features.get("intraday_likelihood"))
            if isinstance(risk_features.get("intraday_likelihood"), Mapping)
            else {}
        ),
        "recent_daily_tmax": list(recent[:4]),
        "updated_at": as_of or _now_iso(),
    }
