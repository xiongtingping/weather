"""Extract the smallest database the paper ledger can be run against.

The ledger scores a book from compiled envelopes.  To score a counterfactual
-- envelopes compiled under a different upstream constant -- it has to read
those envelopes instead of production's, and it reads whatever database
``--db`` names.  Pointing it at a copy of the SSOT is not available: that file
is 27 GiB against a 29 GiB ceiling and the disk holding it has less headroom
than the copy would need.

So this extracts only what the ledger actually opens.  Measured by reading
every FROM clause in ``paper_trading_ledger.py``: five input tables, plus the
three ``paper_trade_*`` tables it creates and writes itself.

``envelope_json`` in the bridge history is an empty stub.  The body lives in
``basket_probability_envelope`` keyed by checkpoint, and the bucket
projection in ``basket_probability_bridge_bucket_probability`` keyed by
history id; both must be carried or every envelope resolves to nothing.

SIZE IS THE BINDING CONSTRAINT, not row count.  Measured 2026-08-27: the
bodies average 19 KB, so a fourteen-day window is about 5 GB of envelope on
top of 2.7 GB of bucket projection, and an attempt at that window filled the
disk to 351 MB free and tripped the resident's storage guard to
``blocked_fail_closed`` before it was killed.  Nothing was lost -- the guard
did exactly what it exists for and cleared on its own once the partial file
was deleted -- but the lesson is in this comment rather than in a second
incident: size the window against free disk first, extract second, and
prefer mutating one replay database between arms over holding two.

Schemas are copied verbatim from the source rather than restated here.  A
hand-written schema that drifts from production's would make the replay score
a table shape nothing serves, which is the same class of error as an audit
that reimplements the rule it audits.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "bayes_basket_target_ssot.db"

# (table, column the window cuts on, or None to copy whole)
INPUT_TABLES = (
    ("basket_probability_bridge_history", "event_date"),
    # The envelope bodies.  The history row's ``envelope_json`` column is an
    # empty stub on everything recent -- production writes the body here
    # instead, keyed by checkpoint, because copying it into both doubled the
    # store and 5.9 GB of that doubling is what hit the 27 GiB ceiling.  A
    # replay that omits this table resolves every envelope to nothing.
    ("basket_probability_envelope", "event_date"),
    ("basket_official_ws_snapshot", "event_date"),
    ("calibration_station_daily_high_truth", "local_date"),
    ("calibration_station_market_timezone_binding", None),
)
# Carried by join rather than by date: it has no date of its own and is
# meaningless without the history rows it hangs from.
JOINED_TABLE = "basket_probability_bridge_bucket_probability"


def _schema_of(conn: sqlite3.Connection, table: str) -> list[str]:
    """Every CREATE the table needs, table first then its indexes.

    Triggers are deliberately left behind.  The append-only guards on the
    production tables exist to protect production evidence; a replay database
    is rebuilt from scratch every time and would only be blocked by them.
    """

    rows = conn.execute(
        "SELECT type, sql FROM sqlite_master"
        " WHERE tbl_name=? AND sql IS NOT NULL"
        " ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END",
        (table,),
    ).fetchall()
    return [str(sql) for kind, sql in rows if kind in ("table", "index")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="event date floor")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()

    if args.out.exists():
        args.out.unlink()
    for suffix in ("-wal", "-shm"):
        stale = args.out.with_name(args.out.name + suffix)
        if stale.exists():
            stale.unlink()

    src = sqlite3.connect(
        f"file:{args.source.resolve(strict=True)}?mode=ro",
        uri=True, timeout=120.0,
    )
    src.execute("PRAGMA query_only=ON")
    out = sqlite3.connect(str(args.out), timeout=120.0)
    # No journal while building.  This database is created from scratch on
    # every run and thrown away, so there is nothing a rollback journal could
    # protect -- and in WAL the log grew as large as the file itself, which
    # is what turned a 2.5 GB extract into a 7.8 GB one and filled the disk.
    out.execute("PRAGMA journal_mode=OFF")
    out.execute("PRAGMA synchronous=OFF")

    counts: dict[str, int] = {}
    try:
        for table, date_column in INPUT_TABLES:
            statements = _schema_of(src, table)
            if not statements:
                print(f"  {table}: absent from source, skipped")
                continue
            for statement in statements:
                out.execute(statement)
            if date_column:
                rows = src.execute(
                    f"SELECT * FROM {table} WHERE {date_column}>=?",
                    (args.since,),
                ).fetchall()
            else:
                rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if rows:
                marks = ",".join("?" for _ in rows[0])
                out.executemany(
                    f"INSERT INTO {table} VALUES({marks})", rows
                )
            counts[table] = len(rows)
            out.commit()
            print(f"  {table}: {len(rows)} rows")

        statements = _schema_of(src, JOINED_TABLE)
        for statement in statements:
            out.execute(statement)
        history_ids = [
            row[0] for row in out.execute(
                "SELECT history_id FROM basket_probability_bridge_history"
            )
        ]
        carried = 0
        CHUNK = 400
        for start in range(0, len(history_ids), CHUNK):
            chunk = history_ids[start:start + CHUNK]
            marks = ",".join("?" for _ in chunk)
            rows = src.execute(
                f"SELECT * FROM {JOINED_TABLE}"
                f" WHERE history_id IN ({marks})",
                chunk,
            ).fetchall()
            if rows:
                value_marks = ",".join("?" for _ in rows[0])
                out.executemany(
                    f"INSERT INTO {JOINED_TABLE} VALUES({value_marks})",
                    rows,
                )
                carried += len(rows)
        out.commit()
        counts[JOINED_TABLE] = carried
        print(f"  {JOINED_TABLE}: {carried} rows"
              f" (for {len(history_ids)} envelopes)")
    finally:
        out.close()
        src.close()

    # The ledger writes its own tables into this file, so hand it back a
    # journalled database rather than the unjournalled one used to load it.
    finish = sqlite3.connect(str(args.out), timeout=120.0)
    try:
        finish.execute("PRAGMA journal_mode=WAL")
    finally:
        finish.close()

    size = args.out.stat().st_size
    print(f"\n  wrote {args.out} ({size/1048576:.1f} MB)")
    print("  the ledger creates its own paper_trade_* tables on first run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
