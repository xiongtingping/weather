#!/usr/bin/env python3
"""Read-only apply contract for production identity merge-fill.

The contract consumes the merge-fill plan and projects exact row-level updates
for empty identity fields. It does not write archive.db, create backups, write
manifests, fetch sources, restart services, or place orders.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import production_identity_merge_fill_plan as merge_plan  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "production_identity_merge_fill_apply_contract_v1"
CONFIRMATION_PHRASE = merge_plan.CONFIRMATION_PHRASE


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_auto_orders": False,
        "writes_source_observations": False,
        "writes_manifest_file": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


def _coverage_report_has_no_identity_gaps(coverage_report: Mapping[str, Any] | None) -> bool:
    if not isinstance(coverage_report, Mapping):
        return False
    if coverage_report.get("ok") is not True:
        return False
    if list(coverage_report.get("gap_tables") or []):
        return False
    tables = coverage_report.get("tables")
    if isinstance(tables, Mapping):
        for item in tables.values():
            if not isinstance(item, Mapping):
                continue
            counts = item.get("empty_required_field_counts")
            if not isinstance(counts, Mapping):
                continue
            for value in counts.values():
                try:
                    if int(value or 0) > 0:
                        return False
                except Exception:
                    return False
    return True


def _load_json_file(path: str | os.PathLike[str] | None) -> dict[str, Any] | None:
    if not path:
        return None
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _operation_from_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    table = str(candidate.get("table") or "")
    primary_key = _as_mapping(candidate.get("primary_key"))
    set_fields = _as_mapping(candidate.get("set_fields"))
    return {
        "table": table,
        "primary_key": primary_key,
        "operation": "merge_fill_empty_identity_fields",
        "set_fields": {
            str(field): {
                "old_value_policy": "must_be_empty",
                "new_value": value,
            }
            for field, value in sorted(set_fields.items())
        },
        "where_guard": {
            "primary_key": primary_key,
            "empty_fields_required": sorted(str(field) for field in set_fields),
        },
        "source": candidate.get("source"),
        "sql_preview": f"UPDATE {table} SET ... WHERE primary key matches and target identity fields are empty",
    }


def build_contract(
    db_path: Path | str = DEFAULT_DB,
    *,
    duplicate_decision_manifest: Mapping[str, Any] | None = None,
    limit: int = 1000000,
    max_items_output: int = 20,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    include_coverage: bool = True,
    immutable: bool = False,
    coverage_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    backup_path = backup_root / "archive.db.production-identity-merge-fill.bak"
    explicit_coverage_report = isinstance(coverage_report, Mapping)
    effective_coverage_report: Mapping[str, Any] | None = coverage_report
    if effective_coverage_report is None and include_coverage:
        effective_coverage_report = merge_plan.production_identity_coverage_audit.audit(
            db,
            immutable=bool(immutable),
            sample_limit=max(1, int(max_items_output)),
        )
    if (
        explicit_coverage_report
        and isinstance(effective_coverage_report, Mapping)
        and effective_coverage_report.get("ok") is not True
    ):
        coverage_blockers = [
            str(item)
            for item in _as_sequence(effective_coverage_report.get("blockers"))
            if str(item or "").strip()
        ]
        duplicate_precondition = {
            "schema": "production_identity_duplicate_fact_snapshot_packet_v1",
            "ok": False,
            "duplicate_group_count": 0,
            "duplicate_row_count": 0,
            "hard_blockers": ["production_identity_coverage_not_ready"],
            "short_circuited": True,
            "short_circuit_reason": "production_identity_coverage_not_ready",
        }
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "production_identity_merge_fill_apply_contract_no_apply",
            "db": str(db),
            "read_mode": "coverage_report_dependency_blocked",
            "plan_summary": {
                "schema": merge_plan.SCHEMA_VERSION + "_summary",
                "source_schema": merge_plan.SCHEMA_VERSION,
                "ok": False,
                "read_only": True,
                "dry_run": True,
                "db": str(db),
                "coverage_ok_before_apply": False,
                "coverage_gap_tables_before_apply": list(effective_coverage_report.get("gap_tables") or []),
                "needs_apply": False,
                "candidate_count": 0,
                "safe_candidate_count": 0,
                "blocked_candidate_count": 0,
                "candidate_scope_truncated": False,
                "short_circuited": True,
                "short_circuit_reason": "production_identity_coverage_not_ready",
                "duplicate_fact_snapshot_precondition": duplicate_precondition,
                "blockers": ["production_identity_coverage_not_ready", *coverage_blockers],
                "guards": merge_plan._guards(),
                "next_boundary": "resolve_production_identity_coverage_before_merge_fill_contract",
            },
            "duplicate_fact_snapshot_precondition": duplicate_precondition,
            "operation_count": 0,
            "operations": [],
            "operations_truncated": False,
            "blocked_candidate_count": 0,
            "safe_candidate_count": 0,
            "candidate_count": 0,
            "blockers": sorted(set(["production_identity_coverage_not_ready", *coverage_blockers])),
            "operator_contract": {
                "schema": "production_identity_merge_fill_apply_operator_contract_v1",
                "confirmation_phrase": CONFIRMATION_PHRASE,
                "requires_backup": True,
                "requires_writer_quiescence": True,
                "requires_duplicate_fact_snapshot_precondition": True,
                "allow_apply_now": False,
                "blocked_until": "production_identity_coverage_ready",
                "backup_path": str(backup_path),
                "backup_command": f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup_path)}\"",
                "verify_writer_quiescence_command": (
                    f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} "
                    "--summary --max-items-output 8"
                ),
                "post_apply_verification_commands": [
                    "python3 scripts/production_identity_coverage_audit.py --summary --max-items-output 8",
                    "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary --limit 1000000 --max-items-output 8",
                ],
            },
            "guards": _guards(),
            "next_boundary": "resolve_production_identity_coverage_before_merge_fill_contract",
        }
    if _coverage_report_has_no_identity_gaps(effective_coverage_report):
        duplicate_precondition = {
            "schema": "production_identity_duplicate_fact_snapshot_packet_v1",
            "ok": True,
            "duplicate_group_count": 0,
            "duplicate_row_count": 0,
            "hard_blockers": [],
            "short_circuited": True,
            "short_circuit_reason": "production_identity_coverage_ok_no_merge_fill_candidates",
        }
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "production_identity_merge_fill_apply_contract_no_apply",
            "db": str(db),
            "read_mode": "coverage_report_short_circuit",
            "plan_summary": {
                "schema": merge_plan.SCHEMA_VERSION + "_summary",
                "source_schema": merge_plan.SCHEMA_VERSION,
                "ok": True,
                "read_only": True,
                "dry_run": True,
                "db": str(db),
                "coverage_ok_before_apply": True,
                "coverage_gap_tables_before_apply": [],
                "needs_apply": False,
                "candidate_count": 0,
                "safe_candidate_count": 0,
                "blocked_candidate_count": 0,
                "candidate_scope_truncated": False,
                "short_circuited": True,
                "short_circuit_reason": "production_identity_coverage_ok_no_merge_fill_candidates",
                "duplicate_fact_snapshot_precondition": duplicate_precondition,
                "blockers": [],
                "guards": merge_plan._guards(),
                "next_boundary": "production_identity_merge_fill_not_required",
            },
            "duplicate_fact_snapshot_precondition": duplicate_precondition,
            "operation_count": 0,
            "operations": [],
            "operations_truncated": False,
            "blocked_candidate_count": 0,
            "safe_candidate_count": 0,
            "candidate_count": 0,
            "blockers": [],
            "operator_contract": {
                "schema": "production_identity_merge_fill_apply_operator_contract_v1",
                "confirmation_phrase": CONFIRMATION_PHRASE,
                "requires_backup": True,
                "requires_writer_quiescence": True,
                "requires_duplicate_fact_snapshot_precondition": True,
                "allow_apply_now": False,
                "backup_path": str(backup_path),
                "backup_command": f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup_path)}\"",
                "verify_writer_quiescence_command": (
                    f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} "
                    "--summary --max-items-output 8"
                ),
                "post_apply_verification_commands": [
                    "python3 scripts/production_identity_coverage_audit.py --summary --max-items-output 8",
                    "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary --limit 1000000 --max-items-output 8",
                ],
            },
            "guards": _guards(),
            "next_boundary": "production_identity_merge_fill_not_required",
        }
    plan = merge_plan.build_plan(
        db,
        immutable=bool(immutable),
        limit=int(limit),
        max_items_output=int(max_items_output),
        duplicate_decision_manifest=duplicate_decision_manifest,
        include_coverage=bool(include_coverage),
        coverage_report=effective_coverage_report,
    )
    blockers: list[str] = []
    if plan.get("ok") is not True:
        blockers.append("merge_fill_plan_not_ready")
        blockers.extend(str(item) for item in plan.get("blockers") or [] if str(item or "").strip())
    if plan.get("candidate_scope_truncated"):
        blockers.append("candidate_scope_truncated")
    if plan.get("blocked_candidate_count"):
        blockers.append("blocked_candidates_present")
    duplicate_precondition = _as_mapping(plan.get("duplicate_fact_snapshot_precondition"))
    if duplicate_precondition.get("ok") is not True:
        blockers.append("duplicate_fact_snapshot_precondition_not_ready")

    candidates = [_as_mapping(item) for item in _as_sequence(plan.get("candidate_operations"))]
    safe_candidates = [
        candidate
        for candidate in candidates
        if not [item for item in candidate.get("blockers") or [] if str(item or "").strip()]
        and _as_mapping(candidate.get("set_fields"))
    ]
    operations = [_operation_from_candidate(candidate) for candidate in safe_candidates]

    ok = bool(not blockers)
    visible_limit = max(0, int(max_items_output))
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "production_identity_merge_fill_apply_contract_no_apply",
        "db": str(db),
        "read_mode": "immutable_snapshot" if immutable else "wal_safe_read_only",
        "plan_summary": merge_plan.compact_plan(plan, max_items_output=visible_limit),
        "duplicate_fact_snapshot_precondition": duplicate_precondition,
        "operation_count": len(operations),
        "operations": operations,
        "operations_truncated": False,
        "blocked_candidate_count": int(plan.get("blocked_candidate_count") or 0),
        "safe_candidate_count": int(plan.get("safe_candidate_count") or 0),
        "candidate_count": int(plan.get("candidate_count") or 0),
        "blockers": sorted(set(blockers)),
        "operator_contract": {
            "schema": "production_identity_merge_fill_apply_operator_contract_v1",
            "confirmation_phrase": CONFIRMATION_PHRASE,
            "requires_backup": True,
            "requires_writer_quiescence": True,
            "requires_duplicate_fact_snapshot_precondition": True,
            "allow_apply_now": False,
            "backup_path": str(backup_path),
            "backup_command": f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup_path)}\"",
            "verify_writer_quiescence_command": (
                f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} "
                "--summary --max-items-output 8"
            ),
            "post_apply_verification_commands": [
                "python3 scripts/production_identity_coverage_audit.py --summary --max-items-output 8",
                "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary --limit 1000000 --max-items-output 8",
            ],
        },
        "guards": _guards(),
        "next_boundary": (
            "run_controlled_identity_merge_fill_apply_after_backup_writer_quiescence_and_confirmation"
            if ok and operations
            else "production_identity_merge_fill_not_required"
            if ok
            else "resolve_identity_merge_fill_contract_blockers_before_apply"
        ),
    }


def compact_contract(contract: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    operations = list(contract.get("operations") or [])
    visible_limit = max(0, int(max_items_output))
    return {
        "schema": str(contract.get("schema") or SCHEMA_VERSION) + "_summary",
        "source_schema": contract.get("schema"),
        "ok": bool(contract.get("ok")),
        "read_only": bool(contract.get("read_only")),
        "dry_run": bool(contract.get("dry_run")),
        "db": contract.get("db"),
        "read_mode": contract.get("read_mode"),
        "operation_count": int(contract.get("operation_count") or 0),
        "operations": operations[:visible_limit],
        "operations_truncated": len(operations) > visible_limit,
        "candidate_count": int(contract.get("candidate_count") or 0),
        "safe_candidate_count": int(contract.get("safe_candidate_count") or 0),
        "blocked_candidate_count": int(contract.get("blocked_candidate_count") or 0),
        "duplicate_fact_snapshot_precondition": dict(_as_mapping(contract.get("duplicate_fact_snapshot_precondition"))),
        "blockers": list(contract.get("blockers") or [])[:50],
        "operator_contract": dict(_as_mapping(contract.get("operator_contract"))),
        "guards": dict(_as_mapping(contract.get("guards"))),
        "next_boundary": contract.get("next_boundary"),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or DEFAULT_DB))
    parser.add_argument("--duplicate-decision-manifest", default="")
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument(
        "--immutable",
        action="store_true",
        help="Use SQLite immutable=1 only for offline, quiescent DB snapshots; live archive.db should use the default WAL-safe read-only mode.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    contract = build_contract(
        args.db,
        duplicate_decision_manifest=_load_json_file(args.duplicate_decision_manifest),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
        immutable=bool(args.immutable),
    )
    payload: Mapping[str, Any] = (
        compact_contract(contract, max_items_output=int(args.max_items_output))
        if args.summary
        else contract
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if contract.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
