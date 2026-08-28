#!/usr/bin/env python3
"""Widen the historical member-run transport allowlist to the live table.

The JMA MSM capture lane (RISH Kyoto archive, byte-range GRIB reads) was
built and scheduled weeks ago and never stored a row: the table's
``transport_provider_id`` CHECK predates the transport, and a CHECK cannot
be altered in place.  The pre-flight guard added on 2026-08-2x reports the
refusal instead of burning the fetch, which is why the sweep has been
green-but-empty rather than failing.

Standard SQLite CHECK migration: build the successor table from the
current schema source (``ssot.py``, already widened), copy every row,
swap names, rebuild the indexes and the append-only triggers, all inside
one transaction.  Row count and per-transport counts are asserted equal
before commit; any mismatch rolls the whole thing back.

Read-only unless ``--commit``.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TABLE = "calibration_native_ensemble_historical_member_run"
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
# Every transport the schema source now carries beyond what the original
# CHECK held.  Re-running after each ruling is the intended shape: the
# done-check below compares the live DDL against this list, so widening
# the list re-arms the migration.
NEW_TRANSPORTS = (
    "RISH_KYOTO_JMA_ARCHIVE",   # ruled 2026-08-28, JMA MSM via RISH Kyoto
    "NOAA_HRRR_PDS_S3",         # ruled 2026-08-28, HRRR via NOAA open data
    "NOAA_NBM_PDS_S3",          # ruled 2026-08-28, NBM core via NOAA open data
)


def _schema_ddl() -> str:
    """The table's DDL as the schema source now states it."""

    source = (ROOT / "bayes_basket_target" / "ssot.py").read_text(
        encoding="utf-8"
    )
    start = source.index(f"CREATE TABLE IF NOT EXISTS {TABLE}")
    # Comments are stripped BEFORE the parenthesis walk.  The third run of
    # this migration died on "incomplete input": a schema comment contained
    # a quoted ')' -- an unbalanced parenthesis as far as a character count
    # is concerned -- and the walk closed the DDL there, mid-statement.
    # The same class of failure hit the capture pre-flight an hour earlier
    # from the other side (comments INSIDE a list read as members).  Text
    # inside comments is not structure; remove it before counting.
    segment = re.sub(r"--[^\n]*", "", source[start:start + 20000])
    depth = 0
    for index, char in enumerate(segment):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                body = segment[:index + 1]
                break
    else:
        raise SystemExit("DDL not found in schema source")
    missing = [t for t in NEW_TRANSPORTS if t not in body]
    if missing:
        raise SystemExit(
            f"schema source does not carry {missing}; edit ssot.py first"
        )
    return body.replace(
        f"CREATE TABLE IF NOT EXISTS {TABLE}",
        f"CREATE TABLE {TABLE}__widened",
        1,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    conn = sqlite3.connect(
        str(database) if args.commit else f"file:{database}?mode=ro",
        uri=not args.commit, timeout=120.0,
    )
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        live = str(conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (TABLE,)
        ).fetchone()[0])
        # Widened AND clean: the first migration shipped the widening with
        # SQL comments inside the IN(...) list, which the capture
        # pre-flight parses -- the comment text read as phantom allowlist
        # members and its embedded ')' truncated the parse before the new
        # transport.  A live DDL still carrying comments in the list is
        # therefore not done, however wide it is.
        # Done = every ruled transport appears in BOTH checks (storage and
        # the narrower OOS-eligibility one) and no comment pollutes a list.
        if (
            all(live.count(t) >= 2 for t in NEW_TRANSPORTS)
            and "-- Ruled" not in live
        ):
            print("already widened; nothing to do")
            return 0
        before = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        per = dict(conn.execute(
            f"SELECT transport_provider_id, COUNT(*) FROM {TABLE} GROUP BY 1"
        ))
        print(f"rows={before}  per-transport={per}")
        objects = conn.execute(
            "SELECT type, name, sql FROM sqlite_master"
            " WHERE tbl_name=? AND type IN ('index','trigger')"
            " AND sql IS NOT NULL", (TABLE,)
        ).fetchall()
        print(f"dependent objects: {[(t, n) for t, n, _ in objects]}")
        if not args.commit:
            print("\n(report only; --commit performs the migration)")
            return 0
        ddl = _schema_ddl()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(ddl)
            conn.execute(
                f"INSERT INTO {TABLE}__widened SELECT * FROM {TABLE}"
            )
            copied = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE}__widened"
            ).fetchone()[0]
            if copied != before:
                raise RuntimeError(
                    f"row count moved during copy: {before} -> {copied}"
                )
            per_after = dict(conn.execute(
                f"SELECT transport_provider_id, COUNT(*)"
                f" FROM {TABLE}__widened GROUP BY 1"
            ))
            if per_after != per:
                raise RuntimeError("per-transport counts diverged")
            # Triggers on the old table die with it; indexes too.
            conn.execute(f"DROP TABLE {TABLE}")
            conn.execute(
                f"ALTER TABLE {TABLE}__widened RENAME TO {TABLE}"
            )
            for kind, name, sql in objects:
                conn.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        widened = str(conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (TABLE,)
        ).fetchone()[0])
        assert all(t in widened for t in NEW_TRANSPORTS)
        after = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        print(
            f"migrated: rows={after}, allowlist now carries"
            f" {', '.join(NEW_TRANSPORTS)}"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
