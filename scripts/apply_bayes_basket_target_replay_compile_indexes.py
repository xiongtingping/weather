#!/usr/bin/env python3
"""Additive index-only migration for the native P0 compile's two slow reads.

Charter 176.  Profiled 2026-09-21 on the live store, one native P0 compile
ran ~311 statements and 89% of its time was SQL; two statements were 75% of
that, and the compile runs once per packet (~90) every cycle -- 24% of a
healthy cycle, the largest single stage:

* a scope's settlement timezone, DISTINCTed off ``basket_target_market_
  registry``.  The registry appends a row per scope every refresh, ~880 by
  the event day, each ~1.6KB with its registry_json, and the scope index made
  every one a table lookup: 75ms a call warm.  Six other readers ask the
  same question (label and truth materializers, forward training-fact
  validation, the observation store, training facts, the OOS scan).
  ``basket_target_market_registry_scope_tz_idx`` covers it.
* the activations valid at the decision instant for one (station, season,
  lead bucket).  The asof index puts run_cycle before the validity window
  and the compile does not know the run cycle, so every activation the scope
  ever had was read (1,264 for CYYZ h24_48): 72ms a call warm.
  ``calibration_native_activation_window_idx`` ends on valid_until_utc, so
  "still valid" is a range seek.

Charter 179 adds a third, on the same table: the observed-high read asks
the scope's timezone *and source lane*, and every snapshot re-asks it for
every live scope (122-135 calls, 12.6-20.9s in the cycle after each
snapshot).  ``basket_target_market_registry_scope_lane_idx`` covers it.

All are declared in the schema DDL verbatim; the resident never applies
schema, so this is how they reach the live store.  Verified the way the
recycle-index migration verifies: each index is read through and its count
compared with the same count taken off the table with unary ``+``
suppressing it; a mismatch rolls the transaction back.

The writer must be stopped: this takes BEGIN IMMEDIATE and builds indexes
over ~536k and ~168k rows.  Rerunning it creates only what is missing.
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
MIN_FREE_BYTES = 1024 * 1024 * 1024

# Verbatim from the schema DDL (ssot.py, market_registry.py).  One spelling, copied rather
# than imported, because the DDL there is one script and this migration is
# entitled to exactly these statements out of it.
STATEMENTS = (
    (
        "basket_target_market_registry_scope_tz_idx",
        "CREATE INDEX IF NOT EXISTS"
        " basket_target_market_registry_scope_tz_idx"
        " ON basket_target_market_registry"
        "(station_id,event_date,settlement_timezone)",
    ),
    (
        "calibration_native_activation_window_idx",
        "CREATE INDEX IF NOT EXISTS"
        " calibration_native_activation_window_idx"
        " ON calibration_native_ensemble_activation"
        "(station_id,artifact_kind,season_bin,lead_bucket,valid_until_utc)",
    ),
    (
        "basket_target_market_registry_scope_lane_idx",
        "CREATE INDEX IF NOT EXISTS"
        " basket_target_market_registry_scope_lane_idx"
        " ON basket_target_market_registry"
        "(station_id,event_date,settlement_timezone,source_lane)",
    ),
)


# The counts each new index has to reproduce, and the table each is read
# from with the index suppressed.  The index side names its index with
# INDEXED BY: without it the planner is free to answer from an older index
# (it did, for one of these, on the first test run) and the check would
# prove nothing about the new one.  Unary ``+`` on the indexed column is
# SQLite's documented no-op that makes the planner ignore an index for one
# query, so the two sides are the same question asked two ways.
#
# It has to be the unary operator and nothing cleverer: ``state+0=state``
# looks like the same trick and is not one -- ``+`` is arithmetic on a TEXT
# column, so it coerces every non-numeric state to 0 and the predicate is
# false for every row.  Written that way this check failed its own probe
# database on the first run, which is the only reason it is documented here
# rather than repeated in the next migration.
_INDEX_AGREEMENT_CHECKS = (
    (
        "basket_target_market_registry_scope_tz_idx",
        "SELECT COUNT(*) FROM ("
        " SELECT DISTINCT station_id, event_date, settlement_timezone"
        " FROM basket_target_market_registry"
        " INDEXED BY basket_target_market_registry_scope_tz_idx"
        " WHERE station_id>='' AND settlement_timezone<>'')",
        "SELECT COUNT(*) FROM ("
        " SELECT DISTINCT station_id, event_date, settlement_timezone"
        " FROM basket_target_market_registry"
        " WHERE +station_id>='' AND +settlement_timezone<>'')",
    ),
    (
        "calibration_native_activation_window_idx",
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation"
        " INDEXED BY calibration_native_activation_window_idx"
        " WHERE station_id>='' AND artifact_kind='CROSS_NATIVE_ENSEMBLE'"
        " AND valid_until_utc>'2026-09-21T00:00:00Z'",
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation"
        " WHERE +station_id>='' AND +artifact_kind='CROSS_NATIVE_ENSEMBLE'"
        " AND +valid_until_utc>'2026-09-21T00:00:00Z'",
    ),
    (
        "basket_target_market_registry_scope_lane_idx",
        "SELECT COUNT(*) FROM ("
        " SELECT DISTINCT station_id, event_date, settlement_timezone, source_lane"
        " FROM basket_target_market_registry"
        " INDEXED BY basket_target_market_registry_scope_lane_idx"
        " WHERE station_id>='')",
        "SELECT COUNT(*) FROM ("
        " SELECT DISTINCT station_id, event_date, settlement_timezone, source_lane"
        " FROM basket_target_market_registry"
        " WHERE +station_id>='')",
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
                "replay_compile_index_migration_index_disagrees_with_table:" + name
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
        raise ContractError("replay_compile_index_migration_symlink_forbidden")
    database = requested.resolve(strict=True)
    is_production_target = database == DEFAULT_DB
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    if wal_bytes > MAX_TARGET_WAL_BYTES:
        raise ContractError("replay_compile_index_migration_wal_limit_exceeded")
    if free_bytes < MIN_FREE_BYTES:
        raise ContractError("replay_compile_index_migration_free_space_too_low")
    if is_production_target:
        if not confirmed_writer_stopped:
            raise ContractError(
                "replay_compile_index_migration_quiescence_confirmation_required"
            )
        if _service_running():
            raise ContractError("replay_compile_index_migration_writer_still_running")
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
        for _name, statement in STATEMENTS:
            conn.execute(statement)
        after = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        missing = [name for name, _ in STATEMENTS if name not in after]
        if missing:
            raise ContractError(
                "replay_compile_index_migration_index_absent:" + ",".join(missing)
            )
        # Verified against the tables these indexes serve, not against the
        # database: PRAGMA quick_check on this file is twelve minutes inside
        # the BEGIN IMMEDIATE (measured 2026-09-09 by the registry-index
        # migration) and answers a question adding an index cannot raise.
        # What can go wrong is an index that answers differently from its
        # table, so each new index is read through and its count compared
        # with the same count taken off the table with the index suppressed.
        # Unlike the recycle migration this is not milliseconds: the table
        # side of the registry check reads the whole ~870MB table once.
        # That is the price of the proof, paid with the writer already
        # stopped, and it is printed in the receipt's elapsed seconds.
        verification = _verify_indexes_agree_with_their_tables(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    elapsed = time.perf_counter() - started
    return {
        "schema": "bayes_basket_target_replay_compile_index_migration_v1",
        "state": "migrated",
        "database": str(database),
        "indexes_created": sorted(after - before),
        "indexes_already_present": sorted(
            {name for name, _ in STATEMENTS} & before
        ),
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
        present = []
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
