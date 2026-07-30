#!/usr/bin/env python3
"""Rehearse or apply the row-preserving five-model calibration migration."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    isolated_target_writer_quiescence_receipt,
    validate_writer_quiescence_receipt,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    MAX_TARGET_WAL_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    write_bounded_json,
)
from bayes_basket_target.ssot import (  # noqa: E402
    CALIBRATION_FIVE_MODEL_TABLES,
    inspect_calibration_model_pool_schema,
    upgrade_calibration_model_pool_schema_preserving_rows,
)


SCHEMA = "bayes_basket_target_five_model_pool_schema_migration_v1"
CONFIRMATION = "confirm-five-model-pool-production-no-live"
MAX_ROWS_PER_TABLE = 1_000_000
MAX_ROW_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
COPY_MEMORY_BUDGET_BYTES = 8 * 1024 * 1024


class MigrationError(RuntimeError):
    pass


def _file_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _resource_gate(path: Path, *, copies: int) -> dict[str, int]:
    if not path.is_file() or path.is_symlink():
        raise MigrationError("five_model_migration_database_path_invalid")
    db_bytes = int(path.stat().st_size)
    wal = path.with_name(path.name + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.is_file() else 0
    if db_bytes > MAX_TARGET_DB_BYTES:
        raise MigrationError("five_model_migration_database_bytes_exceeded")
    if wal_bytes > MAX_TARGET_WAL_BYTES:
        raise MigrationError("five_model_migration_wal_bytes_exceeded")
    free_bytes = int(shutil.disk_usage(path.parent).free)
    required = (
        MIN_TARGET_FREE_BYTES
        + max(1, copies) * (db_bytes + wal_bytes)
        + db_bytes
    )
    if free_bytes < required:
        raise MigrationError("five_model_migration_disk_free_space_insufficient")
    assert_process_memory_budget(projected_bytes=COPY_MEMORY_BUDGET_BYTES)
    return {
        "database_bytes": db_bytes,
        "wal_bytes": wal_bytes,
        "free_bytes": free_bytes,
        "required_free_bytes": required,
    }


def _digest(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for table in CALIBRATION_FIVE_MODEL_TABLES:
        columns = [
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        ]
        if not columns:
            raise MigrationError(f"five_model_migration_table_missing:{table}")
        primary_key = next(
            (
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})")
                if int(row[5] or 0) == 1
            ),
            columns[0],
        )
        digest = hashlib.sha256()
        count = 0
        for row in conn.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {primary_key}"
        ):
            count += 1
            if count > MAX_ROWS_PER_TABLE:
                raise MigrationError(
                    f"five_model_migration_row_budget_exceeded:{table}"
                )
            encoded = json.dumps(
                list(row), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded) > MAX_ROW_BYTES:
                raise MigrationError(
                    f"five_model_migration_row_bytes_exceeded:{table}"
                )
            digest.update(encoded)
            digest.update(b"\n")
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def _quick_check(conn: sqlite3.Connection) -> None:
    if [str(row[0]) for row in conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise MigrationError("five_model_migration_quick_check_failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise MigrationError("five_model_migration_foreign_key_check_failed")


def _clone(source: Path, destination: Path) -> None:
    with sqlite3.connect(
        f"file:{source}?mode=ro", uri=True, timeout=30.0
    ) as source_conn:
        source_conn.execute("PRAGMA query_only=ON")
        with sqlite3.connect(destination, timeout=30.0) as target_conn:
            source_conn.backup(target_conn, pages=256, sleep=0.01)
            target_conn.commit()
    if int(destination.stat().st_size) > MAX_TARGET_DB_BYTES:
        raise MigrationError("five_model_migration_clone_bytes_exceeded")


def run_rehearsal(*, source_db: Path) -> dict[str, Any]:
    source = resolve_target_database(source_db)
    resources = _resource_gate(source, copies=1)
    source_identity = _file_identity(source)
    with sqlite3.connect(
        f"file:{source}?mode=ro", uri=True, timeout=30.0
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        before_schema = inspect_calibration_model_pool_schema(conn)
        before_digest = _digest(conn)
        _quick_check(conn)

    with tempfile.TemporaryDirectory(
        prefix="five-model-pool-rehearsal-", dir="/private/tmp"
    ) as temporary:
        clone = Path(temporary) / "target-clone.db"
        _clone(source, clone)
        with sqlite3.connect(clone, timeout=30.0) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            if _digest(conn) != before_digest:
                raise MigrationError("five_model_migration_clone_digest_drift")

            conn.execute("BEGIN IMMEDIATE")
            rollback_probe = upgrade_calibration_model_pool_schema_preserving_rows(
                conn
            )
            if inspect_calibration_model_pool_schema(conn).get("ok") is not True:
                raise MigrationError("five_model_migration_forward_probe_failed")
            conn.rollback()
            if (
                inspect_calibration_model_pool_schema(conn) != before_schema
                or _digest(conn) != before_digest
            ):
                raise MigrationError("five_model_migration_rollback_not_exact")

            conn.execute("BEGIN IMMEDIATE")
            forward = upgrade_calibration_model_pool_schema_preserving_rows(conn)
            conn.commit()
            _quick_check(conn)
            if _digest(conn) != before_digest:
                raise MigrationError("five_model_migration_rows_not_preserved")
            if inspect_calibration_model_pool_schema(conn).get("ok") is not True:
                raise MigrationError("five_model_migration_schema_invalid")

            conn.execute("BEGIN IMMEDIATE")
            second = upgrade_calibration_model_pool_schema_preserving_rows(conn)
            conn.commit()
            if second.get("state") != "already_current":
                raise MigrationError("five_model_migration_idempotency_failed")
            _quick_check(conn)

    if _file_identity(source) != source_identity:
        raise MigrationError("five_model_migration_source_changed")
    identity = {
        "schema": SCHEMA,
        "status": "PASS",
        "mode": "REHEARSAL",
        "source_database": str(source),
        "source_opened_read_only": True,
        "source_schema_before": before_schema,
        "source_digest": before_digest,
        "forward_then_rollback": rollback_probe,
        "rollback_exact": True,
        "forward": forward,
        "second_forward": second,
        "resources": resources,
        "temporary_clone_removed": not clone.exists(),
        "boundary": {
            "writes_source_database": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def apply_migration(
    *,
    db_path: Path,
    backup_path: Path,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != CONFIRMATION:
        raise MigrationError("five_model_migration_confirmation_required")
    database = resolve_target_database(db_path)
    backup = backup_path.expanduser().resolve()
    if backup == database or backup.exists() or backup.is_symlink():
        raise MigrationError("five_model_migration_backup_path_invalid")
    resources = _resource_gate(database, copies=1)
    quiescence = isolated_target_writer_quiescence_receipt(database)
    validate_writer_quiescence_receipt(
        quiescence,
        db_path=database,
        require_current_database_identity=True,
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        _clone(database, backup)
        validate_writer_quiescence_receipt(
            quiescence,
            db_path=database,
            require_current_database_identity=True,
        )
        with sqlite3.connect(database, timeout=30.0) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            before = _digest(conn)
            conn.execute("BEGIN EXCLUSIVE")
            try:
                migration = (
                    upgrade_calibration_model_pool_schema_preserving_rows(conn)
                )
                if _digest(conn) != before:
                    raise MigrationError(
                        "five_model_migration_apply_digest_drift"
                    )
                _quick_check(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    identity = {
        "schema": SCHEMA,
        "status": "PASS",
        "mode": "APPLY_PRODUCTION_NO_LIVE",
        "database": str(database),
        "backup": str(backup),
        "migration": migration,
        "digest": before,
        "resources": resources,
        "quiescence_receipt_sha256": quiescence["receipt_sha256"],
        "boundary": {
            "writer_quiescence_required": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply-production-no-live", action="store_true")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    mode = (
        "APPLY_PRODUCTION_NO_LIVE"
        if args.apply_production_no_live
        else "REHEARSAL"
    )
    try:
        if args.apply_production_no_live:
            if args.backup is None:
                raise MigrationError("five_model_migration_backup_required")
            receipt = apply_migration(
                db_path=args.db,
                backup_path=args.backup,
                confirmation=args.confirmation,
            )
        else:
            receipt = run_rehearsal(source_db=args.db)
    except (
        MigrationError,
        RuntimeError,
        ValueError,
        sqlite3.Error,
        OSError,
    ) as exc:
        receipt = {
            "schema": SCHEMA,
            "status": "BLOCKED",
            "mode": mode,
            "blockers": [f"{type(exc).__name__}:{exc}"],
            "boundary": {
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "places_orders": False,
            },
        }
        receipt["content_sha256"] = content_sha256(receipt)
    write_bounded_json(args.output, receipt, limit=MAX_RECEIPT_BYTES)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
