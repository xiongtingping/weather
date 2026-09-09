#!/usr/bin/env python3
"""Additive index-only migration for the target order ledger.

Two indexes have been declared in ``bayes_basket_target/ssot.py`` since
2026-07-30 and do not exist in the production database:

  * ``basket_target_reconciliation_state_idx`` (state, updated_at)
  * ``basket_target_order_event_intent_idx`` (order_intent_id, observed_at,
    event_id)

They are absent because both tables were rebuilt by a later twelve-step
ALTER migration -- the live DDL for each carries the quoted table name that
rebuild leaves behind -- and a rebuild drops the indexes it does not
recreate.  Nothing re-applies the DDL afterwards: the resident deliberately
never calls ``apply_production_ssot_schema``, so an index lost that way
stays lost, and the schema test that would catch it builds a fresh database
where the index is present.

The reconciliation one is read by the per-station quiescence guard, which
runs once per station on every evidence pass and every mid-loop quote
refresh.  Its plan against production today is ``SCAN r``; against the
declared schema it is ``SEARCH r USING INDEX basket_target_reconciliation_
state_idx``.  At 810 rows the difference is immaterial, which is exactly why
this is worth doing now rather than after it is not: this is the same shape
-- an unindexed predicate over an append-only ledger, read per scope, inside
the path that holds up a decision -- that has been found by measuring a
stall seven times in this repo.

Deliberately not applied: ``basket_probability_envelope_committed_scope_idx``
is the third declared index missing here, and production has
``basket_probability_envelope_asof_idx`` (station_id, event_date, state,
decision_at_utc, fresh_until_utc, stage) instead, which the envelope read
seeks on.  Building a second index over 417k rows to serve a query already
served would cost pages and buy nothing.

Applies only the two CREATE INDEX statements, verbatim from ssot.py, rather
than the whole SSOT DDL: that DDL carries table rebuilds and constraint
upgrades whose preconditions this migration has not checked.

The writer must be stopped: this takes BEGIN IMMEDIATE on a 38GB database,
and the repo's other index migrations refuse to race the resident for the
same reason.
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

# Verbatim from bayes_basket_target/ssot.py.  One spelling, copied rather
# than imported, because the DDL there is one script and this migration is
# entitled to exactly two statements out of it.
STATEMENTS = (
    (
        "basket_target_reconciliation_state_idx",
        "CREATE INDEX IF NOT EXISTS basket_target_reconciliation_state_idx"
        " ON basket_target_order_reconciliations(state,updated_at)",
    ),
    (
        "basket_target_order_event_intent_idx",
        "CREATE INDEX IF NOT EXISTS basket_target_order_event_intent_idx"
        " ON basket_target_order_events(order_intent_id,observed_at,event_id)",
    ),
)


# The counts each new index has to reproduce, and the table each is read
# from with the index suppressed.  Unary ``+`` on the indexed column is
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
        "basket_target_reconciliation_state_idx",
        "SELECT COUNT(*) FROM basket_target_order_reconciliations"
        " WHERE state IN ('pending','unknown')",
        "SELECT COUNT(*) FROM basket_target_order_reconciliations"
        " WHERE +state IN ('pending','unknown')",
    ),
    (
        "basket_target_order_event_intent_idx",
        "SELECT COUNT(*) FROM basket_target_order_events"
        " WHERE order_intent_id IS NOT NULL",
        "SELECT COUNT(*) FROM basket_target_order_events"
        " WHERE +order_intent_id IS NOT NULL",
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
                "order_ledger_index_migration_index_disagrees_with_table:" + name
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
        raise ContractError("order_ledger_index_migration_symlink_forbidden")
    database = requested.resolve(strict=True)
    is_production_target = database == DEFAULT_DB
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    if wal_bytes > MAX_TARGET_WAL_BYTES:
        raise ContractError("order_ledger_index_migration_wal_limit_exceeded")
    if free_bytes < MIN_FREE_BYTES:
        raise ContractError("order_ledger_index_migration_free_space_too_low")
    if is_production_target:
        if not confirmed_writer_stopped:
            raise ContractError(
                "order_ledger_index_migration_quiescence_confirmation_required"
            )
        if _service_running():
            raise ContractError("order_ledger_index_migration_writer_still_running")
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
                "order_ledger_index_migration_index_absent:" + ",".join(missing)
            )
        # Verified against the tables these indexes serve, not against the
        # database.
        #
        # ``PRAGMA quick_check`` is what the registry index migration runs,
        # and on this file it is the whole cost of the migration: measured
        # 2026-09-09, still scanning 38GB after twelve minutes inside the
        # BEGIN IMMEDIATE, with the resident stopped for the duration.  It
        # also answers a question this change cannot raise -- building two
        # indexes over 1,834 rows does not reach the pages it would inspect.
        #
        # What can go wrong here is an index that answers differently from
        # the table it indexes, so that is what is checked: each new index is
        # read through, and its count is compared with the same count taken
        # off the table with the index suppressed.  A mismatch fails the
        # transaction, and the whole thing takes milliseconds.
        verification = _verify_indexes_agree_with_their_tables(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    elapsed = time.perf_counter() - started
    return {
        "schema": "bayes_basket_target_order_ledger_index_migration_v1",
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
                    " AND name IN (?,?)",
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
