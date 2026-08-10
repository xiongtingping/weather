#!/usr/bin/env python3
"""Widen the correction-review decision domain to admit the replacement contract.

An on-hour corrected report is eligible truth input, so clearing it as
reference-only would drop a reading the day's maximum is entitled to -- which
is why the gate refuses that combination and has been waiting for a
"replacement contract" instead.  ``CLEAR_WITH_REPLACEMENT`` is that contract,
and this migration is the one place the stored CHECK has to learn it.

SQLite cannot alter a CHECK in place, so the table is rebuilt.  It holds only
review receipts -- a few hundred rows -- and every existing row is copied
verbatim: this widens what may be written from here on and reinterprets
nothing already recorded.

Append-only is enforced by triggers rather than by the table, so they are
dropped and reinstated around the rebuild; the writer must be stopped for the
duration, which this refuses to proceed without.
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
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from weather_basket_yes_schema import (  # noqa: E402
    CORRECTION_REVIEW_DDL,
)


DEFAULT_DB = ROOT / "data" / "archive.db"
SERVICE_LABEL = f"gui/{os.getuid()}/com.weather.weather_basket_yes"
MAX_ARCHIVE_WAL_BYTES = 512 * 1024 * 1024
MIN_ARCHIVE_FREE_BYTES = 1024 * 1024 * 1024
TABLE = "reported_trajectory_station_halt_reviews"
# The only table the rebuilt one points at, and therefore the only other one a
# rehearsal has to carry.
REFERENCED_TABLE = "reported_trajectory_observations"
WIDENED_DECISION = "CLEAR_WITH_REPLACEMENT"


def _service_running() -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", SERVICE_LABEL],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def rehearse(*, db_path: Path) -> dict[str, object]:
    """Run this exact migration against a copy of only the tables it touches.

    Rehearsing on a copy is the only way to know a rebuild preserves the rows,
    the append-only triggers and the foreign keys -- but copying the whole
    archive to find out is what filled the disk to 171MiB and had to be
    abandoned.  The migration reads and writes one table of a few hundred rows,
    so the copy is that table plus the rows its foreign key points at: a few
    hundred kilobytes, seconds to build, and exactly as conclusive.

    Touches nothing outside its own temporary file, so it needs no quiescence
    and can run while the writer is up.
    """

    source = db_path.expanduser().resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="correction-review-rehearsal-") as work:
        copy = Path(work) / "rehearsal.db"
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30.0)
        src.execute("PRAGMA query_only=ON")
        dst = sqlite3.connect(copy)
        try:
            for name in (TABLE, REFERENCED_TABLE):
                ddl = src.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone()
                if ddl is None:
                    raise ContractError(
                        "correction_review_rehearsal_source_table_missing"
                    )
                dst.execute(str(ddl[0]))
            for (ddl,) in src.execute(
                "SELECT sql FROM sqlite_master WHERE type IN ('index','trigger') "
                "AND tbl_name=? AND sql IS NOT NULL",
                (TABLE,),
            ):
                dst.execute(str(ddl))
            # Only the observations the receipts point at: the rest of that
            # table is irrelevant to this rebuild and is what makes a full copy
            # expensive.
            referenced_columns = [
                str(row[1])
                for row in src.execute(f"PRAGMA table_info({REFERENCED_TABLE})")
            ]
            column_list = ",".join(referenced_columns)
            placeholders = ",".join("?" * len(referenced_columns))
            dst.executemany(
                f"INSERT INTO {REFERENCED_TABLE} ({column_list}) "
                f"VALUES ({placeholders})",
                src.execute(
                    f"SELECT {column_list} FROM {REFERENCED_TABLE} "
                    f"WHERE observation_id IN "
                    f"(SELECT triggering_observation_id FROM {TABLE})"
                ),
            )
            review_columns = [
                str(row[1]) for row in src.execute(f"PRAGMA table_info({TABLE})")
            ]
            review_list = ",".join(review_columns)
            dst.executemany(
                f"INSERT INTO {TABLE} ({review_list}) VALUES "
                f"({','.join('?' * len(review_columns))})",
                src.execute(f"SELECT {review_list} FROM {TABLE}"),
            )
            dst.commit()
        finally:
            dst.close()
            src.close()
        result = migrate(db_path=copy, confirmed_stopped=True, rehearsal=True)
        return {"rehearsal": True, **result}


def migrate(
    *,
    db_path: Path,
    confirmed_stopped: bool,
    rehearsal: bool = False,
) -> dict[str, object]:
    requested = db_path.expanduser()
    if requested.is_symlink():
        raise ContractError("correction_review_migration_symlink_forbidden")
    database = requested.resolve(strict=True)
    # A rehearsal owns its temporary file outright, so the quiescence and
    # capacity checks -- which exist to protect the live archive -- have
    # nothing to protect here.
    if not rehearsal:
        wal = Path(str(database) + "-wal")
        wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
        if wal_bytes > MAX_ARCHIVE_WAL_BYTES:
            raise ContractError("correction_review_migration_wal_limit_exceeded")
        if int(shutil.disk_usage(database.parent).free) < MIN_ARCHIVE_FREE_BYTES:
            raise ContractError(
                "correction_review_migration_free_space_too_low"
            )
        if not confirmed_stopped:
            raise ContractError(
                "correction_review_migration_quiescence_confirmation_required"
            )
        if _service_running():
            raise ContractError(
                "correction_review_migration_writer_still_running"
            )
    conn = sqlite3.connect(database, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=FULL")
    try:
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone()
        if existing is None:
            raise ContractError("correction_review_migration_table_missing")
        if WIDENED_DECISION in str(existing[0]):
            return {
                "state": "ALREADY_WIDENED",
                "rows_before": int(
                    conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
                ),
                "rows_after": int(
                    conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
                ),
            }
        before = int(
            conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        )
        columns = [
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({TABLE})")
        ]
        column_list = ",".join(columns)
        triggers = [
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name=?",
                (TABLE,),
            )
        ]
        indexes = [
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name=? AND sql IS NOT NULL",
                (TABLE,),
            )
        ]
        # Foreign keys stay off for the rebuild only: the table references
        # itself through supersedes_review_id, and a partially populated copy
        # would otherwise be rejected mid-transfer.  They are checked again
        # before the transaction is allowed to commit.
        # Three stages rather than one transaction: ``executescript`` commits
        # implicitly, so the table DDL cannot run inside one.  The original
        # table survives under ``_pre_replacement`` until the copy has been
        # counted, so an interrupted run leaves the rows recoverable by
        # renaming it back rather than leaving nothing behind.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        for name, _sql in triggers:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for name, _sql in indexes:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute(f"ALTER TABLE {TABLE} RENAME TO {TABLE}_pre_replacement")
        conn.execute("COMMIT")

        conn.executescript(CORRECTION_REVIEW_DDL)

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"INSERT INTO {TABLE} ({column_list}) "
            f"SELECT {column_list} FROM {TABLE}_pre_replacement"
        )
        after = int(
            conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        )
        if after != before:
            raise ContractError("correction_review_migration_row_count_changed")
        conn.execute(f"DROP TABLE {TABLE}_pre_replacement")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ContractError("correction_review_migration_foreign_key_broken")
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys=ON")
        widened = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone()
        if WIDENED_DECISION not in str(widened[0]):
            raise ContractError("correction_review_migration_check_not_widened")
        restored = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name=?",
                (TABLE,),
            )
        }
        missing = {name for name, _sql in triggers} - restored
        if missing:
            raise ContractError(
                "correction_review_migration_append_only_triggers_missing"
            )
        return {
            "state": "WIDENED",
            "rows_before": before,
            "rows_after": after,
            "restored_trigger_count": len(restored),
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--confirm-writer-stopped",
        action="store_true",
        help="Assert the resident writer is stopped; the check is repeated here.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Migrate the real database.  Without it the run rehearses on a "
            "copy of just the affected tables and leaves the archive alone."
        ),
    )
    args = parser.parse_args()
    try:
        # Rehearsal is what happens by default.  The evidence that this rebuild
        # is safe costs a few hundred kilobytes and no downtime, so there is no
        # reason to reach the real archive without it.
        result = (
            migrate(
                db_path=args.db,
                confirmed_stopped=bool(args.confirm_writer_stopped),
            )
            if args.apply
            else rehearse(db_path=args.db)
        )
    except Exception as exc:
        print(json.dumps({
            "schema": "correction_review_replacement_decision_migration_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps({
        "schema": "correction_review_replacement_decision_migration_v1",
        **result,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
