#!/usr/bin/env python3
"""Re-address every basket decision by what it was made of, not by when.

User ruling 2026-09-02: the economic decision is content-addressed on four
legs -- probability, the objective's sizing output, the position book, the
policy artefacts.  ``auto_trade_decisions`` addressed it on those *plus* the
commit clock (``target_execution_hard_deadline_at`` is ``created_at`` + 3s),
so every re-decision was a fresh row: 67,492 basket decisions, of which
52,140 are blocked and carry no economic hashes at all, churning 1.6x on
their reasons alone.

This migration recomputes ``decision_sha256`` and ``decision_id`` with
``atomic_ledger.economic_decision_identity`` -- the same function the commit
path now uses, so migrated history and new rows agree by construction --
collapses rows that resolve to one id, and re-points every foreign key at
the survivor.  The earliest row of a group survives; the others were
restatements of it.

Three modes.  ``--plan`` computes and reports without touching anything.
``--rehearse`` copies the affected tables into a disposable database and
runs the migration there, then verifies.  ``--apply`` runs it against the
real SSOT, and refuses unless the resident writer is quiescent.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.atomic_ledger import (  # noqa: E402
    DECISION_SCHEMA_V2,
    economic_decision_identity,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
LANE = "basket"

# Every column, in every table, that names a decision.
FK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("basket_target_execution_version_vectors", "decision_id"),
    ("basket_target_order_intents", "decision_id"),
    ("basket_policy_training_fact", "decision_id"),
    ("basket_target_prepared_supersession_receipts", "old_decision_id"),
    ("basket_target_prepared_supersession_receipts", "new_decision_id"),
    ("basket_target_execution_transition_versions", "decision_id"),
)


def _derive(identity: dict[str, Any]) -> tuple[str, str]:
    sha = content_sha256({"schema": DECISION_SCHEMA_V2, **identity})
    return sha, f"decision_{sha[:40]}"


def plan(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute the new identity of every basket decision; write nothing."""

    scope_by_decision = {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT decision_id, decision_scope_key"
            " FROM basket_policy_training_fact"
        )
    }
    rows = conn.execute(
        "SELECT decision_id, decision_sha256, station_id, event_date,"
        " settlement_timezone, strategy_lane, decided_state, will_order,"
        " reason_codes_json, details_json, created_at"
        " FROM auto_trade_decisions WHERE strategy_lane=?"
        " ORDER BY created_at, decision_id",
        (LANE,),
    ).fetchall()
    mapping: dict[str, tuple[str, str, str | None]] = {}
    groups: dict[str, list[str]] = defaultdict(list)
    by_state: Counter[str] = Counter()
    keyed_from: Counter[str] = Counter()
    for (old_id, _old_sha, st, day, tz, lane, state, will,
         reasons_json, details_json, _created) in rows:
        try:
            reasons = json.loads(reasons_json or "[]")
            details = json.loads(details_json or "{}")
        except (TypeError, ValueError):
            reasons, details = [], {}
        scope_key = scope_by_decision.get(str(old_id)) or None
        keyed_from["training_fact" if scope_key else "details"] += 1
        identity = economic_decision_identity(
            station_id=st, event_date=day, settlement_timezone=tz,
            strategy_lane=lane, decided_state=state, will_order=int(will),
            reason_codes=reasons, details=details,
            decision_scope_key=scope_key,
        )
        new_sha, new_id = _derive(identity)
        mapping[str(old_id)] = (new_id, new_sha, scope_key)
        groups[new_id].append(str(old_id))
        by_state[str(state)] += 1
    survivors = {new_id: olds[0] for new_id, olds in groups.items()}
    collapsed = sum(len(olds) - 1 for olds in groups.values())
    fk_counts: dict[str, int] = {}
    for table, column in FK_COLUMNS:
        try:
            fk_counts[f"{table}.{column}"] = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IN"
                f" (SELECT decision_id FROM auto_trade_decisions"
                f"  WHERE strategy_lane=?)", (LANE,),
            ).fetchone()[0])
        except sqlite3.Error:
            fk_counts[f"{table}.{column}"] = -1
    return {
        "schema": "decision_identity_migration_plan_v1",
        "decisions": len(rows),
        "by_state": dict(by_state),
        "keyed_from": dict(keyed_from),
        "distinct_new_ids": len(groups),
        "collapsed_rows": collapsed,
        "churn_before": round(len(rows) / max(1, len(groups)), 3),
        "largest_group": max((len(v) for v in groups.values()), default=0),
        "fk_rows_to_repoint": fk_counts,
        "_mapping": mapping,
        "_survivors": survivors,
    }


def apply(conn: sqlite3.Connection, planned: dict[str, Any]) -> dict[str, Any]:
    """Rewrite ids and shas, collapse groups, re-point every foreign key.

    Set-based, not row-by-row.  The first version issued one UPDATE per old
    id per foreign-key table -- 67,492 x 6 statements, each a scan of an
    unindexed column -- and was still running after sixteen minutes on a
    copy.  A temporary mapping table turns each table's re-point into one
    statement the planner can drive off an index.
    """

    mapping: dict[str, tuple[str, str, str | None]] = planned["_mapping"]
    survivors: dict[str, str] = planned["_survivors"]
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(auto_trade_decisions)")}
    if "decision_scope_key" not in columns:
        conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN decision_scope_key TEXT")
    repointed: dict[str, int] = {}
    # The append-only guards.  Every table this migration rewrites carries a
    # BEFORE UPDATE / BEFORE DELETE trigger that RAISEs, and the first
    # production attempt on 2026-09-02 died on the first UPDATE -- after a
    # rehearsal had passed on a copy that carried the tables but not their
    # triggers.  They are dropped inside the transaction, from their stored
    # SQL, and recreated from that same SQL before commit, so the guards are
    # lifted for exactly this rewrite and for nothing else.
    tables = ["auto_trade_decisions"] + sorted({t for t, _ in FK_COLUMNS})
    guards = [
        (str(name), str(sql)) for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            " AND tbl_name IN (%s) AND sql IS NOT NULL"
            % ",".join("?" for _ in tables), tables,
        )
        if "append_only" in str(sql) or "_no_update" in str(name)
        or "_no_delete" in str(name)
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name, _sql in guards:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        # old_id -> (survivor_old_id, new_id, new_sha, scope_key)
        conn.execute("DROP TABLE IF EXISTS temp._decision_remap")
        conn.execute(
            "CREATE TEMP TABLE _decision_remap("
            " old_id TEXT PRIMARY KEY, survivor_old_id TEXT NOT NULL,"
            " new_id TEXT NOT NULL, new_sha TEXT NOT NULL, scope_key TEXT)"
        )
        rows = []
        for old, (new_id, new_sha, key) in mapping.items():
            surv = survivors[new_id]
            rows.append((old, surv, new_id, new_sha, key))
        conn.executemany(
            "INSERT INTO _decision_remap VALUES(?,?,?,?,?)", rows
        )
        conn.execute("CREATE INDEX _decision_remap_new ON _decision_remap(new_id)")
        # 1. Foreign keys, one statement per column, while old ids still
        #    exist to match on.  Every reference lands on the survivor's new
        #    id; a reference to a loser lands on the same new id, which is
        #    what collapsing means.
        for table, column in FK_COLUMNS:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone() is None:
                continue
            cur = conn.execute(
                f"UPDATE {table} SET {column}="
                f" (SELECT new_id FROM _decision_remap WHERE old_id={table}.{column})"
                f" WHERE {column} IN (SELECT old_id FROM _decision_remap)"
            )
            repointed[f"{table}.{column}"] = int(cur.rowcount)
        # 2. Drop the restatements; their references already moved.
        losers = conn.execute(
            "DELETE FROM auto_trade_decisions WHERE decision_id IN"
            " (SELECT old_id FROM _decision_remap WHERE old_id<>survivor_old_id)"
        ).rowcount
        # 3. Re-address the survivors in one statement.
        readdressed = conn.execute(
            "UPDATE auto_trade_decisions SET"
            "  decision_sha256=(SELECT new_sha FROM _decision_remap m"
            "                    WHERE m.old_id=auto_trade_decisions.decision_id),"
            "  decision_scope_key=(SELECT scope_key FROM _decision_remap m"
            "                       WHERE m.old_id=auto_trade_decisions.decision_id),"
            "  decision_id=(SELECT new_id FROM _decision_remap m"
            "                WHERE m.old_id=auto_trade_decisions.decision_id)"
            " WHERE decision_id IN"
            "  (SELECT old_id FROM _decision_remap WHERE old_id=survivor_old_id)"
        ).rowcount
        conn.execute("DROP TABLE _decision_remap")
        for _name, sql in guards:
            conn.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "schema": "decision_identity_migration_apply_v2",
        "guards_lifted_and_restored": [name for name, _ in guards],
        "deleted_restatements": int(losers),
        "readdressed_survivors": int(readdressed),
        "fk_rows_repointed": repointed,
    }


def verify(conn: sqlite3.Connection, planned: dict[str, Any]) -> dict[str, Any]:
    """Every decision addressed by v2, every reference resolving."""

    decisions = int(conn.execute(
        "SELECT COUNT(*) FROM auto_trade_decisions WHERE strategy_lane=?",
        (LANE,),
    ).fetchone()[0])
    dangling: dict[str, int] = {}
    for table, column in FK_COLUMNS:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is None:
            continue
        dangling[f"{table}.{column}"] = int(conn.execute(
            f"SELECT COUNT(*) FROM {table} t"
            f" WHERE t.{column} LIKE 'decision_%' AND NOT EXISTS"
            f" (SELECT 1 FROM auto_trade_decisions d"
            f"  WHERE d.decision_id=t.{column})"
        ).fetchone()[0])
    # Recompute from the surviving rows and confirm the stored sha matches.
    scope_by_decision = {
        str(r[0]): str(r[1] or "") for r in conn.execute(
            "SELECT decision_id, decision_scope_key"
            " FROM basket_policy_training_fact"
        )
    }
    mismatched = 0
    for (did, sha, st, day, tz, lane, state, will, rj, dj, key) in conn.execute(
        "SELECT decision_id, decision_sha256, station_id, event_date,"
        " settlement_timezone, strategy_lane, decided_state, will_order,"
        " reason_codes_json, details_json, decision_scope_key"
        " FROM auto_trade_decisions WHERE strategy_lane=?", (LANE,),
    ):
        identity = economic_decision_identity(
            station_id=st, event_date=day, settlement_timezone=tz,
            strategy_lane=lane, decided_state=state, will_order=int(will),
            reason_codes=json.loads(rj or "[]"), details=json.loads(dj or "{}"),
            decision_scope_key=str(key) if key else None,
        )
        new_sha, new_id = _derive(identity)
        if new_sha != str(sha) or new_id != str(did):
            mismatched += 1
    return {
        "schema": "decision_identity_migration_verify_v1",
        "decisions_after": decisions,
        "expected_after": planned["distinct_new_ids"],
        "count_matches_plan": decisions == planned["distinct_new_ids"],
        "dangling_references": dangling,
        "all_references_resolve": all(v == 0 for v in dangling.values()),
        "rows_not_reproducing_their_own_id": mismatched,
    }


def _quiescent(path: Path) -> tuple[bool, str]:
    """No resident writer.  The WAL is checkpointed by us, not required empty.

    A checkpointed WAL file is not truncated by default and can sit at its
    high-water mark (64 MiB here) with nothing in it, so "WAL is empty" is
    the wrong test.  The right one is that nobody else can write; then we
    take the checkpoint ourselves inside the apply connection.
    """

    # Ask launchd, not pgrep: ``pgrep -f`` matches any process whose command
    # text contains the label -- on 2026-09-02 that was another session's
    # shell grepping the daemon's source, and the check refused a migration
    # against a resident that launchd had already booted out.
    uid = os.getuid()
    loaded = os.system(
        f"/bin/launchctl print gui/{uid}/com.weather.weather_basket_yes"
        " >/dev/null 2>&1"
    ) == 0
    if loaded:
        return False, "resident daemon is loaded; stop it first (targetctl.sh stop)"
    # Who has THIS file open -- not who has a ledger process anywhere.  A
    # replay arm on a scratch database is not a production writer, and the
    # first version of this check refused on exactly that.
    holders = [
        line.split()[1] for line in
        os.popen(f"lsof -- '{path}' 2>/dev/null").read().splitlines()[1:]
        if line.strip()
    ]
    holders = sorted({h for h in holders if h != str(os.getpid())})
    # Other holders are readers until proven otherwise: in WAL mode a reader
    # neither blocks BEGIN IMMEDIATE nor is blocked by it, and the apply
    # connection carries a 300 s busy_timeout for the one thing that could
    # hold RESERVED.  A refusal here on 2026-09-02 was another session's
    # dbstat scan of the whole file -- a reader that would have run for
    # tens of minutes, during which production sat stopped for nothing.
    # Reported on the receipt, not refused.
    note = f"other holders (readers assumed): {','.join(holders)}" if holders else "no other holder"
    return True, note


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--rehearse", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--db", type=Path, default=SSOT)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if args.plan:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120)
        planned = plan(conn)
        report = {k: v for k, v in planned.items() if not k.startswith("_")}
    elif args.rehearse:
        tmp = Path(tempfile.mkdtemp(prefix="decision_identity_rehearsal_"))
        copy = tmp / "rehearsal.db"
        src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120)
        dst = sqlite3.connect(str(copy))
        dst.execute("PRAGMA journal_mode=OFF")
        tables = ["auto_trade_decisions"] + sorted({t for t, _ in FK_COLUMNS})
        copied: dict[str, int] = {}
        for table in tables:
            ddl = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if ddl is None:
                continue
            dst.execute(ddl[0])
            cur = src.execute(f"SELECT * FROM {table}")
            n = 0
            while True:
                batch = cur.fetchmany(5000)
                if not batch:
                    break
                marks = ",".join("?" for _ in batch[0])
                dst.executemany(f"INSERT INTO {table} VALUES({marks})", batch)
                n += len(batch)
            dst.commit()
            copied[table] = n
        # Triggers last, after the rows: the copy must carry the same
        # append-only guards production has, or a rehearsal proves nothing
        # about the apply -- which is exactly what happened the first time.
        for table in tables:
            for name, sql in src.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
                " AND tbl_name=? AND sql IS NOT NULL", (table,),
            ):
                dst.execute(str(sql))
        copied["_triggers"] = int(dst.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
        ).fetchone()[0])
        dst.commit()
        src.close()
        t0 = time.perf_counter()
        planned = plan(dst)
        applied = apply(dst, planned)
        verified = verify(dst, planned)
        dst.close()
        shutil.rmtree(tmp, ignore_errors=True)
        report = {
            "mode": "rehearse",
            "copied_rows": copied,
            "plan": {k: v for k, v in planned.items() if not k.startswith("_")},
            "apply": applied,
            "verify": verified,
            "elapsed_seconds": round(time.perf_counter() - t0, 2),
            "rehearsal_db_removed": True,
        }
    else:
        ok, why = _quiescent(args.db)
        if not ok:
            print(json.dumps({"refused": why}, ensure_ascii=False))
            return 2
        print(json.dumps({"quiescence": why}, ensure_ascii=False), file=sys.stderr)
        conn = sqlite3.connect(str(args.db), timeout=300)
        conn.execute("PRAGMA busy_timeout=300000")
        # PASSIVE, never TRUNCATE.  A TRUNCATE checkpoint waits for every
        # reader to release its snapshot; on 2026-09-02 that reader was
        # another session's dbstat scan of the whole 29 GB file, the apply
        # sat on it for four minutes with production stopped, and did
        # nothing.  The migration does not need a checkpoint at all -- its
        # BEGIN IMMEDIATE takes RESERVED, which readers neither hold nor
        # are blocked by -- so this is a courtesy that must not cost time.
        ck = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        print(json.dumps({"wal_checkpoint_passive": list(ck)}), file=sys.stderr)
        planned = plan(conn)
        applied = apply(conn, planned)
        verified = verify(conn, planned)
        conn.close()
        report = {
            "mode": "apply",
            "plan": {k: v for k, v in planned.items() if not k.startswith("_")},
            "apply": applied,
            "verify": verified,
        }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
