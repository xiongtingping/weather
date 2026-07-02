#!/usr/bin/env python3
"""Read-only backtest for physical likelihood gain on highest-temperature markets."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_SNAPSHOT_PLAN = ROOT / "data" / "reports" / "physical_backtest_snapshot_apply_plan_latest.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayesian_likelihood_runtime import (  # noqa: E402
    _bucket_bounds_c,
    _convert_temp,
    compute_weather_likelihood_vector,
    parse_temperature_bucket,
    precipitation_evidence_profile,
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _source_key(value: Any) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _normalize_probs(values: Sequence[float | None]) -> list[float]:
    cleaned = [max(0.0, float(v or 0.0)) if math.isfinite(float(v or 0.0)) else 0.0 for v in values]
    total = sum(cleaned)
    if total <= 0:
        return [1.0 / len(cleaned) for _ in cleaned] if cleaned else []
    return [v / total for v in cleaned]


FEATURE_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    "temperature_trajectory": (
        "observed_temp",
        "observed_temp_c",
        "observed_today_max_c",
        "temperature_rise_rate_c_per_hour",
        "temperature_momentum_1h_c_per_hour",
        "temperature_momentum_2h_c_per_hour",
        "temperature_momentum_3h_c_per_hour",
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_realization_state",
        "peak_estimator",
    ),
    "radiation": (
        "cti_observed",
        "cti",
        "solar_radiation_observed",
        "shortwave_radiation_observed",
        "solar_radiation",
    ),
    "cloud_shading": (
        "cloud_cover_low_pct",
        "cloud_cover_mid_pct",
        "cloud_cover_pct",
        "metar_effective_cloud_cover_pct",
        "nws_effective_cloud_cover_pct",
    ),
    "precipitation_evaporative_cooling": (
        "radar_precip_rate_mm_per_h",
        "qpe_precip_rate_mm_per_h",
        "observed_precip_rate_mm_per_h",
        "precipitation_rate_mm_per_h",
        "rain_rate_mm_per_h",
        "is_raining_observed",
        "is_raining",
    ),
    "airmass_mixing": (
        "wind_speed_mps",
        "wind_speed_10m_mps",
        "wind_direction_deg",
        "pressure_trend_hpa_3h",
        "pressure_hpa",
    ),
    "wet_bulb_constraint": (
        "dewpoint_c",
        "dew_point_c",
        "dewpoint_2m_c",
        "wet_bulb_c",
    ),
}


def _feature_has_value(features: Mapping[str, Any], key: str) -> bool:
    if key not in features:
        return False
    value = features.get(key)
    if key == "peak_estimator":
        return bool(value.get("enabled")) if isinstance(value, Mapping) else bool(value)
    if value is None:
        return False
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value) > 0
    return True


def _derived_feature_group_status(features: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for group, keys in FEATURE_GROUP_KEYS.items():
        if group == "precipitation_evaporative_cooling":
            precip_profile = precipitation_evidence_profile(features)
            present = []
            if precip_profile.get("available_strong"):
                source_field = str(precip_profile.get("source_field") or "")
                if source_field:
                    present.append(source_field)
            out[group] = {
                "available": bool(present),
                "present_keys": present,
                "na_reason": "" if present else str(precip_profile.get("na_reason") or "feature_group_missing"),
                "source_profile": dict(precip_profile),
                "evidence_state": str(precip_profile.get("evidence_state") or "missing"),
            }
            continue
        present = [key for key in keys if _feature_has_value(features, key)]
        out[group] = {
            "available": bool(present),
            "present_keys": present,
            "na_reason": "" if present else str(features.get(f"{group}_na_reason") or "feature_group_missing"),
        }
    return out


def _extract_saved_feature_quality(payload: Mapping[str, Any], rows: Sequence[Any]) -> str:
    quality = str(payload.get("weather_likelihood_feature_quality") or "").strip().lower()
    if quality in {"complete", "partial", "minimal", "missing"}:
        return quality
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        quality = str(item.get("weather_likelihood_feature_quality") or "").strip().lower()
        if quality in {"complete", "partial", "minimal", "missing"}:
            return quality
    return ""


def _has_saved_feature_snapshot(payload: Mapping[str, Any], rows: Sequence[Any]) -> bool:
    for key in ("weather_likelihood_features", "likelihood_features", "bayes_likelihood_features"):
        if isinstance(payload.get(key), Mapping):
            return True
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        for key in ("weather_likelihood_features", "likelihood_features", "bayes_likelihood_features"):
            if isinstance(item.get(key), Mapping):
                return True
    return False


def _extract_likelihood_features(payload: Mapping[str, Any], rows: Sequence[Any]) -> dict[str, Any]:
    for key in ("weather_likelihood_features", "likelihood_features", "bayes_likelihood_features"):
        val = payload.get(key)
        if isinstance(val, Mapping):
            return dict(val)
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        for key in ("weather_likelihood_features", "likelihood_features", "bayes_likelihood_features"):
            val = item.get(key)
            if isinstance(val, Mapping):
                return dict(val)
        q_meta = item.get("q_bayes_meta")
        if isinstance(q_meta, Mapping):
            row = q_meta.get("physical_likelihood_row")
            if isinstance(row, Mapping):
                peak = row.get("peak_estimator")
                if isinstance(peak, Mapping):
                    return {"peak_estimator": dict(peak)}
    features: dict[str, Any] = {}
    unit = str(payload.get("market_unit") or payload.get("unit") or "C").strip()
    if unit:
        features["unit"] = unit
        features["market_unit"] = unit
    values: list[float] = []
    model_members = payload.get("model_members")
    if isinstance(model_members, Mapping):
        for raw_vals in model_members.values():
            if isinstance(raw_vals, Sequence) and not isinstance(raw_vals, (str, bytes, bytearray)):
                for raw_val in raw_vals:
                    val = _to_float(raw_val)
                    if val is not None:
                        values.append(float(val))
    raw_members = payload.get("members")
    if isinstance(raw_members, Sequence) and not isinstance(raw_members, (str, bytes, bytearray)):
        for raw_val in raw_members:
            val = _to_float(raw_val)
            if val is not None:
                values.append(float(val))
    if values:
        values.sort()
        features["temperature_distribution"] = values
        features["model_peak_temps"] = values
        features["forecast_peak_temp"] = values[len(values) // 2]
    observed_nowcast = payload.get("observed_nowcast")
    if isinstance(observed_nowcast, Mapping):
        obs = _to_float(observed_nowcast.get("value"))
        if obs is not None:
            features["observed_temp"] = float(obs)
            features["observed_nowcast_share"] = _to_float(observed_nowcast.get("share")) or 1.0
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        peak_mu = _to_float(item.get("peak_estimator_mu_c"))
        peak_sigma = _to_float(item.get("peak_estimator_sigma_c"))
        if peak_mu is not None and peak_sigma is not None:
            features["peak_estimator"] = {
                "enabled": True,
                "source": str(item.get("peak_estimator_source") or "historical_payload_peak_estimator"),
                "mu_c": float(peak_mu),
                "sigma_c": float(peak_sigma),
                "kalman_gain": _to_float(item.get("peak_estimator_kalman_gain")),
                "combined_discount": _to_float(item.get("peak_estimator_combined_discount")),
            }
            break
    return features


def _feature_quality(features: Mapping[str, Any]) -> str:
    if not features:
        return "missing"
    groups = _derived_feature_group_status(features)
    count = sum(1 for status in groups.values() if status.get("available"))
    if count >= 5 and groups["precipitation_evaporative_cooling"]["available"]:
        return "complete"
    if count >= 3:
        return "partial"
    return "minimal"


def _hit(label: str, actual: float, market_unit: str) -> bool:
    bucket = parse_temperature_bucket(label)
    if bucket is None:
        return False
    actual_c = _convert_temp(float(actual), market_unit or bucket.unit or "C", "C")
    low_c, high_c, rep_c = _bucket_bounds_c(bucket)
    if bucket.kind == "le" and high_c is not None:
        return actual_c <= high_c + 0.5
    if bucket.kind == "ge" and low_c is not None:
        return actual_c >= low_c - 0.5
    if bucket.kind == "range" and low_c is not None and high_c is not None:
        return low_c - 0.5 <= actual_c <= high_c + 0.5
    return abs(actual_c - rep_c) <= 0.5


def _representative_c(label: str) -> float | None:
    bucket = parse_temperature_bucket(label)
    if bucket is None:
        return None
    return float(_bucket_bounds_c(bucket)[2])


def _metrics(events: list[dict[str, Any]], prob_key: str) -> dict[str, Any]:
    brier = 0.0
    logloss = 0.0
    mae = 0.0
    correct = 0
    n = 0
    for event in events:
        rows = event["rows"]
        probs = event[prob_key]
        hits = event["hits"]
        reps = event["representatives_c"]
        if not rows or not probs or not hits or not any(hits):
            continue
        hit_idx = hits.index(True)
        brier += sum((float(p) - (1.0 if idx == hit_idx else 0.0)) ** 2 for idx, p in enumerate(probs))
        logloss += -math.log(max(1e-12, float(probs[hit_idx])))
        pred_idx = max(range(len(probs)), key=lambda idx: probs[idx])
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


def _improvement(base: float | None, new: float | None, lower_is_better: bool) -> float | None:
    if base is None or new is None or not math.isfinite(base) or not math.isfinite(new) or abs(base) <= 1e-12:
        return None
    if lower_is_better:
        return (base - new) / abs(base)
    return (new - base) / abs(base)


def _event_from_payload(
    *,
    slug: str,
    event_date: str,
    market_unit: str,
    settlement_actual: Any,
    payload: Mapping[str, Any],
    event_source: str,
) -> dict[str, Any] | None:
    actual_val = _to_float(settlement_actual)
    payload_rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if actual_val is None or not isinstance(payload_rows, list) or not payload_rows:
        return None
    labels: list[str] = []
    base_probs_raw: list[float | None] = []
    posterior_probs_raw: list[float | None] = []
    reps: list[float] = []
    hits: list[bool] = []
    explain_groups: dict[str, int] = {}
    current_physical_report: dict[str, Any] = {}
    for item in payload_rows:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("outcome") or item.get("label") or "").strip()
        rep = _representative_c(label)
        if not label or rep is None:
            continue
        base = (
            _to_float(item.get("q_raw_static"))
            or _to_float(item.get("q_raw"))
            or _to_float(item.get("q_round"))
            or _to_float((item.get("q") or {}).get("q_raw") if isinstance(item.get("q"), Mapping) else None)
            or _to_float((item.get("q") or {}).get("q_round") if isinstance(item.get("q"), Mapping) else None)
        )
        posterior = (
            _to_float(item.get("q_bayes_posterior"))
            or _to_float((item.get("q") or {}).get("q_bayes_posterior") if isinstance(item.get("q"), Mapping) else None)
            or base
        )
        labels.append(label)
        base_probs_raw.append(base)
        posterior_probs_raw.append(posterior)
        reps.append(float(rep))
        hits.append(_hit(label, actual_val, str(market_unit or "")))
        meta = item.get("q_bayes_meta")
        physical_row = (meta or {}).get("physical_likelihood_row") if isinstance(meta, Mapping) else None
        groups = (physical_row or {}).get("component_groups") if isinstance(physical_row, Mapping) else None
        if isinstance(groups, Mapping):
            for group, entry in groups.items():
                if isinstance(entry, Mapping) and entry.get("components"):
                    explain_groups[str(group)] = explain_groups.get(str(group), 0) + 1
    if len(labels) < 2 or not any(hits):
        return None
    base_probs = _normalize_probs(base_probs_raw)
    historical_posterior_probs = _normalize_probs(posterior_probs_raw)
    current_probs = list(historical_posterior_probs)
    saved_feature_snapshot = _has_saved_feature_snapshot(payload, payload_rows)
    saved_quality = _extract_saved_feature_quality(payload, payload_rows)
    features = _extract_likelihood_features(payload, payload_rows)
    if (
        isinstance(payload, Mapping)
        and str(payload.get("physical_backtest_snapshot_source") or "") == "dry_run_physical_feature_reconstruction"
        and _to_float(features.get("observed_today_max_c")) is not None
    ):
        features.setdefault("observed_today_max_source", "dry_run_physical_feature_reconstruction")
    derived_quality = _feature_quality(features)
    quality = saved_quality or derived_quality
    feature_groups_raw = payload.get("weather_likelihood_feature_groups") if isinstance(payload, Mapping) else None
    if isinstance(feature_groups_raw, Mapping):
        feature_groups = {
            str(group): dict(status) if isinstance(status, Mapping) else {"available": bool(status)}
            for group, status in feature_groups_raw.items()
        }
    else:
        feature_groups = _derived_feature_group_status(features)
    missing_groups = [
        group
        for group, status in feature_groups.items()
        if isinstance(status, Mapping) and not bool(status.get("available"))
    ]
    if features:
        try:
            current_physical_report = compute_weather_likelihood_vector(
                labels,
                features,
                base_probs=base_probs,
            )
            adjusted = current_physical_report.get("adjusted_probs")
            likelihood = current_physical_report.get("likelihood_probs")
            if isinstance(adjusted, list) and len(adjusted) == len(labels):
                current_probs = _normalize_probs([_to_float(v) for v in adjusted])
            elif isinstance(likelihood, list) and len(likelihood) == len(labels):
                like_probs = _normalize_probs([_to_float(v) for v in likelihood])
                current_probs = _normalize_probs(
                    [base_probs[idx] * like_probs[idx] for idx in range(len(labels))]
                )
            for report_row in current_physical_report.get("rows") or []:
                groups = report_row.get("component_groups") if isinstance(report_row, Mapping) else None
                if isinstance(groups, Mapping):
                    for group, entry in groups.items():
                        if isinstance(entry, Mapping) and entry.get("components"):
                            explain_groups[str(group)] = explain_groups.get(str(group), 0) + 1
        except Exception as exc:
            current_physical_report = {"enabled": False, "source": f"recompute_error:{exc.__class__.__name__}"}
    market_unit_norm = str(market_unit or "C").replace("°", "").upper() or "C"
    actual_c = _convert_temp(float(actual_val), market_unit_norm, "C")
    return {
        "slug": slug,
        "event_date": event_date,
        "rows": labels,
        "baseline_probs": base_probs,
        "historical_posterior_probs": historical_posterior_probs,
        "posterior_probs": current_probs,
        "hits": hits,
        "representatives_c": reps,
        "settlement_actual_c": actual_c,
        "explain_groups": explain_groups,
        "features_recomputed": bool(features),
        "feature_quality": quality,
        "feature_quality_source": "payload" if saved_quality else "derived",
        "saved_feature_snapshot": saved_feature_snapshot,
        "missing_feature_groups": missing_groups,
        "current_physical_enabled": bool(current_physical_report.get("enabled")),
        "current_physical_source": str(current_physical_report.get("source") or ""),
        "event_source": event_source,
    }


def _extract_events(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[dict[str, Any]]:
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
        SELECT e.slug, e.event_date, e.market_unit, e.settlement_actual, e.payload_json
        FROM latest l
        JOIN event_runs e ON e.id = l.max_id
        ORDER BY e.event_date, e.slug
        """,
        (start_date, end_date),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for slug, event_date, market_unit, actual, payload_raw in rows:
        payload = _safe_json(payload_raw)
        if not isinstance(payload, Mapping):
            continue
        event = _event_from_payload(
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            market_unit=str(market_unit or ""),
            settlement_actual=actual,
            payload=payload,
            event_source="formal_db",
        )
        if event is not None:
            events.append(event)
    return events


def _extract_source_matched_daily_actuals(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[tuple[str, str], float]:
    try:
        rows = conn.execute(
            """
            SELECT event_date, resolution_source, settlement_actual
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
    out: dict[tuple[str, str], float] = {}
    for event_date, resolution_source, actual in rows:
        actual_val = _to_float(actual)
        source_key = _source_key(resolution_source)
        date_key = str(event_date or "").split("T", 1)[0]
        if actual_val is None or not source_key or not date_key:
            continue
        out[(source_key, date_key)] = float(actual_val)
    return out


def _extract_snapshot_plan_events(
    plan_path: Path,
    start_date: str,
    end_date: str,
    fallback_actual_c_by_event: Mapping[tuple[str, str], float] | None = None,
    source_matched_actual_c_by_source_event: Mapping[tuple[str, str], float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "enabled": True,
        "path": str(plan_path),
        "exists": plan_path.exists(),
        "read_only": True,
        "writes_performed": False,
        "candidate_rows": 0,
        "included_events": 0,
        "skipped_counts": {},
        "source_kind_counts": {},
        "fallback_actual_events": 0,
        "source_matched_actual_events": 0,
        "actual_feature_mismatch_events": 0,
        "plan_ok": None,
        "plan_read_only": None,
    }
    events: list[dict[str, Any]] = []
    if not plan_path.exists():
        return events, diagnostics
    try:
        plan_doc = _safe_json(plan_path.read_text())
    except Exception as exc:
        diagnostics["error"] = f"read_error:{exc.__class__.__name__}"
        return events, diagnostics
    if not isinstance(plan_doc, Mapping):
        diagnostics["error"] = "plan_json_not_object"
        return events, diagnostics
    candidate_projection = plan_doc.get("candidate_projection")
    apply_plan = (
        candidate_projection.get("physical_backtest_apply_plan")
        if isinstance(candidate_projection, Mapping)
        else None
    )
    if not isinstance(apply_plan, Mapping):
        diagnostics["error"] = "physical_backtest_apply_plan_missing"
        return events, diagnostics
    diagnostics["plan_ok"] = bool(apply_plan.get("ok"))
    diagnostics["plan_read_only"] = bool(apply_plan.get("read_only"))
    diagnostics["writes_performed"] = bool(apply_plan.get("writes_performed"))
    rows = apply_plan.get("rows")
    if not isinstance(rows, list):
        diagnostics["error"] = "plan_rows_missing"
        return events, diagnostics
    diagnostics["candidate_rows"] = len(rows)

    def skip(reason: str) -> None:
        skipped = diagnostics.setdefault("skipped_counts", {})
        if isinstance(skipped, dict):
            skipped[reason] = int(skipped.get(reason, 0)) + 1

    source_kind_counts: dict[str, int] = {}
    fallback_actuals = fallback_actual_c_by_event or {}
    source_matched_actuals = source_matched_actual_c_by_source_event or {}
    for row in rows:
        if not isinstance(row, Mapping):
            skip("row_not_object")
            continue
        slug = str(row.get("slug") or "")
        event_date = str(row.get("event_date") or "").split("T", 1)[0]
        if not event_date or event_date < start_date or event_date > end_date:
            skip("outside_window")
            continue
        if str(row.get("feature_quality") or "") != "complete":
            skip("feature_quality_not_complete")
            continue
        payload = row.get("snapshot_payload")
        if not isinstance(payload, Mapping):
            skip("snapshot_payload_missing")
            continue
        actual = _to_float(row.get("candidate_settlement_actual"))
        actual_unit = str(row.get("market_unit") or payload.get("market_unit") or "")
        actual_source = str(row.get("settlement_actual_source") or "")
        using_fallback_actual = False
        using_source_matched_actual = False
        if actual is None:
            source_actual = _to_float(
                source_matched_actuals.get((_source_key(row.get("resolution_source")), event_date))
            )
            if source_actual is not None:
                actual = float(source_actual)
                actual_unit = actual_unit or "C"
                actual_source = "event_daily_temps_resolution_source_match"
                using_source_matched_actual = True
                diagnostics["source_matched_actual_events"] = (
                    int(diagnostics.get("source_matched_actual_events") or 0) + 1
                )
            else:
                fallback_actual = _to_float(fallback_actuals.get((slug, event_date)))
                if fallback_actual is None:
                    skip("candidate_settlement_actual_missing")
                    continue
                actual = float(fallback_actual)
                actual_unit = "C"
                actual_source = "formal_db_existing_actual"
                using_fallback_actual = True
                diagnostics["fallback_actual_events"] = int(diagnostics.get("fallback_actual_events") or 0) + 1
        if actual is None:
            skip("candidate_settlement_actual_missing")
            continue
        if (using_fallback_actual or using_source_matched_actual) and isinstance(payload, Mapping):
            payload = dict(payload)
            features = dict(payload.get("weather_likelihood_features") or {})
            feature_actual = _to_float(features.get("observed_today_max_c"))
            feature_source = str(features.get("observed_today_max_source") or "")
            mismatch = (
                using_source_matched_actual
                and feature_source == "weather_com_history_api"
                and feature_actual is not None
                and abs(float(feature_actual) - float(actual)) > 0.25
            )
            if using_fallback_actual or mismatch:
                if feature_source == "weather_com_history_api":
                    features["observed_today_max_trusted"] = False
                    features.pop("temperature_observation_final_daily_max", None)
                payload["weather_likelihood_features"] = features
                payload["weather_likelihood_feature_quality"] = "partial"
                payload["weather_likelihood_feature_quality_reason"] = (
                    "source_matched_actual_temperature_feature_mismatch"
                    if mismatch
                    else "fallback_actual_source_mismatch"
                )
                if mismatch:
                    diagnostics["actual_feature_mismatch_events"] = (
                        int(diagnostics.get("actual_feature_mismatch_events") or 0) + 1
                    )
        event = _event_from_payload(
            slug=slug,
            event_date=event_date,
            market_unit=actual_unit,
            settlement_actual=actual,
            payload=payload,
            event_source="read_only_snapshot_plan_overlay",
        )
        if event is None:
            skip("event_not_evaluable")
            continue
        event["snapshot_plan_resolution_source_kind"] = str(row.get("resolution_source_kind") or "")
        event["snapshot_plan_settlement_actual_source"] = actual_source
        event["snapshot_plan_review_checksum"] = str(row.get("review_checksum") or "")
        source_kind = str(row.get("resolution_source_kind") or "missing")
        source_kind_counts[source_kind] = source_kind_counts.get(source_kind, 0) + 1
        events.append(event)
    diagnostics["included_events"] = len(events)
    diagnostics["source_kind_counts"] = dict(sorted(source_kind_counts.items()))
    return events, diagnostics


def _merge_events_with_overlay(
    formal_events: Sequence[Mapping[str, Any]],
    overlay_events: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for event in formal_events:
        key = (str(event.get("slug") or ""), str(event.get("event_date") or ""))
        if key[0] and key[1]:
            merged[key] = dict(event)
    replacements = 0
    additions = 0
    for event in overlay_events:
        key = (str(event.get("slug") or ""), str(event.get("event_date") or ""))
        if not key[0] or not key[1]:
            continue
        if key in merged:
            replacements += 1
        else:
            additions += 1
        merged[key] = dict(event)
    out = sorted(merged.values(), key=lambda item: (str(item.get("event_date") or ""), str(item.get("slug") or "")))
    return out, {
        "formal_db_events": len(formal_events),
        "overlay_events": len(overlay_events),
        "overlay_replacements": replacements,
        "overlay_additions": additions,
        "merged_events": len(out),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--compare-baseline", action="store_true")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--min-samples", type=int, default=100)
    ap.add_argument("--snapshot-plan", default=str(DEFAULT_SNAPSHOT_PLAN))
    ap.add_argument("--no-snapshot-plan-overlay", action="store_true")
    ap.add_argument("--include-current-day", action="store_true")
    args = ap.parse_args()

    tz = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone(timedelta(hours=-5))
    today_et = datetime.now(timezone.utc).astimezone(tz).date()
    end_day = today_et if args.include_current_day else today_et - timedelta(days=1)
    start_date = (end_day - timedelta(days=max(1, int(args.days)) - 1)).isoformat()
    end_date = end_day.isoformat()
    db = Path(args.db)
    blockers: list[str] = []
    formal_events: list[dict[str, Any]] = []
    source_matched_actuals: dict[tuple[str, str], float] = {}
    if not db.exists():
        blockers.append("archive_db_missing")
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
        try:
            formal_events = _extract_events(conn, start_date, end_date)
            source_matched_actuals = _extract_source_matched_daily_actuals(conn, start_date, end_date)
        finally:
            conn.close()
    overlay_events: list[dict[str, Any]] = []
    overlay_diagnostics: dict[str, Any] = {
        "enabled": False,
        "path": str(Path(args.snapshot_plan)),
        "read_only": True,
        "writes_performed": False,
    }
    if not args.no_snapshot_plan_overlay:
        fallback_actuals = {
            (str(event.get("slug") or ""), str(event.get("event_date") or "")): float(event["settlement_actual_c"])
            for event in formal_events
            if _to_float(event.get("settlement_actual_c")) is not None
        }
        overlay_events, overlay_diagnostics = _extract_snapshot_plan_events(
            Path(args.snapshot_plan),
            start_date,
            end_date,
            fallback_actual_c_by_event=fallback_actuals,
            source_matched_actual_c_by_source_event=source_matched_actuals,
        )
    events, merge_diagnostics = _merge_events_with_overlay(formal_events, overlay_events)

    baseline = _metrics(events, "baseline_probs")
    historical_posterior = _metrics(events, "historical_posterior_probs")
    posterior = _metrics(events, "posterior_probs")
    complete_events = [event for event in events if str(event.get("feature_quality") or "") == "complete"]
    complete_baseline = _metrics(complete_events, "baseline_probs")
    complete_historical_posterior = _metrics(complete_events, "historical_posterior_probs")
    complete_posterior = _metrics(complete_events, "posterior_probs")
    recomputed_events = sum(1 for event in events if event.get("features_recomputed"))
    physical_enabled_events = sum(1 for event in events if event.get("current_physical_enabled"))
    saved_feature_snapshot_events = sum(1 for event in events if event.get("saved_feature_snapshot"))
    feature_quality_counts: dict[str, int] = {}
    feature_quality_source_counts: dict[str, int] = {}
    event_source_counts: dict[str, int] = {}
    missing_group_counts: dict[str, int] = {}
    for event in events:
        quality = str(event.get("feature_quality") or "missing")
        feature_quality_counts[quality] = feature_quality_counts.get(quality, 0) + 1
        quality_source = str(event.get("feature_quality_source") or "unknown")
        feature_quality_source_counts[quality_source] = feature_quality_source_counts.get(quality_source, 0) + 1
        event_source = str(event.get("event_source") or "unknown")
        event_source_counts[event_source] = event_source_counts.get(event_source, 0) + 1
        for group in event.get("missing_feature_groups") or []:
            missing_group_counts[str(group)] = missing_group_counts.get(str(group), 0) + 1
    sample_n = int(posterior.get("n") or 0)
    complete_sample_n = int(complete_posterior.get("n") or 0)
    if sample_n < int(args.min_samples):
        blockers.append(f"sample_count_below_min:{sample_n}<{int(args.min_samples)}")
    if complete_sample_n < int(args.min_samples):
        blockers.append(
            f"complete_physical_feature_samples_below_min:{complete_sample_n}<{int(args.min_samples)}"
        )
    all_brier_improvement = _improvement(_to_float(baseline.get("brier")), _to_float(posterior.get("brier")), True)
    all_logloss_improvement = _improvement(_to_float(baseline.get("logloss")), _to_float(posterior.get("logloss")), True)
    all_mae_improvement = _improvement(_to_float(baseline.get("mae_c")), _to_float(posterior.get("mae_c")), True)
    all_accuracy_lift = (
        _to_float(posterior.get("direction_accuracy")) or 0.0
    ) - (
        _to_float(baseline.get("direction_accuracy")) or 0.0
    )
    brier_improvement = _improvement(_to_float(complete_baseline.get("brier")), _to_float(complete_posterior.get("brier")), True)
    logloss_improvement = _improvement(_to_float(complete_baseline.get("logloss")), _to_float(complete_posterior.get("logloss")), True)
    mae_improvement = _improvement(_to_float(complete_baseline.get("mae_c")), _to_float(complete_posterior.get("mae_c")), True)
    accuracy_lift = (
        _to_float(complete_posterior.get("direction_accuracy")) or 0.0
    ) - (
        _to_float(complete_baseline.get("direction_accuracy")) or 0.0
    )
    if complete_sample_n >= int(args.min_samples):
        gain_metric = brier_improvement if brier_improvement is not None else logloss_improvement
        if gain_metric is None or gain_metric < 0.10:
            blockers.append("brier_or_logloss_improvement_below_10pct")
        if mae_improvement is None or mae_improvement < 0.10:
            blockers.append("mae_improvement_below_10pct")
        if accuracy_lift < 0.08:
            blockers.append("direction_accuracy_lift_below_8pp")
    else:
        blockers.append("gain_metrics_not_evaluated:complete_samples_below_min")

    group_counts: dict[str, int] = {}
    if args.explain:
        for event in events:
            for group, count in (event.get("explain_groups") or {}).items():
                group_counts[group] = group_counts.get(group, 0) + int(count)

    report = {
        "ok": not blockers,
        "db": str(db),
        "window": {"start_date_et": start_date, "end_date_et": end_date, "days": int(args.days)},
        "current_day_policy": {
            "today_et": today_et.isoformat(),
            "include_current_day": bool(args.include_current_day),
            "excluded_current_day_by_default": not bool(args.include_current_day),
        },
        "evaluation_basis": (
            "complete_feature_events_with_read_only_snapshot_plan_overlay"
            if overlay_diagnostics.get("included_events")
            else "complete_feature_events"
        ),
        "read_only_snapshot_plan_overlay": overlay_diagnostics,
        "event_merge_diagnostics": merge_diagnostics,
        "sample_count": sample_n,
        "complete_feature_sample_count": complete_sample_n,
        "recomputed_feature_events": recomputed_events,
        "current_physical_enabled_events": physical_enabled_events,
        "saved_feature_snapshot_events": saved_feature_snapshot_events,
        "feature_quality_counts": feature_quality_counts,
        "feature_quality_source_counts": feature_quality_source_counts,
        "event_source_counts": event_source_counts,
        "coverage_diagnostics": {
            "candidate_event_days": len(events),
            "formal_db_event_days": len(formal_events),
            "overlay_event_days": len(overlay_events),
            "saved_feature_snapshot_events": saved_feature_snapshot_events,
            "complete_feature_events": complete_sample_n,
            "missing_group_counts": dict(sorted(missing_group_counts.items())),
            "event_dates": sorted({str(event.get("event_date") or "") for event in events if event.get("event_date")}),
            "sample_slugs": [str(event.get("slug") or "") for event in events[:10]],
        },
        "baseline": baseline,
        "historical_posterior": historical_posterior,
        "posterior": posterior,
        "all_event_improvements": {
            "brier": all_brier_improvement,
            "logloss": all_logloss_improvement,
            "mae_c": all_mae_improvement,
            "direction_accuracy_lift_pp": all_accuracy_lift * 100.0,
        },
        "complete_feature_baseline": complete_baseline,
        "complete_feature_historical_posterior": complete_historical_posterior,
        "complete_feature_posterior": complete_posterior,
        "improvements": {
            "brier": brier_improvement,
            "logloss": logloss_improvement,
            "mae_c": mae_improvement,
            "direction_accuracy_lift_pp": accuracy_lift * 100.0,
        },
        "explain_group_counts": group_counts,
        "blockers": blockers,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
