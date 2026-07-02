#!/usr/bin/env python3
"""Leak-aware walk-forward backtest for live physical-likelihood snapshots.

This script evaluates only probabilities that were already persisted on an
event_run row at that row's created_at. Labels may come from later settlement
rows or source-matched daily observations, but feature reconstruction and
snapshot-plan overlays are intentionally not used.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_physical_likelihood import (  # noqa: E402
    _hit,
    _improvement,
    _normalize_probs,
    _representative_c,
    _safe_json,
    _source_key,
    _to_float,
)
from bayesian_likelihood_runtime import _convert_temp  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


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


def _infer_tz(tz: Any, resolution_source: Any, lon: Any = None) -> str:
    raw = str(tz or "").strip()
    source = str(resolution_source or "").lower()
    if raw and raw.upper() != "UTC":
        return raw
    if "/history/daily/cn/" in source:
        return "Asia/Shanghai"
    if "/history/daily/us/co/" in source:
        return "America/Denver"
    if "/history/daily/us/fl/" in source or "/history/daily/us/ny/" in source:
        return "America/New_York"
    if "/history/daily/us/wa/" in source:
        return "America/Los_Angeles"
    lon_val = _to_float(lon)
    if lon_val is not None:
        offset_hours = int(round(float(lon_val) / 15.0))
        return f"Etc/GMT{(-offset_hours):+d}"
    return raw or "UTC"


def _zone(name: str) -> timezone:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)  # type: ignore[return-value]
        except Exception:
            pass
    match = re.fullmatch(r"Etc/GMT([+-]\d{1,2})", name)
    if match:
        return timezone(timedelta(hours=-int(match.group(1))))
    return timezone.utc


def _hours_to_peak_window(row: Mapping[str, Any], *, peak_hour: float) -> float | None:
    created_at = _parse_dt(row.get("created_at"))
    event_date = _event_date(row.get("event_date"))
    if created_at is None or not event_date:
        return None
    try:
        day = date.fromisoformat(event_date)
    except ValueError:
        return None
    tz_name = _infer_tz(row.get("tz"), row.get("resolution_source"), row.get("lon"))
    zone = _zone(tz_name)
    hour = int(math.floor(float(peak_hour)))
    minute = int(round((float(peak_hour) - hour) * 60.0))
    peak_local = datetime.combine(day, time(hour, minute), tzinfo=zone)
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
        slug = str(row["slug"] or "")
        event_date = _event_date(row["event_date"])
        actual = _to_float(row["settlement_actual"])
        if not slug or not event_date or actual is None:
            continue
        out[(slug, event_date)] = {
            "actual": float(actual),
            "unit": str(row["market_unit"] or "C"),
            "source": "formal_closed_event_runs",
            "source_created_at": str(row["created_at"] or ""),
        }
    return out


def _load_source_matched_actuals(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT event_date, resolution_source, market_unit, settlement_actual, created_at
            FROM event_daily_temps
            WHERE market_var = 'temperature_2m_max'
              AND event_date >= ?
              AND event_date <= ?
              AND settlement_actual IS NOT NULL
              AND COALESCE(resolution_source, '') != ''
            ORDER BY event_date, resolution_source, created_at
            """,
            (start_date, end_date),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        source = _source_key(row["resolution_source"])
        event_date = _event_date(row["event_date"])
        actual = _to_float(row["settlement_actual"])
        if not source or not event_date or actual is None:
            continue
        out[(source, event_date)] = {
            "actual": float(actual),
            "unit": str(row["market_unit"] or "C"),
            "source": "event_daily_temps_resolution_source_match",
            "source_created_at": str(row["created_at"] or ""),
        }
    return out


def _actual_for_row(
    row: Mapping[str, Any],
    formal_actuals: Mapping[tuple[str, str], Mapping[str, Any]],
    source_actuals: Mapping[tuple[str, str], Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    event_date = _event_date(row.get("event_date"))
    source_key = _source_key(row.get("resolution_source"))
    if source_key:
        actual = source_actuals.get((source_key, event_date))
        if actual is not None:
            return actual
    return formal_actuals.get((str(row.get("slug") or ""), event_date))


def _snapshot_event(
    row: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    baseline_key: str,
    posterior_key: str,
    peak_hour: float,
) -> dict[str, Any] | None:
    payload = _safe_json(row.get("payload_json"))
    if not isinstance(payload, Mapping):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    labels: list[str] = []
    baseline_raw: list[float | None] = []
    posterior_raw: list[float | None] = []
    physical_raw: list[float | None] = []
    physical_evidence_raw: list[float | None] = []
    prior_strength_raw: list[float | None] = []
    model_evidence_raw: list[float | None] = []
    historical_bucket_counts: list[float | None] = []
    component_group_counts: list[int] = []
    component_counts: list[int] = []
    hard_zero_flags: list[bool | None] = []
    bias_enabled_flags: list[bool] = []
    model_weight_multiplier_rows: list[dict[str, float]] = []
    hits: list[bool] = []
    reps: list[float] = []
    actual_value = _to_float(actual.get("actual"))
    actual_unit = str(actual.get("unit") or row.get("market_unit") or "C")
    if actual_value is None:
        return None
    physical_enabled = False
    physical_sources: set[str] = set()
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("outcome") or item.get("label") or "").strip()
        rep = _representative_c(label)
        if not label or rep is None:
            continue
        base = _to_float(item.get(baseline_key))
        if base is None and baseline_key != "q_raw_static":
            base = _to_float(item.get("q_raw_static"))
        if base is None:
            base = _to_float(item.get("q_model_round"))
        posterior = _to_float(item.get(posterior_key))
        if posterior is None:
            posterior = _to_float(item.get("q_bayes_posterior"))
        if posterior is None:
            posterior = _to_float(item.get("q_raw"))
        labels.append(label)
        baseline_raw.append(base)
        posterior_raw.append(posterior)
        physical_raw.append(_to_float(item.get("q_bayes_physical_likelihood_prob")))
        physical_evidence_raw.append(_to_float(item.get("q_bayes_physical_likelihood_evidence")))
        prior_strength_raw.append(_to_float(item.get("q_bayes_prior_strength")))
        model_evidence_raw.append(_to_float(item.get("q_bayes_model_evidence")))
        historical_bucket_counts.append(_to_float(item.get("q_bayes_historical_bucket_count")))
        components = item.get("q_bayes_physical_likelihood_components")
        component_counts.append(len(components) if isinstance(components, Mapping) else 0)
        component_groups = item.get("q_bayes_physical_likelihood_component_groups")
        component_group_counts.append(len(component_groups) if isinstance(component_groups, Mapping) else 0)
        hard_zero = item.get("q_bayes_physical_likelihood_hard_zero")
        hard_zero_flags.append(bool(hard_zero) if hard_zero is not None else None)
        bias_enabled_flags.append(bool(item.get("q_bayes_bias_correction_enabled")))
        raw_multipliers = item.get("q_bayes_model_weight_multipliers")
        if isinstance(raw_multipliers, str):
            raw_multipliers = _safe_json(raw_multipliers)
        if isinstance(raw_multipliers, Mapping):
            multipliers: dict[str, float] = {}
            for key, value in raw_multipliers.items():
                num = _to_float(value)
                if num is not None:
                    multipliers[str(key)] = float(num)
            model_weight_multiplier_rows.append(multipliers)
        else:
            model_weight_multiplier_rows.append({})
        hits.append(_hit(label, float(actual_value), actual_unit))
        reps.append(float(rep))
        if item.get("q_bayes_physical_likelihood_enabled") is True:
            physical_enabled = True
        source = str(item.get("q_bayes_physical_likelihood_source") or "")
        if source:
            physical_sources.add(source)
    if len(labels) < 2 or not any(hits):
        return None
    hours = _hours_to_peak_window(row, peak_hour=peak_hour)
    return {
        "id": row.get("id"),
        "slug": str(row.get("slug") or ""),
        "event_date": _event_date(row.get("event_date")),
        "created_at": str(row.get("created_at") or ""),
        "hours_to_peak_window": hours,
        "time_bucket": _time_bucket(hours),
        "rows": labels,
        "baseline_probs": _normalize_probs(baseline_raw),
        "posterior_probs": _normalize_probs(posterior_raw),
        "physical_likelihood_probs": _normalize_probs(physical_raw) if any(v is not None for v in physical_raw) else [],
        "physical_likelihood_raw": physical_raw,
        "physical_likelihood_evidence": physical_evidence_raw,
        "prior_strength": prior_strength_raw,
        "model_evidence": model_evidence_raw,
        "historical_bucket_counts": historical_bucket_counts,
        "physical_component_group_counts": component_group_counts,
        "physical_component_counts": component_counts,
        "physical_hard_zero_flags": hard_zero_flags,
        "bias_correction_enabled_flags": bias_enabled_flags,
        "model_weight_multipliers": model_weight_multiplier_rows,
        "hits": hits,
        "representatives_c": reps,
        "settlement_actual_c": _convert_temp(float(actual_value), actual_unit or "C", "C"),
        "settlement_actual": float(actual_value),
        "settlement_actual_unit": actual_unit,
        "actual_source": str(actual.get("source") or ""),
        "actual_source_created_at": str(actual.get("source_created_at") or ""),
        "physical_likelihood_enabled": physical_enabled,
        "physical_likelihood_sources": sorted(physical_sources),
}


def _finite_stats(values: Sequence[Any]) -> dict[str, Any]:
    nums: list[float] = []
    for value in values:
        num = _to_float(value)
        if num is not None:
            nums.append(float(num))
    nums.sort()
    if not nums:
        return {"n": 0}
    mid = len(nums) // 2
    median = nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2.0
    return {
        "n": len(nums),
        "min": nums[0],
        "max": nums[-1],
        "mean": sum(nums) / len(nums),
        "median": median,
    }


def _index_value(values: Any, idx: int) -> Any:
    if isinstance(values, list) and 0 <= idx < len(values):
        return values[idx]
    return None


def _flatten_multiplier_values(events: Sequence[Mapping[str, Any]]) -> list[float]:
    out: list[float] = []
    for event in events:
        rows = event.get("model_weight_multipliers")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for value in row.values():
                num = _to_float(value)
                if num is not None:
                    out.append(float(num))
    return out


def _probability_drag_diagnostics(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    analyzed: list[dict[str, Any]] = []
    hit_physical: list[float] = []
    pred_physical: list[float] = []
    hit_history_counts: list[float] = []
    pred_history_counts: list[float] = []
    hit_deltas: list[float] = []
    evidence_prior: list[float] = []
    evidence_model: list[float] = []
    evidence_physical: list[float] = []
    enabled_without_components = 0
    physical_extreme_against_hit = 0
    historical_prefers_pred = 0
    posterior_worse_count = 0

    for event in events:
        baseline = event.get("baseline_probs")
        posterior = event.get("posterior_probs")
        hits = event.get("hits")
        if not isinstance(baseline, list) or not isinstance(posterior, list) or not isinstance(hits, list) or not any(hits):
            continue
        hit_idx = hits.index(True)
        pred_idx = max(range(len(posterior)), key=lambda idx: float(posterior[idx]))
        labels = event.get("rows") if isinstance(event.get("rows"), list) else []
        physical_probs = event.get("physical_likelihood_probs")
        physical_hit = _to_float(_index_value(physical_probs, hit_idx))
        physical_pred = _to_float(_index_value(physical_probs, pred_idx))
        hist_counts = event.get("historical_bucket_counts")
        hist_hit = _to_float(_index_value(hist_counts, hit_idx))
        hist_pred = _to_float(_index_value(hist_counts, pred_idx))
        prior_strength = _to_float(_index_value(event.get("prior_strength"), hit_idx))
        model_evidence = _to_float(_index_value(event.get("model_evidence"), hit_idx))
        physical_evidence = _to_float(_index_value(event.get("physical_likelihood_evidence"), hit_idx))
        component_groups = event.get("physical_component_group_counts")
        hit_group_count = _to_float(_index_value(component_groups, hit_idx))
        pred_group_count = _to_float(_index_value(component_groups, pred_idx))
        hit_prob_delta = float(posterior[hit_idx]) - float(baseline[hit_idx])
        hit_deltas.append(hit_prob_delta)
        if physical_hit is not None:
            hit_physical.append(float(physical_hit))
        if physical_pred is not None:
            pred_physical.append(float(physical_pred))
        if hist_hit is not None:
            hit_history_counts.append(float(hist_hit))
        if hist_pred is not None:
            pred_history_counts.append(float(hist_pred))
        if prior_strength is not None:
            evidence_prior.append(float(prior_strength))
        if model_evidence is not None:
            evidence_model.append(float(model_evidence))
        if physical_evidence is not None:
            evidence_physical.append(float(physical_evidence))
        if event.get("physical_likelihood_enabled") and not any(
            int(v or 0) > 0 for v in (component_groups if isinstance(component_groups, list) else [])
        ):
            enabled_without_components += 1
        if physical_hit is not None and physical_pred is not None and physical_hit <= 1e-6 and physical_pred >= 0.5:
            physical_extreme_against_hit += 1
        if hist_hit is not None and hist_pred is not None and hist_pred > hist_hit:
            historical_prefers_pred += 1
        if float(posterior[hit_idx]) < float(baseline[hit_idx]):
            posterior_worse_count += 1
        baseline_loss = -math.log(max(1e-12, float(baseline[hit_idx])))
        posterior_loss = -math.log(max(1e-12, float(posterior[hit_idx])))
        analyzed.append(
            {
                "id": event.get("id"),
                "slug": event.get("slug"),
                "event_date": event.get("event_date"),
                "created_at": event.get("created_at"),
                "time_bucket": event.get("time_bucket"),
                "hours_to_peak_window": event.get("hours_to_peak_window"),
                "actual_c": event.get("settlement_actual_c"),
                "hit_label": _index_value(labels, hit_idx),
                "posterior_pred_label": _index_value(labels, pred_idx),
                "baseline_hit_prob": float(baseline[hit_idx]),
                "posterior_hit_prob": float(posterior[hit_idx]),
                "posterior_pred_prob": float(posterior[pred_idx]),
                "hit_prob_delta": hit_prob_delta,
                "logloss_delta": posterior_loss - baseline_loss,
                "physical_hit_prob": physical_hit,
                "physical_pred_prob": physical_pred,
                "historical_hit_count": hist_hit,
                "historical_pred_count": hist_pred,
                "prior_strength": prior_strength,
                "model_evidence": model_evidence,
                "physical_likelihood_evidence": physical_evidence,
                "bias_correction_enabled": any(event.get("bias_correction_enabled_flags") or []),
                "model_weight_multipliers": _index_value(event.get("model_weight_multipliers"), hit_idx),
                "physical_hit_component_group_count": hit_group_count,
                "physical_pred_component_group_count": pred_group_count,
            }
        )

    analyzed.sort(key=lambda row: float(row.get("logloss_delta") or 0.0), reverse=True)
    extreme_physical = sorted(
        (
            row
            for row in analyzed
            if _to_float(row.get("physical_hit_prob")) is not None
        ),
        key=lambda row: float(row.get("physical_hit_prob") or 0.0),
    )
    multiplier_values = _flatten_multiplier_values(events)
    return {
        "sample_count": len(analyzed),
        "posterior_hit_prob_worse_than_baseline_count": posterior_worse_count,
        "posterior_hit_prob_delta": _finite_stats(hit_deltas),
        "physical_likelihood": {
            "enabled_samples": sum(1 for event in events if event.get("physical_likelihood_enabled")),
            "enabled_without_persisted_component_groups": enabled_without_components,
            "hit_prob": _finite_stats(hit_physical),
            "posterior_pred_prob": _finite_stats(pred_physical),
            "hit_prob_lt_1e_6_count": sum(1 for value in hit_physical if value < 1e-6),
            "posterior_pred_prob_gt_0_5_count": sum(1 for value in pred_physical if value > 0.5),
            "extreme_against_hit_count": physical_extreme_against_hit,
        },
        "historical_prior": {
            "hit_bucket_count": _finite_stats(hit_history_counts),
            "posterior_pred_bucket_count": _finite_stats(pred_history_counts),
            "posterior_pred_count_gt_hit_count": historical_prefers_pred,
        },
        "bias_correction": {
            "enabled_samples": sum(1 for event in events if any(event.get("bias_correction_enabled_flags") or [])),
            "model_weight_multiplier_values": _finite_stats(multiplier_values),
        },
        "evidence_strength": {
            "prior_strength": _finite_stats(evidence_prior),
            "model_evidence": _finite_stats(evidence_model),
            "physical_likelihood_evidence": _finite_stats(evidence_physical),
        },
        "worst_samples_by_logloss_delta": analyzed[:10],
        "worst_samples_by_physical_hit_prob": extreme_physical[:10],
    }


def _metrics(events: Sequence[Mapping[str, Any]], prob_key: str) -> dict[str, Any]:
    brier = 0.0
    logloss = 0.0
    mae = 0.0
    correct = 0
    n = 0
    for event in events:
        probs = event.get(prob_key)
        hits = event.get("hits")
        reps = event.get("representatives_c")
        if not isinstance(probs, list) or not isinstance(hits, list) or not isinstance(reps, list) or not any(hits):
            continue
        hit_idx = hits.index(True)
        brier += sum((float(p) - (1.0 if idx == hit_idx else 0.0)) ** 2 for idx, p in enumerate(probs))
        logloss += -math.log(max(1e-12, float(probs[hit_idx])))
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
        "mae_c": mae / n,
        "direction_accuracy": correct / n,
    }


def _sample_rows(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            """
            SELECT id, slug, event_date, created_at, event_end_utc, market_var, market_unit,
                   settlement_actual, market_closed, payload_json, tz, lon, resolution_source
            FROM event_runs
            WHERE market_var = 'temperature_2m_max'
              AND event_date >= ?
              AND event_date <= ?
              AND payload_json IS NOT NULL
            ORDER BY datetime(created_at) ASC, id ASC
            """,
            (start_date, end_date),
        ).fetchall()
    )


def _thin_events(events: Sequence[Mapping[str, Any]], thin_minutes: float) -> list[dict[str, Any]]:
    if thin_minutes <= 0:
        return [dict(event) for event in events]
    out: list[dict[str, Any]] = []
    last_by_event: dict[tuple[str, str], datetime] = {}
    min_delta = timedelta(minutes=float(thin_minutes))
    for event in events:
        key = (str(event.get("slug") or ""), str(event.get("event_date") or ""))
        ts = _parse_dt(event.get("created_at"))
        if ts is None:
            continue
        prev = last_by_event.get(key)
        if prev is not None and ts - prev < min_delta:
            continue
        out.append(dict(event))
        last_by_event[key] = ts
    return out


def build_report(
    *,
    db_path: Path,
    days: int,
    include_current_day: bool,
    baseline_key: str,
    posterior_key: str,
    peak_hour: float,
    thin_minutes: float,
    min_samples: int,
    min_market_days: int,
    min_brier_or_logloss_improvement: float,
    min_mae_improvement: float,
    min_direction_lift_pp: float,
    allow_post_event_end: bool = False,
) -> dict[str, Any]:
    tz = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone(timedelta(hours=-5))
    today_et = datetime.now(timezone.utc).astimezone(tz).date()
    end_day = today_et if include_current_day else today_et - timedelta(days=1)
    start_date = (end_day - timedelta(days=max(1, int(days)) - 1)).isoformat()
    end_date = end_day.isoformat()
    diagnostics: dict[str, Any] = {
        "read_only": True,
        "writes_performed": False,
        "skipped_counts": {},
        "leakage_guards": {
            "uses_persisted_event_run_probabilities_only": True,
            "excludes_snapshot_plan_overlay": True,
            "excludes_rows_with_settlement_actual": True,
            "excludes_closed_snapshot_rows": True,
            "excludes_post_event_end_by_default": not allow_post_event_end,
        },
    }

    def skip(reason: str) -> None:
        skipped = diagnostics.setdefault("skipped_counts", {})
        if isinstance(skipped, dict):
            skipped[reason] = int(skipped.get(reason, 0)) + 1

    blockers: list[str] = []
    events: list[dict[str, Any]] = []
    if not db_path.exists():
        blockers.append("archive_db_missing")
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            formal_actuals = _load_formal_actuals(conn, start_date, end_date)
            source_actuals = _load_source_matched_actuals(conn, start_date, end_date)
            diagnostics["formal_actual_count"] = len(formal_actuals)
            diagnostics["source_matched_actual_count"] = len(source_actuals)
            for row in _sample_rows(conn, start_date, end_date):
                row_map = dict(row)
                created_at = _parse_dt(row_map.get("created_at"))
                if created_at is None:
                    skip("created_at_missing")
                    continue
                payload = _safe_json(row_map.get("payload_json"))
                if isinstance(payload, Mapping) and str(payload.get("physical_backtest_snapshot_source") or ""):
                    skip("snapshot_plan_or_reconstructed_payload")
                    continue
                if _to_float(row_map.get("settlement_actual")) is not None:
                    skip("snapshot_row_has_settlement_actual")
                    continue
                if int(row_map.get("market_closed") or 0) != 0:
                    skip("snapshot_row_market_closed")
                    continue
                event_end = _parse_dt(row_map.get("event_end_utc"))
                if not allow_post_event_end and event_end is not None and created_at >= event_end:
                    skip("snapshot_at_or_after_event_end")
                    continue
                actual = _actual_for_row(row_map, formal_actuals, source_actuals)
                if actual is None:
                    skip("settlement_label_missing")
                    continue
                event = _snapshot_event(
                    row_map,
                    actual,
                    baseline_key=baseline_key,
                    posterior_key=posterior_key,
                    peak_hour=peak_hour,
                )
                if event is None:
                    skip("snapshot_not_evaluable")
                    continue
                events.append(event)
        finally:
            conn.close()

    thinned_events = _thin_events(events, thin_minutes)
    baseline = _metrics(thinned_events, "baseline_probs")
    posterior = _metrics(thinned_events, "posterior_probs")
    improvements = {
        "brier": _improvement(baseline.get("brier"), posterior.get("brier"), True),
        "logloss": _improvement(baseline.get("logloss"), posterior.get("logloss"), True),
        "mae_c": _improvement(baseline.get("mae_c"), posterior.get("mae_c"), True),
        "direction_accuracy_lift_pp": (
            (float(posterior.get("direction_accuracy")) - float(baseline.get("direction_accuracy"))) * 100.0
            if posterior.get("direction_accuracy") is not None and baseline.get("direction_accuracy") is not None
            else None
        ),
    }
    market_days = sorted({(str(event.get("slug")), str(event.get("event_date"))) for event in thinned_events})
    stratified: dict[str, Any] = {}
    for bucket in ("gt_4h", "2_to_4h", "0_to_2h", "peak_or_after", "unknown"):
        bucket_events = [event for event in thinned_events if event.get("time_bucket") == bucket]
        if not bucket_events:
            continue
        base_m = _metrics(bucket_events, "baseline_probs")
        post_m = _metrics(bucket_events, "posterior_probs")
        stratified[bucket] = {
            "sample_count": len(bucket_events),
            "market_day_count": len({(str(event.get("slug")), str(event.get("event_date"))) for event in bucket_events}),
            "baseline": base_m,
            "posterior": post_m,
            "improvements": {
                "brier": _improvement(base_m.get("brier"), post_m.get("brier"), True),
                "logloss": _improvement(base_m.get("logloss"), post_m.get("logloss"), True),
                "mae_c": _improvement(base_m.get("mae_c"), post_m.get("mae_c"), True),
                "direction_accuracy_lift_pp": (
                    (float(post_m.get("direction_accuracy")) - float(base_m.get("direction_accuracy"))) * 100.0
                    if post_m.get("direction_accuracy") is not None and base_m.get("direction_accuracy") is not None
                    else None
                ),
            },
        }

    sample_count = int(posterior.get("n") or 0)
    market_day_count = len(market_days)
    brier_ok = improvements["brier"] is not None and float(improvements["brier"]) >= min_brier_or_logloss_improvement
    logloss_ok = improvements["logloss"] is not None and float(improvements["logloss"]) >= min_brier_or_logloss_improvement
    mae_ok = improvements["mae_c"] is not None and float(improvements["mae_c"]) >= min_mae_improvement
    direction_ok = (
        improvements["direction_accuracy_lift_pp"] is not None
        and float(improvements["direction_accuracy_lift_pp"]) >= min_direction_lift_pp
    )
    if sample_count < min_samples:
        blockers.append("walk_forward_sample_count_below_min")
    if market_day_count < min_market_days:
        blockers.append("walk_forward_market_day_count_below_min")
    if not (brier_ok or logloss_ok):
        blockers.append("brier_or_logloss_improvement_below_threshold")
    if not mae_ok:
        blockers.append("mae_improvement_below_threshold")
    if not direction_ok:
        blockers.append("direction_accuracy_lift_below_threshold")

    return {
        "ok": not blockers,
        "blockers": blockers,
        "db": str(db_path),
        "window": {"days": days, "start_date_et": start_date, "end_date_et": end_date},
        "method": {
            "type": "walk_forward_persisted_snapshots",
            "baseline_key": baseline_key,
            "posterior_key": posterior_key,
            "peak_hour_local": peak_hour,
            "thin_minutes": thin_minutes,
            "include_current_day": include_current_day,
            "allow_post_event_end": allow_post_event_end,
        },
        "thresholds": {
            "min_samples": min_samples,
            "min_market_days": min_market_days,
            "min_brier_or_logloss_improvement": min_brier_or_logloss_improvement,
            "min_mae_improvement": min_mae_improvement,
            "min_direction_lift_pp": min_direction_lift_pp,
        },
        "sample_count_raw": len(events),
        "sample_count": sample_count,
        "market_day_count": market_day_count,
        "physical_likelihood_enabled_samples": sum(1 for event in thinned_events if event.get("physical_likelihood_enabled")),
        "actual_source_counts": {
            source: sum(1 for event in thinned_events if event.get("actual_source") == source)
            for source in sorted({str(event.get("actual_source") or "") for event in thinned_events})
        },
        "baseline": baseline,
        "posterior": posterior,
        "improvements": improvements,
        "stratified_by_hours_to_peak": stratified,
        "probability_drag_diagnostics": _probability_drag_diagnostics(thinned_events),
        "diagnostics": diagnostics,
        "sample_events": [
            {
                "id": event.get("id"),
                "slug": event.get("slug"),
                "event_date": event.get("event_date"),
                "created_at": event.get("created_at"),
                "hours_to_peak_window": event.get("hours_to_peak_window"),
                "time_bucket": event.get("time_bucket"),
                "actual_source": event.get("actual_source"),
            }
            for event in thinned_events[:10]
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--include-current-day", action="store_true")
    ap.add_argument("--baseline-key", default="q_raw_static")
    ap.add_argument("--posterior-key", default="q_bayes_posterior")
    ap.add_argument("--peak-hour", type=float, default=16.0)
    ap.add_argument("--thin-minutes", type=float, default=30.0)
    ap.add_argument("--min-samples", type=int, default=100)
    ap.add_argument("--min-market-days", type=int, default=100)
    ap.add_argument("--min-brier-or-logloss-improvement", type=float, default=0.10)
    ap.add_argument("--min-mae-improvement", type=float, default=0.10)
    ap.add_argument("--min-direction-lift-pp", type=float, default=8.0)
    ap.add_argument("--allow-post-event-end", action="store_true")
    args = ap.parse_args()
    report = build_report(
        db_path=Path(args.db),
        days=args.days,
        include_current_day=args.include_current_day,
        baseline_key=args.baseline_key,
        posterior_key=args.posterior_key,
        peak_hour=args.peak_hour,
        thin_minutes=args.thin_minutes,
        min_samples=args.min_samples,
        min_market_days=args.min_market_days,
        min_brier_or_logloss_improvement=args.min_brier_or_logloss_improvement,
        min_mae_improvement=args.min_mae_improvement,
        min_direction_lift_pp=args.min_direction_lift_pp,
        allow_post_event_end=args.allow_post_event_end,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
