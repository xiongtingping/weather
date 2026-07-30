#!/usr/bin/env python3
"""Restore one target SQLite backup with bounded logical-equivalence proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    resolve_target_database,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
)


SCHEMA = "bayes_basket_target_backup_restore_v1"


class TargetBackupRestoreError(RuntimeError):
    """Raised when the target backup cannot be restored exactly and safely."""


def _quick_check(connection: sqlite3.Connection, *, role: str) -> None:
    if [
        str(row[0]) for row in connection.execute("PRAGMA quick_check")
    ] != ["ok"]:
        raise TargetBackupRestoreError(
            f"target_backup_restore_{role}_quick_check_failed"
        )


def _logical_fingerprint(
    connection: sqlite3.Connection,
    *,
    role: str,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    line_count = 0
    try:
        for line in connection.iterdump():
            encoded = line.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            line_count += 1
    except (sqlite3.Error, UnicodeError) as exc:
        raise TargetBackupRestoreError(
            f"target_backup_restore_{role}_logical_scan_failed"
        ) from exc
    return {
        "sha256": digest.hexdigest(),
        "line_count": line_count,
        "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
        "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
        "freelist_count": int(
            connection.execute("PRAGMA freelist_count").fetchone()[0]
        ),
        "user_version": int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        ),
    }


def restore_target_backup(
    *,
    backup_path: Path,
    db_path: Path,
) -> dict[str, Any]:
    backup_candidate = backup_path.expanduser()
    target_candidate = db_path.expanduser()
    if backup_candidate.is_symlink() or target_candidate.is_symlink():
        raise TargetBackupRestoreError(
            "target_backup_restore_path_invalid"
        )
    backup = backup_candidate.resolve()
    target = resolve_target_database(target_candidate)
    if (
        not backup.is_file()
        or not target.is_file()
        or backup.parent != target.parent
        or backup == target
    ):
        raise TargetBackupRestoreError(
            "target_backup_restore_path_invalid"
        )
    backup_bytes = int(backup.stat().st_size)
    if backup_bytes <= 0 or backup_bytes > MAX_TARGET_DB_BYTES:
        raise TargetBackupRestoreError(
            "target_backup_restore_size_invalid"
        )
    try:
        free_bytes = int(shutil.disk_usage(target.parent).free)
    except OSError as exc:
        raise TargetBackupRestoreError(
            "target_backup_restore_disk_probe_failed"
        ) from exc
    if free_bytes < MIN_TARGET_FREE_BYTES:
        raise TargetBackupRestoreError(
            "target_backup_restore_min_free_space_guard"
        )
    rss_bytes = int(
        assert_process_memory_budget(projected_bytes=8 * 1024 * 1024)
    )

    try:
        with sqlite3.connect(
            f"file:{backup}?mode=ro",
            uri=True,
            timeout=60.0,
        ) as source:
            _quick_check(source, role="source")
            source_fingerprint = _logical_fingerprint(
                source,
                role="source",
            )
            with sqlite3.connect(
                target,
                timeout=60.0,
                isolation_level=None,
            ) as destination:
                destination.execute("PRAGMA busy_timeout=60000")
                source.backup(destination)
                busy, log_frames, checkpointed_frames = destination.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if (
                    int(busy) != 0
                    or int(log_frames) != int(checkpointed_frames)
                ):
                    raise TargetBackupRestoreError(
                        "target_backup_restore_checkpoint_busy"
                    )
                _quick_check(destination, role="target")
                target_fingerprint = _logical_fingerprint(
                    destination,
                    role="target",
                )
    except sqlite3.Error as exc:
        raise TargetBackupRestoreError(
            f"target_backup_restore_sqlite_failed:{exc}"
        ) from exc

    if target_fingerprint != source_fingerprint:
        raise TargetBackupRestoreError(
            "target_backup_restore_logical_fingerprint_mismatch"
        )
    wal = target.with_name(target.name + "-wal")
    shm = target.with_name(target.name + "-shm")
    if wal.is_symlink() or shm.is_symlink():
        raise TargetBackupRestoreError(
            "target_backup_restore_sidecar_symlink_forbidden"
        )
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    if wal_bytes != 0:
        raise TargetBackupRestoreError(
            "target_backup_restore_wal_not_truncated"
        )
    return {
        "schema": SCHEMA,
        "state": "RESTORED_EXACT",
        "database": str(target),
        "backup": str(backup),
        "backup_bytes": backup_bytes,
        "logical_fingerprint": source_fingerprint,
        "wal_bytes_after": wal_bytes,
        "shm_bytes_after": int(shm.stat().st_size) if shm.exists() else 0,
        "free_bytes_before": free_bytes,
        "rss_bytes": rss_bytes,
        "quick_check": "ok",
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-db", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    args = parser.parse_args()
    try:
        result = restore_target_backup(
            backup_path=args.backup_db,
            db_path=args.db,
        )
    except Exception as exc:
        print(json.dumps({
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
