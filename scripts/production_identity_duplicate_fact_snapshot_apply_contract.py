#!/usr/bin/env python3
"""Read-only apply contract for duplicate production fact_snapshot_id repair.

This contract consumes an operator-reviewed duplicate decision manifest and
projects the exact event_fact_history / event_fact_latest mutations required to
make duplicated payload fact_snapshot_id values unique. It never writes
archive.db, never creates a backup, and never writes a manifest.
"""

from __future__ import annotations

import argparse
import hashlib
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

from lineage_identity_runtime import canonical_json  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_packet as duplicate_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "production_identity_duplicate_fact_snapshot_apply_contract_v1"
CONFIRMATION_PHRASE = "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_manifest_file": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _connect(db: Path, *, immutable: bool) -> sqlite3.Connection:
    uri = f"file:{db}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


def _load_json_file(path: str | os.PathLike[str] | None) -> dict[str, Any] | None:
    if not path:
        return None
    payload_path = Path(path).expanduser()
    with payload_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _json_loads_map(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"] or "") for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _history_rows_by_id(conn: sqlite3.Connection, row_ids: Sequence[Any]) -> dict[int, dict[str, Any]]:
    ids = sorted({int(item) for item in row_ids if str(item or "").isdigit()})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT id, slug, event_date, fact_as_of, fact_json, fact_snapshot_id
        FROM event_fact_history
        WHERE id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def _latest_rows_for_fact_ids(conn: sqlite3.Connection, fact_ids: Sequence[Any]) -> list[dict[str, Any]]:
    ids = sorted({int(item) for item in fact_ids if str(item or "").isdigit()})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"""
            SELECT *
            FROM event_fact_latest
            WHERE fact_id IN ({placeholders})
            ORDER BY slug, event_date
            """,
            ids,
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _canonical_row_by_current_fact(packet: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for group in _as_sequence(packet.get("groups")):
        group_map = _as_mapping(group)
        fact_snapshot_id = _text(group_map.get("fact_snapshot_id"))
        canonical_id = int(group_map.get("canonical_row_id") or 0)
        if fact_snapshot_id and canonical_id:
            out[fact_snapshot_id] = canonical_id
    return out


def _history_operation(
    *,
    row: Mapping[str, Any],
    audit_item: Mapping[str, Any],
    has_fact_snapshot_column: bool,
) -> dict[str, Any]:
    row_id = int(row.get("id") or audit_item.get("row_id") or 0)
    proposed_id = _text(audit_item.get("proposed_fact_snapshot_id"))
    current_id = _text(audit_item.get("current_fact_snapshot_id"))
    payload = _json_loads_map(row.get("fact_json"))
    old_payload_id = _text(payload.get("fact_snapshot_id"))
    new_payload = dict(payload)
    if bool(audit_item.get("requires_payload_update")):
        new_payload["fact_snapshot_id"] = proposed_id
    new_payload_json = canonical_json(new_payload)
    set_fields: dict[str, Any] = {}
    if bool(audit_item.get("requires_payload_update")):
        set_fields["fact_json"] = {
            "json_path": "$.fact_snapshot_id",
            "old_value": old_payload_id,
            "new_value": proposed_id,
            "old_sha256": _sha256_text(str(row.get("fact_json") or "")),
            "new_sha256": _sha256_text(new_payload_json),
        }
    if has_fact_snapshot_column:
        set_fields["fact_snapshot_id"] = {
            "old_value": _text(row.get("fact_snapshot_id")),
            "new_value": proposed_id,
        }
    return {
        "table": "event_fact_history",
        "primary_key": {"id": row_id},
        "decision": audit_item.get("decision"),
        "operation": "update_history_payload_and_identity_column"
        if bool(audit_item.get("requires_payload_update"))
        else "align_history_identity_column_with_canonical_payload",
        "current_fact_snapshot_id": current_id,
        "proposed_fact_snapshot_id": proposed_id,
        "set_fields": set_fields,
        "where_guard": {
            "id": row_id,
            "payload_fact_snapshot_id_currently": old_payload_id,
        },
        "sql_preview": "UPDATE event_fact_history SET ... WHERE id = ? AND json_extract(fact_json, '$.fact_snapshot_id') = ?",
    }


def _latest_operations(
    *,
    conn: sqlite3.Connection,
    packet: Mapping[str, Any],
    audit_items_by_row_id: Mapping[int, Mapping[str, Any]],
    latest_anchor_policy: str,
) -> list[dict[str, Any]]:
    latest_columns = _table_columns(conn, "event_fact_latest")
    if "fact_id" not in latest_columns:
        return []
    latest_rows = _latest_rows_for_fact_ids(conn, audit_items_by_row_id.keys())
    canonical_by_current = _canonical_row_by_current_fact(packet)
    operations: list[dict[str, Any]] = []
    for row in latest_rows:
        fact_id = int(row.get("fact_id") or 0)
        audit_item = _as_mapping(audit_items_by_row_id.get(fact_id))
        if not audit_item:
            continue
        proposed_id = _text(audit_item.get("proposed_fact_snapshot_id"))
        current_id = _text(audit_item.get("current_fact_snapshot_id"))
        set_fields: dict[str, Any] = {}
        operation = "align_latest_identity_column_with_existing_fact_anchor"
        if latest_anchor_policy == "repoint_latest_to_natural_fact_snapshot_owner_after_duplicate_apply":
            canonical_id = int(canonical_by_current.get(current_id) or 0)
            if canonical_id and canonical_id != fact_id:
                set_fields["fact_id"] = {"old_value": fact_id, "new_value": canonical_id}
                operation = "repoint_latest_to_natural_fact_snapshot_owner"
        if "fact_snapshot_id" in latest_columns:
            latest_fact_snapshot_id = proposed_id
            if latest_anchor_policy == "repoint_latest_to_natural_fact_snapshot_owner_after_duplicate_apply":
                canonical_id = int(canonical_by_current.get(current_id) or 0)
                canonical_item = _as_mapping(audit_items_by_row_id.get(canonical_id))
                latest_fact_snapshot_id = _text(canonical_item.get("proposed_fact_snapshot_id")) or current_id
            set_fields["fact_snapshot_id"] = {
                "old_value": _text(row.get("fact_snapshot_id")),
                "new_value": latest_fact_snapshot_id,
            }
        if not set_fields:
            continue
        operations.append(
            {
                "table": "event_fact_latest",
                "primary_key": {"slug": row.get("slug"), "event_date": row.get("event_date")},
                "operation": operation,
                "latest_anchor_policy": latest_anchor_policy,
                "current_fact_id": fact_id,
                "set_fields": set_fields,
                "where_guard": {"slug": row.get("slug"), "event_date": row.get("event_date"), "fact_id": fact_id},
                "sql_preview": "UPDATE event_fact_latest SET ... WHERE slug = ? AND event_date = ? AND fact_id = ?",
            }
        )
    return operations


def build_contract(
    db_path: Path | str = DEFAULT_DB,
    *,
    decision_manifest: Mapping[str, Any] | None = None,
    immutable: bool = False,
    max_items_output: int = 20,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    backup_path = backup_root / "archive.db.production-duplicate-fact-snapshot-repair.bak"
    blockers: list[str] = []
    if not db.exists():
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "read_only": True,
            "dry_run": True,
            "db": str(db),
            "blockers": ["archive_db_missing"],
            "guards": _guards(),
        }

    packet = duplicate_packet.build_packet(
        db,
        immutable=immutable,
        max_items_output=1000000,
        decision_manifest=decision_manifest,
    )
    duplicate_row_count = int(packet.get("duplicate_row_count") or 0)
    if duplicate_row_count > 0 and not decision_manifest:
        blockers.append("duplicate_decision_manifest_required")
    audit = _as_mapping(packet.get("decision_manifest_audit"))
    if packet.get("ok") is not True:
        blockers.append("duplicate_decision_manifest_audit_not_ready")
        blockers.extend(str(item) for item in packet.get("hard_blockers") or [])

    audit_items = [_as_mapping(item) for item in _as_sequence(audit.get("items"))]
    ready_items = [item for item in audit_items if _text(item.get("state")) == "ready_for_duplicate_resolution_apply"]
    ready_by_row_id = {int(item["row_id"]): item for item in ready_items if str(item.get("row_id") or "").isdigit()}
    latest_anchor_policy = _text(audit.get("latest_anchor_policy"))
    operations: list[dict[str, Any]] = []
    with _connect(db, immutable=immutable) as conn:
        history_columns = _table_columns(conn, "event_fact_history")
        if not {"id", "fact_json"}.issubset(history_columns):
            blockers.append("event_fact_history_required_columns_missing")
        history_rows = _history_rows_by_id(conn, ready_by_row_id.keys())
        missing_history_rows = sorted(row_id for row_id in ready_by_row_id if row_id not in history_rows)
        if missing_history_rows:
            blockers.append("event_fact_history_ready_row_missing")
        for row_id, audit_item in sorted(ready_by_row_id.items()):
            row = history_rows.get(row_id)
            if not row:
                continue
            operations.append(
                _history_operation(
                    row=row,
                    audit_item=audit_item,
                    has_fact_snapshot_column="fact_snapshot_id" in history_columns,
                )
            )
        operations.extend(
            _latest_operations(
                conn=conn,
                packet=packet,
                audit_items_by_row_id=ready_by_row_id,
                latest_anchor_policy=latest_anchor_policy,
            )
        )

    if ready_items and len(ready_items) != int(audit.get("expected_decision_count") or 0):
        blockers.append("not_all_duplicate_decisions_ready")
    if ready_items and not operations:
        blockers.append("duplicate_apply_contract_has_no_operations")

    visible_limit = max(0, int(max_items_output))
    ok = bool(not blockers and (operations or duplicate_row_count == 0))
    backup_command = f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup_path)}\""
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "production_identity_duplicate_fact_snapshot_apply_contract_no_apply",
        "db": str(db),
        "packet_schema": packet.get("schema"),
        "duplicate_group_count": int(packet.get("duplicate_group_count") or 0),
        "duplicate_row_count": duplicate_row_count,
        "decision_manifest_audit": {
            "schema": audit.get("schema"),
            "manifest_provided": bool(audit.get("manifest_provided")),
            "expected_decision_count": int(audit.get("expected_decision_count") or 0),
            "ready_decision_count": int(audit.get("ready_decision_count") or 0),
            "blocking_for_current_apply": bool(audit.get("blocking_for_current_apply")),
            "latest_anchor_policy": audit.get("latest_anchor_policy"),
            "latest_anchor_policy_ready": bool(audit.get("latest_anchor_policy_ready")),
            "hard_blockers": list(audit.get("hard_blockers") or []),
        },
        "operation_count": len(operations),
        "operations": operations[:visible_limit],
        "operations_truncated": len(operations) > visible_limit,
        "blockers": sorted(set(blockers)),
        "operator_contract": {
            "schema": "production_identity_duplicate_fact_snapshot_operator_contract_v1",
            "confirmation_phrase": CONFIRMATION_PHRASE,
            "requires_backup": True,
            "requires_writer_quiescence": True,
            "requires_reviewed_decision_manifest": True,
            "allow_apply_now": False,
            "backup_path": str(backup_path),
            "backup_command": backup_command,
            "verify_writer_quiescence_command": (
                f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} "
                "--summary --max-items-output 8"
            ),
            "post_apply_verification_commands": [
                "python3 scripts/production_identity_duplicate_fact_snapshot_packet.py --summary --max-items-output 8 --decision-manifest <reviewed_duplicate_manifest.json>",
                "python3 scripts/production_identity_merge_fill_plan.py --summary --limit 1000000 --max-items-output 8 --duplicate-decision-manifest <reviewed_duplicate_manifest.json>",
                "python3 scripts/production_identity_coverage_audit.py --summary --max-items-output 8",
            ],
        },
        "guards": _guards(),
        "blocked_operations": [
            "automatic_duplicate_fact_snapshot_repair_apply",
            "automatic_manifest_file_write",
            "automatic_backup_creation",
            "historical_backfill",
            "launchd_restart",
            "order_or_trade_decision_write",
            "parameter_change",
        ],
        "next_boundary": (
            "implement_or_run_controlled_duplicate_fact_snapshot_apply_after_backup_writer_quiescence_and_confirmation"
            if ok and operations
            else "duplicate_fact_snapshot_apply_not_required"
            if ok
            else "provide_reviewed_duplicate_decision_manifest_before_apply_contract"
        ),
    }


def compact_contract(contract: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    return {
        "schema": str(contract.get("schema") or SCHEMA_VERSION) + "_summary",
        "source_schema": contract.get("schema"),
        "ok": bool(contract.get("ok")),
        "read_only": bool(contract.get("read_only")),
        "dry_run": bool(contract.get("dry_run")),
        "db": contract.get("db"),
        "duplicate_group_count": int(contract.get("duplicate_group_count") or 0),
        "duplicate_row_count": int(contract.get("duplicate_row_count") or 0),
        "decision_manifest_audit": dict(_as_mapping(contract.get("decision_manifest_audit"))),
        "operation_count": int(contract.get("operation_count") or 0),
        "operations": list(contract.get("operations") or [])[: max(0, int(max_items_output))],
        "operations_truncated": bool(contract.get("operations_truncated")),
        "blockers": list(contract.get("blockers") or [])[:50],
        "operator_contract": dict(_as_mapping(contract.get("operator_contract"))),
        "guards": dict(_as_mapping(contract.get("guards"))),
        "next_boundary": contract.get("next_boundary"),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only duplicate fact_snapshot_id apply contract.")
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or DEFAULT_DB))
    parser.add_argument("--immutable", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--decision-manifest", default="")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    contract = build_contract(
        args.db,
        immutable=bool(args.immutable),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
        decision_manifest=_load_json_file(args.decision_manifest),
    )
    output: Mapping[str, Any] = (
        compact_contract(contract, max_items_output=int(args.max_items_output))
        if args.summary
        else contract
    )
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if contract.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
