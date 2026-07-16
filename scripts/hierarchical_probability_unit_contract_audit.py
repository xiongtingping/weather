#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast_weak_likelihood_runtime import (
    build_forecast_weak_likelihood,
    build_forecast_weak_likelihood_contract,
)
from metar_strong_likelihood_runtime import (
    build_metar_strong_likelihood,
    build_metar_strong_likelihood_contract,
)
from sequential_remaining_warming_bayes_runtime import build_sequential_remaining_warming_posterior
from source_observation_runtime import SOURCE_CLASSES
from temperature_prior_runtime import build_temperature_prior_distribution

SCHEMA_VERSION = "hierarchical_probability_unit_contract_audit_v1"
STAGE_REPORT_SCHEMA = "hierarchical_probability_unit_stage_contract_v1"
BUCKETS = ["29", "30", "31", "32", "33"]


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _round_float(value: float | None, ndigits: int = 12) -> float | None:
    if value is None:
        return None
    return round(float(value), ndigits)


def _sum_mapping(values: Mapping[str, Any]) -> float:
    total = 0.0
    for value in values.values():
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            total += max(0.0, parsed)
    return total


def _stage_report(
    *,
    stage: str,
    checks: Mapping[str, bool],
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    failed = [name for name, ok in checks.items() if not ok]
    blockers = [
        {
            "blocker": f"{stage}_unit_contract_{name}_failed",
            "stage": stage,
            "check": name,
        }
        for name in failed
    ]
    return {
        "schema": STAGE_REPORT_SCHEMA,
        "stage": stage,
        "ok": not blockers,
        "read_only": True,
        "coverage_mode": "runtime_unit_contract_fixtures_no_db_writes",
        "coverage": {
            "checked_fact_count": len(checks),
            "checked_fixture_count": len(checks),
            "ok_fixture_count": len(checks) - len(failed),
            "issue_fixture_count": len(failed),
        },
        "checks": dict(checks),
        "issue_counts": {name: 1 for name in failed},
        "blockers": blockers,
        "warnings": [],
        "guards": _guards(),
        "details": dict(details or {}),
        "next_boundary": (
            f"{stage}_unit_contract_ok"
            if not blockers
            else f"fix_{stage}_unit_contract_before_release_gate"
        ),
    }


def _build_prior_fixture() -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_runs(
            id INTEGER PRIMARY KEY,
            event_date TEXT,
            settlement_actual REAL,
            created_at TEXT,
            station_id TEXT,
            city TEXT,
            market_var TEXT,
            market_unit TEXT,
            rounding_rule TEXT,
            weather_type TEXT,
            terrain_class TEXT,
            site_class TEXT,
            urbanization_class TEXT
        )
        """
    )
    rows = [
        ("2026-06-20", 29.0, "low_cloud_wet_layer"),
        ("2026-06-21", 30.0, "subtropical_high_open"),
        ("2026-06-22", 30.0, "subtropical_high_open"),
        ("2026-06-23", 31.0, "subtropical_high_open"),
        ("2026-06-24", 32.0, "subtropical_high_open"),
        ("2026-07-03", 35.0, "subtropical_high_open"),
        ("2026-07-04", 35.0, "subtropical_high_open"),
    ]
    for event_date, actual, weather_type in rows:
        conn.execute(
            """
            INSERT INTO event_runs(
                event_date, settlement_actual, created_at, station_id, city,
                market_var, market_unit, rounding_rule, weather_type,
                terrain_class, site_class, urbanization_class
            ) VALUES (
                ?, ?, '2026-07-02T00:00:00+00:00', 'ZUUU', 'Chengdu',
                'temperature_2m_max', 'C', 'rounded_degrees', ?,
                'basin', 'airport', 'suburban'
            )
            """,
            (event_date, actual, weather_type),
        )
    try:
        prior = build_temperature_prior_distribution(
            buckets=BUCKETS,
            event_date="2026-07-03",
            as_of="2026-07-03T00:00:00+00:00",
            conn=conn,
            city="Chengdu",
            station_id="ZUUU",
            market_var="temperature_2m_max",
            market_unit="C",
            weather_type="subtropical_high_open",
            terrain_class="basin",
            site_class="airport",
            urbanization_class="suburban",
            date_window_days=20,
            min_samples=3,
            smoothing=0.0,
        )
    finally:
        conn.close()

    distribution = prior.get("prior_distribution") if isinstance(prior.get("prior_distribution"), Mapping) else {}
    meta = prior.get("prior_meta") if isinstance(prior.get("prior_meta"), Mapping) else {}
    boundary = str(meta.get("boundary") or "")
    sample_rows = meta.get("sample_rows") if isinstance(meta.get("sample_rows"), list) else []
    checks = {
        "prior_runtime_ok": prior.get("ok") is True,
        "prior_distribution_normalized": abs(_sum_mapping(distribution) - 1.0) <= 1e-9,
        "prior_boundary_excludes_forecast_metar_market": (
            "no_forecast" in boundary and "no_metar" in boundary and "no_market" in boundary
        ),
        "prior_guards_exclude_today_sources": (
            prior.get("guards", {}).get("reads_forecast_sources") is False
            and prior.get("guards", {}).get("reads_current_observations") is False
            and prior.get("guards", {}).get("reads_market_prices") is False
        ),
        "prior_excludes_target_and_future_dates": bool(sample_rows)
        and all(
            str(row.get("event_date") or "") < "2026-07-03"
            for row in sample_rows
            if isinstance(row, Mapping)
        ),
        "weather_type_conditioning_is_prior_layer": any(
            isinstance(row, Mapping)
            and row.get("condition") == "weather_type"
            and row.get("state") == "applied"
            for row in meta.get("conditioning_trace", [])
            if isinstance(row, Mapping)
        ),
    }
    return _stage_report(
        stage="prior",
        checks=checks,
        details={
            "prior_state": prior.get("state"),
            "prior_distribution_sum": _round_float(_sum_mapping(distribution)),
            "prior_na_reason": prior.get("prior_na_reason"),
            "sample_count": meta.get("sample_count"),
            "conditioning_trace_count": len(meta.get("conditioning_trace", []))
            if isinstance(meta.get("conditioning_trace"), list)
            else 0,
        },
    )


def _build_likelihood_fixture() -> dict[str, Any]:
    forecast = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        phase="D1_late",
        source_observations=[
            {
                "source_event_id": "srcevt_model_ecmwf",
                "source_id": "ecmwf_model_peak",
                "source_class": SOURCE_CLASSES["forecast_weak"],
                "forecast_peak_c": 33.0,
                "sigma_c": 0.35,
                "confidence": 1.0,
            },
            {
                "source_event_id": "srcevt_model_gfs",
                "source_id": "gfs_model_peak",
                "source_class": SOURCE_CLASSES["forecast_weak"],
                "forecast_peak_c": 33.0,
                "sigma_c": 0.35,
                "confidence": 1.0,
            },
            {
                "source_event_id": "srcevt_taf",
                "source_id": "zuuu_taf_forecast_peak",
                "source_class": SOURCE_CLASSES["forecast_weak"],
                "forecast_peak_c": 31.0,
            },
            {
                "source_event_id": "srcevt_twc_current",
                "source_id": "twc_current_icao_metric",
                "source_class": SOURCE_CLASSES["diagnostic_current"],
                "temperature_c": 34.0,
                "observed_today_max_c": 34.0,
                "forecast_peak_c": 34.0,
            },
            {
                "source_event_id": "srcevt_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "source_class": SOURCE_CLASSES["hard_observation"],
                "temperature_c": 34.0,
                "observed_today_max_c": 34.0,
                "forecast_peak_c": 34.0,
            },
            {
                "source_event_id": "srcevt_market",
                "source_id": "market_realtime_orderbook",
                "source_class": SOURCE_CLASSES["market"],
                "forecast_peak_c": 34.0,
            },
            {
                "source_event_id": "srcevt_forecast_with_hard_fields",
                "source_id": "wu_forecast_high",
                "source_class": SOURCE_CLASSES["forecast_weak"],
                "forecast_peak_c": 32.0,
                "temperature_c": 35.0,
                "observed_today_max_c": 35.0,
            },
        ],
    )
    forecast_contract = build_forecast_weak_likelihood_contract(forecast)
    source_trace = forecast.get("source_trace") if isinstance(forecast.get("source_trace"), list) else []
    included = [row for row in source_trace if isinstance(row, Mapping) and row.get("included")]
    excluded_non_forecast = [
        row
        for row in source_trace
        if isinstance(row, Mapping)
        and not row.get("included")
        and row.get("source_class")
        in {
            SOURCE_CLASSES["diagnostic_current"],
            SOURCE_CLASSES["hard_observation"],
            SOURCE_CLASSES["market"],
            SOURCE_CLASSES["settlement"],
        }
    ]
    hard_field_forecast = [
        row
        for row in included
        if row.get("source_event_id") == "srcevt_forecast_with_hard_fields"
    ]
    model_groups = [
        row
        for row in forecast.get("source_group_trace", [])
        if isinstance(row, Mapping) and row.get("group") == "model"
    ]

    confirmed = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW011 SCT020",
            "dewpoint_trend_c_per_hour": -1.0,
            "temperature_momentum_1h_c_per_hour": 1.4,
            "temperature_momentum_45m_c_per_hour": 1.2,
            "temperature_momentum_30m_c_per_hour": 1.1,
        },
        source_observations=[
            {
                "source_event_id": "srcevt_bad_twc",
                "source_id": "twc_current_icao_metric",
                "source_class": SOURCE_CLASSES["diagnostic_current"],
                "temperature_c": 33.0,
                "observed_today_max_c": 33.0,
            },
            {
                "source_event_id": "srcevt_bad_forecast",
                "source_id": "zuuu_taf_forecast_peak",
                "source_class": SOURCE_CLASSES["forecast_weak"],
                "forecast_peak_c": 33.0,
            },
        ],
    )
    short_only = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW011 SCT020",
            "dewpoint_trend_c_per_hour": -1.0,
            "temperature_momentum_5m_c_per_hour": 2.4,
            "temperature_momentum_3m_c_per_hour": 2.8,
        },
    )
    hard_floor = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T15:00:00+08:00",
        observation_inputs={
            "current_temp_c": 31.0,
            "observed_today_max_c": 31.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW020",
            "momentum_rate_c_per_hour": 0.3,
        },
    )
    metar_contract = build_metar_strong_likelihood_contract(confirmed)
    confirmed_trace = confirmed.get("source_trace") if isinstance(confirmed.get("source_trace"), list) else []
    excluded_from_metar = [
        row for row in confirmed_trace if isinstance(row, Mapping) and not row.get("included")
    ]
    confirmed_map = confirmed.get("metar_strong_likelihood_map")
    short_map = short_only.get("metar_strong_likelihood_map")
    confirmed_map = confirmed_map if isinstance(confirmed_map, Mapping) else {}
    short_map = short_map if isinstance(short_map, Mapping) else {}
    checks = {
        "forecast_contract_ok": forecast_contract.get("ok") is True,
        "forecast_only_includes_weak_sources": all(
            row.get("source_class") == SOURCE_CLASSES["forecast_weak"] for row in included
        ),
        "forecast_excludes_current_hard_market_sources": (
            len(excluded_non_forecast) >= 3
            and all(
                row.get("exclusion_reason") == "source_class_not_forecast_weak"
                for row in excluded_non_forecast
            )
        ),
        "forecast_hard_observation_fields_ignored": bool(hard_field_forecast)
        and hard_field_forecast[0].get("hard_observation_fields_ignored") is True,
        "forecast_group_precision_cap_applied": bool(model_groups)
        and model_groups[0].get("cap_hit") is True
        and model_groups[0].get("capped_precision") == model_groups[0].get("precision_cap"),
        "metar_contract_ok": metar_contract.get("ok") is True,
        "metar_excludes_non_hard_sources": len(excluded_from_metar) >= 2
        and all(row.get("exclusion_reason") == "source_class_not_hard_observation" for row in excluded_from_metar),
        "temperature_trajectory_is_internal_deduped_feature": (
            confirmed.get("temperature_trajectory", {}).get("boundary")
            == "observation_layer_internal_feature_not_independent_likelihood"
            and confirmed.get("boundary", {}).get("temperature_trajectory_consumed_once_inside_strong_likelihood")
            is True
        ),
        "short_window_momentum_not_overconfirmed": (
            short_only.get("temperature_trajectory", {}).get("gate_state") == "short_window_watch"
            and float(short_map.get("33") or 0.0) < float(confirmed_map.get("33") or 0.0)
        ),
        "observed_max_hard_floor_masks_lower_buckets_once": (
            hard_floor.get("hard_constraints", {}).get("masked_zero_buckets") == ["29", "30"]
            and hard_floor.get("hard_constraints", {}).get("hard_mask", {}).get("29") == 0.0
            and hard_floor.get("hard_constraints", {}).get("hard_mask", {}).get("30") == 0.0
        ),
    }
    return _stage_report(
        stage="likelihood",
        checks=checks,
        details={
            "forecast_included_source_count": len(included),
            "forecast_excluded_source_count": len(source_trace) - len(included),
            "forecast_group_count": len(forecast.get("source_group_trace", []))
            if isinstance(forecast.get("source_group_trace"), list)
            else 0,
            "metar_trajectory_state": confirmed.get("temperature_trajectory", {}).get("state"),
            "metar_trajectory_gate_state": confirmed.get("temperature_trajectory", {}).get("gate_state"),
            "short_window_gate_state": short_only.get("temperature_trajectory", {}).get("gate_state"),
            "hard_floor_masked_buckets": hard_floor.get("hard_constraints", {}).get("masked_zero_buckets"),
        },
    )


def _build_posterior_fixture() -> dict[str, Any]:
    labels = ["29", "30", "31", "32"]
    prior = {label: 0.25 for label in labels}
    neutral_metar = {
        "hard_constraints": {
            "hard_mask": {label: 1.0 for label in labels},
            "hard_mask_reason": "none",
        },
        "observation_features": {"expected_peak_window_end_hour_local": 17.5},
        "metar_strong_likelihood_map": {label: 0.25 for label in labels},
    }

    def forecast(mean_c: float) -> dict[str, Any]:
        return {
            "source_group_trace": [
                {
                    "group": "model",
                    "forecast_peak_mean_c": mean_c,
                    "forecast_sigma_effective_c": 0.45,
                    "capped_precision": 1.0,
                    "source_count": 2,
                }
            ],
            "forecast_weak_likelihood_map": {label: 0.25 for label in labels},
        }

    def build(
        *,
        forecast_mean_c: float,
        phase: str,
        beta_weak: float,
        metar_output: Mapping[str, Any] | None = None,
        observations: Sequence[Mapping[str, Any]] | None = None,
        hard_mask: Mapping[str, Any] | None = None,
        as_of: str = "2026-07-02T10:00:00+00:00",
    ) -> dict[str, Any]:
        return build_sequential_remaining_warming_posterior(
            buckets=labels,
            prior_distribution=prior,
            forecast_output=forecast(forecast_mean_c),
            metar_output=metar_output or neutral_metar,
            source_observations=observations or [],
            hard_mask=hard_mask or {label: 1.0 for label in labels},
            event_date="2026-07-03",
            as_of=as_of,
            station_timezone="Asia/Shanghai",
            rounding_rule="rounded_degrees",
            phase=phase,
            phase_reason="unit_contract_fixture",
            beta_weak=beta_weak,
            beta_strong=1.0,
            information_quality={"metar_strong": {"quality": 1.0}},
        )

    weak_low = build(forecast_mean_c=29.0, phase="D1_late", beta_weak=0.0)
    weak_high = build(forecast_mean_c=32.0, phase="D1_late", beta_weak=0.0)
    d1 = build(forecast_mean_c=32.0, phase="D1_late", beta_weak=0.9)
    observations = [
        {
            "source_event_id": f"metar_{index}",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": SOURCE_CLASSES["hard_observation"],
            "event_date": "2026-07-03",
            "valid_time": valid_time,
            "fetched_at": fetched_at,
            "temperature_c": temperature,
            "observed_today_max_c": temperature,
            "payload": {"cloud_text": "CAVOK"},
        }
        for index, (valid_time, fetched_at, temperature) in enumerate(
            (
                ("2026-07-03T04:00:00+00:00", "2026-07-03T04:01:00+00:00", 29.0),
                ("2026-07-03T04:30:00+00:00", "2026-07-03T04:31:00+00:00", 30.0),
                ("2026-07-03T05:00:00+00:00", "2026-07-03T05:01:00+00:00", 31.0),
            )
        )
    ]
    hard_mask = {"29": 0.0, "30": 0.0, "31": 1.0, "32": 1.0}
    observed_metar = {
        "hard_constraints": {
            "hard_mask": hard_mask,
            "observed_today_max_c": 31.0,
            "current_temperature_c": 31.0,
            "hard_mask_reason": "observed_today_max_c",
        },
        "observation_features": {"expected_peak_window_end_hour_local": 17.5},
        "metar_strong_likelihood_map": {label: 0.25 for label in labels},
    }
    d0 = build(
        forecast_mean_c=32.0,
        phase="D0_midday",
        beta_weak=0.2,
        metar_output=observed_metar,
        observations=observations,
        hard_mask=hard_mask,
        as_of="2026-07-03T06:00:00+00:00",
    )
    weak_low_map = weak_low.get("posterior_temperature_distribution_map", {})
    weak_high_map = weak_high.get("posterior_temperature_distribution_map", {})
    d1_map = d1.get("posterior_temperature_distribution_map", {})
    d0_map = d0.get("posterior_temperature_distribution_map", {})
    d0_grid = d0.get("posterior_continuous_distribution", {}).get("grid", [])
    checks = {
        "posterior_runtime_read_only": (
            weak_low.get("read_only") is True
            and weak_low.get("production_entry_enabled") is False
            and weak_low.get("guards", {}).get("writes_facts_sqlite") is False
        ),
        "beta_zero_makes_weak_layer_neutral": weak_low_map == weak_high_map,
        "hard_mask_conditions_bucket_and_continuous_distribution": (
            d0_map.get("29") == 0.0
            and d0_map.get("30") == 0.0
            and d0.get("hard_constraints", {}).get("hard_constraint_violation_count") == 0
            and all(
                float(row.get("mass") or 0.0) == 0.0
                for row in d0_grid
                if isinstance(row, Mapping) and float(row.get("temp_c") or 0.0) < 30.5
            )
        ),
        "legacy_bucket_product_retired_from_live_composer": (
            d0.get("boundary", {}).get("legacy_bucket_product_used") is False
            and d0.get("boundary", {}).get("live_primary_composer") is True
        ),
        "d1_weak_weight_can_dominate_when_strong_beta_zero": float(d1_map.get("32") or 0.0)
        > float(d1_map.get("29") or 0.0),
        "d0_uses_distinct_valid_time_sequential_state": (
            d0.get("sequential_state_filter", {}).get("point_count") == 3
            and float(d0.get("observation_information_gate", {}).get("observation_weight") or 0.0) > 0.0
        ),
        "layer_boundaries_present": (
            d0.get("layer_contributions", {}).get("prior", {}).get("role") == "root_prior"
            and d0.get("layer_contributions", {}).get("forecast_weak_likelihood", {}).get("role")
            == "forecast_error_mixture_likelihood_on_continuous_grid"
            and d0.get("layer_contributions", {}).get("metar_strong_likelihood", {}).get("role")
            == "zero_inflated_remaining_warming_predictive_from_sequential_state_filter"
            and d0.get("layer_contributions", {}).get("phase_weight", {}).get("strong_beta_used_as_bucket_exponent")
            is False
        ),
        "posterior_distribution_normalized": abs(
            _sum_mapping(d0.get("posterior_temperature_distribution_map", {})) - 1.0
        )
        <= 1e-9,
    }
    return _stage_report(
        stage="posterior",
        checks=checks,
        details={
            "d1_top_bucket": max(d1_map, key=lambda key: float(d1_map.get(key) or 0.0)),
            "d0_top_bucket": max(d0_map, key=lambda key: float(d0_map.get(key) or 0.0)),
            "hard_masked_buckets": d0.get("hard_constraints", {}).get("masked_zero_buckets"),
            "sequential_point_count": d0.get("sequential_state_filter", {}).get("point_count"),
            "observation_weight": d0.get("observation_information_gate", {}).get("observation_weight"),
        },
    )


def build_hierarchical_probability_unit_contract_audit() -> dict[str, Any]:
    stage_reports = {
        "prior": _build_prior_fixture(),
        "likelihood": _build_likelihood_fixture(),
        "posterior": _build_posterior_fixture(),
    }
    blockers = [
        dict(blocker)
        for report in stage_reports.values()
        for blocker in report.get("blockers") or []
        if isinstance(blocker, Mapping)
    ]
    issue_counts: dict[str, int] = {}
    for stage, report in stage_reports.items():
        for issue, count in dict(report.get("issue_counts") or {}).items():
            issue_counts[f"{stage}.{issue}"] = int(count)
    checked = sum(
        int(report.get("coverage", {}).get("checked_fact_count") or 0)
        for report in stage_reports.values()
        if isinstance(report.get("coverage"), Mapping)
    )
    ok_count = sum(
        int(report.get("coverage", {}).get("ok_fixture_count") or 0)
        for report in stage_reports.values()
        if isinstance(report.get("coverage"), Mapping)
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "hierarchical_probability_unit_contract_audit_read_only",
        "coverage_mode": "runtime_unit_contract_fixtures_no_db_writes",
        "coverage": {
            "checked_fact_count": checked,
            "checked_fixture_count": checked,
            "ok_fixture_count": ok_count,
            "issue_fixture_count": checked - ok_count,
        },
        "checks": {stage: bool(report.get("ok")) for stage, report in stage_reports.items()},
        "stage_reports": stage_reports,
        "issue_counts": issue_counts,
        "blockers": blockers,
        "warnings": [],
        "guards": _guards(),
        "next_boundary": (
            "hierarchical_probability_unit_contract_ok_continue_to_fact_coverage_release_gate"
            if not blockers
            else "fix_hierarchical_probability_unit_contract_before_release_gate"
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only unit contract audit for prior -> likelihood -> posterior runtime semantics."
    )
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_hierarchical_probability_unit_contract_audit()
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "execution_mode": report["execution_mode"],
            "coverage": report["coverage"],
            "checks": report["checks"],
            "issue_counts": report["issue_counts"],
            "blockers": report["blockers"],
            "guards": report["guards"],
            "next_boundary": report["next_boundary"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
