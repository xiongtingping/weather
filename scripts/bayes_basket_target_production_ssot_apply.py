#!/usr/bin/env python3
"""Controlled migration for the Bayes × basket production SSOT.

The default action is a read-only plan.  A mutating run requires explicit
confirmation, a pause marker, proven writer quiescence and a local SQLite
backup path.  If the current
``auto_trade_decisions`` is the legacy table, it is renamed and retained as an
offline archive before the target M5 authority is installed; rows are never
deleted or converted in place.  The resident service never invokes this
script at startup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from bayes_basket_target.atomic_ledger import initialize_capital_pool, target_ledger_schema_status  # noqa: E402
from bayes_basket_target.activation import assert_target_database_owner  # noqa: E402
from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    DEFAULT_TARGET_PAUSE_MARKER,
    resolve_target_database,
)
from bayes_basket_target.execution_vector_repair import (  # noqa: E402
    inspect_legacy_blocked_execution_vector_repair,
    repair_legacy_blocked_execution_vectors,
)
from bayes_basket_target.migration_quiescence import validate_writer_quiescence_receipt  # noqa: E402
from bayes_basket_target.market_registry import apply_target_market_registry_schema  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    MIN_TARGET_FREE_BYTES,
    ResourceBoundError,
    assert_process_memory_budget,
    inspect_target_resource_budget,
)
from bayes_basket_target.ssot import (  # noqa: E402
    apply_production_ssot_schema,
    production_ssot_schema_status,
)


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TARGET_DECISION_COLUMNS = {
    "decision_id", "cycle_id", "plan_id", "station_id", "event_date",
    "strategy_lane", "decided_state", "will_order", "reason_codes_json",
    "details_json", "decision_sha256", "created_at",
}
PAUSE_MARKER_TOKEN = "bayes-basket-target-m5-migration-v1"
# Backup, rollback journal/WAL and post-migration database may coexist.
MIGRATION_WORKING_COPY_MULTIPLIER = 3

CASH_PHASE_AUTHORITY_MANIFEST_COLUMNS = (
    ("manifest_sha256", "TEXT", 0, 1),
    ("station_id", "TEXT", 1, 0),
    ("policy_version", "TEXT", 1, 0),
    ("artifact_sha256", "TEXT", 1, 0),
    ("station_timezone", "TEXT", 1, 0),
    ("valid_from_utc", "TEXT", 1, 0),
    ("valid_until_utc", "TEXT", 1, 0),
    ("authority_scope", "TEXT", 1, 0),
)
CASH_PHASE_AUTHORITY_MANIFEST_INDEX = (
    "station_id",
    "authority_scope",
    "valid_from_utc",
    "valid_until_utc",
)
CASH_PHASE_AUTHORITY_MANIFEST_TRIGGERS = (
    "basket_cash_phase_authority_no_update",
    "basket_cash_phase_authority_no_delete",
)


class MigrationError(RuntimeError):
    """Raised when the controlled migration cannot prove a safe boundary."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _quote_identifier(value: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise MigrationError("legacy_archive_table_name_invalid")
    return '"' + value.replace('"', '""') + '"'


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _legacy_decision_columns(conn: sqlite3.Connection) -> set[str]:
    if "auto_trade_decisions" not in _table_names(conn):
        return set()
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(auto_trade_decisions)")}


def inspect_cash_phase_authority_manifest_schema(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Check the persisted cash-phase authority contract, not just its name."""

    table = "basket_cash_phase_authority_manifest"
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if table_row is None:
        return {
            "ok": False,
            "table_present": False,
            "columns_ok": False,
            "table_constraints_ok": False,
            "index_ok": False,
            "triggers_ok": False,
            "missing": ["table"],
        }

    actual_columns = tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in conn.execute(f'PRAGMA table_info("{table}")')
    )
    columns_ok = actual_columns == CASH_PHASE_AUTHORITY_MANIFEST_COLUMNS
    table_sql = str(table_row[0] or "").lower()
    required_constraints = (
        "check(length(manifest_sha256)=64)",
        "check(length(artifact_sha256)=64)",
        "authority_scope in ('formal_promoted','production_no_live_candidate')",
        "check(valid_from_utc<valid_until_utc)",
        "foreign key(policy_version) references basket_cash_phase_policy_artifact(policy_version)",
    )
    table_constraints_ok = all(item in table_sql for item in required_constraints)

    index_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        ("basket_cash_phase_authority_scope_idx",),
    ).fetchone()
    index_columns = tuple(
        str(row[2])
        for row in conn.execute(
            'PRAGMA index_info("basket_cash_phase_authority_scope_idx")'
        )
    )
    index_ok = (
        index_row is not None
        and index_columns == CASH_PHASE_AUTHORITY_MANIFEST_INDEX
        and "on basket_cash_phase_authority_manifest" in str(index_row[0] or "").lower()
    )

    trigger_sql: dict[str, str] = {}
    for name in CASH_PHASE_AUTHORITY_MANIFEST_TRIGGERS:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
        if row is not None:
            trigger_sql[name] = str(row[0] or "")
    triggers_ok = (
        set(trigger_sql) == set(CASH_PHASE_AUTHORITY_MANIFEST_TRIGGERS)
        and "before update on basket_cash_phase_authority_manifest" in trigger_sql.get(
            "basket_cash_phase_authority_no_update", ""
        ).lower()
        and "before delete on basket_cash_phase_authority_manifest" in trigger_sql.get(
            "basket_cash_phase_authority_no_delete", ""
        ).lower()
        and all("append_only" in sql.lower() for sql in trigger_sql.values())
    )
    missing = []
    if not columns_ok:
        missing.append("columns")
    if not table_constraints_ok:
        missing.append("table_constraints")
    if not index_ok:
        missing.append("index")
    if not triggers_ok:
        missing.append("triggers")
    return {
        "ok": not missing,
        "table_present": True,
        "columns_ok": columns_ok,
        "table_constraints_ok": table_constraints_ok,
        "index_ok": index_ok,
        "triggers_ok": triggers_ok,
        "columns": [list(item) for item in actual_columns],
        "index_columns": list(index_columns),
        "trigger_names": sorted(trigger_sql),
        "missing": missing,
    }


def inspect_migration(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a read-only migration plan without changing the connection."""

    tables = _table_names(conn)
    columns = _legacy_decision_columns(conn)
    collision = bool(columns) and not TARGET_DECISION_COLUMNS.issubset(columns)
    legacy_count = (
        int(conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0])
        if "auto_trade_decisions" in tables else 0
    )
    target_status = target_ledger_schema_status(conn)
    ssot_status = production_ssot_schema_status(conn)
    manifest_schema = inspect_cash_phase_authority_manifest_schema(conn)
    vector_repair = inspect_legacy_blocked_execution_vector_repair(conn)
    return {
        "schema": "bayes_basket_target_ssot_migration_plan_v1",
        "legacy_decision_table_present": "auto_trade_decisions" in tables,
        "legacy_decision_table_collision": collision,
        "legacy_decision_row_count": legacy_count,
        "target_ledger_schema": target_status,
        "production_ssot_schema": ssot_status,
        "cash_phase_authority_manifest_schema": manifest_schema,
        "blocked_execution_vector_lineage": {
            key: value
            for key, value in vector_repair.items()
            if key != "_candidates"
        },
        "mutation_required": (
            collision
            or not target_status.get("ok", False)
            or not ssot_status.get("ok", False)
            or not manifest_schema.get("ok", False)
            or not vector_repair.get("ok", False)
        ),
        "writes_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def _migration_resource_snapshot(source: Path) -> dict[str, Any]:
    try:
        snapshot = inspect_target_resource_budget(source)
        assert_process_memory_budget(projected_bytes=8 * 1024 * 1024)
    except ResourceBoundError as exc:
        raise MigrationError(str(exc)) from exc
    if snapshot.get("ok") is not True:
        raise MigrationError(
            "target_ssot_migration_resource_budget_exceeded:"
            + ",".join(str(item) for item in snapshot.get("violations", []))
        )
    source_bytes = int(snapshot.get("database_bytes") or 0)
    wal_bytes = int(snapshot.get("wal_bytes") or 0)
    required_free_bytes = (
        MIN_TARGET_FREE_BYTES
        + MIGRATION_WORKING_COPY_MULTIPLIER * (source_bytes + wal_bytes)
    )
    if int(snapshot.get("free_bytes") or 0) < required_free_bytes:
        raise MigrationError("target_ssot_migration_working_space_insufficient")
    return {
        **snapshot,
        "required_free_bytes": required_free_bytes,
        "working_copy_multiplier": MIGRATION_WORKING_COPY_MULTIPLIER,
    }


def _backup_database(source: Path, destination: Path) -> dict[str, Any]:
    destination = destination.expanduser().resolve()
    if destination == source.expanduser().resolve():
        raise MigrationError("backup_path_equals_database")
    if destination.is_symlink():
        raise MigrationError("backup_path_must_not_be_symlink")
    source_root = source.expanduser().resolve().parent
    try:
        if os.path.commonpath((str(source_root), str(destination.parent))) != str(source_root):
            raise MigrationError("backup_path_outside_database_directory")
    except ValueError as exc:
        raise MigrationError("backup_path_filesystem_mismatch") from exc
    source_device = source.stat().st_dev
    parent = destination.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if parent.stat().st_dev != source_device:
        raise MigrationError("backup_path_external_filesystem_forbidden")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise MigrationError("backup_path_already_exists")
    # SQLite's online backup API includes committed WAL state; a raw file
    # copy would be an unsafe recovery artifact while the database is in WAL
    # mode.
    try:
        with sqlite3.connect(
            f"file:{source}?mode=ro", uri=True, timeout=60.0
        ) as source_conn:
            with sqlite3.connect(destination, timeout=60.0) as destination_conn:
                source_conn.backup(destination_conn)
                destination_conn.commit()
                quick_check = [
                    str(row[0])
                    for row in destination_conn.execute("PRAGMA quick_check")
                ]
                if quick_check != ["ok"]:
                    raise MigrationError("backup_database_quick_check_failed")
                page_count = int(
                    destination_conn.execute("PRAGMA page_count").fetchone()[0]
                )
                page_size = int(
                    destination_conn.execute("PRAGMA page_size").fetchone()[0]
                )
    except Exception:
        # An incomplete backup is neither lineage nor a recovery artifact.
        # Remove it immediately so a later run cannot mistake it for a valid
        # pre-migration image.
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        backup_bytes = int(destination.stat().st_size)
    except OSError as exc:
        raise MigrationError("backup_database_stat_failed") from exc
    if backup_bytes <= 0:
        raise MigrationError("backup_database_empty")
    if backup_bytes > MAX_TARGET_DB_BYTES:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise MigrationError("backup_database_budget_exceeded")
    return {
        "path": str(destination),
        "bytes": backup_bytes,
        "page_count": page_count,
        "page_size": page_size,
        "quick_check": "ok",
    }


def apply_controlled_migration(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    backup_path: Path,
    pause_file: Path,
    legacy_archive_table: str,
    confirmed: bool,
    writer_quiescence_receipt: dict[str, Any] | None = None,
    capital_balance_usdc: float | None = None,
    capital_epoch_id: str | None = None,
) -> dict[str, Any]:
    """Apply target DDL only after the operator boundary is explicit.

    This function is intended for a maintenance window and is tested on a
    temporary same-schema database.  It does not populate probability,
    market, order, decision or intent facts.  Capital initialization is also
    opt-in and requires both an explicit amount and epoch.
    """

    if not confirmed:
        raise MigrationError("target_ssot_migration_confirmation_required")
    if writer_quiescence_receipt is None:
        raise MigrationError("target_ssot_migration_writer_quiescence_receipt_required")
    if not pause_file.exists():
        raise MigrationError("target_ssot_migration_pause_marker_missing")
    try:
        pause_marker = pause_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise MigrationError("target_ssot_migration_pause_marker_unreadable") from exc
    if PAUSE_MARKER_TOKEN not in {line.strip() for line in pause_marker.splitlines()}:
        raise MigrationError("target_ssot_migration_pause_marker_invalid")
    source = resolve_target_database(db_path)
    if not source.exists() or source.is_symlink() or not source.is_file():
        raise MigrationError("target_ssot_migration_database_path_invalid")
    if (capital_balance_usdc is None) != (capital_epoch_id is None):
        raise MigrationError("capital_pool_amount_and_epoch_must_be_paired")
    main_db = conn.execute("PRAGMA database_list").fetchall()
    connection_path = next((str(row[2] or "") for row in main_db if str(row[1]) == "main"), "")
    if not connection_path or Path(connection_path).expanduser().resolve() != source:
        raise MigrationError("migration_connection_database_mismatch")
    try:
        validated_quiescence = validate_writer_quiescence_receipt(
            writer_quiescence_receipt,
            db_path=source,
            require_current_database_identity=True,
        )
    except ValueError as exc:
        raise MigrationError(str(exc)) from exc
    archive = str(legacy_archive_table or "").strip()
    _quote_identifier(archive)
    if archive == "auto_trade_decisions":
        raise MigrationError("legacy_archive_table_must_differ")
    before = inspect_migration(conn)
    if before["mutation_required"] or capital_balance_usdc is not None or capital_epoch_id is not None:
        try:
            assert_target_database_owner(conn)
        except ValueError as exc:
            raise MigrationError(str(exc)) from exc
    if before["legacy_decision_table_collision"]:
        if archive in _table_names(conn):
            raise MigrationError("legacy_archive_table_already_exists")
    mutating = bool(before["mutation_required"] or capital_balance_usdc is not None or capital_epoch_id is not None)
    if not mutating:
        return {
            "schema": "bayes_basket_target_ssot_migration_receipt_v1",
            "state": "already_ready",
            "before": before,
            "after": before,
            "legacy_archive_table": None,
            "writes_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
        }
    # Every mutating invocation creates a fresh local backup.  Reusing an
    # existing path is rejected by _backup_database so an operator cannot
    # accidentally rely on a stale recovery image.
    resource_before = _migration_resource_snapshot(source)
    backup_receipt = _backup_database(source, backup_path)

    own_transaction = not conn.in_transaction
    savepoint = "bayes_target_controlled_migration"
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        if before["legacy_decision_table_collision"]:
            conn.execute(
                f"ALTER TABLE auto_trade_decisions RENAME TO {_quote_identifier(archive)}"
            )
        schema_result = apply_production_ssot_schema(conn, commit=False)
        registry_schema_result = apply_target_market_registry_schema(conn, commit=False)
        execution_vector_repair = repair_legacy_blocked_execution_vectors(
            conn,
            commit=False,
        )
        manifest_schema = inspect_cash_phase_authority_manifest_schema(conn)
        if not manifest_schema.get("ok", False):
            raise MigrationError(
                "target_ssot_migration_cash_phase_authority_manifest_schema_failed:"
                + ",".join(str(item) for item in manifest_schema.get("missing", []))
            )
        schema_result = {
            **schema_result,
            "cash_phase_authority_manifest_schema": manifest_schema,
        }
        pool_result: dict[str, Any] | None = None
        if capital_balance_usdc is not None and capital_epoch_id is not None:
            pool_result = initialize_capital_pool(
                conn,
                balance_usdc=capital_balance_usdc,
                capital_epoch_id=capital_epoch_id,
                updated_at_utc=_utc_now(),
                commit=False,
            )
        after = inspect_migration(conn)
        if not after["production_ssot_schema"].get("ok") or not after["target_ledger_schema"].get("ok"):
            raise MigrationError("target_ssot_migration_postcheck_failed")
        if not after["blocked_execution_vector_lineage"].get("ok"):
            raise MigrationError(
                "target_ssot_migration_execution_vector_lineage_failed"
            )
        resource_after = _migration_resource_snapshot(source)
    except Exception:
        if own_transaction:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        if own_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE {savepoint}")
    return {
        "schema": "bayes_basket_target_ssot_migration_receipt_v1",
        "state": "migrated",
        "migration_at_utc": _utc_now(),
        "before": before,
        "after": after,
        "schema_result": schema_result,
        "cash_phase_authority_manifest_schema": schema_result[
            "cash_phase_authority_manifest_schema"
        ],
        "market_registry_schema": registry_schema_result,
        "execution_vector_repair": execution_vector_repair,
        "capital_pool": pool_result,
        "legacy_archive_table": archive if before["legacy_decision_table_collision"] else None,
        "backup_path": str(Path(backup_path).expanduser().resolve()),
        "backup": backup_receipt,
        "resource_before": resource_before,
        "resource_after": resource_after,
        "pause_file": str(pause_file.expanduser().resolve()),
        "pause_marker_token": PAUSE_MARKER_TOKEN,
        "writer_quiescence_receipt": validated_quiescence,
        "writes_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--plan", action="store_true", help="read-only migration plan; default when no confirmation is supplied")
    parser.add_argument("--confirm-target-ssot-migration", action="store_true")
    parser.add_argument("--writer-quiescence-receipt", type=Path, help="receipt produced by the read-only writer-quiescence probe")
    parser.add_argument("--backup-db", type=Path, help="new local backup path required for a mutating run")
    parser.add_argument("--pause-file", type=Path, default=DEFAULT_TARGET_PAUSE_MARKER)
    parser.add_argument(
        "--legacy-archive-table",
        default="",
        help="Legacy archive table; defaults to the current UTC date when a rename is required.",
    )
    parser.add_argument("--capital-balance-usdc", type=float)
    parser.add_argument("--capital-epoch-id")
    parser.add_argument("--receipt", type=Path, default=ROOT / "output/audits/bayes_basket_target_ssot_migration_receipt.json")
    args = parser.parse_args()
    legacy_archive_table = args.legacy_archive_table or (
        "auto_trade_decisions_legacy_" + datetime.now(timezone.utc).strftime("%Y%m%d")
    )
    db_path = args.db.expanduser().resolve()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        plan = inspect_migration(conn)
    if args.check_only or args.plan or not args.confirm_target_ssot_migration:
        output = {
            **plan,
            "state": "read_only_plan",
            "confirmation_required": True,
            "backup_required": True,
            "writer_quiescence_required": True,
            "pause_file": str(args.pause_file.expanduser().resolve()),
            "pause_marker_token": PAUSE_MARKER_TOKEN,
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0 if args.check_only or args.plan else 2
    if args.backup_db is None:
        print(json.dumps({"state": "blocked", "reason": "backup_db_required"}, ensure_ascii=False, sort_keys=True))
        return 2
    if args.writer_quiescence_receipt is None:
        print(json.dumps({"state": "blocked", "reason": "writer_quiescence_receipt_required"}, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        quiescence_receipt = json.loads(args.writer_quiescence_receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"state": "blocked", "reason": f"writer_quiescence_receipt_unreadable:{exc}"}, ensure_ascii=False, sort_keys=True))
        return 2
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        result = apply_controlled_migration(
            conn,
            db_path=db_path,
            backup_path=args.backup_db,
            pause_file=args.pause_file,
            legacy_archive_table=legacy_archive_table,
            confirmed=True,
            writer_quiescence_receipt=quiescence_receipt,
            capital_balance_usdc=args.capital_balance_usdc,
            capital_epoch_id=args.capital_epoch_id,
        )
    receipt_path = args.receipt.expanduser().resolve()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["receipt_path"] = str(receipt_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
