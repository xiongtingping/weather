#!/usr/bin/env python3
"""Read-only apply contract for production identity audit support indexes.

This contract exists only to make production identity coverage auditable
without hidden large-table scans. It does not write archive.db, create backups,
restart services, fetch sources, backfill history, or place orders.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "production_identity_index_apply_contract_v1"
CONFIRMATION_PHRASE = "APPLY_PRODUCTION_IDENTITY_AUDIT_INDEXES"

REQUIRED_INDEXES: tuple[dict[str, str], ...] = (
    {
        "table": "auto_trade_decisions",
        "column": "correlation_id",
        "index_name": "idx_phase0_auto_trade_decisions_correlation_id_nonempty_scan",
        "reason": "prove_auto_trade_decisions_correlation_id_nonempty_without_large_table_scan",
    },
    {
        "table": "auto_trade_jobs",
        "column": "correlation_id",
        "index_name": "idx_phase0_auto_trade_jobs_correlation_id_nonempty_scan",
        "reason": "prove_auto_trade_jobs_correlation_id_nonempty_without_payload_json_scan",
    },
)


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "creates_backup": False,
        "executes_sql_writes": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _connect(db: Path, *, immutable: bool) -> sqlite3.Connection:
    uri = f"file:{db}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"] or "") for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")}


def _index_sql(conn: sqlite3.Connection, index_name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return str(row["sql"] or "") if row else ""


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


def _ddl_for_index(table: str, column: str, index_name: str) -> str:
    table_q = _quote_identifier(table)
    column_q = _quote_identifier(column)
    index_q = _quote_identifier(index_name)
    return (
        f"CREATE INDEX IF NOT EXISTS {index_q} ON {table_q} ({column_q}) "
        f"WHERE {column_q} IS NOT NULL AND {column_q} <> ''"
    )


def _backup_dir_state(path: Path | str) -> dict[str, Any]:
    raw = Path(path).expanduser()
    resolved = raw.resolve(strict=False)
    is_symlink = raw.is_symlink()
    symlink_target = ""
    if is_symlink:
        try:
            symlink_target = os.readlink(raw)
        except OSError:
            symlink_target = ""
    return {
        "raw_path": str(raw),
        "resolved_path": str(resolved),
        "exists": raw.exists(),
        "is_symlink": is_symlink,
        "symlink_target": symlink_target,
        "symlink_target_exists": resolved.exists() if is_symlink else None,
        "parent_exists": resolved.parent.exists(),
    }


def _required_index_status(conn: sqlite3.Connection, spec: Mapping[str, str]) -> dict[str, Any]:
    table = str(spec.get("table") or "")
    column = str(spec.get("column") or "")
    index_name = str(spec.get("index_name") or "")
    table_exists = _table_exists(conn, table)
    columns = _columns(conn, table) if table_exists else set()
    column_exists = column in columns
    exists = _index_exists(conn, index_name)
    ddl = _ddl_for_index(table, column, index_name)
    return {
        "table": table,
        "column": column,
        "index_name": index_name,
        "reason": str(spec.get("reason") or ""),
        "table_exists": table_exists,
        "column_exists": column_exists,
        "index_exists": exists,
        "existing_sql": _index_sql(conn, index_name) if exists else "",
        "ddl": ddl,
        "needs_apply": bool(table_exists and column_exists and not exists),
        "blocked": bool(not table_exists or not column_exists),
        "blockers": [
            item
            for item, present in (
                ("table_missing", table_exists),
                ("column_missing", column_exists),
            )
            if not present
        ],
    }


def build_contract(
    db_path: Path | str = DEFAULT_DB,
    *,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    immutable: bool = False,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backup_state = _backup_dir_state(backup_dir)
    backup_root = Path(str(backup_state["resolved_path"]))
    backup_path = backup_root / "archive.db.production-identity-audit-indexes.bak"
    blockers: list[str] = []
    index_statuses: list[dict[str, Any]] = []
    if not db.exists():
        blockers.append("archive_db_missing")
    else:
        with _connect(db, immutable=bool(immutable)) as conn:
            index_statuses = [_required_index_status(conn, spec) for spec in REQUIRED_INDEXES]
    for item in index_statuses:
        blockers.extend(str(blocker) for blocker in item.get("blockers") or [])
    needs_apply = any(bool(item.get("needs_apply")) for item in index_statuses)
    ddl = [str(item.get("ddl") or "") for item in index_statuses if item.get("needs_apply")]
    apply_blockers = (
        ["apply_flag", "confirmation_phrase", "fresh_archive_db_backup", "writer_quiescence"]
        if needs_apply
        else []
    )
    backup_command_blocked_reason = ""
    if needs_apply and backup_state.get("is_symlink") and not backup_state.get("symlink_target_exists"):
        apply_blockers.append("backup_dir_symlink_target_missing")
        backup_command_blocked_reason = "backup_dir_symlink_target_missing"
    backup_command = (
        ""
        if backup_command_blocked_reason
        else f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup_path)}\""
    )
    ok = bool(not blockers and not needs_apply)
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "production_identity_index_apply_contract_no_apply",
        "db": str(db),
        "required_indexes": index_statuses,
        "needs_apply": needs_apply,
        "ddl": ddl,
        "ddl_count": len(ddl),
        "blockers": sorted(set(blockers)),
        "apply_blockers": apply_blockers,
        "operator_contract": {
            "schema": "production_identity_index_apply_operator_contract_v1",
            "confirmation_phrase": CONFIRMATION_PHRASE,
            "requires_backup": bool(needs_apply),
            "requires_writer_quiescence": bool(needs_apply),
            "allow_apply_now": False,
            "backup_dir_state": backup_state,
            "backup_path": str(backup_path),
            "backup_command": backup_command,
            "backup_command_blocked_reason": backup_command_blocked_reason,
            "verify_writer_quiescence_command": (
                f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} "
                "--summary --max-items-output 8"
            ),
            "apply_command_template": (
                f"sqlite3 {_q(db)} " + _q("; ".join(ddl))
                if ddl
                else ""
            ),
            "post_apply_verification_commands": [
                "python3 scripts/production_identity_coverage_audit.py --summary --max-items-output 8",
                "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary --limit 1000000 --max-items-output 8 --trace-stages",
            ],
        },
        "guards": _guards(),
        "next_boundary": (
            "production_identity_audit_indexes_ready"
            if ok
            else "controlled_apply_required_for_production_identity_audit_indexes"
            if needs_apply
            else "fix_production_identity_index_contract_blockers"
        ),
    }


def compact_contract(contract: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    indexes = list(contract.get("required_indexes") or [])
    visible_limit = max(0, int(max_items_output))
    return {
        "schema": str(contract.get("schema") or SCHEMA_VERSION) + "_summary",
        "source_schema": str(contract.get("schema") or ""),
        "ok": bool(contract.get("ok")),
        "read_only": bool(contract.get("read_only")),
        "dry_run": bool(contract.get("dry_run")),
        "db": str(contract.get("db") or ""),
        "needs_apply": bool(contract.get("needs_apply")),
        "ddl_count": int(contract.get("ddl_count") or 0),
        "required_indexes": indexes[:visible_limit],
        "required_indexes_truncated": len(indexes) > visible_limit,
        "blockers": list(contract.get("blockers") or [])[:50],
        "apply_blockers": list(contract.get("apply_blockers") or [])[:50],
        "operator_contract": dict(contract.get("operator_contract") or {}),
        "guards": dict(contract.get("guards") or {}),
        "next_boundary": str(contract.get("next_boundary") or ""),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or DEFAULT_DB))
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--immutable", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--max-items-output", type=int, default=8)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    contract = build_contract(
        args.db,
        backup_dir=args.backup_dir,
        immutable=bool(args.immutable),
    )
    payload: Mapping[str, Any] = (
        compact_contract(contract, max_items_output=int(args.max_items_output))
        if args.summary
        else contract
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if contract.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
