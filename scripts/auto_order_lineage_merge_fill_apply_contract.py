#!/usr/bin/env python3
"""Read-only apply contract for auto_orders lineage merge-fill.

The contract consumes auto_order_lineage_merge_fill_plan and projects exact
row-level updates for empty auto_orders.decision_id / signal_id fields. It does
not write archive.db, create backups, fetch external sources, restart services,
or place orders.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import auto_order_lineage_merge_fill_plan as merge_plan  # noqa: E402


DEFAULT_DB = merge_plan.DEFAULT_DB
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "auto_order_lineage_merge_fill_apply_contract_v1"
CONFIRMATION_PHRASE = "APPLY_AUTO_ORDER_LINEAGE_MERGE_FILL"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_auto_orders": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_signal_to_order_timing": False,
        "writes_manifest_file": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "fetches_orderbook": False,
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


def _operation_from_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    set_fields = {
        str(field): value
        for field, value in _as_mapping(candidate.get("set_fields")).items()
        if str(field) in {"decision_id", "signal_id"}
    }
    return {
        "table": "auto_orders",
        "primary_key": _as_mapping(candidate.get("primary_key")),
        "operation": "merge_fill_empty_auto_order_lineage_fields",
        "set_fields": {
            field: {
                "old_value_policy": "must_be_empty",
                "new_value": value,
            }
            for field, value in sorted(set_fields.items())
        },
        "where_guard": {
            "primary_key": _as_mapping(candidate.get("primary_key")),
            "empty_fields_required": sorted(set_fields),
        },
        "source": candidate.get("source"),
        "evidence": list(candidate.get("evidence") or [])[:10],
        "sql_preview": "UPDATE auto_orders SET ... WHERE id matches and target lineage fields are empty",
    }


def build_contract(
    db_path: Path | str = DEFAULT_DB,
    *,
    limit: int = 1000000,
    max_items_output: int = 20,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    resolve_candidates: bool = False,
    enable_decision_order_id_lookup: bool = False,
    max_small_table_scan_rows: int = 5000,
    ignore_legacy_orders: bool = False,
    order_since_hours: float = 0.0,
    ignore_orders_before: str = "",
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    backup_path = backup_root / "archive.db.auto-order-lineage-merge-fill.bak"
    scope_flags = (
        f" --ignore-legacy-orders --ignore-orders-before {shlex.quote(str(ignore_orders_before))}"
        if bool(ignore_legacy_orders) and str(ignore_orders_before or "").strip()
        else f" --ignore-legacy-orders --order-since-hours {float(order_since_hours):g}"
        if bool(ignore_legacy_orders)
        else ""
    )
    resolution_flags = " --resolve-candidates" + (
        " --enable-decision-order-id-lookup" if bool(enable_decision_order_id_lookup) else ""
    )
    joint_scope_flags = (
        f" --ignore-orders-before {shlex.quote(str(ignore_orders_before))}"
        if bool(ignore_legacy_orders) and str(ignore_orders_before or "").strip()
        else f" --order-lineage-since-hours {float(order_since_hours):g}"
        if bool(ignore_legacy_orders)
        else " --include-legacy-order-lineage"
    )
    plan = merge_plan.build_plan(
        db,
        limit=int(limit),
        max_items_output=int(max_items_output),
        max_small_table_scan_rows=int(max_small_table_scan_rows),
        resolve_candidates=bool(resolve_candidates),
        enable_decision_order_id_lookup=bool(enable_decision_order_id_lookup),
        ignore_legacy_orders=bool(ignore_legacy_orders),
        order_since_hours=float(order_since_hours),
        ignore_orders_before=str(ignore_orders_before or ""),
    )
    blockers: list[str] = []
    if plan.get("ok") is not True:
        blockers.append("auto_order_lineage_plan_not_ready")
        blockers.extend(str(item) for item in plan.get("blockers") or [] if str(item or "").strip())
    if plan.get("candidate_resolution_enabled") is not True and plan.get("needs_apply"):
        blockers.append("candidate_resolution_required_before_apply_contract")
    if plan.get("candidate_scope_truncated"):
        blockers.append("candidate_scope_truncated")
    if int(plan.get("blocked_candidate_count") or 0) > 0:
        blockers.append("blocked_candidates_present")
    if plan.get("safe_to_apply_all_candidates") is not True and plan.get("needs_apply"):
        blockers.append("not_all_missing_orders_have_safe_candidates")

    candidates = [_as_mapping(item) for item in _as_sequence(plan.get("candidate_operations"))]
    safe_candidates = [
        candidate
        for candidate in candidates
        if not [item for item in candidate.get("blockers") or [] if str(item or "").strip()]
        and _as_mapping(candidate.get("set_fields"))
    ]
    operations = [_operation_from_candidate(candidate) for candidate in safe_candidates]
    ok = bool(not blockers)
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "auto_order_lineage_merge_fill_apply_contract_no_apply",
        "db": str(db),
        "plan_summary": merge_plan.compact_plan(plan, max_items_output=int(max_items_output)),
        "operation_count": len(operations),
        "operations": operations,
        "operations_truncated": False,
        "candidate_count": int(plan.get("candidate_count") or 0),
        "safe_candidate_count": int(plan.get("safe_candidate_count") or 0),
        "blocked_candidate_count": int(plan.get("blocked_candidate_count") or 0),
        "missing_order_count": int(plan.get("missing_order_count") or 0),
        "blockers": sorted(set(blockers)),
        "operator_contract": {
            "schema": "auto_order_lineage_merge_fill_apply_operator_contract_v1",
            "confirmation_phrase": CONFIRMATION_PHRASE,
            "requires_backup": True,
            "requires_writer_quiescence": True,
            "requires_resolved_candidates": True,
            "requires_strong_lineage_sources": True,
            "allow_apply_now": False,
            "backup_path": str(backup_path),
            "backup_command": f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup_path)}\"",
            "verify_writer_quiescence_command": (
                f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} "
                "--summary --max-items-output 8"
            ),
            "apply_command_template": (
                "python3 scripts/auto_order_lineage_merge_fill_apply.py --apply "
                f"--confirm {CONFIRMATION_PHRASE} --writer-quiescent "
                f"--backup-path {_q(backup_path)}{resolution_flags}{scope_flags}"
            ),
            "post_apply_verification_commands": [
                f"python3 scripts/order_lineage_coverage_audit.py --summary{scope_flags} --max-items-output 8",
                "python3 scripts/hierarchical_joint_plan_coverage_audit.py "
                f"--summary --limit 1000000 --max-items-output 8{joint_scope_flags}",
            ],
        },
        "guards": _guards(),
        "next_boundary": (
            "run_controlled_auto_order_lineage_merge_fill_apply_after_backup_writer_quiescence_and_confirmation"
            if ok and operations
            else "auto_order_lineage_merge_fill_not_required"
            if ok
            else "resolve_auto_order_lineage_apply_contract_blockers_before_apply"
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
        "operation_count": int(contract.get("operation_count") or 0),
        "operations": operations[:visible_limit],
        "operations_truncated": len(operations) > visible_limit,
        "candidate_count": int(contract.get("candidate_count") or 0),
        "safe_candidate_count": int(contract.get("safe_candidate_count") or 0),
        "blocked_candidate_count": int(contract.get("blocked_candidate_count") or 0),
        "missing_order_count": int(contract.get("missing_order_count") or 0),
        "plan_summary": dict(_as_mapping(contract.get("plan_summary"))),
        "blockers": list(contract.get("blockers") or [])[:50],
        "operator_contract": dict(_as_mapping(contract.get("operator_contract"))),
        "guards": dict(_as_mapping(contract.get("guards"))),
        "next_boundary": contract.get("next_boundary"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--max-small-table-scan-rows", type=int, default=5000)
    parser.add_argument("--resolve-candidates", action="store_true")
    parser.add_argument("--enable-decision-order-id-lookup", action="store_true")
    parser.add_argument("--ignore-legacy-orders", action="store_true")
    parser.add_argument("--order-since-hours", type=float, default=0.0)
    parser.add_argument("--ignore-orders-before", default="")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    contract = build_contract(
        args.db,
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
        max_small_table_scan_rows=int(args.max_small_table_scan_rows),
        resolve_candidates=bool(args.resolve_candidates),
        enable_decision_order_id_lookup=bool(args.enable_decision_order_id_lookup),
        ignore_legacy_orders=bool(args.ignore_legacy_orders),
        order_since_hours=float(args.order_since_hours),
        ignore_orders_before=str(args.ignore_orders_before or ""),
    )
    payload: Mapping[str, Any] = (
        compact_contract(contract, max_items_output=int(args.max_items_output)) if args.summary else contract
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if contract.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
