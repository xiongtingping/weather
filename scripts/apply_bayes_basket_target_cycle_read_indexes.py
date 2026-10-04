#!/usr/bin/env python3
"""Additive index-only migration for three production-cycle reads (2026-10-03).

User ruling 2026-10-03 「停机建索引」.  Measured on the live store the same
afternoon, each a read the resident makes every cycle against an index that
stops one column short:

* ``basket_order_state_snapshot_version_idx`` -- the forward training check
  finds an order-state snapshot by its version (``_table_row``, LIMIT 2 to
  refuse an ambiguous one); the as-of index stops at the state, so every
  committed snapshot of the station-day (200-800) was read from the table to
  compare the version: ~13 s a cycle cold.
* ``auto_trade_decisions_exposure_idx`` -- the basket's open exposure, once
  per compile, probes a decision per filled or resting order for four
  columns; through the primary key each probe read the whole ~5 KB decision
  row: 8-38 s a cycle cold.  Covering, so the probe stays in the index.
* ``calibration_forward_taf_capture_tx_idx`` -- "does this station issue TAF
  TX" walks every capture of a station without one (28 of 45) and reads each
  row for ``tx_c``: 1-3 s a cycle.  Partial on ``tx_c IS NOT NULL``.

All three are declared in the schema DDL verbatim; the resident never
applies schema, so this is how they reach the live store.  Verified the way
the replay-compile migration verifies: each index is read through and its
count compared with the same count taken off the table with unary ``+``
suppressing it; a mismatch rolls the transaction back.

The writer must be stopped (``launchctl bootout``: the service is KeepAlive):
this takes BEGIN IMMEDIATE and builds indexes over ~1.18M and ~139k rows.
Rerunning it creates only what is missing.
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
from bayes_basket_target.resource_bounds import MAX_TARGET_WAL_BYTES

DEFAULT_DB = (ROOT / "data" / "bayes_basket_target_ssot.db").resolve()
SERVICE_LABEL = f"gui/{os.getuid()}/com.weather.weather_basket_yes"
MIN_FREE_BYTES = 4 * 1024 * 1024 * 1024

# Verbatim from the schema DDL (ssot.py).
STATEMENTS = (
    (
        "basket_order_state_snapshot_version_idx",
        "CREATE INDEX IF NOT EXISTS basket_order_state_snapshot_version_idx\n"
        "ON basket_order_state_snapshot(station_id,event_date,order_state_version,state)",
    ),
    (
        "auto_trade_decisions_exposure_idx",
        "CREATE INDEX IF NOT EXISTS auto_trade_decisions_exposure_idx\n"
        "ON auto_trade_decisions(decision_id,event_date,plan_id,station_id,settlement_timezone)",
    ),
    (
        "calibration_forward_taf_capture_tx_idx",
        "CREATE INDEX IF NOT EXISTS calibration_forward_taf_capture_tx_idx"
        " ON calibration_forward_taf_capture(station_id) WHERE tx_c IS NOT NULL",
    ),
)

# The counts each new index has to reproduce, and the table each is read from
# with the index suppressed (unary ``+``; see the replay-compile migration for
# why it has to be exactly that operator).  The index side names its index
# with INDEXED BY so the planner cannot answer from an older one.
_INDEX_AGREEMENT_CHECKS = (
    (
        "basket_order_state_snapshot_version_idx",
        "SELECT COUNT(*) FROM basket_order_state_snapshot"
        " INDEXED BY basket_order_state_snapshot_version_idx"
        " WHERE station_id>='' AND state='COMMITTED'",
        "SELECT COUNT(*) FROM basket_order_state_snapshot"
        " WHERE +station_id>='' AND +state='COMMITTED'",
    ),
    (
        "auto_trade_decisions_exposure_idx",
        "SELECT COUNT(*) FROM auto_trade_decisions"
        " INDEXED BY auto_trade_decisions_exposure_idx"
        " WHERE decision_id>=''",
        "SELECT COUNT(*) FROM auto_trade_decisions"
        " WHERE +decision_id>=''",
    ),
    (
        "calibration_forward_taf_capture_tx_idx",
        "SELECT COUNT(*) FROM calibration_forward_taf_capture"
        " INDEXED BY calibration_forward_taf_capture_tx_idx"
        " WHERE station_id>='' AND tx_c IS NOT NULL",
        "SELECT COUNT(*) FROM calibration_forward_taf_capture"
        " WHERE +station_id>='' AND tx_c IS NOT NULL",
    ),
)


def _verify_indexes_agree_with_their_tables(
    conn: sqlite3.Connection,
) -> list[dict[str, object]]:
    """Each new index answers what its table answers, or the write rolls back."""

    receipts: list[dict[str, object]] = []
    for name, indexed_sql, table_sql in _INDEX_AGREEMENT_CHECKS:
        plan = [
            str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + indexed_sql)
        ]
        through_index = int(conn.execute(indexed_sql).fetchone()[0])
        through_table = int(conn.execute(table_sql).fetchone()[0])
        if through_index != through_table:
            raise ContractError(
                "cycle_read_index_migration_index_disagrees_with_table:" + name
            )
        receipts.append({
            "index": name,
            "rows": through_index,
            "planner_used_index": any(name in step for step in plan),
            "plan": plan,
        })
    return receipts


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
        raise ContractError("cycle_read_index_migration_symlink_forbidden")
    database = requested.resolve(strict=True)
    is_production_target = database == DEFAULT_DB
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    if wal_bytes > MAX_TARGET_WAL_BYTES:
        raise ContractError("cycle_read_index_migration_wal_limit_exceeded")
    if free_bytes < MIN_FREE_BYTES:
        raise ContractError("cycle_read_index_migration_free_space_too_low")
    if is_production_target:
        if not confirmed_writer_stopped:
            raise ContractError(
                "cycle_read_index_migration_quiescence_confirmation_required"
            )
        if _service_running():
            raise ContractError("cycle_read_index_migration_writer_still_running")
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
        built: list[dict[str, object]] = []
        for name, statement in STATEMENTS:
            step = time.perf_counter()
            conn.execute(statement)
            built.append({
                "index": name,
                "seconds": round(time.perf_counter() - step, 3),
                "already_present": name in before,
            })
        after = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        missing = [name for name, _ in STATEMENTS if name not in after]
        if missing:
            raise ContractError(
                "cycle_read_index_migration_index_absent:" + ",".join(missing)
            )
        verification = _verify_indexes_agree_with_their_tables(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    elapsed = time.perf_counter() - started
    return {
        "schema": "bayes_basket_target_cycle_read_index_migration_v1",
        "state": "migrated",
        "database": str(database),
        "indexes_created": sorted(after - before),
        "indexes_already_present": sorted(
            {name for name, _ in STATEMENTS} & before
        ),
        "build": built,
        "index_verification": verification,
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
        present: list[str] = []
        try:
            probe = sqlite3.connect(
                f"file:{args.db}?mode=ro", uri=True, timeout=30.0
            )
            present = sorted(
                str(row[0]) for row in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                    " AND name IN ("
                    + ",".join("?" for _ in STATEMENTS) + ")",
                    tuple(name for name, _ in STATEMENTS),
                )
            )
            probe.close()
        except sqlite3.Error as exc:
            present = [f"probe_failed:{exc}"]
        print(json.dumps({
            "state": "dry_run",
            "database": str(args.db),
            "indexes": [name for name, _ in STATEMENTS],
            "already_present": present,
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
