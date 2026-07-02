#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "chengdu_model_hit_rate_audit_latest.json"
DEFAULT_LOC_KEY = "30.5583,103.9460"
DEFAULT_STATION_ID = "ZUUU"
DEFAULT_MODELS = ("gfs_seamless", "icon_seamless", "ecmwf_ifs025")
DEFAULT_MARKET_VAR = "temperature_2m_max"
DEFAULT_MARKET_UNIT = "C"
DEFAULT_TZ = "Asia/Shanghai"
BUCKET_INDICES = tuple(range(25, 36))
CONTINUOUS_WEIGHT_METRICS = ("brier", "rps", "logloss", "crps_c")
SHADOW_SHRINKAGE_K_DAYS = 40.0
SHADOW_SINGLE_MODEL_WEIGHT_CAP = 0.45
MIN_LIVE_MARKET_DAYS_FOR_RUNTIME_WEIGHT_CHANGE = 80
MIN_LIVE_MARKET_DAYS_FOR_SHADOW_WEIGHT = 30


def parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def bucket_label(value: float, *, rule: str) -> str:
    k = bucket_index(value, rule=rule)
    if k <= 25:
        return "25°C or below"
    if k >= 35:
        return "35°C or higher"
    return f"{k}°C"


def bucket_index(value: float, *, rule: str) -> int:
    raw = str(rule or "round").strip().lower()
    if raw in {"floor", "whole_degrees", "floor_degrees"}:
        k = math.floor(float(value))
    else:
        k = int(math.floor(float(value) + 0.5))
    return max(25, min(35, int(k)))


def dte_bucket(event_date: str, created_at: str, *, tz_name: str = DEFAULT_TZ) -> str:
    created = parse_dt(created_at)
    if created is None:
        return "unknown"
    try:
        event_day = date.fromisoformat(str(event_date or "")[:10])
    except ValueError:
        return "unknown"
    zone = ZoneInfo(tz_name)
    delta = (event_day - created.astimezone(zone).date()).days
    if delta < 0:
        return "post_event_local"
    if delta == 0:
        return "d0"
    if delta == 1:
        return "d_minus_1"
    if delta == 2:
        return "d_minus_2"
    return "d_minus_3_plus"


def before_peak_window_start(
    event_date: str,
    created_at: str,
    *,
    tz_name: str = DEFAULT_TZ,
    peak_start_hour_local: int = 12,
) -> bool:
    created = parse_dt(created_at)
    if created is None:
        return False
    try:
        event_day = date.fromisoformat(str(event_date or "")[:10])
    except ValueError:
        return False
    zone = ZoneInfo(tz_name)
    peak_start = datetime.combine(
        event_day,
        time(max(0, min(23, int(peak_start_hour_local))), 0),
        tzinfo=zone,
    ).astimezone(timezone.utc)
    return created < peak_start


def _load_members(raw: Any) -> List[float]:
    try:
        payload = json.loads(str(raw or ""))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    values: List[float] = []
    for item in payload:
        val = _to_float(item)
        if val is not None:
            values.append(float(val))
    return values


def _load_json_object(raw: Any) -> Dict[str, Any]:
    try:
        payload = json.loads(str(raw or ""))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _top_bucket(values: Sequence[float], *, rule: str) -> str:
    counts = Counter(bucket_label(float(v), rule=rule) for v in values)
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-int(item[1]), str(item[0])))[0][0]


def _top_bucket_indices(values: Sequence[float], *, rule: str, n: int = 3) -> List[int]:
    counts = Counter(bucket_index(float(v), rule=rule) for v in values)
    return [int(k) for k, _count in sorted(counts.items(), key=lambda item: (-int(item[1]), int(item[0])))[: max(1, int(n))]]


def _bucket_probabilities(values: Sequence[float], *, rule: str) -> Dict[str, float]:
    counts = Counter(bucket_index(float(v), rule=rule) for v in values)
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {str(k): float(counts.get(k, 0)) / float(total) for k in BUCKET_INDICES}


def _mean(values: Sequence[float]) -> Optional[float]:
    nums = [float(v) for v in values if math.isfinite(float(v))]
    return sum(nums) / len(nums) if nums else None


def _wilson_interval(hits: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = float(hits) / float(n)
    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt((p * (1.0 - p) / n) + (z * z) / (4.0 * n * n))
    return max(0.0, center - margin), min(1.0, center + margin)


def _brier_score(probs: Mapping[str, float], actual_idx: int) -> Optional[float]:
    if not probs:
        return None
    return sum((float(probs.get(str(k), 0.0)) - (1.0 if int(k) == int(actual_idx) else 0.0)) ** 2 for k in BUCKET_INDICES)


def _logloss_score(probs: Mapping[str, float], actual_idx: int) -> Optional[float]:
    if not probs:
        return None
    return -math.log(max(1e-12, float(probs.get(str(int(actual_idx)), 0.0))))


def _rps_score(probs: Mapping[str, float], actual_idx: int) -> Optional[float]:
    if not probs:
        return None
    acc_p = 0.0
    total = 0.0
    for k in BUCKET_INDICES:
        acc_p += float(probs.get(str(k), 0.0))
        obs_cdf = 1.0 if int(k) >= int(actual_idx) else 0.0
        total += (acc_p - obs_cdf) ** 2
    return total / max(1, len(BUCKET_INDICES) - 1)


def _crps_score(probs: Mapping[str, float], actual_idx: int) -> Optional[float]:
    if not probs:
        return None
    acc_p = 0.0
    total = 0.0
    for k in BUCKET_INDICES:
        acc_p += float(probs.get(str(k), 0.0))
        obs_cdf = 1.0 if int(k) >= int(actual_idx) else 0.0
        total += (acc_p - obs_cdf) ** 2
    return total


def _season(event_date: str) -> str:
    try:
        month = int(str(event_date or "")[5:7])
    except Exception:
        return "unknown"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


def _load_actuals(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    market_var: str,
    market_unit: str,
    start_date: str,
    end_date: str,
) -> Dict[str, Dict[str, Any]]:
    actuals: Dict[str, Dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT event_date, settlement_actual, resolution_source, created_at, 'event_runs' AS source_table
        FROM event_runs
        WHERE upper(coalesce(station_id, '')) = upper(?)
          AND market_var = ?
          AND market_unit = ?
          AND settlement_actual IS NOT NULL
          AND event_date >= ?
          AND event_date <= ?
        ORDER BY event_date ASC, datetime(created_at) DESC, id DESC
        """,
        (station_id, market_var, market_unit, start_date, end_date),
    ):
        event_date = str(row["event_date"] or "")[:10]
        if event_date and event_date not in actuals:
            actuals[event_date] = dict(row)
    for row in conn.execute(
        """
        SELECT event_date, settlement_actual, resolution_source, created_at, 'event_daily_temps' AS source_table
        FROM event_daily_temps
        WHERE upper(coalesce(station_id, '')) = upper(?)
          AND market_var = ?
          AND market_unit = ?
          AND settlement_actual IS NOT NULL
          AND event_date >= ?
          AND event_date <= ?
        ORDER BY event_date ASC,
                 CASE WHEN lower(coalesce(resolution_source, '')) LIKE '%wunderground%' THEN 0 ELSE 1 END,
                 datetime(created_at) DESC,
                 id DESC
        """,
        (station_id, market_var, market_unit, start_date, end_date),
    ):
        event_date = str(row["event_date"] or "")[:10]
        if event_date and event_date not in actuals:
            actuals[event_date] = dict(row)
    return actuals


def _model_rows(
    conn: sqlite3.Connection,
    *,
    actuals: Mapping[str, Mapping[str, Any]],
    models: Sequence[str],
    loc_key: str,
    market_var: str,
    market_unit: str,
    start_date: str,
    end_date: str,
    tz_name: str,
    peak_start_hour_local: int,
) -> List[Dict[str, Any]]:
    if not models:
        return []
    placeholders = ",".join("?" for _ in models)
    columns = _table_columns(conn, "model_member_cache")
    if {
        "basin_regime",
        "basin_regime_features_json",
        "basin_regime_source",
        "basin_regime_updated_at",
        "basin_regime_confidence",
    }.issubset(columns):
        basin_select = (
            "basin_regime, basin_regime_features_json, basin_regime_source, "
            "basin_regime_updated_at, basin_regime_confidence"
        )
    else:
        basin_select = (
            "'' AS basin_regime, '' AS basin_regime_features_json, "
            "'' AS basin_regime_source, '' AS basin_regime_updated_at, "
            "NULL AS basin_regime_confidence"
        )
    rows: List[Dict[str, Any]] = []
    for row in conn.execute(
        f"""
        SELECT id, model, loc_key, event_date, market_var, market_unit, members_json,
               created_at, official_updated_at, official_updated_source,
               {basin_select}
        FROM model_member_cache
        WHERE loc_key = ?
          AND market_var = ?
          AND market_unit = ?
          AND event_date >= ?
          AND event_date <= ?
          AND model IN ({placeholders})
        ORDER BY event_date ASC, model ASC, datetime(created_at) ASC, id ASC
        """,
        (loc_key, market_var, market_unit, start_date, end_date, *models),
    ):
        event_date = str(row["event_date"] or "")[:10]
        actual = actuals.get(event_date)
        actual_c = _to_float(actual.get("settlement_actual") if actual else None)
        if actual_c is None:
            continue
        values = _load_members(row["members_json"])
        mean_c = _mean(values)
        if mean_c is None:
            continue
        basin_features = _load_json_object(row["basin_regime_features_json"])
        basin_regime = str(row["basin_regime"] or "").strip() or str(
            basin_features.get("basin_regime") or ""
        ).strip()
        if not basin_regime:
            basin_regime = "unknown"
        dte = dte_bucket(event_date, str(row["created_at"] or ""), tz_name=tz_name)
        before_peak = before_peak_window_start(
            event_date,
            str(row["created_at"] or ""),
            tz_name=tz_name,
            peak_start_hour_local=peak_start_hour_local,
        )
        rows.append(
            {
                "id": int(row["id"]),
                "model": str(row["model"] or ""),
                "event_date": event_date,
                "created_at": str(row["created_at"] or ""),
                "created_at_local": (
                    parse_dt(row["created_at"]).astimezone(ZoneInfo(tz_name)).isoformat()
                    if parse_dt(row["created_at"])
                    else ""
                ),
                "official_updated_at": str(row["official_updated_at"] or ""),
                "official_updated_source": str(row["official_updated_source"] or ""),
                "basin_regime": basin_regime,
                "basin_regime_source": str(row["basin_regime_source"] or basin_features.get("source") or ""),
                "basin_regime_updated_at": str(row["basin_regime_updated_at"] or basin_features.get("updated_at") or ""),
                "basin_regime_confidence": _to_float(row["basin_regime_confidence"]),
                "basin_regime_features": basin_features,
                "dte_bucket": dte,
                "before_peak_window_start": bool(before_peak),
                "member_count": len(values),
                "mean_c": mean_c,
                "median_c": float(median(values)),
                "top_round_label": _top_bucket(values, rule="round"),
                "top3_round_indices": _top_bucket_indices(values, rule="round", n=3),
                "top_round_index": _top_bucket_indices(values, rule="round", n=1)[0],
                "mean_round_label": bucket_label(mean_c, rule="round"),
                "mean_round_index": bucket_index(mean_c, rule="round"),
                "bucket_probabilities_round": _bucket_probabilities(values, rule="round"),
                "top_floor_label": _top_bucket(values, rule="floor"),
                "top3_floor_indices": _top_bucket_indices(values, rule="floor", n=3),
                "top_floor_index": _top_bucket_indices(values, rule="floor", n=1)[0],
                "mean_floor_label": bucket_label(mean_c, rule="floor"),
                "mean_floor_index": bucket_index(mean_c, rule="floor"),
                "bucket_probabilities_floor": _bucket_probabilities(values, rule="floor"),
                "actual_c": float(actual_c),
                "actual_round_label": bucket_label(float(actual_c), rule="round"),
                "actual_round_index": bucket_index(float(actual_c), rule="round"),
                "actual_floor_label": bucket_label(float(actual_c), rule="floor"),
                "actual_floor_index": bucket_index(float(actual_c), rule="floor"),
                "actual_source_table": str(actual.get("source_table") if actual else ""),
                "actual_resolution_source": str(actual.get("resolution_source") if actual else ""),
                "season": _season(event_date),
                "hot_day": float(actual_c) >= 30.0,
            }
        )
    return rows


def _model_cache_regime_rows(
    conn: sqlite3.Connection,
    *,
    models: Sequence[str],
    loc_key: str,
    market_var: str,
    market_unit: str,
    start_date: str,
    end_date: str,
    tz_name: str,
    peak_start_hour_local: int,
) -> List[Dict[str, Any]]:
    columns = _table_columns(conn, "model_member_cache")
    if "basin_regime" not in columns:
        return []
    placeholders = ",".join("?" for _ in models)
    feature_select = "basin_regime_features_json" if "basin_regime_features_json" in columns else "'' AS basin_regime_features_json"
    source_select = "basin_regime_source" if "basin_regime_source" in columns else "'' AS basin_regime_source"
    updated_select = "basin_regime_updated_at" if "basin_regime_updated_at" in columns else "'' AS basin_regime_updated_at"
    confidence_select = "basin_regime_confidence" if "basin_regime_confidence" in columns else "NULL AS basin_regime_confidence"
    rows: List[Dict[str, Any]] = []
    for row in conn.execute(
        f"""
        SELECT id, model, event_date, created_at, official_updated_source,
               basin_regime, {feature_select}, {source_select}, {updated_select}, {confidence_select}
        FROM model_member_cache
        WHERE loc_key = ?
          AND market_var = ?
          AND market_unit = ?
          AND event_date >= ?
          AND event_date <= ?
          AND model IN ({placeholders})
          AND COALESCE(official_updated_source, '') != 'open_meteo_historical_forecast'
          AND COALESCE(basin_regime, '') NOT IN ('', 'unknown', 'all')
        ORDER BY event_date ASC, model ASC, datetime(created_at) ASC, id ASC
        """,
        (loc_key, market_var, market_unit, start_date, end_date, *models),
    ):
        event_date = str(row["event_date"] or "")[:10]
        basin_regime = str(row["basin_regime"] or "").strip()
        if not basin_regime:
            continue
        dte = dte_bucket(event_date, str(row["created_at"] or ""), tz_name=tz_name)
        rows.append(
            {
                "id": int(row["id"]),
                "model": str(row["model"] or ""),
                "event_date": event_date,
                "created_at": str(row["created_at"] or ""),
                "official_updated_source": str(row["official_updated_source"] or ""),
                "basin_regime": basin_regime,
                "basin_regime_source": str(row["basin_regime_source"] or ""),
                "basin_regime_updated_at": str(row["basin_regime_updated_at"] or ""),
                "basin_regime_confidence": _to_float(row["basin_regime_confidence"]),
                "basin_regime_features": _load_json_object(row["basin_regime_features_json"]),
                "dte_bucket": dte,
                "before_peak_window_start": before_peak_window_start(
                    event_date,
                    str(row["created_at"] or ""),
                    tz_name=tz_name,
                    peak_start_hour_local=peak_start_hour_local,
                ),
                "season": _season(event_date),
                "has_settlement_label": False,
            }
        )
    return rows


def _latest_by(keys: Iterable[Tuple[str, str]], rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    latest: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    key_set = set(keys)
    for row in rows:
        key = (str(row.get("model") or ""), str(row.get("event_date") or ""))
        if key_set and key not in key_set:
            continue
        prev = latest.get(key)
        if prev is None or str(row.get("created_at") or "") > str(prev.get("created_at") or ""):
            latest[key] = row
    return [dict(v) for v in latest.values()]


def _summarize(rows: Sequence[Mapping[str, Any]], *, models: Sequence[str], metrics: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for metric in metrics:
        actual_field = "actual_round_label" if metric.endswith("_round_label") else "actual_floor_label"
        model_rows: Dict[str, Any] = {}
        for model in models:
            subset = [row for row in rows if str(row.get("model") or "") == model]
            hits = sum(1 for row in subset if str(row.get(metric) or "") == str(row.get(actual_field) or ""))
            n = len(subset)
            lower, upper = _wilson_interval(hits, n)
            mae_values = [
                abs(float(row["mean_c"]) - float(row["actual_c"]))
                for row in subset
                if _to_float(row.get("mean_c")) is not None and _to_float(row.get("actual_c")) is not None
            ]
            bias_values = [
                float(row["mean_c"]) - float(row["actual_c"])
                for row in subset
                if _to_float(row.get("mean_c")) is not None and _to_float(row.get("actual_c")) is not None
            ]
            hot_subset = [row for row in subset if bool(row.get("hot_day"))]
            hot_hits = sum(1 for row in hot_subset if str(row.get(metric) or "") == str(row.get(actual_field) or ""))
            model_rows[model] = {
                "n": n,
                "market_day_count": len({str(row.get("event_date") or "") for row in subset}),
                "hits": hits,
                "hit_rate": (hits / n) if n else None,
                "hit_rate_wilson_95": {"low": lower, "high": upper},
                "mae_c": (sum(mae_values) / len(mae_values)) if mae_values else None,
                "bias_c": (sum(bias_values) / len(bias_values)) if bias_values else None,
                "hot_day_n": len(hot_subset),
                "hot_day_hit_rate": (hot_hits / len(hot_subset)) if hot_subset else None,
            }
        out[metric] = model_rows
    return out


def _rate(count: int, total: int) -> Optional[float]:
    return (int(count) / int(total)) if int(total or 0) > 0 else None


def _summarize_trade_metrics(rows: Sequence[Mapping[str, Any]], *, models: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    thresholds = (30, 31, 32)
    for model in models:
        subset = [row for row in rows if str(row.get("model") or "") == model]
        n = len(subset)
        exact_hits = sum(
            int(row.get("top_round_index") or -999) == int(row.get("actual_round_index") or -998)
            for row in subset
        )
        top3_hits = sum(
            int(row.get("actual_round_index") or -999) in [int(x) for x in (row.get("top3_round_indices") or [])]
            for row in subset
        )
        top1_pm1 = sum(
            abs(int(row.get("top_round_index") or -999) - int(row.get("actual_round_index") or -998)) <= 1
            for row in subset
        )
        mean_pm1 = sum(
            abs(int(row.get("mean_round_index") or -999) - int(row.get("actual_round_index") or -998)) <= 1
            for row in subset
        )
        threshold_hits = {}
        for threshold in thresholds:
            hits = sum(
                (float(row.get("mean_c") or 0.0) >= float(threshold))
                == (float(row.get("actual_c") or 0.0) >= float(threshold))
                for row in subset
            )
            threshold_hits[f"mean_ge_{threshold}c_direction_hit_rate"] = _rate(hits, n)
        out[model] = {
            "n": n,
            "market_day_count": len({str(row.get("event_date") or "") for row in subset}),
            "exact_top1_hit_rate": _rate(exact_hits, n),
            "top3_contains_actual_rate": _rate(top3_hits, n),
            "top1_within_1c_rate": _rate(top1_pm1, n),
            "mean_within_1c_rate": _rate(mean_pm1, n),
            "threshold_direction": threshold_hits,
            "interpretation": (
                "exact_top1 is strict single settlement-bucket accuracy; threshold_direction and within_1c "
                "are trading/temperature skill diagnostics and are not interchangeable."
            ),
        }
    return out


def _summarize_distribution_metrics(rows: Sequence[Mapping[str, Any]], *, models: Sequence[str], rule: str = "round") -> Dict[str, Any]:
    probs_field = "bucket_probabilities_round" if str(rule) == "round" else "bucket_probabilities_floor"
    actual_field = "actual_round_index" if str(rule) == "round" else "actual_floor_index"
    out: Dict[str, Any] = {}
    for model in models:
        subset = [row for row in rows if str(row.get("model") or "") == model]
        briers: List[float] = []
        loglosses: List[float] = []
        rps_values: List[float] = []
        crps_values: List[float] = []
        for row in subset:
            probs = row.get(probs_field) if isinstance(row.get(probs_field), Mapping) else {}
            actual = _to_float(row.get(actual_field))
            if actual is None:
                continue
            brier = _brier_score(probs, int(actual))
            logloss = _logloss_score(probs, int(actual))
            rps = _rps_score(probs, int(actual))
            crps = _crps_score(probs, int(actual))
            if brier is not None:
                briers.append(float(brier))
            if logloss is not None:
                loglosses.append(float(logloss))
            if rps is not None:
                rps_values.append(float(rps))
            if crps is not None:
                crps_values.append(float(crps))
        out[model] = {
            "n": len(subset),
            "market_day_count": len({str(row.get("event_date") or "") for row in subset}),
            "rule": str(rule),
            "brier": (sum(briers) / len(briers)) if briers else None,
            "logloss": (sum(loglosses) / len(loglosses)) if loglosses else None,
            "rps": (sum(rps_values) / len(rps_values)) if rps_values else None,
            "crps_c": (sum(crps_values) / len(crps_values)) if crps_values else None,
            "metric_direction": "lower_is_better",
        }
    return out


def _history_reference_summary(rows: Sequence[Mapping[str, Any]], *, models: Sequence[str]) -> Dict[str, Any]:
    summary = {
        "overall": _summarize(rows, models=models, metrics=("mean_round_label", "mean_floor_label")),
        "by_season": {},
    }
    for season in sorted({str(row.get("season") or "unknown") for row in rows}):
        season_rows = [row for row in rows if str(row.get("season") or "unknown") == season]
        summary["by_season"][season] = _summarize(
            season_rows,
            models=models,
            metrics=("mean_round_label", "mean_floor_label"),
        )
    return summary


def _normalized_loss_scores(
    metrics_by_model: Mapping[str, Mapping[str, Any]],
    *,
    models: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    medians: Dict[str, float] = {}
    for field in CONTINUOUS_WEIGHT_METRICS:
        values = [
            float(value)
            for model in models
            for value in [_to_float((metrics_by_model.get(model) or {}).get(field))]
            if value is not None and value > 0
        ]
        if values:
            medians[field] = float(median(values))
    out: Dict[str, Dict[str, Any]] = {}
    for model in models:
        row = metrics_by_model.get(model) or {}
        normalized: Dict[str, float] = {}
        for field, med in medians.items():
            value = _to_float(row.get(field))
            if value is not None and value > 0 and med > 0:
                normalized[field] = float(value) / float(med)
        composite = (sum(normalized.values()) / len(normalized)) if normalized else None
        score = (1.0 / max(0.05, float(composite))) if composite is not None else 1.0
        out[model] = {
            "normalized_losses": normalized,
            "composite_loss": composite,
            "raw_score": score,
        }
    return out


def _renormalize_weights(weights: Mapping[str, float]) -> Dict[str, float]:
    clean = {str(k): max(0.0, float(v)) for k, v in weights.items() if math.isfinite(float(v))}
    total = sum(clean.values())
    if total <= 0:
        n = max(1, len(clean))
        return {model: 1.0 / n for model in clean}
    return {model: value / total for model, value in clean.items()}


def _cap_and_renormalize_weights(weights: Mapping[str, float], *, cap: float) -> Dict[str, float]:
    remaining = _renormalize_weights(weights)
    fixed: Dict[str, float] = {}
    cap_value = max(0.0, min(1.0, float(cap)))
    for _ in range(len(remaining) + 1):
        fixed_total = sum(fixed.values())
        free_total = sum(remaining.values())
        if free_total <= 0 or fixed_total >= 1.0:
            break
        scaled = {model: value * (1.0 - fixed_total) / free_total for model, value in remaining.items()}
        over = {model: value for model, value in scaled.items() if value > cap_value}
        if not over:
            fixed.update(scaled)
            remaining = {}
            break
        for model in over:
            fixed[model] = cap_value
            remaining.pop(model, None)
    if remaining:
        fixed.update(remaining)
    return _renormalize_weights(fixed)


def _shadow_weight_segment(
    rows: Sequence[Mapping[str, Any]],
    *,
    models: Sequence[str],
    segment_key: str,
    dte: str,
    season: str,
    basin_regime: str,
    segment_level: str,
) -> Dict[str, Any]:
    metrics = _summarize_distribution_metrics(rows, models=models, rule="round")
    trade_skill = _summarize_trade_metrics(rows, models=models)
    score_rows = _normalized_loss_scores(metrics, models=models)
    raw_scores = {model: float((score_rows.get(model) or {}).get("raw_score") or 1.0) for model in models}
    raw_weights = _renormalize_weights(raw_scores)
    market_days = len({str(row.get("event_date") or "") for row in rows})
    shrinkage_alpha = (
        float(market_days) / (float(market_days) + SHADOW_SHRINKAGE_K_DAYS)
        if market_days > 0
        else 0.0
    )
    equal_weight = 1.0 / max(1, len(models))
    shrunk = {
        model: (equal_weight * (1.0 - shrinkage_alpha)) + (raw_weights.get(model, equal_weight) * shrinkage_alpha)
        for model in models
    }
    capped = _cap_and_renormalize_weights(shrunk, cap=SHADOW_SINGLE_MODEL_WEIGHT_CAP)
    blockers: List[str] = []
    if market_days < MIN_LIVE_MARKET_DAYS_FOR_RUNTIME_WEIGHT_CHANGE:
        blockers.append("live_market_days_below_runtime_gate")
    if market_days < MIN_LIVE_MARKET_DAYS_FOR_SHADOW_WEIGHT:
        blockers.append("live_market_days_below_shadow_gate")
    if str(basin_regime or "").lower() in {"", "unknown", "all"} and "basin_regime" in str(segment_level):
        blockers.append("basin_regime_unavailable_for_local_precision")
    blockers.append("strict_walk_forward_non_degradation_not_yet_verified")
    model_rows: List[Dict[str, Any]] = []
    for model in models:
        metric_row = metrics.get(model) or {}
        trade_row = trade_skill.get(model) or {}
        score_row = score_rows.get(model) or {}
        model_rows.append(
            {
                "model": model,
                "raw_weight": raw_weights.get(model, equal_weight),
                "shrunk_weight": shrunk.get(model, equal_weight),
                "shadow_weight": capped.get(model, equal_weight),
                "composite_loss": score_row.get("composite_loss"),
                "normalized_losses": score_row.get("normalized_losses") or {},
                "metrics": {field: metric_row.get(field) for field in CONTINUOUS_WEIGHT_METRICS},
                "sample_rows": metric_row.get("n"),
                "sample_market_days": metric_row.get("market_day_count"),
                "diagnostics_only": {
                    "exact_top1_hit_rate": trade_row.get("exact_top1_hit_rate"),
                    "top3_contains_actual_rate": trade_row.get("top3_contains_actual_rate"),
                    "mean_within_1c_rate": trade_row.get("mean_within_1c_rate"),
                    "threshold_direction": trade_row.get("threshold_direction") or {},
                },
            }
        )
    best_by_metric: Dict[str, str] = {}
    for field in CONTINUOUS_WEIGHT_METRICS:
        candidates = [
            (model, _to_float((metrics.get(model) or {}).get(field)))
            for model in models
        ]
        candidates = [(model, value) for model, value in candidates if value is not None]
        if candidates:
            best_by_metric[field] = min(candidates, key=lambda item: float(item[1]))[0]
    return {
        "segment_key": segment_key,
        "segment_level": segment_level,
        "dte": dte,
        "season": season,
        "basin_regime": basin_regime,
        "sample_market_days": market_days,
        "sample_rows": len(rows),
        "runtime_promotion_allowed": market_days >= MIN_LIVE_MARKET_DAYS_FOR_RUNTIME_WEIGHT_CHANGE and not blockers,
        "shadow_display_allowed": market_days >= MIN_LIVE_MARKET_DAYS_FOR_SHADOW_WEIGHT,
        "not_online_reasons": blockers,
        "weighting_basis": list(CONTINUOUS_WEIGHT_METRICS),
        "exact_top1_role": "diagnostics_only_not_weight_basis",
        "shrinkage": {
            "method": "hierarchical_shrink_to_equal_weight",
            "k_days": SHADOW_SHRINKAGE_K_DAYS,
            "alpha": shrinkage_alpha,
            "equal_weight": equal_weight,
            "single_model_cap": SHADOW_SINGLE_MODEL_WEIGHT_CAP,
        },
        "best_by_metric": best_by_metric,
        "model_weights": sorted(model_rows, key=lambda row: float(row.get("shadow_weight") or 0.0), reverse=True),
    }


def _build_model_weight_shadow(
    *,
    live_d1: Sequence[Mapping[str, Any]],
    live_d0_pre: Sequence[Mapping[str, Any]],
    live_d0_all: Sequence[Mapping[str, Any]],
    models: Sequence[str],
) -> Dict[str, Any]:
    segment_specs: List[Tuple[str, str, Sequence[Mapping[str, Any]]]] = [
        ("d_minus_1", "dte", live_d1),
        ("d0_pre_peak", "dte", live_d0_pre),
        ("d0_all_diagnostic", "diagnostic", live_d0_all),
    ]
    segments: List[Dict[str, Any]] = []
    for dte, level, rows in segment_specs:
        if rows:
            segments.append(
                _shadow_weight_segment(
                    rows,
                    models=models,
                    segment_key=f"{dte}|season=all|basin_regime=all",
                    dte=dte,
                    season="all",
                    basin_regime="all",
                    segment_level=level,
                )
            )
        seasons = sorted({str(row.get("season") or "unknown") for row in rows if str(row.get("season") or "")})
        for season_name in seasons:
            season_rows = [row for row in rows if str(row.get("season") or "unknown") == season_name]
            if not season_rows:
                continue
            segments.append(
                _shadow_weight_segment(
                    season_rows,
                    models=models,
                    segment_key=f"{dte}|season={season_name}|basin_regime=all",
                    dte=dte,
                    season=season_name,
                    basin_regime="all",
                    segment_level=f"{level}+season",
                )
            )
            real_regimes = sorted(
                {
                    str(row.get("basin_regime") or "").strip()
                    for row in season_rows
                    if str(row.get("basin_regime") or "").strip().lower() not in {"", "unknown", "all"}
                }
            )
            for basin_regime in real_regimes:
                regime_rows = [
                    row
                    for row in season_rows
                    if str(row.get("basin_regime") or "").strip() == basin_regime
                ]
                if not regime_rows:
                    continue
                segments.append(
                    _shadow_weight_segment(
                        regime_rows,
                        models=models,
                        segment_key=f"{dte}|season={season_name}|basin_regime={basin_regime}",
                        dte=dte,
                        season=season_name,
                        basin_regime=basin_regime,
                        segment_level=f"{level}+season+basin_regime",
                    )
                )
            if real_regimes:
                continue
            segments.append(
                _shadow_weight_segment(
                    season_rows,
                    models=models,
                    segment_key=f"{dte}|season={season_name}|basin_regime=unknown",
                    dte=dte,
                    season=season_name,
                    basin_regime="unknown",
                    segment_level=f"{level}+season+basin_regime_fallback",
                )
            )
    primary_keys = {
        "d_minus_1|season=all|basin_regime=all",
        "d0_pre_peak|season=all|basin_regime=all",
    }
    primary_segments = [segment for segment in segments if segment.get("segment_key") in primary_keys]
    runtime_allowed = bool(primary_segments) and all(bool(segment.get("runtime_promotion_allowed")) for segment in primary_segments)
    return {
        "version": "model_weight_shadow_v1",
        "status": "shadow_only",
        "read_only": True,
        "applied_to_runtime": False,
        "runtime_promotion_allowed": runtime_allowed,
        "runtime_promotion_blockers": sorted(
            {
                str(reason)
                for segment in primary_segments
                for reason in (segment.get("not_online_reasons") or [])
            }
        ),
        "weighting_basis": list(CONTINUOUS_WEIGHT_METRICS),
        "excluded_from_weighting": ["exact_top1_hit_rate"],
        "fallback_order": [
            "station+dte+season+basin_regime",
            "station+dte+season",
            "station+dte",
            "station_global",
            "similar_city_climate_zone",
            "equal_weight",
        ],
        "promotion_gates": {
            "min_live_market_days_for_runtime_weight_change": MIN_LIVE_MARKET_DAYS_FOR_RUNTIME_WEIGHT_CHANGE,
            "min_live_market_days_for_shadow_weight": MIN_LIVE_MARKET_DAYS_FOR_SHADOW_WEIGHT,
            "requires_strict_walk_forward_non_degradation": True,
            "requires_d1_d0_hot_day_season_regime_non_degradation": True,
        },
        "segments": segments,
    }


def _build_basin_regime_feature_set(
    *,
    live_d1: Sequence[Mapping[str, Any]],
    live_d0_pre: Sequence[Mapping[str, Any]],
    live_d0_all: Sequence[Mapping[str, Any]],
    materialized_cache_rows: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    def real_basin_regime(row: Mapping[str, Any]) -> str:
        label = str(row.get("basin_regime") or "").strip()
        return "" if label.lower() in {"", "unknown", "all"} else label

    all_live_rows = [*live_d1, *live_d0_pre, *live_d0_all]
    materialized_by_id: Dict[str, Mapping[str, Any]] = {}
    for row in [*all_live_rows, *materialized_cache_rows]:
        if not real_basin_regime(row):
            continue
        row_id = str(row.get("id") or "")
        key = row_id if row_id else f"{row.get('model')}|{row.get('event_date')}|{row.get('created_at')}"
        materialized_by_id[key] = row
    materialized_rows = list(materialized_by_id.values())
    materialized = bool(materialized_rows)
    strata: List[Dict[str, Any]] = []
    for dte, rows in (
        ("d_minus_1", live_d1),
        ("d0_pre_peak", live_d0_pre),
        ("d0_all_diagnostic", live_d0_all),
    ):
        cache_rows = [
            row
            for row in materialized_cache_rows
            if str(row.get("dte_bucket") or "") == ("d0" if dte != "d_minus_1" else "d_minus_1")
            and (dte != "d0_pre_peak" or bool(row.get("before_peak_window_start")))
            and (dte != "d0_all_diagnostic" or str(row.get("dte_bucket") or "") == "d0")
        ]
        merged_rows_by_id: Dict[str, Mapping[str, Any]] = {}
        for row in [*cache_rows, *rows]:
            row_id = str(row.get("id") or "")
            key = row_id if row_id else f"{row.get('model')}|{row.get('event_date')}|{row.get('created_at')}"
            merged_rows_by_id[key] = row
        rows = list(merged_rows_by_id.values())
        for season_name in sorted({str(row.get("season") or "unknown") for row in rows}):
            season_rows = [row for row in rows if str(row.get("season") or "unknown") == season_name]
            regimes = sorted({real_basin_regime(row) for row in season_rows if real_basin_regime(row)})
            for regime_name in regimes:
                regime_rows = [row for row in season_rows if real_basin_regime(row) == regime_name]
                labeled_rows = [row for row in regime_rows if row.get("has_settlement_label") is not False]
                strata.append(
                    {
                        "dte": dte,
                        "season": season_name,
                        "basin_regime": regime_name,
                        "market_days": len({str(row.get("event_date") or "") for row in regime_rows}),
                        "labeled_market_days": len({str(row.get("event_date") or "") for row in labeled_rows}),
                        "rows": len(regime_rows),
                        "labeled_rows": len(labeled_rows),
                        "fallback_used": "source+station+dte+season+basin_regime",
                        "promotion_status": (
                            "sample_accumulating_not_runtime_promoted"
                            if labeled_rows
                            else "materialized_waiting_for_settlement_labels"
                        ),
                    }
                )
            if regimes:
                unknown_rows = [row for row in season_rows if not real_basin_regime(row)]
                if not unknown_rows:
                    continue
                season_rows = unknown_rows
            strata.append(
                {
                    "dte": dte,
                    "season": season_name,
                    "basin_regime": "unknown",
                    "market_days": len({str(row.get("event_date") or "") for row in season_rows}),
                    "rows": len(season_rows),
                    "fallback_used": "station+dte+season",
                    "promotion_status": (
                        "fallback_for_rows_without_materialized_basin_regime"
                        if materialized
                        else "blocked_until_basin_regime_feature_is_materialized"
                    ),
                }
            )
    extra_cache_rows = [
        row
        for row in materialized_cache_rows
        if real_basin_regime(row) and str(row.get("dte_bucket") or "") not in {"d_minus_1", "d0"}
    ]
    for dte in sorted({str(row.get("dte_bucket") or "unknown") for row in extra_cache_rows}):
        dte_rows = [row for row in extra_cache_rows if str(row.get("dte_bucket") or "unknown") == dte]
        for season_name in sorted({str(row.get("season") or "unknown") for row in dte_rows}):
            season_rows = [row for row in dte_rows if str(row.get("season") or "unknown") == season_name]
            for regime_name in sorted({real_basin_regime(row) for row in season_rows if real_basin_regime(row)}):
                regime_rows = [row for row in season_rows if real_basin_regime(row) == regime_name]
                strata.append(
                    {
                        "dte": dte,
                        "season": season_name,
                        "basin_regime": regime_name,
                        "market_days": len({str(row.get("event_date") or "") for row in regime_rows}),
                        "labeled_market_days": 0,
                        "rows": len(regime_rows),
                        "labeled_rows": 0,
                        "fallback_used": "source+station+dte+season+basin_regime",
                        "promotion_status": "materialized_outside_live_weight_windows",
                    }
                )
    promotion_blockers = [
        "local_regime_sample_days_below_runtime_gate",
        "strict_walk_forward_non_degradation_not_yet_verified",
    ]
    if not materialized:
        promotion_blockers.insert(0, "basin_regime_feature_not_materialized_in_model_member_cache")
    return {
        "version": "basin_regime_feature_set_v1",
        "read_only": True,
        "status": "materialized_sample_coverage" if materialized else "feature_contract_and_sample_coverage",
        "applied_to_runtime": False,
        "materialized_rows": len(materialized_rows),
        "materialized_market_days": len({str(row.get("event_date") or "") for row in materialized_rows}),
        "features": [
            {"name": "dte", "status": "active", "source": "created_at vs event_date"},
            {"name": "season", "status": "active", "source": "event_date month"},
            {
                "name": "basin_regime",
                "status": "active" if materialized else "contract_defined_not_materialized",
                "source": "model_member_cache.basin_regime",
                "examples": [
                    "moderate_ventilation",
                    "moderate_ventilation_heat_buildup",
                    "weak_ventilation_heat_buildup",
                    "cloud_limited_or_rain_cooled",
                    "post_cool_rebound",
                ],
            },
            {
                "name": "similar_city_climate_zone",
                "status": "fallback_defined_not_materialized",
                "examples": ["Sichuan_Basin", "humid_subtropical_basin"],
            },
        ],
        "sample_strata": strata,
        "fallback_order": [
            "source+station+dte+season+basin_regime",
            "source+station+dte+season",
            "source+station+dte",
            "source+station",
            "source+similar_city_climate_zone",
            "source_global",
            "equal_weight",
        ],
        "promotion_blockers": promotion_blockers,
        "non_degradation_metrics": list(CONTINUOUS_WEIGHT_METRICS),
    }


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    db_path = Path(args.db)
    models = tuple(part.strip() for part in str(args.models or "").split(",") if part.strip()) or DEFAULT_MODELS
    if not db_path.exists():
        return {"ok": False, "status": "db_missing", "db": str(db_path)}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=20.0)
    conn.row_factory = sqlite3.Row
    try:
        actuals = _load_actuals(
            conn,
            station_id=str(args.station_id),
            market_var=str(args.market_var),
            market_unit=str(args.market_unit),
            start_date=str(args.start_date),
            end_date=str(args.end_date),
        )
        rows_all = _model_rows(
            conn,
            actuals=actuals,
            models=models,
            loc_key=str(args.loc_key),
            market_var=str(args.market_var),
            market_unit=str(args.market_unit),
            start_date=str(args.start_date),
            end_date=str(args.end_date),
            tz_name=str(args.tz),
            peak_start_hour_local=int(args.peak_start_hour_local),
        )
        materialized_cache_rows = _model_cache_regime_rows(
            conn,
            models=models,
            loc_key=str(args.loc_key),
            market_var=str(args.market_var),
            market_unit=str(args.market_unit),
            start_date=str(args.start_date),
            end_date=str(args.end_date),
            tz_name=str(args.tz),
            peak_start_hour_local=int(args.peak_start_hour_local),
        )
    finally:
        conn.close()
    live_rows = [row for row in rows_all if str(row.get("official_updated_source") or "") == "observed"]
    historical_rows = [
        row for row in rows_all if str(row.get("official_updated_source") or "") == "open_meteo_historical_forecast"
    ]
    d1_keys = {
        (str(row.get("model") or ""), str(row.get("event_date") or ""))
        for row in live_rows
        if str(row.get("dte_bucket") or "") == "d_minus_1"
    }
    d0_pre_keys = {
        (str(row.get("model") or ""), str(row.get("event_date") or ""))
        for row in live_rows
        if str(row.get("dte_bucket") or "") == "d0" and bool(row.get("before_peak_window_start"))
    }
    d0_all_keys = {
        (str(row.get("model") or ""), str(row.get("event_date") or ""))
        for row in live_rows
        if str(row.get("dte_bucket") or "") == "d0"
    }
    live_d1 = _latest_by(d1_keys, [row for row in live_rows if row.get("dte_bucket") == "d_minus_1"])
    live_d0_pre = _latest_by(
        d0_pre_keys,
        [row for row in live_rows if row.get("dte_bucket") == "d0" and bool(row.get("before_peak_window_start"))],
    )
    live_d0_all = _latest_by(d0_all_keys, [row for row in live_rows if row.get("dte_bucket") == "d0"])
    historical_latest = _latest_by(
        {
            (str(row.get("model") or ""), str(row.get("event_date") or ""))
            for row in historical_rows
        },
        historical_rows,
    )
    model_weight_shadow = _build_model_weight_shadow(
        live_d1=live_d1,
        live_d0_pre=live_d0_pre,
        live_d0_all=live_d0_all,
        models=models,
    )
    basin_regime_feature_set = _build_basin_regime_feature_set(
        live_d1=live_d1,
        live_d0_pre=live_d0_pre,
        live_d0_all=live_d0_all,
        materialized_cache_rows=materialized_cache_rows,
    )
    return {
        "ok": True,
        "read_only": True,
        "writes_performed": False,
        "db": str(db_path),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": {
            "city": "Chengdu",
            "station_id": str(args.station_id),
            "loc_key": str(args.loc_key),
            "market_var": str(args.market_var),
            "market_unit": str(args.market_unit),
            "start_date": str(args.start_date),
            "end_date": str(args.end_date),
            "tz": str(args.tz),
            "peak_start_hour_local": int(args.peak_start_hour_local),
            "models": list(models),
        },
        "method": {
            "live_d1": "official_updated_source=observed; latest snapshot on local D-1 per model/event.",
            "live_d0_pre_peak": "official_updated_source=observed; latest D0 snapshot before local peak window start.",
            "live_d0_all_diagnostic": "latest D0 snapshot regardless of peak timing; diagnostic only.",
            "historical_reference": "official_updated_source=open_meteo_historical_forecast; not D1/D0 as-of evidence.",
            "primary_hit_metric": "top_round_label against actual_round_label for market-bucket direction; mean_round_label is secondary.",
            "anti_leakage": "Do not use historical_reference to claim D1/D0 live hit rate; do not use post-peak D0 for pre-peak decisions.",
        },
        "coverage": {
            "actual_days": len(actuals),
            "all_model_rows_joined": len(rows_all),
            "live_observed_rows_joined": len(live_rows),
            "historical_reference_rows_joined": len(historical_rows),
            "live_d1_rows": len(live_d1),
            "live_d1_market_days": len({row["event_date"] for row in live_d1}),
            "live_d0_pre_peak_rows": len(live_d0_pre),
            "live_d0_pre_peak_market_days": len({row["event_date"] for row in live_d0_pre}),
            "live_d0_all_rows": len(live_d0_all),
            "live_d0_all_market_days": len({row["event_date"] for row in live_d0_all}),
            "historical_reference_rows": len(historical_latest),
            "historical_reference_market_days": len({row["event_date"] for row in historical_latest}),
        },
        "model_weight_shadow": model_weight_shadow,
        "basin_regime_feature_set": basin_regime_feature_set,
        "live": {
            "d_minus_1": _summarize(
                live_d1,
                models=models,
                metrics=("top_round_label", "mean_round_label", "top_floor_label", "mean_floor_label"),
            ),
            "d_minus_1_trade_skill": _summarize_trade_metrics(live_d1, models=models),
            "d_minus_1_distribution_metrics": _summarize_distribution_metrics(live_d1, models=models, rule="round"),
            "d0_pre_peak": _summarize(
                live_d0_pre,
                models=models,
                metrics=("top_round_label", "mean_round_label", "top_floor_label", "mean_floor_label"),
            ),
            "d0_pre_peak_trade_skill": _summarize_trade_metrics(live_d0_pre, models=models),
            "d0_pre_peak_distribution_metrics": _summarize_distribution_metrics(live_d0_pre, models=models, rule="round"),
            "d0_all_diagnostic": _summarize(
                live_d0_all,
                models=models,
                metrics=("top_round_label", "mean_round_label", "top_floor_label", "mean_floor_label"),
            ),
            "d0_all_diagnostic_trade_skill": _summarize_trade_metrics(live_d0_all, models=models),
            "d0_all_diagnostic_distribution_metrics": _summarize_distribution_metrics(
                live_d0_all, models=models, rule="round"
            ),
        },
        "historical_reference": _history_reference_summary(historical_latest, models=models),
        "recommendation_guardrails": {
            "min_live_market_days_for_runtime_weight_change": MIN_LIVE_MARKET_DAYS_FOR_RUNTIME_WEIGHT_CHANGE,
            "min_live_market_days_for_shadow_weight": MIN_LIVE_MARKET_DAYS_FOR_SHADOW_WEIGHT,
            "current_live_samples_sufficient_for_runtime_weight_change": bool(
                model_weight_shadow.get("runtime_promotion_allowed")
            ),
            "current_shadow_output_available": bool(model_weight_shadow.get("segments")),
            "runtime_promotion_blockers": model_weight_shadow.get("runtime_promotion_blockers") or [],
            "weight_change_rule": (
                "Use hierarchical shrinkage toward equal weights; allow DTE/season/regime-specific precision only after "
                "walk-forward Brier/RPS/logloss/CRPS improve without D1/D0, hot-day, season, or basin/regime degradation."
            ),
            "qm_rule": (
                "QM/local correction may run shadow with strict created_at gates; promotion requires enough live days "
                "and non-degradation by D1, D0_pre_peak, hot_day, season/regime."
            ),
        },
        "sample_rows": {
            "live_d1": sorted(live_d1, key=lambda row: (row["event_date"], row["model"]))[-12:],
            "live_d0_pre_peak": sorted(live_d0_pre, key=lambda row: (row["event_date"], row["model"]))[-12:],
        },
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Chengdu model D1/D0 hit-rate audit")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default="")
    parser.add_argument("--station-id", default=DEFAULT_STATION_ID)
    parser.add_argument("--loc-key", default=DEFAULT_LOC_KEY)
    parser.add_argument("--market-var", default=DEFAULT_MARKET_VAR)
    parser.add_argument("--market-unit", default=DEFAULT_MARKET_UNIT)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--start-date", default="2025-06-30")
    parser.add_argument("--end-date", default="2026-06-30")
    parser.add_argument("--tz", default=DEFAULT_TZ)
    parser.add_argument("--peak-start-hour-local", type=int, default=12)
    parser.add_argument("--write-latest", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    out_raw = str(args.out or "").strip()
    if bool(args.write_latest):
        out_raw = out_raw or str(DEFAULT_OUT)
    if out_raw:
        out = Path(out_raw).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
