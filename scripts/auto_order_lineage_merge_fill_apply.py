#!/usr/bin/env python3
"""Controlled apply for auto_orders lineage merge-fill.

Default mode is no-write and prints the read-only apply contract. The apply
path only fills empty auto_orders.decision_id / signal_id fields projected by
the contract, after explicit confirmation, an existing backup, and
writer-quiescence verification.
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

from scripts import auto_order_lineage_merge_fill_apply_contract as apply_contract  # noqa: E402
from scripts import controlled_apply_maintenance_window_packet as maintenance_window  # noqa: E402


DEFAULT_DB = apply_contract.DEFAULT_DB
CONFIRMATION_PHRASE = apply_contract.CONFIRMATION_PHRASE
RESULT_SCHEMA = "auto_order_lineage_merge_fill_apply_result_v1"
ALLOWED_TABLE = "auto_orders"
ALLOWED_OPERATION = "merge_fill_empty_auto_order_lineage_fields"
ALLOWED_FIELDS = {"decision_id", "signal_id"}


def _guards(*, writes: bool) -> dict[str, bool]:
    return {
        "writes_archive_db": bool(writes),
        "writes_auto_orders": bool(writes),
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_signal_to_order_timing": False,
        "writes_manifest_file": False,
        "executes_sql_writes": bool(writes),
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "fetches_orderbook": False,
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


def _quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


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


def _connect_rw(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _apply_operation(conn: sqlite3.Connection, operation: Mapping[str, Any]) -> dict[str, Any]:
    table = _text(operation.get("table"))
    op_name = _text(operation.get("operation"))
    if table != ALLOWED_TABLE:
        return {"ok": False, "table": table, "reason": "unsupported_table"}
    if op_name != ALLOWED_OPERATION:
        return {"ok": False, "table": table, "operation": op_name, "reason": "unsupported_operation"}

    primary_key = _as_mapping(operation.get("primary_key"))
    if set(primary_key) != {"id"} or _text(primary_key.get("id")) == "":
        return {"ok": False, "table": table, "primary_key": primary_key, "reason": "unsupported_primary_key"}

    raw_set_fields = _as_mapping(operation.get("set_fields"))
    unexpected_fields = sorted(set(str(field) for field in raw_set_fields) - ALLOWED_FIELDS)
    if unexpected_fields:
        return {"ok": False, "table": table, "reason": "unsupported_set_fields", "fields": unexpected_fields}

    set_values = {
        str(field): _as_mapping(value).get("new_value")
        for field, value in raw_set_fields.items()
        if str(field) in ALLOWED_FIELDS and _text(_as_mapping(value).get("new_value"))
    }
    if not set_values:
        return {"ok": False, "table": table, "primary_key": primary_key, "reason": "operation_missing_set_fields"}

    for field, value in raw_set_fields.items():
        if str(field) in set_values and _text(_as_mapping(value).get("old_value_policy")) != "must_be_empty":
            return {"ok": False, "table": table, "field": field, "reason": "old_value_policy_must_be_empty"}

    ordered_fields = sorted(set_values)
    set_sql = ", ".join(f"{_quote_ident(field)} = ?" for field in ordered_fields)
    params = [set_values[field] for field in ordered_fields]
    params.append(primary_key["id"])
    where_parts = [f"{_quote_ident('id')} = ?"]
    for field in ordered_fields:
        where_parts.append(f"({_quote_ident(field)} IS NULL OR trim(CAST({_quote_ident(field)} AS TEXT)) = '')")

    sql = f"UPDATE {_quote_ident(table)} SET {set_sql} WHERE {' AND '.join(where_parts)}"
    cur = conn.execute(sql, params)
    if cur.rowcount != 1:
        return {
            "ok": False,
            "table": table,
            "primary_key": primary_key,
            "reason": "merge_fill_guard_mismatch_or_row_missing",
            "rowcount": cur.rowcount,
        }
    return {"ok": True, "table": table, "primary_key": primary_key, "changed": True, "rowcount": cur.rowcount}


def apply_auto_order_lineage_merge_fill_contract(
    conn: sqlite3.Connection,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if contract.get("ok") is not True:
        return _blocked("contract_not_ok", extra={"contract_blockers": list(contract.get("blockers") or [])})
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
            result = _apply_operation(conn, operation)
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
    backup_path: Path | None,
    apply: bool,
    confirm: str,
    writer_quiescent: bool,
    limit: int,
    max_items_output: int,
    resolve_candidates: bool,
    enable_decision_order_id_lookup: bool,
    max_small_table_scan_rows: int,
    ignore_legacy_orders: bool,
    order_since_hours: float,
    ignore_orders_before: str,
    maintenance_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    contract = apply_contract.build_contract(
        db,
        limit=int(limit),
        max_items_output=1000000,
        resolve_candidates=bool(resolve_candidates),
        enable_decision_order_id_lookup=bool(enable_decision_order_id_lookup),
        max_small_table_scan_rows=int(max_small_table_scan_rows),
        ignore_legacy_orders=bool(ignore_legacy_orders),
        order_since_hours=float(order_since_hours),
        ignore_orders_before=str(ignore_orders_before or ""),
    )
    if not apply:
        return {
            "schema": RESULT_SCHEMA,
            "ok": True,
            "applied": False,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "auto_order_lineage_merge_fill_apply_cli_no_apply",
            "contract": apply_contract.compact_contract(contract, max_items_output=int(max_items_output)),
            "guards": _guards(writes=False),
        }
    if confirm != CONFIRMATION_PHRASE:
        return _blocked("confirmation_phrase_missing_or_mismatched")
    if not writer_quiescent:
        return _blocked("writer_quiescent_flag_required")
    backup = _verify_existing_backup(backup_path)
    if backup.get("ok") is not True:
        return _blocked("fresh_backup_required", extra={"backup": backup})
    maintenance = dict(maintenance_packet or maintenance_window.build_maintenance_window_packet(db, max_items_output=8))
    if maintenance.get("writer_quiescent") is not True or maintenance.get("ok") is not True:
        return _blocked(
            "maintenance_window_not_quiescent",
            extra={"maintenance_window": _maintenance_summary(maintenance), "backup": backup},
        )
    if contract.get("ok") is not True:
        return _blocked(
            "auto_order_lineage_merge_fill_apply_contract_not_ready",
            extra={
                "contract": apply_contract.compact_contract(contract, max_items_output=int(max_items_output)),
                "backup": backup,
                "maintenance_window": _maintenance_summary(maintenance),
            },
        )

    with _connect_rw(db) as conn:
        apply_result = apply_auto_order_lineage_merge_fill_contract(conn, contract)
    return {
        "schema": RESULT_SCHEMA,
        "ok": bool(apply_result.get("ok")),
        "applied": bool(apply_result.get("applied")),
        "read_only": False,
        "dry_run": False,
        "execution_mode": "auto_order_lineage_merge_fill_apply_cli_apply",
        "contract": apply_contract.compact_contract(contract, max_items_output=int(max_items_output)),
        "backup": backup,
        "maintenance_window": _maintenance_summary(maintenance),
        "apply_result": apply_result,
        "guards": dict(apply_result.get("guards") or _guards(writes=False)),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--max-small-table-scan-rows", type=int, default=5000)
    parser.add_argument("--resolve-candidates", action="store_true")
    parser.add_argument("--enable-decision-order-id-lookup", action="store_true")
    parser.add_argument("--ignore-legacy-orders", action="store_true")
    parser.add_argument("--order-since-hours", type=float, default=0.0)
    parser.add_argument("--ignore-orders-before", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="", help=f"Required exact phrase for --apply: {CONFIRMATION_PHRASE}")
    parser.add_argument("--backup-path", "--backup", dest="backup_path", type=Path, default=None)
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = build_cli_result(
        db_path=args.db,
        backup_path=args.backup_path,
        apply=bool(args.apply),
        confirm=str(args.confirm or ""),
        writer_quiescent=bool(args.writer_quiescent),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        resolve_candidates=bool(args.resolve_candidates),
        enable_decision_order_id_lookup=bool(args.enable_decision_order_id_lookup),
        max_small_table_scan_rows=int(args.max_small_table_scan_rows),
        ignore_legacy_orders=bool(args.ignore_legacy_orders),
        order_since_hours=float(args.order_since_hours),
        ignore_orders_before=str(args.ignore_orders_before or ""),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
