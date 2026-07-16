#!/usr/bin/env python3
"""Controlled apply for production identity audit support indexes.

Default mode is no-apply and prints the read-only index contract. The apply
path only creates the explicitly planned audit-support indexes after explicit
confirmation, an existing backup, and writer-quiescence verification.
"""

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

from scripts import controlled_apply_maintenance_window_packet as maintenance_window  # noqa: E402
from scripts import production_identity_index_apply_contract as index_contract  # noqa: E402


DEFAULT_DB = index_contract.DEFAULT_DB
CONFIRMATION_PHRASE = index_contract.CONFIRMATION_PHRASE
RESULT_SCHEMA = "production_identity_index_apply_result_v1"


def _guards(*, writes: bool) -> dict[str, bool]:
    return {
        "writes_archive_db": bool(writes),
        "creates_backup": False,
        "executes_sql_writes": bool(writes),
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _blocked(reason: str, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "applied": False,
        "blocked": True,
        "block_reason": reason,
        "guards": _guards(writes=False),
    }
    out.update(dict(extra or {}))
    return out


def _verify_existing_backup(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"ok": False, "reason": "backup_path_required", "path": ""}
    backup = Path(path).expanduser().resolve()
    if not backup.exists():
        return {"ok": False, "reason": "backup_path_not_found", "path": str(backup)}
    size = int(backup.stat().st_size) if backup.exists() else 0
    if size <= 0:
        return {"ok": False, "reason": "backup_path_empty", "path": str(backup), "size_bytes": size}
    return {"ok": True, "reason": "existing_backup_verified", "path": str(backup), "size_bytes": size}


def _maintenance_summary(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "writer_quiescent": bool(packet.get("writer_quiescent")),
        "required_stop_labels": list(packet.get("required_stop_labels") or []),
        "blockers": list(packet.get("blockers") or [])[:20],
        "counts": dict(_as_mapping(packet.get("counts"))),
        "next_boundary": packet.get("next_boundary"),
    }


def apply_index_contract(db: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    ddl = [str(item or "").strip() for item in contract.get("ddl") or [] if str(item or "").strip()]
    if not ddl:
        return {
            "schema": RESULT_SCHEMA,
            "ok": True,
            "applied": False,
            "blocked": False,
            "ddl_count": 0,
            "applied_ddl_count": 0,
            "guards": _guards(writes=False),
        }
    try:
        with sqlite3.connect(str(db), timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            for statement in ddl:
                conn.execute(statement)
            conn.commit()
    except sqlite3.Error as exc:
        return _blocked(
            "sqlite_apply_error",
            extra={"sqlite_error": str(exc), "ddl_count": len(ddl)},
        )
    return {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "applied": True,
        "blocked": False,
        "ddl_count": len(ddl),
        "applied_ddl_count": len(ddl),
        "guards": _guards(writes=True),
    }


def build_cli_result(
    *,
    db_path: Path,
    backup_path: Path | None,
    apply: bool,
    confirm: str,
    writer_quiescent: bool,
    max_items_output: int,
    maintenance_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    contract = index_contract.build_contract(db)
    compact = index_contract.compact_contract(contract, max_items_output=int(max_items_output))
    if not apply:
        return {
            "schema": RESULT_SCHEMA,
            "ok": True,
            "applied": False,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "production_identity_index_apply_cli_no_apply",
            "contract": compact,
            "guards": _guards(writes=False),
        }
    if confirm != CONFIRMATION_PHRASE:
        return _blocked("confirmation_phrase_missing_or_mismatched", extra={"contract": compact})
    if not writer_quiescent:
        return _blocked("writer_quiescent_flag_required", extra={"contract": compact})
    backup = _verify_existing_backup(backup_path)
    if backup.get("ok") is not True:
        return _blocked("fresh_backup_required", extra={"contract": compact, "backup": backup})
    maintenance = dict(maintenance_packet or maintenance_window.build_maintenance_window_packet(db, max_items_output=8))
    if maintenance.get("writer_quiescent") is not True or maintenance.get("ok") is not True:
        return _blocked(
            "maintenance_window_not_quiescent",
            extra={"contract": compact, "backup": backup, "maintenance_window": _maintenance_summary(maintenance)},
        )
    if contract.get("blockers"):
        return _blocked(
            "production_identity_index_contract_blocked",
            extra={"contract": compact, "backup": backup, "maintenance_window": _maintenance_summary(maintenance)},
        )
    apply_result = apply_index_contract(db, contract)
    post_contract = index_contract.build_contract(db)
    return {
        "schema": RESULT_SCHEMA,
        "ok": bool(apply_result.get("ok")) and post_contract.get("ok") is True,
        "applied": bool(apply_result.get("applied")),
        "read_only": False,
        "dry_run": False,
        "execution_mode": "production_identity_index_apply_cli_apply",
        "contract": compact,
        "backup": backup,
        "maintenance_window": _maintenance_summary(maintenance),
        "apply_result": apply_result,
        "post_apply_contract": index_contract.compact_contract(
            post_contract,
            max_items_output=int(max_items_output),
        ),
        "guards": dict(apply_result.get("guards") or _guards(writes=False)),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="", help=f"Required exact phrase for --apply: {CONFIRMATION_PHRASE}")
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--writer-quiescent", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = build_cli_result(
        db_path=args.db,
        backup_path=args.backup,
        apply=bool(args.apply),
        confirm=str(args.confirm or ""),
        writer_quiescent=bool(args.writer_quiescent),
        max_items_output=int(args.max_items_output),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
