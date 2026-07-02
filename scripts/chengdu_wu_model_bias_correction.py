#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta, timezone, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scan_weather_market as swm  # noqa: E402

DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "chengdu_wu_model_bias_correction_latest.json"
DEFAULT_STATION_ID = "ZUUU"
DEFAULT_EVENT_SLUG = "highest-temperature-in-chengdu-on-june-9-2026"
DEFAULT_DAYS = 0
DEFAULT_MIN_WU_SAMPLES = 5
DEFAULT_CONFIGURED_MODELS = ("gfs_seamless", "icon_seamless", "ecmwf_ifs025", "nws_forecast")
DEFAULT_WU_WEATHER_URL = "https://www.wunderground.com/weather/cn/chengdu/ZUUU"
DEFAULT_WU_HISTORY_URL = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"
D0_AFTERNOON_STAGES = (
    ("early_12_14", "12:00-14:00 local", 4, 6),
    ("mid_14_16", "14:00-16:00 local", 6, 8),
    ("late_16_18", "16:00-18:00 local", 8, 10),
)


@dataclass(frozen=True)
class Actual:
    value_c: float
    source: str
    source_rank: int
    row_id: int
    created_at: str


@dataclass(frozen=True)
class ModelSample:
    model: str
    event_date: str
    values_c: Tuple[float, ...]
    loc_key: str
    created_at: str
    source: str
    official_updated_source: str


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


def _median(values: Iterable[float]) -> Optional[float]:
    vals = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _json_loads(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(str(raw))
    except Exception:
        return None


def _f_to_c(value_f: float) -> float:
    return (float(value_f) - 32.0) * 5.0 / 9.0


def _date_from_wu_local(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10]


def extract_wu_daily_summary_from_html(html_text: str) -> Dict[str, Any]:
    """Extract WU Angular SSR 30-day daily summary from a weather page.

    The WU current weather page embeds API responses in `app-root-state`.
    This parser does not call external APIs; it only reports what the page
    already contains.
    """

    match = re.search(
        r'<script\s+id=["\']app-root-state["\']\s+type=["\']application/json["\']>(.*?)</script>',
        html_text or "",
        flags=re.S | re.I,
    )
    if not match:
        return {
            "ok": False,
            "source": "wunderground_app_root_state",
            "reason": "app_root_state_missing",
            "rows": [],
        }
    try:
        state = json.loads(html.unescape(match.group(1)))
    except Exception as exc:
        return {
            "ok": False,
            "source": "wunderground_app_root_state",
            "reason": f"app_root_state_json_error:{type(exc).__name__}",
            "rows": [],
        }
    if not isinstance(state, Mapping):
        return {
            "ok": False,
            "source": "wunderground_app_root_state",
            "reason": "app_root_state_not_mapping",
            "rows": [],
        }

    found_url = ""
    body: Optional[Mapping[str, Any]] = None
    for item in state.values():
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("u") or "")
        if "api.weather.com/v3/wx/conditions/historical/dailysummary/30day" not in url:
            continue
        payload = item.get("b")
        if isinstance(payload, Mapping):
            found_url = url
            body = payload
            break

    if body is None:
        return {
            "ok": False,
            "source": "wunderground_app_root_state",
            "reason": "dailysummary_30day_missing",
            "rows": [],
        }

    valid_times = body.get("validTimeLocal")
    max_values = body.get("temperatureMax")
    min_values = body.get("temperatureMin")
    if not isinstance(valid_times, list) or not isinstance(max_values, list):
        return {
            "ok": False,
            "source": "wunderground_app_root_state",
            "reason": "dailysummary_fields_missing",
            "url": found_url,
            "rows": [],
        }

    rows: List[Dict[str, Any]] = []
    units = "F" if "units=e" in found_url else ""
    for idx, raw_date in enumerate(valid_times):
        event_date = _date_from_wu_local(raw_date)
        high_raw = max_values[idx] if idx < len(max_values) else None
        low_raw = min_values[idx] if isinstance(min_values, list) and idx < len(min_values) else None
        high_num = _to_float(high_raw)
        low_num = _to_float(low_raw)
        row: Dict[str, Any] = {
            "event_date": event_date,
            "valid_time_local": raw_date,
            "temperature_max_raw": high_num,
            "temperature_min_raw": low_num,
            "unit_raw": units or "unknown",
        }
        if high_num is not None:
            row["temperature_max_c"] = _f_to_c(high_num) if units == "F" else high_num
        if low_num is not None:
            row["temperature_min_c"] = _f_to_c(low_num) if units == "F" else low_num
        rows.append(row)

    return {
        "ok": True,
        "source": "wunderground_app_root_state",
        "url": found_url,
        "unit_raw": units or "unknown",
        "row_count": len(rows),
        "min_date": min((r["event_date"] for r in rows if r.get("event_date")), default=""),
        "max_date": max((r["event_date"] for r in rows if r.get("event_date")), default=""),
        "rows": rows,
    }
    try:
        return json.loads(str(raw))
    except Exception:
        return None


def _date_range(days: int, end_date: str) -> Tuple[str, str]:
    end = date.fromisoformat(end_date)
    start = end - timedelta(days=max(1, int(days)) - 1)
    return start.isoformat(), end.isoformat()


def _available_actual_range(conn: sqlite3.Connection, *, station_id: str) -> Tuple[str, str]:
    row = conn.execute(
        """
        SELECT MIN(event_date), MAX(event_date)
        FROM event_daily_temps
        WHERE upper(coalesce(station_id, '')) = upper(?)
          AND market_var = 'temperature_2m_max'
          AND market_unit = 'C'
          AND settlement_actual IS NOT NULL
        """,
        (station_id,),
    ).fetchone()
    if row and row[0] and row[1]:
        return str(row[0]), str(row[1])
    today = date.today().isoformat()
    return today, today


def _resolve_history_window(conn: sqlite3.Connection, args: argparse.Namespace, *, station_id: str) -> Tuple[str, str, str]:
    available_start, available_end = _available_actual_range(conn, station_id=station_id)
    requested_start = str(getattr(args, "start_date", "") or "").strip()
    requested_end = str(getattr(args, "end_date", "") or "").strip()
    requested_days = int(getattr(args, "days", 0) or 0)

    if requested_start:
        start_date = requested_start
        end_date = requested_end or available_end
        return start_date, end_date, "explicit_start_date"
    if requested_days > 0:
        end_date = requested_end or available_end
        start_date, end_date = _date_range(requested_days, end_date)
        return start_date, end_date, "explicit_days"
    if requested_end:
        return available_start, requested_end, "available_start_to_explicit_end"
    return available_start, available_end, "available_history"


def _iter_dates(start_date: str, end_date: str) -> Iterable[str]:
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _is_wu_source(value: Any) -> bool:
    return "wunderground.com/history/daily/cn/chengdu/zuuu" in str(value or "").lower()


def _actual_rank(source: str, table_name: str) -> int:
    if table_name == "event_runs" and _is_wu_source(source):
        return 4
    if _is_wu_source(source):
        return 3
    if str(source or "").strip().lower() == "open-meteo-archive":
        return 1
    return 0


def _pick_actual(existing: Optional[Actual], candidate: Actual) -> Actual:
    if existing is None:
        return candidate
    if candidate.source_rank != existing.source_rank:
        return candidate if candidate.source_rank > existing.source_rank else existing
    if candidate.created_at != existing.created_at:
        return candidate if candidate.created_at > existing.created_at else existing
    return candidate if candidate.row_id > existing.row_id else existing


def collect_actuals(conn: sqlite3.Connection, *, start_date: str, end_date: str, station_id: str) -> Dict[str, Actual]:
    actuals: Dict[str, Actual] = {}

    for row in conn.execute(
        """
        SELECT id, event_date, settlement_actual, resolution_source, created_at
        FROM event_daily_temps
        WHERE event_date >= ?
          AND event_date <= ?
          AND upper(coalesce(station_id, '')) = upper(?)
          AND market_var = 'temperature_2m_max'
          AND market_unit = 'C'
          AND settlement_actual IS NOT NULL
        ORDER BY event_date ASC, datetime(created_at) ASC, id ASC
        """,
        (start_date, end_date, station_id),
    ):
        val = _to_float(row["settlement_actual"])
        if val is None:
            continue
        source = str(row["resolution_source"] or "")
        item = Actual(
            value_c=float(val),
            source=source,
            source_rank=_actual_rank(source, "event_daily_temps"),
            row_id=int(row["id"]),
            created_at=str(row["created_at"] or ""),
        )
        event_date = str(row["event_date"])
        actuals[event_date] = _pick_actual(actuals.get(event_date), item)

    for row in conn.execute(
        """
        SELECT id, event_date, settlement_actual, resolution_source, created_at
        FROM event_runs
        WHERE event_date >= ?
          AND event_date <= ?
          AND upper(coalesce(station_id, '')) = upper(?)
          AND lower(coalesce(slug, '')) LIKE '%chengdu%'
          AND market_var = 'temperature_2m_max'
          AND market_unit = 'C'
          AND settlement_actual IS NOT NULL
        ORDER BY event_date ASC, datetime(created_at) ASC, id ASC
        """,
        (start_date, end_date, station_id),
    ):
        val = _to_float(row["settlement_actual"])
        if val is None:
            continue
        source = str(row["resolution_source"] or "")
        item = Actual(
            value_c=float(val),
            source=source,
            source_rank=_actual_rank(source, "event_runs"),
            row_id=int(row["id"]),
            created_at=str(row["created_at"] or ""),
        )
        event_date = str(row["event_date"])
        actuals[event_date] = _pick_actual(actuals.get(event_date), item)

    return actuals


def _station_loc_key(conn: sqlite3.Connection, station_id: str, slug: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT lat, lon
        FROM event_runs
        WHERE slug = ?
          AND upper(coalesce(station_id, '')) = upper(?)
          AND lat IS NOT NULL
          AND lon IS NOT NULL
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT 1
        """,
        (slug, station_id),
    ).fetchone()
    if not row:
        return None
    lat = _to_float(row["lat"])
    lon = _to_float(row["lon"])
    if lat is None or lon is None:
        return None
    return f"{lat:.4f},{lon:.4f}"


def _near_chengdu_loc_keys(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> List[str]:
    keys: List[str] = []
    for row in conn.execute(
        """
        SELECT loc_key, COUNT(*) AS c
        FROM model_member_cache
        WHERE event_date >= ?
          AND event_date <= ?
          AND market_var = 'temperature_2m_max'
          AND CAST(substr(loc_key, 1, instr(loc_key, ',') - 1) AS REAL) BETWEEN 29 AND 32
          AND CAST(substr(loc_key, instr(loc_key, ',') + 1) AS REAL) BETWEEN 102 AND 106
        GROUP BY loc_key
        ORDER BY c DESC, loc_key ASC
        """,
        (start_date, end_date),
    ):
        keys.append(str(row["loc_key"]))
    return keys


def collect_model_cache(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    loc_key: str,
) -> List[ModelSample]:
    latest: Dict[Tuple[str, str], sqlite3.Row] = {}
    for row in conn.execute(
        """
        SELECT id, model, loc_key, event_date, members_json, created_at, official_updated_source
        FROM model_member_cache
        WHERE event_date >= ?
          AND event_date <= ?
          AND loc_key = ?
          AND market_var = 'temperature_2m_max'
          AND market_unit = 'C'
        ORDER BY event_date ASC, model ASC, datetime(created_at) ASC, id ASC
        """,
        (start_date, end_date, loc_key),
    ):
        key = (str(row["model"]), str(row["event_date"]))
        prev = latest.get(key)
        if prev is None or str(row["created_at"] or "") > str(prev["created_at"] or "") or int(row["id"]) > int(prev["id"]):
            latest[key] = row

    out: List[ModelSample] = []
    for row in latest.values():
        raw = _json_loads(row["members_json"])
        if not isinstance(raw, list):
            continue
        values = tuple(float(v) for v in (_to_float(x) for x in raw) if v is not None)
        if not values:
            continue
        out.append(
            ModelSample(
                model=str(row["model"]),
                event_date=str(row["event_date"]),
                values_c=values,
                loc_key=str(row["loc_key"]),
                created_at=str(row["created_at"] or ""),
                source="model_member_cache",
                official_updated_source=str(row["official_updated_source"] or ""),
            )
        )
    return sorted(out, key=lambda x: (x.event_date, x.model))


def collect_chengdu_model_provenance(conn: sqlite3.Connection, *, scan_limit: int = 300) -> Dict[str, Any]:
    summary: Dict[str, Dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT id, event_date, slug, payload_json, created_at
        FROM event_runs
        WHERE lower(coalesce(slug, '')) LIKE '%chengdu%'
          AND payload_json IS NOT NULL
          AND payload_json LIKE '%model_provenance%'
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        (max(1, int(scan_limit)),),
    ):
        payload = _json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        provenance = payload.get("model_provenance")
        if not isinstance(provenance, Mapping):
            continue
        for model, meta in provenance.items():
            if not isinstance(meta, Mapping):
                continue
            status = str(meta.get("status") or "unknown")
            error = str(meta.get("error") or "")
            entry = summary.setdefault(
                str(model),
                {
                    "seen": 0,
                    "status_counts": Counter(),
                    "error_counts": Counter(),
                    "latest_status": "",
                    "latest_error": "",
                    "latest_event_date": "",
                    "latest_created_at": "",
                    "latest_slug": "",
                },
            )
            entry["seen"] += 1
            entry["status_counts"][status] += 1
            if error:
                entry["error_counts"][error] += 1
            if str(row["created_at"] or "") >= str(entry.get("latest_created_at") or ""):
                entry["latest_status"] = status
                entry["latest_error"] = error
                entry["latest_event_date"] = str(row["event_date"] or "")
                entry["latest_created_at"] = str(row["created_at"] or "")
                entry["latest_slug"] = str(row["slug"] or "")

    out: Dict[str, Any] = {}
    for model, entry in sorted(summary.items()):
        out[model] = {
            "seen": int(entry["seen"]),
            "scan_limit": max(1, int(scan_limit)),
            "status_counts": dict(entry["status_counts"]),
            "error_counts": dict(entry["error_counts"]),
            "latest_status": entry.get("latest_status") or "",
            "latest_error": entry.get("latest_error") or "",
            "latest_event_date": entry.get("latest_event_date") or "",
            "latest_created_at": entry.get("latest_created_at") or "",
            "latest_slug": entry.get("latest_slug") or "",
        }
    return out


def _bias_metrics(pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not pairs:
        return {
            "n": 0,
            "mean_bias_c": None,
            "median_bias_c": None,
            "recommended_temp_shift_c": None,
            "mae_c": None,
            "rmse_c": None,
            "hot_count": 0,
            "cold_count": 0,
            "flat_count": 0,
        }
    residuals = [float(p["bias_c"]) for p in pairs]
    abs_res = [abs(x) for x in residuals]
    sq = [x * x for x in residuals]
    ordered = sorted(residuals)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0
    mean = sum(residuals) / n
    return {
        "n": n,
        "mean_bias_c": mean,
        "median_bias_c": median,
        "recommended_temp_shift_c": -mean,
        "mae_c": sum(abs_res) / n,
        "rmse_c": math.sqrt(sum(sq) / n),
        "hot_count": sum(1 for x in residuals if x > 1e-9),
        "cold_count": sum(1 for x in residuals if x < -1e-9),
        "flat_count": sum(1 for x in residuals if abs(x) <= 1e-9),
    }


def compute_bias(
    actuals: Mapping[str, Actual],
    samples: Iterable[ModelSample],
    *,
    min_rank: int,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    by_model: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    all_pairs: List[Dict[str, Any]] = []
    for sample in samples:
        actual = actuals.get(sample.event_date)
        if actual is None or actual.source_rank < min_rank:
            continue
        forecast_mean = sum(sample.values_c) / len(sample.values_c)
        row = {
            "event_date": sample.event_date,
            "model": sample.model,
            "forecast_mean_c": forecast_mean,
            "forecast_min_c": min(sample.values_c),
            "forecast_max_c": max(sample.values_c),
            "member_count": len(sample.values_c),
            "actual_c": actual.value_c,
            "actual_source": actual.source,
            "actual_source_rank": actual.source_rank,
            "bias_c": forecast_mean - actual.value_c,
            "model_created_at": sample.created_at,
            "model_source": sample.source,
            "model_official_updated_source": sample.official_updated_source,
        }
        by_model[sample.model].append(row)
        all_pairs.append(row)

    stats = {model: _bias_metrics(rows) for model, rows in sorted(by_model.items())}
    return stats, sorted(all_pairs, key=lambda x: (x["event_date"], x["model"]))


def _first_payload_float(payload: Mapping[str, Any], features: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        val = _to_float(features.get(key))
        if val is not None:
            return val
        val = _to_float(payload.get(key))
        if val is not None:
            return val
    return None


def _soft_peak_candidates(payload: Mapping[str, Any], features: Mapping[str, Any]) -> Dict[str, float]:
    candidates: Dict[str, float] = {}
    for name, keys in {
        "peak_estimator": ("peak_estimator_mu_c",),
        "soft_forecast": ("soft_forecast_peak_c",),
        "weather_com": ("weather_com_forecast_peak_c",),
        "twc_forecast": ("twc_forecast_peak_c",),
        "wu_forecast": ("wu_forecast_peak_c",),
    }.items():
        val = _first_payload_float(payload, features, *keys)
        if val is not None:
            candidates[name] = float(val)
    weather_com_peak = payload.get("weather_com_forecast_peak")
    if isinstance(weather_com_peak, Mapping):
        val = _to_float(
            weather_com_peak.get("peak_c")
            if weather_com_peak.get("peak_c") is not None
            else weather_com_peak.get("value_c")
        )
        if val is not None:
            candidates.setdefault("weather_com", float(val))
    return candidates


def _observed_reference_c(
    *,
    actual: Optional[Actual],
    payload: Mapping[str, Any],
    features: Mapping[str, Any],
) -> Tuple[Optional[float], str, bool]:
    if actual is not None:
        return float(actual.value_c), actual.source, True
    candidates: List[Tuple[float, str]] = []
    for key in (
        "observed_today_max_c",
        "zuuu_metar_raw_awc_today_max_c",
        "wu_twc_observed_intraday_today_max_c",
        "observed_temp_c",
        "metar_temperature_c",
    ):
        val = _first_payload_float(payload, features, key)
        if val is not None:
            candidates.append((float(val), key))
    if not candidates:
        return None, "", False
    value, key = max(candidates, key=lambda item: item[0])
    source = str(
        features.get("observed_today_max_source")
        or payload.get("observed_today_max_source")
        or key
    )
    return float(value), source, False


def _high_temp_day_tier(value_c: float) -> str:
    if value_c >= 33.0:
        return "extreme_33_plus"
    if value_c >= 32.0:
        return "very_hot_32_32_9"
    if value_c >= 30.0:
        return "hot_30_31_9"
    return "below_30"


def _ratio(num: int, den: int) -> Optional[float]:
    return (float(num) / float(den)) if den else None


def _summarize_snapshot_records(records: List[Mapping[str, Any]]) -> Dict[str, Any]:
    checked_count = len(records)
    underestimated_count = sum(1 for row in records if row.get("underestimated_sources"))
    all_soft_count = sum(1 for row in records if bool(row.get("all_soft_sources_underestimated")))
    worst_gaps = [
        float(row["worst_source_underestimate_gap_c"])
        for row in records
        if _to_float(row.get("worst_source_underestimate_gap_c")) is not None
    ]
    conservative_gaps = [
        float(row["conservative_all_source_gap_c"])
        for row in records
        if _to_float(row.get("conservative_all_source_gap_c")) is not None
    ]
    references = [
        float(row["reference_actual_or_observed_max_c"])
        for row in records
        if _to_float(row.get("reference_actual_or_observed_max_c")) is not None
    ]
    return {
        "checked_count": checked_count,
        "underestimated_count": underestimated_count,
        "underestimated_share": _ratio(underestimated_count, checked_count),
        "all_soft_sources_underestimated_count": all_soft_count,
        "all_soft_sources_underestimated_share": _ratio(all_soft_count, checked_count),
        "distinct_days": len({str(row.get("event_date") or "") for row in records if row.get("event_date")}),
        "mean_reference_actual_or_observed_max_c": _mean(references),
        "mean_worst_source_gap_c": _mean(worst_gaps),
        "median_worst_source_gap_c": _median(worst_gaps),
        "max_worst_source_gap_c": max(worst_gaps) if worst_gaps else None,
        "mean_conservative_all_source_gap_c": _mean(conservative_gaps),
        "median_conservative_all_source_gap_c": _median(conservative_gaps),
    }


def _summarize_source_gap_records(records: List[Mapping[str, Any]], *, threshold_c: float) -> Dict[str, Any]:
    checked_count = len(records)
    underestimated_count = sum(1 for row in records if float(row.get("gap_c") or 0.0) >= float(threshold_c))
    gaps = [float(row["gap_c"]) for row in records if _to_float(row.get("gap_c")) is not None]
    return {
        "checked_count": checked_count,
        "underestimated_count": underestimated_count,
        "underestimated_share": _ratio(underestimated_count, checked_count),
        "mean_gap_c": _mean(gaps),
        "median_gap_c": _median(gaps),
        "max_gap_c": max(gaps) if gaps else None,
        "min_gap_c": min(gaps) if gaps else None,
        "threshold_c": float(threshold_c),
        "distinct_days": len({str(row.get("event_date") or "") for row in records if row.get("event_date")}),
    }


def _group_snapshot_records(records: List[Mapping[str, Any]], key: str) -> Dict[str, Any]:
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[str(row.get(key) or "unknown")].append(row)
    return {name: _summarize_snapshot_records(items) for name, items in sorted(grouped.items())}


def _group_source_records(records: List[Mapping[str, Any]], key: str, *, threshold_c: float) -> Dict[str, Any]:
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[str(row.get(key) or "unknown")].append(row)
    return {name: _summarize_source_gap_records(items, threshold_c=threshold_c) for name, items in sorted(grouped.items())}


def _d0_underestimation_shadow_gate(
    checked_rows: List[Mapping[str, Any]],
    source_gap_rows: List[Mapping[str, Any]],
    *,
    threshold_c: float,
) -> Dict[str, Any]:
    min_distinct_days = 10
    min_stage_snapshots = 20
    min_source_observations = 10
    min_source_underestimated_share = 0.55
    min_source_mean_gap_c = max(1.0, float(threshold_c) * 0.75)
    distinct_days = len({str(row.get("event_date") or "") for row in checked_rows if row.get("event_date")})
    overall = _summarize_snapshot_records(checked_rows)
    by_source = _group_source_records(source_gap_rows, "soft_source", threshold_c=threshold_c)
    stable_sources = {}
    for source, stats in by_source.items():
        checked = int(stats.get("checked_count") or 0)
        share = _to_float(stats.get("underestimated_share")) or 0.0
        mean_gap = _to_float(stats.get("mean_gap_c")) or 0.0
        if checked < min_source_observations:
            continue
        if share < min_source_underestimated_share or mean_gap < min_source_mean_gap_c:
            continue
        stable_sources[source] = {
            "checked_count": checked,
            "underestimated_share": share,
            "mean_gap_c": mean_gap,
            "median_gap_c": stats.get("median_gap_c"),
            "candidate_peak_uplift_c": min(2.0, max(0.0, float(stats.get("median_gap_c") or mean_gap))),
        }

    if distinct_days < min_distinct_days or len(checked_rows) < min_stage_snapshots:
        status = "insufficient_samples"
        reason = "Need more D0 afternoon stage snapshots before creating dry-run shadow parameters."
        recommended_action = "keep_auditing_no_shadow_params"
    elif stable_sources:
        status = "dry_run_shadow_candidate_requires_confirmation"
        reason = "At least one soft peak source shows repeated D0 afternoon high-temperature underestimation."
        recommended_action = "prepare_dry_run_shadow_params_for_review"
    else:
        status = "not_stable"
        reason = "Expanded D0 afternoon slice does not show a stable source-level underestimation bias."
        recommended_action = "do_not_enter_dry_run_shadow"

    return {
        "status": status,
        "reason": reason,
        "recommended_action": recommended_action,
        "runtime_probability_write": False,
        "criteria": {
            "min_distinct_days": min_distinct_days,
            "min_stage_snapshots": min_stage_snapshots,
            "min_source_observations": min_source_observations,
            "min_source_underestimated_share": min_source_underestimated_share,
            "min_source_mean_gap_c": min_source_mean_gap_c,
        },
        "observed": {
            "distinct_days": distinct_days,
            "checked_stage_snapshots": len(checked_rows),
            "overall": overall,
        },
        "stable_sources": stable_sources,
    }


def collect_d0_afternoon_high_temp_underestimation(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    actuals: Mapping[str, Actual],
    threshold_c: float = 1.5,
    min_observed_c: float = 30.0,
) -> Dict[str, Any]:
    skipped: Counter[str] = Counter()
    event_rows = conn.execute(
        """
        SELECT DISTINCT slug, event_date
        FROM event_runs
        WHERE event_date >= ?
          AND event_date <= ?
          AND slug LIKE 'highest-temperature-in-chengdu-on-%'
        ORDER BY event_date ASC, slug ASC
        """,
        (start_date, end_date),
    ).fetchall()
    rows: List[Tuple[str, str, str, sqlite3.Row]] = []
    candidate_snapshot_source_counts: Counter[str] = Counter()
    for event in event_rows:
        event_date = str(event["event_date"] or "")[:10]
        try:
            day = date.fromisoformat(event_date)
        except ValueError:
            skipped["event_date_invalid"] += 1
            continue
        for stage_key, stage_label, start_hour_utc, end_hour_utc in D0_AFTERNOON_STAGES:
            # Chengdu is UTC+8; D0 local 12:00-18:00 maps to UTC 04:00-10:00.
            start_utc = datetime(day.year, day.month, day.day, start_hour_utc, 0, 0, tzinfo=timezone.utc).isoformat()
            end_utc = datetime(day.year, day.month, day.day, end_hour_utc, 0, 0, tzinfo=timezone.utc).isoformat()
            row = conn.execute(
                """
                SELECT id, run_date, event_date, slug, fact_as_of, created_at, fact_json
                FROM event_fact_history
                WHERE slug = ?
                  AND event_date = ?
                  AND fact_as_of >= ?
                  AND fact_as_of < ?
                ORDER BY fact_as_of DESC, id DESC
                LIMIT 1
                """,
                (str(event["slug"] or ""), event_date, start_utc, end_utc),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT
                        id,
                        run_date,
                        event_date,
                        slug,
                        created_at AS fact_as_of,
                        created_at,
                        payload_json AS fact_json
                    FROM event_runs
                    WHERE slug = ?
                      AND event_date = ?
                      AND created_at >= ?
                      AND created_at < ?
                      AND payload_json IS NOT NULL
                      AND payload_json LIKE '%weather_likelihood_features%'
                    ORDER BY datetime(created_at) DESC, id DESC
                    LIMIT 1
                    """,
                    (str(event["slug"] or ""), event_date, start_utc, end_utc),
                ).fetchone()
                if row is None:
                    skipped["d0_afternoon_fact_missing"] += 1
                    skipped[f"{stage_key}_fact_missing"] += 1
                    continue
                snapshot_source = "event_runs_payload_json"
            else:
                snapshot_source = "event_fact_history"
            candidate_snapshot_source_counts[snapshot_source] += 1
            rows.append((stage_key, stage_label, snapshot_source, row))
    checked: List[Dict[str, Any]] = []
    underestimated: List[Dict[str, Any]] = []
    source_gap_rows: List[Dict[str, Any]] = []
    gaps: List[float] = []
    for stage_key, stage_label, snapshot_source, row in rows:
        payload = _json_loads(row["fact_json"])
        if not isinstance(payload, Mapping):
            skipped["fact_json_invalid"] += 1
            continue
        features = payload.get("weather_likelihood_features_compact")
        if not isinstance(features, Mapping):
            features = payload.get("weather_likelihood_features")
        if not isinstance(features, Mapping):
            features = {}
        soft_peaks = _soft_peak_candidates(payload, features)
        if not soft_peaks:
            skipped["soft_peak_missing"] += 1
            continue
        event_date = str(row["event_date"] or "")[:10]
        actual = actuals.get(event_date)
        reference_c, reference_source, settlement_actual_available = _observed_reference_c(
            actual=actual,
            payload=payload,
            features=features,
        )
        if reference_c is None:
            skipped["reference_temperature_missing"] += 1
            continue
        if float(reference_c) < float(min_observed_c):
            skipped["below_min_observed_c"] += 1
            continue
        soft_peak_max_c = max(soft_peaks.values())
        soft_peak_min_c = min(soft_peaks.values())
        conservative_gap_c = float(reference_c) - float(soft_peak_max_c)
        worst_source_gap_c = float(reference_c) - float(soft_peak_min_c)
        source_gaps = {
            key: float(reference_c) - float(value)
            for key, value in soft_peaks.items()
        }
        underestimated_sources = {
            key: round(float(gap), 4)
            for key, gap in sorted(source_gaps.items())
            if float(gap) >= float(threshold_c)
        }
        row_out = {
            "event_date": event_date,
            "slug": str(row["slug"] or ""),
            "snapshot_id": int(row["id"]),
            "snapshot_created_at_local": str(row["created_at"] or ""),
            "fact_as_of": str(row["fact_as_of"] or ""),
            "snapshot_source": snapshot_source,
            "afternoon_stage": stage_key,
            "afternoon_stage_label": stage_label,
            "reference_actual_or_observed_max_c": round(float(reference_c), 4),
            "reference_source": reference_source,
            "settlement_actual_available": bool(settlement_actual_available),
            "high_temp_day_tier": _high_temp_day_tier(float(reference_c)),
            "soft_peak_max_c": round(float(soft_peak_max_c), 4),
            "soft_peak_min_c": round(float(soft_peak_min_c), 4),
            "soft_peak_candidates_c": {key: round(float(val), 4) for key, val in sorted(soft_peaks.items())},
            "source_gaps_c": {key: round(float(gap), 4) for key, gap in sorted(source_gaps.items())},
            "underestimate_gap_c": round(float(worst_source_gap_c), 4),
            "conservative_all_source_gap_c": round(float(conservative_gap_c), 4),
            "worst_source_underestimate_gap_c": round(float(worst_source_gap_c), 4),
            "underestimated_sources": underestimated_sources,
            "all_soft_sources_underestimated": conservative_gap_c >= float(threshold_c),
            "threshold_c": float(threshold_c),
            "observed_today_max_source": str(
                features.get("observed_today_max_source")
                or payload.get("observed_today_max_source")
                or ""
            ),
            "temperature_momentum_state": str(features.get("temperature_momentum_realization_state") or ""),
        }
        checked.append(row_out)
        for source, gap in sorted(source_gaps.items()):
            source_gap_rows.append(
                {
                    "event_date": event_date,
                    "afternoon_stage": stage_key,
                    "high_temp_day_tier": row_out["high_temp_day_tier"],
                    "temperature_momentum_state": row_out["temperature_momentum_state"] or "unknown",
                    "soft_source": source,
                    "gap_c": round(float(gap), 4),
                    "soft_peak_c": round(float(soft_peaks[source]), 4),
                    "reference_actual_or_observed_max_c": row_out["reference_actual_or_observed_max_c"],
                    "underestimated": float(gap) >= float(threshold_c),
                }
            )
        if underestimated_sources:
            underestimated.append(row_out)
            gaps.append(float(worst_source_gap_c))
        else:
            skipped["gap_below_threshold"] += 1
    return {
        "available": True,
        "version": "d0_afternoon_high_temp_underestimation_v1",
        "definition": (
            "D0 afternoon Chengdu snapshots where at least one soft peak forecast or peak estimator "
            "undershoots settlement/observed max by threshold_c. conservative_all_source_gap_c "
            "separately shows whether even the highest soft peak was too low."
        ),
        "scope": "chengdu_d0_local_afternoon_12_18_temperature_2m_max",
        "read_only": True,
        "start_date": start_date,
        "end_date": end_date,
        "afternoon_stages": [
            {"key": key, "label": label}
            for key, label, _start_hour_utc, _end_hour_utc in D0_AFTERNOON_STAGES
        ],
        "threshold_c": float(threshold_c),
        "min_observed_c": float(min_observed_c),
        "checked_snapshot_events": len(checked),
        "checked_stage_snapshots": len(checked),
        "checked_distinct_days": len({row["event_date"] for row in checked}),
        "snapshot_source_counts": dict(Counter(row["snapshot_source"] for row in checked)),
        "candidate_snapshot_source_counts": dict(candidate_snapshot_source_counts),
        "underestimated_count": len(underestimated),
        "underestimated_share": (len(underestimated) / len(checked)) if checked else None,
        "gap_summary": {
            "n": len(gaps),
            "mean_gap_c": _mean(gaps),
            "median_gap_c": _median(gaps) if gaps else None,
            "max_gap_c": max(gaps) if gaps else None,
        },
        "strata": {
            "overall": _summarize_snapshot_records(checked),
            "by_afternoon_stage": _group_snapshot_records(checked, "afternoon_stage"),
            "by_high_temp_day_tier": _group_snapshot_records(checked, "high_temp_day_tier"),
            "by_temperature_momentum_state": _group_snapshot_records(checked, "temperature_momentum_state"),
            "by_soft_source": _group_source_records(source_gap_rows, "soft_source", threshold_c=threshold_c),
            "by_afternoon_stage_and_soft_source": _group_source_records(
                [
                    {**row, "stage_source": f"{row.get('afternoon_stage') or 'unknown'}|{row.get('soft_source') or 'unknown'}"}
                    for row in source_gap_rows
                ],
                "stage_source",
                threshold_c=threshold_c,
            ),
            "by_high_temp_day_tier_and_soft_source": _group_source_records(
                [
                    {**row, "tier_source": f"{row.get('high_temp_day_tier') or 'unknown'}|{row.get('soft_source') or 'unknown'}"}
                    for row in source_gap_rows
                ],
                "tier_source",
                threshold_c=threshold_c,
            ),
            "by_temperature_momentum_state_and_soft_source": _group_source_records(
                [
                    {**row, "state_source": f"{row.get('temperature_momentum_state') or 'unknown'}|{row.get('soft_source') or 'unknown'}"}
                    for row in source_gap_rows
                ],
                "state_source",
                threshold_c=threshold_c,
            ),
        },
        "source_gap_rows": source_gap_rows,
        "shadow_parameter_gate": _d0_underestimation_shadow_gate(
            checked,
            source_gap_rows,
            threshold_c=threshold_c,
        ),
        "rows": underestimated,
        "checked_rows": checked,
        "skipped": dict(skipped),
        "calibration_note": (
            "This is an intraday residual audit slice only. It should inform D0-afternoon high-temperature "
            "underestimation diagnostics, not directly write runtime parameters without a confirmed dry-run gate."
        ),
    }


def _weight_multipliers(stats: Mapping[str, Mapping[str, Any]]) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for model, row in stats.items():
        rmse = _to_float(row.get("rmse_c"))
        n = int(row.get("n") or 0)
        if rmse is None or n <= 0:
            continue
        scores[model] = 1.0 / max(0.25, rmse)
    if not scores:
        return {}
    mean_score = sum(scores.values()) / len(scores)
    return {m: max(0.25, min(2.5, score / mean_score)) for m, score in scores.items()}


def _sample_quality(
    stats: Mapping[str, Mapping[str, Any]],
    *,
    min_wu_samples: int,
    source_label: str,
) -> Dict[str, Any]:
    counts = {model: int(row.get("n") or 0) for model, row in stats.items()}
    min_count = min(counts.values()) if counts else 0
    if min_count >= 60:
        tier = "high"
    elif min_count >= 30:
        tier = "medium"
    elif min_count >= max(1, int(min_wu_samples)):
        tier = "low"
    else:
        tier = "insufficient"
    return {
        "tier": tier,
        "min_model_sample_count": min_count,
        "counts_by_model": counts,
        "note": (
            f"Temperature shifts are usable as a Chengdu-only {source_label} dry-run prior, but should not be applied broadly without confirmation."
            if tier in {"low", "medium"}
            else (
                f"Not enough {source_label}/model overlap to apply this correction."
                if tier == "insufficient"
                else f"{source_label}/model overlap is broad enough for a station-scoped correction candidate."
            )
        ),
    }


def build_wu_page_evidence(args: argparse.Namespace, actuals: Mapping[str, Actual]) -> Dict[str, Any]:
    raw_path = str(getattr(args, "wu_weather_html", "") or "").strip()
    if not raw_path:
        return {
            "requested": False,
            "ok": False,
            "reason": "wu_weather_html_not_provided",
            "weather_url": DEFAULT_WU_WEATHER_URL,
        }
    html_path = Path(raw_path)
    if not html_path.exists():
        return {
            "requested": True,
            "ok": False,
            "reason": "wu_weather_html_missing",
            "path": str(html_path),
            "weather_url": DEFAULT_WU_WEATHER_URL,
        }
    if not html_path.is_file():
        return {
            "requested": True,
            "ok": False,
            "reason": "wu_weather_html_not_file",
            "path": str(html_path),
            "weather_url": DEFAULT_WU_WEATHER_URL,
        }
    parsed = extract_wu_daily_summary_from_html(html_path.read_text(encoding="utf-8", errors="ignore"))
    comparisons: List[Dict[str, Any]] = []
    matched = 0
    wu_actual_matches = 0
    fallback_matches = 0
    max_abs_delta = None
    for row in parsed.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        event_date = str(row.get("event_date") or "")
        high_c = _to_float(row.get("temperature_max_c"))
        actual = actuals.get(event_date)
        if high_c is None or actual is None:
            continue
        delta = high_c - actual.value_c
        matched += 1
        if actual.source_rank >= 3:
            wu_actual_matches += 1
        else:
            fallback_matches += 1
        max_abs_delta = abs(delta) if max_abs_delta is None else max(float(max_abs_delta), abs(delta))
        comparisons.append(
            {
                "event_date": event_date,
                "page_temperature_max_c": high_c,
                "local_actual_c": actual.value_c,
                "local_actual_source": actual.source,
                "local_actual_source_rank": actual.source_rank,
                "delta_page_minus_local_c": delta,
            }
        )
    parsed["comparison_to_local_actuals"] = {
        "matched_days": matched,
        "matched_wu_actual_days": wu_actual_matches,
        "matched_fallback_days": fallback_matches,
        "max_abs_delta_c": max_abs_delta,
        "rows": comparisons,
        "note": (
            "WU current weather page SSR provides a 30-day daily summary; it is useful evidence, "
            "but it is not the same as the per-day history table hourly extreme used for final settlement."
        ),
    }
    parsed["requested"] = True
    parsed["path"] = str(html_path)
    parsed["weather_url"] = DEFAULT_WU_WEATHER_URL
    return parsed


def build_wu_backfill_gap(
    *,
    actuals: Mapping[str, Actual],
    start_date: str,
    end_date: str,
    station_id: str,
) -> Dict[str, Any]:
    strong_dates: List[str] = []
    fallback_dates: List[str] = []
    missing_dates: List[str] = []
    fallback_rows: List[Dict[str, Any]] = []
    for event_date in _iter_dates(start_date, end_date):
        actual = actuals.get(event_date)
        if actual is None:
            missing_dates.append(event_date)
            continue
        if actual.source_rank >= 3:
            strong_dates.append(event_date)
            continue
        fallback_dates.append(event_date)
        fallback_rows.append(
            {
                "event_date": event_date,
                "current_value_c": actual.value_c,
                "current_source": actual.source,
                "current_source_rank": actual.source_rank,
                "row_id": actual.row_id,
            }
        )

    affected_dates = fallback_dates + missing_dates
    return {
        "needed": bool(affected_dates),
        "station_id": station_id,
        "scope": "chengdu_zuuu_temperature_2m_max_only",
        "strong_wu_count": len(strong_dates),
        "fallback_count": len(fallback_dates),
        "missing_count": len(missing_dates),
        "affected_count": len(affected_dates),
        "strong_wu_dates": strong_dates,
        "fallback_dates": fallback_dates,
        "missing_dates": missing_dates,
        "fallback_rows": fallback_rows,
        "history_url": DEFAULT_WU_HISTORY_URL,
        "candidate_dry_run_command": (
            ".venv/bin/python scripts/backfill_chengdu_wu_settlement_history.py "
            "--backfill-mode daily_temps --start-date "
            f"{start_date} --end-date {end_date} --city chengdu --station-id {station_id} "
            "--strict-wu-history --no-preview"
        ),
        "candidate_apply_requires_confirmation": True,
        "apply_confirm_phrase": "APPLY_CHENGDU_HISTORY_SETTLEMENTS",
        "note": (
            "These dates are not currently backed by a strong WU history source in the local facts. "
            "Do not use fallback rows as WU-only calibration evidence without an explicit WU refresh/apply confirmation."
        ),
    }


def overlay_actuals_from_wu_preview(
    actuals: Mapping[str, Actual],
    preview_path: str,
) -> Tuple[Dict[str, Actual], Dict[str, Any]]:
    out = dict(actuals)
    raw_path = str(preview_path or "").strip()
    meta: Dict[str, Any] = {
        "requested": bool(raw_path),
        "path": raw_path,
        "applied_count": 0,
        "skipped_count": 0,
        "source": "strict_wu_preview_report",
    }
    if not raw_path:
        return out, meta
    path = Path(raw_path)
    if not path.exists():
        meta["ok"] = False
        meta["reason"] = "preview_report_missing"
        return out, meta
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        meta["ok"] = False
        meta["reason"] = f"preview_report_read_error:{exc.__class__.__name__}"
        return out, meta
    rows = report.get("rows") if isinstance(report, Mapping) else None
    if not isinstance(rows, list):
        meta["ok"] = False
        meta["reason"] = "preview_rows_missing"
        return out, meta
    generated_at = str(report.get("generated_at") or _utc_now()) if isinstance(report, Mapping) else _utc_now()
    examples: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, Mapping):
            meta["skipped_count"] += 1
            continue
        if str(row.get("action") or "") != "ready_apply":
            meta["skipped_count"] += 1
            continue
        event_date = str(row.get("event_date") or "").strip()
        value = _to_float(row.get("candidate_settlement_actual"))
        if not event_date or value is None:
            meta["skipped_count"] += 1
            continue
        fetch_meta = row.get("fetch_meta") if isinstance(row.get("fetch_meta"), Mapping) else {}
        method = str(fetch_meta.get("method") or "weather_com_history_api")
        source = str(fetch_meta.get("source_url") or DEFAULT_WU_HISTORY_URL)
        out[event_date] = Actual(
            value_c=float(value),
            source=f"{source}#{method}",
            source_rank=5,
            row_id=-idx - 1,
            created_at=generated_at,
        )
        meta["applied_count"] += 1
        if len(examples) < 5:
            examples.append({"event_date": event_date, "value_c": float(value), "method": method})
    meta["ok"] = True
    meta["reason"] = "applied"
    meta["examples"] = examples
    return out, meta


def build_wu_history_coverage(
    conn: sqlite3.Connection,
    *,
    actuals: Mapping[str, Actual],
    start_date: str,
    end_date: str,
    station_id: str,
) -> Dict[str, Any]:
    daily_rows: List[Dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT id, event_date, settlement_actual, resolution_source, created_at
        FROM event_daily_temps
        WHERE event_date >= ?
          AND event_date <= ?
          AND upper(coalesce(station_id, '')) = upper(?)
          AND lower(coalesce(resolution_source, '')) LIKE '%wunderground.com/history/daily/cn/chengdu/zuuu%'
        ORDER BY event_date ASC, datetime(created_at) ASC, id ASC
        """,
        (start_date, end_date, station_id),
    ):
        daily_rows.append(
            {
                "id": int(row["id"]),
                "event_date": str(row["event_date"]),
                "settlement_actual_c": _to_float(row["settlement_actual"]),
                "resolution_source": str(row["resolution_source"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
        )

    observation_rows: List[Dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT id, event_date, observed_at, published_at, value, unit, resolution_source, created_at
        FROM station_observations
        WHERE event_date >= ?
          AND event_date <= ?
          AND upper(coalesce(station_id, '')) = upper(?)
          AND lower(coalesce(source_kind, '')) = 'wunderground_history'
        ORDER BY event_date ASC, datetime(created_at) ASC, id ASC
        """,
        (start_date, end_date, station_id),
    ):
        observation_rows.append(
            {
                "id": int(row["id"]),
                "event_date": str(row["event_date"]),
                "observed_at": str(row["observed_at"] or ""),
                "published_at": str(row["published_at"] or ""),
                "value_raw": _to_float(row["value"]),
                "unit_raw": str(row["unit"] or ""),
                "resolution_source": str(row["resolution_source"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
        )

    actual_wu_dates = sorted(d for d, a in actuals.items() if a.source_rank >= 3)
    all_dates = list(_iter_dates(start_date, end_date))
    missing_wu_dates = [d for d in all_dates if d not in set(actual_wu_dates)]
    return {
        "history_url": DEFAULT_WU_HISTORY_URL,
        "weather_url": DEFAULT_WU_WEATHER_URL,
        "window": {"start_date": start_date, "end_date": end_date, "days": len(all_dates)},
        "actual_wu_days": len(actual_wu_dates),
        "actual_wu_coverage_ratio": (len(actual_wu_dates) / len(all_dates)) if all_dates else 0.0,
        "daily_temps_wu_rows": len(daily_rows),
        "station_observation_wu_rows": len(observation_rows),
        "missing_wu_days": len(missing_wu_dates),
        "missing_wu_dates": missing_wu_dates,
        "daily_temps_rows": daily_rows,
        "station_observation_rows": observation_rows,
        "status": "complete" if len(actual_wu_dates) == len(all_dates) else "partial",
        "note": (
            "Only days backed by WU history rows are treated as WU-strong calibration evidence; "
            "open-meteo rows remain fallback and do not prove WU history coverage."
        ),
    }


def _bucket_for_value(rows: List[Dict[str, Any]], value: float, rounding_rule: Optional[str]) -> str:
    for row in rows:
        label = str(row.get("outcome") or "")
        if not label:
            continue
        try:
            spec = swm.parse_outcome_label(label)
        except ValueError:
            continue
        adjusted = swm.apply_rounding(value, rounding_rule, spec.unit)
        if spec.kind == "eq" and abs(adjusted - spec.k) <= 1e-9:
            return label
        if spec.kind == "le" and adjusted <= spec.k:
            return label
        if spec.kind == "ge" and adjusted >= spec.k:
            return label
        if spec.kind == "range" and spec.k2 is not None and spec.k <= adjusted <= spec.k2:
            return label
    return ""


def latest_event_payload(conn: sqlite3.Connection, slug: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, event_date, created_at, rounding_rule, payload_json
        FROM event_runs
        WHERE slug = ?
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT 1
        """,
        (slug,),
    ).fetchone()
    if not row:
        return None
    payload = _json_loads(row["payload_json"])
    if not isinstance(payload, dict):
        return None
    payload["_event_run"] = {
        "id": int(row["id"]),
        "event_date": str(row["event_date"]),
        "created_at": str(row["created_at"] or ""),
        "rounding_rule": str(row["rounding_rule"] or ""),
    }
    return payload


def corrected_bucket_preview(
    payload: Mapping[str, Any],
    *,
    shifts: Mapping[str, Any],
) -> Dict[str, Any]:
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    labels = [str(r.get("outcome") or "") for r in rows if isinstance(r, Mapping)]
    model_members = payload.get("model_members") if isinstance(payload.get("model_members"), Mapping) else {}
    rounding_rule = str((payload.get("_event_run") or {}).get("rounding_rule") or "whole_degrees")
    raw_counts: Counter[str] = Counter()
    corrected_counts: Counter[str] = Counter()
    per_model: Dict[str, Dict[str, Any]] = {}
    total_raw = 0
    total_corrected = 0
    for model, raw_values in sorted(model_members.items()):
        if not isinstance(raw_values, list):
            continue
        shift = _to_float((shifts.get(model) or {}).get("recommended_temp_shift_c") if isinstance(shifts.get(model), Mapping) else None)
        if shift is None:
            shift = 0.0
        model_raw: Counter[str] = Counter()
        model_corrected: Counter[str] = Counter()
        for item in raw_values:
            val = _to_float(item)
            if val is None:
                continue
            raw_label = _bucket_for_value(rows, val, rounding_rule)
            corrected_label = _bucket_for_value(rows, val + shift, rounding_rule)
            if raw_label:
                raw_counts[raw_label] += 1
                model_raw[raw_label] += 1
                total_raw += 1
            if corrected_label:
                corrected_counts[corrected_label] += 1
                model_corrected[corrected_label] += 1
                total_corrected += 1
        per_model[model] = {
            "shift_c": shift,
            "raw_top": model_raw.most_common(1)[0][0] if model_raw else "",
            "corrected_top": model_corrected.most_common(1)[0][0] if model_corrected else "",
            "raw_bucket_counts": dict(model_raw),
            "corrected_bucket_counts": dict(model_corrected),
        }

    def probs(counts: Counter[str], total: int) -> Dict[str, float]:
        if total <= 0:
            return {label: 0.0 for label in labels}
        return {label: float(counts.get(label, 0)) / float(total) for label in labels}

    raw_probs = probs(raw_counts, total_raw)
    corrected_probs = probs(corrected_counts, total_corrected)
    return {
        "event_run": payload.get("_event_run") or {},
        "labels": labels,
        "raw_total_members": total_raw,
        "corrected_total_members": total_corrected,
        "raw_member_probs": raw_probs,
        "corrected_member_probs": corrected_probs,
        "delta_member_probs": {label: corrected_probs.get(label, 0.0) - raw_probs.get(label, 0.0) for label in labels},
        "raw_top": max(raw_probs.items(), key=lambda kv: kv[1])[0] if raw_probs else "",
        "corrected_top": max(corrected_probs.items(), key=lambda kv: kv[1])[0] if corrected_probs else "",
        "per_model": per_model,
    }


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    db_path = Path(args.db)
    station_id = str(args.station_id or DEFAULT_STATION_ID).upper()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        start_date, end_date, history_window_policy = _resolve_history_window(conn, args, station_id=station_id)
        actuals = collect_actuals(conn, start_date=start_date, end_date=end_date, station_id=station_id)
        actuals, wu_preview_overlay = overlay_actuals_from_wu_preview(
            actuals,
            str(getattr(args, "wu_preview_report", "") or ""),
        )
        wu_history_coverage = build_wu_history_coverage(
            conn,
            actuals=actuals,
            start_date=start_date,
            end_date=end_date,
            station_id=station_id,
        )
        loc_key = str(args.loc_key or "") or _station_loc_key(conn, station_id, str(args.event_slug))
        loc_keys = _near_chengdu_loc_keys(conn, start_date=start_date, end_date=end_date)
        if not loc_key and loc_keys:
            loc_key = loc_keys[0]
        samples = collect_model_cache(conn, start_date=start_date, end_date=end_date, loc_key=loc_key or "")
        wu_stats, wu_pairs = compute_bias(actuals, samples, min_rank=3)
        all_stats, all_pairs = compute_bias(actuals, samples, min_rank=1)
        payload = latest_event_payload(conn, str(args.event_slug))
        provenance_summary = collect_chengdu_model_provenance(
            conn,
            scan_limit=int(args.model_provenance_scan_limit),
        )
        d0_afternoon_high_temp_underestimation = collect_d0_afternoon_high_temp_underestimation(
            conn,
            start_date=start_date,
            end_date=end_date,
            actuals=actuals,
            threshold_c=float(args.d0_afternoon_underestimate_threshold_c),
            min_observed_c=float(args.d0_afternoon_min_observed_c),
        )
    finally:
        conn.close()

    actual_source_counts = Counter()
    for actual in actuals.values():
        key = "wunderground" if actual.source_rank >= 3 else (actual.source or "unknown")
        actual_source_counts[key] += 1

    model_dates_by_model: Dict[str, List[str]] = defaultdict(list)
    model_source_counts: Dict[str, Counter[str]] = defaultdict(Counter)
    for sample in samples:
        model_dates_by_model[sample.model].append(sample.event_date)
        source_key = sample.official_updated_source or sample.source or "unknown"
        model_source_counts[sample.model][source_key] += 1

    recommended_scope = "wu_only" if all((row.get("n") or 0) >= int(args.min_wu_samples) for row in wu_stats.values()) and wu_stats else "all_sources_fallback"
    selected_stats = wu_stats if recommended_scope == "wu_only" else all_stats
    preview = corrected_bucket_preview(payload, shifts=selected_stats) if payload else {}
    wu_page_evidence = build_wu_page_evidence(args, actuals)
    wu_backfill_gap = build_wu_backfill_gap(
        actuals=actuals,
        start_date=start_date,
        end_date=end_date,
        station_id=station_id,
    )

    active_models = sorted(set(model_dates_by_model.keys()))
    current_payload_models = []
    provenance = {}
    if payload:
        if isinstance(payload.get("models"), list):
            current_payload_models = [str(x) for x in payload.get("models") or []]
        if isinstance(payload.get("model_provenance"), Mapping):
            provenance = dict(payload.get("model_provenance") or {})

    configured_models = list(DEFAULT_CONFIGURED_MODELS)
    missing_configured_models = [m for m in configured_models if m not in current_payload_models]
    unavailable_configured_models = {
        model: provenance_summary.get(model)
        for model in missing_configured_models
        if provenance_summary.get(model)
    }

    return {
        "ok": True,
        "read_only": True,
        "db_path": str(db_path),
        "station_id": station_id,
        "city": "chengdu",
        "wu_history_url": DEFAULT_WU_HISTORY_URL,
        "requested_weather_url": DEFAULT_WU_WEATHER_URL,
        "start_date": start_date,
        "end_date": end_date,
        "days": len(list(_iter_dates(start_date, end_date))),
        "history_window_policy": history_window_policy,
        "requested_days_limit": int(args.days or 0) or None,
        "generated_at": _utc_now(),
        "actual_coverage": {
            "distinct_days": len(actuals),
            "source_counts": dict(actual_source_counts),
            "wu_strong_days": sum(1 for a in actuals.values() if a.source_rank >= 3),
            "fallback_days": sum(1 for a in actuals.values() if a.source_rank < 3),
        },
        "wu_history_coverage": wu_history_coverage,
        "wu_preview_overlay": wu_preview_overlay,
        "wu_backfill_gap": wu_backfill_gap,
        "wu_page_evidence": wu_page_evidence,
        "d0_afternoon_high_temp_underestimation": d0_afternoon_high_temp_underestimation,
        "model_coverage": {
            "loc_key": loc_key or "",
            "candidate_loc_keys": loc_keys,
            "sample_rows": len(samples),
            "configured_models": configured_models,
            "active_models_from_cache": active_models,
            "date_ranges_by_model": {
                model: {
                    "n_dates": len(set(dates)),
                    "min_date": min(dates) if dates else "",
                    "max_date": max(dates) if dates else "",
                }
                for model, dates in sorted(model_dates_by_model.items())
            },
            "sample_source_counts_by_model": {
                model: dict(counts)
                for model, counts in sorted(model_source_counts.items())
            },
            "sample_source_note": (
                "Rows marked open_meteo_historical_forecast are historical forecast proxy samples, "
                "not recovered original real-time ensemble member caches."
            ),
            "current_payload_models": current_payload_models,
            "missing_configured_models": missing_configured_models,
            "unavailable_configured_model_evidence": unavailable_configured_models,
            "current_payload_model_provenance": provenance,
            "fourth_model_status": (
                "nws_forecast_unavailable_for_chengdu"
                if "nws_forecast" in missing_configured_models
                else "missing_for_chengdu_current_payload"
                if missing_configured_models
                else "present"
            ),
            "model_completeness": {
                "requested_model_count": len(configured_models),
                "active_model_count": len(active_models),
                "can_calibrate_all_configured_models": all(m in active_models for m in configured_models),
                "calibratable_models": active_models,
                "uncalibratable_models": [m for m in configured_models if m not in active_models],
                "note": (
                    "nws_forecast is configured but cannot provide Chengdu/ZUUU samples; "
                    "the report records its unavailable status instead of fabricating a fourth-model correction."
                    if "nws_forecast" in [m for m in configured_models if m not in active_models]
                    else ""
                ),
            },
        },
        "bias_correction": {
            "recommended_scope": recommended_scope,
            "min_wu_samples": int(args.min_wu_samples),
            "basis": "all_available_chengdu_history_in_window",
            "sample_quality": _sample_quality(
                selected_stats,
                min_wu_samples=int(args.min_wu_samples),
                source_label="WU" if recommended_scope == "wu_only" else "all-source fallback",
            ),
            "wu_only": {
                "stats_by_model": wu_stats,
                "weight_multipliers": _weight_multipliers(wu_stats),
                "sample_quality": _sample_quality(
                    wu_stats,
                    min_wu_samples=int(args.min_wu_samples),
                    source_label="WU",
                ),
                "pairs": wu_pairs,
            },
            "all_sources_fallback": {
                "stats_by_model": all_stats,
                "weight_multipliers": _weight_multipliers(all_stats),
                "sample_quality": _sample_quality(
                    all_stats,
                    min_wu_samples=int(args.min_wu_samples),
                    source_label="all-source fallback",
                ),
                "pairs": all_pairs,
            },
        },
        "latest_event_preview": preview,
        "write_plan_requires_confirmation": {
            "needed": True,
            "reason": "Applying these corrections would change runtime probabilities for Chengdu markets; this report is dry-run only.",
            "blocked_until": (
                "WU/model overlap reaches min_wu_samples"
                if recommended_scope != "wu_only"
                else ""
            ),
            "candidate_artifacts": [
                "city/station scoped model temperature shifts for ZUUU",
                "city/station scoped q_bayes model_weight_multipliers",
                "optional WU-only settlement actual refresh for fallback days",
            ],
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Chengdu WU model bias correction report")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--station-id", default=DEFAULT_STATION_ID)
    parser.add_argument("--event-slug", default=DEFAULT_EVENT_SLUG)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Optional date-window limit; 0 means all available history")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--loc-key", default="")
    parser.add_argument("--min-wu-samples", type=int, default=DEFAULT_MIN_WU_SAMPLES)
    parser.add_argument(
        "--wu-weather-html",
        default="",
        help="Optional saved WU weather page HTML for SSR 30-day summary evidence",
    )
    parser.add_argument(
        "--wu-preview-report",
        default="",
        help="Optional strict WU backfill preview report to overlay as dry-run actuals",
    )
    parser.add_argument("--d0-afternoon-underestimate-threshold-c", type=float, default=1.5)
    parser.add_argument("--d0-afternoon-min-observed-c", type=float, default=30.0)
    parser.add_argument("--model-provenance-scan-limit", type=int, default=300)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        summary = {
            "ok": report.get("ok"),
            "out": str(out),
            "actual_coverage": report.get("actual_coverage"),
            "model_coverage": report.get("model_coverage"),
            "recommended_scope": (report.get("bias_correction") or {}).get("recommended_scope"),
            "selected_stats": (
                (report.get("bias_correction") or {}).get(
                    (report.get("bias_correction") or {}).get("recommended_scope") or ""
                )
                or {}
            ).get("stats_by_model"),
            "latest_event_preview": report.get("latest_event_preview"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
