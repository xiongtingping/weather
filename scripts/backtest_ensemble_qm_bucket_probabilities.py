#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import numpy  # noqa: F401
except ModuleNotFoundError:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        os.execv(str(venv_python), [str(venv_python), *sys.argv])
    raise

from weather_ensemble_bias_correction import (  # noqa: E402
    bucket_label_for_value,
    bucket_probabilities_from_members,
    calibrate_members_and_bucket_probabilities,
)
from climo_regime_runtime import (  # noqa: E402
    cap_climo_evidence_by_pp_delta,
    climo_regime_pp_cap,
    load_climo_regime_context,
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_LOC_KEY = "30.5583,103.9460"
DEFAULT_STATION_ID = "ZUUU"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_OUTCOME_LABELS = [
    "25°C or below",
    "26°C",
    "27°C",
    "28°C",
    "29°C",
    "30°C",
    "31°C",
    "32°C",
    "33°C",
    "34°C",
    "35°C or higher",
]


def _parse_dt(value: Any) -> Optional[datetime]:
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


def _zone(name: str) -> timezone:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(str(name or "UTC"))  # type: ignore[return-value]
        except Exception:
            pass
    return timezone.utc


def _date_key(value: Any) -> str:
    return str(value or "").split("T", 1)[0][:10]


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _json_loads(raw: Any) -> Any:
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return None


def _city_defaults(city: str) -> Tuple[str, str, Sequence[str]]:
    key = str(city or "").strip().lower()
    if key in {"chengdu", "成都"}:
        return DEFAULT_STATION_ID, DEFAULT_LOC_KEY, DEFAULT_OUTCOME_LABELS
    raise ValueError(f"unsupported_city:{city}")


def _city_tz(city: str) -> str:
    key = str(city or "").strip().lower()
    if key in {"chengdu", "成都"}:
        return DEFAULT_TZ
    return "UTC"


def _load_actual_events(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    source: str,
    market_var: str,
    market_unit: str,
) -> List[Dict[str, Any]]:
    source_clause = ""
    params: List[Any] = [station_id, market_var, market_unit]
    if source == "wunderground":
        source_clause = "AND lower(coalesce(resolution_source, '')) LIKE ?"
        params.append("%wunderground.com/history/daily%")
    rows = conn.execute(
        f"""
        SELECT event_date, settlement_actual, resolution_source, created_at
        FROM event_daily_temps
        WHERE upper(coalesce(station_id, '')) = upper(?)
          AND market_var = ?
          AND market_unit = ?
          AND settlement_actual IS NOT NULL
          {source_clause}
        ORDER BY event_date ASC, datetime(created_at) DESC, id DESC
        """,
        params,
    ).fetchall()
    seen = set()
    events: List[Dict[str, Any]] = []
    for row in rows:
        event_date = str(row["event_date"] or "")[:10]
        actual = _to_float(row["settlement_actual"])
        if not event_date or event_date in seen or actual is None:
            continue
        seen.add(event_date)
        events.append(
            {
                "event_date": event_date,
                "actual": float(actual),
                "resolution_source": str(row["resolution_source"] or ""),
            }
        )
    formal_params: List[Any] = [station_id, market_var, market_unit]
    formal_rows = conn.execute(
        """
        SELECT event_date, settlement_actual, created_at, slug
        FROM event_runs
        WHERE upper(coalesce(station_id, '')) = upper(?)
          AND market_var = ?
          AND market_unit = ?
          AND settlement_actual IS NOT NULL
          AND COALESCE(market_closed, 0) = 1
        ORDER BY event_date ASC, datetime(created_at) DESC, id DESC
        """,
        formal_params,
    ).fetchall()
    for row in formal_rows:
        event_date = str(row["event_date"] or "")[:10]
        actual = _to_float(row["settlement_actual"])
        if not event_date or event_date in seen or actual is None:
            continue
        seen.add(event_date)
        events.append(
            {
                "event_date": event_date,
                "actual": float(actual),
                "resolution_source": "formal_closed_event_runs",
            }
        )
    events.sort(key=lambda item: str(item.get("event_date") or ""))
    return events


def _load_prediction_rows(
    conn: sqlite3.Connection,
    *,
    city: str,
    station_id: str,
    event_dates: Sequence[str],
    market_var: str,
    market_unit: str,
    allow_closed_snapshots: bool,
    allow_snapshot_settlement_actual: bool,
    allow_post_event_end: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    if not event_dates:
        return [], {}
    placeholders = ",".join("?" for _ in event_dates)
    params: List[Any] = [
        market_var,
        market_unit,
        station_id,
        f"%{str(city).strip().lower()}%",
        *event_dates,
    ]
    rows = conn.execute(
        f"""
        SELECT id, slug, event_date, city, station_id, market_var, market_unit,
               created_at, event_peak_time_utc, event_end_utc, market_closed,
               settlement_actual, tz, lon, payload_json
        FROM event_runs
        WHERE market_var = ?
          AND market_unit = ?
          AND (upper(coalesce(station_id, '')) = upper(?) OR lower(coalesce(city, '')) LIKE ?)
          AND event_date IN ({placeholders})
          AND created_at IS NOT NULL
        ORDER BY datetime(created_at) ASC, id ASC
        """,
        params,
    ).fetchall()
    out: List[Dict[str, Any]] = []
    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = int(skipped.get(reason, 0)) + 1

    for row in rows:
        item = dict(row)
        created_at = _parse_dt(item.get("created_at"))
        if created_at is None:
            skip("created_at_missing")
            continue
        if not allow_snapshot_settlement_actual and _to_float(item.get("settlement_actual")) is not None:
            skip("snapshot_row_has_settlement_actual")
            continue
        if not allow_closed_snapshots and int(item.get("market_closed") or 0) != 0:
            skip("snapshot_row_market_closed")
            continue
        event_end = _parse_dt(item.get("event_end_utc"))
        if not allow_post_event_end and event_end is not None and created_at >= event_end:
            skip("snapshot_at_or_after_event_end")
            continue
        out.append(item)
    return out, skipped


def _load_pending_prediction_rows(
    conn: sqlite3.Connection,
    *,
    city: str,
    station_id: str,
    known_event_dates: Sequence[str],
    market_var: str,
    market_unit: str,
    allow_post_event_end: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    known = [str(value)[:10] for value in known_event_dates if str(value).strip()]
    known_clause = ""
    params: List[Any] = [
        market_var,
        market_unit,
        station_id,
        f"%{str(city).strip().lower()}%",
    ]
    if known:
        known_clause = "AND event_date NOT IN ({})".format(",".join("?" for _ in known))
        params.extend(known)
    rows = conn.execute(
        f"""
        SELECT id, slug, event_date, city, station_id, market_var, market_unit,
               created_at, event_peak_time_utc, event_end_utc, market_closed,
               settlement_actual, tz, lon, payload_json
        FROM event_runs
        WHERE market_var = ?
          AND market_unit = ?
          AND (upper(coalesce(station_id, '')) = upper(?) OR lower(coalesce(city, '')) LIKE ?)
          AND created_at IS NOT NULL
          AND COALESCE(market_closed, 0) = 0
          AND settlement_actual IS NULL
          {known_clause}
        ORDER BY datetime(created_at) ASC, id ASC
        """,
        params,
    ).fetchall()
    out: List[Dict[str, Any]] = []
    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = int(skipped.get(reason, 0)) + 1

    for row in rows:
        item = dict(row)
        created_at = _parse_dt(item.get("created_at"))
        if created_at is None:
            skip("created_at_missing")
            continue
        event_end = _parse_dt(item.get("event_end_utc"))
        if not allow_post_event_end and event_end is not None and created_at >= event_end:
            skip("snapshot_at_or_after_event_end")
            continue
        out.append(item)
    return out, skipped


def _load_event_members_as_of(
    conn: sqlite3.Connection,
    *,
    loc_key: str,
    event_date: str,
    market_var: str,
    market_unit: str,
    as_of: str,
) -> Tuple[Dict[str, List[float]], Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT model, members_json, created_at
        FROM model_member_cache
        WHERE loc_key = ?
          AND event_date = ?
          AND market_var = ?
          AND market_unit = ?
          AND datetime(created_at) < datetime(?)
        ORDER BY model, datetime(created_at) DESC, id DESC
        """,
        (loc_key, event_date, market_var, market_unit, as_of),
    ).fetchall()
    out: Dict[str, List[float]] = {}
    model_created_at: Dict[str, str] = {}
    for row in rows:
        model = str(row["model"] or "")
        if not model or model in out:
            continue
        raw = _json_loads(row["members_json"])
        if not isinstance(raw, list):
            continue
        vals: List[float] = []
        for item in raw:
            val = _to_float(item)
            if val is not None:
                vals.append(float(val))
        if vals:
            out[model] = vals
            model_created_at[model] = str(row["created_at"] or "")
    return out, {
        "model_count": len(out),
        "member_count": sum(len(values) for values in out.values()),
        "model_created_at": model_created_at,
        "created_at_max": max(model_created_at.values()) if model_created_at else "",
    }


def _load_station_temperature_qm_history_as_of(
    conn: sqlite3.Connection,
    *,
    loc_key: str,
    station_id: str,
    market_var: str,
    market_unit: str,
    target_event_date: str,
    source: str,
    max_days: int,
    as_of: str,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    source_clause = ""
    params: List[Any] = [station_id, market_var, market_unit, target_event_date, as_of]
    if source == "wunderground":
        source_clause = "AND lower(coalesce(resolution_source, '')) LIKE ?"
        params.append("%wunderground.com/history/daily%")
    rows = conn.execute(
        f"""
        SELECT event_date, settlement_actual, resolution_source, created_at
        FROM event_daily_temps
        WHERE upper(coalesce(station_id, '')) = upper(?)
          AND market_var = ?
          AND market_unit = ?
          AND event_date < ?
          AND datetime(created_at) < datetime(?)
          AND settlement_actual IS NOT NULL
          {source_clause}
        ORDER BY event_date DESC, datetime(created_at) DESC, id DESC
        """,
        params,
    ).fetchall()
    actuals: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        event_date = _date_key(row["event_date"])
        actual = _to_float(row["settlement_actual"])
        if event_date and actual is not None and event_date not in actuals:
            actuals[event_date] = {
                "actual": float(actual),
                "actual_source": source,
                "actual_created_at": str(row["created_at"] or ""),
            }
        if len(actuals) >= max(1, int(max_days)):
            break
    if not actuals:
        return {}, {
            "history_days": 0,
            "history_event_date_min": "",
            "history_event_date_max": "",
            "model_count": 0,
            "member_count": 0,
        }
    start_date = min(actuals)
    end_date = max(actuals)
    cache_rows = conn.execute(
        """
        SELECT model, event_date, members_json, created_at
        FROM model_member_cache
        WHERE loc_key = ?
          AND event_date >= ?
          AND event_date <= ?
          AND market_var = ?
          AND market_unit = ?
          AND datetime(created_at) < datetime(?)
        ORDER BY model, event_date, datetime(created_at) DESC, id DESC
        """,
        (loc_key, start_date, end_date, market_var, market_unit, as_of),
    ).fetchall()
    latest: Dict[Tuple[str, str], Tuple[List[float], str]] = {}
    for row in cache_rows:
        model = str(row["model"] or "")
        event_date = _date_key(row["event_date"])
        if not model or event_date not in actuals:
            continue
        key = (model, event_date)
        if key in latest:
            continue
        raw = _json_loads(row["members_json"])
        if not isinstance(raw, list):
            continue
        values: List[float] = []
        for item in raw:
            val = _to_float(item)
            if val is not None:
                values.append(float(val))
        if values:
            latest[key] = (values, str(row["created_at"] or ""))
    history: Dict[str, List[Dict[str, Any]]] = {}
    model_created_values: List[str] = []
    for (model, event_date), (values, member_created_at) in latest.items():
        actual_entry = actuals[event_date]
        history.setdefault(model, []).append(
            {
                "event_date": event_date,
                "forecast_values": values,
                "actual": actual_entry["actual"],
                "actual_source": actual_entry["actual_source"],
                "actual_created_at": actual_entry["actual_created_at"],
                "member_created_at": member_created_at,
            }
        )
        if member_created_at:
            model_created_values.append(member_created_at)
    for model in list(history):
        history[model].sort(key=lambda item: str(item.get("event_date") or ""))
    event_dates = sorted({item["event_date"] for rows_for_model in history.values() for item in rows_for_model})
    return history, {
        "history_days": len(event_dates),
        "history_event_date_min": event_dates[0] if event_dates else "",
        "history_event_date_max": event_dates[-1] if event_dates else "",
        "model_count": len(history),
        "member_count": sum(len(item.get("forecast_values") or []) for rows_for_model in history.values() for item in rows_for_model),
        "member_created_at_max": max(model_created_values) if model_created_values else "",
        "actual_created_at_max": max(
            str(entry.get("actual_created_at") or "") for entry in actuals.values()
        ) if actuals else "",
    }


def _score(probs: Mapping[str, float], actual_label: str, labels: Sequence[str]) -> Tuple[float, float]:
    brier = 0.0
    mae = 0.0
    for label in labels:
        target = 1.0 if label == actual_label else 0.0
        p = float(probs.get(label, 0.0) or 0.0)
        brier += (p - target) ** 2
        mae += abs(p - target)
    denom = max(1, len(labels))
    return brier / float(denom), mae / float(denom)


def _boundary_score(probs: Mapping[str, float], actual_label: str, labels: Sequence[str]) -> float:
    try:
        actual_idx = list(labels).index(actual_label)
    except ValueError:
        return float("nan")
    cumulative = 0.0
    score = 0.0
    for idx, label in enumerate(labels[:-1]):
        cumulative += float(probs.get(label, 0.0) or 0.0)
        target = 1.0 if actual_idx <= idx else 0.0
        score += (cumulative - target) ** 2
    return score / float(max(1, len(labels) - 1))


def _pct_improvement(old: float, new: float) -> Optional[float]:
    if old <= 0 or not math.isfinite(old) or not math.isfinite(new):
        return None
    return (old - new) / old


def _logloss(probs: Mapping[str, float], actual_label: str) -> float:
    return -math.log(max(1e-12, float(probs.get(actual_label, 0.0) or 0.0)))


def _top_label(probs: Mapping[str, float]) -> str:
    if not probs:
        return ""
    return max(probs, key=lambda label: float(probs.get(label, 0.0) or 0.0))


def _parse_float_grid(raw: Any) -> List[float]:
    values: List[float] = []
    for item in str(raw or "").split(","):
        val = _to_float(item.strip())
        if val is not None and val >= 0:
            values.append(float(val))
    if not values:
        return [0.0]
    return sorted(set(values))


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    if math.isinf(x):
        return 0.0 if x < 0 else 1.0
    z = (float(x) - float(mu)) / max(1e-12, float(sigma))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _label_unit(label: str) -> str:
    match = re.search(r"([A-Za-z°/%]+)\s*$", str(label or "").strip())
    return str(match.group(1) if match else "").upper().replace("°", "")


def _bucket_bounds(label: str, rounding_rule: str) -> Optional[Tuple[float, float]]:
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
        if rounded:
            return float("-inf"), k + 0.5
        return float("-inf"), k + 1.0 if whole else k
    match = re.match(rf"^{num}\s*{unit}\s+or\s+(higher|above|more)$", raw, flags=re.I)
    if match:
        k = float(match.group(1))
        return (k - 0.5 if rounded else k), float("inf")
    match = re.match(rf"^{num}\s*{unit}$", raw)
    if match:
        k = float(match.group(1))
        return (k, k + 1.0) if whole else (k - 0.5, k + 0.5)
    return None


def _taf_bucket_probs(labels: Sequence[str], peak_c: float, *, sigma_c: float, rounding_rule: str) -> Dict[str, float]:
    raw_probs: Dict[str, float] = {}
    total = 0.0
    for label in labels:
        bounds = _bucket_bounds(str(label), rounding_rule)
        if bounds is None:
            raw_probs[str(label)] = 0.0
            continue
        low, high = bounds
        prob = max(0.0, _normal_cdf(float(high), float(peak_c), float(sigma_c)) - _normal_cdf(float(low), float(peak_c), float(sigma_c)))
        raw_probs[str(label)] = float(prob)
        total += float(prob)
    if total <= 0:
        return {str(label): 0.0 for label in labels}
    return {label: value / total for label, value in raw_probs.items()}


def _extract_taf_peak_c_from_payload(raw_payload: Any) -> Optional[float]:
    payload = _json_loads(raw_payload)
    if not isinstance(payload, Mapping):
        return None
    candidates: List[Any] = []
    taf = payload.get("taf_forecast_peak")
    if isinstance(taf, Mapping):
        candidates.extend([taf.get("value_c"), taf.get("peak_c")])
    features = payload.get("weather_likelihood_features")
    if isinstance(features, Mapping):
        candidates.append(features.get("taf_forecast_peak_c"))
    for row in payload.get("rows") or []:
        if isinstance(row, Mapping):
            candidates.append(row.get("taf_forecast_peak_c"))
            candidates.append(row.get("q_bayes_taf_peak_c"))
    for value in candidates:
        parsed = _to_float(value)
        if parsed is not None:
            return float(parsed)
    return None


def _blend_with_taf(
    base_probs: Mapping[str, float],
    taf_probs: Mapping[str, float],
    *,
    base_evidence: float,
    taf_evidence: float,
    labels: Sequence[str],
) -> Dict[str, float]:
    base_w = max(0.0, float(base_evidence))
    taf_w = max(0.0, float(taf_evidence))
    values: Dict[str, float] = {}
    total = 0.0
    for label in labels:
        value = base_w * float(base_probs.get(label, 0.0) or 0.0) + taf_w * float(taf_probs.get(label, 0.0) or 0.0)
        values[str(label)] = max(0.0, value)
        total += values[str(label)]
    if total <= 0:
        denom = float(max(1, len(labels)))
        return {str(label): 1.0 / denom for label in labels}
    return {label: value / total for label, value in values.items()}


def _extract_weather_features_from_payload(raw_payload: Any) -> Dict[str, Any]:
    payload = _json_loads(raw_payload)
    if not isinstance(payload, Mapping):
        return {}
    features = payload.get("weather_likelihood_features")
    return dict(features) if isinstance(features, Mapping) else {}


def _blend_with_climo(
    base_probs: Mapping[str, float],
    climo_probs: Mapping[str, float],
    *,
    base_evidence: float,
    climo_evidence: float,
    labels: Sequence[str],
) -> Dict[str, float]:
    base_w = max(0.0, float(base_evidence))
    climo_w = max(0.0, float(climo_evidence))
    values: Dict[str, float] = {}
    total = 0.0
    for label in labels:
        value = base_w * float(base_probs.get(label, 0.0) or 0.0) + climo_w * float(climo_probs.get(label, 0.0) or 0.0)
        values[str(label)] = max(0.0, value)
        total += values[str(label)]
    if total <= 0:
        denom = float(max(1, len(labels)))
        return {str(label): 1.0 / denom for label in labels}
    return {label: value / total for label, value in values.items()}


def _climo_cap_for_event(
    *,
    base_probs: Mapping[str, float],
    climo_probs: Mapping[str, float],
    raw_evidence: float,
    base_evidence: float,
    labels: Sequence[str],
    features: Mapping[str, Any],
    days_to_event: Optional[float],
) -> Dict[str, Any]:
    pp_cap_meta = climo_regime_pp_cap(days_to_event=days_to_event, features=features)
    base_scores = [max(0.0, float(base_evidence)) * float(base_probs.get(label, 0.0) or 0.0) for label in labels]
    cap_meta = cap_climo_evidence_by_pp_delta(
        base_scores=base_scores,
        climo_probs=[float(climo_probs.get(label, 0.0) or 0.0) for label in labels],
        raw_evidence=max(0.0, float(raw_evidence)),
        max_abs_pp=float(_to_float(pp_cap_meta.get("max_abs_pp")) or 0.0),
    )
    return {
        "raw_evidence": max(0.0, float(raw_evidence)),
        "evidence": max(0.0, float(cap_meta.get("evidence") or 0.0)),
        "pp_cap": float(_to_float(pp_cap_meta.get("max_abs_pp")) or 0.0),
        "pp_cap_stage": str(pp_cap_meta.get("stage") or ""),
        "pp_cap_direct_weight_allowed": bool(pp_cap_meta.get("direct_weight_allowed")),
        "pp_max_delta": float(_to_float(cap_meta.get("max_abs_delta")) or 0.0),
        "pp_raw_max_delta": float(_to_float(cap_meta.get("raw_max_abs_delta")) or _to_float(cap_meta.get("max_abs_delta")) or 0.0),
        "pp_cap_hit": bool(cap_meta.get("cap_hit")),
    }


def _prob_max_abs_delta(
    left: Mapping[str, float],
    right: Mapping[str, float],
    labels: Sequence[str],
) -> float:
    if not labels:
        return 0.0
    return max(abs(float(left.get(label, 0.0) or 0.0) - float(right.get(label, 0.0) or 0.0)) for label in labels)


def _taf_calibration_report(
    events: Sequence[Mapping[str, Any]],
    *,
    labels: Sequence[str],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    taf_events = [
        event for event in events
        if isinstance(event.get("taf_probs"), Mapping) and _to_float(event.get("taf_peak_c")) is not None
    ]
    min_samples = int(getattr(args, "taf_min_samples", 5) or 5)
    if len(taf_events) < min_samples:
        return {
            "status": "insufficient_visible_taf_samples" if taf_events else "no_visible_taf_samples",
            "visible_sample_count": len(taf_events),
            "visible_market_day_count": len({str(event.get("event_date") or "") for event in taf_events}),
            "min_samples": min_samples,
            "created_at_guard": "uses_only_taf_fields_present_in_event_runs_payload_at_sample_created_at",
        }
    base_evidence = float(getattr(args, "taf_baseline_evidence", 36.0) or 36.0)
    grid = _parse_float_grid(getattr(args, "taf_weight_grid", "0,6,12,18,24,30,36"))
    candidates: List[Dict[str, Any]] = []
    for weight in grid:
        scored: List[Dict[str, Any]] = []
        for event in taf_events:
            blended = _blend_with_taf(
                event.get("qm_probs") if isinstance(event.get("qm_probs"), Mapping) else {},
                event.get("taf_probs") if isinstance(event.get("taf_probs"), Mapping) else {},
                base_evidence=base_evidence,
                taf_evidence=float(weight),
                labels=labels,
            )
            actual_label = str(event.get("actual_label") or "")
            brier, mae = _score(blended, actual_label, labels)
            scored.append(
                {
                    "event_date": str(event.get("event_date") or ""),
                    "actual_label": actual_label,
                    "taf_brier": brier,
                    "taf_mae": mae,
                    "taf_boundary_rps": _boundary_score(blended, actual_label, labels),
                    "taf_logloss": _logloss(blended, actual_label),
                    "taf_top_label": _top_label(blended),
                }
            )
        baseline = [
            {
                "event_date": str(event.get("event_date") or ""),
                "actual_label": str(event.get("actual_label") or ""),
                "raw_brier": event.get("qm_brier"),
                "qm_brier": scored_item.get("taf_brier"),
                "raw_mae": event.get("qm_mae"),
                "qm_mae": scored_item.get("taf_mae"),
                "raw_boundary_rps": event.get("qm_boundary_rps"),
                "qm_boundary_rps": scored_item.get("taf_boundary_rps"),
                "raw_logloss": event.get("qm_logloss"),
                "qm_logloss": scored_item.get("taf_logloss"),
                "raw_top_label": event.get("qm_top_label"),
                "qm_top_label": scored_item.get("taf_top_label"),
            }
            for event, scored_item in zip(taf_events, scored)
        ]
        metrics = _metric_summary(baseline)
        candidates.append({"taf_evidence": float(weight), "metrics_vs_qm": metrics})
    best = min(
        candidates,
        key=lambda item: (
            float((item.get("metrics_vs_qm") or {}).get("qm_brier") or float("inf")),
            float((item.get("metrics_vs_qm") or {}).get("qm_boundary_rps") or float("inf")),
        ),
    )
    return {
        "status": "ok",
        "visible_sample_count": len(taf_events),
        "visible_market_day_count": len({str(event.get("event_date") or "") for event in taf_events}),
        "min_samples": min_samples,
        "sigma_c": float(getattr(args, "taf_sigma_c", 0.8) or 0.8),
        "baseline_evidence": base_evidence,
        "recommended_taf_evidence": best.get("taf_evidence"),
        "best_metrics_vs_qm": best.get("metrics_vs_qm"),
        "grid": candidates,
        "created_at_guard": "uses_only_taf_fields_present_in_event_runs_payload_at_sample_created_at",
    }


def _climo_regime_weight_calibration_report(
    events: Sequence[Mapping[str, Any]],
    *,
    labels: Sequence[str],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    climo_events = [
        event
        for event in events
        if isinstance(event.get("qm_probs_without_climo"), Mapping)
        and isinstance(event.get("climo_regime_probs"), Mapping)
    ]
    min_samples = int(getattr(args, "climo_regime_min_samples", 3) or 3)
    if len(climo_events) < min_samples:
        return {
            "status": "insufficient_visible_climo_samples" if climo_events else "no_visible_climo_samples",
            "visible_sample_count": len(climo_events),
            "visible_market_day_count": len({str(event.get("event_date") or "") for event in climo_events}),
            "min_samples": min_samples,
            "created_at_guard": "uses_only_climo_features_and_history_visible_before_sample_created_at",
        }
    base_evidence = float(getattr(args, "climo_regime_base_evidence", 36.0) or 36.0)
    grid = _parse_float_grid(getattr(args, "climo_regime_weight_grid", "0,0.25,0.5,1,2,3,4,6,8,10,12"))
    candidates: List[Dict[str, Any]] = []
    for weight in grid:
        scored: List[Dict[str, Any]] = []
        for event in climo_events:
            scale = max(0.0, min(1.0, float(_to_float(event.get("climo_regime_evidence_scale")) or 0.0)))
            features = event.get("climo_regime_features") if isinstance(event.get("climo_regime_features"), Mapping) else {}
            cap_meta = _climo_cap_for_event(
                base_probs=event.get("qm_probs_without_climo") if isinstance(event.get("qm_probs_without_climo"), Mapping) else {},
                climo_probs=event.get("climo_regime_probs") if isinstance(event.get("climo_regime_probs"), Mapping) else {},
                base_evidence=base_evidence,
                raw_evidence=float(weight) * scale,
                labels=labels,
                features=features,
                days_to_event=(
                    float(_to_float(event.get("hours_to_peak_window"))) / 24.0
                    if _to_float(event.get("hours_to_peak_window")) is not None
                    else None
                ),
            )
            blended = _blend_with_climo(
                event.get("qm_probs_without_climo") if isinstance(event.get("qm_probs_without_climo"), Mapping) else {},
                event.get("climo_regime_probs") if isinstance(event.get("climo_regime_probs"), Mapping) else {},
                base_evidence=base_evidence,
                climo_evidence=float(cap_meta.get("evidence") or 0.0),
                labels=labels,
            )
            actual_label = str(event.get("actual_label") or "")
            brier, mae = _score(blended, actual_label, labels)
            scored.append(
                {
                    "event_date": str(event.get("event_date") or ""),
                    "actual_label": actual_label,
                    "dte_bucket": str(event.get("dte_bucket") or "unknown"),
                    "hours_to_peak_bucket": str(event.get("hours_to_peak_bucket") or "unknown"),
                    "climo_brier": brier,
                    "climo_mae": mae,
                    "climo_boundary_rps": _boundary_score(blended, actual_label, labels),
                    "climo_logloss": _logloss(blended, actual_label),
                    "climo_top_label": _top_label(blended),
                    "climo_pp_max_delta": cap_meta.get("pp_max_delta"),
                    "climo_pp_cap": cap_meta.get("pp_cap"),
                }
            )
        baseline = [
            {
                "event_date": str(event.get("event_date") or ""),
                "actual_label": str(event.get("actual_label") or ""),
                "dte_bucket": str(event.get("dte_bucket") or "unknown"),
                "hours_to_peak_bucket": str(event.get("hours_to_peak_bucket") or "unknown"),
                "raw_brier": event.get("qm_without_climo_brier"),
                "qm_brier": scored_item.get("climo_brier"),
                "raw_mae": event.get("qm_without_climo_mae"),
                "qm_mae": scored_item.get("climo_mae"),
                "raw_boundary_rps": event.get("qm_without_climo_boundary_rps"),
                "qm_boundary_rps": scored_item.get("climo_boundary_rps"),
                "raw_logloss": event.get("qm_without_climo_logloss"),
                "qm_logloss": scored_item.get("climo_logloss"),
                "raw_top_label": event.get("qm_without_climo_top_label"),
                "qm_top_label": scored_item.get("climo_top_label"),
            }
            for event, scored_item in zip(climo_events, scored)
        ]
        metrics = _metric_summary(baseline)
        dte_segments = _segment_metrics(baseline, "dte_bucket")
        hours_segments = _segment_metrics(baseline, "hours_to_peak_bucket")
        candidates.append(
            {
                "climo_regime_evidence": float(weight),
                "metrics_vs_no_climo": metrics,
                "stratified_by_lead_time": dte_segments,
                "stratified_by_peak_window": hours_segments,
            }
        )

    def candidate_key(item: Mapping[str, Any]) -> Tuple[float, float, float]:
        metrics = item.get("metrics_vs_no_climo") if isinstance(item.get("metrics_vs_no_climo"), Mapping) else {}
        return (
            -float(_to_float(metrics.get("boundary_rps_improvement")) or -999.0),
            -float(_to_float(metrics.get("brier_improvement")) or -999.0),
            float(item.get("climo_regime_evidence") or 0.0),
        )

    best = min(candidates, key=candidate_key)
    best_metrics = best.get("metrics_vs_no_climo") if isinstance(best.get("metrics_vs_no_climo"), Mapping) else {}
    lead = best.get("stratified_by_lead_time") if isinstance(best.get("stratified_by_lead_time"), Mapping) else {}
    d1 = lead.get("d_minus_1") if isinstance(lead.get("d_minus_1"), Mapping) else {}
    d2 = lead.get("d_minus_2") if isinstance(lead.get("d_minus_2"), Mapping) else {}
    d0 = lead.get("d0") if isinstance(lead.get("d0"), Mapping) else {}
    required_improvement = 0.05
    max_d0_degradation = 0.02
    gate_passed = bool(
        (
            (_to_float(d1.get("boundary_rps_improvement")) or -999.0) >= required_improvement
            or (_to_float(d1.get("logloss_improvement")) or -999.0) >= required_improvement
        )
        and (
            int(d2.get("n") or 0) < int(getattr(args, "min_segment_samples", 5) or 5)
            or (_to_float(d2.get("boundary_rps_improvement")) or -999.0) >= required_improvement
            or (_to_float(d2.get("logloss_improvement")) or -999.0) >= required_improvement
        )
        and (_to_float(d0.get("boundary_rps_improvement")) or 0.0) >= -max_d0_degradation
        and (_to_float(best_metrics.get("brier_improvement")) or 0.0) >= -max_d0_degradation
        and (_to_float(best_metrics.get("top1_accuracy_lift_pp")) or 0.0) >= -2.0
    )
    return {
        "status": "ok" if gate_passed else "gate_failed",
        "visible_sample_count": len(climo_events),
        "visible_market_day_count": len({str(event.get("event_date") or "") for event in climo_events}),
        "min_samples": min_samples,
        "baseline_evidence": base_evidence,
        "recommended_climo_regime_evidence": best.get("climo_regime_evidence"),
        "best_metrics_vs_no_climo": best_metrics,
        "best_stratified_by_lead_time": best.get("stratified_by_lead_time"),
        "best_stratified_by_peak_window": best.get("stratified_by_peak_window"),
        "goal_gate": {
            "passed": gate_passed,
            "required_d_minus_1_rps_or_logloss_improvement": required_improvement,
            "required_d_minus_2_rps_or_logloss_improvement": required_improvement,
            "max_d0_near_peak_degradation": max_d0_degradation,
            "max_brier_degradation": max_d0_degradation,
            "max_top1_drop_pp": 2.0,
            "note": "D-2 gate is reported but not enforced when sample count is below min_segment_samples.",
        },
        "grid": candidates,
        "created_at_guard": "uses_only_climo_features_and_history_visible_before_sample_created_at",
        "display_label": "气候/环流弱参考，不覆盖实况温度轨迹",
    }


def _climo_pp_impact_audit(events: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = [event for event in events if _to_float(event.get("climo_regime_evidence_raw")) is not None]
    if not rows:
        return {
            "status": "no_climo_samples",
            "climo_pp_max_delta": 0.0,
            "climo_pp_cap_pass": True,
            "temperature_trajectory_priority": True,
        }
    max_delta = max(float(_to_float(event.get("climo_regime_pp_max_delta")) or 0.0) for event in rows)
    failures = [
        {
            "event_date": str(event.get("event_date") or ""),
            "created_at": str(event.get("created_at") or ""),
            "stage": str(event.get("climo_regime_pp_cap_stage") or ""),
            "pp_max_delta": float(_to_float(event.get("climo_regime_pp_max_delta")) or 0.0),
            "pp_cap": float(_to_float(event.get("climo_regime_pp_cap")) or 0.0),
        }
        for event in rows
        if not bool(event.get("climo_regime_pp_cap_pass", True))
    ]
    by_stage: Dict[str, Dict[str, Any]] = {}
    for event in rows:
        stage = str(event.get("climo_regime_pp_cap_stage") or "unknown")
        item = by_stage.setdefault(
            stage,
            {
                "n": 0,
                "climo_pp_max_delta": 0.0,
                "climo_pp_cap": float(_to_float(event.get("climo_regime_pp_cap")) or 0.0),
                "climo_pp_cap_pass": True,
            },
        )
        item["n"] += 1
        item["climo_pp_max_delta"] = max(
            float(item.get("climo_pp_max_delta") or 0.0),
            float(_to_float(event.get("climo_regime_pp_max_delta")) or 0.0),
        )
        item["climo_pp_cap_pass"] = bool(item["climo_pp_cap_pass"]) and bool(event.get("climo_regime_pp_cap_pass", True))
    return {
        "status": "ok" if not failures else "pp_cap_failed",
        "sample_count": len(rows),
        "climo_pp_max_delta": max_delta,
        "climo_pp_cap_pass": not failures,
        "temperature_trajectory_priority": True,
        "by_stage": by_stage,
        "failures": failures[:20],
    }


def _regime_stratification_report(events: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for event in events:
        regime = str(event.get("climo_regime_primary_regime") or "neutral_proxy")
        groups.setdefault(regime, []).append(event)
    if not groups:
        return {"status": "no_regime_samples", "segments": {}}
    segments: Dict[str, Any] = {}
    for regime, rows in sorted(groups.items()):
        sample_count = len(rows)
        segments[regime] = {
            "regime_sample_count": sample_count,
            "regime_auto_weight_allowed": sample_count >= 10,
            "temperature_trajectory_priority": True,
            "metrics": _metric_summary(rows),
        }
    return {
        "status": "ok",
        "segments": segments,
        "regime_sample_count": sum(len(rows) for rows in groups.values()),
        "regime_auto_weight_allowed": all(len(rows) >= 10 for rows in groups.values()),
        "sample_lt_10_policy": "display_only_no_auto_weight",
    }


def _hours_to_peak_window(row: Mapping[str, Any], *, peak_hour: float, default_tz: str) -> Optional[float]:
    created_at = _parse_dt(row.get("created_at"))
    event_date = _date_key(row.get("event_date"))
    if created_at is None or not event_date:
        return None
    peak_utc = _parse_dt(row.get("event_peak_time_utc"))
    if peak_utc is None:
        try:
            day = date.fromisoformat(event_date)
        except ValueError:
            return None
        zone = _snapshot_zone(row, default_tz=default_tz)
        hour = int(math.floor(float(peak_hour)))
        minute = int(round((float(peak_hour) - hour) * 60.0))
        peak_utc = datetime.combine(day, time(hour, minute), tzinfo=zone).astimezone(timezone.utc)
    return (peak_utc - created_at).total_seconds() / 3600.0


def _hours_bucket(hours_to_peak: Optional[float]) -> str:
    if hours_to_peak is None:
        return "unknown"
    if hours_to_peak > 12.0:
        return "gt_12h"
    if hours_to_peak > 6.0:
        return "6_to_12h"
    if hours_to_peak > 2.0:
        return "2_to_6h"
    if hours_to_peak > 0.0:
        return "0_to_2h"
    return "peak_or_after"


def _snapshot_zone(row: Mapping[str, Any], *, default_tz: str) -> timezone:
    row_tz = str(row.get("tz") or "").strip()
    fallback_tz = str(default_tz or "UTC").strip() or "UTC"
    if fallback_tz.upper() != "UTC" and (not row_tz or row_tz.upper() == "UTC"):
        return _zone(fallback_tz)
    return _zone(row_tz or fallback_tz)


def _dte_bucket(row: Mapping[str, Any], *, default_tz: str) -> str:
    created_at = _parse_dt(row.get("created_at"))
    event_date = _date_key(row.get("event_date"))
    if created_at is None or not event_date:
        return "unknown"
    try:
        event_day = date.fromisoformat(event_date)
    except ValueError:
        return "unknown"
    zone = _snapshot_zone(row, default_tz=default_tz)
    created_local_day = created_at.astimezone(zone).date()
    delta = (event_day - created_local_day).days
    if delta < 0:
        return "post_event_local"
    if delta == 0:
        return "d0"
    if delta == 1:
        return "d_minus_1"
    if delta == 2:
        return "d_minus_2"
    return "d_minus_3_plus"


def _mean(values: Sequence[float]) -> Optional[float]:
    nums = [float(v) for v in values if math.isfinite(float(v))]
    return sum(nums) / len(nums) if nums else None


def _metric_summary(events: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not events:
        return {"n": 0}
    raw_brier = _mean([float(e["raw_brier"]) for e in events])
    qm_brier = _mean([float(e["qm_brier"]) for e in events])
    raw_mae = _mean([float(e["raw_mae"]) for e in events])
    qm_mae = _mean([float(e["qm_mae"]) for e in events])
    raw_boundary = _mean([float(e["raw_boundary_rps"]) for e in events])
    qm_boundary = _mean([float(e["qm_boundary_rps"]) for e in events])
    raw_logloss = _mean([float(e["raw_logloss"]) for e in events])
    qm_logloss = _mean([float(e["qm_logloss"]) for e in events])
    raw_top_hit = _mean([1.0 if e.get("raw_top_label") == e.get("actual_label") else 0.0 for e in events])
    qm_top_hit = _mean([1.0 if e.get("qm_top_label") == e.get("actual_label") else 0.0 for e in events])
    return {
        "n": len(events),
        "market_day_count": len({str(e.get("event_date") or "") for e in events}),
        "raw_brier": raw_brier,
        "qm_brier": qm_brier,
        "brier_improvement": _pct_improvement(float(raw_brier), float(qm_brier)) if raw_brier is not None and qm_brier is not None else None,
        "raw_mae": raw_mae,
        "qm_mae": qm_mae,
        "mae_improvement": _pct_improvement(float(raw_mae), float(qm_mae)) if raw_mae is not None and qm_mae is not None else None,
        "raw_boundary_rps": raw_boundary,
        "qm_boundary_rps": qm_boundary,
        "boundary_rps_improvement": _pct_improvement(float(raw_boundary), float(qm_boundary)) if raw_boundary is not None and qm_boundary is not None else None,
        "raw_logloss": raw_logloss,
        "qm_logloss": qm_logloss,
        "logloss_improvement": _pct_improvement(float(raw_logloss), float(qm_logloss)) if raw_logloss is not None and qm_logloss is not None else None,
        "raw_top1_accuracy": raw_top_hit,
        "qm_top1_accuracy": qm_top_hit,
        "top1_accuracy_lift_pp": (
            (float(qm_top_hit) - float(raw_top_hit)) * 100.0
            if qm_top_hit is not None and raw_top_hit is not None
            else None
        ),
    }


def _segment_metrics(events: Sequence[Mapping[str, Any]], field: str) -> Dict[str, Any]:
    values = sorted({str(event.get(field) or "unknown") for event in events})
    out: Dict[str, Any] = {}
    for value in values:
        selected = [event for event in events if str(event.get(field) or "unknown") == value]
        out[value] = _metric_summary(selected)
    return out


def _climo_compare_events(events: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for event in events:
        if _to_float(event.get("qm_without_climo_brier")) is None:
            continue
        out.append(
            {
                **dict(event),
                "raw_brier": event.get("qm_without_climo_brier"),
                "qm_brier": event.get("qm_brier"),
                "raw_mae": event.get("qm_without_climo_mae"),
                "qm_mae": event.get("qm_mae"),
                "raw_boundary_rps": event.get("qm_without_climo_boundary_rps"),
                "qm_boundary_rps": event.get("qm_boundary_rps"),
                "raw_logloss": event.get("qm_without_climo_logloss"),
                "qm_logloss": event.get("qm_logloss"),
                "raw_top_label": event.get("qm_without_climo_top_label"),
                "qm_top_label": event.get("qm_top_label"),
            }
        )
    return out


def _thin_events(events: Sequence[Mapping[str, Any]], thin_minutes: float) -> Tuple[List[Dict[str, Any]], int]:
    if thin_minutes <= 0:
        return [dict(event) for event in events], 0
    out: List[Dict[str, Any]] = []
    skipped = 0
    last_by_event: Dict[Tuple[str, str], datetime] = {}
    min_delta = timedelta(minutes=float(thin_minutes))
    for event in events:
        key = (str(event.get("slug") or ""), str(event.get("event_date") or ""))
        ts = _parse_dt(event.get("created_at"))
        if ts is None:
            skipped += 1
            continue
        prev = last_by_event.get(key)
        if prev is not None and ts - prev < min_delta:
            skipped += 1
            continue
        out.append(dict(event))
        last_by_event[key] = ts
    return out, skipped


def _metric_passes(metrics: Mapping[str, Any], *, min_improvement: float, min_boundary_improvement: float, max_mae_degradation: float, max_logloss_degradation: float) -> bool:
    brier = _to_float(metrics.get("brier_improvement"))
    boundary = _to_float(metrics.get("boundary_rps_improvement"))
    mae = _to_float(metrics.get("mae_improvement"))
    logloss = _to_float(metrics.get("logloss_improvement"))
    return bool(
        brier is not None
        and brier >= float(min_improvement)
        and boundary is not None
        and boundary >= float(min_boundary_improvement)
        and (mae is None or mae >= -float(max_mae_degradation))
        and (logloss is None or logloss >= -float(max_logloss_degradation))
    )


def _metric_non_degrading(metrics: Mapping[str, Any], *, max_degradation: float = 0.01) -> bool:
    brier = _to_float(metrics.get("brier_improvement"))
    boundary = _to_float(metrics.get("boundary_rps_improvement"))
    mae = _to_float(metrics.get("mae_improvement"))
    logloss = _to_float(metrics.get("logloss_improvement"))
    floor = -max(0.0, float(max_degradation))
    return bool(
        brier is not None
        and brier >= floor
        and boundary is not None
        and boundary >= floor
        and mae is not None
        and mae >= floor
        and logloss is not None
        and logloss >= floor
    )


def _calibration_scope(args: argparse.Namespace) -> set[str]:
    raw = str(getattr(args, "calibration_dte_scope", "") or "d_minus_1,d0")
    values = {item.strip() for item in raw.split(",") if item.strip()}
    return values or {"d_minus_1", "d0"}


def _pending_snapshot_summary(
    conn: sqlite3.Connection,
    *,
    args: argparse.Namespace,
    city: str,
    station_id: str,
    loc_key: str,
    labels: Sequence[str],
    known_event_dates: Sequence[str],
    default_tz: str,
) -> Dict[str, Any]:
    pending_rows, pending_skips = _load_pending_prediction_rows(
        conn,
        city=city,
        station_id=station_id,
        known_event_dates=known_event_dates,
        market_var=str(args.market_var),
        market_unit=str(args.market_unit),
        allow_post_event_end=bool(args.allow_post_event_end),
    )
    skipped: List[Dict[str, str]] = []
    for reason, count in pending_skips.items():
        for _ in range(count):
            skipped.append({"event_date": "", "reason": reason})
    eligible: List[Dict[str, Any]] = []
    for row in pending_rows:
        event_date = _date_key(row.get("event_date"))
        created_at_text = str(row.get("created_at") or "")
        members, member_meta = _load_event_members_as_of(
            conn,
            loc_key=loc_key,
            event_date=event_date,
            market_var=str(args.market_var),
            market_unit=str(args.market_unit),
            as_of=created_at_text,
        )
        if not members:
            skipped.append({"event_date": event_date, "reason": "missing_model_members"})
            continue
        dte_bucket = _dte_bucket(row, default_tz=default_tz)
        calibration_in_scope = dte_bucket in _calibration_scope(args)
        history, history_meta = _load_station_temperature_qm_history_as_of(
            conn,
            loc_key=loc_key,
            station_id=station_id,
            market_var=str(args.market_var),
            market_unit=str(args.market_unit),
            source=str(args.source),
            max_days=int(args.history_max_days),
            target_event_date=event_date,
            as_of=created_at_text,
        )
        if calibration_in_scope:
            result = calibrate_members_and_bucket_probabilities(
                members,
                history,
                labels,
                rounding_rule=str(args.rounding_rule),
                market_unit=str(args.market_unit),
                target_event_date=event_date,
                min_sample_days=int(args.min_sample_days),
                seasonal_correction_enabled=not bool(args.disable_seasonal_correction),
                seasonal_correction_base=(
                    "raw" if str(args.calibration_mode) == "raw_seasonal_bias" else "qm"
                ),
                min_seasonal_sample_days=int(args.min_seasonal_sample_days),
                seasonal_shrinkage_days=float(args.seasonal_shrinkage_days),
                seasonal_max_abs_adjustment=float(args.seasonal_max_abs_adjustment),
            )
            qmeta = result.get("provenance") or {}
            qm_fit = qmeta.get("qm_fit") if isinstance(qmeta, Mapping) else {}
            qm_apply = qmeta.get("qm_apply") if isinstance(qmeta, Mapping) else {}
            seasonal = qmeta.get("seasonal_bias_correction") if isinstance(qmeta, Mapping) else {}
            seasonal_fit = seasonal.get("fit") if isinstance(seasonal, Mapping) else {}
            seasonal_apply = seasonal.get("apply") if isinstance(seasonal, Mapping) else {}
            if str(args.calibration_mode) == "raw_seasonal_bias":
                if not isinstance(seasonal_fit, Mapping) or seasonal_fit.get("status") != "ok":
                    skipped.append({"event_date": event_date, "reason": "seasonal_not_enough_samples"})
                    continue
                coverage = _to_float(
                    seasonal_apply.get("member_coverage") if isinstance(seasonal_apply, Mapping) else None
                )
            else:
                if not isinstance(qm_fit, Mapping) or qm_fit.get("status") != "ok":
                    skipped.append({"event_date": event_date, "reason": "qm_not_enough_samples"})
                    continue
                coverage = _to_float(qm_apply.get("member_coverage") if isinstance(qm_apply, Mapping) else None)
        else:
            coverage = 1.0
        if coverage is None or coverage < float(args.min_member_coverage):
            skipped.append({"event_date": event_date, "reason": "calibrated_member_coverage_below_min"})
            continue
        hours_to_peak = _hours_to_peak_window(row, peak_hour=float(args.peak_hour), default_tz=default_tz)
        eligible.append(
            {
                "snapshot_id": int(row.get("id") or 0),
                "slug": str(row.get("slug") or ""),
                "event_date": event_date,
                "created_at": created_at_text,
                "dte_bucket": dte_bucket,
                "calibration_in_scope": bool(calibration_in_scope),
                "hours_to_peak_window": hours_to_peak,
                "hours_to_peak_bucket": _hours_bucket(hours_to_peak),
                "member_count": int(member_meta.get("member_count") or 0),
                "member_cache_created_at_max": str(member_meta.get("created_at_max") or ""),
                "history_sample_days": int(history_meta.get("history_days") or 0),
                "history_event_date_max": str(history_meta.get("history_event_date_max") or ""),
                "calibration_member_coverage": coverage,
            }
        )
    thinned, thinned_count = _thin_events(eligible, float(args.thin_minutes))
    for _ in range(thinned_count):
        skipped.append({"event_date": "", "reason": "thinned_same_market_snapshot"})
    by_event_date = {
        event_date: sum(1 for row in thinned if str(row.get("event_date") or "") == event_date)
        for event_date in sorted({str(row.get("event_date") or "") for row in thinned})
    }
    return {
        "candidate_snapshot_rows": len(pending_rows),
        "eligible_before_thinning": len(eligible),
        "eligible_after_thinning": len(thinned),
        "market_day_count_after_thinning": len(by_event_date),
        "by_event_date": by_event_date,
        "skipped_count": len(skipped),
        "skipped_reasons": _reason_counts(skipped),
        "sample_rows": thinned[:10],
    }


def run_backtest(args: argparse.Namespace) -> Dict[str, Any]:
    if bool(getattr(args, "audit_climo_pp_impact", False)) or bool(getattr(args, "stratify_by_regime", False)):
        setattr(args, "include_climo_regime", True)
        setattr(args, "compare_without_climo", True)
    station_id, loc_key, default_labels = _city_defaults(args.city)
    default_tz = _city_tz(args.city)
    labels = [str(x) for x in (args.labels or default_labels)]
    db_path = Path(args.db)
    if not db_path.exists():
        return {"ok": False, "status": "db_missing", "db": str(db_path)}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    diagnostics: Dict[str, Any] = {
        "read_only": True,
        "writes_performed": False,
        "leakage_guards": {
            "sample_source": "event_runs.created_at_snapshots",
            "evaluation_label_source": "event_daily_temps_preferred_then_formal_closed_event_runs",
            "target_members_created_before_sample_created_at": True,
            "training_actuals_created_before_sample_created_at": True,
            "training_members_created_before_sample_created_at": True,
            "training_event_date_before_target_event_date": True,
            "taf_forecast_from_snapshot_payload_created_at_only": True,
            "seasonal_bias_trained_only_from_visible_history": True,
            "climo_regime_uses_only_visible_history_before_sample_created_at": bool(
                getattr(args, "include_climo_regime", False)
            ),
            "excludes_snapshot_rows_with_settlement_actual": not bool(args.allow_snapshot_settlement_actual),
            "excludes_closed_snapshot_rows": not bool(args.allow_closed_snapshots),
            "excludes_post_event_end_by_default": not bool(args.allow_post_event_end),
        },
    }
    pending_future_snapshots: Dict[str, Any] = {}
    try:
        events_all = _load_actual_events(
            conn,
            station_id=station_id,
            source=str(args.source),
            market_var=str(args.market_var),
            market_unit=str(args.market_unit),
        )
        events = events_all[-max(1, int(args.max_events)) :]
        event_dates = [str(event["event_date"]) for event in events]
        prediction_rows, prediction_skips = _load_prediction_rows(
            conn,
            city=str(args.city),
            station_id=station_id,
            event_dates=event_dates,
            market_var=str(args.market_var),
            market_unit=str(args.market_unit),
            allow_closed_snapshots=bool(args.allow_closed_snapshots),
            allow_snapshot_settlement_actual=bool(args.allow_snapshot_settlement_actual),
            allow_post_event_end=bool(args.allow_post_event_end),
        )
        evaluated: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []
        prediction_rows_before_thinning = len(prediction_rows)
        if float(args.thin_minutes) > 0:
            prediction_rows, prediction_pre_thinned_count = _thin_events(prediction_rows, float(args.thin_minutes))
        else:
            prediction_pre_thinned_count = 0
        for reason, count in prediction_skips.items():
            for _ in range(count):
                skipped.append({"event_date": "", "reason": reason})
        for _ in range(prediction_pre_thinned_count):
            skipped.append({"event_date": "", "reason": "pre_thinned_same_market_snapshot"})
        actual_by_date = {str(event["event_date"]): event for event in events}
        for row in prediction_rows:
            event_date = _date_key(row.get("event_date"))
            event = actual_by_date.get(event_date)
            if event is None:
                skipped.append({"event_date": event_date, "reason": "settlement_label_missing"})
                continue
            created_at_text = str(row.get("created_at") or "")
            created_dt = _parse_dt(created_at_text)
            if created_dt is None:
                skipped.append({"event_date": event_date, "reason": "created_at_missing"})
                continue
            members, member_meta = _load_event_members_as_of(
                conn,
                loc_key=loc_key,
                event_date=event_date,
                market_var=str(args.market_var),
                market_unit=str(args.market_unit),
                as_of=created_at_text,
            )
            if not members:
                skipped.append({"event_date": event_date, "reason": "missing_model_members"})
                continue
            actual_label = bucket_label_for_value(labels, float(event["actual"]), args.rounding_rule)
            if not actual_label:
                skipped.append({"event_date": event_date, "reason": "actual_bucket_unmatched"})
                continue
            hours_to_peak = _hours_to_peak_window(row, peak_hour=float(args.peak_hour), default_tz=default_tz)
            dte_bucket = _dte_bucket(row, default_tz=default_tz)
            calibration_in_scope = dte_bucket in _calibration_scope(args)
            history, history_meta = _load_station_temperature_qm_history_as_of(
                conn,
                loc_key=loc_key,
                station_id=station_id,
                market_var=str(args.market_var),
                market_unit=str(args.market_unit),
                source=str(args.source),
                max_days=int(args.history_max_days),
                target_event_date=event_date,
                as_of=created_at_text,
            )
            raw_probs, raw_meta = bucket_probabilities_from_members(
                members,
                labels,
                rounding_rule=str(args.rounding_rule),
            )
            calibration_mode = str(args.calibration_mode)
            if calibration_in_scope:
                result = calibrate_members_and_bucket_probabilities(
                    members,
                    history,
                    labels,
                    rounding_rule=str(args.rounding_rule),
                    market_unit=str(args.market_unit),
                    target_event_date=event_date,
                    min_sample_days=int(args.min_sample_days),
                    seasonal_correction_enabled=not bool(args.disable_seasonal_correction),
                    seasonal_correction_base=(
                        "raw" if calibration_mode == "raw_seasonal_bias" else "qm"
                    ),
                    min_seasonal_sample_days=int(args.min_seasonal_sample_days),
                    seasonal_shrinkage_days=float(args.seasonal_shrinkage_days),
                    seasonal_max_abs_adjustment=float(args.seasonal_max_abs_adjustment),
                )
                qm_probs = result.get("bucket_probabilities") or {}
                qmeta = result.get("provenance") or {}
                qm_fit = qmeta.get("qm_fit") if isinstance(qmeta, Mapping) else {}
                qm_apply = qmeta.get("qm_apply") if isinstance(qmeta, Mapping) else {}
                seasonal = qmeta.get("seasonal_bias_correction") if isinstance(qmeta, Mapping) else {}
                seasonal_fit = seasonal.get("fit") if isinstance(seasonal, Mapping) else {}
                seasonal_apply = seasonal.get("apply") if isinstance(seasonal, Mapping) else {}
                if calibration_mode == "raw_seasonal_bias":
                    if not isinstance(seasonal_fit, Mapping) or seasonal_fit.get("status") != "ok":
                        skipped.append({"event_date": event_date, "reason": "seasonal_not_enough_samples"})
                        continue
                    coverage = _to_float(
                        seasonal_apply.get("member_coverage") if isinstance(seasonal_apply, Mapping) else None
                    )
                else:
                    if not isinstance(qm_fit, Mapping) or qm_fit.get("status") != "ok":
                        skipped.append({"event_date": event_date, "reason": "qm_not_enough_samples"})
                        continue
                    coverage = _to_float(qm_apply.get("member_coverage") if isinstance(qm_apply, Mapping) else None)
            else:
                qm_probs = dict(raw_probs)
                coverage = 1.0
                qm_fit = {"status": "stage_out_of_scope_noop"}
                seasonal_apply = {"applied": False, "member_coverage": 0.0, "model_meta": {}}
            if coverage is None or coverage < float(args.min_member_coverage):
                skipped.append({"event_date": event_date, "reason": "calibrated_member_coverage_below_min"})
                continue
            qm_probs_without_climo = dict(qm_probs)
            climo_regime_context: Dict[str, Any] = {}
            climo_regime_probs: Dict[str, float] = {}
            climo_regime_evidence = 0.0
            climo_regime_evidence_raw = 0.0
            climo_regime_evidence_scale = 0.0
            climo_regime_pp_cap = 0.0
            climo_regime_pp_max_delta = 0.0
            climo_regime_pp_cap_stage = ""
            climo_regime_pp_cap_pass = True
            climo_regime_primary_regime = "neutral_proxy"
            climo_regime_auto_weight_allowed = False
            weather_features_for_climo = _extract_weather_features_from_payload(row.get("payload_json"))
            if hours_to_peak is not None:
                weather_features_for_climo["hours_to_peak_window"] = float(hours_to_peak)
            if bool(getattr(args, "include_climo_regime", False)):
                climo_regime_context = load_climo_regime_context(
                    db_path=db_path,
                    labels=labels,
                    target_event_date=event_date,
                    as_of=created_at_text,
                    city=str(row.get("city") or args.city),
                    station_id=station_id,
                    market_var=str(args.market_var),
                    market_unit=str(args.market_unit),
                    rounding_rule=str(args.rounding_rule),
                    weather_features=weather_features_for_climo,
                    days_to_event=hours_to_peak / 24.0 if hours_to_peak is not None else None,
                    min_samples=int(getattr(args, "climo_regime_min_samples", 3) or 3),
                    reference_path=str(getattr(args, "climo_regime_reference_path", "") or ""),
                    reference_min_samples=int(getattr(args, "climo_regime_reference_min_samples", 30) or 30),
                )
                probs = climo_regime_context.get("bucket_probabilities")
                if isinstance(probs, Mapping):
                    climo_regime_probs = {str(label): float(probs.get(label, 0.0) or 0.0) for label in labels}
                scale_meta = climo_regime_context.get("evidence_scale")
                if isinstance(scale_meta, Mapping):
                    climo_regime_evidence_scale = _to_float(scale_meta.get("scale")) or 0.0
                climo_regime_evidence = (
                    max(0.0, float(getattr(args, "climo_regime_evidence", 2.0) or 2.0))
                    * max(0.0, min(1.0, float(climo_regime_evidence_scale)))
                )
                climo_regime_evidence_raw = climo_regime_evidence
                features_for_cap = (
                    climo_regime_context.get("features")
                    if isinstance(climo_regime_context.get("features"), Mapping)
                    else weather_features_for_climo
                )
                if isinstance(features_for_cap, Mapping):
                    climo_regime_primary_regime = str(features_for_cap.get("primary_regime_label") or "neutral_proxy")
                    climo_regime_auto_weight_allowed = int(features_for_cap.get("climo_tmax_sample_count") or 0) >= 10
                if climo_regime_probs:
                    cap_meta = _climo_cap_for_event(
                        base_probs=qm_probs,
                        climo_probs=climo_regime_probs,
                        raw_evidence=climo_regime_evidence_raw,
                        base_evidence=float(getattr(args, "climo_regime_base_evidence", 36.0) or 36.0),
                        labels=labels,
                        features=features_for_cap if isinstance(features_for_cap, Mapping) else {},
                        days_to_event=hours_to_peak / 24.0 if hours_to_peak is not None else None,
                    )
                    climo_regime_evidence = float(cap_meta.get("evidence") or 0.0)
                    climo_regime_pp_cap = float(cap_meta.get("pp_cap") or 0.0)
                    climo_regime_pp_max_delta = float(cap_meta.get("pp_max_delta") or 0.0)
                    climo_regime_pp_cap_stage = str(cap_meta.get("pp_cap_stage") or "")
                    climo_regime_pp_cap_pass = climo_regime_pp_max_delta <= climo_regime_pp_cap + 1e-9
                if climo_regime_probs and climo_regime_evidence > 0:
                    qm_probs = _blend_with_climo(
                        qm_probs,
                        climo_regime_probs,
                        base_evidence=float(getattr(args, "climo_regime_base_evidence", 36.0) or 36.0),
                        climo_evidence=climo_regime_evidence,
                        labels=labels,
                    )
            raw_brier, raw_mae = _score(raw_probs, actual_label, labels)
            qm_brier, qm_mae = _score(qm_probs, actual_label, labels)
            qm_without_climo_brier, qm_without_climo_mae = _score(qm_probs_without_climo, actual_label, labels)
            raw_boundary = _boundary_score(raw_probs, actual_label, labels)
            qm_boundary = _boundary_score(qm_probs, actual_label, labels)
            qm_without_climo_boundary = _boundary_score(qm_probs_without_climo, actual_label, labels)
            taf_peak_c = _extract_taf_peak_c_from_payload(row.get("payload_json"))
            taf_probs = (
                _taf_bucket_probs(
                    labels,
                    float(taf_peak_c),
                    sigma_c=float(args.taf_sigma_c),
                    rounding_rule="rounded_degrees",
                )
                if taf_peak_c is not None
                else {}
            )
            evaluated.append(
                {
                    "snapshot_id": int(row.get("id") or 0),
                    "slug": str(row.get("slug") or ""),
                    "event_date": event_date,
                    "created_at": created_at_text,
                    "dte_bucket": dte_bucket,
                    "hours_to_peak_window": hours_to_peak,
                    "hours_to_peak_bucket": _hours_bucket(hours_to_peak),
                    "actual": float(event["actual"]),
                    "actual_label": actual_label,
                    "actual_source": str(event.get("resolution_source") or ""),
                    "raw_brier": raw_brier,
                    "qm_brier": qm_brier,
                    "qm_without_climo_brier": qm_without_climo_brier,
                    "raw_mae": raw_mae,
                    "qm_mae": qm_mae,
                    "qm_without_climo_mae": qm_without_climo_mae,
                    "raw_boundary_rps": raw_boundary,
                    "qm_boundary_rps": qm_boundary,
                    "qm_without_climo_boundary_rps": qm_without_climo_boundary,
                    "raw_logloss": _logloss(raw_probs, actual_label),
                    "qm_logloss": _logloss(qm_probs, actual_label),
                    "qm_without_climo_logloss": _logloss(qm_probs_without_climo, actual_label),
                    "raw_top_label": _top_label(raw_probs),
                    "qm_top_label": _top_label(qm_probs),
                    "qm_without_climo_top_label": _top_label(qm_probs_without_climo),
                    "climo_regime_evidence": climo_regime_evidence,
                    "climo_regime_evidence_raw": climo_regime_evidence_raw,
                    "climo_regime_evidence_scale": climo_regime_evidence_scale,
                    "climo_regime_pp_cap": climo_regime_pp_cap,
                    "climo_regime_pp_max_delta": climo_regime_pp_max_delta,
                    "climo_regime_pp_cap_stage": climo_regime_pp_cap_stage,
                    "climo_regime_pp_cap_pass": climo_regime_pp_cap_pass,
                    "climo_regime_pp_delta": _prob_max_abs_delta(qm_probs, qm_probs_without_climo, labels),
                    "climo_regime_primary_regime": climo_regime_primary_regime,
                    "regime_sample_count": (
                        int((climo_regime_context.get("features") or {}).get("climo_tmax_sample_count") or 0)
                        if isinstance(climo_regime_context.get("features"), Mapping)
                        else 0
                    ),
                    "regime_auto_weight_allowed": bool(climo_regime_auto_weight_allowed),
                    "temperature_trajectory_priority": True,
                    "climo_regime_status": str(climo_regime_context.get("status") or ""),
                    "climo_regime_features": climo_regime_context.get("features") if isinstance(climo_regime_context, Mapping) else {},
                    "climo_regime_probs": climo_regime_probs,
                    "taf_peak_c": taf_peak_c,
                    "taf_visible": bool(taf_peak_c is not None),
                    "taf_probs": taf_probs,
                    "qm_probs": dict(qm_probs),
                    "qm_probs_without_climo": dict(qm_probs_without_climo),
                    "member_count": int(raw_meta.get("member_count") or 0),
                    "member_cache_created_at_max": str(member_meta.get("created_at_max") or ""),
                    "history_sample_days": int(history_meta.get("history_days") or 0),
                    "history_event_date_min": str(history_meta.get("history_event_date_min") or ""),
                    "history_event_date_max": str(history_meta.get("history_event_date_max") or ""),
                    "history_member_created_at_max": str(history_meta.get("member_created_at_max") or ""),
                    "history_actual_created_at_max": str(history_meta.get("actual_created_at_max") or ""),
                    "qm_member_coverage": coverage,
                    "qm_status": str(qm_fit.get("status") or ""),
                    "calibration_mode": calibration_mode,
                    "calibration_in_scope": bool(calibration_in_scope),
                    "calibration_scope": sorted(_calibration_scope(args)),
                    "calibration_member_coverage": coverage,
                    "seasonal_bias_enabled": not bool(args.disable_seasonal_correction),
                    "seasonal_bias_applied": bool(
                        seasonal_apply.get("applied") if isinstance(seasonal_apply, Mapping) else False
                    ),
                    "seasonal_bias_member_coverage": _to_float(
                        seasonal_apply.get("member_coverage") if isinstance(seasonal_apply, Mapping) else None
                    ),
                    "seasonal_bias_model_meta": (
                        seasonal_apply.get("model_meta") if isinstance(seasonal_apply, Mapping) else {}
                    ),
                }
            )
        pending_future_snapshots = _pending_snapshot_summary(
            conn,
            args=args,
            city=str(args.city),
            station_id=station_id,
            loc_key=loc_key,
            labels=labels,
            known_event_dates=event_dates,
            default_tz=default_tz,
        )
    finally:
        conn.close()

    evaluated_raw_count = len(evaluated)
    evaluated, thinned_count = _thin_events(evaluated, float(args.thin_minutes))
    for _ in range(thinned_count):
        skipped.append({"event_date": "", "reason": "thinned_same_market_snapshot"})
    n = len(evaluated)
    overall_metrics = _metric_summary(evaluated)
    dte_segments = _segment_metrics(evaluated, "dte_bucket")
    hours_segments = _segment_metrics(evaluated, "hours_to_peak_bucket")
    climo_compare_events = _climo_compare_events(evaluated) if bool(getattr(args, "compare_without_climo", False)) else []
    climo_compare_metrics = _metric_summary(climo_compare_events) if climo_compare_events else {"n": 0}
    climo_compare_dte_segments = _segment_metrics(climo_compare_events, "dte_bucket") if climo_compare_events else {}
    climo_compare_hours_segments = (
        _segment_metrics(climo_compare_events, "hours_to_peak_bucket") if climo_compare_events else {}
    )
    taf_weight_calibration = _taf_calibration_report(evaluated, labels=labels, args=args)
    climo_regime_weight_calibration = (
        _climo_regime_weight_calibration_report(evaluated, labels=labels, args=args)
        if bool(getattr(args, "include_climo_regime", False))
        else {"status": "disabled"}
    )
    climo_pp_impact_audit = (
        _climo_pp_impact_audit(evaluated)
        if bool(getattr(args, "audit_climo_pp_impact", False))
        else {"status": "disabled", "climo_pp_cap_pass": True, "temperature_trajectory_priority": True}
    )
    regime_stratification = (
        _regime_stratification_report(evaluated)
        if bool(getattr(args, "stratify_by_regime", False))
        else {"status": "disabled"}
    )
    required_segment_blockers: List[str] = []
    for segment_name in ("d_minus_1", "d0"):
        metrics = dte_segments.get(segment_name)
        if not isinstance(metrics, Mapping) or int(metrics.get("n") or 0) < int(args.min_segment_samples):
            required_segment_blockers.append(f"{segment_name}_sample_count_below_min")
            continue
        if not _metric_non_degrading(metrics, max_degradation=float(args.max_core_degradation)):
            required_segment_blockers.append(f"{segment_name}_metrics_below_gate")
    required_peak_segment_blockers: List[str] = []
    if bool(args.stratify_peak_window):
        for segment_name, metrics in sorted(hours_segments.items()):
            if str(segment_name) in {"unknown", "peak_or_after"}:
                continue
            if not isinstance(metrics, Mapping) or int(metrics.get("n") or 0) < int(args.min_segment_samples):
                continue
            if not _metric_non_degrading(metrics, max_degradation=float(args.max_core_degradation)):
                required_peak_segment_blockers.append(f"{segment_name}_hours_metrics_below_gate")
    blockers: List[str] = []
    warnings: List[str] = []
    climo_audit_mode = bool(getattr(args, "audit_climo_pp_impact", False))
    if n < int(args.min_events):
        blockers.append("sample_count_below_min")
    if int(overall_metrics.get("market_day_count") or 0) < int(args.min_market_days):
        if bool(getattr(args, "require_min_market_days", False)):
            blockers.append("market_day_count_below_min")
        else:
            warnings.append("market_day_count_below_min_metrics_only_gate_used")
    legacy_overall_gate_failed = not _metric_passes(
        overall_metrics,
        min_improvement=float(args.min_improvement),
        min_boundary_improvement=float(args.min_boundary_improvement),
        max_mae_degradation=float(args.max_mae_degradation),
        max_logloss_degradation=float(args.max_logloss_degradation),
    )
    if legacy_overall_gate_failed and not climo_audit_mode:
        blockers.append("overall_metrics_below_gate")
    elif legacy_overall_gate_failed:
        warnings.append("legacy_qm_vs_raw_overall_gate_below_target_not_blocking_climo_audit")
    if bool(getattr(args, "audit_climo_pp_impact", False)) and not bool(climo_pp_impact_audit.get("climo_pp_cap_pass")):
        blockers.append("climo_pp_cap_failed")
    if climo_audit_mode:
        if climo_compare_metrics and not _metric_non_degrading(
            climo_compare_metrics,
            max_degradation=float(args.max_core_degradation),
        ):
            blockers.append("climo_compare_core_metrics_below_gate")
        for segment_name, metrics in sorted(climo_compare_dte_segments.items()):
            if str(segment_name) in {"unknown", "post_event_local", "peak_or_after"}:
                continue
            if not isinstance(metrics, Mapping) or int(metrics.get("n") or 0) < int(args.min_segment_samples):
                continue
            if not _metric_non_degrading(metrics, max_degradation=float(args.max_core_degradation)):
                blockers.append(f"climo_compare_{segment_name}_metrics_below_gate")
        for segment_name, metrics in sorted(climo_compare_hours_segments.items()):
            if str(segment_name) in {"unknown", "peak_or_after"}:
                continue
            if not isinstance(metrics, Mapping) or int(metrics.get("n") or 0) < int(args.min_segment_samples):
                continue
            if not _metric_non_degrading(metrics, max_degradation=float(args.max_core_degradation)):
                blockers.append(f"climo_compare_{segment_name}_hours_metrics_below_gate")
        warnings.extend(required_segment_blockers)
        warnings.extend(required_peak_segment_blockers)
    else:
        blockers.extend(required_segment_blockers)
        blockers.extend(required_peak_segment_blockers)
    ok = not blockers
    if ok:
        status = "ok"
    else:
        status = "gate_failed"
    verify_markers = [
        f"climo_pp_max_delta={float(climo_pp_impact_audit.get('climo_pp_max_delta') or 0.0):.6f}",
        f"climo_pp_cap_pass={'true' if bool(climo_pp_impact_audit.get('climo_pp_cap_pass')) else 'false'}",
        "temperature_trajectory_priority=true",
    ]
    if bool(getattr(args, "stratify_by_regime", False)):
        verify_markers.append(
            f"regime_sample_count={int(regime_stratification.get('regime_sample_count') or 0)}"
        )
        segments = regime_stratification.get("segments") if isinstance(regime_stratification.get("segments"), Mapping) else {}
        if any(
            isinstance(segment, Mapping)
            and int(segment.get("regime_sample_count") or 0) < 10
            and not bool(segment.get("regime_auto_weight_allowed"))
            for segment in segments.values()
        ):
            verify_markers.append("regime_auto_weight_allowed=false")
    return {
        "ok": ok,
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "city": str(args.city),
        "station_id": station_id,
        "loc_key": loc_key,
        "source": str(args.source),
        "labels": labels,
        "min_events": int(args.min_events),
        "min_market_days": int(args.min_market_days),
        "min_segment_samples": int(args.min_segment_samples),
        "evaluated_events": n,
        "evaluated_snapshots": n,
        "evaluated_snapshots_before_thinning": evaluated_raw_count,
        "evaluated_market_days": int(overall_metrics.get("market_day_count") or 0),
        "available_actual_events": len(events_all),
        "candidate_snapshot_rows": len(prediction_rows),
        "candidate_snapshot_rows_before_pre_thinning": prediction_rows_before_thinning,
        "actual_source_counts": {
            source: sum(1 for event in evaluated if str(event.get("actual_source") or "") == source)
            for source in sorted({str(event.get("actual_source") or "") for event in evaluated})
        },
        "skipped_count": len(skipped),
        "skipped_reasons": _reason_counts(skipped),
        "metrics": overall_metrics | {
            "target_brier_improvement": float(args.min_improvement),
            "target_boundary_rps_improvement": float(args.min_boundary_improvement),
            "max_mae_degradation": float(args.max_mae_degradation),
            "max_logloss_degradation": float(args.max_logloss_degradation),
            "boundary_metric": "ranked_probability_score",
            "required_dte_segments": ["d_minus_1", "d0"],
            "required_dte_segment_min_brier_improvement": -float(args.max_core_degradation),
            "required_dte_segment_min_boundary_rps_improvement": -float(args.max_core_degradation),
            "required_dte_segment_min_mae_improvement": -float(args.max_core_degradation),
            "required_dte_segment_min_logloss_improvement": -float(args.max_core_degradation),
            "required_dte_segment_max_core_degradation": float(args.max_core_degradation),
            "required_peak_window_segment_min_brier_improvement": -float(args.max_core_degradation),
            "required_peak_window_segment_min_boundary_rps_improvement": -float(args.max_core_degradation),
            "required_peak_window_segment_min_mae_improvement": -float(args.max_core_degradation),
            "required_peak_window_segment_min_logloss_improvement": -float(args.max_core_degradation),
            "required_peak_window_segment_max_core_degradation": float(args.max_core_degradation),
        },
        "stratified_by_dte": dte_segments,
        "stratified_by_lead_time": dte_segments,
        "stratified_by_hours_to_peak": hours_segments,
        "stratified_by_peak_window": hours_segments,
        "climo_regime_compare": {
            "enabled": bool(getattr(args, "include_climo_regime", False)),
            "compare_without_climo": bool(getattr(args, "compare_without_climo", False)),
            "field": "q_bayes_climo_regime_*",
            "role": "slow variable background layer / weak prior / calibration stratification feature",
            "display_label": "气候/环流弱参考，不覆盖实况温度轨迹",
            "potential_label": "支持升温潜力 ≠ 已兑现升温",
            "trajectory_priority_label": "实况温度轨迹优先",
            "climo_pp_max_delta": climo_pp_impact_audit.get("climo_pp_max_delta"),
            "climo_pp_cap_pass": climo_pp_impact_audit.get("climo_pp_cap_pass"),
            "temperature_trajectory_priority": True,
            "created_at_guard": "uses_only_settled_history_and_payload_features_visible_before_sample_created_at",
            "metrics": climo_compare_metrics,
            "stratified_by_dte": climo_compare_dte_segments,
            "stratified_by_lead_time": climo_compare_dte_segments,
            "stratified_by_hours_to_peak": climo_compare_hours_segments,
            "stratified_by_peak_window": climo_compare_hours_segments,
        },
        "climo_pp_impact_audit": climo_pp_impact_audit,
        "stratified_by_regime": regime_stratification,
        "verify_markers": verify_markers,
        "climo_regime_weight_calibration": climo_regime_weight_calibration,
        "taf_weight_calibration": taf_weight_calibration,
        "promotion_gate": {
            "qm_or_local_shadow_allowed": ok,
            "chengdu_small_whitelist_allowed": ok,
            "calibration_dte_scope_allowed": sorted(_calibration_scope(args)),
            "d_minus_1_calibration_allowed": "d_minus_1" in _calibration_scope(args),
            "d0_calibration_allowed": "d0" in _calibration_scope(args),
            "reason": "passed_strict_created_at_walk_forward" if ok else "blocked_until_strict_walk_forward_passes",
            "blockers": blockers,
            "warnings": warnings,
        },
        "pending_future_snapshots": pending_future_snapshots,
        "method": {
            "type": "created_at_walk_forward_qm_bucket_backtest",
            "walk_forward": bool(args.walk_forward),
            "strict_created_at": bool(args.strict_created_at),
            "stratify_d0_d1": bool(args.stratify_d0_d1),
            "stratify_peak_window": bool(args.stratify_peak_window),
            "thin_minutes": float(args.thin_minutes),
            "peak_hour_local_fallback": float(args.peak_hour),
            "default_tz": default_tz,
            "history_max_days": int(args.history_max_days),
            "min_sample_days": int(args.min_sample_days),
            "min_market_days": int(args.min_market_days),
            "require_min_market_days": bool(args.require_min_market_days),
            "min_member_coverage": float(args.min_member_coverage),
            "overall_gate": {
                "min_brier_improvement": float(args.min_improvement),
                "min_boundary_rps_improvement": float(args.min_boundary_improvement),
                "max_mae_degradation": float(args.max_mae_degradation),
                "max_logloss_degradation": float(args.max_logloss_degradation),
            },
            "required_dte_segment_gate": {
                "segments": ["d_minus_1", "d0"],
                "min_brier_improvement": -float(args.max_core_degradation),
                "min_boundary_rps_improvement": -float(args.max_core_degradation),
                "min_mae_improvement": -float(args.max_core_degradation),
                "min_logloss_improvement": -float(args.max_core_degradation),
                "max_core_degradation": float(args.max_core_degradation),
            },
            "required_peak_window_segment_gate": {
                "enabled": bool(args.stratify_peak_window),
                "min_segment_samples": int(args.min_segment_samples),
                "ignored_segments": ["unknown", "peak_or_after"],
                "min_brier_improvement": -float(args.max_core_degradation),
                "min_boundary_rps_improvement": -float(args.max_core_degradation),
                "min_mae_improvement": -float(args.max_core_degradation),
                "min_logloss_improvement": -float(args.max_core_degradation),
                "max_core_degradation": float(args.max_core_degradation),
            },
            "calibration_mode": str(args.calibration_mode),
            "calibration_dte_scope": sorted(_calibration_scope(args)),
            "seasonal_correction_enabled": not bool(args.disable_seasonal_correction),
            "min_seasonal_sample_days": int(args.min_seasonal_sample_days),
            "seasonal_shrinkage_days": float(args.seasonal_shrinkage_days),
            "seasonal_max_abs_adjustment": float(args.seasonal_max_abs_adjustment),
            "taf_weight_calibration": {
                "weight_grid": _parse_float_grid(getattr(args, "taf_weight_grid", "")),
                "sigma_c": float(args.taf_sigma_c),
                "min_samples": int(args.taf_min_samples),
                "baseline_evidence": float(args.taf_baseline_evidence),
            },
            "climo_regime": {
                "enabled": bool(getattr(args, "include_climo_regime", False)),
                "compare_without_climo": bool(getattr(args, "compare_without_climo", False)),
                "evidence": float(getattr(args, "climo_regime_evidence", 2.0) or 2.0),
                "base_evidence": float(getattr(args, "climo_regime_base_evidence", 36.0) or 36.0),
                "min_samples": int(getattr(args, "climo_regime_min_samples", 3) or 3),
                "reference_path": str(getattr(args, "climo_regime_reference_path", "") or ""),
                "reference_min_samples": int(getattr(args, "climo_regime_reference_min_samples", 30) or 30),
                "reference_created_at_guard": "reference_uses_only_years_before_target_event_year",
            },
        },
        "diagnostics": diagnostics,
        "events": evaluated,
        "skipped": skipped[:50],
    }


def _reason_counts(items: Sequence[Mapping[str, str]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        reason = str(item.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--walk-forward", action="store_true", help="Evaluate event_run snapshots as created_at walk-forward samples.")
    parser.add_argument("--strict-created-at", action="store_true", help="Require all model/history training data to be visible before each sample created_at.")
    parser.add_argument("--created-at-safe", dest="strict_created_at", action="store_true", help="Alias for --strict-created-at.")
    parser.add_argument("--stratify-d0-d1", action="store_true", help="Emit D-1/D0 stratified metrics.")
    parser.add_argument("--stratify-by-lead-time", dest="stratify_d0_d1", action="store_true", help="Alias for D-3/D-2/D-1/D0 stratification.")
    parser.add_argument("--stratify-peak-window", action="store_true", help="Emit metrics by hours to local peak window.")
    parser.add_argument("--stratify-by-peak-window", dest="stratify_peak_window", action="store_true", help="Alias for --stratify-peak-window.")
    parser.add_argument("--city", default="Chengdu")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--source", default="wunderground", choices=["wunderground", "all"])
    parser.add_argument("--market-var", default="temperature_2m_max")
    parser.add_argument("--market-unit", default="C")
    parser.add_argument("--rounding-rule", default="whole_degrees")
    parser.add_argument("--min-events", type=int, default=30)
    parser.add_argument("--min-market-days", type=int, default=5)
    parser.add_argument("--require-min-market-days", action="store_true")
    parser.add_argument("--min-segment-samples", type=int, default=5)
    parser.add_argument("--max-events", type=int, default=365)
    parser.add_argument("--min-sample-days", type=int, default=60)
    parser.add_argument("--history-max-days", type=int, default=365)
    parser.add_argument("--min-member-coverage", type=float, default=0.95)
    parser.add_argument("--max-core-degradation", type=float, default=0.01)
    parser.add_argument("--min-improvement", type=float, default=-0.01)
    parser.add_argument("--min-boundary-improvement", type=float, default=-0.01)
    parser.add_argument("--max-mae-degradation", type=float, default=0.01)
    parser.add_argument("--max-logloss-degradation", type=float, default=0.01)
    parser.add_argument(
        "--calibration-mode",
        default="raw_seasonal_bias",
        choices=["raw_seasonal_bias", "qm_seasonal"],
    )
    parser.add_argument("--calibration-dte-scope", default="d0")
    parser.add_argument("--disable-seasonal-correction", action="store_true")
    parser.add_argument("--min-seasonal-sample-days", type=int, default=20)
    parser.add_argument("--seasonal-shrinkage-days", type=float, default=30.0)
    parser.add_argument("--seasonal-max-abs-adjustment", type=float, default=3.0)
    parser.add_argument("--taf-weight-grid", default="0,6,12,18,24,30,36")
    parser.add_argument("--taf-sigma-c", type=float, default=0.8)
    parser.add_argument("--taf-min-samples", type=int, default=5)
    parser.add_argument("--taf-baseline-evidence", type=float, default=36.0)
    parser.add_argument("--include-climo-regime", action="store_true")
    parser.add_argument("--compare-without-climo", action="store_true")
    parser.add_argument("--audit-climo-pp-impact", action="store_true")
    parser.add_argument("--stratify-by-regime", action="store_true")
    parser.add_argument("--climo-regime-evidence", type=float, default=2.0)
    parser.add_argument("--climo-regime-base-evidence", type=float, default=36.0)
    parser.add_argument("--climo-regime-min-samples", type=int, default=3)
    parser.add_argument("--climo-regime-weight-grid", default="0,0.25,0.5,1,2,3,4,6,8,10,12")
    parser.add_argument("--climo-regime-reference-path", default="")
    parser.add_argument("--climo-regime-reference-min-samples", type=int, default=30)
    parser.add_argument("--thin-minutes", type=float, default=30.0)
    parser.add_argument("--peak-hour", type=float, default=16.0)
    parser.add_argument("--allow-closed-snapshots", action="store_true")
    parser.add_argument("--allow-snapshot-settlement-actual", action="store_true")
    parser.add_argument("--allow-post-event-end", action="store_true")
    parser.add_argument("--labels", nargs="*")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = run_backtest(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
