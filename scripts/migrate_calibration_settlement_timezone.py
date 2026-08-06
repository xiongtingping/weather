#!/usr/bin/env python3
"""Add the missing ``settlement_timezone`` column to two calibration tables.

``ssot.DDL`` has declared ``settlement_timezone TEXT NOT NULL`` on
``calibration_historical_asof_model_run`` and
``calibration_station_daily_high_truth`` for some time, but it installs schema
with ``CREATE TABLE IF NOT EXISTS``.  Both tables predate the column, so it was
never added and ``production_ssot_schema_status`` reported
``calibration_base_schema_ok: false`` -- which fails the target writer open and
blocks the entire production mainline.

The value is not invented here.  ``calibration_ingress`` already requires every
ingested row to carry ``target_timezone`` equal to the station's entry in
``calibration_station_market_timezone_binding``, and ``content_sha256`` is
computed over that payload.  Backfilling from the same binding therefore
restores a column the content address already commits to, rather than guessing
one: the charter's ban on default-filling is not in play, and hashes must not
be recomputed.

SQLite cannot ``ADD COLUMN`` a NOT NULL column without a default, so each table
is rebuilt.  Both are append-only (``no_update``/``no_delete`` triggers), so the
triggers are dropped and reinstalled from the same authoritative DDL.

Everything runs in one transaction and rolls back unless, for every table:
  * the row count is unchanged;
  * the set of ``content_sha256`` values is unchanged;
  * every row's ``settlement_timezone`` is non-empty and equals the binding.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import ssot  # noqa: E402

SCHEMA = "calibration_settlement_timezone_backfill_receipt_v1"
CONFIRMATION = "migrate-calibration-settlement-timezone-production-no-live"
BINDING_TABLE = "calibration_station_market_timezone_binding"
TABLES = (
    "calibration_historical_asof_model_run",
    "calibration_station_daily_high_truth",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_statements(table: str) -> tuple[str, tuple[str, ...]]:
    """Pull the authoritative CREATE TABLE and its triggers out of ssot.DDL."""

    ddl = ssot.DDL
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((?:[^;]*?)\n\);",
        ddl,
        re.DOTALL,
    )
    if match is None:
        raise SystemExit(f"authoritative_ddl_not_found:{table}")
    create_sql = match.group(0)
    if "settlement_timezone" not in create_sql:
        raise SystemExit(f"authoritative_ddl_missing_settlement_timezone:{table}")
    triggers = tuple(
        m.group(0)
        for m in re.finditer(
            rf"CREATE TRIGGER IF NOT EXISTS \w+\s+BEFORE (?:UPDATE|DELETE) ON {table}\b.*?END;",
            ddl,
            re.DOTALL,
        )
    )
    if not triggers:
        raise SystemExit(f"authoritative_ddl_missing_triggers:{table}")
    return create_sql, triggers


def existing_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def migrate_table(conn: sqlite3.Connection, table: str) -> dict[str, object]:
    columns = existing_columns(conn, table)
    if "settlement_timezone" in columns:
        return {"table": table, "state": "already_present", "migrated_rows": 0}

    create_sql, trigger_sqls = extract_statements(table)
    before_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    before_hashes = {
        str(row[0]) for row in conn.execute(f"SELECT content_sha256 FROM {table}")
    }

    uncovered = conn.execute(
        f"""SELECT COUNT(*) FROM {table} t
             LEFT JOIN {BINDING_TABLE} b ON b.station_id = t.station_id
            WHERE b.settlement_timezone IS NULL
               OR TRIM(b.settlement_timezone) = ''"""
    ).fetchone()[0]
    if uncovered:
        raise SystemExit(f"station_timezone_binding_incomplete:{table}:{uncovered}")

    staging = f"{table}__settlement_timezone_new"
    conn.execute(f"DROP TABLE IF EXISTS {staging}")
    conn.execute(create_sql.replace(f"IF NOT EXISTS {table} (", f"IF NOT EXISTS {staging} ("))

    target_columns = existing_columns(conn, staging)
    if "settlement_timezone" not in target_columns:
        raise SystemExit(f"staging_missing_settlement_timezone:{table}")
    carried = [c for c in target_columns if c != "settlement_timezone"]
    unexpected = sorted(set(carried) - set(columns))
    if unexpected:
        # A column the old table cannot supply means this migration is not the
        # right tool; fail closed rather than inventing values for it.
        raise SystemExit(f"unmapped_target_columns:{table}:{','.join(unexpected)}")

    select_list = ", ".join(f"t.{c}" for c in carried)
    insert_list = ", ".join([*carried, "settlement_timezone"])
    conn.execute(
        f"""INSERT INTO {staging} ({insert_list})
            SELECT {select_list}, b.settlement_timezone
              FROM {table} t
              JOIN {BINDING_TABLE} b ON b.station_id = t.station_id"""
    )

    after_count = conn.execute(f"SELECT COUNT(*) FROM {staging}").fetchone()[0]
    if after_count != before_count:
        raise SystemExit(f"row_count_changed:{table}:{before_count}->{after_count}")
    after_hashes = {
        str(row[0]) for row in conn.execute(f"SELECT content_sha256 FROM {staging}")
    }
    if after_hashes != before_hashes:
        raise SystemExit(f"content_sha256_set_changed:{table}")
    bad_timezone = conn.execute(
        f"""SELECT COUNT(*) FROM {staging} s
             JOIN {BINDING_TABLE} b ON b.station_id = s.station_id
            WHERE s.settlement_timezone IS NULL
               OR TRIM(s.settlement_timezone) = ''
               OR s.settlement_timezone <> b.settlement_timezone"""
    ).fetchone()[0]
    if bad_timezone:
        raise SystemExit(f"settlement_timezone_mismatch:{table}:{bad_timezone}")

    for name, in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(f"DROP TABLE {table}")
    # Other tables carry triggers that reference this one by name.  Between the
    # DROP and the RENAME those references dangle, and modern ALTER TABLE
    # re-validates every trigger in the schema and refuses.  Legacy rename does
    # the one thing wanted here -- rename the table, touch nothing else -- and
    # the references resolve again the moment the name is restored.
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute(f"ALTER TABLE {staging} RENAME TO {table}")
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
    for trigger_sql in trigger_sqls:
        conn.execute(trigger_sql)

    return {
        "table": table,
        "state": "migrated",
        "migrated_rows": after_count,
        "carried_columns": len(carried),
        "triggers_reinstalled": len(trigger_sqls),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "bayes_basket_target_ssot.db")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and args.confirm != CONFIRMATION:
        print(f"refusing without --confirm {CONFIRMATION}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db, timeout=60.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=OFF")
    results: list[dict[str, object]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in TABLES:
            results.append(migrate_table(conn, table))
        if args.dry_run:
            conn.execute("ROLLBACK")
            state = "dry_run_rolled_back"
        else:
            conn.execute("COMMIT")
            state = "committed"
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()

    receipt = {
        "schema": SCHEMA,
        "state": state,
        "database": str(args.db),
        "tables": results,
        "calls_order_gateway": False,
        "places_orders": False,
        "at": _utc_now(),
    }
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
