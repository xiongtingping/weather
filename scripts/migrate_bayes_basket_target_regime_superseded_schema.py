#!/usr/bin/env python3
"""Rehearse or explicitly apply the regime SUPERSEDED schema migration."""

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
    REGIME_ARTIFACT_COLUMNS,
    inspect_regime_superseded_schema,
    upgrade_regime_superseded_schema,
)


SCHEMA = "bayes_basket_target_regime_superseded_schema_migration_v1"
MAX_REGIME_ROWS = 100_000
MAX_REGIME_PAYLOAD_BYTES = 64 * 1024 * 1024
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
        raise MigrationError("regime_migration_database_path_invalid")
    db_bytes = int(path.stat().st_size)
    wal = path.with_name(path.name + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.is_file() else 0
    if db_bytes > MAX_TARGET_DB_BYTES:
        raise MigrationError("regime_migration_database_bytes_exceeded")
    if wal_bytes > MAX_TARGET_WAL_BYTES:
        raise MigrationError("regime_migration_wal_bytes_exceeded")
    projected_database_bytes = db_bytes * 2 + wal_bytes
    if projected_database_bytes > MAX_TARGET_DB_BYTES:
        raise MigrationError("regime_migration_projected_pages_exceeded")
    free_bytes = int(shutil.disk_usage(path.parent).free)
    required_free_bytes = (
        MIN_TARGET_FREE_BYTES
        + max(1, int(copies)) * (db_bytes + wal_bytes)
        + projected_database_bytes
    )
    if free_bytes < required_free_bytes:
        raise MigrationError("regime_migration_disk_free_space_insufficient")
    assert_process_memory_budget(projected_bytes=COPY_MEMORY_BUDGET_BYTES)
    return {
        "database_bytes": db_bytes,
        "wal_bytes": wal_bytes,
        "projected_database_bytes": projected_database_bytes,
        "free_bytes": free_bytes,
        "required_free_bytes": required_free_bytes,
    }


def _connection_gate(conn: sqlite3.Connection) -> dict[str, int]:
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    page_bytes = page_size * page_count
    if page_bytes > MAX_TARGET_DB_BYTES:
        raise MigrationError("regime_migration_page_budget_exceeded")
    conn.execute(f"PRAGMA max_page_count={MAX_TARGET_DB_BYTES // page_size}")
    row = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(artifact_json)),0) "
        "FROM basket_regime_artifact"
    ).fetchone()
    rows = int(row[0])
    payload_bytes = int(row[1])
    if rows > MAX_REGIME_ROWS:
        raise MigrationError("regime_migration_row_budget_exceeded")
    if payload_bytes > MAX_REGIME_PAYLOAD_BYTES:
        raise MigrationError("regime_migration_payload_budget_exceeded")
    return {
        "page_size": page_size,
        "page_count": page_count,
        "page_bytes": page_bytes,
        "regime_rows": rows,
        "regime_payload_bytes": payload_bytes,
    }


def _regime_digest(conn: sqlite3.Connection) -> dict[str, Any]:
    columns = ",".join(REGIME_ARTIFACT_COLUMNS)
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(
        f"SELECT {columns} FROM basket_regime_artifact "
        "ORDER BY artifact_id"
    ):
        count += 1
        if count > MAX_REGIME_ROWS:
            raise MigrationError("regime_migration_row_budget_exceeded")
        encoded = json.dumps(
            list(row),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise MigrationError("regime_migration_row_bytes_exceeded")
        digest.update(encoded)
        digest.update(b"\n")
    return {"rows": count, "sha256": digest.hexdigest()}


def _quick_check(conn: sqlite3.Connection) -> None:
    if [str(row[0]) for row in conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise MigrationError("regime_migration_quick_check_failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise MigrationError("regime_migration_foreign_key_check_failed")


def _clone_database(source: Path, destination: Path) -> None:
    with sqlite3.connect(
        f"file:{source}?mode=ro",
        uri=True,
        timeout=30.0,
    ) as source_conn:
        source_conn.execute("PRAGMA query_only=ON")
        with sqlite3.connect(destination, timeout=30.0) as target_conn:
            source_conn.backup(target_conn, pages=256, sleep=0.01)
            target_conn.commit()
    if int(destination.stat().st_size) > MAX_TARGET_DB_BYTES:
        raise MigrationError("regime_migration_clone_bytes_exceeded")


def run_rehearsal(*, source_db: Path) -> dict[str, Any]:
    source = resolve_target_database(source_db)
    resources = _resource_gate(source, copies=1)
    source_identity_before = _file_identity(source)
    with sqlite3.connect(
        f"file:{source}?mode=ro",
        uri=True,
        timeout=30.0,
    ) as source_conn:
        source_conn.execute("PRAGMA query_only=ON")
        source_schema = inspect_regime_superseded_schema(source_conn)
        source_digest = _regime_digest(source_conn)
        _quick_check(source_conn)

    with tempfile.TemporaryDirectory(
        prefix="regime-superseded-rehearsal-",
        dir="/private/tmp",
    ) as temporary:
        clone = Path(temporary) / "target-clone.db"
        _clone_database(source, clone)
        with sqlite3.connect(clone, timeout=30.0) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            clone_resources = _connection_gate(conn)
            clone_before = _regime_digest(conn)
            if clone_before != source_digest:
                raise MigrationError("regime_migration_clone_digest_drift")

            conn.execute("BEGIN IMMEDIATE")
            forward_then_rollback = upgrade_regime_superseded_schema(conn)
            if inspect_regime_superseded_schema(conn).get("ok") is not True:
                raise MigrationError("regime_migration_forward_probe_failed")
            conn.rollback()
            rollback_schema = inspect_regime_superseded_schema(conn)
            rollback_digest = _regime_digest(conn)
            if rollback_schema != source_schema or rollback_digest != clone_before:
                raise MigrationError("regime_migration_rollback_not_exact")

            conn.execute("BEGIN IMMEDIATE")
            forward = upgrade_regime_superseded_schema(conn)
            conn.commit()
            _quick_check(conn)
            after_forward = inspect_regime_superseded_schema(conn)
            after_digest = _regime_digest(conn)
            if after_forward.get("ok") is not True:
                raise MigrationError("regime_migration_forward_schema_invalid")
            if after_digest != clone_before:
                raise MigrationError("regime_migration_rows_not_preserved")

            conn.execute("BEGIN IMMEDIATE")
            second = upgrade_regime_superseded_schema(conn)
            conn.commit()
            if second.get("state") != "already_current":
                raise MigrationError("regime_migration_idempotency_failed")
            _quick_check(conn)
            if _regime_digest(conn) != after_digest:
                raise MigrationError("regime_migration_idempotent_digest_drift")
            final_resources = _connection_gate(conn)

    if _file_identity(source) != source_identity_before:
        raise MigrationError("regime_migration_source_changed_during_rehearsal")
    identity = {
        "schema": SCHEMA,
        "status": "PASS",
        "mode": "REHEARSAL",
        "source_database": str(source),
        "source_opened_read_only": True,
        "source_schema_before": source_schema,
        "source_regime_digest": source_digest,
        "forward_then_rollback": forward_then_rollback,
        "rollback_exact": True,
        "forward": forward,
        "second_forward": second,
        "resources": resources,
        "clone_resources_before": clone_resources,
        "clone_resources_after": final_resources,
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
) -> dict[str, Any]:
    database = resolve_target_database(db_path)
    backup = backup_path.expanduser().resolve()
    if backup == database or backup.exists() or backup.is_symlink():
        raise MigrationError("regime_migration_backup_path_invalid")
    resources = _resource_gate(database, copies=1)
    quiescence = isolated_target_writer_quiescence_receipt(database)
    validate_writer_quiescence_receipt(
        quiescence,
        db_path=database,
        require_current_database_identity=True,
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        _clone_database(database, backup)
        validate_writer_quiescence_receipt(
            quiescence,
            db_path=database,
            require_current_database_identity=True,
        )
        with sqlite3.connect(database, timeout=5.0) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN EXCLUSIVE")
            try:
                connection_resources = _connection_gate(conn)
                before = _regime_digest(conn)
                result = upgrade_regime_superseded_schema(conn)
                _quick_check(conn)
                after = _regime_digest(conn)
                if after != before:
                    raise MigrationError(
                        "regime_migration_apply_row_digest_drift"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception:
        backup.unlink(missing_ok=True)
        raise
    identity = {
        "schema": SCHEMA,
        "status": "PASS",
        "mode": "APPLY_PRODUCTION_NO_LIVE",
        "database": str(database),
        "backup": str(backup),
        "migration": result,
        "regime_digest": after,
        "resources": resources,
        "connection_resources": connection_resources,
        "quiescence_receipt_sha256": quiescence["receipt_sha256"],
        "boundary": {
            "writer_quiescence_required": True,
            "writes_only_regime_schema": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _blocked(
    *,
    mode: str,
    error: Exception,
) -> dict[str, Any]:
    identity = {
        "schema": SCHEMA,
        "status": "BLOCKED",
        "mode": mode,
        "blockers": [f"{type(error).__name__}:{error}"],
        "boundary": {
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
    args = parser.parse_args()
    mode = (
        "APPLY_PRODUCTION_NO_LIVE"
        if args.apply_production_no_live
        else "REHEARSAL"
    )
    try:
        if args.apply_production_no_live:
            if args.backup is None:
                raise MigrationError(
                    "regime_migration_apply_backup_required"
                )
            receipt = apply_migration(
                db_path=args.db,
                backup_path=args.backup,
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
        receipt = _blocked(mode=mode, error=exc)
    write_bounded_json(args.output, receipt, limit=MAX_RECEIPT_BYTES)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
