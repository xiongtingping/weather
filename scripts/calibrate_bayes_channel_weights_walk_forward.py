#!/usr/bin/env python3
"""Created-at-safe walk-forward calibration for Bayes evidence weights.

The script is read-only with respect to fact tables. It evaluates persisted
probability snapshots only, then reports whether S_wu_fcst / S_taf / S_obs /
S_phys have enough leak-safe evidence to receive non-zero weights.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "bayes_channel_weight_walk_forward_latest.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_physical_likelihood import (  # noqa: E402
    _hit,
    _representative_c,
    _safe_json,
    _source_key,
    _to_float,
)
from bayesian_likelihood_runtime import _convert_temp  # noqa: E402
from bayesian_probability_runtime import _normal_bucket_probs  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


EPS = 1e-12
CHANNELS: tuple[tuple[str, str], ...] = (
    ("S_wu_fcst", "wu_fcst"),
    ("S_taf", "taf"),
    ("S_obs", "obs"),
    ("S_phys", "phys"),
)
ZERO_WEIGHTS = {key: 0.0 for key, _ in CHANNELS}


def _parse_dt(value: Any) -> datetime | None:
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


def _event_date(value: Any) -> str:
    return str(value or "").split("T", 1)[0]


def _zone(name: str) -> timezone:
    raw = str(name or "").strip() or "UTC"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(raw)  # type: ignore[return-value]
        except Exception:
            pass
    match = re.fullmatch(r"Etc/GMT([+-]\d{1,2})", raw)
    if match:
        return timezone(timedelta(hours=-int(match.group(1))))
    return timezone.utc


def _infer_tz(row: Mapping[str, Any], payload: Mapping[str, Any] | None = None) -> str:
    payload = payload or {}
    raw = str(row.get("tz") or payload.get("tz") or "").strip()
    source = str(row.get("resolution_source") or payload.get("resolution_source") or "").lower()
    if raw and raw.upper() != "UTC":
        return raw
    if "/history/daily/cn/" in source:
        return "Asia/Shanghai"
    if "/history/daily/us/co/" in source:
        return "America/Denver"
    if "/history/daily/us/wa/" in source:
        return "America/Los_Angeles"
    if "/history/daily/us/" in source:
        return "America/New_York"
    lon = _to_float(row.get("lon") or payload.get("lon") or payload.get("city_lon") or payload.get("station_lon"))
    if lon is not None:
        offset_hours = int(round(float(lon) / 15.0))
        return f"Etc/GMT{(-offset_hours):+d}"
    return "UTC"


def _hours_to_peak_window(row: Mapping[str, Any], payload: Mapping[str, Any], *, peak_hour: float) -> float | None:
    created_at = _parse_dt(row.get("created_at"))
    event_date = _event_date(row.get("event_date") or payload.get("event_date"))
    if created_at is None or not event_date:
        return None
    try:
        day = date.fromisoformat(event_date)
    except ValueError:
        return None
    hour = int(math.floor(float(peak_hour)))
    minute = int(round((float(peak_hour) - hour) * 60.0))
    peak_local = datetime.combine(day, time(hour, minute), tzinfo=_zone(_infer_tz(row, payload)))
    return (peak_local.astimezone(timezone.utc) - created_at).total_seconds() / 3600.0


def _time_bucket(hours_to_peak: float | None) -> str:
    if hours_to_peak is None:
        return "unknown"
    if hours_to_peak > 4.0:
        return "gt_4h"
    if hours_to_peak > 2.0:
        return "2_to_4h"
    if hours_to_peak > 0.0:
        return "0_to_2h"
    return "peak_or_after"


def _lead_day_bucket(row: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    created_at = _parse_dt(row.get("created_at"))
    event_date = _event_date(row.get("event_date") or payload.get("event_date"))
    if created_at is None or not event_date:
        return "unknown"
    try:
        day = date.fromisoformat(event_date)
    except ValueError:
        return "unknown"
    created_local_day = created_at.astimezone(_zone(_infer_tz(row, payload))).date()
    delta = (day - created_local_day).days
    if delta <= 0:
        return "D0"
    if delta == 1:
        return "D-1"
    if delta == 2:
        return "D-2"
    return "D-3+"


def _normalize(values: Sequence[float | None]) -> list[float]:
    cleaned: list[float] = []
    for value in values:
        num = _to_float(value)
        cleaned.append(max(0.0, float(num)) if num is not None else 0.0)
    total = sum(cleaned)
    if total <= EPS:
        return []
    return [value / total for value in cleaned]


def _field_prob(item: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = _to_float(item.get(key))
        if value is not None:
            return float(value)
    q_obj = item.get("q")
    if isinstance(q_obj, Mapping):
        for key in keys:
            value = _to_float(q_obj.get(key))
            if value is not None:
                return float(value)
    meta = item.get("q_bayes_meta")
    if isinstance(meta, Mapping):
        for key in keys:
            value = _to_float(meta.get(key))
            if value is not None:
                return float(value)
    return None


def _deep_get(obj: Mapping[str, Any], key: str) -> Any:
    current: Any = obj
    for part in str(key or "").split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _deep_float(obj: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = _to_float(_deep_get(obj, key) if "." in str(key) else obj.get(key))
        if value is not None:
            return float(value)
    return None


def _normal_peak_probs(
    labels: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    *,
    field_keys: Sequence[str],
    sigma_keys: Sequence[str],
    default_sigma_c: float,
    rounding_rule: str | None,
) -> list[float]:
    peak: float | None = None
    sigma: float | None = None
    peak = _deep_float(payload, field_keys)
    if peak is None:
        for item in rows:
            peak = _deep_float(item, field_keys)
            if peak is not None:
                break
    if peak is None:
        return []
    sigma = _deep_float(payload, sigma_keys)
    if sigma is not None and sigma <= 0:
        sigma = None
    if sigma is None:
        for item in rows:
            sigma = _deep_float(item, sigma_keys)
            if sigma is not None and sigma <= 0:
                sigma = None
            if sigma is not None:
                break
    probs = _normal_bucket_probs(
        list(labels),
        mu=float(peak),
        sigma=max(0.05, float(sigma or default_sigma_c)),
        rounding_rule=rounding_rule,
    )
    return _normalize(probs)


def _phys_quality_meta(payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    weather_ref = str(payload.get("weather_likelihood_refreshed_at") or "").strip()
    features = payload.get("weather_likelihood_features")
    feature_count = len(features) if isinstance(features, Mapping) else 0
    feature_quality = str(payload.get("weather_likelihood_feature_quality") or "").strip().lower()
    evidence_values = [
        _to_float(row.get("q_bayes_physical_likelihood_evidence"))
        for row in rows
        if isinstance(row, Mapping)
    ]
    max_evidence = max([float(v) for v in evidence_values if v is not None] or [0.0])
    has_scale_details = any(
        isinstance(row.get("q_bayes_physical_likelihood_evidence_scale_details"), Mapping)
        for row in rows
        if isinstance(row, Mapping)
    )
    phys_enabled = any(
        bool(row.get("q_bayes_physical_likelihood_enabled"))
        for row in rows
        if isinstance(row, Mapping)
    )
    legacy_empty_features = bool(max_evidence > 0 and not weather_ref and feature_count <= 0)
    reasons: list[str] = []
    if not weather_ref:
        reasons.append("weather_likelihood_refreshed_at_missing")
    if feature_count <= 0:
        reasons.append("weather_likelihood_features_empty")
    if feature_quality and feature_quality not in {"complete", "partial"}:
        reasons.append(f"feature_quality_not_candidate:{feature_quality}")
    if not feature_quality:
        reasons.append("feature_quality_missing")
    if not has_scale_details:
        reasons.append("evidence_scale_details_missing")
    if legacy_empty_features:
        reasons.append("legacy_s_phys_evidence_with_empty_features")
    candidate = bool(
        weather_ref
        and feature_count > 0
        and feature_quality in {"complete", "partial"}
        and has_scale_details
        and not legacy_empty_features
    )
    return {
        "phys_quality_candidate": candidate,
        "phys_quality_reasons": reasons or ["candidate_quality_passed"],
        "weather_likelihood_refreshed_at_present": bool(weather_ref),
        "weather_likelihood_feature_count": int(feature_count),
        "weather_likelihood_feature_quality": feature_quality,
        "phys_evidence_scale_details_present": bool(has_scale_details),
        "phys_persisted_enabled": bool(phys_enabled),
        "phys_persisted_evidence_max": float(max_evidence),
        "phys_legacy_empty_features": bool(legacy_empty_features),
    }


def _extract_event(
    row: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    peak_hour: float,
    baseline_keys: Sequence[str],
) -> dict[str, Any] | None:
    payload = _safe_json(row.get("payload_json"))
    if not isinstance(payload, Mapping):
        return None
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return None
    actual_value = _to_float(actual.get("actual"))
    actual_unit = str(actual.get("unit") or row.get("market_unit") or payload.get("market_unit") or "C")
    if actual_value is None:
        return None

    labels: list[str] = []
    reps: list[float] = []
    hits: list[bool] = []
    payload_rows: list[Mapping[str, Any]] = []
    baseline_raw: list[float | None] = []
    obs_raw: list[float | None] = []
    phys_raw: list[float | None] = []
    wu_raw: list[float | None] = []
    taf_raw: list[float | None] = []

    for item in raw_rows:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("outcome") or item.get("label") or "").strip()
        rep = _representative_c(label)
        if not label or rep is None:
            continue
        base = _field_prob(item, baseline_keys)
        labels.append(label)
        reps.append(float(rep))
        hits.append(_hit(label, float(actual_value), actual_unit))
        payload_rows.append(item)
        baseline_raw.append(base)
        obs_raw.append(_field_prob(item, ("q_observed_nowcast", "observed_nowcast_prob")))
        phys_raw.append(
            _field_prob(
                item,
                (
                    "q_bayes_independent_physics_residual_prob",
                    "q_bayes_physical_likelihood_prob",
                    "physical_likelihood_prob",
                ),
            )
        )
        wu_raw.append(_field_prob(item, ("q_bayes_wu_forecast_prob", "q_wu_forecast_peak")))
        taf_raw.append(_field_prob(item, ("q_bayes_taf_peak_prob", "q_taf_peak")))

    if len(labels) < 2 or not any(hits):
        return None
    baseline = _normalize(baseline_raw)
    if not baseline:
        return None
    rounding_rule = str(row.get("rounding_rule") or payload.get("rounding_rule") or "").strip() or None
    wu_probs = _normalize(wu_raw)
    if not wu_probs:
        wu_probs = _normal_peak_probs(
            labels,
            payload_rows,
            payload,
            field_keys=(
                "wu_forecast_peak.value_c",
                "wu_forecast_peak.value",
                "weather_likelihood_features.wu_forecast_peak_c",
                "weather_likelihood_features.settlement_source_forecast_peak_c",
                "q_bayes_wu_forecast_peak_c",
                "wu_forecast_peak_c",
                "settlement_source_forecast_peak_c",
                "wu_forecast_peak_value",
                "settlement_source_forecast_peak_value",
            ),
            sigma_keys=(
                "wu_forecast_peak.sigma_c",
                "weather_likelihood_features.wu_forecast_peak_sigma_c",
                "q_bayes_wu_forecast_sigma_c",
                "wu_forecast_peak_sigma_c",
                "settlement_source_forecast_peak_sigma_c",
            ),
            default_sigma_c=1.1,
            rounding_rule=rounding_rule,
        )
    taf_probs = _normalize(taf_raw)
    if not taf_probs:
        taf_probs = _normal_peak_probs(
            labels,
            payload_rows,
            payload,
            field_keys=(
                "taf_forecast_peak.value_c",
                "taf_forecast_peak.value",
                "weather_likelihood_features.taf_forecast_peak_c",
                "q_bayes_taf_peak_c",
                "taf_forecast_peak_c",
                "taf_peak_c",
                "taf_tx_peak_c",
            ),
            sigma_keys=(
                "taf_forecast_peak.sigma_c",
                "weather_likelihood_features.taf_forecast_peak_sigma_c",
                "q_bayes_taf_peak_sigma_c",
                "taf_forecast_peak_sigma_c",
                "taf_peak_sigma_c",
            ),
            default_sigma_c=0.8,
            rounding_rule=rounding_rule,
        )
    hours = _hours_to_peak_window(row, payload, peak_hour=peak_hour)
    created_at = str(row.get("created_at") or "")
    actual_source_created_at = str(actual.get("source_created_at") or "")
    phys_quality = _phys_quality_meta(payload, payload_rows)
    return {
        "source_table": str(row.get("source_table") or ""),
        "id": row.get("id"),
        "slug": str(row.get("slug") or payload.get("slug") or ""),
        "event_date": _event_date(row.get("event_date") or payload.get("event_date")),
        "created_at": created_at,
        "created_at_dt": _parse_dt(created_at),
        "hours_to_peak_window": hours,
        "time_bucket": _time_bucket(hours),
        "lead_day_bucket": _lead_day_bucket(row, payload),
        "labels": labels,
        "baseline_probs": baseline,
        "channels": {
            "wu_fcst": wu_probs,
            "taf": taf_probs,
            "obs": _normalize(obs_raw),
            "phys": _normalize(phys_raw),
        },
        "hits": hits,
        "representatives_c": reps,
        "settlement_actual_c": _convert_temp(float(actual_value), actual_unit or "C", "C"),
        "settlement_actual": float(actual_value),
        "settlement_actual_unit": actual_unit,
        "actual_source": str(actual.get("source") or ""),
        "actual_source_created_at": actual_source_created_at,
        "actual_source_created_at_dt": _parse_dt(actual_source_created_at),
        **phys_quality,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _load_formal_actuals(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict[tuple[str, str], dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest AS (
          SELECT slug, event_date, MAX(id) AS max_id
          FROM event_runs
          WHERE market_var = 'temperature_2m_max'
            AND event_date >= ?
            AND event_date <= ?
            AND settlement_actual IS NOT NULL
            AND COALESCE(market_closed, 0) = 1
          GROUP BY slug, event_date
        )
        SELECT e.slug, e.event_date, e.market_unit, e.settlement_actual, e.created_at
        FROM latest l
        JOIN event_runs e ON e.id = l.max_id
        """,
        (start_date, end_date),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        actual = _to_float(row["settlement_actual"])
        slug = str(row["slug"] or "")
        event_date = _event_date(row["event_date"])
        if actual is None or not slug or not event_date:
            continue
        out[(slug, event_date)] = {
            "actual": float(actual),
            "unit": str(row["market_unit"] or "C"),
            "source": "formal_closed_event_runs",
            "source_created_at": str(row["created_at"] or ""),
        }
    return out


def _load_source_actuals(conn: sqlite3.Connection, start_date: str, end_date: str, *, city: str, station_id: str) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    by_source: dict[tuple[str, str], dict[str, Any]] = {}
    by_date: dict[str, dict[str, Any]] = {}
    if not _table_exists(conn, "event_daily_temps"):
        return by_source, by_date
    rows = conn.execute(
        """
        SELECT event_date, resolution_source, market_unit, settlement_actual, created_at, station_id, city
        FROM event_daily_temps
        WHERE market_var = 'temperature_2m_max'
          AND event_date >= ?
          AND event_date <= ?
          AND settlement_actual IS NOT NULL
          AND (
            LOWER(COALESCE(city, '')) = LOWER(?)
            OR UPPER(COALESCE(station_id, '')) = UPPER(?)
          )
        ORDER BY event_date, created_at
        """,
        (start_date, end_date, city, station_id),
    ).fetchall()
    for row in rows:
        actual = _to_float(row["settlement_actual"])
        event_date = _event_date(row["event_date"])
        if actual is None or not event_date:
            continue
        entry = {
            "actual": float(actual),
            "unit": str(row["market_unit"] or "C"),
            "source": "event_daily_temps",
            "source_created_at": str(row["created_at"] or ""),
            "resolution_source": str(row["resolution_source"] or ""),
        }
        source_key = _source_key(row["resolution_source"])
        if source_key:
            by_source[(source_key, event_date)] = entry
        old = by_date.get(event_date)
        if old is None or str(entry["source_created_at"]) > str(old.get("source_created_at") or ""):
            by_date[event_date] = entry
    return by_source, by_date


def _actual_for_snapshot(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    formal_actuals: Mapping[tuple[str, str], Mapping[str, Any]],
    source_actuals: Mapping[tuple[str, str], Mapping[str, Any]],
    date_actuals: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    slug = str(row.get("slug") or payload.get("slug") or "")
    event_date = _event_date(row.get("event_date") or payload.get("event_date"))
    source_key = _source_key(row.get("resolution_source") or payload.get("resolution_source"))
    if source_key:
        actual = source_actuals.get((source_key, event_date))
        if actual is not None:
            return actual
    actual = formal_actuals.get((slug, event_date))
    if actual is not None:
        return actual
    return date_actuals.get(event_date)


def _snapshot_rows(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    city: str,
    station_id: str,
    source_tables: set[str],
    allow_merged_settlement_snapshots: bool,
    allow_post_event_snapshots: bool,
    thin_minutes: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    slug_like = f"%{city.lower()}%"
    if "event_runs" in source_tables and _table_exists(conn, "event_runs"):
        merged_settlement_filter = "" if allow_merged_settlement_snapshots else "AND settlement_actual IS NULL AND COALESCE(market_closed, 0) = 0"
        station_clause = "OR UPPER(station_id) = UPPER(?)" if str(station_id or "").strip() else ""
        station_params: tuple[str, ...] = (str(station_id),) if station_clause else ()
        metadata_rows = conn.execute(
            f"""
            SELECT id, slug, event_date, created_at, event_end_utc, market_var, market_unit,
                   settlement_actual, market_closed, tz, lon, resolution_source,
                   rounding_rule
            FROM event_runs
            WHERE market_var = 'temperature_2m_max'
              AND event_date >= ?
              AND event_date <= ?
              AND (LOWER(COALESCE(slug, '')) LIKE ? {station_clause})
              {merged_settlement_filter}
            """,
            (start_date, end_date, slug_like, *station_params),
        ).fetchall()
        metadata_by_id: dict[int, dict[str, Any]] = {}
        payload_ids: list[int] = []
        prefiltered_metadata: list[dict[str, Any]] = []
        for row in metadata_rows:
            item = dict(row)
            row_id = int(item.get("id") or 0)
            if row_id <= 0:
                continue
            if not allow_post_event_snapshots and _is_post_event_snapshot(item, {}):
                continue
            prefiltered_metadata.append(item)
        for item in _thin_snapshot_metadata_rows(
            prefiltered_metadata,
            source_table="event_runs",
            thin_minutes=thin_minutes,
        ):
            row_id = int(item.get("id") or 0)
            if row_id <= 0:
                continue
            item["source_table"] = "event_runs"
            metadata_by_id[row_id] = item
            payload_ids.append(row_id)
        for offset in range(0, len(payload_ids), 500):
            chunk = payload_ids[offset : offset + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            for payload_row in conn.execute(
                f"SELECT id, payload_json FROM event_runs WHERE id IN ({placeholders}) AND payload_json IS NOT NULL",
                tuple(chunk),
            ).fetchall():
                row_id = int(payload_row["id"] or 0)
                item = metadata_by_id.get(row_id)
                if item is None:
                    continue
                item = dict(item)
                item["payload_json"] = payload_row["payload_json"]
                rows.append(item)
    if "model_snapshots" in source_tables and _table_exists(conn, "model_snapshots"):
        metadata_rows = conn.execute(
            """
            SELECT id, slug, event_date, created_at
            FROM model_snapshots
            WHERE event_date >= ?
              AND event_date <= ?
              AND LOWER(COALESCE(slug, '')) LIKE ?
            """,
            (start_date, end_date, slug_like),
        ).fetchall()
        metadata_by_id: dict[int, dict[str, Any]] = {}
        payload_ids: list[int] = []
        for item in _thin_snapshot_metadata_rows(
            metadata_rows,
            source_table="model_snapshots",
            thin_minutes=thin_minutes,
        ):
            row_id = int(item.get("id") or 0)
            if row_id <= 0:
                continue
            item["source_table"] = "model_snapshots"
            metadata_by_id[row_id] = item
            payload_ids.append(row_id)
        for offset in range(0, len(payload_ids), 500):
            chunk = payload_ids[offset : offset + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            for payload_row in conn.execute(
                f"SELECT id, payload_json FROM model_snapshots WHERE id IN ({placeholders}) AND payload_json IS NOT NULL",
                tuple(chunk),
            ).fetchall():
                row_id = int(payload_row["id"] or 0)
                item = metadata_by_id.get(row_id)
                if item is None:
                    continue
                item = dict(item)
                item["payload_json"] = payload_row["payload_json"]
                rows.append(item)
    return rows


def _is_post_event_snapshot(row: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    created_at = _parse_dt(row.get("created_at"))
    if created_at is None:
        return False
    event_end = _parse_dt(row.get("event_end_utc") or payload.get("event_end_utc") or payload.get("event_end"))
    if event_end is not None:
        return created_at > event_end
    event_date = _event_date(row.get("event_date") or payload.get("event_date"))
    if not event_date:
        return False
    try:
        day = date.fromisoformat(event_date)
    except ValueError:
        return False
    end_local = datetime.combine(day, time(23, 59), tzinfo=_zone(_infer_tz(row, payload)))
    return created_at > end_local.astimezone(timezone.utc)


def _thin_events(events: Sequence[Mapping[str, Any]], thin_minutes: float) -> list[dict[str, Any]]:
    if thin_minutes <= 0:
        return [dict(event) for event in events]
    out: list[dict[str, Any]] = []
    last_by_key: dict[tuple[str, str, str], datetime] = {}
    min_delta = timedelta(minutes=float(thin_minutes))
    for event in events:
        ts = event.get("created_at_dt")
        if not isinstance(ts, datetime):
            continue
        key = (
            str(event.get("source_table") or ""),
            str(event.get("slug") or ""),
            str(event.get("event_date") or ""),
        )
        prev = last_by_key.get(key)
        if prev is not None and ts - prev < min_delta:
            continue
        out.append(dict(event))
        last_by_key[key] = ts
    return out


def _thin_snapshot_metadata_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_table: str,
    thin_minutes: float,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        created_at = _parse_dt(item.get("created_at"))
        if created_at is None:
            continue
        item["_created_at_dt"] = created_at
        items.append(item)
    items.sort(key=lambda item: (item["_created_at_dt"], int(item.get("id") or 0)))
    if thin_minutes <= 0:
        for item in items:
            item.pop("_created_at_dt", None)
        return items
    out: list[dict[str, Any]] = []
    last_by_key: dict[tuple[str, str, str], datetime] = {}
    min_delta = timedelta(minutes=float(thin_minutes))
    for item in items:
        created_at = item.get("_created_at_dt")
        if not isinstance(created_at, datetime):
            continue
        key = (
            source_table,
            str(item.get("slug") or ""),
            str(item.get("event_date") or ""),
        )
        prev = last_by_key.get(key)
        if prev is not None and created_at - prev < min_delta:
            continue
        item = dict(item)
        item.pop("_created_at_dt", None)
        out.append(item)
        last_by_key[key] = created_at
    return out


def _combine(event: Mapping[str, Any], weights: Mapping[str, float], *, base_strength: float) -> list[float]:
    baseline = event.get("baseline_probs")
    if not isinstance(baseline, list):
        return []
    scores = [max(0.0, float(base_strength)) * float(p) for p in baseline]
    channels = event.get("channels")
    if not isinstance(channels, Mapping):
        return _normalize(scores)
    for weight_key, channel_key in CHANNELS:
        weight = max(0.0, float(weights.get(weight_key, 0.0) or 0.0))
        probs = channels.get(channel_key)
        if weight <= 0 or not isinstance(probs, list) or len(probs) != len(scores):
            continue
        for idx, prob in enumerate(probs):
            scores[idx] += weight * float(prob)
    return _normalize(scores)


def _rps(probs: Sequence[float], hits: Sequence[bool]) -> float:
    if not probs or len(probs) != len(hits) or not any(hits):
        return float("nan")
    hit_idx = hits.index(True)
    pred_cdf = 0.0
    total = 0.0
    for idx, prob in enumerate(probs):
        pred_cdf += float(prob)
        actual_cdf = 1.0 if idx >= hit_idx else 0.0
        total += (pred_cdf - actual_cdf) ** 2
    return total / max(1, len(probs) - 1)


def _metrics_for_probs(events: Sequence[Mapping[str, Any]], probs_by_event: Sequence[Sequence[float]]) -> dict[str, Any]:
    brier = 0.0
    logloss = 0.0
    rps_total = 0.0
    mae = 0.0
    correct = 0
    n = 0
    for event, probs in zip(events, probs_by_event):
        hits = event.get("hits")
        reps = event.get("representatives_c")
        if (
            not isinstance(probs, Sequence)
            or not isinstance(hits, list)
            or not isinstance(reps, list)
            or len(probs) != len(hits)
            or not any(hits)
        ):
            continue
        hit_idx = hits.index(True)
        brier += sum((float(p) - (1.0 if idx == hit_idx else 0.0)) ** 2 for idx, p in enumerate(probs))
        logloss += -math.log(max(1e-12, float(probs[hit_idx])))
        rps_total += _rps(probs, hits)
        pred_idx = max(range(len(probs)), key=lambda idx: float(probs[idx]))
        if pred_idx == hit_idx:
            correct += 1
        pred_peak = sum(float(p) * float(rep) for p, rep in zip(probs, reps))
        mae += abs(pred_peak - float(event["settlement_actual_c"]))
        n += 1
    if n <= 0:
        return {"n": 0}
    return {
        "n": n,
        "brier": brier / n,
        "logloss": logloss / n,
        "rps": rps_total / n,
        "mae_c": mae / n,
        "direction_accuracy": correct / n,
    }


def _metrics(events: Sequence[Mapping[str, Any]], *, key: str) -> dict[str, Any]:
    return _metrics_for_probs(events, [event.get(key) or [] for event in events])


def _metric_improvements(base: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("brier", "logloss", "rps", "mae_c"):
        b = _to_float(base.get(key))
        n = _to_float(new.get(key))
        out[f"{key}_improvement"] = None if b is None or n is None or abs(b) <= EPS else (b - n) / abs(b)
    b_acc = _to_float(base.get("direction_accuracy"))
    n_acc = _to_float(new.get("direction_accuracy"))
    out["direction_accuracy_lift_pp"] = None if b_acc is None or n_acc is None else (n_acc - b_acc) * 100.0
    return out


def _parse_grid(raw: str) -> list[float]:
    out: list[float] = []
    for part in str(raw or "").split(","):
        value = _to_float(part.strip())
        if value is None or value < 0:
            continue
        out.append(float(value))
    return sorted(set(out)) or [0.0]


def _grid_candidates(args: argparse.Namespace) -> list[dict[str, float]]:
    grids = {
        "S_wu_fcst": _parse_grid(args.wu_grid),
        "S_taf": _parse_grid(args.taf_grid),
        "S_obs": _parse_grid(args.obs_grid),
        "S_phys": _parse_grid(args.phys_grid),
    }
    out: list[dict[str, float]] = []
    for values in itertools.product(*(grids[key] for key, _ in CHANNELS)):
        out.append({CHANNELS[idx][0]: float(values[idx]) for idx in range(len(CHANNELS))})
    if ZERO_WEIGHTS not in out:
        out.append(dict(ZERO_WEIGHTS))
    return out


def _objective_tuple(metrics: Mapping[str, Any], weights: Mapping[str, float]) -> tuple[float, float, float, float, float]:
    logloss = _to_float(metrics.get("logloss"))
    brier = _to_float(metrics.get("brier"))
    rps = _to_float(metrics.get("rps"))
    mae = _to_float(metrics.get("mae_c"))
    weight_sum = sum(float(weights.get(key, 0.0) or 0.0) for key, _ in CHANNELS)
    return (
        float(logloss if logloss is not None else 1e9),
        float(brier if brier is not None else 1e9),
        float(rps if rps is not None else 1e9),
        float(mae if mae is not None else 1e9),
        weight_sum,
    )


def _best_weights(
    events: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, float]],
    *,
    base_strength: float,
) -> tuple[dict[str, float], dict[str, Any]]:
    best_weights = dict(ZERO_WEIGHTS)
    best_metrics = _metrics_for_probs(events, [_combine(event, ZERO_WEIGHTS, base_strength=base_strength) for event in events])
    best_key = _objective_tuple(best_metrics, best_weights)
    for weights in candidates:
        probs = [_combine(event, weights, base_strength=base_strength) for event in events]
        metrics = _metrics_for_probs(events, probs)
        key = _objective_tuple(metrics, weights)
        if key < best_key:
            best_key = key
            best_weights = {k: float(weights.get(k, 0.0) or 0.0) for k, _ in CHANNELS}
            best_metrics = metrics
    return best_weights, best_metrics


def _visible_training_events(events: Sequence[Mapping[str, Any]], test_event: Mapping[str, Any]) -> list[dict[str, Any]]:
    test_ts = test_event.get("created_at_dt")
    if not isinstance(test_ts, datetime):
        return []
    out: list[dict[str, Any]] = []
    for event in events:
        created_at = event.get("created_at_dt")
        actual_visible_at = event.get("actual_source_created_at_dt")
        if not isinstance(created_at, datetime) or not isinstance(actual_visible_at, datetime):
            continue
        if created_at < test_ts and actual_visible_at <= test_ts:
            out.append(dict(event))
    return out


def _walk_forward(
    events: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, float]],
    *,
    base_strength: float,
    min_train_samples: int,
    min_train_market_days: int,
) -> list[dict[str, Any]]:
    candidate_list = [{key: float(weights.get(key, 0.0) or 0.0) for key, _ in CHANNELS} for weights in candidates]
    if not any(all(abs(float(weights.get(key, 0.0) or 0.0)) <= EPS for key, _ in CHANNELS) for weights in candidate_list):
        candidate_list.append(dict(ZERO_WEIGHTS))
    zero_idx = next(
        idx
        for idx, weights in enumerate(candidate_list)
        if all(abs(float(weights.get(key, 0.0) or 0.0)) <= EPS for key, _ in CHANNELS)
    )
    candidate_count = len(candidate_list)
    brier_sums = [0.0] * candidate_count
    logloss_sums = [0.0] * candidate_count
    rps_sums = [0.0] * candidate_count
    mae_sums = [0.0] * candidate_count
    correct_sums = [0.0] * candidate_count
    counts = [0] * candidate_count
    eligible_market_days: set[tuple[Any, Any]] = set()
    eligible_count = 0
    pending_by_actual_visible_at: list[tuple[datetime, int]] = []
    next_prior_idx = 0

    def contribution(event: Mapping[str, Any], probs: Sequence[float]) -> tuple[float, float, float, float, float, int] | None:
        hits = event.get("hits")
        reps = event.get("representatives_c")
        if (
            not isinstance(probs, Sequence)
            or not isinstance(hits, list)
            or not isinstance(reps, list)
            or len(probs) != len(hits)
            or not any(hits)
        ):
            return None
        hit_idx = hits.index(True)
        brier = sum((float(p) - (1.0 if idx == hit_idx else 0.0)) ** 2 for idx, p in enumerate(probs))
        logloss = -math.log(max(1e-12, float(probs[hit_idx])))
        rps = _rps(probs, hits)
        pred_idx = max(range(len(probs)), key=lambda idx: float(probs[idx]))
        pred_peak = sum(float(p) * float(rep) for p, rep in zip(probs, reps))
        mae = abs(pred_peak - float(event["settlement_actual_c"]))
        return brier, logloss, rps, mae, 1.0 if pred_idx == hit_idx else 0.0, 1

    def add_training_event(event: Mapping[str, Any]) -> None:
        nonlocal eligible_count
        eligible_count += 1
        eligible_market_days.add((event.get("slug"), event.get("event_date")))
        for idx, weights in enumerate(candidate_list):
            contrib = contribution(event, _combine(event, weights, base_strength=base_strength))
            if contrib is None:
                continue
            brier, logloss, rps, mae, correct, n = contrib
            brier_sums[idx] += brier
            logloss_sums[idx] += logloss
            rps_sums[idx] += rps
            mae_sums[idx] += mae
            correct_sums[idx] += correct
            counts[idx] += n

    def aggregate_metrics(idx: int) -> dict[str, Any]:
        n = int(counts[idx])
        if n <= 0:
            return {"n": 0}
        return {
            "n": n,
            "brier": brier_sums[idx] / n,
            "logloss": logloss_sums[idx] / n,
            "rps": rps_sums[idx] / n,
            "mae_c": mae_sums[idx] / n,
            "direction_accuracy": correct_sums[idx] / n,
        }

    out: list[dict[str, Any]] = []
    for event in events:
        test_ts = event.get("created_at_dt")
        if not isinstance(test_ts, datetime):
            continue
        while next_prior_idx < len(events):
            prior = events[next_prior_idx]
            prior_created_at = prior.get("created_at_dt")
            if not isinstance(prior_created_at, datetime) or prior_created_at >= test_ts:
                break
            actual_visible_at = prior.get("actual_source_created_at_dt")
            if isinstance(actual_visible_at, datetime):
                heapq.heappush(pending_by_actual_visible_at, (actual_visible_at, next_prior_idx))
            next_prior_idx += 1
        while pending_by_actual_visible_at and pending_by_actual_visible_at[0][0] <= test_ts:
            _, prior_idx = heapq.heappop(pending_by_actual_visible_at)
            add_training_event(events[prior_idx])

        train_day_count = len(eligible_market_days)
        if eligible_count < min_train_samples or train_day_count < min_train_market_days:
            weights = dict(ZERO_WEIGHTS)
            reason = "insufficient_visible_training_labels"
            train_metrics: dict[str, Any] = {"n": eligible_count, "market_days": train_day_count}
        else:
            best_idx = zero_idx
            best_weights = dict(ZERO_WEIGHTS)
            best_metrics = aggregate_metrics(best_idx)
            best_key = _objective_tuple(best_metrics, best_weights)
            for idx, candidate_weights in enumerate(candidate_list):
                metrics = aggregate_metrics(idx)
                key = _objective_tuple(metrics, candidate_weights)
                if key < best_key:
                    best_idx = idx
                    best_key = key
                    best_weights = dict(candidate_weights)
                    best_metrics = metrics
            weights = best_weights
            train_metrics = best_metrics
            reason = "trained_on_visible_prior_labels"
        calibrated = _combine(event, weights, base_strength=base_strength)
        row = dict(event)
        row["selected_weights"] = weights
        row["selection_reason"] = reason
        row["train_sample_count"] = eligible_count
        row["train_market_day_count"] = train_day_count
        row["train_metrics"] = train_metrics
        row["calibrated_probs"] = calibrated
        out.append(row)
    return out


def _stratified(events: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        groups.setdefault(str(event.get(field) or "unknown"), []).append(event)
    out: dict[str, Any] = {}
    for key, vals in sorted(groups.items()):
        base = _metrics(vals, key="baseline_probs")
        calibrated = _metrics(vals, key="calibrated_probs")
        out[key] = {
            "baseline": base,
            "calibrated": calibrated,
            "improvements": _metric_improvements(base, calibrated),
        }
    return out


def _phys_only_metrics(
    events: Sequence[Mapping[str, Any]],
    weights: Sequence[float],
    *,
    base_strength: float,
) -> dict[str, Any]:
    base = _metrics(events, key="baseline_probs")
    out: dict[str, Any] = {
        "baseline": base,
        "sample_count": int(base.get("n") or 0),
        "market_day_count": _market_day_count(events),
        "weights": {},
    }
    best_weight = 0.0
    best_metrics = base
    best_key = _objective_tuple(base, ZERO_WEIGHTS)
    for weight in weights:
        phys_weights = {
            "S_wu_fcst": 0.0,
            "S_taf": 0.0,
            "S_obs": 0.0,
            "S_phys": float(weight),
        }
        probs = [_combine(event, phys_weights, base_strength=base_strength) for event in events]
        metrics = _metrics_for_probs(events, probs)
        improvements = _metric_improvements(base, metrics)
        out["weights"][str(float(weight))] = {
            "metrics": metrics,
            "improvements": improvements,
        }
        key = _objective_tuple(metrics, phys_weights)
        if key < best_key:
            best_key = key
            best_weight = float(weight)
            best_metrics = metrics
    out["best_weight_by_objective"] = best_weight
    out["best_metrics_by_objective"] = best_metrics
    out["best_improvements_by_objective"] = _metric_improvements(base, best_metrics)
    return out


def _phys_candidate_gate(
    scope: Mapping[str, Any],
    *,
    min_samples: int,
    min_market_days: int,
) -> dict[str, Any]:
    sample_count = int(scope.get("sample_count") or 0)
    market_day_count = int(scope.get("market_day_count") or 0)
    best_weight = float(scope.get("best_weight_by_objective") or 0.0)
    improvements = scope.get("best_improvements_by_objective")
    improvements = improvements if isinstance(improvements, Mapping) else {}
    reasons: list[str] = []
    if sample_count < min_samples:
        reasons.append("phys_candidate_sample_count_below_min")
    if market_day_count < min_market_days:
        reasons.append("phys_candidate_market_day_count_below_min")
    if best_weight <= EPS:
        reasons.append("phys_candidate_best_weight_zero")
    for metric in ("brier", "logloss", "rps"):
        value = _to_float(improvements.get(f"{metric}_improvement"))
        if value is None or value < 0:
            reasons.append(f"phys_candidate_{metric}_not_improved")
    activated = not reasons
    return {
        "activated": activated,
        "recommended_weight": best_weight if activated else 0.0,
        "best_weight_by_objective": best_weight,
        "sample_count": sample_count,
        "market_day_count": market_day_count,
        "min_samples": int(min_samples),
        "min_market_days": int(min_market_days),
        "improvements": dict(improvements),
        "reasons": reasons or ["passed_phys_quality_candidate_gate"],
    }


def _phys_quality_audit(
    events: Sequence[Mapping[str, Any]],
    *,
    phys_grid: Sequence[float],
    base_strength: float,
    min_d0_candidate_samples: int,
    min_d0_candidate_market_days: int,
) -> dict[str, Any]:
    all_events = [dict(event) for event in events]
    candidate_events = [
        dict(event)
        for event in events
        if bool(event.get("phys_quality_candidate"))
        and isinstance((event.get("channels") or {}).get("phys"), list)
        and bool((event.get("channels") or {}).get("phys"))
    ]
    rejected_events = [dict(event) for event in events if event not in candidate_events]
    reason_counts: Counter[str] = Counter()
    for event in events:
        if bool(event.get("phys_quality_candidate")):
            continue
        reasons = event.get("phys_quality_reasons")
        if isinstance(reasons, list):
            for reason in reasons:
                reason_counts[str(reason)] += 1
        else:
            reason_counts["unknown"] += 1

    stratified: dict[str, Any] = {}
    for field in ("lead_day_bucket", "time_bucket"):
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for event in candidate_events:
            groups.setdefault(str(event.get(field) or "unknown"), []).append(event)
        stratified[field] = {
            key: _phys_only_metrics(vals, phys_grid, base_strength=base_strength)
            for key, vals in sorted(groups.items())
            if len(vals) >= 3
        }

    all_scope = _phys_only_metrics(all_events, phys_grid, base_strength=base_strength)
    candidate_scope = _phys_only_metrics(candidate_events, phys_grid, base_strength=base_strength)
    d0_scope = stratified.get("lead_day_bucket", {}).get("D0") if isinstance(stratified.get("lead_day_bucket"), Mapping) else None
    d0_scope = d0_scope if isinstance(d0_scope, Mapping) else {}

    return {
        "read_only": True,
        "main_posterior_weight_side_effect": False,
        "definition": {
            "candidate_required": [
                "weather_likelihood_refreshed_at_present",
                "weather_likelihood_features_nonempty",
                "weather_likelihood_feature_quality in complete|partial",
                "q_bayes_physical_likelihood_evidence_scale_details present",
                "not legacy_s_phys_evidence_with_empty_features",
            ]
        },
        "d0_candidate_activation_gate": _phys_candidate_gate(
            d0_scope,
            min_samples=min_d0_candidate_samples,
            min_market_days=min_d0_candidate_market_days,
        ),
        "sample_counts": {
            "all": len(all_events),
            "candidate": len(candidate_events),
            "rejected": len(events) - len(candidate_events),
        },
        "rejected_reason_counts": dict(sorted(reason_counts.items())),
        "all_samples_phys_only": all_scope,
        "candidate_samples_phys_only": candidate_scope,
        "candidate_stratified": stratified,
    }


def _channel_coverage(events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out = {channel_key: 0 for _, channel_key in CHANNELS}
    for event in events:
        channels = event.get("channels")
        if not isinstance(channels, Mapping):
            continue
        for _, channel_key in CHANNELS:
            probs = channels.get(channel_key)
            if isinstance(probs, list) and probs and any(float(v) > EPS for v in probs):
                out[channel_key] += 1
    return out


def _selected_weight_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for weight_key, _ in CHANNELS:
        values = [float((event.get("selected_weights") or {}).get(weight_key, 0.0) or 0.0) for event in events]
        counter = Counter(values)
        out[weight_key] = {
            "nonzero_count": sum(1 for value in values if value > EPS),
            "values": {str(key): counter[key] for key in sorted(counter)},
        }
    return out


def _passes_metric_gate(
    base: Mapping[str, Any],
    calibrated: Mapping[str, Any],
    *,
    max_regression: float,
) -> tuple[bool, list[str], dict[str, Any]]:
    improvements = _metric_improvements(base, calibrated)
    failed: list[str] = []
    for metric in ("brier", "logloss", "rps"):
        value = _to_float(improvements.get(f"{metric}_improvement"))
        if value is None or value < -abs(max_regression):
            failed.append(f"{metric}_regressed")
    return not failed, failed, improvements


def _recommendations(
    events: Sequence[Mapping[str, Any]],
    *,
    global_weights: Mapping[str, float],
    coverage: Mapping[str, int],
    min_channel_samples: int,
    oos_gate_ok: bool,
    oos_failures: Sequence[str],
    min_nonzero_selected: int,
) -> dict[str, Any]:
    selected = _selected_weight_summary(events)
    out: dict[str, Any] = {}
    for weight_key, channel_key in CHANNELS:
        reasons: list[str] = []
        sample_count = int(coverage.get(channel_key, 0) or 0)
        nonzero_count = int((selected.get(weight_key) or {}).get("nonzero_count") or 0)
        proposed = float(global_weights.get(weight_key, 0.0) or 0.0)
        if sample_count < min_channel_samples:
            reasons.append("channel_sample_count_below_min")
        if nonzero_count < min_nonzero_selected and proposed > 0:
            reasons.append("walk_forward_nonzero_selection_below_min")
        if not oos_gate_ok:
            reasons.extend(oos_failures)
        if proposed <= EPS:
            reasons.append("global_grid_selected_zero")
        activated = not reasons
        out[weight_key] = {
            "recommended_weight": proposed if activated else 0.0,
            "global_grid_weight": proposed,
            "activated": activated,
            "channel_key": channel_key,
            "channel_sample_count": sample_count,
            "walk_forward_nonzero_selected_count": nonzero_count,
            "reasons": reasons or ["passed_created_at_safe_walk_forward_gate"],
        }
    return out


def _market_day_count(events: Sequence[Mapping[str, Any]]) -> int:
    return len({(event.get("slug"), event.get("event_date")) for event in events})


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    source_tables = {part.strip() for part in str(args.snapshot_sources).split(",") if part.strip()}
    end_day = datetime.now(timezone.utc).date()
    if not args.include_current_day:
        end_day = end_day - timedelta(days=1)
    start_day = end_day - timedelta(days=max(1, int(args.days)) - 1)
    start_date = start_day.isoformat()
    end_date = end_day.isoformat()

    diagnostics: dict[str, Any] = {
        "read_only": True,
        "writes_performed": False,
        "db_path": str(args.db),
        "start_date": start_date,
        "end_date": end_date,
        "city": args.city,
        "station_id": args.station_id,
        "snapshot_sources": sorted(source_tables),
        "leakage_guards": {
            "uses_persisted_snapshot_probabilities": True,
            "trains_only_on_labels_visible_before_test_created_at": True,
            "excludes_closed_or_settled_event_run_snapshots": not args.allow_merged_settlement_snapshots,
            "excludes_post_event_snapshots_by_default": not args.allow_post_event_snapshots,
            "no_fact_table_writes": True,
        },
        "skipped_counts": {},
    }

    def skip(reason: str) -> None:
        skipped = diagnostics.setdefault("skipped_counts", {})
        if isinstance(skipped, dict):
            skipped[reason] = int(skipped.get(reason, 0)) + 1

    formal_actuals = _load_formal_actuals(conn, start_date, end_date)
    source_actuals, date_actuals = _load_source_actuals(
        conn,
        start_date,
        end_date,
        city=args.city,
        station_id=args.station_id,
    )
    events: list[dict[str, Any]] = []
    for row in _snapshot_rows(
        conn,
        start_date=start_date,
        end_date=end_date,
        city=args.city,
        station_id=args.station_id,
        source_tables=source_tables,
        allow_merged_settlement_snapshots=args.allow_merged_settlement_snapshots,
        allow_post_event_snapshots=args.allow_post_event_snapshots,
        thin_minutes=float(args.thin_minutes),
    ):
        payload = _safe_json(row.get("payload_json"))
        if not isinstance(payload, Mapping):
            skip("payload_not_object")
            continue
        market_var = str(row.get("market_var") or payload.get("market_var") or "").strip()
        if market_var and market_var != "temperature_2m_max":
            skip("market_var_not_temperature_2m_max")
            continue
        unit = str(row.get("market_unit") or payload.get("market_unit") or "C").replace("°", "").upper()
        if unit and unit != "C":
            skip("market_unit_not_c")
            continue
        if not args.allow_post_event_snapshots and _is_post_event_snapshot(row, payload):
            skip("post_event_snapshot")
            continue
        actual = _actual_for_snapshot(row, payload, formal_actuals, source_actuals, date_actuals)
        if actual is None:
            skip("actual_missing")
            continue
        event = _extract_event(
            row,
            actual,
            peak_hour=float(args.peak_hour),
            baseline_keys=tuple(args.baseline_keys.split(",")),
        )
        if event is None:
            skip("event_extract_failed")
            continue
        if event.get("created_at_dt") is None:
            skip("created_at_invalid")
            continue
        if event.get("actual_source_created_at_dt") is None:
            skip("actual_source_created_at_missing")
            continue
        events.append(event)

    events.sort(key=lambda item: (item["created_at_dt"], str(item.get("source_table")), int(item.get("id") or 0)))
    events = _thin_events(events, float(args.thin_minutes))
    candidates = _grid_candidates(args)
    walked = _walk_forward(
        events,
        candidates,
        base_strength=float(args.base_strength),
        min_train_samples=int(args.min_train_samples),
        min_train_market_days=int(args.min_train_market_days),
    )

    base_metrics = _metrics(walked, key="baseline_probs")
    calibrated_metrics = _metrics(walked, key="calibrated_probs")
    oos_gate_ok, oos_failures, improvements = _passes_metric_gate(
        base_metrics,
        calibrated_metrics,
        max_regression=float(args.max_metric_regression),
    )
    global_weights, global_train_metrics = _best_weights(walked, candidates, base_strength=float(args.base_strength))
    coverage = _channel_coverage(walked)
    recommendations = _recommendations(
        walked,
        global_weights=global_weights,
        coverage=coverage,
        min_channel_samples=int(args.min_channel_samples),
        oos_gate_ok=oos_gate_ok,
        oos_failures=oos_failures,
        min_nonzero_selected=int(args.min_nonzero_selected),
    )

    blockers: list[str] = []
    if len(walked) < int(args.min_samples):
        blockers.append("sample_count_below_min")
    if _market_day_count(walked) < int(args.min_market_days):
        blockers.append("market_day_count_below_min")
    if not oos_gate_ok:
        blockers.extend(oos_failures)
    if not any(bool(row.get("activated")) for row in recommendations.values()):
        blockers.append("no_channel_qualified_for_nonzero_weight")

    return {
        "ok": not blockers,
        "blockers": sorted(set(blockers)),
        "diagnostics": diagnostics,
        "sample_count": len(walked),
        "market_day_count": _market_day_count(walked),
        "channel_coverage": coverage,
        "baseline_metrics": base_metrics,
        "calibrated_metrics": calibrated_metrics,
        "improvements": improvements,
        "stratified": {
            "lead_day_bucket": _stratified(walked, "lead_day_bucket"),
            "hours_to_peak_window": _stratified(walked, "time_bucket"),
        },
        "s_phys_quality_audit": _phys_quality_audit(
            walked,
            phys_grid=_parse_grid(args.phys_grid),
            base_strength=float(args.base_strength),
            min_d0_candidate_samples=int(args.min_phys_d0_candidate_samples),
            min_d0_candidate_market_days=int(args.min_phys_d0_candidate_market_days),
        ),
        "walk_forward_selection_summary": _selected_weight_summary(walked),
        "global_grid_weights": global_weights,
        "global_grid_train_metrics": global_train_metrics,
        "recommendations": recommendations,
        "recent_evaluated_samples": [
            {
                "source_table": row.get("source_table"),
                "id": row.get("id"),
                "slug": row.get("slug"),
                "event_date": row.get("event_date"),
                "created_at": row.get("created_at"),
                "lead_day_bucket": row.get("lead_day_bucket"),
                "time_bucket": row.get("time_bucket"),
                "train_sample_count": row.get("train_sample_count"),
                "train_market_day_count": row.get("train_market_day_count"),
                "selection_reason": row.get("selection_reason"),
                "selected_weights": row.get("selected_weights"),
                "actual_source": row.get("actual_source"),
                "actual_source_created_at": row.get("actual_source_created_at"),
                "phys_quality_candidate": row.get("phys_quality_candidate"),
                "phys_quality_reasons": row.get("phys_quality_reasons"),
                "weather_likelihood_refreshed_at_present": row.get("weather_likelihood_refreshed_at_present"),
                "weather_likelihood_feature_count": row.get("weather_likelihood_feature_count"),
                "weather_likelihood_feature_quality": row.get("weather_likelihood_feature_quality"),
                "phys_persisted_evidence_max": row.get("phys_persisted_evidence_max"),
            }
            for row in walked[-20:]
        ],
        "config": {
            "base_strength": float(args.base_strength),
            "min_samples": int(args.min_samples),
            "min_market_days": int(args.min_market_days),
            "min_train_samples": int(args.min_train_samples),
            "min_train_market_days": int(args.min_train_market_days),
            "min_channel_samples": int(args.min_channel_samples),
            "min_nonzero_selected": int(args.min_nonzero_selected),
            "min_phys_d0_candidate_samples": int(args.min_phys_d0_candidate_samples),
            "min_phys_d0_candidate_market_days": int(args.min_phys_d0_candidate_market_days),
            "max_metric_regression": float(args.max_metric_regression),
            "candidate_count": len(candidates),
            "grids": {
                "S_wu_fcst": _parse_grid(args.wu_grid),
                "S_taf": _parse_grid(args.taf_grid),
                "S_obs": _parse_grid(args.obs_grid),
                "S_phys": _parse_grid(args.phys_grid),
            },
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--include-current-day", action="store_true")
    parser.add_argument("--city", default="chengdu")
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--snapshot-sources", default="event_runs,model_snapshots")
    parser.add_argument("--baseline-keys", default="q_raw_static,q_model_round,q_raw,q_round")
    parser.add_argument("--peak-hour", type=float, default=16.0)
    parser.add_argument("--thin-minutes", type=float, default=30.0)
    parser.add_argument("--base-strength", type=float, default=1.0)
    parser.add_argument("--wu-grid", default="0,1,2,4,6,8")
    parser.add_argument("--taf-grid", default="0,2,4,8,12,16")
    parser.add_argument("--obs-grid", default="0,2,4,8,12,16")
    parser.add_argument("--phys-grid", default="0,1,2,4,6,8")
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--min-market-days", type=int, default=5)
    parser.add_argument("--min-train-samples", type=int, default=8)
    parser.add_argument("--min-train-market-days", type=int, default=3)
    parser.add_argument("--min-channel-samples", type=int, default=10)
    parser.add_argument("--min-nonzero-selected", type=int, default=3)
    parser.add_argument("--min-phys-d0-candidate-samples", type=int, default=30)
    parser.add_argument("--min-phys-d0-candidate-market-days", type=int, default=5)
    parser.add_argument("--max-metric-regression", type=float, default=0.0)
    parser.add_argument("--allow-post-event-snapshots", action="store_true")
    parser.add_argument("--allow-merged-settlement-snapshots", action="store_true")
    parser.add_argument("--strict-exit", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = build_report(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if args.strict_exit and not bool(report.get("ok")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
