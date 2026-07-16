#!/usr/bin/env python3
"""Read-only integration contract for the hierarchical Bayes core chain.

The audit uses an in-memory fixture to prove the same posterior SSOT fact can
flow through fact writer, UI snapshot lineage, auto-trade candidate details,
and daily review/calibration runtime without a second target口径. It does not
read or write production DB state, fetch external sources, or place orders.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import auto_trade_decision_writer as decision_writer  # noqa: E402
import weather_fact_snapshot_writer as fact_writer  # noqa: E402
from daily_weather_review_runtime import build_daily_weather_review  # noqa: E402
from scripts import ui_snapshot_lineage_audit as ui_lineage_audit  # noqa: E402


SCHEMA_VERSION = "hierarchical_core_integration_contract_audit_v1"


def _guards() -> dict[str, bool]:
    return {
        "reads_archive_db": False,
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "writes_daily_calibration_records": False,
        "writes_bias_correction_candidates": False,
        "writes_parameter_change_events": False,
        "fetches_external_weather_sources": False,
        "fetches_orderbook": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _source_trace() -> dict[str, Any]:
    return {
        "schema": "weather_source_acquisition_trace_v1",
        "correlation_id": "corr_core_integration_20260703",
        "source_observations": [
            {
                "source_event_id": "srcevt_core_integration_20260703_awc",
                "source_observation_id": "srcobs_core_integration_20260703_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "source_class": "hard_observation_candidate",
                "hard_eligible": True,
                "valid_time": "2026-07-03T09:00:00Z",
                "temperature_c": 30.0,
            }
        ],
        "source_latency_trace": [
            {
                "source_event_id": "srcevt_core_integration_20260703_awc",
                "source_observation_id": "srcobs_core_integration_20260703_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "valid_time": "2026-07-03T09:00:00Z",
                "response_duration_ms": 280,
            }
        ],
        "source_observation_ids": ["srcobs_core_integration_20260703_awc"],
        "hard_observation_candidate_ids": ["srcevt_core_integration_20260703_awc"],
    }


def _prior_fields(distribution: Mapping[str, float]) -> dict[str, Any]:
    return {
        "prior_distribution": dict(distribution),
        "prior_na_reason": "",
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": "available",
            "source": "core_integration_fixture_history_prior",
            "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
            "sample_count": 30,
            "shrinkage_target": "broader_settled_history",
            "date_window_days": 15,
            "prior_na_reason": "",
        },
    }


def _incoming_payload() -> dict[str, Any]:
    posterior = {
        "30°C": 0.20,
        "31°C": 0.55,
        "32°C": 0.25,
    }
    return {
        "posterior_temperature_distribution": {
            "bucket_probabilities": [
                {"bucket": bucket, "probability": probability}
                for bucket, probability in posterior.items()
            ]
        },
        "distribution": [
            {"outcome": bucket, "q_temperature_bucket_posterior": probability}
            for bucket, probability in posterior.items()
        ],
        **_prior_fields({"30°C": 0.30, "31°C": 0.45, "32°C": 0.25}),
        "phase": "D0_midday",
        "phase_reason": "core_integration_fixture_after_hard_source_update",
        "beta_weak": 0.30,
        "beta_strong": 0.85,
        "hard_mask_reason": "observed_today_max_floor_30c",
        "layer_contributions": {
            "prior": {"boundary": "historical_climate_date_site_terrain_weather_type_only"},
            "forecast_weak_likelihood": {"boundary": "forecast_weak_source_group_shrinkage"},
            "metar_strong_likelihood": {"boundary": "metar_temperature_momentum_physics_once"},
        },
        "temperature_trajectory": {
            "state": "high_temperature_platform",
            "gate_state": "confirmed_multi_window",
            "gate_reason": "rolling_slope_supports_remaining_warming",
            "effective_rate_c_per_hour": 1.2,
            "adjusted_rate_c_per_hour": 1.0,
        },
        "weather_likelihood_features": {
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "awc_cache_metars_csv_gz",
        },
    }


def _decision_details_from_ssot(
    *,
    payload: Mapping[str, Any],
    ssot: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "decision_ssot": "posterior_ssot_fact",
        "posterior_ssot_fact_used": True,
        "posterior_settlement_target_bucket": ssot.get("target_bucket"),
        "posterior_settlement_target_probability": ssot.get("target_probability"),
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "fact_snapshot_id": payload.get("fact_snapshot_id"),
        "posterior_run_id": payload.get("posterior_run_id"),
        "source_event_ids": list(payload.get("source_event_ids") or []),
        "source_observation_ids": list(payload.get("source_observation_ids") or []),
    }


def _json_from_row(row: Sequence[Any], index: int) -> Any:
    try:
        return json.loads(str(row[index] or "{}"))
    except (TypeError, ValueError, IndexError):
        return {}


def build_audit(
    *,
    mutate_payload: Callable[[dict[str, Any]], None] | None = None,
    mutate_ui_item: Callable[[dict[str, Any]], None] | None = None,
    mutate_decision_payload: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []

    trace = _source_trace()
    plan = fact_writer.build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload=_incoming_payload(),
        source_acquisition_trace=trace,
        include_payload=True,
        enable_apply_contract=True,
    )
    payload = copy.deepcopy(dict(plan.get("effective_payload") or {}))
    if mutate_payload:
        mutate_payload(payload)
    ssot = dict(payload.get("posterior_ssot_fact") or {})

    if not plan.get("ok"):
        blockers.append("fact_writer_plan_not_ok")
    if not plan.get("read_only") or plan.get("dry_run") is not True:
        blockers.append("fact_writer_plan_not_read_only_dry_run")
    if not dict(plan.get("posterior_ssot_apply_contract") or {}).get("ok"):
        blockers.append("posterior_ssot_apply_contract_not_ok")
    if ssot.get("schema") != fact_writer.POSTERIOR_SSOT_FACT_SCHEMA:
        blockers.append("posterior_ssot_fact_schema_invalid")
    if ssot.get("state") != "available":
        blockers.append("posterior_ssot_fact_not_available")
    if not ssot.get("target_bucket") or ssot.get("target_probability") is None:
        blockers.append("posterior_ssot_fact_target_missing")
    if ssot.get("fact_snapshot_id") != payload.get("fact_snapshot_id"):
        blockers.append("posterior_ssot_fact_snapshot_id_mismatch")
    if ssot.get("posterior_run_id") != payload.get("posterior_run_id"):
        blockers.append("posterior_ssot_posterior_run_id_mismatch")
    if dict(ssot.get("boundaries") or {}).get("frontend_recompute_allowed") is not False:
        blockers.append("posterior_ssot_frontend_recompute_boundary_missing")
    if dict(ssot.get("boundaries") or {}).get("decision_recompute_allowed") is not False:
        blockers.append("posterior_ssot_decision_recompute_boundary_missing")

    ui_item = copy.deepcopy(payload)
    ui_item.setdefault("id", 1001)
    ui_item.setdefault("ui_sections", {})
    ui_item["ui_sections"]["bayes_posterior_ssot"] = {
        "source": "posterior_ssot_fact",
        "target_bucket": ssot.get("target_bucket"),
        "target_probability": ssot.get("target_probability"),
        "fact_snapshot_id": ssot.get("fact_snapshot_id"),
        "posterior_run_id": ssot.get("posterior_run_id"),
    }
    if mutate_ui_item:
        mutate_ui_item(ui_item)
    ui_issues = ui_lineage_audit._posterior_ssot_lineage_issues(  # noqa: SLF001
        ui_item=ui_item,
        fact_payload=payload,
    )
    for issue in ui_issues:
        blockers.append(f"ui_snapshot_lineage:{issue}")

    decision_payload = {
        "run_id": "run_core_integration_20260703",
        "run_date": "2026-07-03",
        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "title": "Highest temperature in Chengdu on July 3?",
        "outcome": ssot.get("target_bucket"),
        "side": "BUY",
        "action": "entry",
        "decided_state": "entry_candidate",
        "will_order": 1,
        "reason_codes": ["posterior_settlement_target_candidate"],
        "reasons": ["posterior SSOT target candidate from fact payload"],
        "details": _decision_details_from_ssot(payload=payload, ssot=ssot),
        "created_at": "2026-07-03T09:05:41Z",
    }
    if mutate_decision_payload:
        mutate_decision_payload(decision_payload)
    decision_row = decision_writer.build_auto_trade_decision_row_from_payload(decision_payload)
    decision_details = _json_from_row(decision_row, 12)
    decision_checks = {
        "row_target_matches_ssot": decision_row[5] == ssot.get("target_bucket"),
        "details_target_matches_ssot": decision_details.get("posterior_settlement_target_bucket") == ssot.get("target_bucket"),
        "details_probability_matches_ssot": decision_details.get("posterior_settlement_target_probability") == ssot.get("target_probability"),
        "details_fact_snapshot_matches_ssot": decision_details.get("fact_snapshot_id") == ssot.get("fact_snapshot_id"),
        "details_posterior_run_matches_ssot": decision_details.get("posterior_run_id") == ssot.get("posterior_run_id"),
        "decision_source_is_posterior_ssot": decision_details.get("decision_ssot") == "posterior_ssot_fact"
        and decision_details.get("posterior_ssot_fact_used") is True,
    }
    for key, ok in decision_checks.items():
        if not ok:
            blockers.append(f"auto_trade_candidate_{key}_failed")

    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=payload,
        decisions=[decision_payload],
        orders=[{"order_id": "ord_core_integration_1", "order_status": "submitted"}],
        settlement={"settlement_actual": 31.0},
        source_health={"ok": True},
        timing={"signal_to_order_ms": 2200},
        audit_attachments={
            "ui_snapshot_lineage": {
                "schema": "ui_snapshot_lineage_audit_fixture_v1",
                "ok": not ui_issues,
                "blockers": ui_issues,
                "read_only": True,
            }
        },
    )
    review_json = dict(review.get("review_json") or {})
    review_checks = {
        "daily_review_uses_posterior_ssot_target": review_json.get("target_bucket") == ssot.get("target_bucket"),
        "daily_review_has_no_bad_link": review.get("first_bad_link") == "none",
        "daily_review_lineage_matches_fact": review.get("fact_snapshot_id") == ssot.get("fact_snapshot_id")
        and review.get("posterior_run_id") == ssot.get("posterior_run_id"),
        "daily_calibration_record_no_apply": dict(review.get("daily_calibration_record") or {}).get("state") == "no_apply",
        "parameter_change_state_no_auto_apply": dict(review.get("parameter_change_state") or {}).get("guards", {}).get("changes_parameters") is False,
    }
    for key, ok in review_checks.items():
        if not ok:
            blockers.append(f"{key}_failed")

    checks = {
        "source_to_fact_writer_plan": bool(plan.get("ok") and plan.get("read_only") and plan.get("dry_run") is True),
        "production_posterior_output_contract": not any(
            blocker.startswith("posterior_ssot") or blocker == "posterior_ssot_fact_target_missing"
            for blocker in blockers
        ),
        "ui_snapshot_reads_posterior_ssot": not any(blocker.startswith("ui_snapshot_lineage:") for blocker in blockers),
        "auto_trade_candidate_reads_posterior_ssot": all(decision_checks.values()),
        "fact_snapshot_to_daily_review_to_calibration": all(review_checks.values()),
    }

    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "hierarchical_core_integration_contract_audit_read_only",
        "checks": checks,
        "fact_writer": {
            "schema": plan.get("schema"),
            "ok": plan.get("ok"),
            "fact_snapshot_id": plan.get("fact_snapshot_id"),
            "posterior_run_id": plan.get("posterior_run_id"),
            "posterior_ssot_apply_contract": plan.get("posterior_ssot_apply_contract"),
        },
        "posterior_ssot_fact": {
            "schema": ssot.get("schema"),
            "state": ssot.get("state"),
            "target_bucket": ssot.get("target_bucket"),
            "target_probability": ssot.get("target_probability"),
            "fact_snapshot_id": ssot.get("fact_snapshot_id"),
            "posterior_run_id": ssot.get("posterior_run_id"),
        },
        "ui_snapshot_contract": {
            "issues": ui_issues,
            "target_bucket": dict(ui_item.get("posterior_ssot_fact") or {}).get("target_bucket"),
            "target_probability": dict(ui_item.get("posterior_ssot_fact") or {}).get("target_probability"),
        },
        "auto_trade_candidate_contract": {
            "checks": decision_checks,
            "row_outcome": decision_row[5],
            "details": decision_details,
        },
        "daily_review_contract": {
            "first_bad_link": review.get("first_bad_link"),
            "first_bad_link_reason": review.get("first_bad_link_reason"),
            "target_bucket": review_json.get("target_bucket"),
            "target_probability": review_json.get("target_probability"),
            "daily_calibration_state": dict(review.get("daily_calibration_record") or {}).get("state"),
            "parameter_change_state": dict(review.get("parameter_change_state") or {}).get("state"),
        },
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": _guards(),
        "next_boundary": "core_integration_contract_ready" if not blockers else "fix_core_integration_contract",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_audit()
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "execution_mode": report["execution_mode"],
            "checks": report["checks"],
            "blockers": report["blockers"],
            "warnings": report["warnings"],
            "guards": report["guards"],
            "next_boundary": report["next_boundary"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
