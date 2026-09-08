#!/usr/bin/env python3
"""Additive index-only migration for the target market registry.

The registry's DDL is idempotent (every statement is IF NOT EXISTS), but
nothing in the running system ever re-applies it: ``ingest_target_market_
registry`` fails closed on a missing table rather than creating one, so an
index added to the DDL after the table was first installed is never built.

This migration exists to build one such index.  ``basket_target_market_
registry_eligible_day_idx`` (event_date, station_id) WHERE source_eligible=1
answers the truth-increment probe, which had no seekable predicate against
either existing index -- the scope index leads with the station and this
read knows only a date -- and scanned all 429,165 rows every cycle: 54.7
seconds in one statement on 2026-09-08, inside a cycle whose decision loop
then lost its quote lease and took 43 of 44 scopes with it.

Applies the whole registry DDL rather than one CREATE INDEX, because the
DDL is the schema's single spelling and re-running it is what "idempotent"
was written to allow.  Everything already present is untouched.

The writer must be stopped: this takes BEGIN IMMEDIATE on a 38GB database
and builds an index over 429k rows, and the repo's other migrations refuse
to race the resident for the same reason.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.market_registry import (
    TABLE,
    apply_target_market_registry_schema,
)
from bayes_basket_target.resource_bounds import MAX_TARGET_WAL_BYTES

DEFAULT_DB = (ROOT / "data" / "bayes_basket_target_ssot.db").resolve()
SERVICE_LABEL = f"gui/{os.getuid()}/com.weather.weather_basket_yes"
MIN_FREE_BYTES = 1024 * 1024 * 1024
INDEX_NAME = "basket_target_market_registry_eligible_day_idx"


def _service_running() -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", SERVICE_LABEL],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def migrate(*, db_path: Path, confirmed_writer_stopped: bool) -> dict[str, object]:
    requested = db_path.expanduser()
    if requested.is_symlink():
        raise ContractError("registry_index_migration_symlink_forbidden")
    database = requested.resolve(strict=True)
    is_production_target = database == DEFAULT_DB
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    if wal_bytes > MAX_TARGET_WAL_BYTES:
        raise ContractError("registry_index_migration_wal_limit_exceeded")
    if free_bytes < MIN_FREE_BYTES:
        raise ContractError("registry_index_migration_free_space_too_low")
    if is_production_target:
        if not confirmed_writer_stopped:
            raise ContractError(
                "registry_index_migration_quiescence_confirmation_required"
            )
        if _service_running():
            raise ContractError("registry_index_migration_writer_still_running")
    conn = sqlite3.connect(database, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=FULL")
    started = time.perf_counter()
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        apply_target_market_registry_schema(conn, commit=False)
        after = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise ContractError("registry_index_migration_quick_check_failed")
        if INDEX_NAME not in after:
            raise ContractError("registry_index_migration_index_absent")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    elapsed = time.perf_counter() - started
    return {
        "schema": "bayes_basket_target_registry_index_migration_v1",
        "state": "migrated",
        "database": str(database),
        "table": TABLE,
        "indexes_created": sorted(after - before),
        "indexes_already_present": sorted(before & {INDEX_NAME}),
        "quick_check": quick_check,
        "elapsed_seconds": round(elapsed, 3),
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
        "--confirm-weather-basket-yes-stopped",
        action="store_true",
        help="the resident writer is stopped; required for the production DB",
    )
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({
            "state": "dry_run",
            "database": str(args.db),
            "index": INDEX_NAME,
            "writer_running": _service_running(),
            "note": "re-run with --apply and the confirmation flag",
        }, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(
        migrate(
            db_path=args.db,
            confirmed_writer_stopped=(
                args.confirm_weather_basket_yes_stopped
            ),
        ),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
