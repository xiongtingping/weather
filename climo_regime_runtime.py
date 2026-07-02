from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


EPS = 1e-12
DEFAULT_OPEN_METEO_REFERENCE_PATH = (
    Path(__file__).resolve().parent / "data" / "climo" / "chengdu_zuuu_open_meteo_climo_2016_2025.json"
)


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    nums = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not nums:
        return None
    if len(nums) == 1:
        return float(nums[0])
    rank = max(0.0, min(1.0, float(pct))) * (len(nums) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return float(nums[low])
    weight = rank - low
    return float(nums[low] * (1.0 - weight) + nums[high] * weight)


def _mean(values: Sequence[float]) -> Optional[float]:
    nums = [float(v) for v in values if math.isfinite(float(v))]
    return sum(nums) / len(nums) if nums else None


def _season_bucket(day: datetime.date) -> Tuple[str, int]:
    if day.day <= 10:
        return "early_month", 1
    if day.day <= 20:
        return "mid_month", 2
    return "late_month", 3


def _select_tmax_sample(
    rows: Sequence[Mapping[str, Any]],
    *,
    target_day: datetime.date,
    min_samples: int,
) -> Tuple[List[float], str]:
    target_decade = _season_bucket(target_day)[1]

    def values_for(kind: str) -> List[float]:
        selected: List[float] = []
        for row in rows:
            row_day = _parse_date(row.get("event_date"))
            value = _to_float(row.get("actual"))
            if row_day is None or value is None:
                continue
            if kind == "same_month_decade":
                ok = row_day.month == target_day.month and _season_bucket(row_day)[1] == target_decade
            elif kind == "same_month":
                ok = row_day.month == target_day.month
            elif kind == "hot_season":
                ok = row_day.month in {5, 6, 7, 8, 9}
            else:
                ok = True
            if ok:
                selected.append(float(value))
        return selected

    samples = {
        "same_month_decade": values_for("same_month_decade"),
        "same_month": values_for("same_month"),
        "hot_season": values_for("hot_season"),
        "all_visible_history": values_for("all_visible_history"),
    }
    min_n = max(1, int(min_samples))
    decade_vals = samples["same_month_decade"]
    month_vals = samples["same_month"]
    if len(decade_vals) >= min_n and len(month_vals) >= max(min_n, 8):
        decade_p50 = _percentile(decade_vals, 0.50)
        decade_p90 = _percentile(decade_vals, 0.90)
        month_p50 = _percentile(month_vals, 0.50)
        decade_spread = (
            float(decade_p90) - float(decade_p50)
            if decade_p50 is not None and decade_p90 is not None
            else None
        )
        p50_gap = (
            abs(float(decade_p50) - float(month_p50))
            if decade_p50 is not None and month_p50 is not None
            else None
        )
        if len(decade_vals) < 15 and (
            (p50_gap is not None and p50_gap >= 1.5)
            or (decade_spread is not None and decade_spread >= 3.5)
        ):
            return month_vals, "same_month_robust_shrink_from_decade"

    for basis in ("same_month_decade", "same_month", "hot_season", "all_visible_history"):
        vals = samples[basis]
        if len(vals) >= max(1, int(min_samples)):
            return vals, basis
    return samples["all_visible_history"], "all_visible_history_low_sample"


def _extract_weather_features_from_payload(payload_raw: Any) -> Dict[str, Any]:
    try:
        payload = json.loads(str(payload_raw or "{}"))
    except Exception:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    value = payload.get("weather_likelihood_features")
    return dict(value) if isinstance(value, Mapping) else {}


def _first_feature_float(features: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        val = _to_float(features.get(key))
        if val is not None:
            return float(val)
    return None


def _resolve_reference_path(reference_path: Any = None) -> Optional[Path]:
    explicit = str(reference_path or "").strip()
    if explicit:
        return Path(explicit)
    env_path = str(os.environ.get("PM_CLIMO_REGIME_REFERENCE_PATH") or "").strip()
    if env_path:
        return Path(env_path)
    return DEFAULT_OPEN_METEO_REFERENCE_PATH if DEFAULT_OPEN_METEO_REFERENCE_PATH.exists() else None


def _load_reference_climo_rows(
    reference_path: Any,
    *,
    target_event_date: str,
) -> Tuple[List[Dict[str, Any]], List[float], Dict[str, Any]]:
    path = _resolve_reference_path(reference_path)
    if path is None:
        return [], [], {"available": False, "status": "reference_path_not_configured"}
    target_day = _parse_date(target_event_date)
    if target_day is None:
        return [], [], {"available": False, "status": "reference_invalid_target_event_date", "path": str(path)}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return [], [], {"available": False, "status": "reference_load_error", "path": str(path), "error": str(exc)[:160]}
    daily = payload.get("daily") if isinstance(payload, Mapping) else {}
    if not isinstance(daily, Mapping):
        return [], [], {"available": False, "status": "reference_daily_missing", "path": str(path)}
    times = daily.get("time") if isinstance(daily.get("time"), list) else []
    tmax_values = daily.get("temperature_2m_max") if isinstance(daily.get("temperature_2m_max"), list) else []
    dew_values = daily.get("dew_point_2m_mean") if isinstance(daily.get("dew_point_2m_mean"), list) else []
    rows: List[Dict[str, Any]] = []
    dewpoints: List[float] = []
    years: List[int] = []
    for idx, day_text in enumerate(times):
        row_day = _parse_date(day_text)
        if row_day is None:
            continue
        if row_day >= target_day or row_day.year >= target_day.year:
            continue
        tmax = _to_float(tmax_values[idx] if idx < len(tmax_values) else None)
        if tmax is not None:
            rows.append(
                {
                    "event_date": row_day.isoformat(),
                    "actual": float(tmax),
                    "created_at": "",
                    "resolution_source": "open_meteo_archive_reference_prior_years",
                }
            )
            years.append(row_day.year)
        dew = _to_float(dew_values[idx] if idx < len(dew_values) else None)
        if dew is not None and row_day.month in {5, 6, 7, 8, 9}:
            dewpoints.append(float(dew))
    source = str(payload.get("source") or "open_meteo_archive_reference_prior_years") if isinstance(payload, Mapping) else "open_meteo_archive_reference_prior_years"
    meta = {
        "available": bool(rows),
        "status": "ok" if rows else "reference_no_visible_prior_year_rows",
        "path": str(path),
        "source": source,
        "sample_count": len(rows),
        "dewpoint_sample_count": len(dewpoints),
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
        "created_at_guard": "reference_uses_only_years_before_target_event_year",
    }
    if isinstance(payload, Mapping):
        meta["provider"] = payload.get("provider") or payload.get("source")
        meta["latitude"] = payload.get("latitude")
        meta["longitude"] = payload.get("longitude")
        meta["timezone"] = payload.get("timezone")
    return rows, dewpoints, meta


def _monsoon_humidity_regime(dewpoint_c: Optional[float], anomaly_c: Optional[float]) -> str:
    if dewpoint_c is None and anomaly_c is None:
        return "unknown"
    if (dewpoint_c is not None and dewpoint_c >= 22.0) or (anomaly_c is not None and anomaly_c >= 1.5):
        return "humid_monsoon"
    if (dewpoint_c is not None and dewpoint_c <= 18.0) or (anomaly_c is not None and anomaly_c <= -1.5):
        return "dry_or_ventilated"
    return "seasonal_normal"


def _subtropical_high_regime(
    features: Mapping[str, Any],
    *,
    p90_c: Optional[float],
) -> str:
    model_peak = _first_feature_float(features, "forecast_peak_temp_c", "forecast_peak_temp", "peak_estimator_mu_c")
    low_cloud = _first_feature_float(features, "cloud_cover_low_pct", "metar_effective_cloud_cover_pct")
    total_cloud = _first_feature_float(features, "cloud_cover_pct", "open_meteo_cloud_cover_pct")
    precip = _first_feature_float(
        features,
        "radar_precip_rate_mm_per_h",
        "qpe_precip_rate_mm_per_h",
        "observed_precip_rate_mm_per_h",
        "precipitation_rate_mm_per_h",
        "rain_rate_mm_per_h",
    )
    radiation = _first_feature_float(features, "solar_radiation_observed", "shortwave_radiation_observed", "solar_radiation")
    cloud_ref = low_cloud if low_cloud is not None else total_cloud
    if precip is not None and precip > 0.05:
        return "monsoon_cloud_precip_proxy"
    if cloud_ref is not None and cloud_ref >= 75.0:
        return "cloud_limited_proxy"
    if p90_c is not None and model_peak is not None and model_peak >= p90_c:
        if cloud_ref is None or cloud_ref <= 45.0:
            return "warm_ridge_proxy"
    if radiation is not None and radiation >= 600.0 and (cloud_ref is None or cloud_ref <= 45.0):
        return "warm_ridge_proxy"
    return "neutral_proxy"


def _basin_ventilation(features: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    wind = _first_feature_float(features, "wind_speed_mps", "wind_speed_10m_mps")
    if wind is None:
        return None, "unknown"
    if wind <= 1.8:
        return 1.0, "weak_ventilation_static"
    if wind <= 4.0:
        return 0.55, "moderate_ventilation"
    return 0.2, "well_ventilated"


def _first_feature_bool(features: Mapping[str, Any], *keys: str) -> Optional[bool]:
    for key in keys:
        if key not in features:
            continue
        value = features.get(key)
        if isinstance(value, bool):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on", "active"}:
            return True
        if text in {"0", "false", "no", "off", "inactive"}:
            return False
    return None


def _mechanism_item(
    *,
    key: str,
    label: str,
    role: str,
    score: float,
    status: str,
    evidence: Mapping[str, Any],
    basis: str,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "role": role,
        "score": max(0.0, min(1.0, float(score))),
        "status": status,
        "evidence": dict(evidence),
        "basis": basis,
    }


def basin_heat_mechanism_model(features: Mapping[str, Any], *, p90_c: Optional[float] = None) -> Dict[str, Any]:
    """Composite Sichuan Basin heating regime model.

    This is a background/regime model.  It summarizes common mechanisms reported
    in Sichuan Basin heat studies and must remain weaker than D0 trajectory,
    METAR/current observations, and final settlement locks.
    """
    p90 = _to_float(p90_c)
    model_peak = _first_feature_float(
        features,
        "forecast_peak_temp_c",
        "forecast_peak_temp",
        "peak_estimator_mu_c",
        "model_peak_c",
        "model_peak_temp_c",
    )
    soft_peak = _first_feature_float(
        features,
        "taf_forecast_peak_c",
        "wu_forecast_peak_c",
        "weather_com_forecast_peak_c",
        "soft_forecast_peak_c",
        "forecast_high_c",
    )
    t850 = _first_feature_float(features, "temperature_850hpa_c", "t850_c", "t850_temperature_c")
    t850_anom = _first_feature_float(features, "temperature_850hpa_anomaly_c", "t850_anomaly_c", "t850_temp_anomaly_c")
    low_cloud = _first_feature_float(features, "cloud_cover_low_pct", "metar_effective_cloud_cover_pct")
    total_cloud = _first_feature_float(features, "cloud_cover_pct", "open_meteo_cloud_cover_pct")
    cloud_ref = low_cloud if low_cloud is not None else total_cloud
    radiation = _first_feature_float(
        features,
        "solar_radiation_observed",
        "shortwave_radiation_observed",
        "direct_radiation_observed",
        "solar_radiation",
        "shortwave_radiation",
    )
    pblh = _first_feature_float(
        features,
        "boundary_layer_height_m",
        "planetary_boundary_layer_height_m",
        "pbl_height_m",
        "pblh_m",
    )
    warming_rate = _first_feature_float(
        features,
        "temperature_momentum_posterior_effective_c_per_hour",
        "temperature_momentum_effective_c_per_hour",
        "temperature_trajectory_stable_rate_c_per_hour",
        "temperature_momentum_1h_c_per_hour",
        "warming_rate_c_per_hour",
    )
    wind = _first_feature_float(features, "wind_speed_mps", "wind_speed_10m_mps")
    dew = _first_feature_float(features, "dewpoint_c", "dew_point_c", "dewpoint_2m_c")
    dew_anomaly = _first_feature_float(features, "dewpoint_anomaly_c")
    rh = _first_feature_float(features, "relative_humidity_pct", "relative_humidity_2m_pct", "humidity_pct")
    precip = _first_feature_float(
        features,
        "radar_precip_rate_mm_per_h",
        "qpe_precip_rate_mm_per_h",
        "observed_precip_rate_mm_per_h",
        "precipitation_rate_mm_per_h",
        "rain_rate_mm_per_h",
        "precip_nowcast_1_3h_max_rate_mm_per_h",
        "forecast_precip_nowcast_1_3h_max_rate_mm_per_h",
    )
    southwest_vortex = _first_feature_bool(
        features,
        "southwest_vortex_active",
        "swv_active",
        "southwest_vortex_precip_risk",
        "low_vortex_precip_risk",
    )
    post_cool_state = str(
        features.get("post_cool_rebound_intraday_likelihood_state")
        or features.get("post_cool_rebound_regime_prior_state")
        or ""
    ).strip().lower()

    mechanisms: List[Dict[str, Any]] = []

    warm_ridge_score = 0.0
    warm_ridge_status = "missing_synoptic_proxy"
    if p90 is not None and model_peak is not None and model_peak >= p90:
        warm_ridge_score = max(warm_ridge_score, 0.45)
        warm_ridge_status = "model_peak_above_climo_p90"
    if p90 is not None and soft_peak is not None and soft_peak >= p90:
        warm_ridge_score = max(warm_ridge_score, 0.35)
        warm_ridge_status = "soft_forecast_peak_above_climo_p90"
    if t850_anom is not None and t850_anom >= 1.5:
        warm_ridge_score = max(warm_ridge_score, 0.55)
        warm_ridge_status = "low_level_warm_anomaly"
    if warm_ridge_score > 0:
        mechanisms.append(_mechanism_item(
            key="subtropical_high_warm_ridge",
            label="副高/暖脊控制",
            role="support",
            score=warm_ridge_score,
            status=warm_ridge_status,
            evidence={"model_peak_c": model_peak, "soft_peak_c": soft_peak, "climo_p90_c": p90, "t850_anomaly_c": t850_anom},
            basis="500hPa高度场偏高/副高偏西偏北与850hPa暖区支持静稳增温",
        ))

    if t850 is not None or t850_anom is not None:
        score = 0.0
        if t850_anom is not None and t850_anom >= 2.0:
            score = 0.45
        elif t850_anom is not None and t850_anom >= 1.0:
            score = 0.30
        elif t850 is not None and t850 >= 23.0:
            score = 0.25
        if score > 0:
            mechanisms.append(_mechanism_item(
                key="low_level_warm_airmass",
                label="低层暖空气团",
                role="support",
                score=score,
                status="warm_low_level_airmass",
                evidence={"t850_c": t850, "t850_anomaly_c": t850_anom},
                basis="850hPa暖区/暖平流提高当日可达温度上限",
            ))

    clear_sky = cloud_ref is None or cloud_ref <= 45.0
    if radiation is not None or cloud_ref is not None:
        score = 0.0
        if radiation is not None and radiation >= 700.0 and clear_sky:
            score = 0.50
        elif radiation is not None and radiation >= 550.0 and clear_sky:
            score = 0.35
        elif cloud_ref is not None and cloud_ref <= 35.0:
            score = 0.25
        if score > 0:
            mechanisms.append(_mechanism_item(
                key="clear_sky_shortwave_radiation",
                label="晴空短波辐射",
                role="support",
                score=score,
                status="shortwave_heating_window",
                evidence={"shortwave_w_m2": radiation, "cloud_ref_pct": cloud_ref},
                basis="短波辐射是成都平原高温复合事件的重要驱动因子之一",
            ))

    if pblh is not None or warming_rate is not None:
        score = 0.0
        if pblh is not None and pblh >= 1200.0:
            score = max(score, 0.40)
        elif pblh is not None and pblh >= 800.0:
            score = max(score, 0.25)
        if warming_rate is not None and warming_rate >= 0.50:
            score = max(score, 0.45)
        elif warming_rate is not None and warming_rate >= 0.25:
            score = max(score, 0.30)
        if score > 0:
            mechanisms.append(_mechanism_item(
                key="boundary_layer_growth_mixing",
                label="边界层发展/混合增温",
                role="support",
                score=score,
                status="pbl_or_momentum_supports_heating",
                evidence={"pblh_m": pblh, "warming_rate_c_per_hour": warming_rate},
                basis="边界层高度和升温轨迹共同描述盘中增温兑现程度",
            ))

    if wind is not None:
        if wind <= 1.8:
            mechanisms.append(_mechanism_item(
                key="weak_wind_static_heating",
                label="弱风静稳蓄热",
                role="support",
                score=0.28,
                status="weak_ventilation_static",
                evidence={"wind_speed_mps": wind},
                basis="近地层弱风/静稳有利于热量累积，但不是硬实况",
            ))
        elif wind >= 4.5:
            mechanisms.append(_mechanism_item(
                key="well_ventilated_mixing",
                label="通风/混合作用",
                role="suppress",
                score=0.25,
                status="well_ventilated",
                evidence={"wind_speed_mps": wind},
                basis="较强通风削弱局地静稳蓄热信号",
            ))

    dry_air = (dew is not None and dew <= 18.0) or (dew_anomaly is not None and dew_anomaly <= -1.5) or (rh is not None and rh <= 55.0)
    humid_air = (dew is not None and dew >= 22.0) or (dew_anomaly is not None and dew_anomaly >= 1.5) or (rh is not None and rh >= 82.0)
    if dry_air:
        mechanisms.append(_mechanism_item(
            key="dry_air_diurnal_heating",
            label="干空气日间增温",
            role="support",
            score=0.18,
            status="dry_or_ventilated_airmass",
            evidence={"dewpoint_c": dew, "dewpoint_anomaly_c": dew_anomaly, "relative_humidity_pct": rh},
            basis="低湿/少云背景下白天感热增温更容易兑现",
        ))
    elif humid_air:
        mechanisms.append(_mechanism_item(
            key="humid_monsoon_damping",
            label="湿润季风抑制",
            role="suppress",
            score=0.22,
            status="humid_monsoon",
            evidence={"dewpoint_c": dew, "dewpoint_anomaly_c": dew_anomaly, "relative_humidity_pct": rh},
            basis="高湿常与云/降水/潜热过程相伴，降低白天干热突破确定性",
        ))

    if post_cool_state in {"prior", "watch", "lift", "active"}:
        mechanisms.append(_mechanism_item(
            key="post_cool_rebound_setup",
            label="冷池消退后反弹",
            role="support",
            score=0.35 if post_cool_state in {"lift", "active"} else 0.18,
            status=post_cool_state,
            evidence={"post_cool_rebound_state": post_cool_state},
            basis="冷池/逆温消退后升温加快是盆地边界层常见过程之一",
        ))

    if precip is not None and precip > 0.05:
        mechanisms.append(_mechanism_item(
            key="precipitation_evaporative_cloud_damping",
            label="降水/蒸发冷却压制",
            role="suppress",
            score=0.62 if precip >= 0.5 else 0.42,
            status="precipitation_or_nowcast",
            evidence={"precip_rate_mm_per_h": precip},
            basis="降水、蒸发冷却和云系会压低白天最高温兑现概率",
        ))
    elif cloud_ref is not None and cloud_ref >= 75.0:
        mechanisms.append(_mechanism_item(
            key="thick_cloud_shortwave_damping",
            label="厚云短波受限",
            role="suppress",
            score=0.38,
            status="cloud_limited",
            evidence={"cloud_ref_pct": cloud_ref},
            basis="厚云减少地表短波辐射，是高温突破的常见压制项",
        ))

    if southwest_vortex is True:
        mechanisms.append(_mechanism_item(
            key="southwest_vortex_precip_regime",
            label="西南涡/降水型环流",
            role="suppress",
            score=0.45,
            status="southwest_vortex_active",
            evidence={"southwest_vortex_active": southwest_vortex},
            basis="西南涡是四川盆地常见降水系统，会改变温湿风和边界层结构",
        ))

    support_score = min(1.5, sum(float(item["score"]) for item in mechanisms if item.get("role") == "support"))
    suppress_score = min(1.2, sum(float(item["score"]) for item in mechanisms if item.get("role") == "suppress"))
    net_score = support_score - 0.85 * suppress_score
    heat_uplift_index = max(0.0, min(1.0, 0.35 + 0.45 * net_score))
    if support_score <= 0.0 and suppress_score <= 0.0:
        state = "off"
        status = "mechanism_inputs_missing"
    elif suppress_score >= support_score + 0.25:
        state = "off"
        status = "suppressed_by_cloud_precip_or_ventilation"
    elif support_score >= 0.75 and support_score >= suppress_score + 0.20:
        state = "active"
        status = "multi_mechanism_heating_support"
    else:
        state = "watch"
        status = "partial_mechanism_support"
    temperature_adjustment_c = max(-0.8, min(0.8, 0.55 * support_score - 0.55 * suppress_score))
    active_support = [item for item in mechanisms if item.get("role") == "support" and float(item.get("score") or 0.0) > 0.0]
    active_suppression = [item for item in mechanisms if item.get("role") == "suppress" and float(item.get("score") or 0.0) > 0.0]
    return {
        "version": "sichuan_basin_heat_mechanism_v1",
        "role": "background_regime_model_not_hard_likelihood",
        "state": state,
        "status": status,
        "heat_uplift_index": heat_uplift_index,
        "support_score": support_score,
        "suppression_score": suppress_score,
        "net_score": net_score,
        "temperature_adjustment_c": temperature_adjustment_c,
        "mechanisms": mechanisms,
        "active_support_keys": [str(item.get("key")) for item in active_support],
        "active_suppression_keys": [str(item.get("key")) for item in active_suppression],
        "model_boundaries": {
            "not_observation": True,
            "not_final_settlement": True,
            "weak_prior_or_explanation_only": True,
            "temperature_trajectory_and_hard_sources_override": True,
        },
    }


def climo_primary_regime(features: Mapping[str, Any]) -> str:
    mechanism_model = features.get("basin_heat_mechanism_model")
    if isinstance(mechanism_model, Mapping):
        suppressions = set(str(v) for v in mechanism_model.get("active_suppression_keys") or [])
        supports = set(str(v) for v in mechanism_model.get("active_support_keys") or [])
        if "precipitation_evaporative_cloud_damping" in suppressions or "thick_cloud_shortwave_damping" in suppressions:
            return "cloud_limited_proxy"
        if str(mechanism_model.get("state") or "") == "active":
            if "subtropical_high_warm_ridge" in supports:
                return "warm_ridge_proxy"
            if "weak_wind_static_heating" in supports:
                return "weak_ventilation_static"
            if "boundary_layer_growth_mixing" in supports:
                return "boundary_layer_heating_proxy"
    ridge = str(features.get("subtropical_high_regime") or "")
    monsoon = str(features.get("monsoon_humidity_regime") or "")
    ventilation = str(features.get("basin_ventilation_regime") or "")
    if ridge == "warm_ridge_proxy":
        return "warm_ridge_proxy"
    if ridge in {"cloud_limited_proxy", "monsoon_cloud_precip_proxy"}:
        return "cloud_limited_proxy"
    if monsoon == "humid_monsoon":
        return "humid_monsoon"
    if ventilation == "weak_ventilation_static":
        return "weak_ventilation_static"
    return "neutral_proxy"


def derive_basin_regime_label(features: Mapping[str, Any]) -> str:
    """Stable stratification label for model-weight calibration.

    The full climo/basin feature payload is intentionally richer than this
    label.  This label stays coarse so each regime can accumulate enough live
    D1/D0 samples before it is eligible for model-weight promotion.
    """
    if not isinstance(features, Mapping):
        return "unknown"
    primary = str(
        features.get("primary_regime_label")
        or features.get("subtropical_high_regime")
        or ""
    ).strip()
    ventilation = str(features.get("basin_ventilation_regime") or "").strip()
    heat_state = str(features.get("basin_heat_mechanism_state") or "").strip()
    heat_status = str(features.get("basin_heat_mechanism_status") or "").strip()
    humidity = str(features.get("monsoon_humidity_regime") or "").strip()
    post_cool_state = str(
        features.get("post_cool_rebound_intraday_likelihood_state")
        or features.get("post_cool_rebound_regime_prior_state")
        or ""
    ).strip()
    if post_cool_state in {"watch", "prior", "lift", "active"}:
        return "post_cool_rebound"
    if primary in {"monsoon_cloud_precip_proxy", "cloud_limited_proxy"}:
        return "cloud_limited_or_rain_cooled"
    if ventilation == "well_ventilated":
        return "well_ventilated"
    if heat_state in {"active", "lift"} or heat_status in {"active", "supporting"}:
        if ventilation == "weak_ventilation_static":
            return "weak_ventilation_heat_buildup"
        if ventilation == "moderate_ventilation":
            return "moderate_ventilation_heat_buildup"
        return "basin_heat_buildup"
    if ventilation == "weak_ventilation_static":
        return "weak_ventilation_background"
    if ventilation == "moderate_ventilation":
        return "moderate_ventilation"
    if humidity == "humid_monsoon":
        return "humid_monsoon_background"
    if primary:
        return primary
    return "unknown"


def climo_regime_confidence(features: Mapping[str, Any]) -> float:
    score = 0.25
    sample_count = int(_to_float(features.get("climo_tmax_sample_count")) or 0)
    if sample_count >= 30:
        score += 0.25
    elif sample_count >= 10:
        score += 0.15
    elif sample_count >= 3:
        score += 0.05
    if _to_float(features.get("dewpoint_c")) is not None:
        score += 0.10
    if _to_float(features.get("basin_ventilation_index")) is not None:
        score += 0.10
    if str(features.get("subtropical_high_regime") or "") not in {"", "unknown", "neutral_proxy"}:
        score += 0.10
    mechanism_model = features.get("basin_heat_mechanism_model")
    if isinstance(mechanism_model, Mapping) and mechanism_model.get("state") in {"watch", "active"}:
        score += 0.10
    if bool(features.get("climo_reference_used")):
        score += 0.10
    return max(0.0, min(0.85, float(score)))


def climo_high_temp_potential_gate(features: Mapping[str, Any]) -> Dict[str, Any]:
    p90 = _to_float(features.get("climo_tmax_p90_c"))
    p95 = _to_float(features.get("climo_tmax_p95_c"))
    target_high = p90 if p90 is not None else p95
    model_peak = _first_feature_float(
        features,
        "forecast_peak_temp_c",
        "forecast_peak_temp",
        "peak_estimator_mu_c",
        "model_peak_c",
        "model_peak_temp_c",
    )
    soft_forecast = _first_feature_float(
        features,
        "taf_forecast_peak_c",
        "wu_forecast_peak_c",
        "settlement_source_forecast_peak_c",
        "soft_forecast_peak_c",
        "forecast_high_c",
    )
    current_temp = _first_feature_float(
        features,
        "current_temp_c",
        "observed_current_temp_c",
        "zuuu_metar_raw_awc_latest_temp_c",
        "temperature_c",
    )
    warming_rate = _first_feature_float(
        features,
        "temperature_momentum_1h_c_per_hour",
        "warming_rate_1h_c_per_hour",
        "warming_rate_c_per_hour",
        "temp_rate_c_per_hour",
    )
    low_cloud = _first_feature_float(features, "cloud_cover_low_pct", "metar_effective_cloud_cover_pct")
    total_cloud = _first_feature_float(features, "cloud_cover_pct", "open_meteo_cloud_cover_pct")
    cloud_ref = low_cloud if low_cloud is not None else total_cloud
    precip = _first_feature_float(
        features,
        "radar_precip_rate_mm_per_h",
        "qpe_precip_rate_mm_per_h",
        "observed_precip_rate_mm_per_h",
        "precipitation_rate_mm_per_h",
        "rain_rate_mm_per_h",
    )
    primary = climo_primary_regime(features)
    mechanism_model = features.get("basin_heat_mechanism_model")
    mechanism_state = str(mechanism_model.get("state") or "") if isinstance(mechanism_model, Mapping) else ""
    mechanism_net = _to_float(mechanism_model.get("net_score")) if isinstance(mechanism_model, Mapping) else None
    mechanism_supports = mechanism_state in {"watch", "active"} and (mechanism_net is None or mechanism_net > 0.0)
    climo_supports = primary in {"warm_ridge_proxy", "weak_ventilation_static", "boundary_layer_heating_proxy"} or mechanism_supports
    model_supports = target_high is not None and model_peak is not None and model_peak >= float(target_high)
    soft_supports = target_high is not None and soft_forecast is not None and soft_forecast >= float(target_high)
    no_precip_cloud = (precip is None or precip <= 0.05) and (cloud_ref is None or cloud_ref < 75.0)
    momentum_realized = warming_rate is not None and warming_rate > 0.0
    hard_block = False
    hard_block_reason = ""
    if current_temp is not None and soft_forecast is not None and warming_rate is not None:
        if current_temp < soft_forecast - 2.0 and warming_rate <= 0.0:
            hard_block = True
            hard_block_reason = "current_temp_below_soft_forecast_by_gt_2c_and_non_positive_momentum"
            momentum_realized = False
    allow = bool(climo_supports and model_supports and soft_supports and momentum_realized and no_precip_cloud and not hard_block)
    missing = []
    if target_high is None:
        missing.append("climo_p90")
    if model_peak is None:
        missing.append("model_peak")
    if soft_forecast is None:
        missing.append("taf_or_wu_soft_forecast")
    if warming_rate is None:
        missing.append("temperature_momentum")
    return {
        "allow_high_temp_uplift": allow,
        "climo_supports_heating_potential": bool(climo_supports),
        "model_peak_supports_high_temp": bool(model_supports),
        "soft_forecast_supports_high_temp": bool(soft_supports),
        "temperature_momentum_realized": bool(momentum_realized),
        "no_strong_precip_or_thick_cloud": bool(no_precip_cloud),
        "hard_blocked": bool(hard_block),
        "hard_block_reason": hard_block_reason,
        "target_high_threshold_c": target_high,
        "model_peak_c": model_peak,
        "soft_forecast_peak_c": soft_forecast,
        "current_temp_c": current_temp,
        "warming_rate_c_per_hour": warming_rate,
        "cloud_ref_pct": cloud_ref,
        "precip_rate_mm_per_h": precip,
        "missing_inputs": missing,
        "charter": "supports_heating_potential_not_realized_heating",
        "temperature_trajectory_priority": True,
    }


def climo_regime_pp_cap(
    *,
    days_to_event: Optional[float],
    features: Mapping[str, Any],
) -> Dict[str, Any]:
    days = _to_float(days_to_event)
    hours_to_peak = _first_feature_float(features, "hours_to_peak_window", "hours_to_peak")
    if hours_to_peak is not None:
        if hours_to_peak <= 2.0:
            return {
                "max_abs_pp": 0.0,
                "stage": "d0_within_2h_explanation_only",
                "direct_weight_allowed": False,
                "days_to_event": days,
                "hours_to_peak_window": hours_to_peak,
            }
        if hours_to_peak <= 6.0:
            return {
                "max_abs_pp": 0.005,
                "stage": "d0_within_6h_cap_0_5pp",
                "direct_weight_allowed": True,
                "days_to_event": days,
                "hours_to_peak_window": hours_to_peak,
            }
    if days is not None:
        if days <= 0.25:
            return {
                "max_abs_pp": 0.005,
                "stage": "d0_near_peak_cap_0_5pp",
                "direct_weight_allowed": True,
                "days_to_event": days,
                "hours_to_peak_window": hours_to_peak,
            }
        if days <= 1.0:
            return {
                "max_abs_pp": 0.01,
                "stage": "d0_cap_1pp",
                "direct_weight_allowed": True,
                "days_to_event": days,
                "hours_to_peak_window": hours_to_peak,
            }
        if days <= 2.0:
            return {
                "max_abs_pp": 0.01,
                "stage": "d1_cap_1pp",
                "direct_weight_allowed": True,
                "days_to_event": days,
                "hours_to_peak_window": hours_to_peak,
            }
    return {
        "max_abs_pp": 0.02,
        "stage": "d3_d2_cap_2pp",
        "direct_weight_allowed": True,
        "days_to_event": days,
        "hours_to_peak_window": hours_to_peak,
    }


def cap_climo_evidence_by_pp_delta(
    *,
    base_scores: Sequence[float],
    climo_probs: Sequence[float],
    raw_evidence: float,
    max_abs_pp: float,
) -> Dict[str, Any]:
    base = [max(0.0, float(v)) if math.isfinite(float(v)) else 0.0 for v in base_scores]
    probs = [max(0.0, float(v)) if math.isfinite(float(v)) else 0.0 for v in climo_probs]
    if not base or not probs or len(base) != len(probs) or raw_evidence <= 0.0:
        return {
            "evidence": 0.0,
            "raw_evidence": max(0.0, float(raw_evidence)),
            "max_abs_delta": 0.0,
            "cap_hit": False,
            "max_abs_pp": max(0.0, float(max_abs_pp)),
        }
    cap = max(0.0, float(max_abs_pp))
    if cap <= 0.0:
        return {
            "evidence": 0.0,
            "raw_evidence": max(0.0, float(raw_evidence)),
            "max_abs_delta": 0.0,
            "cap_hit": raw_evidence > 0.0,
            "max_abs_pp": cap,
        }
    base_total = sum(base)
    prob_total = sum(probs)
    if base_total <= EPS or prob_total <= EPS:
        return {
            "evidence": 0.0,
            "raw_evidence": max(0.0, float(raw_evidence)),
            "max_abs_delta": 0.0,
            "cap_hit": raw_evidence > 0.0,
            "max_abs_pp": cap,
        }
    base_norm = [v / base_total for v in base]
    climo_norm = [v / prob_total for v in probs]

    def max_delta(weight: float) -> float:
        total = base_total + max(0.0, float(weight))
        if total <= EPS:
            return 0.0
        return max(
            abs(((base[idx] + weight * climo_norm[idx]) / total) - base_norm[idx])
            for idx in range(len(base))
        )

    raw = max(0.0, float(raw_evidence))
    raw_delta = max_delta(raw)
    if raw_delta <= cap + 1e-12:
        return {
            "evidence": raw,
            "raw_evidence": raw,
            "max_abs_delta": raw_delta,
            "cap_hit": False,
            "max_abs_pp": cap,
        }
    low = 0.0
    high = raw
    for _ in range(36):
        mid = (low + high) / 2.0
        if max_delta(mid) <= cap:
            low = mid
        else:
            high = mid
    return {
        "evidence": low,
        "raw_evidence": raw,
        "max_abs_delta": max_delta(low),
        "raw_max_abs_delta": raw_delta,
        "cap_hit": True,
        "max_abs_pp": cap,
    }


def _label_unit(label: str) -> str:
    match = re.search(r"([A-Za-z°/%]+)\s*$", str(label or "").strip())
    return str(match.group(1) if match else "").upper().replace("°", "")


def bucket_bounds(label: str, rounding_rule: Optional[str]) -> Optional[Tuple[float, float]]:
    raw = str(label or "").strip().replace("–", "-")
    if not raw:
        return None
    num = r"(-?\d+(?:\.\d+)?)"
    unit = r"([A-Za-z°/%]+)?"
    whole = str(rounding_rule or "").strip().lower() == "whole_degrees" and _label_unit(raw) in {"C", "F"}
    rounded = str(rounding_rule or "").strip().lower() == "rounded_degrees" and _label_unit(raw) in {"C", "F"}
    match = re.match(rf"^{num}\s*-\s*{num}\s*{unit}$", raw)
    if not match:
        match = re.match(rf"^{num}\s+to\s+{num}\s*{unit}$", raw, flags=re.I)
    if match:
        low = min(float(match.group(1)), float(match.group(2)))
        high = max(float(match.group(1)), float(match.group(2)))
        if rounded:
            return low - 0.5, high + 0.5
        return low, high + 1.0 if whole else high
    match = re.match(rf"^{num}\s*{unit}\s+or\s+below$", raw, flags=re.I)
    if match:
        k = float(match.group(1))
        return float("-inf"), k + 0.5 if rounded else (k + 1.0 if whole else k)
    match = re.match(rf"^{num}\s*{unit}\s+or\s+(higher|above|more)$", raw, flags=re.I)
    if match:
        k = float(match.group(1))
        return (k - 0.5 if rounded else k), float("inf")
    match = re.match(rf"^{num}\s*{unit}$", raw)
    if match:
        k = float(match.group(1))
        return (k, k + 1.0) if whole else (k - 0.5, k + 0.5)
    return None


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    if math.isinf(x):
        return 0.0 if x < 0 else 1.0
    z = (float(x) - float(mu)) / max(EPS, float(sigma))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _normalize(values: Mapping[str, float], labels: Sequence[str]) -> Dict[str, float]:
    total = sum(max(0.0, float(values.get(str(label), 0.0) or 0.0)) for label in labels)
    if total <= EPS:
        denom = float(max(1, len(labels)))
        return {str(label): 1.0 / denom for label in labels}
    return {str(label): max(0.0, float(values.get(str(label), 0.0) or 0.0)) / total for label in labels}


def climo_regime_bucket_probabilities(
    labels: Sequence[str],
    features: Mapping[str, Any],
    *,
    rounding_rule: Optional[str],
) -> Dict[str, float]:
    p50 = _to_float(features.get("climo_tmax_p50_c"))
    mean = _to_float(features.get("climo_tmax_mean_c"))
    p95 = _to_float(features.get("climo_tmax_p95_c"))
    if p50 is None and mean is None:
        return {}
    mu = float(p50 if p50 is not None else mean)
    adjustment = 0.0
    monsoon = str(features.get("monsoon_humidity_regime") or "")
    if monsoon == "humid_monsoon":
        adjustment -= 0.20
    elif monsoon == "dry_or_ventilated":
        adjustment += 0.15
    ridge = str(features.get("subtropical_high_regime") or "")
    if ridge == "warm_ridge_proxy":
        adjustment += 0.35
    elif ridge in {"cloud_limited_proxy", "monsoon_cloud_precip_proxy"}:
        adjustment -= 0.35
    ventilation = str(features.get("basin_ventilation_regime") or "")
    if ventilation == "weak_ventilation_static":
        adjustment += 0.15
    elif ventilation == "well_ventilated":
        adjustment -= 0.15
    mechanism_model = features.get("basin_heat_mechanism_model")
    if isinstance(mechanism_model, Mapping):
        mechanism_adjustment = _to_float(mechanism_model.get("temperature_adjustment_c"))
        if mechanism_adjustment is not None:
            adjustment = float(mechanism_adjustment)
    gate = features.get("high_temp_potential_gate")
    if isinstance(gate, Mapping) and not bool(gate.get("allow_high_temp_uplift")):
        adjustment = min(0.0, adjustment)
    adjustment = max(-0.8, min(0.8, adjustment))
    mu += adjustment
    if p95 is not None and p50 is not None and p95 > p50:
        sigma = max(0.7, min(2.5, (float(p95) - float(p50)) / 1.645))
    else:
        sigma = 1.4
    raw: Dict[str, float] = {}
    for label in labels:
        bounds = bucket_bounds(str(label), rounding_rule)
        if bounds is None:
            raw[str(label)] = 0.0
            continue
        low, high = bounds
        raw[str(label)] = max(0.0, _normal_cdf(high, mu, sigma) - _normal_cdf(low, mu, sigma))
    return _normalize(raw, labels)


def climo_regime_evidence_scale(
    *,
    days_to_event: Optional[float],
    features: Mapping[str, Any],
) -> Dict[str, Any]:
    days = _to_float(days_to_event)
    if days is None:
        scale = 0.6
        stage = "unknown_lead"
    elif days > 2.0:
        scale = 1.0
        stage = "d3_d2"
    elif days > 1.0:
        scale = 0.85
        stage = "d1"
    elif days > 0.25:
        scale = 0.35
        stage = "d0_pre_peak"
    else:
        scale = 0.12
        stage = "d0_near_peak"
    hours_to_peak = _first_feature_float(features, "hours_to_peak_window", "hours_to_peak")
    if hours_to_peak is not None:
        if hours_to_peak <= 0.0:
            scale = 0.0
            stage = "peak_or_after_zero_weight"
        elif hours_to_peak <= 2.0:
            scale = 0.0
            stage = "d0_within_2h_explanation_only"
        elif hours_to_peak <= 6.0:
            scale = min(scale, 0.05)
            stage = "d0_within_6h_weak_cap"
    status = str(features.get("status") or "ok")
    sample_count = int(_to_float(features.get("climo_tmax_sample_count")) or 0)
    if status.startswith("insufficient") or sample_count <= 0:
        scale = 0.0
        stage = f"{stage}_insufficient_history"
    return {
        "scale": max(0.0, min(1.0, float(scale))),
        "stage": stage,
        "days_to_event": days,
        "hours_to_peak_window": hours_to_peak,
    }


def climo_regime_explanation(features: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "role": "P_climo_regime slow_variable_background_layer_weak_prior_regime_feature",
        "display_label": "气候/环流弱参考，不覆盖实况温度轨迹",
        "potential_label": "支持升温潜力 ≠ 已兑现升温",
        "trajectory_priority_label": "实况温度轨迹优先",
        "charter": {
            "slow_variable_background_layer": True,
            "weak_prior_shrinkage": True,
            "regime_label_source": True,
            "calibration_stratification_feature": True,
            "risk_explanation_gate_input": True,
            "not_strong_likelihood": True,
            "does_not_decide_target_bucket_alone": True,
            "temperature_trajectory_priority": True,
        },
        "boundaries": {
            "not_p_obs": True,
            "not_wu_metar_observed": True,
            "not_p_model": True,
            "not_p_physics": True,
            "d0_near_peak_downweighted": True,
            "d0_within_2h_direct_weight_zero": True,
        },
        "summary": {
            "climo_tmax_p50_c": features.get("climo_tmax_p50_c"),
            "climo_tmax_p90_c": features.get("climo_tmax_p90_c"),
            "primary_regime_label": features.get("primary_regime_label"),
            "regime_confidence": features.get("regime_confidence"),
            "monsoon_humidity_regime": features.get("monsoon_humidity_regime"),
            "subtropical_high_regime": features.get("subtropical_high_regime"),
            "basin_ventilation_regime": features.get("basin_ventilation_regime"),
            "high_temp_potential_gate": features.get("high_temp_potential_gate"),
            "basin_heat_mechanism_model": features.get("basin_heat_mechanism_model"),
            "basin_heat_mechanism_state": features.get("basin_heat_mechanism_state"),
            "basin_heat_mechanism_active_support_keys": features.get("basin_heat_mechanism_active_support_keys"),
            "basin_heat_mechanism_active_suppression_keys": features.get("basin_heat_mechanism_active_suppression_keys"),
        },
    }


def build_climo_regime_features(
    *,
    tmax_history: Sequence[Mapping[str, Any]],
    target_event_date: Any,
    weather_features: Optional[Mapping[str, Any]] = None,
    dewpoint_history_c: Optional[Sequence[float]] = None,
    updated_at: Optional[str] = None,
    min_samples: int = 3,
    history_source: str = "event_daily_temps_visible_history+event_runs_weather_features_proxy",
) -> Dict[str, Any]:
    target_day = _parse_date(target_event_date)
    features_in = dict(weather_features or {})
    if target_day is None:
        return {
            "status": "invalid_target_event_date",
            "climo_tmax_mean_c": None,
            "climo_tmax_p50_c": None,
            "climo_tmax_p90_c": None,
            "climo_tmax_p95_c": None,
            "dewpoint_anomaly_c": None,
            "monsoon_humidity_regime": "unknown",
            "subtropical_high_regime": "unknown",
            "basin_ventilation_index": None,
            "basin_ventilation_regime": "unknown",
            "basin_regime": "unknown",
            "basin_heat_mechanism_model": basin_heat_mechanism_model(features_in, p90_c=None),
            "basin_heat_mechanism_state": "off",
            "basin_heat_mechanism_active_support_keys": [],
            "basin_heat_mechanism_active_suppression_keys": [],
            "source": str(history_source or "event_daily_temps_visible_history"),
            "updated_at": updated_at or _now_iso(),
        }
    selected_tmax, basis = _select_tmax_sample(tmax_history, target_day=target_day, min_samples=min_samples)
    mean = _mean(selected_tmax)
    p50 = _percentile(selected_tmax, 0.50)
    p90 = _percentile(selected_tmax, 0.90)
    p95 = _percentile(selected_tmax, 0.95)

    dewpoint_now = _first_feature_float(
        features_in,
        "dewpoint_c",
        "dew_point_c",
        "dewpoint_2m_c",
        "zuuu_metar_raw_awc_latest_dewpoint_c",
    )
    dew_hist = [float(v) for v in (dewpoint_history_c or []) if _to_float(v) is not None]
    dew_baseline = _mean(dew_hist) if len(dew_hist) >= max(1, min_samples) else None
    dew_anomaly = (dewpoint_now - dew_baseline) if dewpoint_now is not None and dew_baseline is not None else None
    ventilation_index, ventilation_regime = _basin_ventilation(features_in)
    out = {
        "status": "ok" if len(selected_tmax) >= max(1, min_samples) else "insufficient_climo_history",
        "climo_tmax_mean_c": mean,
        "climo_tmax_p50_c": p50,
        "climo_tmax_p90_c": p90,
        "climo_tmax_p95_c": p95,
        "climo_tmax_sample_count": len(selected_tmax),
        "climo_tmax_basis": basis,
        "climo_tmax_month": target_day.month,
        "dewpoint_c": dewpoint_now,
        "dewpoint_baseline_c": dew_baseline,
        "dewpoint_baseline_sample_count": len(dew_hist),
        "dewpoint_anomaly_c": dew_anomaly,
        "monsoon_humidity_regime": _monsoon_humidity_regime(dewpoint_now, dew_anomaly),
        "subtropical_high_regime": _subtropical_high_regime(features_in, p90_c=p90),
        "basin_ventilation_index": ventilation_index,
        "basin_ventilation_regime": ventilation_regime,
        "source": str(history_source or "event_daily_temps_visible_history+event_runs_weather_features_proxy"),
        "updated_at": updated_at or _now_iso(),
    }
    mechanism_model = basin_heat_mechanism_model(out | features_in, p90_c=p90)
    out["basin_heat_mechanism_model"] = mechanism_model
    out["basin_heat_mechanism_state"] = mechanism_model.get("state")
    out["basin_heat_mechanism_status"] = mechanism_model.get("status")
    out["basin_heat_uplift_index"] = mechanism_model.get("heat_uplift_index")
    out["basin_heat_mechanism_support_score"] = mechanism_model.get("support_score")
    out["basin_heat_mechanism_suppression_score"] = mechanism_model.get("suppression_score")
    out["basin_heat_mechanism_temperature_adjustment_c"] = mechanism_model.get("temperature_adjustment_c")
    out["basin_heat_mechanism_active_support_keys"] = mechanism_model.get("active_support_keys") or []
    out["basin_heat_mechanism_active_suppression_keys"] = mechanism_model.get("active_suppression_keys") or []
    for key in ("hours_to_peak_window", "hours_to_peak"):
        if key in features_in:
            out[key] = features_in.get(key)
    out["primary_regime_label"] = climo_primary_regime(out)
    out["basin_regime"] = derive_basin_regime_label(out)
    out["regime_source"] = out["source"]
    out["regime_updated_at"] = out["updated_at"]
    out["regime_confidence"] = climo_regime_confidence(out)
    out["high_temp_potential_gate"] = climo_high_temp_potential_gate(out | features_in)
    out["temperature_trajectory_priority"] = True
    out["background_layer_label"] = "气候/环流弱参考，不覆盖实况温度轨迹"
    out["potential_not_realized_label"] = "支持升温潜力 ≠ 已兑现升温"
    out["trajectory_priority_label"] = "实况温度轨迹优先"
    return out


def _load_history_rows_from_conn(
    conn: sqlite3.Connection,
    *,
    city: Optional[str],
    station_id: Optional[str],
    market_var: str,
    market_unit: str,
    target_event_date: str,
    as_of: str,
) -> Tuple[List[Dict[str, Any]], List[float]]:
    city_like = f"%{_normalize_text(city)}%" if _normalize_text(city) else "%"
    station = str(station_id or "").strip()
    target_day = _parse_date(target_event_date)
    start_event_date = (
        (target_day - timedelta(days=730)).isoformat()
        if target_day is not None
        else "1900-01-01"
    )
    rows: List[Dict[str, Any]] = []
    dewpoints: List[float] = []
    try:
        daily_rows = conn.execute(
            """
            SELECT event_date, settlement_actual, created_at, resolution_source
            FROM event_daily_temps
            WHERE market_var = ?
              AND market_unit = ?
              AND event_date >= ?
              AND event_date < ?
              AND datetime(created_at) < datetime(?)
              AND settlement_actual IS NOT NULL
              AND (? = '' OR upper(coalesce(station_id, '')) = upper(?) OR lower(coalesce(city, '')) LIKE ?)
            ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
            LIMIT 2000
            """,
            (market_var, market_unit, start_event_date, target_event_date, as_of, station, station, city_like),
        ).fetchall()
    except sqlite3.Error:
        daily_rows = []
    seen = set()
    for row in daily_rows:
        event_date = _date_key(row["event_date"])
        actual = _to_float(row["settlement_actual"])
        if not event_date or event_date in seen or actual is None:
            continue
        seen.add(event_date)
        rows.append({"event_date": event_date, "actual": float(actual), "created_at": str(row["created_at"] or "")})
    try:
        run_rows = conn.execute(
            """
            SELECT event_date, settlement_actual, payload_json, created_at
            FROM event_runs
            WHERE market_var = ?
              AND market_unit = ?
              AND event_date >= ?
              AND event_date < ?
              AND datetime(created_at) < datetime(?)
              AND settlement_actual IS NOT NULL
              AND (? = '' OR upper(coalesce(station_id, '')) = upper(?) OR lower(coalesce(city, '')) LIKE ?)
            ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
            LIMIT 2000
            """,
            (market_var, market_unit, start_event_date, target_event_date, as_of, station, station, city_like),
        ).fetchall()
    except sqlite3.Error:
        run_rows = []
    for row in run_rows:
        event_date = _date_key(row["event_date"])
        actual = _to_float(row["settlement_actual"])
        if event_date and event_date not in seen and actual is not None:
            seen.add(event_date)
            rows.append({"event_date": event_date, "actual": float(actual), "created_at": str(row["created_at"] or "")})
        features = _extract_weather_features_from_payload(row["payload_json"])
        dew = _first_feature_float(features, "dewpoint_c", "dew_point_c", "dewpoint_2m_c", "zuuu_metar_raw_awc_latest_dewpoint_c")
        if dew is not None:
            dewpoints.append(float(dew))
    return rows, dewpoints


def load_climo_regime_context(
    *,
    db_path: Any,
    labels: Sequence[str],
    target_event_date: str,
    as_of: str,
    city: Optional[str],
    station_id: Optional[str],
    market_var: str,
    market_unit: str,
    rounding_rule: Optional[str],
    weather_features: Optional[Mapping[str, Any]] = None,
    days_to_event: Optional[float] = None,
    min_samples: int = 3,
    reference_path: Any = None,
    reference_min_samples: int = 30,
) -> Dict[str, Any]:
    reference_rows, reference_dewpoints, reference_meta = _load_reference_climo_rows(
        reference_path,
        target_event_date=target_event_date,
    )
    reference_usable = len(reference_rows) >= max(1, int(reference_min_samples))
    path = Path(str(db_path or ""))
    if not path.exists():
        if reference_usable:
            features = build_climo_regime_features(
                tmax_history=reference_rows,
                target_event_date=target_event_date,
                weather_features=weather_features,
                dewpoint_history_c=reference_dewpoints,
                updated_at=as_of,
                min_samples=min_samples,
                history_source=str(reference_meta.get("source") or "open_meteo_archive_reference_prior_years"),
            )
            features.update(
                {
                    "climo_reference_used": True,
                    "climo_reference_meta": reference_meta,
                    "created_at_guard": "reference_uses_only_years_before_target_event_year",
                }
            )
            probs = climo_regime_bucket_probabilities(labels, features, rounding_rule=rounding_rule)
            return {
                "enabled": True,
                "status": str(features.get("status") or "ok"),
                "features": features,
                "bucket_probabilities": probs,
                "evidence_scale": climo_regime_evidence_scale(days_to_event=days_to_event, features=features),
                "explanation": climo_regime_explanation(features),
            }
        features = build_climo_regime_features(
            tmax_history=[],
            target_event_date=target_event_date,
            weather_features=weather_features,
            updated_at=as_of,
            min_samples=min_samples,
        )
        return {
            "enabled": True,
            "status": "db_missing",
            "features": features,
            "bucket_probabilities": {},
            "evidence_scale": climo_regime_evidence_scale(days_to_event=days_to_event, features=features),
            "explanation": climo_regime_explanation(features),
            "reference_meta": reference_meta,
        }
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
        conn.row_factory = sqlite3.Row
        history, dewpoints = _load_history_rows_from_conn(
            conn,
            city=city,
            station_id=station_id,
            market_var=market_var,
            market_unit=market_unit,
            target_event_date=target_event_date,
            as_of=as_of,
        )
    except Exception as exc:
        features = build_climo_regime_features(
            tmax_history=[],
            target_event_date=target_event_date,
            weather_features=weather_features,
            updated_at=as_of,
            min_samples=min_samples,
        )
        return {
            "enabled": True,
            "status": "history_load_error",
            "error": str(exc)[:160],
            "features": features,
            "bucket_probabilities": {},
            "evidence_scale": climo_regime_evidence_scale(days_to_event=days_to_event, features=features),
            "explanation": climo_regime_explanation(features),
        }
    finally:
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass
    features = build_climo_regime_features(
        tmax_history=reference_rows if reference_usable else history,
        target_event_date=target_event_date,
        weather_features=weather_features,
        dewpoint_history_c=reference_dewpoints if reference_usable and reference_dewpoints else dewpoints,
        updated_at=as_of,
        min_samples=min_samples,
        history_source=(
            str(reference_meta.get("source") or "open_meteo_archive_reference_prior_years")
            if reference_usable
            else "event_daily_temps_visible_history+event_runs_weather_features_proxy"
        ),
    )
    features.update(
        {
            "climo_reference_used": bool(reference_usable),
            "climo_reference_meta": reference_meta,
            "created_at_guard": (
                "reference_uses_only_years_before_target_event_year"
                if reference_usable
                else "uses_only_settled_history_visible_before_sample_created_at"
            ),
        }
    )
    probs = climo_regime_bucket_probabilities(labels, features, rounding_rule=rounding_rule)
    return {
        "enabled": True,
        "status": str(features.get("status") or "ok"),
        "features": features,
        "bucket_probabilities": probs,
        "evidence_scale": climo_regime_evidence_scale(days_to_event=days_to_event, features=features),
        "explanation": climo_regime_explanation(features),
    }
