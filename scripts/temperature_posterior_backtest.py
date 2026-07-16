#!/usr/bin/env python3
"""Backtest gate for Continuous Temperature Posterior v1."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"

sys.path.insert(0, str(ROOT))
from posterior_pipeline_entry_runtime import apply_bayesian_probability_update  # noqa: E402
from temperature_posterior_runtime import temperature_bucket_bounds  # noqa: E402


EPS = 1e-12
HISTORICAL_FACT_SAMPLES_PER_EVENT = 50


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


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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


def _actual_rank(source: str) -> int:
    if source == "event_runs":
        return 0
    if source == "event_daily_temps.exact":
        return 1
    if source == "event_daily_temps.date":
        return 2
    return 9


def _record_actual(
    target: Dict[Tuple[str, str], Dict[str, Any]],
    key: Tuple[str, str],
    *,
    actual: float,
    source: str,
    created_at: Any,
) -> None:
    current = target.get(key)
    candidate = {
        "actual_c": float(actual),
        "source": source,
        "created_at": str(created_at or ""),
        "rank": _actual_rank(source),
    }
    if current is None:
        target[key] = candidate
        return
    current_dt = _parse_dt(current.get("created_at"))
    candidate_dt = _parse_dt(candidate.get("created_at"))
    if candidate["rank"] < int(current.get("rank", 9)):
        target[key] = candidate
    elif candidate["rank"] == int(current.get("rank", 9)) and (
        candidate_dt is not None
        and (current_dt is None or candidate_dt >= current_dt)
    ):
        target[key] = candidate


def _load_actual_index(
    conn: sqlite3.Connection,
) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_date: Dict[str, Dict[str, Any]] = {}
    if _table_exists(conn, "event_runs"):
        cols = _column_names(conn, "event_runs")
        if {"slug", "event_date", "settlement_actual"}.issubset(cols):
            created_expr = "created_at" if "created_at" in cols else "'' AS created_at"
            for row in conn.execute(
                f"""
                SELECT slug, event_date, settlement_actual, {created_expr}
                FROM event_runs
                WHERE settlement_actual IS NOT NULL
                ORDER BY event_date, slug, created_at
                """
            ).fetchall():
                actual = _to_float(row["settlement_actual"])
                slug = str(row["slug"] or "")
                event_date = str(row["event_date"] or "")
                if actual is None or not slug or not event_date:
                    continue
                _record_actual(
                    by_key,
                    (slug, event_date),
                    actual=float(actual),
                    source="event_runs",
                    created_at=row["created_at"],
                )
    if _table_exists(conn, "event_daily_temps"):
        cols = _column_names(conn, "event_daily_temps")
        if {"event_date", "settlement_actual"}.issubset(cols):
            slug_expr = "slug" if "slug" in cols else "'' AS slug"
            created_expr = "created_at" if "created_at" in cols else "'' AS created_at"
            for row in conn.execute(
                f"""
                SELECT {slug_expr}, event_date, settlement_actual, {created_expr}
                FROM event_daily_temps
                WHERE settlement_actual IS NOT NULL
                ORDER BY event_date, slug, created_at
                """
            ).fetchall():
                actual = _to_float(row["settlement_actual"])
                event_date = str(row["event_date"] or "")
                if actual is None or not event_date:
                    continue
                slug = str(row["slug"] or "")
                if slug:
                    _record_actual(
                        by_key,
                        (slug, event_date),
                        actual=float(actual),
                        source="event_daily_temps.exact",
                        created_at=row["created_at"],
                    )
                current = by_date.get(event_date)
                candidate = {
                    "actual_c": float(actual),
                    "source": "event_daily_temps.date",
                    "created_at": str(row["created_at"] or ""),
                    "rank": _actual_rank("event_daily_temps.date"),
                }
                current_dt = _parse_dt(current.get("created_at")) if current else None
                candidate_dt = _parse_dt(candidate.get("created_at"))
                if current is None or (
                    candidate_dt is not None
                    and (current_dt is None or candidate_dt >= current_dt)
                ):
                    by_date[event_date] = candidate
    return by_key, by_date


def _iter_latest_facts(db_path: Path) -> Iterable[Dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT h.slug, h.event_date, h.fact_as_of, h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            ORDER BY h.event_date, h.slug
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for row in rows:
        payload = _json_obj(row["fact_json"])
        if payload:
            payload.setdefault("slug", row["slug"])
            payload.setdefault("event_date", row["event_date"])
            payload.setdefault("fact_as_of", row["fact_as_of"])
            out.append(payload)
    return out


def _iter_historical_backtest_facts(db_path: Path) -> Iterable[Dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "event_fact_history"):
            return []
        cols = _column_names(conn, "event_fact_history")
        if not {"slug", "event_date", "fact_json"}.issubset(cols):
            return []
        actual_by_key, actual_by_date = _load_actual_index(conn)
        actual_dates = sorted(
            {
                event_date
                for _, event_date in actual_by_key.keys()
            }
            | set(actual_by_date.keys())
        )
        if not actual_dates:
            return []
        fact_as_of_expr = "fact_as_of" if "fact_as_of" in cols else "created_at"
        if fact_as_of_expr not in cols:
            fact_as_of_expr = "''"
        id_expr = "id" if "id" in cols else "rowid"
        placeholders = ",".join("?" for _ in actual_dates)
        rows = conn.execute(
            f"""
            WITH ranked AS (
              SELECT
                {id_expr} AS fact_id,
                slug,
                event_date,
                {fact_as_of_expr} AS fact_as_of,
                fact_json,
                ROW_NUMBER() OVER (
                  PARTITION BY slug, event_date
                  ORDER BY {fact_as_of_expr} DESC, {id_expr} DESC
                ) AS rn
              FROM event_fact_history
              WHERE event_date IN ({placeholders})
                AND lower(slug) LIKE '%temperature%'
            )
            SELECT fact_id, slug, event_date, fact_as_of, fact_json, rn
            FROM ranked
            WHERE rn <= ?
            ORDER BY event_date DESC, slug, rn ASC
            """,
            [*actual_dates, HISTORICAL_FACT_SAMPLES_PER_EVENT],
        ).fetchall()
    except sqlite3.Error:
        rows = []
        actual_by_key, actual_by_date = {}, {}
    finally:
        conn.close()

    out: List[Dict[str, Any]] = []
    for row in rows:
        slug = str(row["slug"] or "")
        event_date = str(row["event_date"] or "")
        payload = _json_obj(row["fact_json"])
        if not payload:
            continue
        actual_meta = actual_by_key.get((slug, event_date)) or actual_by_date.get(event_date)
        if actual_meta:
            payload["_backtest_actual_c"] = actual_meta.get("actual_c")
            payload["_backtest_actual_source"] = actual_meta.get("source")
        payload.setdefault("slug", slug)
        payload.setdefault("event_date", event_date)
        payload.setdefault("fact_as_of", row["fact_as_of"])
        payload["_backtest_fact_id"] = row["fact_id"]
        payload["_backtest_sample_rank"] = row["rn"]
        out.append(payload)
    return out


def _iter_backtest_facts(db_path: Path) -> Iterable[Dict[str, Any]]:
    historical = list(_iter_historical_backtest_facts(db_path))
    if historical:
        return historical
    return _iter_latest_facts(db_path)


def _actual_temp_c(payload: Mapping[str, Any]) -> Optional[float]:
    actual = _to_float(payload.get("_backtest_actual_c"))
    if actual is not None:
        return float(actual)
    features = payload.get("weather_likelihood_features")
    if not isinstance(features, Mapping):
        features = payload.get("weather_likelihood_features_compact")
    if not isinstance(features, Mapping):
        features = {}
    for source in (payload, features):
        for key in ("settlement_actual_c", "settlement_actual", "official_max_c", "final_today_max_c"):
            val = _to_float(source.get(key))
            if val is not None:
                return float(val)
    return None


def _bucket_hit(actual_c: float, label: str, rounding_rule: Optional[str]) -> bool:
    bounds = temperature_bucket_bounds(label, rounding_rule)
    if bounds is None:
        return False
    low, high = bounds
    return float(actual_c) >= low and float(actual_c) < high


def _ctp_probs(payload: Mapping[str, Any]) -> Dict[str, float]:
    posterior = payload.get("posterior_temperature_distribution")
    if not isinstance(posterior, Mapping):
        return {}
    rows = posterior.get("bucket_probabilities")
    if not isinstance(rows, list):
        return {}
    out: Dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("outcome") or "")
        prob = _to_float(row.get("probability"))
        if label and prob is not None:
            out[label] = max(0.0, min(1.0, float(prob)))
    return out


def _probability_input(row: Mapping[str, Any]) -> float:
    for key in (
        "q_raw_static",
        "q_ensemble_qm_raw_bucket",
        "q_model_temperature_bucket_round",
        "q_raw_pre_hard_source_override",
        "q_round_static",
        "q_raw",
        "q_round",
        "q_temperature_bucket_posterior_pre_observed_constraints",
        "q_bayes_posterior_pre_observed_constraints",
        "q_temperature_bucket_posterior",
        "q_bayes_posterior",
    ):
        val = _to_float(row.get(key))
        if val is not None:
            return max(0.0, min(1.0, float(val)))
    return 0.0


def _replay_features(payload: Mapping[str, Any], rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    features = payload.get("weather_likelihood_features")
    if not isinstance(features, Mapping):
        features = payload.get("weather_likelihood_features_compact")
    out: Dict[str, Any] = dict(features) if isinstance(features, Mapping) else {}
    for key in (
        "settlement_actual",
        "settlement_actual_c",
        "settlement_actual_source",
        "settlement_actual_at",
        "settlement_final_at",
        "official_max_c",
        "final_today_max_c",
        "final_bucket",
        "final_bucket_lock",
        "hard_source_final_bucket_lock",
        "hard_source_final_lock_reason",
        "hard_source_settlement_final",
        "hard_source_final_daily_max",
        "observed_today_max_c",
        "observed_today_max_source",
        "observed_today_max_final",
        "temperature_observation_final_daily_max",
    ):
        if key in payload and payload.get(key) is not None and key not in out:
            out[key] = payload.get(key)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        applied = str(row.get("hard_source_posterior_override_applied") or "").lower() in {"1", "true", "yes"}
        if applied:
            value = _to_float(row.get("hard_source_posterior_override_value_c"))
            if value is not None and "settlement_actual_c" not in out:
                out["settlement_actual_c"] = float(value)
            source = str(row.get("hard_source_posterior_override_source") or "").strip()
            if source and "settlement_actual_source" not in out:
                out["settlement_actual_source"] = source
            if row.get("hard_source_posterior_override_final_bucket_lock") is not None:
                out.setdefault(
                    "hard_source_final_bucket_lock",
                    row.get("hard_source_posterior_override_final_bucket_lock"),
                )
            reason = str(row.get("hard_source_posterior_override_final_lock_reason") or "").strip()
            if reason:
                out.setdefault("hard_source_final_lock_reason", reason)
    return out


def _replay_ctp_posterior(payload: Mapping[str, Any]) -> Dict[str, Any]:
    rows = payload.get("distribution")
    if not isinstance(rows, list) or not rows:
        return {}
    source_rows = [row for row in rows if isinstance(row, Mapping)]
    features = _replay_features(payload, source_rows)
    replay_items: List[Dict[str, Any]] = []
    for row in source_rows:
        label = str(row.get("outcome") or row.get("label") or "").strip()
        if not label:
            continue
        item = copy.deepcopy(dict(row))
        item["label"] = label
        item["q"] = {
            "q_raw": _probability_input(row),
            "q_round": _probability_input(row),
        }
        item["weather_likelihood_features"] = dict(features)
        if payload.get("rounding_rule") and not item.get("rounding_rule"):
            item["rounding_rule"] = payload.get("rounding_rule")
        replay_items.append(item)
    if not replay_items:
        return {}
    try:
        meta = apply_bayesian_probability_update(
            replay_items,
            weights={},
            model_cache_freshness=_to_float(payload.get("model_cache_freshness")),
            days_to_event=_to_float(payload.get("days_to_event")),
            lcb_stage_tz=str(payload.get("event_tz") or payload.get("tz") or ""),
            historical_context={"weather_likelihood_features": dict(features)},
        )
    except Exception as exc:
        return {"status": "replay_failed", "error": str(exc)}
    posterior = meta.get("posterior_temperature_distribution") if isinstance(meta, Mapping) else None
    return dict(posterior) if isinstance(posterior, Mapping) else {}


def _posterior_temperature_distribution(
    payload: Mapping[str, Any],
    *,
    force_replay: bool = False,
) -> Dict[str, Any]:
    if force_replay:
        replayed = _replay_ctp_posterior(payload)
        if isinstance(replayed, Mapping) and _ctp_probs({"posterior_temperature_distribution": replayed}):
            return dict(replayed)
    posterior = payload.get("posterior_temperature_distribution")
    if isinstance(posterior, Mapping) and _ctp_probs(payload):
        return dict(posterior)
    return _replay_ctp_posterior(payload)


def _baseline_probs(payload: Mapping[str, Any]) -> Dict[str, float]:
    rows = payload.get("distribution")
    if not isinstance(rows, list):
        return {}
    out: Dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("outcome") or row.get("label") or "")
        prob = _to_float(row.get("q_bayes_posterior"))
        if prob is None:
            prob = _to_float(row.get("q_temperature_bucket_posterior"))
        if label and prob is not None:
            out[label] = max(0.0, min(1.0, float(prob)))
    return out


def _top_label(probs: Mapping[str, float]) -> str:
    if not probs:
        return ""
    return max(probs.items(), key=lambda item: (float(item[1]), item[0]))[0]


def _brier(probs: Mapping[str, float], actual_c: float, rounding_rule: Optional[str]) -> Optional[float]:
    if not probs:
        return None
    return sum((float(p) - (1.0 if _bucket_hit(actual_c, label, rounding_rule) else 0.0)) ** 2 for label, p in probs.items())


def _logloss(probs: Mapping[str, float], actual_c: float, rounding_rule: Optional[str]) -> Optional[float]:
    if not probs:
        return None
    hit_prob = 0.0
    for label, prob in probs.items():
        if _bucket_hit(actual_c, label, rounding_rule):
            hit_prob += float(prob)
    return -math.log(max(EPS, min(1.0, hit_prob)))


def _bucket_center(label: str, rounding_rule: Optional[str]) -> Optional[float]:
    bounds = temperature_bucket_bounds(label, rounding_rule)
    if bounds is None:
        return None
    low, high = bounds
    if math.isfinite(low) and math.isfinite(high):
        return (float(low) + float(high)) / 2.0
    if math.isfinite(low):
        return float(low) + 1.0
    if math.isfinite(high):
        return float(high) - 1.0
    return None


def _crps_from_bucket_probs(
    probs: Mapping[str, float],
    actual_c: float,
    rounding_rule: Optional[str],
) -> Optional[float]:
    points: List[Tuple[float, float]] = []
    for label, prob in probs.items():
        center = _bucket_center(str(label), rounding_rule)
        if center is None:
            continue
        p = max(0.0, min(1.0, float(prob)))
        if p <= 0:
            continue
        points.append((float(center), p))
    total_prob = sum(p for _, p in points)
    if total_prob <= EPS:
        return None
    points = [(center, p / total_prob) for center, p in points]
    expected_abs_error = sum(p * abs(center - float(actual_c)) for center, p in points)
    expected_pairwise_abs = 0.0
    for center_a, prob_a in points:
        for center_b, prob_b in points:
            expected_pairwise_abs += prob_a * prob_b * abs(center_a - center_b)
    return expected_abs_error - 0.5 * expected_pairwise_abs


def _crps_from_cdf(posterior: Mapping[str, Any], actual_c: float) -> Optional[float]:
    rows = posterior.get("cdf_values") if isinstance(posterior, Mapping) else None
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    points: List[Tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        temp = _to_float(row.get("temp_c"))
        cdf = _to_float(row.get("cdf"))
        if temp is not None and cdf is not None:
            points.append((float(temp), max(0.0, min(1.0, float(cdf)))))
    if len(points) < 2:
        return None
    points.sort()
    total = 0.0
    width_total = 0.0
    for (x0, c0), (x1, c1) in zip(points, points[1:]):
        width = max(0.0, x1 - x0)
        if width <= 0:
            continue
        x_mid = (x0 + x1) / 2.0
        c_mid = (c0 + c1) / 2.0
        target = 1.0 if x_mid >= actual_c else 0.0
        total += (c_mid - target) ** 2 * width
        width_total += width
    if width_total <= EPS:
        return None
    return total


def _mean(values: List[float]) -> Optional[float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    return sum(vals) / len(vals) if vals else None


def _quantile_covered(posterior: Mapping[str, Any], actual_c: float) -> Optional[bool]:
    quantiles = posterior.get("quantiles_c") if isinstance(posterior, Mapping) else None
    if not isinstance(quantiles, Mapping):
        return None
    low = _to_float(quantiles.get("p05") or quantiles.get("q05") or quantiles.get("0.05"))
    high = _to_float(quantiles.get("p95") or quantiles.get("q95") or quantiles.get("0.95"))
    if low is None or high is None:
        return None
    return float(low) <= float(actual_c) <= float(high)


def _target_churn(samples: List[Dict[str, Any]], key: str) -> Optional[float]:
    changes = 0
    transitions = 0
    by_event: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for sample in samples:
        event_key = (str(sample.get("slug") or ""), str(sample.get("event_date") or ""))
        by_event.setdefault(event_key, []).append(sample)
    for rows in by_event.values():
        ordered = sorted(rows, key=lambda row: str(row.get("fact_as_of") or ""))
        if len(ordered) < 2:
            continue
        last_dt = _parse_dt(ordered[-1].get("fact_as_of"))
        if last_dt is not None:
            windowed = [
                row
                for row in ordered
                if (
                    _parse_dt(row.get("fact_as_of")) is not None
                    and 0 <= (last_dt - _parse_dt(row.get("fact_as_of"))).total_seconds() <= 3 * 3600
                )
            ]
            if len(windowed) >= 2:
                ordered = windowed
        labels = [str(row.get(key) or "") for row in ordered if row.get(key)]
        for prev, cur in zip(labels, labels[1:]):
            transitions += 1
            if prev != cur:
                changes += 1
    if transitions <= 0:
        return None
    return changes / transitions


def run_backtest(db_path: Path, min_settled: int) -> Dict[str, Any]:
    facts = list(_iter_backtest_facts(db_path))
    samples: List[Dict[str, Any]] = []
    for payload in facts:
        actual = _actual_temp_c(payload)
        if actual is None:
            continue
        rounding_rule = str(payload.get("rounding_rule") or payload.get("market_rounding_rule") or "whole_degrees")
        posterior = _posterior_temperature_distribution(payload)
        ctp = {}
        if posterior:
            payload_for_probs = dict(payload)
            payload_for_probs["posterior_temperature_distribution"] = posterior
            ctp = _ctp_probs(payload_for_probs)
        baseline = _baseline_probs(payload)
        baseline_top = _top_label(baseline)
        ctp_top = _top_label(ctp)
        hard_locked = isinstance(posterior, Mapping) and (
            str(posterior.get("status") or "") == "hard_source_locked"
            or str(posterior.get("source") or "").endswith("hard_source_override")
        )
        samples.append(
            {
                "slug": payload.get("slug"),
                "event_date": payload.get("event_date"),
                "fact_as_of": payload.get("fact_as_of"),
                "actual_c": actual,
                "rounding_rule": rounding_rule,
                "ctp_brier": _brier(ctp, actual, rounding_rule),
                "baseline_brier": _brier(baseline, actual, rounding_rule),
                "ctp_logloss": _logloss(ctp, actual, rounding_rule),
                "baseline_logloss": _logloss(baseline, actual, rounding_rule),
                "ctp_crps": _crps_from_bucket_probs(ctp, actual, rounding_rule),
                "baseline_crps": _crps_from_bucket_probs(baseline, actual, rounding_rule),
                "ctp_continuous_crps": _crps_from_cdf(posterior, actual) if isinstance(posterior, Mapping) else None,
                "ctp_quantile_covered": (
                    _quantile_covered(posterior, actual) if isinstance(posterior, Mapping) else None
                ),
                "d0_violation_count": (
                    (posterior.get("observation_constraints") or {}).get("d0_violation_count")
                    if isinstance(posterior, Mapping) and isinstance(posterior.get("observation_constraints"), Mapping)
                    else None
                ),
                "hard_source_locked": hard_locked,
                "hard_source_consistent": (
                    _bucket_hit(actual, ctp_top, rounding_rule) if hard_locked and ctp_top else None
                ),
                "has_ctp": bool(ctp),
                "has_baseline": bool(baseline),
                "ctp_top_label": ctp_top,
                "baseline_top_label": baseline_top,
            }
        )

    ctp_samples = [s for s in samples if s["has_ctp"]]
    comparable = [s for s in ctp_samples if s["has_baseline"]]
    baseline_churn = _target_churn(comparable, "baseline_top_label")
    ctp_churn = _target_churn(comparable, "ctp_top_label")
    hard_locked_count = sum(1 for s in ctp_samples if s.get("hard_source_locked"))
    hard_locked_consistent = sum(1 for s in ctp_samples if s.get("hard_source_consistent") is True)
    result = {
        "status": "ok",
        "db_path": str(db_path),
        "settled_samples": len(samples),
        "ctp_samples": len(ctp_samples),
        "comparable_samples": len(comparable),
        "min_settled": int(min_settled),
        "metrics": {
            "ctp_brier": _mean([s["ctp_brier"] for s in comparable if s["ctp_brier"] is not None]),
            "baseline_brier": _mean([s["baseline_brier"] for s in comparable if s["baseline_brier"] is not None]),
            "ctp_logloss": _mean([s["ctp_logloss"] for s in comparable if s["ctp_logloss"] is not None]),
            "baseline_logloss": _mean([s["baseline_logloss"] for s in comparable if s["baseline_logloss"] is not None]),
            "ctp_crps": _mean([s["ctp_crps"] for s in comparable if s["ctp_crps"] is not None]),
            "baseline_crps": _mean([s["baseline_crps"] for s in comparable if s["baseline_crps"] is not None]),
            "ctp_continuous_crps": _mean([s["ctp_continuous_crps"] for s in ctp_samples if s["ctp_continuous_crps"] is not None]),
            "d0_violation_total": sum(int(s["d0_violation_count"] or 0) for s in ctp_samples),
            "hard_source_locked_count": hard_locked_count,
            "hard_source_locked_consistency": (
                hard_locked_consistent / hard_locked_count if hard_locked_count else None
            ),
            "quantile_coverage_p05_p95": (
                _mean([1.0 if s["ctp_quantile_covered"] else 0.0 for s in ctp_samples if s["ctp_quantile_covered"] is not None])
            ),
            "baseline_target_churn_last_3h": baseline_churn,
            "ctp_target_churn_last_3h": ctp_churn,
            "target_churn_relative_change": (
                (ctp_churn - baseline_churn) / baseline_churn
                if baseline_churn is not None and baseline_churn > EPS and ctp_churn is not None
                else None
            ),
        },
        "gate_failures": [],
    }
    failures: List[str] = result["gate_failures"]
    if len(ctp_samples) < min_settled:
        failures.append("insufficient_ctp_settled_samples")
    if result["metrics"]["d0_violation_total"] != 0:
        failures.append("d0_violation_total_nonzero")
    if hard_locked_count and hard_locked_consistent != hard_locked_count:
        failures.append("hard_source_locked_inconsistent")
    if comparable:
        for metric in ("brier", "logloss", "crps"):
            ctp_val = result["metrics"].get(f"ctp_{metric}")
            baseline_val = result["metrics"].get(f"baseline_{metric}")
            if ctp_val is None or baseline_val is None:
                failures.append(f"{metric}_missing")
            elif float(ctp_val) > float(baseline_val) + 1e-12:
                failures.append(f"{metric}_worse_than_baseline")
        if ctp_churn is not None and baseline_churn is not None and ctp_churn > baseline_churn + 1e-12:
            failures.append("target_churn_worse_than_baseline")
    else:
        failures.append("no_comparable_baseline_samples")
    if failures:
        result["status"] = "fail"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="ctp_v1")
    parser.add_argument("--baseline", default="additive_pool")
    parser.add_argument("--min-settled", type=int, default=50)
    parser.add_argument("--metrics", default="crps,brier,logloss,d0,quantile_coverage,target_churn")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--fail-on-gate", action="store_true")
    args = parser.parse_args()

    result = run_backtest(Path(args.db), int(args.min_settled))
    result["engine"] = args.engine
    result["baseline"] = args.baseline
    result["requested_metrics"] = [part.strip() for part in str(args.metrics).split(",") if part.strip()]
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if args.fail_on_gate and result["status"] != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
