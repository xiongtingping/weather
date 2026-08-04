#!/usr/bin/env python3
"""Narrow additive migration for DeltaBasketScore v2 authority only."""

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
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.execution_transition_runtime import (
    apply_execution_transition_schema,
)
from bayes_basket_target.resource_bounds import MAX_TARGET_WAL_BYTES
from bayes_basket_target.ssot import apply_delta_v2_authority_schema


DEFAULT_DB = (ROOT / "data" / "bayes_basket_target_ssot.db").resolve()
SERVICE_LABEL = f"gui/{os.getuid()}/com.weather.weather_basket_yes"
MAX_WAL_BYTES = MAX_TARGET_WAL_BYTES
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
        raise ContractError("delta_v2_migration_symlink_forbidden")
    database = requested.resolve(strict=True)
    is_production_target = database == DEFAULT_DB
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    if wal_bytes > MAX_WAL_BYTES:
        raise ContractError("delta_v2_migration_wal_limit_exceeded")
    if free_bytes < MIN_FREE_BYTES:
        raise ContractError("delta_v2_migration_free_space_too_low")
    if is_production_target:
        if not confirmed_writer_stopped:
            raise ContractError(
                "delta_v2_migration_quiescence_confirmation_required"
            )
        if _service_running():
            raise ContractError("delta_v2_migration_writer_still_running")
    conn = sqlite3.connect(database, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = apply_delta_v2_authority_schema(conn, commit=False)
        ledger_result = apply_target_ledger_schema(conn, commit=False)
        if ledger_result.get("ok") is not True:
            raise ContractError("delta_v2_migration_target_ledger_incomplete")
        transition_result = apply_execution_transition_schema(
            conn,
            commit=False,
        )
        if transition_result.get("state") != "READY":
            raise ContractError(
                "delta_v2_migration_execution_transition_incomplete"
            )
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise ContractError("delta_v2_migration_quick_check_failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        **result,
        "target_ledger_schema": ledger_result,
        "execution_transition_schema": transition_result,
        "state": "migrated",
        "database": str(database),
        "quick_check": quick_check,
        "wal_bytes_before": wal_bytes,
        "free_bytes_before": free_bytes,
        "production_target": is_production_target,
        "writer_confirmed_stopped": (
            confirmed_writer_stopped if is_production_target else None
        ),
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
            "schema": "delta_basket_score_authority_migration_v2",
            "state": "dry_run",
            "database": str(args.db.expanduser().resolve()),
            "requires_service_stopped_for_production": SERVICE_LABEL,
            "performs_legacy_backfill": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
        }, sort_keys=True))
        return 0
    result = migrate(
        db_path=args.db,
        confirmed_writer_stopped=(
            args.confirm_reported_trajectory_no_stopped
        ),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
