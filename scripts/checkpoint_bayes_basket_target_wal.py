#!/usr/bin/env python3
"""Bounded target-only WAL checkpoint for a quiescent maintenance window."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    resolve_target_database,
)
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    validate_writer_quiescence_receipt,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    MAX_TARGET_WAL_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_wal_checkpoint_v1"
MAX_PRECHECKPOINT_WAL_BYTES = MAX_TARGET_DB_BYTES


class TargetWalCheckpointError(RuntimeError):
    """Raised when a target-only checkpoint cannot be proven safe."""


def _sidecar_bytes(path: Path) -> int:
    if path.is_symlink():
        raise TargetWalCheckpointError(
            "target_wal_checkpoint_sidecar_symlink_forbidden"
        )
    try:
        return int(path.stat().st_size) if path.exists() else 0
    except OSError as exc:
        raise TargetWalCheckpointError(
            "target_wal_checkpoint_sidecar_stat_failed"
        ) from exc


def checkpoint_target_wal(
    *,
    db_path: Path,
    quiescence_receipt: Mapping[str, Any],
    max_wal_bytes: int = MAX_TARGET_WAL_BYTES,
) -> dict[str, Any]:
    """Checkpoint committed WAL frames without vacuuming or replacing the DB."""

    db = resolve_target_database(db_path)
    if not db.is_file() or db.is_symlink():
        raise TargetWalCheckpointError(
            "target_wal_checkpoint_database_path_invalid"
        )
    if not 0 <= int(max_wal_bytes) <= MAX_TARGET_WAL_BYTES:
        raise TargetWalCheckpointError(
            "target_wal_checkpoint_post_limit_invalid"
        )
    try:
        validate_writer_quiescence_receipt(
            quiescence_receipt,
            db_path=db,
            require_current_database_identity=True,
        )
    except ValueError as exc:
        raise TargetWalCheckpointError(str(exc)) from exc

    wal = db.with_name(db.name + "-wal")
    shm = db.with_name(db.name + "-shm")
    wal_bytes_before = _sidecar_bytes(wal)
    shm_bytes_before = _sidecar_bytes(shm)
    if wal_bytes_before > MAX_PRECHECKPOINT_WAL_BYTES:
        raise TargetWalCheckpointError(
            "target_wal_checkpoint_pre_wal_bytes_exceeded"
        )
    try:
        free_bytes_before = int(shutil.disk_usage(db.parent).free)
    except OSError as exc:
        raise TargetWalCheckpointError(
            "target_wal_checkpoint_disk_probe_failed"
        ) from exc
    if free_bytes_before < MIN_TARGET_FREE_BYTES:
        raise TargetWalCheckpointError(
            "target_wal_checkpoint_min_free_space_guard"
        )
    rss_bytes = int(
        assert_process_memory_budget(projected_bytes=4 * 1024 * 1024)
    )

    try:
        with sqlite3.connect(db, timeout=10.0, isolation_level=None) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            if [
                str(row[0]) for row in conn.execute("PRAGMA quick_check")
            ] != ["ok"]:
                raise TargetWalCheckpointError(
                    "target_wal_checkpoint_pre_quick_check_failed"
                )
            busy, log_frames, checkpointed_frames = conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if (
                int(busy) != 0
                or int(log_frames) != int(checkpointed_frames)
            ):
                raise TargetWalCheckpointError(
                    "target_wal_checkpoint_not_fully_checkpointed"
                )
            if [
                str(row[0]) for row in conn.execute("PRAGMA quick_check")
            ] != ["ok"]:
                raise TargetWalCheckpointError(
                    "target_wal_checkpoint_post_quick_check_failed"
                )
    except sqlite3.Error as exc:
        raise TargetWalCheckpointError(
            f"target_wal_checkpoint_sqlite_failed:{exc}"
        ) from exc

    wal_bytes_after = _sidecar_bytes(wal)
    shm_bytes_after = _sidecar_bytes(shm)
    database_bytes_after = int(db.stat().st_size)
    if database_bytes_after > MAX_TARGET_DB_BYTES:
        raise TargetWalCheckpointError(
            "target_wal_checkpoint_database_bytes_exceeded"
        )
    if wal_bytes_after > int(max_wal_bytes):
        raise TargetWalCheckpointError(
            "target_wal_checkpoint_post_wal_bytes_exceeded"
        )
    return {
        "schema": SCHEMA,
        "state": "CHECKPOINTED",
        "database": str(db),
        "mode": "TRUNCATE",
        "busy": int(busy),
        "log_frames": int(log_frames),
        "checkpointed_frames": int(checkpointed_frames),
        "database_bytes_after": database_bytes_after,
        "wal_bytes_before": wal_bytes_before,
        "wal_bytes_after": wal_bytes_after,
        "shm_bytes_before": shm_bytes_before,
        "shm_bytes_after": shm_bytes_after,
        "max_precheckpoint_wal_bytes": MAX_PRECHECKPOINT_WAL_BYTES,
        "max_postcheckpoint_wal_bytes": int(max_wal_bytes),
        "free_bytes_before": free_bytes_before,
        "rss_bytes": rss_bytes,
        "quick_check": "ok",
        "requires_fresh_post_checkpoint_quiescence_receipt": True,
        "boundary": {
            "target_database_only": True,
            "writer_quiescence_required": True,
            "vacuum_performed": False,
            "database_replaced": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "execution_mode": "production_no_live",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--quiescence-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-wal-bytes",
        type=int,
        default=MAX_TARGET_WAL_BYTES,
    )
    args = parser.parse_args()
    receipt = load_bounded_json(args.quiescence_receipt)
    if not isinstance(receipt, Mapping):
        raise SystemExit("target_wal_checkpoint_receipt_mapping_required")
    result = checkpoint_target_wal(
        db_path=args.db,
        quiescence_receipt=receipt,
        max_wal_bytes=args.max_wal_bytes,
    )
    write_bounded_json(args.output, result)
    print(json.dumps({
        "schema": result["schema"],
        "state": result["state"],
        "wal_bytes_before": result["wal_bytes_before"],
        "wal_bytes_after": result["wal_bytes_after"],
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
