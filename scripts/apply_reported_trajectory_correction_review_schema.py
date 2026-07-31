#!/usr/bin/env python3
"""Narrow additive migration for correction-review authority only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError
from reported_trajectory_no_schema import (
    apply_reported_trajectory_correction_review_schema,
)


DEFAULT_DB = ROOT / "data" / "archive.db"
SERVICE_LABEL = f"gui/{os.getuid()}/com.weather.reported_trajectory_no"
MAX_ARCHIVE_WAL_BYTES = 512 * 1024 * 1024
MIN_ARCHIVE_FREE_BYTES = 1024 * 1024 * 1024


def _service_running() -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", SERVICE_LABEL],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def migrate(*, db_path: Path, confirmed_stopped: bool) -> dict:
    requested = db_path.expanduser()
    if requested.is_symlink():
        raise ContractError("correction_review_migration_symlink_forbidden")
    database = requested.resolve(strict=True)
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    if wal_bytes > MAX_ARCHIVE_WAL_BYTES:
        raise ContractError("correction_review_migration_wal_limit_exceeded")
    if free_bytes < MIN_ARCHIVE_FREE_BYTES:
        raise ContractError("correction_review_migration_free_space_too_low")
    if not confirmed_stopped:
        raise ContractError(
            "correction_review_migration_quiescence_confirmation_required"
        )
    if _service_running():
        raise ContractError(
            "correction_review_migration_writer_still_running"
        )
    conn = sqlite3.connect(
        database,
        timeout=30.0,
        isolation_level=None,
    )
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    try:
        result = apply_reported_trajectory_correction_review_schema(conn)
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise ContractError(
                "correction_review_migration_quick_check_failed"
            )
    finally:
        conn.close()
    return {
        **result,
        "state": "migrated",
        "database": str(database),
        "quick_check": quick_check,
        "wal_bytes_before": wal_bytes,
        "free_bytes_before": free_bytes,
        "reported_trajectory_no_confirmed_stopped": True,
        "performs_legacy_backfill": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-reported-trajectory-no-stopped",
        action="store_true",
    )
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({
            "schema": "reported_trajectory_correction_review_migration_v1",
            "state": "dry_run",
            "database": str(args.db.expanduser().resolve()),
            "requires_service_stopped": SERVICE_LABEL,
            "performs_legacy_backfill": False,
        }, sort_keys=True))
        return 0
    result = migrate(
        db_path=args.db,
        confirmed_stopped=args.confirm_reported_trajectory_no_stopped,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
