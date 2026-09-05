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
    # The lineage bodies the envelopes reference by hash.  Without it every
    # envelope with a dehydrated lineage fails closed in
    # ``rehydrate_lineage_documents`` and the whole walk dies on the first
    # station -- which is what it did on 2026-09-01, reported as
    # ``xsb_history_envelope_body_invalid`` with nothing naming the missing
    # table.  Copied whole: it is deduplicated by hash, so the entire store
    # is 295 rows and about 1 MB, and there is no date column to cut on.
    ("basket_probability_lineage_document", None),
    ("basket_official_ws_snapshot", "event_date"),
    ("calibration_station_daily_high_truth", "local_date"),
    ("calibration_station_market_timezone_binding", None),
    # What production's compile resolves on top of the evidence: the station
    # basket policy, the delta-v2 policy and their activations, the global
    # priors and cash phases, and the strategy activation that names the
    # authority scope.  All small and undated, copied whole.  Added for the
    # ledger's production-judge arm (2026-09-03), which resolves them with
    # production's own resolvers as of each decision clock.
    ("basket_policy_activation", None),
    ("basket_station_policy_artifact", None),
    ("basket_global_prior_artifact", None),
    ("basket_cash_phase_policy_artifact", None),
    ("basket_delta_v2_policy_activation", None),
    ("basket_delta_v2_policy_artifact", None),
    # The resolver refuses an artifact whose promotion row is absent
    # (``delta_v2_artifact_not_promoted``), so the promotions travel too.
    ("basket_delta_v2_policy_promotion", None),
    ("basket_strategy_activation_state", None),
    # Charter 68(c): the lambda store the ledger reads as of each decision.
    # Two small tables, copied whole; without them every replay reads the
    # kernel constants and an as-of arm is silently identical to a
    # constants arm -- measured 2026-09-05 on a database that had the
    # tables' schema and none of their rows.
    ("calibration_effective_edge_lambda", None),
    ("calibration_effective_edge_fit_journal", None),
)
# Carried by join rather than by date: it has no date of its own and is
# meaningless without the history rows it hangs from.
JOINED_TABLE = "basket_probability_bridge_bucket_probability"


def _schema_of(conn: sqlite3.Connection, table: str) -> list[str]:
    """Every CREATE the table needs, table first then its indexes.

    Triggers are not part of a table's schema here: they are copied in one
    pass at the end, after every row is in, because production's schema gate
    counts them (``trigger_set_mismatch``) and the ledger's production-judge
    arm has to pass that gate to load production's policy artifacts.  Rows
    go in before the guards exist, so the append-only guards on the
    production tables cost the copy nothing.
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
            # Streamed, not ``fetchall()``.  The envelope bodies average
            # 18.4 KB each, so reading the table in one call materialises
            # 7.2 GB of Python objects for the 2026-07-28 window -- which
            # is what the "vanishing disk" was: swap, not SQLite scratch.
            # Measured 2026-09-01, after two wrong diagnoses (window width,
            # then commit cadence) that each moved the symptom without
            # touching it.  A cursor holds one page at a time.
            if date_column:
                cursor = src.execute(
                    f"SELECT * FROM {table} WHERE {date_column}>=?",
                    (args.since,),
                )
            else:
                cursor = src.execute(f"SELECT * FROM {table}")
            copied_rows = 0
            while True:
                batch = cursor.fetchmany(2000)
                if not batch:
                    break
                marks = ",".join("?" for _ in batch[0])
                out.executemany(
                    f"INSERT INTO {table} VALUES({marks})", batch
                )
                copied_rows += len(batch)
                out.commit()
            counts[table] = copied_rows
            out.commit()
            print(f"  {table}: {copied_rows} rows")

        statements = _schema_of(src, JOINED_TABLE)
        for statement in statements:
            out.execute(statement)
        # Every other production table, schema only.  Production's artifact
        # loader refuses a store whose schema is incomplete before it reads
        # a row (``production_ssot_schema_status``), and an empty table is
        # what "no rows in the window" looks like anyway.
        copied = {table for table, _ in INPUT_TABLES} | {JOINED_TABLE}
        schema_only = 0
        names = [
            str(name) for (name,) in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for name in names:
            if name in copied:
                continue
            for statement in _schema_of(src, name):
                out.execute(statement)
            schema_only += 1
        out.commit()
        print(f"  schema-only tables: {schema_only}")
        # Production's schema gate also counts the triggers
        # (``trigger_set_mismatch``), so they come across last -- after every
        # row is in, where the append-only guards cost nothing and block
        # nothing the ledger does, since it only writes its own tables.
        triggers = 0
        trigger_sql = [
            str(sql) for _name, _table, sql in src.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master"
                " WHERE type='trigger' AND sql IS NOT NULL ORDER BY name"
            )
        ]
        for sql in trigger_sql:
            out.execute(sql)
            triggers += 1
        out.commit()
        print(f"  triggers: {triggers}")
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
            joined = src.execute(
                f"SELECT * FROM {JOINED_TABLE}"
                f" WHERE history_id IN ({marks})",
                chunk,
            )
            while True:
                batch = joined.fetchmany(2000)
                if not batch:
                    break
                value_marks = ",".join("?" for _ in batch[0])
                out.executemany(
                    f"INSERT INTO {JOINED_TABLE} VALUES({value_marks})",
                    batch,
                )
                carried += len(batch)
            # Commit as we go.  One transaction spanning every chunk holds
            # the whole table's pages uncommitted, and measured 2026-09-01
            # that peaked around 11 GiB of scratch above the 6 GiB the file
            # itself had reached -- on a host with 21 GiB free it tripped the
            # storage guard before the join finished.  The rows are the same
            # rows either way; only the peak differs.
            #
            # Every 5 chunks, not every 50: at 50 the same run still drew
            # 7 GiB of scratch in four minutes while the file stayed at
            # 550 MB, because the interval is what bounds the peak and
            # 20,000 envelopes of bucket rows is not a bound.  A commit is
            # cheap here -- there is no journal to flush.
            if start % (CHUNK * 5) == 0:
                out.commit()
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
