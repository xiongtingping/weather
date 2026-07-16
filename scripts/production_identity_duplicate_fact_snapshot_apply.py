#!/usr/bin/env python3
"""Controlled apply for duplicate production fact_snapshot_id repair.

Default mode is no-write and prints the no-apply contract. The apply path is
intentionally narrow: it only mutates rows listed by the reviewed duplicate
decision manifest and its derived contract, after explicit confirmation, an
existing backup, and writer-quiescence verification.
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

from lineage_identity_runtime import canonical_json  # noqa: E402
from scripts import controlled_apply_maintenance_window_packet as maintenance_window  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_apply_contract as apply_contract  # noqa: E402


DEFAULT_DB = apply_contract.DEFAULT_DB
CONFIRMATION_PHRASE = apply_contract.CONFIRMATION_PHRASE
RESULT_SCHEMA = "production_identity_duplicate_fact_snapshot_apply_result_v1"


def _guards(*, writes: bool) -> dict[str, bool]:
    return {
        "writes_archive_db": bool(writes),
        "writes_event_fact_history": bool(writes),
        "writes_event_fact_latest": bool(writes),
        "writes_manifest_file": False,
        "executes_sql_writes": bool(writes),
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_loads_map(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _blocked(reason: str, *, allow_apply: bool, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "applied": False,
        "blocked": True,
        "block_reason": reason,
        "allow_apply": bool(allow_apply),
        "guards": _guards(writes=False),
    }
    out.update(dict(extra or {}))
    return out


def _load_json_file(path: Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _verify_existing_backup(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"ok": False, "reason": "backup_path_required", "path": ""}
    backup = Path(path).expanduser().resolve()
    if not backup.exists():
        return {"ok": False, "reason": "backup_path_not_found", "path": str(backup)}
    try:
        size = int(backup.stat().st_size)
    except OSError:
        size = 0
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


def _connect_rw(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _apply_history_operation(conn: sqlite3.Connection, operation: Mapping[str, Any]) -> dict[str, Any]:
    row_id = int(_as_mapping(operation.get("primary_key")).get("id") or 0)
    guard = _as_mapping(operation.get("where_guard"))
    expected_payload_id = _text(guard.get("payload_fact_snapshot_id_currently"))
    proposed_id = _text(operation.get("proposed_fact_snapshot_id"))
    row = conn.execute(
        "SELECT id, fact_json, fact_snapshot_id FROM event_fact_history WHERE id = ?",
        (row_id,),
    ).fetchone()
    if row is None:
        return {"ok": False, "table": "event_fact_history", "primary_key": {"id": row_id}, "reason": "row_missing"}
    payload = _json_loads_map(row["fact_json"])
    old_payload_id = _text(payload.get("fact_snapshot_id"))
    if old_payload_id != expected_payload_id:
        return {
            "ok": False,
            "table": "event_fact_history",
            "primary_key": {"id": row_id},
            "reason": "payload_fact_snapshot_id_guard_mismatch",
            "expected": expected_payload_id,
            "actual": old_payload_id,
        }
    set_fields = _as_mapping(operation.get("set_fields"))
    updates: list[str] = []
    params: list[Any] = []
    if "fact_json" in set_fields:
        payload["fact_snapshot_id"] = proposed_id
        updates.append("fact_json = ?")
        params.append(canonical_json(payload))
    if "fact_snapshot_id" in set_fields:
        updates.append("fact_snapshot_id = ?")
        params.append(proposed_id)
    if not updates:
        return {"ok": True, "table": "event_fact_history", "primary_key": {"id": row_id}, "changed": False}
    params.append(row_id)
    cur = conn.execute(f"UPDATE event_fact_history SET {', '.join(updates)} WHERE id = ?", params)
    return {
        "ok": cur.rowcount == 1,
        "table": "event_fact_history",
        "primary_key": {"id": row_id},
        "changed": cur.rowcount == 1,
        "rowcount": cur.rowcount,
    }


def _apply_latest_operation(conn: sqlite3.Connection, operation: Mapping[str, Any]) -> dict[str, Any]:
    primary = _as_mapping(operation.get("primary_key"))
    guard = _as_mapping(operation.get("where_guard"))
    slug = _text(primary.get("slug"))
    event_date = _text(primary.get("event_date"))
    current_fact_id = int(guard.get("fact_id") or operation.get("current_fact_id") or 0)
    row = conn.execute(
        "SELECT * FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()
    if row is None:
        return {"ok": False, "table": "event_fact_latest", "primary_key": primary, "reason": "row_missing"}
    if int(row["fact_id"] or 0) != current_fact_id:
        return {
            "ok": False,
            "table": "event_fact_latest",
            "primary_key": primary,
            "reason": "latest_fact_id_guard_mismatch",
            "expected": current_fact_id,
            "actual": int(row["fact_id"] or 0),
        }
    set_fields = _as_mapping(operation.get("set_fields"))
    updates: list[str] = []
    params: list[Any] = []
    if "fact_id" in set_fields:
        updates.append("fact_id = ?")
        params.append(int(_as_mapping(set_fields.get("fact_id")).get("new_value") or 0))
    if "fact_snapshot_id" in set_fields:
        updates.append("fact_snapshot_id = ?")
        params.append(_text(_as_mapping(set_fields.get("fact_snapshot_id")).get("new_value")))
    if not updates:
        return {"ok": True, "table": "event_fact_latest", "primary_key": primary, "changed": False}
    params.extend([slug, event_date, current_fact_id])
    cur = conn.execute(
        f"UPDATE event_fact_latest SET {', '.join(updates)} WHERE slug = ? AND event_date = ? AND fact_id = ?",
        params,
    )
    return {
        "ok": cur.rowcount == 1,
        "table": "event_fact_latest",
        "primary_key": primary,
        "changed": cur.rowcount == 1,
        "rowcount": cur.rowcount,
    }


def apply_duplicate_fact_snapshot_contract(
    conn: sqlite3.Connection,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if contract.get("ok") is not True:
        return _blocked("contract_not_ok", allow_apply=True, extra={"contract_blockers": list(contract.get("blockers") or [])})
    operations = [_as_mapping(item) for item in _as_sequence(contract.get("operations"))]
    if not operations:
        return {
            "schema": RESULT_SCHEMA,
            "ok": True,
            "applied": False,
            "blocked": False,
            "operation_count": 0,
            "applied_operation_count": 0,
            "operation_results": [],
            "guards": _guards(writes=False),
        }
    results: list[dict[str, Any]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for operation in operations:
            table = _text(operation.get("table"))
            if table == "event_fact_history":
                result = _apply_history_operation(conn, operation)
            elif table == "event_fact_latest":
                result = _apply_latest_operation(conn, operation)
            else:
                result = {"ok": False, "table": table, "reason": "unsupported_table"}
            results.append(result)
            if result.get("ok") is not True:
                conn.rollback()
                return {
                    "schema": RESULT_SCHEMA,
                    "ok": False,
                    "applied": False,
                    "blocked": True,
                    "block_reason": "operation_failed",
                    "failed_operation": result,
                    "operation_results": results,
                    "guards": _guards(writes=False),
                }
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return {
            "schema": RESULT_SCHEMA,
            "ok": False,
            "applied": False,
            "blocked": True,
            "block_reason": "sqlite_apply_error",
            "sqlite_error": str(exc),
            "operation_results": results,
            "guards": _guards(writes=False),
        }
    return {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "applied": True,
        "blocked": False,
        "operation_count": len(operations),
        "applied_operation_count": sum(1 for item in results if item.get("changed")),
        "operation_results": results,
        "guards": _guards(writes=True),
    }


def build_cli_result(
    *,
    db_path: Path,
    decision_manifest: Mapping[str, Any] | None,
    backup_path: Path | None,
    apply: bool,
    confirm: str,
    writer_quiescent: bool,
    max_items_output: int,
    maintenance_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    contract = apply_contract.build_contract(
        db,
        decision_manifest=decision_manifest,
        immutable=not apply,
        max_items_output=1000000,
    )
    if not apply:
        return {
            "schema": RESULT_SCHEMA,
            "ok": True,
            "applied": False,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "production_duplicate_fact_snapshot_apply_cli_no_apply",
            "contract": apply_contract.compact_contract(contract, max_items_output=int(max_items_output)),
            "guards": _guards(writes=False),
        }
    if confirm != CONFIRMATION_PHRASE:
        return _blocked("confirmation_phrase_missing_or_mismatched", allow_apply=True)
    if not writer_quiescent:
        return _blocked("writer_quiescent_flag_required", allow_apply=True)
    backup = _verify_existing_backup(backup_path)
    if backup.get("ok") is not True:
        return _blocked("fresh_backup_required", allow_apply=True, extra={"backup": backup})
    maintenance = dict(maintenance_packet or maintenance_window.build_maintenance_window_packet(db, max_items_output=8))
    if maintenance.get("writer_quiescent") is not True or maintenance.get("ok") is not True:
        return _blocked(
            "maintenance_window_not_quiescent",
            allow_apply=True,
            extra={"maintenance_window": _maintenance_summary(maintenance), "backup": backup},
        )
    if contract.get("ok") is not True:
        return _blocked(
            "duplicate_apply_contract_not_ready",
            allow_apply=True,
            extra={
                "contract": apply_contract.compact_contract(contract, max_items_output=int(max_items_output)),
                "backup": backup,
                "maintenance_window": _maintenance_summary(maintenance),
            },
        )
    with _connect_rw(db) as conn:
        apply_result = apply_duplicate_fact_snapshot_contract(conn, contract)
    return {
        "schema": RESULT_SCHEMA,
        "ok": bool(apply_result.get("ok")),
        "applied": bool(apply_result.get("applied")),
        "read_only": False,
        "dry_run": False,
        "execution_mode": "production_duplicate_fact_snapshot_apply_cli_apply",
        "contract": apply_contract.compact_contract(contract, max_items_output=int(max_items_output)),
        "backup": backup,
        "maintenance_window": _maintenance_summary(maintenance),
        "apply_result": apply_result,
        "guards": dict(apply_result.get("guards") or _guards(writes=False)),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--decision-manifest", type=Path, required=True)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="", help=f"Required exact phrase for --apply: {CONFIRMATION_PHRASE}")
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--writer-quiescent", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = _load_json_file(args.decision_manifest)
    result = build_cli_result(
        db_path=args.db,
        decision_manifest=manifest,
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
