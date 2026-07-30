#!/usr/bin/env python3
"""Retire the pre-timezone hard-NO decision table in one bounded transaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import (  # noqa: E402
    MIB,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    write_bounded_json,
)

DEFAULT_DB = ROOT / "data/archive.db"
PAUSE_MARKER = ROOT / "data/bayes_basket_target_migration_paused.flag"
LIVE_PAUSE = ROOT / "data/live_paused.flag"
ARCHIVE_TABLE = "auto_trade_decisions_legacy_no_settlement_timezone_v1"
CONFIRM = "MIGRATE_HARD_NO_TIMEZONE_NO_LIVE"
MAX_HARD_NO_BACKUP_ROWS = 4096
MAX_HARD_NO_BACKUP_PAYLOAD_BYTES = 16 * MIB
MAX_HARD_NO_BACKUP_BYTES = 32 * MIB
BACKUP_BATCH_ROWS = 128
EXPECTED_OLD_COLUMNS = (
    "decision_id", "cycle_id", "plan_id", "station_id", "event_date",
    "strategy_lane", "decided_state", "will_order", "reason_codes_json",
    "details_json", "decision_sha256", "created_at",
)
NEW_STATEMENTS = (
"""CREATE TABLE auto_trade_decisions (
  decision_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL UNIQUE,
  plan_id TEXT NOT NULL UNIQUE,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  strategy_lane TEXT NOT NULL CHECK(strategy_lane IN ('basket','hard_no')),
  decided_state TEXT NOT NULL CHECK(decided_state IN (
    'prepared','blocked','submitting','submitted','reconcile_required','settled'
  )),
  will_order INTEGER NOT NULL CHECK(will_order IN (0,1)),
  reason_codes_json TEXT NOT NULL,
  details_json TEXT NOT NULL,
  decision_sha256 TEXT NOT NULL CHECK(length(decision_sha256)=64),
  created_at TEXT NOT NULL
)""",
"""CREATE INDEX hard_no_decision_blocker_timezone_v2
ON auto_trade_decisions(decided_state,will_order,created_at)""",
"""CREATE TRIGGER hard_no_auto_trade_decisions_no_update_v2
BEFORE UPDATE ON auto_trade_decisions BEGIN SELECT RAISE(ABORT,'append_only'); END""",
"""CREATE TRIGGER hard_no_auto_trade_decisions_no_delete_v2
BEFORE DELETE ON auto_trade_decisions BEGIN SELECT RAISE(ABORT,'append_only'); END""",
)
NEW_DDL = ";\n".join(NEW_STATEMENTS) + ";\n"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _daemon_absent() -> bool:
    result = subprocess.run(
        ["/usr/bin/pgrep", "-f", str(ROOT / "scripts/reported_trajectory_no_daemon.py")],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    return result.returncode == 1


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")'))


def inspect(conn: sqlite3.Connection) -> dict[str, Any]:
    names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    current = _columns(conn, "auto_trade_decisions")
    new_ready = "settlement_timezone" in current
    old_count = (
        int(conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0])
        if current else 0
    )
    unsafe = (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM auto_trade_decisions "
                "WHERE will_order<>0 OR decided_state IN "
                "('prepared','submitting','submitted','reconcile_required')"
            ).fetchone()[0]
        )
        if current and not new_ready else 0
    )
    non_hard_no = (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM auto_trade_decisions WHERE strategy_lane<>'hard_no'"
            ).fetchone()[0]
        )
        if current and not new_ready else 0
    )
    return {
        "schema": "reported_trajectory_hard_no_timezone_plan_v1",
        "state": "already_ready" if new_ready else "migration_required",
        "current_columns": list(current),
        "expected_old_columns": list(EXPECTED_OLD_COLUMNS),
        "old_row_count": old_count,
        "unsafe_row_count": unsafe,
        "non_hard_no_row_count": non_hard_no,
        "archive_table_present": ARCHIVE_TABLE in names,
        "writes_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def _backup_table(conn: sqlite3.Connection, destination: Path) -> dict[str, Any]:
    destination = destination.expanduser().resolve()
    if destination.exists() or destination.parent != DEFAULT_DB.parent.resolve():
        raise RuntimeError("hard_no_backup_path_invalid")
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='auto_trade_decisions'"
    ).fetchone()
    if schema is None:
        raise RuntimeError("hard_no_decision_table_missing")
    columns = _columns(conn, "auto_trade_decisions")
    row_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0]
    )
    if row_count > MAX_HARD_NO_BACKUP_ROWS:
        raise RuntimeError("hard_no_backup_row_limit_exceeded")
    free_bytes = int(shutil.disk_usage(destination.parent).free)
    if (
        free_bytes
        < MIN_TARGET_FREE_BYTES + MAX_HARD_NO_BACKUP_BYTES
    ):
        raise RuntimeError("hard_no_backup_free_space_insufficient")
    assert_process_memory_budget(
        projected_bytes=min(
            MAX_HARD_NO_BACKUP_PAYLOAD_BYTES,
            32 * MIB,
        )
    )
    copied_rows = 0
    payload_bytes = 0
    try:
        with sqlite3.connect(destination) as backup:
            backup.execute(str(schema[0]).replace(
                "CREATE TABLE auto_trade_decisions",
                "CREATE TABLE auto_trade_decisions_backup",
                1,
            ))
            placeholders = ",".join("?" for _ in columns)
            cursor = conn.execute(
                "SELECT * FROM auto_trade_decisions ORDER BY rowid"
            )
            while True:
                rows = cursor.fetchmany(BACKUP_BATCH_ROWS)
                if not rows:
                    break
                batch_bytes = sum(
                    len(str(value or "").encode("utf-8"))
                    for row in rows
                    for value in row
                )
                payload_bytes += batch_bytes
                copied_rows += len(rows)
                if (
                    copied_rows > MAX_HARD_NO_BACKUP_ROWS
                    or payload_bytes
                    > MAX_HARD_NO_BACKUP_PAYLOAD_BYTES
                ):
                    raise RuntimeError(
                        "hard_no_backup_payload_limit_exceeded"
                    )
                assert_process_memory_budget(
                    projected_bytes=min(
                        max(batch_bytes * 2, MIB),
                        8 * MIB,
                    )
                )
                backup.executemany(
                    "INSERT INTO auto_trade_decisions_backup "
                    f"VALUES({placeholders})",
                    rows,
                )
            if copied_rows != row_count:
                raise RuntimeError("hard_no_backup_row_count_mismatch")
            backup.execute(
                "CREATE TABLE backup_metadata("
                "key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            backup.executemany(
                "INSERT INTO backup_metadata VALUES(?,?)",
                (
                    ("source_database", str(DEFAULT_DB.resolve())),
                    ("row_count", str(copied_rows)),
                    ("payload_bytes", str(payload_bytes)),
                    ("created_at_utc", _now()),
                ),
            )
            backup.commit()
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("hard_no_backup_quick_check_failed")
        backup_bytes = int(destination.stat().st_size)
        if backup_bytes > MAX_HARD_NO_BACKUP_BYTES:
            raise RuntimeError("hard_no_backup_file_limit_exceeded")
        assert_process_memory_budget()
        return {
            "path": str(destination),
            "row_count": copied_rows,
            "payload_bytes": payload_bytes,
            "bytes": backup_bytes,
            "quick_check": "ok",
            "free_bytes_before": free_bytes,
        }
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def migrate(db: Path, backup: Path) -> dict[str, Any]:
    db = db.expanduser().resolve()
    if db != DEFAULT_DB.resolve():
        raise RuntimeError("hard_no_migration_database_scope_invalid")
    if not PAUSE_MARKER.exists() or not LIVE_PAUSE.exists():
        raise RuntimeError("hard_no_migration_pause_missing")
    if not _daemon_absent():
        raise RuntimeError("hard_no_writer_not_quiescent")
    with sqlite3.connect(db, timeout=30.0, isolation_level=None) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        before = inspect(conn)
        if before["state"] == "already_ready":
            return {**before, "state": "already_ready", "orders_submitted": 0}
        if tuple(before["current_columns"]) != EXPECTED_OLD_COLUMNS:
            raise RuntimeError("hard_no_old_schema_unexpected")
        if before["archive_table_present"]:
            raise RuntimeError("hard_no_archive_table_collision")
        if before["unsafe_row_count"] or before["non_hard_no_row_count"]:
            raise RuntimeError("hard_no_legacy_rows_not_safe_to_retire")
        backup_result = _backup_table(conn, backup)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                f'ALTER TABLE auto_trade_decisions RENAME TO "{ARCHIVE_TABLE}"'
            )
            for statement in NEW_STATEMENTS:
                conn.execute(statement)
            after = inspect(conn)
            if after["state"] != "already_ready" or after["old_row_count"] != 0:
                raise RuntimeError("hard_no_timezone_schema_verification_failed")
            archived = int(
                conn.execute(f'SELECT COUNT(*) FROM "{ARCHIVE_TABLE}"').fetchone()[0]
            )
            if archived != int(before["old_row_count"]):
                raise RuntimeError("hard_no_archive_row_count_mismatch")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    receipt = {
        "schema": "reported_trajectory_hard_no_timezone_migration_v1",
        "state": "migrated",
        "database": str(db),
        "legacy_rows_preserved": int(before["old_row_count"]),
        "archive_table": ARCHIVE_TABLE,
        "backup": backup_result,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "writes_orders": False,
        "orders_submitted": 0,
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup-db", type=Path)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.confirm != CONFIRM:
        with sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True) as conn:
            print(json.dumps(inspect(conn), ensure_ascii=False, sort_keys=True))
        return 0
    if args.backup_db is None or args.receipt is None:
        raise SystemExit("hard_no_backup_and_receipt_required")
    result = migrate(args.db, args.backup_db)
    receipt = args.receipt.expanduser().resolve()
    write_bounded_json(receipt, result)
    print(json.dumps({"state": result["state"], "receipt": str(receipt), "orders_submitted": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
