#!/usr/bin/env python3
"""Rehearse target M5 migration against the real legacy decision-table shape.

The source database is opened read-only.  Only the ``auto_trade_decisions``
table schema and rows are copied into a disposable local SQLite database;
the controlled migration then runs entirely inside that disposable database.
This proves the legacy-table preservation and target DDL boundary without
pausing a resident service or mutating ``data/archive.db``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.bayes_basket_target_production_ssot_apply import (  # noqa: E402
    PAUSE_MARKER_TOKEN,
    apply_controlled_migration,
)
from bayes_basket_target.migration_quiescence import synthetic_writer_quiescence_receipt  # noqa: E402


SCHEMA = "bayes_basket_target_real_legacy_shape_migration_rehearsal_v1"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_migration_rehearsal.json"
DEFAULT_EPOCH = "migration-rehearsal-capital-epoch-v1"
LEGACY_ARCHIVE_TABLE = "auto_trade_decisions_legacy_rehearsal"
# The rehearsal is deliberately bounded.  It must never turn a large shared
# live database into an unbounded disposable clone just to produce evidence.
MAX_SOURCE_DATABASE_BYTES = 1 * 1024 * 1024 * 1024
MAX_SOURCE_ROWS = 100_000
MAX_TRIAL_DATABASE_BYTES = 512 * 1024 * 1024
TARGET_DECISION_COLUMNS = frozenset(
    {
        "decision_id",
        "cycle_id",
        "plan_id",
        "station_id",
        "event_date",
        "strategy_lane",
        "decided_state",
        "will_order",
        "reason_codes_json",
        "details_json",
        "decision_sha256",
        "created_at",
    }
)


def _canonical_row(row: Iterable[Any]) -> bytes:
    return json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8") + b"\n"


def _table_digest(
    conn: sqlite3.Connection,
    table: str,
    *,
    max_rows: int | None = None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
        if max_rows is not None and count >= int(max_rows):
            raise RuntimeError("rehearsal_source_row_budget_exceeded")
        digest.update(_canonical_row(row))
        count += 1
    return count, digest.hexdigest()


def _quote_identifier(value: str) -> str:
    if not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in value):
        raise RuntimeError("legacy_source_table_name_invalid")
    return '"' + value.replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    quoted = _quote_identifier(table)
    return [str(item[1]) for item in conn.execute(f"PRAGMA table_info({quoted})")]


def _select_legacy_source_table(source: sqlite3.Connection) -> str:
    """Select a real legacy shape without mistaking target DDL for legacy data.

    After an earlier controlled migration, the shared database can contain a
    target-shaped ``auto_trade_decisions`` table plus a renamed legacy table.
    The rehearsal must copy the latter while keeping the source connection
    read-only.  Fresh legacy-only fixtures still use the canonical table.
    """

    table_names = {
        str(row[0])
        for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    canonical = "auto_trade_decisions"
    canonical_columns = set(_table_columns(source, canonical)) if canonical in table_names else set()
    if canonical in table_names and not TARGET_DECISION_COLUMNS.issubset(canonical_columns):
        return canonical

    legacy_candidates = sorted(
        name
        for name in table_names
        if name.startswith("auto_trade_decisions_legacy_")
        and not TARGET_DECISION_COLUMNS.issubset(set(_table_columns(source, name)))
    )
    if legacy_candidates:
        return legacy_candidates[-1]
    if canonical in table_names:
        raise RuntimeError("real_legacy_auto_trade_decisions_shape_missing")
    raise RuntimeError("legacy_auto_trade_decisions_missing")


def _copy_legacy_table(
    source: sqlite3.Connection,
    trial: sqlite3.Connection,
    *,
    source_table: str,
) -> dict[str, Any]:
    source_quoted = _quote_identifier(source_table)
    row = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (source_table,),
    ).fetchone()
    if not row or not str(row[0] or "").strip():
        raise RuntimeError("legacy_auto_trade_decisions_missing")
    create_sql = str(row[0])
    # The copied trial table intentionally uses the canonical target migration
    # input name, regardless of which legacy table was selected in the source.
    create_sql = create_sql.replace(source_quoted, '"auto_trade_decisions"', 1)
    trial.execute(create_sql)
    columns = _table_columns(source, source_table)
    if not columns:
        raise RuntimeError("legacy_auto_trade_decisions_columns_missing")
    quoted = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    placeholders = ",".join("?" for _ in columns)
    insert_sql = f'INSERT INTO "auto_trade_decisions" ({quoted}) VALUES ({placeholders})'
    copied = 0
    cursor = source.execute(f"SELECT * FROM {source_quoted} ORDER BY rowid")
    while True:
        batch = cursor.fetchmany(2000)
        if not batch:
            break
        trial.executemany(insert_sql, batch)
        # Commit each bounded batch in the disposable trial only.  The real
        # migration remains governed by the single controlled transaction;
        # batching here prevents a multi-gigabyte rollback journal when the
        # legacy JSON payload is copied for rehearsal.
        trial.commit()
        trial_db_row = trial.execute("PRAGMA database_list").fetchone()
        if trial_db_row:
            trial_path = Path(str(trial_db_row[2] or ""))
            if trial_path.is_file() and trial_path.stat().st_size > MAX_TRIAL_DATABASE_BYTES:
                raise RuntimeError("rehearsal_trial_database_budget_exceeded")
        copied += len(batch)
    return {"source_table": source_table, "columns": columns, "rows_copied": copied}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def run_rehearsal(
    *,
    source_db: Path,
    capital_balance_usdc: float = 30.0,
    capital_epoch_id: str = DEFAULT_EPOCH,
) -> dict[str, Any]:
    source = source_db.expanduser().resolve()
    if not source.exists() or source.is_symlink() or not source.is_file():
        raise RuntimeError("source_database_path_invalid")
    if float(capital_balance_usdc) <= 0:
        raise RuntimeError("rehearsal_capital_balance_must_be_positive")
    if not str(capital_epoch_id or "").strip():
        raise RuntimeError("rehearsal_capital_epoch_id_missing")
    if source.stat().st_size > MAX_SOURCE_DATABASE_BYTES:
        raise RuntimeError("rehearsal_source_database_budget_exceeded")
    if os.stat(source).st_dev != os.stat("/private/tmp").st_dev:
        raise RuntimeError("source_database_not_on_local_rehearsal_device")

    source_before: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="bayes-target-migration-", dir="/private/tmp") as temp:
        temp_root = Path(temp)
        trial_db = temp_root / "archive.rehearsal.db"
        backup_db = temp_root / "archive.before-migration.db"
        pause_file = temp_root / "live_paused.flag"
        pause_file.write_text(f"{PAUSE_MARKER_TOKEN}\nisolated-migration-rehearsal\n", encoding="utf-8")

        with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60.0) as source_conn:
            source_conn.execute("PRAGMA busy_timeout=60000")
            source_table = _select_legacy_source_table(source_conn)
            source_count, source_digest = _table_digest(
                source_conn,
                source_table,
                max_rows=MAX_SOURCE_ROWS,
            )
            source_before = {
                "legacy_table": source_table,
                "row_count": source_count,
                "rows_sha256": source_digest,
            }
            with sqlite3.connect(trial_db, timeout=60.0) as trial_conn:
                trial_conn.execute("PRAGMA busy_timeout=60000")
                copied = _copy_legacy_table(source_conn, trial_conn, source_table=source_table)
                receipt = apply_controlled_migration(
                    trial_conn,
                    db_path=trial_db,
                    backup_path=backup_db,
                    pause_file=pause_file,
                    legacy_archive_table=LEGACY_ARCHIVE_TABLE,
                    confirmed=True,
                    writer_quiescence_receipt=synthetic_writer_quiescence_receipt(trial_db),
                    capital_balance_usdc=float(capital_balance_usdc),
                    capital_epoch_id=capital_epoch_id,
                )
                if backup_db.stat().st_size > MAX_TRIAL_DATABASE_BYTES:
                    raise RuntimeError("rehearsal_backup_database_budget_exceeded")

                names = _table_names(trial_conn)
                target_ledger = {
                    "legacy_archive_present": LEGACY_ARCHIVE_TABLE in names,
                    "target_decision_present": "auto_trade_decisions" in names,
                    "target_tables_present": sorted(
                        name for name in names if name.startswith("basket_target_")
                    ),
                    "legacy_archive_rows": _table_digest(trial_conn, LEGACY_ARCHIVE_TABLE),
                    "target_decision_rows": int(
                        trial_conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0]
                    ),
                    "capital_pool": dict(
                        zip(
                            ("pool_id", "balance_usdc", "reserved_usdc", "capital_epoch_id", "version"),
                            trial_conn.execute(
                                "SELECT pool_id,balance_usdc,reserved_usdc,capital_epoch_id,version "
                                "FROM basket_target_capital_pool"
                            ).fetchone(),
                        )
                    ),
                    "order_intents": int(
                        trial_conn.execute("SELECT COUNT(*) FROM basket_target_order_intents").fetchone()[0]
                    ),
                    "orders_submitted": 0,
                }
                if target_ledger["legacy_archive_rows"] != (source_count, source_digest):
                    raise RuntimeError("legacy_rows_or_digest_not_preserved")
                if not target_ledger["legacy_archive_present"] or not target_ledger["target_decision_present"]:
                    raise RuntimeError("target_and_legacy_decision_authorities_not_both_present")
                if target_ledger["target_decision_rows"] != 0:
                    raise RuntimeError("target_decision_table_not_empty")
                if target_ledger["order_intents"] != 0:
                    raise RuntimeError("rehearsal_created_order_intents")
                pool = target_ledger["capital_pool"]
                if (
                    float(pool["balance_usdc"]) != float(capital_balance_usdc)
                    or float(pool["reserved_usdc"]) != 0.0
                    or str(pool["capital_epoch_id"]) != str(capital_epoch_id)
                ):
                    raise RuntimeError("capital_pool_rehearsal_mismatch")

            with sqlite3.connect(backup_db, uri=False, timeout=60.0) as backup_conn:
                # The backup is taken from the disposable trial database
                # before migration, where the selected source rows are always
                # copied under the canonical input name.
                backup_count, backup_digest = _table_digest(backup_conn, "auto_trade_decisions")
                if (backup_count, backup_digest) != (source_count, source_digest):
                    raise RuntimeError("pre_migration_backup_not_identical_to_source_legacy_rows")

    trial_db_removed = not trial_db.exists()
    backup_removed = not backup_db.exists()
    return {
        "schema": SCHEMA,
        "state": "passed",
        "source_database": str(source),
        "source_opened_read_only": True,
        "temporary_trial_database_removed": trial_db_removed,
        "temporary_backup_removed": backup_removed,
        "copied_legacy_shape": copied,
        "source_before": source_before,
        "migration_receipt": receipt,
        "target_ledger": target_ledger,
        "boundary": {
            "writes_source_database": False,
            "writes_production_database": False,
            "writes_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "execution_mode": "production_no_live",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--capital-balance-usdc", type=float, default=30.0)
    parser.add_argument("--capital-epoch-id", default=DEFAULT_EPOCH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_rehearsal(
        source_db=args.source_db,
        capital_balance_usdc=args.capital_balance_usdc,
        capital_epoch_id=args.capital_epoch_id,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": result["schema"],
        "state": result["state"],
        "source_rows": result["source_before"]["row_count"],
        "legacy_rows_preserved": result["target_ledger"]["legacy_archive_rows"][0],
        "target_decision_rows": result["target_ledger"]["target_decision_rows"],
        "orders_submitted": result["target_ledger"]["orders_submitted"],
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
