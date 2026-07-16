#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import daily_review_persistence_writer
import phase0_forward_schema_plan
import phase0_schema_apply
import phase0_schema_probe


SCHEMA_VERSION = "phase0_migration_manifest_v1"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_timestamp(value: str) -> str:
    return (
        value.replace("-", "")
        .replace(":", "")
        .replace(".", "")
        .replace("+0000", "Z")
        .replace("+00:00", "Z")
    )


def _recommended_backup_path(db: Path, generated_at: str) -> str:
    return str(db.with_name(f"{db.name}.phase0-{_safe_timestamp(generated_at)}.bak"))


def _file_state(db: Path) -> dict[str, Any]:
    wal = db.with_name(f"{db.name}-wal")
    shm = db.with_name(f"{db.name}-shm")
    return {
        "path": str(db),
        "exists": db.exists(),
        "size_bytes": db.stat().st_size if db.exists() else None,
        "wal_exists": wal.exists(),
        "wal_size_bytes": wal.stat().st_size if wal.exists() else None,
        "shm_exists": shm.exists(),
        "shm_size_bytes": shm.stat().st_size if shm.exists() else None,
    }


def _summarize_ddl(ddl: list[dict[str, Any]]) -> dict[str, Any]:
    add_columns = [item for item in ddl if item.get("action") == "add_column"]
    create_tables = [item for item in ddl if item.get("action") == "create_table"]
    create_unique_indexes = [item for item in ddl if item.get("action") == "create_unique_index"]
    return {
        "ddl_count": len(ddl),
        "add_column_count": len(add_columns),
        "create_table_count": len(create_tables),
        "create_unique_index_count": len(create_unique_indexes),
        "affected_tables": sorted({str(item.get("table")) for item in ddl if item.get("table")}),
        "requires_backup_count": sum(1 for item in ddl if item.get("requires_backup")),
        "apply_order_groups": sorted({str(item.get("apply_order_group") or "ungrouped") for item in ddl}),
    }


def _group_apply_sequence(ddl: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in ddl:
        group = str(item.get("apply_order_group") or "ungrouped")
        groups.setdefault(group, []).append(item)
    sequence = []
    for group in sorted(groups):
        items = groups[group]
        sequence.append(
            {
                "group": group,
                "ddl_count": len(items),
                "requires_backup": any(bool(item.get("requires_backup")) for item in items),
                "tables": sorted({str(item.get("table")) for item in items if item.get("table")}),
                "actions": [
                    {
                        "action": item.get("action"),
                        "table": item.get("table"),
                        "column": item.get("column"),
                        "sql": item.get("sql"),
                        "rollback_hint": item.get("rollback_hint"),
                    }
                    for item in items
                ],
            }
        )
    return sequence


def _rollback_plan(ddl: list[dict[str, Any]], backup_required: bool) -> dict[str, Any]:
    hints = []
    for item in ddl:
        hint = item.get("rollback_hint")
        if hint and hint not in hints:
            hints.append(str(hint))
    return {
        "requires_pre_apply_backup": backup_required,
        "primary_strategy": "restore_fresh_archive_db_backup_before_restart_or_backfill",
        "drop_only_new_empty_tables": [
            str(item.get("table"))
            for item in ddl
            if item.get("action") == "create_table" and item.get("table")
        ],
        "manual_rebuild_required_for_added_columns": any(item.get("action") == "add_column" for item in ddl),
        "rollback_hints": hints,
        "caveat": "No rollback is safe after production writes land without restoring a verified pre-apply backup.",
    }


def _review_persistence_writer_contract(db: Path) -> dict[str, Any]:
    if not db.exists():
        schema_status = {
            "ok": False,
            "reason": "archive_db_missing",
            "tables": {},
        }
    else:
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                schema_status = daily_review_persistence_writer.daily_review_persistence_schema_status(conn)
        except sqlite3.Error as exc:
            schema_status = {
                "ok": False,
                "reason": f"{exc.__class__.__name__}: {exc}",
                "tables": {},
            }
    return {
        "schema": "phase0_review_persistence_writer_contract_v1",
        "writer_schema": daily_review_persistence_writer.SCHEMA_VERSION,
        "plan_schema": daily_review_persistence_writer.PLAN_SCHEMA,
        "writer_module": "daily_review_persistence_writer.py",
        "planned_tables": list(daily_review_persistence_writer.REQUIRED_TABLE_COLUMNS.keys()),
        "supports_no_apply_plan": True,
        "supports_controlled_apply": True,
        "apply_preconditions": [
            "planned_tables_exist_with_required_columns",
            "explicit_allow_apply_required",
            "parameter_event_apply_requires_separate_allow_parameter_event_apply",
            "no_schema_creation_inside_writer",
        ],
        "schema_status": schema_status,
        "guards": {
            "creates_tables": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def build_manifest(db_path: Path, *, immutable: bool = False, generated_at: str | None = None) -> dict[str, Any]:
    generated_at = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    plan = phase0_forward_schema_plan.build_plan(db, immutable=immutable)
    ddl = list(plan.get("ddl") or [])
    backup_required = bool(ddl)
    backup_path = _recommended_backup_path(db, generated_at)
    db_exists = db.exists()
    ddl_summary = _summarize_ddl(ddl)
    review_persistence_contract = _review_persistence_writer_contract(db)

    preflight_checks = [
        {
            "name": "archive_db_exists",
            "status": "pass" if db_exists else "fail",
            "hard_blocker": not db_exists,
            "reason": "archive_db_found" if db_exists else "archive_db_missing",
        },
        {
            "name": "script_apply_support",
            "status": "available_controlled_contract",
            "hard_blocker": False,
            "reason": "phase0_schema_apply_contract_available_but_not_executed_by_manifest",
            "apply_script": "scripts/phase0_schema_apply.py",
        },
        {
            "name": "daily_review_persistence_writer_contract",
            "status": "available_controlled_contract",
            "hard_blocker": False,
            "reason": "daily_review_persistence_writer_available_but_not_executed_by_manifest",
            "writer_module": review_persistence_contract.get("writer_module"),
        },
        {
            "name": "explicit_user_confirmation",
            "status": "required" if ddl else "not_applicable",
            "hard_blocker": bool(ddl),
            "reason": "schema_changes_require_confirmed_apply_window" if ddl else "no_schema_change_needed",
        },
        {
            "name": "fresh_archive_db_backup",
            "status": "required" if backup_required else "not_applicable",
            "hard_blocker": backup_required,
            "reason": "backup_must_exist_before_any_schema_apply" if backup_required else "no_schema_change_needed",
        },
        {
            "name": "writer_quiescence",
            "status": "not_checked" if ddl else "not_applicable",
            "hard_blocker": bool(ddl),
            "reason": "requires_separate_maintenance_window_and_writer_lock_check" if ddl else "no_schema_change_needed",
        },
        {
            "name": "historical_backfill",
            "status": "blocked_by_design",
            "hard_blocker": False,
            "reason": "manifest_does_not_backfill_history",
        },
        {
            "name": "launchd_restart",
            "status": "blocked_by_design",
            "hard_blocker": False,
            "reason": "manifest_does_not_restart_daemons",
        },
        {
            "name": "post_apply_phase0_schema_probe_schema_only_strict",
            "status": "required" if ddl else "not_applicable",
            "hard_blocker": bool(ddl),
            "reason": "schema_surface_strict_probe_must_pass_after_schema_apply" if ddl else "schema_already_ready",
        },
        {
            "name": "post_apply_ssot_alignment",
            "status": "required" if ddl else "not_applicable",
            "hard_blocker": bool(ddl),
            "reason": "ssot_alignment_must_remain_clean_after_manual_apply" if ddl else "schema_already_ready",
        },
        {
            "name": "post_forward_write_phase0_schema_probe_full_strict",
            "status": "future_required" if ddl else "not_applicable",
            "hard_blocker": False,
            "reason": (
                "full_strict_probe_requires_future_fact_json_contract_after_writer_migration"
                if ddl
                else "schema_already_ready"
            ),
        },
    ]
    blockers = [item["name"] for item in preflight_checks if item.get("hard_blocker")]

    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(plan.get("ok", True)),
        "generated_at": generated_at,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "no_apply_preflight_manifest",
        "apply_supported": True,
        "apply_contract_schema": phase0_schema_apply.SCHEMA_VERSION,
        "manifest_valid_for_apply": False,
        "db": str(db),
        "db_file_state": _file_state(db),
        "source_plan_schema": plan.get("schema"),
        "probe_phase0_ready": bool(plan.get("probe_phase0_ready")),
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "creates_backup": False,
            "historical_backfill": False,
            "restarts_launchd": False,
            "places_orders": False,
            "changes_parameters": False,
        },
        "ddl_summary": ddl_summary,
        "backup_required": backup_required,
        "backup_created": False,
        "recommended_backup_path": backup_path if backup_required else None,
        "recommended_backup_path_exists": Path(backup_path).exists() if backup_required else None,
        "preflight_checks": preflight_checks,
        "apply_blockers": blockers,
        "apply_sequence": _group_apply_sequence(ddl),
        "rollback_plan": _rollback_plan(ddl, backup_required),
        "review_persistence_writer_contract": review_persistence_contract,
        "post_apply_verification_commands": [
            "python3 scripts/phase0_schema_probe.py --immutable --strict --schema-only",
            "python3 scripts/check_ssot_alignment.py --immutable",
            "python3 -m pytest tests/test_phase0_schema_probe.py tests/test_phase0_forward_schema_plan.py tests/test_phase0_migration_manifest.py tests/test_phase0_schema_apply.py tests/test_daily_review_persistence_writer.py tests/test_daily_review_persistence_apply.py tests/test_daily_weather_review_report.py tests/test_daily_review_persistence_batch_plan.py tests/test_daily_review_coverage_audit.py tests/test_daily_review_post_apply_acceptance_audit.py tests/test_hierarchical_probability_unit_contract_audit.py tests/test_fact_writer_persisted_apply_contract_audit.py tests/test_canonical_writer_persisted_coverage_audit.py tests/test_prior_layer_contract_audit.py tests/test_forecast_weak_likelihood_contract_audit.py tests/test_metar_strong_likelihood_contract_audit.py tests/test_posterior_ssot_fact_coverage_audit.py",
        ],
        "post_review_plan_verification_commands": [
            "python3 -m pytest tests/test_daily_review_persistence_batch_plan.py",
            "python3 -m pytest tests/test_daily_review_persistence_apply.py",
            "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 --max-items-output 20",
        ],
        "post_review_persistence_verification_commands": [
            "python3 -m pytest tests/test_daily_review_coverage_audit.py",
            "python3 -m pytest tests/test_daily_review_post_apply_acceptance_audit.py",
            "python3 scripts/daily_review_coverage_audit.py --since-days 45 --max-items-output 20",
            "python3 scripts/daily_review_post_apply_acceptance_audit.py --since-days 45 --max-items-output 20",
        ],
        "post_forward_write_verification_commands": [
            "python3 scripts/phase0_schema_probe.py --immutable --strict",
            "python3 scripts/hierarchical_probability_unit_contract_audit.py --summary",
            "python3 scripts/fact_writer_persisted_apply_contract_audit.py --summary",
            "python3 scripts/canonical_writer_persisted_coverage_audit.py --since-hours 168 --latest-only --limit 50 --max-items-output 20 --summary",
            "python3 scripts/prior_layer_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward",
            "python3 scripts/forecast_weak_likelihood_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward",
            "python3 scripts/metar_strong_likelihood_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward",
            "python3 scripts/posterior_ssot_fact_coverage_audit.py --since-hours 168 --latest-only --limit 50 --max-items-output 20 --project-forward",
            "python3 scripts/hierarchical_probability_release_gate.py --since-hours 168 --limit 50 --max-items-output 20",
        ],
        "post_apply_verification_stages": [
            {
                "stage": "schema_surface",
                "commands": [
                    "python3 scripts/phase0_schema_probe.py --immutable --strict --schema-only",
                    "python3 scripts/check_ssot_alignment.py --immutable",
                ],
            },
            {
                "stage": "review_persistence_writer_contract",
                "commands": [
                    "python3 -m pytest tests/test_daily_review_persistence_writer.py tests/test_daily_weather_review_report.py",
                    "python3 -m pytest tests/test_daily_review_persistence_batch_plan.py",
                    "python3 -m pytest tests/test_daily_review_persistence_apply.py",
                    "python3 -m pytest tests/test_daily_review_coverage_audit.py",
                    "python3 -m pytest tests/test_daily_review_post_apply_acceptance_audit.py",
                    "python3 scripts/daily_weather_review_report.py --skip-ui-snapshot-audit --skip-replay-audit --limit 1",
                ],
                "requires": "planned review tables exist before production persistence apply; report command remains read-only",
            },
            {
                "stage": "daily_review_batch_plan_before_persistence",
                "commands": [
                    "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 --max-items-output 20",
                ],
                "requires": "operator reviews no-apply batch rows before any daily review persistence apply",
            },
            {
                "stage": "daily_review_coverage_after_persistence",
                "commands": [
                    "python3 scripts/daily_review_coverage_audit.py --since-days 45 --max-items-output 20",
                    "python3 scripts/daily_review_post_apply_acceptance_audit.py --since-days 45 --max-items-output 20",
                ],
                "requires": "daily_weather_reviews populated for settled markets; this is a read-only Phase 8 acceptance probe, not a schema-apply gate",
            },
            {
                "stage": "future_fact_json_contract",
                "commands": [
                    "python3 scripts/phase0_schema_probe.py --immutable --strict",
                    "python3 scripts/hierarchical_probability_unit_contract_audit.py --summary",
                    "python3 scripts/fact_writer_persisted_apply_contract_audit.py --summary",
                    "python3 scripts/canonical_writer_persisted_coverage_audit.py --since-hours 168 --latest-only --limit 50 --max-items-output 20 --summary",
                    "python3 scripts/prior_layer_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward",
                    "python3 scripts/forecast_weak_likelihood_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward",
                    "python3 scripts/metar_strong_likelihood_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward",
                    "python3 scripts/posterior_ssot_fact_coverage_audit.py --since-hours 168 --latest-only --limit 50 --max-items-output 20 --project-forward",
                    "python3 scripts/hierarchical_probability_release_gate.py --since-hours 168 --limit 50 --max-items-output 20",
                ],
                "requires": "future fact writer no-apply projection emits prior_distribution/prior_meta/prior_na_reason, forecast weak source grouping/precision-cap contract, METAR strong state/trajectory/hard-mask contract, source/phase/lineage/timing/review keys and materialized posterior_ssot_fact without historical backfill",
            },
        ],
        "source_plan_next_actions": plan.get("next_actions") or [],
        "ddl": ddl,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="No-apply Phase 0 migration manifest and preflight report.")
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or phase0_schema_probe.DEFAULT_DB))
    parser.add_argument("--immutable", action="store_true", help="Open SQLite in immutable read-only mode.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = build_manifest(Path(args.db), immutable=bool(args.immutable))
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
