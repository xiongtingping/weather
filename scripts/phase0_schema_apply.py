#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import phase0_forward_schema_plan
import phase0_schema_probe


SCHEMA_VERSION = "phase0_schema_apply_contract_v1"
CONFIRMATION_PHRASE = "APPLY_PHASE0_FORWARD_SCHEMA"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _file_state(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"path": "", "exists": False, "size_bytes": None}
    resolved = Path(path).expanduser().resolve()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "size_bytes": resolved.stat().st_size if resolved.exists() else None,
    }


def _ddl_summary(ddl: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "ddl_count": len(ddl),
        "add_column_count": sum(1 for item in ddl if item.get("action") == "add_column"),
        "create_table_count": sum(1 for item in ddl if item.get("action") == "create_table"),
        "create_unique_index_count": sum(1 for item in ddl if item.get("action") == "create_unique_index"),
        "requires_backup_count": sum(1 for item in ddl if item.get("requires_backup")),
        "affected_tables": sorted({str(item.get("table")) for item in ddl if item.get("table")}),
    }


def _preflight_checks(
    *,
    db: Path,
    ddl: list[Mapping[str, Any]],
    backup_path: Path | None,
    allow_apply: bool,
    confirmation_phrase: str,
    writer_quiescent: bool,
    immutable: bool,
) -> list[dict[str, Any]]:
    backup_state = _file_state(backup_path)
    backup_required = any(bool(item.get("requires_backup")) for item in ddl)
    backup_ok = (
        not backup_required
        or (
            bool(backup_state.get("exists"))
            and int(backup_state.get("size_bytes") or 0) > 0
            and str(backup_state.get("path") or "") != str(db)
        )
    )
    return [
        {
            "name": "apply_flag",
            "status": "pass" if allow_apply else "required",
            "hard_blocker": bool(ddl) and not allow_apply,
            "reason": "allow_apply_true" if allow_apply else "schema_apply_requires_explicit_apply_flag",
        },
        {
            "name": "confirmation_phrase",
            "status": "pass" if confirmation_phrase == CONFIRMATION_PHRASE else "required",
            "hard_blocker": bool(ddl) and confirmation_phrase != CONFIRMATION_PHRASE,
            "reason": "confirmation_phrase_matches" if confirmation_phrase == CONFIRMATION_PHRASE else "confirmation_phrase_missing_or_mismatched",
        },
        {
            "name": "fresh_archive_db_backup",
            "status": "pass" if backup_ok else "required",
            "hard_blocker": bool(ddl) and not backup_ok,
            "reason": "backup_path_exists_and_is_not_db" if backup_ok else "backup_path_required_before_schema_apply",
            "backup_path": backup_state.get("path"),
            "backup_exists": backup_state.get("exists"),
            "backup_size_bytes": backup_state.get("size_bytes"),
        },
        {
            "name": "writer_quiescence",
            "status": "pass" if writer_quiescent else "required",
            "hard_blocker": bool(ddl) and not writer_quiescent,
            "reason": "caller_asserted_writer_quiescence" if writer_quiescent else "requires_explicit_writer_quiescent_assertion",
        },
        {
            "name": "immutable_mode",
            "status": "pass" if not immutable else "blocked",
            "hard_blocker": bool(ddl) and immutable,
            "reason": "write_connection_allowed" if not immutable else "immutable_read_only_connection_cannot_apply_schema",
        },
        {
            "name": "historical_backfill",
            "status": "blocked_by_design",
            "hard_blocker": False,
            "reason": "schema_apply_does_not_backfill_history_or_fact_json",
        },
        {
            "name": "launchd_restart",
            "status": "blocked_by_design",
            "hard_blocker": False,
            "reason": "schema_apply_does_not_restart_daemons",
        },
    ]


def build_apply_contract(
    db_path: Path,
    *,
    immutable: bool = False,
    backup_path: Path | None = None,
    allow_apply: bool = False,
    confirmation_phrase: str = "",
    writer_quiescent: bool = False,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backup = Path(backup_path).expanduser().resolve() if backup_path else None
    plan = phase0_forward_schema_plan.build_plan(db, immutable=immutable)
    ddl = list(plan.get("ddl") or [])
    preflight_checks = _preflight_checks(
        db=db,
        ddl=ddl,
        backup_path=backup,
        allow_apply=allow_apply,
        confirmation_phrase=confirmation_phrase,
        writer_quiescent=writer_quiescent,
        immutable=immutable,
    )
    blockers = [str(item.get("name")) for item in preflight_checks if item.get("hard_blocker")]
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(plan.get("ok", True)),
        "generated_at": _now_utc(),
        "read_only": True,
        "dry_run": True,
        "execution_mode": "controlled_apply_contract_no_apply",
        "apply_supported": True,
        "applied": False,
        "db": str(db),
        "source_plan_schema": plan.get("schema"),
        "confirmation_phrase_required": CONFIRMATION_PHRASE,
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "creates_backup": False,
            "historical_backfill": False,
            "restarts_launchd": False,
            "places_orders": False,
            "changes_parameters": False,
        },
        "ddl_summary": _ddl_summary(ddl),
        "preflight_checks": preflight_checks,
        "apply_blockers": blockers,
        "post_apply_verification_commands": [
            "python3 scripts/phase0_schema_probe.py --immutable --strict --schema-only",
            "python3 scripts/check_ssot_alignment.py --immutable",
            "python3 -m pytest tests/test_daily_review_persistence_writer.py tests/test_daily_review_persistence_apply.py tests/test_daily_weather_review_report.py tests/test_daily_review_persistence_batch_plan.py tests/test_daily_review_coverage_audit.py tests/test_daily_review_post_apply_acceptance_audit.py",
        ],
        "post_forward_write_verification_commands": [
            "python3 scripts/phase0_schema_probe.py --immutable --strict",
        ],
        "boundary": {
            "schema_apply_only": True,
            "fact_json_backfill_included": False,
            "launchd_restart_included": False,
            "orders_or_trade_decisions_included": False,
        },
        "ddl": ddl,
    }


def apply_schema_contract(
    db_path: Path,
    *,
    allow_apply: bool = False,
    confirmation_phrase: str = "",
    backup_path: Path | None = None,
    writer_quiescent: bool = False,
    immutable: bool = False,
) -> dict[str, Any]:
    contract = build_apply_contract(
        db_path,
        immutable=immutable,
        backup_path=backup_path,
        allow_apply=allow_apply,
        confirmation_phrase=confirmation_phrase,
        writer_quiescent=writer_quiescent,
    )
    ddl = list(contract.get("ddl") or [])
    if not ddl:
        contract["execution_mode"] = "controlled_apply_noop_schema_already_ready"
        contract["apply_result"] = {"ok": True, "applied": False, "reason": "no_ddl_to_apply"}
        return contract
    if contract.get("apply_blockers"):
        contract["execution_mode"] = "controlled_apply_blocked"
        contract["apply_result"] = {
            "ok": False,
            "applied": False,
            "blockers": list(contract.get("apply_blockers") or []),
        }
        return contract

    db = Path(db_path).expanduser().resolve()
    applied: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for item in ddl:
                sql = str(item.get("sql") or "").strip()
                if not sql:
                    raise ValueError(f"missing sql for ddl item: {item}")
                conn.execute(sql)
                applied.append(
                    {
                        "action": item.get("action"),
                        "table": item.get("table"),
                        "column": item.get("column"),
                    }
                )
            conn.commit()
    except Exception as exc:
        contract["execution_mode"] = "controlled_apply_failed"
        contract["guards"] = {
            **dict(contract.get("guards") or {}),
            "executes_sql": bool(applied),
            "writes_archive_db": bool(applied),
        }
        contract["apply_result"] = {
            "ok": False,
            "applied": bool(applied),
            "applied_actions": applied,
            "error": f"{exc.__class__.__name__}: {exc}",
        }
        return contract

    checkpoint_result: list[Any] = []
    try:
        with sqlite3.connect(db, timeout=30) as conn:
            checkpoint_result = [list(row) for row in conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()]
    except Exception as exc:
        checkpoint_result = [[f"{exc.__class__.__name__}: {exc}"]]

    post_probe = phase0_schema_probe.run_probe(db, immutable=True)
    contract["read_only"] = False
    contract["dry_run"] = False
    contract["execution_mode"] = "controlled_apply_completed"
    contract["applied"] = True
    contract["guards"] = {
        **dict(contract.get("guards") or {}),
        "executes_sql": True,
        "writes_archive_db": True,
        "creates_backup": False,
        "historical_backfill": False,
        "restarts_launchd": False,
        "places_orders": False,
        "changes_parameters": False,
    }
    contract["apply_result"] = {
        "ok": True,
        "applied": True,
        "applied_actions": applied,
        "wal_checkpoint_truncate": checkpoint_result,
        "schema_surface_ready": bool(post_probe.get("schema_surface_ready")),
        "phase0_ready": bool(post_probe.get("phase0_ready")),
        "fact_json_missing_count": len(post_probe.get("fact_json_missing") or []),
        "remaining_next_actions": post_probe.get("next_actions") or [],
    }
    contract["post_apply_probe"] = {
        "schema": post_probe.get("schema"),
        "ok": post_probe.get("ok"),
        "schema_surface_ready": post_probe.get("schema_surface_ready"),
        "phase0_ready": post_probe.get("phase0_ready"),
        "schema_missing_count": len(post_probe.get("schema_missing") or []),
        "fact_json_missing_count": len(post_probe.get("fact_json_missing") or []),
    }
    contract["ddl"] = []
    return contract


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Controlled Phase 0 schema apply contract. Defaults to no-apply.")
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or phase0_schema_probe.DEFAULT_DB))
    parser.add_argument("--immutable", action="store_true", help="Build contract with immutable read-only probe; incompatible with --apply.")
    parser.add_argument("--apply", action="store_true", help="Execute DDL only when all apply preconditions pass.")
    parser.add_argument("--confirm", default="", help=f"Required exact phrase for --apply: {CONFIRMATION_PHRASE}")
    parser.add_argument("--backup", default="", help="Existing fresh archive.db backup path required when DDL adds columns.")
    parser.add_argument("--writer-quiescent", action="store_true", help="Caller assertion that writers are stopped/quiescent.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    backup_path = Path(args.backup) if str(args.backup or "").strip() else None
    if args.apply:
        result = apply_schema_contract(
            Path(args.db),
            allow_apply=True,
            confirmation_phrase=str(args.confirm or ""),
            backup_path=backup_path,
            writer_quiescent=bool(args.writer_quiescent),
            immutable=bool(args.immutable),
        )
    else:
        result = build_apply_contract(
            Path(args.db),
            immutable=bool(args.immutable),
            backup_path=backup_path,
            allow_apply=False,
            confirmation_phrase=str(args.confirm or ""),
            writer_quiescent=bool(args.writer_quiescent),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if args.apply and not result.get("applied"):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
