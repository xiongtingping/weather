#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import weather_fact_snapshot_writer as fact_writer

SCHEMA_VERSION = "fact_writer_persisted_apply_contract_audit_v1"
SUMMARY_SCHEMA = "weather_fact_snapshot_writer_apply_result_summary_v1"


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
        "executes_archive_sql": False,
        "uses_temporary_sqlite_fixture": True,
    }


def _to_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _prob01(value: Any) -> float | None:
    parsed = _to_float(value)
    if parsed is None:
        return None
    if 1.0 < parsed <= 100.0:
        parsed = parsed / 100.0
    if parsed < 0.0 or parsed > 1.0:
        return None
    return float(parsed)


def _create_fact_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            model_snapshot_id INTEGER,
            market_snapshot_id INTEGER,
            composer_version TEXT NOT NULL,
            market_updated_at TEXT,
            model_updated_at TEXT,
            market_stale_sec INTEGER,
            model_cache_age_sec INTEGER,
            best_side TEXT,
            best_outcome TEXT,
            best_edge_score REAL,
            best_signal_edge_entry REAL,
            fact_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_summary_cache (
            fact_id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            run_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _source_trace() -> dict[str, Any]:
    return {
        "schema": "weather_source_acquisition_trace_v1",
        "correlation_id": "corr_persisted_apply_contract",
        "source_observations": [
            {
                "source_event_id": "srcevt_persisted_apply_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "source_class": "hard_observation_candidate",
                "hard_eligible": True,
                "valid_time": "2026-07-03T09:00:00Z",
                "temperature_c": 30.0,
                "observed_today_max_c": 30.0,
            }
        ],
        "source_latency_trace": [
            {
                "source_event_id": "srcevt_persisted_apply_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "valid_time": "2026-07-03T09:00:00Z",
                "response_duration_ms": 250,
            }
        ],
        "hard_observation_candidate_ids": ["srcevt_persisted_apply_awc"],
    }


def _prior_fields(distribution: Mapping[str, float]) -> dict[str, Any]:
    return {
        "prior_distribution": dict(distribution),
        "prior_na_reason": "",
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": "available",
            "source": "unit_fixture_settled_history_prior",
            "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
            "sample_count": 10,
            "shrinkage_target": "broader_settled_history",
            "date_window_days": 15,
            "prior_na_reason": "",
        },
    }


def _base_payload() -> dict[str, Any]:
    distribution = {"29": 0.05, "30": 0.20, "31": 0.75}
    return {
        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "fact_as_of": "2026-07-03T09:05:40Z",
        "posterior_temperature_distribution": [
            {"bucket": bucket, "probability": probability}
            for bucket, probability in distribution.items()
        ],
        "posterior_temperature_distribution_map": dict(distribution),
        **_prior_fields(distribution),
        "phase": "D0_awc_hard_signal",
        "phase_reason": "persisted_apply_contract_fixture",
        "beta_weak": 0.0,
        "beta_strong": 1.0,
        "hard_mask_reason": "observed_today_max_c",
        "weather_likelihood_features": {
            "zuuu_metar_raw_awc_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        "layer_contributions": {
            "prior": {
                "role": "root_prior",
                "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
                "distribution": dict(distribution),
            },
            "forecast_weak_likelihood": {
                "role": "prediction_weak_likelihood",
                "distribution": {"29": 1 / 3, "30": 1 / 3, "31": 1 / 3},
            },
            "metar_strong_likelihood": {
                "role": "observation_strong_likelihood",
                "distribution": {"29": 0.0, "30": 0.2, "31": 0.8},
            },
        },
    }


def _build_plan(payload: Mapping[str, Any]) -> dict[str, Any]:
    return fact_writer.build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload=payload,
        source_acquisition_trace=_source_trace(),
        composer_version=fact_writer.PARSER_VERSION,
        write_reason="persisted_apply_contract_audit_temp_fixture",
        include_payload=True,
        enable_apply_contract=True,
        write_latest=False,
    )


def audit_writer_apply_result_summary(
    summary: Mapping[str, Any] | None,
    *,
    expected_state: str,
) -> dict[str, Any]:
    item = summary if isinstance(summary, Mapping) else {}
    issues: list[str] = []
    if item.get("schema") != SUMMARY_SCHEMA:
        issues.append("writer_apply_result_summary_schema_invalid")
    if item.get("write_owner") != fact_writer.WRITE_OWNER:
        issues.append("writer_apply_result_summary_write_owner_invalid")
    if item.get("lineage_verification_state") != expected_state:
        issues.append("writer_apply_result_summary_lineage_state_invalid")

    guards = item.get("guards") if isinstance(item.get("guards"), Mapping) else {}
    for key in ("writes_auto_trade_decisions", "places_orders", "restarts_launchd", "historical_backfill", "changes_parameters"):
        if guards.get(key) is True:
            issues.append(f"writer_apply_result_summary_guard_{key}_unsafe")

    if expected_state == "persisted_verified":
        if item.get("ok") is not True or item.get("applied") is not True or item.get("blocked") is True:
            issues.append("persisted_summary_apply_state_invalid")
        if not item.get("fact_id"):
            issues.append("persisted_summary_fact_id_missing")
        if item.get("post_apply_persisted_verification_present") is not True:
            issues.append("persisted_summary_post_apply_verification_missing")
        if item.get("post_apply_persisted_verification_ok") is not True:
            issues.append("persisted_summary_post_apply_verification_not_ok")
        if item.get("post_apply_persisted_verification_fact_id") != item.get("fact_id"):
            issues.append("persisted_summary_fact_id_mismatch")
        if item.get("prior_layer_contract_ok") is not True:
            issues.append("persisted_summary_prior_contract_not_ok")
        if item.get("posterior_ssot_apply_contract_ok") is not True:
            issues.append("persisted_summary_posterior_ssot_contract_not_ok")
        if not str(item.get("fact_snapshot_id") or ""):
            issues.append("persisted_summary_fact_snapshot_id_missing")
        if not str(item.get("posterior_run_id") or ""):
            issues.append("persisted_summary_posterior_run_id_missing")
        if not str(item.get("target_bucket") or ""):
            issues.append("persisted_summary_target_bucket_missing")
        if _prob01(item.get("target_probability")) is None:
            issues.append("persisted_summary_target_probability_missing")
        if not str(item.get("phase") or ""):
            issues.append("persisted_summary_phase_missing")
        if _to_float(item.get("beta_weak")) is None:
            issues.append("persisted_summary_beta_weak_missing")
        if _to_float(item.get("beta_strong")) is None:
            issues.append("persisted_summary_beta_strong_missing")
        if not str(item.get("hard_mask_reason") or ""):
            issues.append("persisted_summary_hard_mask_reason_missing")
    elif expected_state == "blocked_before_persist":
        if item.get("applied") is True or item.get("blocked") is not True:
            issues.append("blocked_summary_apply_state_invalid")
        if item.get("post_apply_persisted_verification_present") is True:
            issues.append("blocked_summary_must_not_have_post_apply_verification")
        if item.get("post_apply_persisted_verification_ok") is True:
            issues.append("blocked_summary_must_not_report_persisted_ok")
        if item.get("fact_id"):
            issues.append("blocked_summary_fact_id_should_be_empty")
        if item.get("posterior_ssot_apply_contract_ok") is True:
            issues.append("blocked_summary_posterior_contract_should_not_be_ok")
        if not item.get("posterior_ssot_apply_contract_issues"):
            issues.append("blocked_summary_posterior_contract_issues_missing")
    else:
        issues.append("expected_state_unsupported")

    return {
        "schema": "writer_apply_result_summary_contract_v1",
        "ok": not issues,
        "expected_state": str(expected_state or ""),
        "issues": issues,
        "lineage_verification_state": str(item.get("lineage_verification_state") or ""),
        "fact_id": item.get("fact_id"),
        "target_bucket": str(item.get("target_bucket") or ""),
        "target_probability": _prob01(item.get("target_probability")),
        "post_apply_persisted_verification_ok": bool(item.get("post_apply_persisted_verification_ok")),
        "posterior_ssot_apply_contract_ok": bool(item.get("posterior_ssot_apply_contract_ok")),
    }


def _run_success_fixture() -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    try:
        plan = _build_plan(_base_payload())
        result = fact_writer.apply_fact_snapshot_write_plan(
            conn,
            plan,
            allow_apply=True,
            created_at="2026-07-03T09:05:41Z",
        )
        summary = fact_writer.compact_fact_snapshot_writer_apply_result_for_lineage(result)
        contract = audit_writer_apply_result_summary(summary, expected_state="persisted_verified")
        persisted_rows = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    finally:
        conn.close()
    if persisted_rows != 1:
        contract["ok"] = False
        contract.setdefault("issues", []).append("success_fixture_persisted_row_count_invalid")
    return {
        "fixture": "persisted_verified",
        "ok": bool(contract.get("ok")),
        "contract": contract,
        "writer_result_ok": bool(result.get("ok")),
        "writer_result_applied": bool(result.get("applied")),
        "summary": summary,
        "persisted_row_count": int(persisted_rows),
    }


def _run_blocked_fixture() -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    try:
        plan = _build_plan(_base_payload())
        tampered = copy.deepcopy(plan)
        payload = tampered["effective_payload"]
        payload["posterior_ssot_fact"]["target_bucket"] = "29"
        payload["posterior_ssot_fact"]["target_probability"] = 0.05
        for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
            payload.setdefault(
                key,
                {
                    "ssot_source": "posterior_ssot_fact",
                    "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
                    "target_probability_field": "posterior_ssot_fact.target_probability",
                    "selection_runtime_recomputed": False,
                    "fact_snapshot_id": payload.get("fact_snapshot_id"),
                    "posterior_run_id": payload.get("posterior_run_id"),
                },
            )
            payload[key]["target_bucket"] = "29"
            payload[key]["target_probability"] = 0.05
        tampered["payload_hash_after"] = fact_writer.sha256_json(payload)
        result = fact_writer.apply_fact_snapshot_write_plan(
            conn,
            tampered,
            allow_apply=True,
            created_at="2026-07-03T09:05:41Z",
        )
        summary = fact_writer.compact_fact_snapshot_writer_apply_result_for_lineage(result)
        contract = audit_writer_apply_result_summary(summary, expected_state="blocked_before_persist")
        persisted_rows = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    finally:
        conn.close()
    if persisted_rows != 0:
        contract["ok"] = False
        contract.setdefault("issues", []).append("blocked_fixture_persisted_row_count_invalid")
    return {
        "fixture": "blocked_before_persist",
        "ok": bool(contract.get("ok")),
        "contract": contract,
        "writer_result_ok": bool(result.get("ok")),
        "writer_result_applied": bool(result.get("applied")),
        "summary": summary,
        "persisted_row_count": int(persisted_rows),
    }


def build_fact_writer_persisted_apply_contract_audit() -> dict[str, Any]:
    fixtures = [_run_success_fixture(), _run_blocked_fixture()]
    blockers: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}
    for fixture in fixtures:
        contract = fixture.get("contract") if isinstance(fixture.get("contract"), Mapping) else {}
        for issue in contract.get("issues") or []:
            issue_key = str(issue)
            issue_counts[issue_key] = issue_counts.get(issue_key, 0) + 1
        if not fixture.get("ok"):
            blockers.append(
                {
                    "blocker": "writer_apply_result_summary_fixture_failed",
                    "fixture": str(fixture.get("fixture") or ""),
                    "issues": list(contract.get("issues") or []),
                }
            )
    compact_fixtures = [
        {
            "fixture": str(item.get("fixture") or ""),
            "ok": bool(item.get("ok")),
            "writer_result_ok": bool(item.get("writer_result_ok")),
            "writer_result_applied": bool(item.get("writer_result_applied")),
            "persisted_row_count": int(item.get("persisted_row_count") or 0),
            "contract": item.get("contract"),
        }
        for item in fixtures
    ]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "temporary_sqlite_fixture_no_archive_db_write",
        "coverage_mode": "writer_apply_result_summary_temp_persisted_fixture",
        "coverage": {
            "checked_fact_count": len(fixtures),
            "checked_fixture_count": len(fixtures),
            "ok_fixture_count": sum(1 for item in fixtures if item.get("ok")),
            "issue_fixture_count": sum(1 for item in fixtures if not item.get("ok")),
        },
        "issue_counts": dict(sorted(issue_counts.items())),
        "blockers": blockers,
        "warnings": [],
        "guards": _guards(),
        "fixtures": compact_fixtures,
        "next_boundary": (
            "writer_apply_result_summary_contract_ok_for_persisted_fact_consumers"
            if not blockers
            else "fix_writer_apply_result_summary_contract_before_persisted_fact_release"
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit weather_fact_snapshot_writer apply result summaries using temporary SQLite fixtures."
    )
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_fact_writer_persisted_apply_contract_audit()
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "execution_mode": report["execution_mode"],
            "coverage_mode": report["coverage_mode"],
            "coverage": report["coverage"],
            "issue_counts": report["issue_counts"],
            "blockers": report["blockers"],
            "guards": report["guards"],
            "next_boundary": report["next_boundary"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
