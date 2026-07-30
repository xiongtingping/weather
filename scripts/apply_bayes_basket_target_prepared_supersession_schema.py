#!/usr/bin/env python3
"""Add the bounded prepared-candidate supersession ledger contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.contracts import ContractError


DEFAULT_DB = (ROOT / "data" / "bayes_basket_target_ssot.db").resolve()
SERVICE_LABEL = "system/com.weather.reported_trajectory_no"
MAX_WAL_BYTES = 64 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024


def _service_running() -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", SERVICE_LABEL],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def migrate(
    *,
    db_path: Path,
    confirmed_writer_stopped: bool,
) -> dict[str, object]:
    requested = db_path.expanduser()
    if requested.is_symlink():
        raise ContractError("prepared_supersession_migration_symlink_forbidden")
    database = requested.resolve(strict=True)
    production_target = database == DEFAULT_DB
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    if wal_bytes > MAX_WAL_BYTES:
        raise ContractError(
            "prepared_supersession_migration_wal_limit_exceeded"
        )
    if free_bytes < MIN_FREE_BYTES:
        raise ContractError(
            "prepared_supersession_migration_free_space_too_low"
        )
    if production_target:
        if not confirmed_writer_stopped:
            raise ContractError(
                "prepared_supersession_migration_quiescence_required"
            )
        if _service_running():
            raise ContractError(
                "prepared_supersession_migration_writer_still_running"
            )

    with sqlite3.connect(
        database, timeout=30.0, isolation_level=None
    ) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        existed_before = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='basket_target_prepared_supersession_receipts'"
        ).fetchone() is not None
        try:
            conn.execute("BEGIN IMMEDIATE")
            status = apply_target_ledger_schema(conn, commit=False)
            if status.get("ok") is not True:
                raise ContractError(
                    "prepared_supersession_target_ledger_incomplete"
                )
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            if quick_check != "ok":
                raise ContractError(
                    "prepared_supersession_migration_quick_check_failed"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "schema": "basket_target_prepared_supersession_migration_v1",
        "state": "unchanged" if existed_before else "migrated",
        "database": str(database),
        "quick_check": quick_check,
        "production_target": production_target,
        "writer_confirmed_stopped": (
            confirmed_writer_stopped if production_target else None
        ),
        "wal_bytes_before": wal_bytes,
        "free_bytes_before": free_bytes,
        "performs_legacy_backfill": False,
        "calls_order_gateway": False,
        "places_orders": False,
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
            "schema": "basket_target_prepared_supersession_migration_v1",
            "state": "dry_run",
            "database": str(args.db.expanduser().resolve()),
            "requires_target_writer_stopped_for_production": True,
            "performs_legacy_backfill": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        }, sort_keys=True))
        return 0
    print(json.dumps(
        migrate(
            db_path=args.db,
            confirmed_writer_stopped=(
                args.confirm_reported_trajectory_no_stopped
            ),
        ),
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
