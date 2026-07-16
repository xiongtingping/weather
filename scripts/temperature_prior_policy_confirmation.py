#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_DB = ROOT / "data" / "archive.db"

from scripts.temperature_prior_continuity_audit import (  # noqa: E402
    _as_mapping,
    _beta,
    _hard_mask_map,
    _likelihood_map,
    _ordered_bucket_rows,
    _posterior_from_prior,
    _rank_scenario,
    _ranked,
    _safe_json,
    _to_float,
    _top_continuity,
)
from temperature_prior_runtime import build_temperature_prior_distribution  # noqa: E402
from posterior_ssot_projection_runtime import build_fact_projection_no_apply  # noqa: E402


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_int_list(raw: str) -> list[int]:
    out: list[int] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError:
            continue
        if value >= 1 and value not in out:
            out.append(value)
    return out or [3, 30, 60]


def _probability_rank(rows: Sequence[Mapping[str, Any]], bucket: str) -> dict[str, Any]:
    ranked = sorted(
        rows,
        key=lambda row: (-float(row.get("probability") or row.get("posterior") or 0.0), int(row.get("order_index") or 0)),
    )
    for index, row in enumerate(ranked, start=1):
        if str(row.get("bucket") or "") == str(bucket or ""):
            probability = _to_float(row.get("probability"))
            if probability is None:
                probability = _to_float(row.get("posterior"))
            return {
                "rank": index,
                "probability": probability,
                "probability_pct": probability * 100.0 if probability is not None else None,
            }
    return {"rank": None, "probability": None, "probability_pct": None}


def _target_bucket(payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    candidates = (
        payload.get("target_bucket"),
        payload.get("target"),
        _as_mapping(payload.get("posterior_ssot_fact")).get("target_bucket"),
        _as_mapping(payload.get("posterior_ssot_fact")).get("target"),
        (_ranked(rows, limit=1)[0].get("bucket") if rows else ""),
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _numeric_bucket_center(label: Any) -> float | None:
    text = str(label or "").strip()
    if "or below" in text.lower() or "or higher" in text.lower():
        return None
    number = ""
    for char in text:
        if char.isdigit() or char in ".-":
            number += char
        elif number:
            break
    return _to_float(number)


def _trajectory_assessment(payload: Mapping[str, Any], target_bucket: str) -> dict[str, Any]:
    trajectory = _as_mapping(payload.get("temperature_trajectory"))
    rolling = _as_mapping(trajectory.get("rolling_temperature_observation_summary"))
    state = str(trajectory.get("state") or "")
    gate_state = str(trajectory.get("gate_state") or "")
    gate_reason = str(trajectory.get("gate_reason") or "")
    effective_rate = _to_float(trajectory.get("effective_rate_c_per_hour"))
    adjusted_rate = _to_float(trajectory.get("adjusted_rate_c_per_hour"))
    current_temp = _to_float(rolling.get("current_temp_c"))
    rolling_max = _to_float(rolling.get("rolling_max_c"))
    today_max = _to_float(rolling.get("today_max_c")) or rolling_max
    target_center = _numeric_bucket_center(target_bucket)
    blockers: list[str] = []
    if target_center is not None and today_max is not None and target_center > today_max:
        if gate_state in {"not_warming", "platform_noise"} or state in {"platform", "cooling"}:
            blockers.append("trajectory_gate_not_warming_below_target")
        if (effective_rate is not None and effective_rate <= 0.0) or (adjusted_rate is not None and adjusted_rate <= 0.0):
            blockers.append("trajectory_rate_not_positive_below_target")
    return {
        "state": state,
        "gate_state": gate_state,
        "gate_reason": gate_reason,
        "current_temp_c": current_temp,
        "rolling_max_c": rolling_max,
        "today_max_c": today_max,
        "target_bucket": target_bucket,
        "target_center_c": target_center,
        "effective_rate_c_per_hour": effective_rate,
        "adjusted_rate_c_per_hour": adjusted_rate,
        "point_count": rolling.get("point_count"),
        "blockers": blockers,
        "assessment": "does_not_independently_support_target" if blockers else "no_trajectory_blocker_detected",
    }


def _runtime_prior_scenario(
    *,
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
    labels: Sequence[str],
    min_samples: int,
) -> dict[str, Any]:
    prior_meta = _as_mapping(payload.get("prior_meta"))
    prior_output = build_temperature_prior_distribution(
        buckets=list(labels),
        event_date=str(payload.get("event_date") or ""),
        as_of=str(payload.get("fact_as_of") or ""),
        conn=conn,
        city=str(prior_meta.get("city") or payload.get("city") or payload.get("city_key") or ""),
        station_id=str(prior_meta.get("station_id") or payload.get("station_id") or "ZUUU"),
        market_var=str(prior_meta.get("market_var") or payload.get("market_var") or "temperature_2m_max"),
        market_unit=str(prior_meta.get("market_unit") or payload.get("market_unit") or "C"),
        rounding_rule=str(prior_meta.get("rounding_rule") or payload.get("rounding_rule") or "rounded_degrees"),
        history_days=int(_to_float(prior_meta.get("history_days")) or 730),
        date_window_days=int(_to_float(prior_meta.get("date_window_days")) or 15),
        min_samples=max(1, int(min_samples)),
        smoothing=float(_to_float(prior_meta.get("smoothing")) if _to_float(prior_meta.get("smoothing")) is not None else 0.5),
        weather_type=str(prior_meta.get("weather_type") or payload.get("weather_type") or ""),
        terrain_class=str(prior_meta.get("terrain_class") or payload.get("terrain_class") or ""),
        site_class=str(prior_meta.get("site_class") or payload.get("site_class") or ""),
        urbanization_class=str(prior_meta.get("urbanization_class") or payload.get("urbanization_class") or ""),
        site_temperature_bias_c=prior_meta.get("site_temperature_bias_c", payload.get("site_temperature_bias_c", 0.0)),
        terrain_temperature_bias_c=prior_meta.get("terrain_temperature_bias_c", payload.get("terrain_temperature_bias_c", 0.0)),
        urban_heat_island_c=prior_meta.get("urban_heat_island_c", payload.get("urban_heat_island_c", 0.0)),
    )
    return prior_output


def _posterior_scenario(
    *,
    labels: Sequence[str],
    prior: Mapping[str, float],
    payload: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    target_bucket: str,
    top_n: int,
) -> dict[str, Any]:
    forecast_weak = _likelihood_map(
        payload,
        rows,
        layer="forecast_weak_likelihood",
        row_key="forecast_weak_likelihood",
    )
    metar_strong = _likelihood_map(
        payload,
        rows,
        layer="metar_strong_likelihood",
        row_key="metar_strong_likelihood",
    )
    hard_mask = _hard_mask_map(rows)
    beta_weak = _beta(payload, "forecast_weak_likelihood", 1.0)
    beta_strong = _beta(payload, "metar_strong_likelihood", 1.0)
    if not prior or not forecast_weak or not metar_strong:
        return {
            "state": "not_available",
            "reason": "prior_or_likelihood_layers_missing",
            "top_buckets": [],
            "target_rank": {"rank": None, "probability": None, "probability_pct": None},
        }
    scenario_rows = _posterior_from_prior(
        labels=labels,
        prior=prior,
        forecast_weak=forecast_weak,
        metar_strong=metar_strong,
        hard_mask=hard_mask,
        beta_weak=beta_weak,
        beta_strong=beta_strong,
    )
    return {
        "state": "available",
        "formula": "prior * forecast_weak^beta_weak * metar_strong^beta_strong * hard_mask",
        "beta_weak": beta_weak,
        "beta_strong": beta_strong,
        "top_buckets": _rank_scenario(scenario_rows, top_n=top_n),
        "target_rank": _probability_rank(scenario_rows, target_bucket),
    }


def _projection_summary(
    *,
    db_path: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    projection = build_fact_projection_no_apply(
        slug=str(payload.get("slug") or ""),
        event_date=str(payload.get("event_date") or ""),
        run_date=str(payload.get("run_date") or ""),
        fact_as_of=str(payload.get("fact_as_of") or ""),
        payload=payload,
        current_fact_id=int(_to_float(payload.get("fact_id") or payload.get("id")) or 0),
        current_fact_as_of=str(payload.get("fact_as_of") or ""),
        db_path=db_path,
        write_reason="temperature_prior_policy_confirmation_no_apply",
        enable_apply_contract=False,
        write_latest=False,
    )
    projected_payload = _as_mapping(projection.get("projected_payload"))
    posterior = _as_mapping(projected_payload.get("posterior_ssot_fact"))
    prior_meta = _as_mapping(projected_payload.get("prior_meta"))
    rows = _ordered_bucket_rows(projected_payload)
    ranked_rows = [
        {
            "bucket": row.get("bucket"),
            "probability": row.get("posterior"),
            "probability_pct": float(row.get("posterior") or 0.0) * 100.0,
            "prior": row.get("prior"),
            "order_index": row.get("order_index"),
        }
        for row in rows
    ]
    return {
        "state": "available" if projected_payload else "not_available",
        "diagnostic_only": True,
        "read_only": True,
        "writes_archive_db": False,
        "projection_ok": bool(projection.get("ok")),
        "projection_mode": projection.get("projection_mode"),
        "prior_source": _as_mapping(projection.get("context")).get("prior_source"),
        "projected_prior_source": _as_mapping(projected_payload.get("posterior_projection_context")).get("prior_source"),
        "prior_meta": {
            "sample_count": prior_meta.get("sample_count"),
            "raw_candidate_count": prior_meta.get("raw_candidate_count"),
            "effective_sample_count": prior_meta.get("effective_sample_count"),
            "date_window_sample_count": prior_meta.get("date_window_sample_count"),
            "date_window_weight": prior_meta.get("date_window_weight"),
            "min_samples": prior_meta.get("min_samples"),
        },
        "target_bucket": posterior.get("target_bucket"),
        "target_probability": posterior.get("target_probability"),
        "target_probability_pct": (
            float(posterior.get("target_probability")) * 100.0
            if _to_float(posterior.get("target_probability")) is not None
            else None
        ),
        "top_buckets": _rank_scenario(ranked_rows, top_n=8),
        "issues": list(projection.get("issues") or []),
    }


def review_payload(
    payload: Mapping[str, Any],
    *,
    conn: sqlite3.Connection,
    db_path: Path | None = None,
    min_samples_values: Sequence[int],
    top_n: int = 6,
) -> dict[str, Any]:
    rows = _ordered_bucket_rows(payload)
    labels = [str(row.get("bucket") or "") for row in rows]
    target_bucket = _target_bucket(payload, rows)
    stored_rows = [
        {
            "bucket": row.get("bucket"),
            "probability": row.get("posterior"),
            "probability_pct": float(row.get("posterior") or 0.0) * 100.0,
            "prior": row.get("prior"),
            "order_index": row.get("order_index"),
        }
        for row in rows
    ]
    stored_scenario = {
        "state": "available" if rows else "not_available",
        "top_buckets": _rank_scenario(stored_rows, top_n=top_n),
        "target_rank": _probability_rank(stored_rows, target_bucket),
        "top_continuity": _top_continuity(rows, top_n=3),
    }
    prior_meta = _as_mapping(payload.get("prior_meta"))
    scenarios: dict[str, Any] = {
        "stored_ssot_fact": {
            "diagnostic_only": False,
            "prior_meta": {
                "sample_count": prior_meta.get("sample_count"),
                "raw_candidate_count": prior_meta.get("raw_candidate_count"),
                "effective_sample_count": prior_meta.get("effective_sample_count"),
                "date_window_sample_count": prior_meta.get("date_window_sample_count"),
                "date_window_weight": prior_meta.get("date_window_weight"),
                "min_samples": prior_meta.get("min_samples"),
            },
            "posterior": stored_scenario,
        }
    }
    blockers: list[str] = []
    if stored_scenario["top_continuity"].get("state") == "noncontiguous":
        blockers.append("stored_posterior_top3_noncontiguous")
    trajectory = _trajectory_assessment(payload, target_bucket)
    if trajectory["blockers"]:
        blockers.append("stored_target_not_supported_by_current_trajectory")

    stored_top = str((stored_scenario["top_buckets"] or [{}])[0].get("bucket") or "")
    for min_samples in min_samples_values:
        prior_output = _runtime_prior_scenario(
            conn=conn,
            payload=payload,
            labels=labels,
            min_samples=max(1, int(min_samples)),
        )
        runtime_prior = {
            str(label): float(value)
            for label, value in _as_mapping(prior_output.get("prior_distribution")).items()
            if _to_float(value) is not None
        }
        posterior = _posterior_scenario(
            labels=labels,
            prior=runtime_prior,
            payload=payload,
            rows=rows,
            target_bucket=target_bucket,
            top_n=top_n,
        )
        scenario_name = f"runtime_prior_min_samples_{max(1, int(min_samples))}"
        runtime_top = str((posterior.get("top_buckets") or [{}])[0].get("bucket") or "")
        prior_output_meta = _as_mapping(prior_output.get("prior_meta"))
        scenarios[scenario_name] = {
            "diagnostic_only": True,
            "uses_current_temperature_prior_runtime": True,
            "prior_state": prior_output.get("state"),
            "prior_meta": {
                "sample_count": prior_output_meta.get("sample_count"),
                "raw_candidate_count": prior_output_meta.get("raw_candidate_count"),
                "effective_sample_count": prior_output_meta.get("effective_sample_count"),
                "date_window_sample_count": prior_output_meta.get("date_window_sample_count"),
                "date_window_matched_sample_count": prior_output_meta.get("date_window_matched_sample_count"),
                "broad_matched_sample_count": prior_output_meta.get("broad_matched_sample_count"),
                "date_window_weight": prior_output_meta.get("date_window_weight"),
                "min_samples": prior_output_meta.get("min_samples"),
                "bucket_counts": prior_output_meta.get("bucket_counts"),
                "broad_bucket_counts": prior_output_meta.get("broad_bucket_counts"),
                "sample_rows": prior_output_meta.get("sample_rows"),
            },
            "posterior": posterior,
            "stored_top_bucket_changed": bool(stored_top and runtime_top and runtime_top != stored_top),
        }
        if runtime_top and stored_top and runtime_top != stored_top:
            blockers.append(f"{scenario_name}_top_bucket_differs_from_stored_ssot")
    return {
        "schema": "temperature_prior_policy_confirmation_v1",
        "ok": not blockers,
        "read_only": True,
        "production_entry_enabled": False,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "reads_market_prices": False,
            "reads_forecast_sources": False,
            "reads_current_observations": False,
        },
        "identity": {
            "fact_id": payload.get("fact_id") or payload.get("id"),
            "slug": payload.get("slug"),
            "event_date": payload.get("event_date"),
            "fact_as_of": payload.get("fact_as_of"),
            "fact_snapshot_id": payload.get("fact_snapshot_id"),
            "posterior_run_id": payload.get("posterior_run_id"),
        },
        "target_bucket": target_bucket,
        "blockers": sorted(set(blockers)),
        "trajectory_assessment": trajectory,
        "scenarios": scenarios,
        "no_apply_projection": (
            _projection_summary(db_path=db_path, payload=payload)
            if db_path is not None
            else {"state": "not_available", "reason": "db_path_missing"}
        ),
        "next_boundary": (
            "confirm_prior_policy_before_materialization_or_parameter_change"
            if blockers
            else "prior_policy_confirmation_clear_continue_ssot_review"
        ),
    }


def _load_payload(conn: sqlite3.Connection, *, fact_id: int | None, slug: str, event_date: str) -> dict[str, Any]:
    params: list[Any] = []
    if fact_id is not None:
        row = conn.execute("SELECT * FROM event_fact_history WHERE id = ? LIMIT 1", (int(fact_id),)).fetchone()
    else:
        where = ["slug LIKE '%chengdu%'"]
        if slug:
            where.append("slug = ?")
            params.append(slug)
        if event_date:
            where.append("event_date = ?")
            params.append(event_date)
        row = conn.execute(
            f"""
            SELECT *
            FROM event_fact_history
            WHERE {' AND '.join(where)}
            ORDER BY event_date DESC, fact_as_of DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
    if row is None:
        return {}
    payload = _safe_json(row["fact_json"])
    payload.setdefault("fact_id", int(row["id"] or 0))
    payload.setdefault("slug", str(row["slug"] or ""))
    payload.setdefault("event_date", str(row["event_date"] or ""))
    payload.setdefault("run_date", str(row["run_date"] or ""))
    payload.setdefault("fact_as_of", str(row["fact_as_of"] or ""))
    payload.setdefault("fact_snapshot_id", str(row["fact_snapshot_id"] or ""))
    payload.setdefault("posterior_run_id", str(row["posterior_run_id"] or ""))
    return payload


def build_report(
    *,
    db_path: Path = DEFAULT_DB,
    fact_id: int | None = None,
    slug: str = "",
    event_date: str = "",
    min_samples_values: Sequence[int] = (3, 30, 60),
    top_n: int = 6,
) -> dict[str, Any]:
    conn = _connect_ro(db_path)
    try:
        payload = _load_payload(conn, fact_id=fact_id, slug=slug, event_date=event_date)
        if not payload:
            return {
                "schema": "temperature_prior_policy_confirmation_report_v1",
                "ok": False,
                "read_only": True,
                "db": str(db_path),
                "blockers": ["fact_payload_not_found"],
                "items": [],
            }
        item = review_payload(
            payload,
            conn=conn,
            db_path=db_path,
            min_samples_values=min_samples_values,
            top_n=top_n,
        )
    finally:
        conn.close()
    return {
        "schema": "temperature_prior_policy_confirmation_report_v1",
        "ok": bool(item.get("ok")),
        "read_only": True,
        "db": str(db_path),
        "scope": {
            "fact_id": fact_id,
            "slug": slug,
            "event_date": event_date,
            "min_samples_values": list(min_samples_values),
            "top_n": int(top_n),
        },
        "blockers": item.get("blockers") or [],
        "items": [item],
        "next_boundary": item.get("next_boundary"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only confirmation packet for temperature prior policy effects on posterior top buckets."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--fact-id", type=int)
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--min-samples", default="3,30,60")
    parser.add_argument("--top-n", type=int, default=6)
    args = parser.parse_args()
    report = build_report(
        db_path=Path(args.db),
        fact_id=args.fact_id,
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        min_samples_values=_parse_int_list(str(args.min_samples or "")),
        top_n=max(1, int(args.top_n)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
