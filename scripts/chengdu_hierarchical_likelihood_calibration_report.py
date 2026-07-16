#!/usr/bin/env python3
"""Read-only Chengdu calibration/backtest for hierarchical joint likelihood.

This script compares three probability views on historical Chengdu intraday
windows:

1. production posterior from archived fact snapshots;
2. legacy temperature-path probability, normalized when present;
3. calibration hierarchical joint likelihood: P0(bucket) * sum_s P(S|E)P(delta|S,M,t).

It opens archive.db in SQLite read-only mode and writes only report artifacts.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from statistics import mean
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_JSON_REPORT = ROOT / "data" / "reports" / "chengdu_hierarchical_likelihood_calibration_latest.json"
DEFAULT_MD_REPORT = ROOT / "data" / "reports" / "chengdu_hierarchical_likelihood_calibration_latest.md"

EVENT_TZ = ZoneInfo("Asia/Shanghai")
STATION_ID = "ZUUU"
EPS = 1e-12


@dataclass(frozen=True)
class Bucket:
    label: str
    lower_c: Optional[float]
    upper_c: Optional[float]


@dataclass
class Sample:
    slug: str
    event_date: str
    fact_id: int
    fact_as_of: str
    fact_as_of_local: str
    local_hour: float
    window: str
    actual_c: float
    actual_bucket: str
    actual_index: int
    buckets: list[Bucket]
    old_posterior: list[float]
    legacy_path: Optional[list[float]]
    hierarchical_joint: list[float]
    state_probs: dict[str, float]
    calibration_inputs: dict[str, Any]


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out


def _nested_get(obj: Mapping[str, Any], path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _first_float(obj: Mapping[str, Any], paths: Iterable[str]) -> Optional[float]:
    for path in paths:
        val = _to_float(_nested_get(obj, path))
        if val is not None:
            return val
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _softmax(scores: Mapping[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    high = max(scores.values())
    exps = {key: math.exp(value - high) for key, value in scores.items()}
    total = sum(exps.values())
    if total <= 0:
        return {key: 1.0 / len(exps) for key in exps}
    return {key: value / total for key, value in exps.items()}


def _normalize(values: Iterable[float], *, floor: float = 0.0) -> Optional[list[float]]:
    probs = [max(0.0, float(value)) + floor for value in values]
    total = sum(probs)
    if total <= 0 or not math.isfinite(total):
        return None
    return [value / total for value in probs]


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    sigma = max(sigma, 0.05)
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def _normal_interval_prob(
    lower: Optional[float],
    upper: Optional[float],
    *,
    mu: float,
    sigma: float,
) -> float:
    lo = 0.0 if lower is None else _normal_cdf(lower, mu, sigma)
    hi = 1.0 if upper is None else _normal_cdf(upper, mu, sigma)
    return max(0.0, hi - lo)


def bucket_bounds_from_label(label: str) -> tuple[Optional[float], Optional[float]]:
    text = str(label or "").strip()
    number = None
    for token in text.replace("°", " ").replace("C", " ").split():
        val = _to_float(token)
        if val is not None:
            number = val
            break
    if number is None:
        return (None, None)
    lower = number - 0.5
    upper = number + 0.5
    lower_text = text.lower()
    if "below" in lower_text:
        return (None, upper)
    if "higher" in lower_text or "above" in lower_text:
        return (lower, None)
    return (lower, upper)


def _bucket_from_entry(entry: Mapping[str, Any], index: int) -> Bucket:
    label = str(entry.get("outcome") or entry.get("bucket") or entry.get("label") or f"bucket_{index}")
    lower = _to_float(entry.get("lower_c"))
    upper = _to_float(entry.get("upper_c"))
    if lower is None and upper is None:
        lower, upper = bucket_bounds_from_label(label)
    return Bucket(label=label, lower_c=lower, upper_c=upper)


def actual_bucket_index(actual_c: float, buckets: list[Bucket]) -> Optional[int]:
    for idx, bucket in enumerate(buckets):
        lower_ok = bucket.lower_c is None or actual_c >= bucket.lower_c
        upper_ok = bucket.upper_c is None or actual_c < bucket.upper_c
        if lower_ok and upper_ok:
            return idx
    return None


def _extract_posterior(fact: Mapping[str, Any]) -> tuple[list[Bucket], Optional[list[float]]]:
    dist = _nested_get(fact, "posterior_temperature_distribution.bucket_probabilities")
    if not isinstance(dist, list) or not dist:
        return ([], None)
    buckets: list[Bucket] = []
    probs: list[float] = []
    for idx, item in enumerate(dist):
        if not isinstance(item, Mapping):
            continue
        buckets.append(_bucket_from_entry(item, idx))
        prob = _to_float(item.get("probability"))
        if prob is None:
            pct = _to_float(item.get("probability_pct"))
            prob = pct / 100.0 if pct is not None else 0.0
        probs.append(prob)
    normalized = _normalize(probs)
    if not buckets or normalized is None:
        return ([], None)
    return (buckets, normalized)


def _extract_legacy_path(fact: Mapping[str, Any], buckets: list[Bucket]) -> Optional[list[float]]:
    path = _nested_get(fact, "temperature_path_distribution.bucket_distribution")
    if not isinstance(path, list) or not path or not buckets:
        return None
    label_to_index = {bucket.label: idx for idx, bucket in enumerate(buckets)}
    raw = [0.0 for _ in buckets]
    seen = 0
    for idx, item in enumerate(path):
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("outcome") or item.get("bucket") or item.get("label") or "")
        target_idx = label_to_index.get(label, idx if idx < len(raw) else -1)
        if target_idx < 0 or target_idx >= len(raw):
            continue
        val = _to_float(item.get("path_probability"))
        if val is None and item.get("path_probability_available") is True:
            val = _to_float(item.get("path_probability_pct"))
            val = val / 100.0 if val is not None else None
        if val is None:
            val = _to_float(item.get("posterior"))
        if val is None:
            val = 0.0
        raw[target_idx] = max(0.0, val)
        seen += 1
    if seen == 0 or sum(raw) <= 0:
        target_bucket = str(_nested_get(fact, "temperature_path_distribution.target_bucket") or "")
        second_bucket = str(_nested_get(fact, "temperature_path_distribution.second_target_bucket") or "")
        for label, path_name in (
            (target_bucket, "temperature_path_distribution.target_bucket_path_probability"),
            (second_bucket, "temperature_path_distribution.second_target_bucket_path_probability"),
        ):
            idx = label_to_index.get(label)
            val = _to_float(_nested_get(fact, path_name))
            if idx is not None and val is not None:
                raw[idx] = max(0.0, val)
    return _normalize(raw)


def _weather_modifier(fact: Mapping[str, Any]) -> dict[str, Any]:
    for path in (
        "physical_likelihood.weather_modifier",
        "physical_weather_modifier",
        "temperature_path_distribution.physical_weather_modifier",
    ):
        value = _nested_get(fact, path)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _metar_profile(modifier: Mapping[str, Any]) -> dict[str, Any]:
    profile = modifier.get("metar_weather_profile")
    return dict(profile) if isinstance(profile, Mapping) else {}


def _first_profile_raw(profile: Mapping[str, Any]) -> str:
    raw = profile.get("raw")
    if raw:
        return str(raw)
    profiles = profile.get("profiles")
    if isinstance(profiles, list):
        for item in profiles:
            if isinstance(item, Mapping) and item.get("raw"):
                return str(item["raw"])
    return ""


def _extract_calibration_inputs(fact: Mapping[str, Any], local_hour: float) -> dict[str, Any]:
    modifier = _weather_modifier(fact)
    metar = _metar_profile(modifier)
    dewpoint = modifier.get("dewpoint_profile")
    dewpoint = dict(dewpoint) if isinstance(dewpoint, Mapping) else {}
    pressure = modifier.get("pressure_profile")
    pressure = dict(pressure) if isinstance(pressure, Mapping) else {}

    current_temp_c = _first_float(
        fact,
        (
            "source_observation_comparison.awc.observed_temp_c",
            "source_observation_comparison.awc.latest_temp_c",
            "temperature_path_distribution.observed_current_temp_c",
            "temperature_path_distribution.observed_temp_c",
            "temperature_path_distribution.observed_path_temp_c",
        ),
    )
    today_max_c = _first_float(
        fact,
        (
            "source_observation_comparison.awc.observed_today_max_c",
            "source_observation_comparison.awc.today_max_c",
            "temperature_path_distribution.observed_today_max_c",
            "temperature_path_distribution.observed_path_temp_c",
        ),
    )
    dewpoint_observed = _to_float(dewpoint.get("observed_temp_c"))
    if current_temp_c is None:
        current_temp_c = dewpoint_observed
    if today_max_c is None:
        today_max_c = current_temp_c

    momentum_rate = _first_float(
        {"modifier": modifier, "fact": fact},
        (
            "modifier.dewpoint_profile.momentum_rate_c_per_hour",
            "modifier.pressure_profile.momentum_rate_c_per_hour",
            "modifier.wind_profile.momentum_rate_c_per_hour",
            "fact.temperature_path_distribution.momentum_rate_c_per_hour",
            "fact.weather_likelihood_features_compact.momentum_rate_c_per_hour",
        ),
    )
    if momentum_rate is None:
        momentum_rate = 0.0

    raw = _first_profile_raw(metar)
    visibility_good = bool(metar.get("cavok")) or " 9999 " in f" {raw} "
    visibility_poor = any(token in raw for token in (" BR ", " HZ ", " FG ", " 3000 ", " 4000 "))

    return {
        "modifier_available": bool(modifier.get("available")),
        "weather_modifier_state": modifier.get("state"),
        "current_temp_c": current_temp_c,
        "today_max_c": today_max_c,
        "momentum_rate_c_per_hour": momentum_rate,
        "local_hour": local_hour,
        "effective_cloud_cover_pct": _to_float(metar.get("effective_cloud_cover_pct"))
        if _to_float(metar.get("effective_cloud_cover_pct")) is not None
        else _to_float(modifier.get("cloud_cover_pct")),
        "low_cloud_cover_pct": _to_float(metar.get("low_cloud_cover_pct"))
        if _to_float(metar.get("low_cloud_cover_pct")) is not None
        else _to_float(modifier.get("cloud_cover_low_pct")),
        "cavok": bool(metar.get("cavok")),
        "visibility_good": visibility_good,
        "visibility_poor": visibility_poor,
        "precip_observed": bool(metar.get("precip_observed")),
        "showery_or_convective": bool(metar.get("showery_or_convective")),
        "no_precip_confirmed": bool(metar.get("no_precip_confirmed")),
        "dewpoint_c": _to_float(dewpoint.get("dewpoint_c")) or _to_float(modifier.get("dewpoint_c")),
        "temp_dewpoint_spread_c": _to_float(dewpoint.get("temp_dewpoint_spread_c")),
        "dewpoint_trend_c": _to_float(dewpoint.get("dewpoint_trend_c")),
        "drying": bool(dewpoint.get("drying")),
        "moistening": bool(dewpoint.get("moistening")),
        "high_dewpoint": bool(dewpoint.get("high_dewpoint")),
        "small_temp_dewpoint_spread": bool(dewpoint.get("small_temp_dewpoint_spread")),
        "large_temp_dewpoint_spread": bool(dewpoint.get("large_temp_dewpoint_spread")),
        "pressure_hpa": _to_float(pressure.get("pressure_hpa")) or _to_float(modifier.get("pressure_hpa")),
        "pressure_trend_hpa": _to_float(pressure.get("pressure_trend_hpa")),
        "metar_raw": raw,
    }


def state_probabilities(inputs: Mapping[str, Any]) -> dict[str, float]:
    cloud = _to_float(inputs.get("effective_cloud_cover_pct"))
    low_cloud = _to_float(inputs.get("low_cloud_cover_pct"))
    if cloud is None:
        cloud = 55.0
    if low_cloud is None:
        low_cloud = min(cloud, 55.0)

    clear_score = _clamp(1.0 - cloud / 100.0, 0.0, 1.0)
    low_cloud_score = _clamp(low_cloud / 80.0, 0.0, 1.0)
    visibility_good = 1.0 if inputs.get("visibility_good") else 0.0
    visibility_poor = 1.0 if inputs.get("visibility_poor") else 0.0
    no_precip = 1.0 if inputs.get("no_precip_confirmed") else 0.0
    precip = 1.0 if inputs.get("precip_observed") else 0.0
    convective = 1.0 if inputs.get("showery_or_convective") else 0.0

    spread = _to_float(inputs.get("temp_dewpoint_spread_c"))
    if spread is None:
        spread = 4.0
    spread_open = _clamp((spread - 3.0) / 5.0, 0.0, 1.0)
    spread_closed = _clamp((4.0 - spread) / 4.0, 0.0, 1.0)
    drying = 1.0 if inputs.get("drying") or (_to_float(inputs.get("dewpoint_trend_c")) or 0.0) < -0.5 else 0.0
    moistening = 1.0 if inputs.get("moistening") or (_to_float(inputs.get("dewpoint_trend_c")) or 0.0) > 0.5 else 0.0
    high_dewpoint = 1.0 if inputs.get("high_dewpoint") else 0.0

    scores = {
        "S0_wet_layer_closed": (
            0.25
            + 1.20 * low_cloud_score
            + 1.20 * spread_closed
            + 0.70 * visibility_poor
            + 0.85 * precip
            + 0.35 * moistening
            + 0.25 * high_dewpoint
            - 0.30 * visibility_good
        ),
        "S1_opening": (
            0.55
            + 0.55 * visibility_good
            + 0.45 * drying
            + 0.45 * no_precip
            + 0.35 * (1.0 - abs(clear_score - 0.45))
            + 0.20 * spread_open
            - 0.25 * precip
        ),
        "S2_open_boundary_layer": (
            0.10
            + 1.20 * clear_score
            + 0.90 * spread_open
            + 0.65 * visibility_good
            + 0.40 * no_precip
            + 0.35 * drying
            - 0.80 * low_cloud_score
            - 1.00 * precip
        ),
        "S3_convective_cutoff": (
            -0.20
            + 2.00 * convective
            + 1.15 * precip
            + 0.45 * visibility_poor
            + 0.30 * high_dewpoint
            - 0.25 * no_precip
        ),
    }
    return _softmax(scores)


def _remaining_warming_params(
    *,
    state: str,
    local_hour: float,
    momentum_rate_c_per_hour: float,
) -> tuple[float, float]:
    remaining_hours = _clamp(17.5 - local_hour, 0.0, 8.5)
    rate_up = max(0.0, momentum_rate_c_per_hour)
    rate_down = max(0.0, -momentum_rate_c_per_hour)
    rate_horizon = min(remaining_hours, 3.0)

    if state == "S0_wet_layer_closed":
        mu = 0.15 + 0.23 * remaining_hours + 0.10 * rate_up * rate_horizon - 0.20 * rate_down
        sigma = 0.85
    elif state == "S1_opening":
        mu = 0.45 + 0.42 * remaining_hours + 0.38 * rate_up * rate_horizon - 0.15 * rate_down
        sigma = 1.05
    elif state == "S2_open_boundary_layer":
        mu = 0.75 + 0.58 * remaining_hours + 0.55 * rate_up * rate_horizon - 0.10 * rate_down
        sigma = 1.15
    elif state == "S3_convective_cutoff":
        mu = 0.05 + 0.12 * remaining_hours + 0.08 * rate_up * rate_horizon - 0.35 * rate_down
        sigma = 0.95
    else:
        mu = 0.35 + 0.30 * remaining_hours + 0.25 * rate_up * rate_horizon
        sigma = 1.10

    if local_hour >= 14.0:
        mu *= _clamp((17.5 - local_hour) / 3.5, 0.05, 1.0)
        sigma = max(0.55, sigma - 0.10)
    elif local_hour <= 10.0:
        sigma += 0.25

    cap = 1.0 + 1.15 * remaining_hours
    return (_clamp(mu, -0.25, cap), sigma)


def hierarchical_joint_distribution(
    *,
    buckets: list[Bucket],
    prior: list[float],
    inputs: Mapping[str, Any],
) -> tuple[Optional[list[float]], dict[str, float]]:
    current_temp = _to_float(inputs.get("current_temp_c"))
    today_max = _to_float(inputs.get("today_max_c"))
    if current_temp is None and today_max is None:
        return (None, {})
    if current_temp is None:
        current_temp = today_max
    if today_max is None:
        today_max = current_temp
    assert current_temp is not None
    assert today_max is not None

    local_hour = _to_float(inputs.get("local_hour")) or 12.0
    momentum = _to_float(inputs.get("momentum_rate_c_per_hour")) or 0.0
    states = state_probabilities(inputs)

    likelihood = [0.0 for _ in buckets]
    base_temp = max(current_temp, today_max)
    for state, state_prob in states.items():
        delta_mu, sigma = _remaining_warming_params(
            state=state,
            local_hour=local_hour,
            momentum_rate_c_per_hour=momentum,
        )
        peak_mu = max(today_max, base_temp + delta_mu)
        for idx, bucket in enumerate(buckets):
            if bucket.upper_c is not None and bucket.upper_c <= today_max:
                prob = 0.0
            else:
                prob = _normal_interval_prob(bucket.lower_c, bucket.upper_c, mu=peak_mu, sigma=sigma)
            likelihood[idx] += state_prob * prob

    normalized_likelihood = _normalize(likelihood, floor=EPS)
    if normalized_likelihood is None:
        return (None, states)
    reweighted = [
        (max(prior[idx], EPS) + 0.0005) * max(normalized_likelihood[idx], EPS)
        for idx in range(len(buckets))
    ]
    return (_normalize(reweighted), states)


def score_distribution(probs: list[float], actual_index: int) -> dict[str, float | int | bool]:
    n = len(probs)
    p_actual = max(EPS, probs[actual_index])
    top_index = max(range(n), key=lambda idx: probs[idx])
    brier = 0.0
    rps = 0.0
    pred_cum = 0.0
    actual_cum = 0.0
    for idx, prob in enumerate(probs):
        y = 1.0 if idx == actual_index else 0.0
        brier += (prob - y) ** 2
        pred_cum += prob
        actual_cum += y
        if idx < n - 1:
            rps += (pred_cum - actual_cum) ** 2
    if n > 1:
        rps /= n - 1
    return {
        "nll": -math.log(p_actual),
        "brier": brier,
        "rps": rps,
        "actual_prob": p_actual,
        "top_prob": probs[top_index],
        "top_hit": top_index == actual_index,
        "top_index": top_index,
    }


def _window(local_hour: float) -> str:
    if local_hour < 9:
        return "pre_09"
    if local_hour < 11:
        return "09_11"
    if local_hour < 13:
        return "11_13"
    if local_hour < 15:
        return "13_15"
    return "15_18"


def _load_actuals(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            event_date,
            MAX(value) AS actual_c,
            COUNT(*) AS source_rows,
            MIN(created_at) AS first_seen_at,
            MAX(created_at) AS last_seen_at,
            GROUP_CONCAT(DISTINCT source_kind) AS source_kinds
        FROM station_observations
        WHERE station_id = ?
          AND market_var = 'temperature_2m_max'
          AND value IS NOT NULL
        GROUP BY event_date
        """,
        (STATION_ID,),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        actual = _to_float(row["actual_c"])
        event_date = str(row["event_date"] or "")
        if actual is None or not event_date:
            continue
        out[event_date] = {
            "actual_c": actual,
            "source_rows": int(row["source_rows"] or 0),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "source_kinds": row["source_kinds"],
            "truth_rule": "max(station_observations.value) for ZUUU temperature_2m_max",
        }
    return out


def _event_dates_with_facts(conn: sqlite3.Connection, actuals: Mapping[str, Any]) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT event_date
        FROM event_fact_history
        WHERE lower(slug) LIKE '%chengdu%'
        ORDER BY event_date
        """
    ).fetchall()
    return [str(row["event_date"]) for row in rows if str(row["event_date"]) in actuals]


def _iter_fact_rows(
    conn: sqlite3.Connection,
    *,
    event_dates: list[str],
    min_local_hour: float,
    max_local_hour: float,
    sample_minutes: int,
) -> Iterable[sqlite3.Row]:
    if not event_dates:
        return

    slot_seconds = max(60, sample_minutes * 60)
    for event_date in event_dates:
        local_midnight = datetime.fromisoformat(f"{event_date}T00:00:00").replace(tzinfo=EVENT_TZ)
        utc_start = (local_midnight + timedelta(hours=min_local_hour)).astimezone(timezone.utc)
        utc_end = (local_midnight + timedelta(hours=max_local_hour)).astimezone(timezone.utc)
        start_text = utc_start.isoformat()
        end_text = utc_end.isoformat()
        if sample_minutes > 0:
            rows = conn.execute(
                """
                WITH filtered AS (
                    SELECT
                        id,
                        event_date,
                        CAST(strftime('%s', fact_as_of) / ? AS INTEGER) AS sample_slot
                    FROM event_fact_history
                    WHERE lower(slug) LIKE '%chengdu%'
                      AND event_date = ?
                      AND fact_as_of >= ?
                      AND fact_as_of <= ?
                      AND json_type(fact_json, '$.posterior_temperature_distribution.bucket_probabilities') = 'array'
                ),
                chosen AS (
                    SELECT MIN(id) AS id
                    FROM filtered
                    GROUP BY event_date, sample_slot
                )
                SELECT h.id, h.slug, h.event_date, h.fact_as_of, h.fact_json
                FROM event_fact_history h
                JOIN chosen c ON c.id = h.id
                ORDER BY h.fact_as_of, h.id
                """,
                (slot_seconds, event_date, start_text, end_text),
            )
        else:
            rows = conn.execute(
                """
                SELECT id, slug, event_date, fact_as_of, fact_json
                FROM event_fact_history
                WHERE lower(slug) LIKE '%chengdu%'
                  AND event_date = ?
                  AND fact_as_of >= ?
                  AND fact_as_of <= ?
                  AND json_type(fact_json, '$.posterior_temperature_distribution.bucket_probabilities') = 'array'
                ORDER BY fact_as_of, id
                """,
                (event_date, start_text, end_text),
            )
        for row in rows:
            yield row


def build_samples(
    conn: sqlite3.Connection,
    *,
    min_local_hour: float,
    max_local_hour: float,
    sample_minutes: int,
    max_samples_per_date: int,
) -> tuple[list[Sample], dict[str, Any]]:
    actuals = _load_actuals(conn)
    event_dates = _event_dates_with_facts(conn, actuals)
    skip = Counter()
    samples: list[Sample] = []
    seen_slots: set[tuple[str, int]] = set()
    per_date = Counter()

    for row in _iter_fact_rows(
        conn,
        event_dates=event_dates,
        min_local_hour=min_local_hour,
        max_local_hour=max_local_hour,
        sample_minutes=sample_minutes,
    ):
        fact_dt = _parse_dt(row["fact_as_of"])
        event_date = str(row["event_date"] or "")
        if fact_dt is None:
            skip["bad_fact_as_of"] += 1
            continue
        local_dt = fact_dt.astimezone(EVENT_TZ)
        if local_dt.date().isoformat() != event_date:
            skip["not_event_local_date"] += 1
            continue
        local_hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0
        if local_hour < min_local_hour or local_hour > max_local_hour:
            skip["outside_local_window"] += 1
            continue
        if max_samples_per_date > 0 and per_date[event_date] >= max_samples_per_date:
            skip["max_samples_per_date"] += 1
            continue
        slot = int(local_dt.timestamp() // max(60, sample_minutes * 60))
        slot_key = (event_date, slot)
        if sample_minutes > 0 and slot_key in seen_slots:
            skip["sample_slot_deduped"] += 1
            continue

        fact = _json_obj(row["fact_json"])
        buckets, posterior = _extract_posterior(fact)
        if posterior is None or not buckets:
            skip["missing_posterior_distribution"] += 1
            continue
        actual = actuals.get(event_date)
        if not actual:
            skip["missing_actual"] += 1
            continue
        actual_c = float(actual["actual_c"])
        actual_idx = actual_bucket_index(actual_c, buckets)
        if actual_idx is None:
            skip["actual_outside_buckets"] += 1
            continue
        inputs = _extract_calibration_inputs(fact, local_hour)
        hierarchical, states = hierarchical_joint_distribution(
            buckets=buckets,
            prior=posterior,
            inputs=inputs,
        )
        if hierarchical is None:
            skip["missing_calibration_observation_inputs"] += 1
            continue
        legacy = _extract_legacy_path(fact, buckets)

        sample = Sample(
            slug=str(row["slug"] or ""),
            event_date=event_date,
            fact_id=int(row["id"]),
            fact_as_of=str(row["fact_as_of"] or ""),
            fact_as_of_local=local_dt.isoformat(),
            local_hour=round(local_hour, 4),
            window=_window(local_hour),
            actual_c=actual_c,
            actual_bucket=buckets[actual_idx].label,
            actual_index=actual_idx,
            buckets=buckets,
            old_posterior=posterior,
            legacy_path=legacy,
            hierarchical_joint=hierarchical,
            state_probs=states,
            calibration_inputs={
                key: value
                for key, value in inputs.items()
                if key != "metar_raw"
            },
        )
        samples.append(sample)
        seen_slots.add(slot_key)
        per_date[event_date] += 1

    coverage = {
        "actual_event_dates": len(actuals),
        "event_dates_with_chengdu_facts_and_actuals": event_dates,
        "actuals_used": {date: actuals[date] for date in event_dates},
        "sample_minutes": sample_minutes,
        "max_samples_per_date": max_samples_per_date,
        "skip_counts": dict(skip),
        "samples_by_date": dict(per_date),
        "scored_event_dates": sorted(per_date),
    }
    return samples, coverage


def _model_vectors(sample: Sample) -> dict[str, Optional[list[float]]]:
    return {
        "old_posterior": sample.old_posterior,
        "legacy_path_normalized": sample.legacy_path,
        "hierarchical_joint_calibration_v0": sample.hierarchical_joint,
    }


def _reliability_bin(top_prob: float) -> str:
    upper = min(100, int(math.ceil(top_prob * 5.0) * 20))
    lower = max(0, upper - 20)
    if upper == 0:
        upper = 20
    return f"{lower:02d}-{upper:02d}%"


def summarize_scores(samples: list[Sample]) -> dict[str, Any]:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_window: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    reliability: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))

    for sample in samples:
        for model, probs in _model_vectors(sample).items():
            if probs is None:
                continue
            score = score_distribution(probs, sample.actual_index)
            score["event_date"] = sample.event_date
            score["window"] = sample.window
            score["top_bucket"] = sample.buckets[int(score["top_index"])].label
            score["actual_bucket"] = sample.actual_bucket
            by_model[model].append(score)
            by_window[sample.window][model].append(score)
            reliability[model][_reliability_bin(float(score["top_prob"]))].append(score)

    def agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"n": 0}
        return {
            "n": len(rows),
            "n_dates": len({str(row["event_date"]) for row in rows}),
            "mean_nll": mean(float(row["nll"]) for row in rows),
            "mean_brier": mean(float(row["brier"]) for row in rows),
            "mean_rps": mean(float(row["rps"]) for row in rows),
            "mean_actual_prob": mean(float(row["actual_prob"]) for row in rows),
            "top_hit_rate": mean(1.0 if row["top_hit"] else 0.0 for row in rows),
            "mean_top_confidence": mean(float(row["top_prob"]) for row in rows),
        }

    return {
        "overall": {model: agg(rows) for model, rows in by_model.items()},
        "by_window": {
            window: {model: agg(rows) for model, rows in models.items()}
            for window, models in sorted(by_window.items())
        },
        "reliability": {
            model: {
                bin_name: {
                    "n": len(rows),
                    "avg_confidence": mean(float(row["top_prob"]) for row in rows),
                    "hit_rate": mean(1.0 if row["top_hit"] else 0.0 for row in rows),
                }
                for bin_name, rows in sorted(bins.items())
            }
            for model, bins in reliability.items()
        },
    }


def _decision(scores: Mapping[str, Any], coverage: Mapping[str, Any]) -> dict[str, Any]:
    overall = scores.get("overall", {}) if isinstance(scores, Mapping) else {}
    old = overall.get("old_posterior", {}) if isinstance(overall, Mapping) else {}
    new = overall.get("hierarchical_joint_calibration_v0", {}) if isinstance(overall, Mapping) else {}
    n_dates = int(new.get("n_dates") or 0) if isinstance(new, Mapping) else 0
    n = int(new.get("n") or 0) if isinstance(new, Mapping) else 0
    if n_dates < 10:
        return {
            "status": "calibration_only_not_ready_for_ssot",
            "reason": "可计分的同口径生产 posterior 快照日期少于 10 天；只能做方向性诊断，不能作为 SSOT 接入口径。",
            "required_next_step": "继续积累同口径事实快照与结算标签，或经确认后生成离线历史特征样本再训练/校准。",
        }
    old_nll = _to_float(old.get("mean_nll")) if isinstance(old, Mapping) else None
    new_nll = _to_float(new.get("mean_nll")) if isinstance(new, Mapping) else None
    old_rps = _to_float(old.get("mean_rps")) if isinstance(old, Mapping) else None
    new_rps = _to_float(new.get("mean_rps")) if isinstance(new, Mapping) else None
    if old_nll is not None and new_nll is not None and old_rps is not None and new_rps is not None:
        if new_nll <= old_nll * 0.97 and new_rps <= old_rps * 0.97 and n >= 80:
            return {
                "status": "candidate_for_extended_calibration",
                "reason": "calibration 分层联合似然在当前样本的 NLL/RPS 均优于旧 posterior，但仍需更长样本和逐日去相关验证。",
                "required_next_step": "接入只读审计面板观察，不接交易判断；累计更多日期后再评估 SSOT。",
            }
    return {
        "status": "calibration_only_not_ready_for_ssot",
        "reason": "当前样本不足或改进不稳定，不能证明分层联合似然优于旧轨迹/旧 posterior。",
        "required_next_step": "扩大只读 calibration 样本并检查按时段的校准偏差。",
    }


def _sample_preview(samples: list[Sample], limit: int = 12) -> list[dict[str, Any]]:
    out = []
    for sample in samples[:limit]:
        model_rows = {}
        for model, probs in _model_vectors(sample).items():
            if probs is None:
                continue
            score = score_distribution(probs, sample.actual_index)
            model_rows[model] = {
                "actual_prob": score["actual_prob"],
                "top_bucket": sample.buckets[int(score["top_index"])].label,
                "top_prob": score["top_prob"],
                "top_hit": score["top_hit"],
            }
        out.append(
            {
                "event_date": sample.event_date,
                "fact_as_of_local": sample.fact_as_of_local,
                "window": sample.window,
                "actual_c": sample.actual_c,
                "actual_bucket": sample.actual_bucket,
                "state_probs": sample.state_probs,
                "calibration_inputs": sample.calibration_inputs,
                "models": model_rows,
            }
        )
    return out


def _round_floats(obj: Any, digits: int = 6) -> Any:
    if isinstance(obj, float):
        return round(obj, digits)
    if isinstance(obj, list):
        return [_round_floats(item, digits) for item in obj]
    if isinstance(obj, dict):
        return {key: _round_floats(value, digits) for key, value in obj.items()}
    return obj


def build_report(
    *,
    samples: list[Sample],
    coverage: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    scores = summarize_scores(samples)
    report = {
        "report_type": "chengdu_hierarchical_likelihood_calibration_report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "guards": {
            "mode": "read_only_calibration_backtest",
            "sqlite_open_mode": "mode=ro",
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "history_backfill": False,
            "ssot_integration": False,
            "report_writes_only": True,
        },
        "scope": {
            "station_id": STATION_ID,
            "market": "Highest temperature in Chengdu",
            "event_tz": str(EVENT_TZ),
            "min_local_hour": args.min_local_hour,
            "max_local_hour": args.max_local_hour,
            "truth_source": "station_observations, max(value) per event_date for ZUUU temperature_2m_max",
            "model_notes": {
                "old_posterior": "archived posterior_temperature_distribution.bucket_probabilities",
                "legacy_path_normalized": "temperature_path_distribution.bucket_distribution path_probability normalized when available",
                "hierarchical_joint_calibration_v0": "old posterior used as P0, then reweighted by mixture over S0/S1/S2/S3 environment state and momentum-conditioned remaining warming likelihood",
            },
        },
        "coverage": coverage,
        "sample_count": len(samples),
        "scores": scores,
        "decision": _decision(scores, coverage),
        "sample_preview": _sample_preview(samples),
    }
    return _round_floats(report)


def _fmt(value: Any, digits: int = 4) -> str:
    val = _to_float(value)
    if val is None:
        return ""
    return f"{val:.{digits}f}"


def render_markdown(report: Mapping[str, Any]) -> str:
    scores = report.get("scores", {})
    overall = scores.get("overall", {}) if isinstance(scores, Mapping) else {}
    coverage = report.get("coverage", {})
    decision = report.get("decision", {})
    lines = [
        "# Chengdu Hierarchical Likelihood Calibration Backtest",
        "",
        "## Guardrails",
        "",
        "- 只读 calibration/backtest；SQLite 使用 `mode=ro` 打开。",
        "- 不写 `archive.db`、不写 facts、不会触发 `auto_trade_decisions`、不会写 signal、不会下单、不会历史回填、不会接入 SSOT。",
        "- 报告只写入 `data/reports`。",
        "",
        "## Data Scope",
        "",
        f"- Station: `{report.get('scope', {}).get('station_id')}`",
        f"- Samples: `{report.get('sample_count')}`",
        f"- Scored event dates: `{', '.join(coverage.get('scored_event_dates', []))}`",
        f"- Event dates with any Chengdu fact and WU truth: `{', '.join(coverage.get('event_dates_with_chengdu_facts_and_actuals', []))}`",
        f"- Truth rule: `{report.get('scope', {}).get('truth_source')}`",
        f"- Skip counts: `{json.dumps(coverage.get('skip_counts', {}), ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Overall Calibration",
        "",
        "| Model | n | dates | NLL ↓ | Brier ↓ | RPS ↓ | Actual Prob ↑ | Top Hit | Top Conf |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model in ("old_posterior", "legacy_path_normalized", "hierarchical_joint_calibration_v0"):
        row = overall.get(model, {}) if isinstance(overall, Mapping) else {}
        lines.append(
            "| {model} | {n} | {dates} | {nll} | {brier} | {rps} | {actual_prob} | {hit} | {conf} |".format(
                model=model,
                n=row.get("n", 0),
                dates=row.get("n_dates", 0),
                nll=_fmt(row.get("mean_nll")),
                brier=_fmt(row.get("mean_brier")),
                rps=_fmt(row.get("mean_rps")),
                actual_prob=_fmt(row.get("mean_actual_prob")),
                hit=_fmt(row.get("top_hit_rate")),
                conf=_fmt(row.get("mean_top_confidence")),
            )
        )
    lines.extend(["", "## By Window", ""])
    by_window = scores.get("by_window", {}) if isinstance(scores, Mapping) else {}
    for window, models in by_window.items():
        lines.extend(
            [
                f"### {window}",
                "",
                "| Model | n | NLL ↓ | Brier ↓ | RPS ↓ | Actual Prob ↑ | Top Hit |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for model in ("old_posterior", "legacy_path_normalized", "hierarchical_joint_calibration_v0"):
            row = models.get(model, {}) if isinstance(models, Mapping) else {}
            if not row:
                continue
            lines.append(
                "| {model} | {n} | {nll} | {brier} | {rps} | {actual_prob} | {hit} |".format(
                    model=model,
                    n=row.get("n", 0),
                    nll=_fmt(row.get("mean_nll")),
                    brier=_fmt(row.get("mean_brier")),
                    rps=_fmt(row.get("mean_rps")),
                    actual_prob=_fmt(row.get("mean_actual_prob")),
                    hit=_fmt(row.get("top_hit_rate")),
                )
            )
        lines.append("")
    lines.extend(
        [
            "## Decision",
            "",
            f"- Status: `{decision.get('status')}`",
            f"- Reason: {decision.get('reason')}",
            f"- Required next step: {decision.get('required_next_step')}",
            "",
            "## Caveats",
            "",
            "- `hierarchical_joint_calibration_v0` 是只读诊断模型；P0 暂用归档 production posterior，因此不能视作完全独立的新生产 posterior。",
            "- `legacy_path_normalized` 只在 archived `temperature_path_distribution` 给出可归一化路径概率时计分。",
            "- 样本以生产事实快照和 `station_observations` 真值交集为准；更早只有结算观测、缺少同口径 fact 快照时不强行回填。",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument("--md-report", type=Path, default=DEFAULT_MD_REPORT)
    parser.add_argument("--min-local-hour", type=float, default=9.0)
    parser.add_argument("--max-local-hour", type=float, default=17.5)
    parser.add_argument("--sample-minutes", type=int, default=10)
    parser.add_argument("--max-samples-per-date", type=int, default=160)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db}")
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        samples, coverage = build_samples(
            conn,
            min_local_hour=args.min_local_hour,
            max_local_hour=args.max_local_hour,
            sample_minutes=args.sample_minutes,
            max_samples_per_date=args.max_samples_per_date,
        )
    finally:
        conn.close()

    report = build_report(samples=samples, coverage=coverage, args=args)
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.md_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    args.md_report.write_text(render_markdown(report))
    print(json.dumps({
        "sample_count": report["sample_count"],
        "json_report": str(args.json_report),
        "md_report": str(args.md_report),
        "decision": report["decision"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
