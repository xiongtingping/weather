#!/usr/bin/env python3
"""Retire the class-A target-SSOT rows written before 2026-09-05.

User ruling 2026-09-19 ("上限提到 60 + 删 A 类").  The store was 55.0 GiB
against a 56 GiB ceiling and burning 3-4 GiB a day.  Deleting rows never
shrinks an SQLite file, but the freed pages go on the freelist and every
later write reuses them before the file grows again -- so retiring ~17 GiB of
pages buys that many GiB of growth without touching the ceiling.

Class A is the set nothing reads once its day has passed: production reads
each of these per current scope (latest row, or by the id a current decision
names) and neither the paper ledger nor the scorecards read them before
2026-09-14.  Measured 2026-09-19 by rowid sampling:

  basket_p0_preselection_plan          4.03 GiB of payload before the cutoff
  basket_probability_envelope          6.92
  basket_probability_update_proof      0.75
  auto_trade_decisions                 0.74
  basket_regime_artifact               0.50
  basket_confirmed_inventory_snapshot  0.16
  basket_order_state_snapshot          0.12
  basket_portfolio_inventory_snapshot  0.07
  canonical_ssot_fact_v16_replaced     0.20  (the whole migration copy)

Three rules keep this from breaking anything that stays:

1. A row something else still names is kept.  Decisions named by an order
   intent, a training fact, a candidate window, a version vector, a
   supersession receipt or the chain's last action -- or whose cycle an
   execution cycle row still carries -- stay; so do envelopes and
   inventory/order-state snapshots a training fact or version vector names,
   every snapshot of a station-day that ever had an order intent, and every
   class-A row of a station-day that had a live-authorised intent.  Class-B tables (the WS
   book, the bridge history, the training facts) are untouched, so nothing
   they point at may disappear.
2. Every retired row is exported before it is deleted: one gzip member per
   batch under ``--export-dir``, fsynced, and the manifest line is written
   only after the delete commits.  ``--restore`` puts rows back verbatim
   (``INSERT OR IGNORE`` by rowid), so a crash between export and commit
   leaves nothing to reconcile.
3. The append-only triggers are dropped and recreated from their own
   ``sqlite_master`` text inside each batch's transaction, so no other
   connection ever sees a table unguarded, and a batch holds the write lock
   for about a second before stepping aside for the resident.

Dry-run is the default; ``--apply`` deletes.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_EXPORT_DIR = Path.home() / "weather_archive_exports" / "ssot_class_a_pre_20260905"
CUTOFF = "2026-09-05"
# Rows are scanned up to the first rowid whose time is two days past the
# cutoff: rowid order tracks write order only to within a few minutes on
# these tables, and the margin keeps a late-written row from being missed.
SCAN_BOUND = "2026-09-07"

# A batch holds the one SSOT write lock the resident and its sweeps share.
# Measured on the first tables: 5,000 update proofs held it 4.5s against a
# 0.3s pause, which left the resident's sweeps the gaps.  Batches are now
# small enough to hold it about a second, and the pause is at least as long
# as the hold, so this run never owns more than half the lock's time.
BATCH_BYTES = 8 * 1024 * 1024
BATCH_ROWS = 1500
PAUSE_SEC = 0.5
MAX_WAL_BYTES = 256 * 1024 * 1024
MIN_FREE_DISK_BYTES = 5 * 1024 ** 3

# Tables that name decisions, and the column they name them by.
DECISION_REFERENCES = (
    ("basket_policy_training_fact", "decision_id"),
    ("basket_candidate_window_evidence", "decision_id"),
    ("basket_target_chain_last_action", "decision_id"),
    ("basket_target_execution_version_vectors", "decision_id"),
    ("basket_target_order_intents", "decision_id"),
    ("basket_target_order_intents_pre_go_live", "decision_id"),
    ("basket_target_prepared_supersession_receipts", "old_decision_id"),
    ("basket_target_prepared_supersession_receipts", "new_decision_id"),
)
CYCLE_REFERENCES = (
    ("basket_target_execution_cycles", "cycle_id"),
    ("basket_target_order_intents", "cycle_id"),
    ("basket_target_order_intents_pre_go_live", "cycle_id"),
    ("basket_target_execution_version_vectors", "cycle_id"),
)

# Which station-days keep every class-A row regardless of date:
#   "live" -- a station-day that had a live-authorised order intent (the 41
#             intents of 2026-08-31..09-04 are the audit trail of real money)
#   "any"  -- a station-day that had any order intent at all
# (table, time column, [(key column, [(referencing table, column), ...])],
#  station-day guard)
TABLES = (
    ("basket_p0_preselection_plan", "decision_at_utc", (), "live"),
    (
        "basket_probability_envelope", "decision_at_utc",
        (("envelope_id", (
            ("basket_policy_training_fact", "probability_envelope_id"),
            ("tiebreak_lane_evaluation", "envelope_id"),
        )),),
        "live",
    ),
    ("basket_probability_update_proof", "decision_at_utc", (), "live"),
    ("basket_regime_artifact", "created_at", (), "live"),
    (
        "auto_trade_decisions", "created_at",
        (("decision_id", DECISION_REFERENCES), ("cycle_id", CYCLE_REFERENCES)),
        "live",
    ),
    (
        "basket_confirmed_inventory_snapshot", "observed_at",
        (
            ("inventory_snapshot_id", (("basket_policy_training_fact", "inventory_snapshot_id"),)),
            ("content_sha256", (
                ("basket_policy_training_fact", "inventory_sha256"),
                ("basket_target_execution_version_vectors", "inventory_sha256"),
            )),
        ),
        "any",
    ),
    (
        "basket_order_state_snapshot", "observed_at",
        (
            ("order_state_snapshot_id", (("basket_policy_training_fact", "order_state_snapshot_id"),)),
            ("content_sha256", (
                ("basket_policy_training_fact", "order_state_sha256"),
                ("basket_target_execution_version_vectors", "order_state_sha256"),
            )),
        ),
        "any",
    ),
    ("basket_portfolio_inventory_snapshot", "created_at", (), None),
    # The v16 copy is retired whole: the time column is only its ordering.
    ("canonical_ssot_fact_v16_replaced", None, (), None),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _referenced(conn: sqlite3.Connection, refs) -> set:
    out: set = set()
    for table, column in refs:
        if not _table_exists(conn, table) or column not in _columns(conn, table):
            continue
        for (value,) in conn.execute(
            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
        ):
            out.add(value)
    return out


def _intent_station_days(conn: sqlite3.Connection, *, live_only: bool) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for table in ("basket_target_order_intents", "basket_target_order_intents_pre_go_live"):
        if not _table_exists(conn, table):
            continue
        where = " WHERE i.live_submit_authorized<>0" if live_only else ""
        for station, day in conn.execute(
            f'SELECT DISTINCT d.station_id, d.event_date FROM "{table}" i'
            f" JOIN auto_trade_decisions d ON d.decision_id = i.decision_id{where}"
        ):
            out.add((station, day))
    return out


def _delete_triggers(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
        (table,),
    ).fetchall()
    return [
        (str(name), str(sql)) for name, sql in rows
        if "BEFORE DELETE" in " ".join(str(sql).upper().split())
    ]


def _scan_bound(conn: sqlite3.Connection, table: str, column: str | None) -> int:
    lo, hi = conn.execute(f'SELECT MIN(rowid), MAX(rowid) FROM "{table}"').fetchone()
    if lo is None:
        return -1
    if column is None:
        return int(hi)
    probe = f'SELECT "{column}" FROM "{table}" WHERE rowid>=? ORDER BY rowid LIMIT 1'
    # First rowid whose time is at or past SCAN_BOUND.
    while lo < hi:
        mid = (lo + hi) // 2
        row = conn.execute(probe, (mid,)).fetchone()
        if row is None or (row[0] is not None and str(row[0]) >= SCAN_BOUND):
            hi = mid
        else:
            lo = mid + 1
    return int(lo)


def _encode(value):
    if isinstance(value, bytes):
        return {"__b64__": base64.b64encode(value).decode("ascii")}
    return value


def _decode(value):
    if isinstance(value, dict) and set(value) == {"__b64__"}:
        return base64.b64decode(value["__b64__"])
    return value


def _free_disk(path: Path) -> int:
    return shutil.disk_usage(path).free


def _wal_bytes(db: Path) -> int:
    wal = db.with_name(db.name + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def _wait_for_wal(db: Path, conn: sqlite3.Connection | None = None) -> None:
    """Hold off while the WAL is large, helping the checkpoint along.

    The resident's own writes keep the WAL near its 512 MiB guard only if
    nobody checkpoints; a passive checkpoint from here copies frames without
    blocking anyone, and the WAL resets at the resident's next write.
    """
    waited = 0.0
    while _wal_bytes(db) > MAX_WAL_BYTES:
        if waited > 1800:
            raise SystemExit(f"WAL stayed above {MAX_WAL_BYTES} bytes for 1800s; stopping")
        if conn is not None:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
        time.sleep(10)
        waited += 10


def retire_table(conn, db: Path, spec, *, apply: bool, export_dir: Path, protect_cache: dict, log,
                 pause_factor: float = 1.0, batch_bytes_limit: int = BATCH_BYTES) -> dict:
    table, column, protections, day_guard = spec
    if not _table_exists(conn, table):
        return {"table": table, "state": "missing"}
    cols = _columns(conn, table)
    key_sets = []
    for key, refs in protections:
        cache_key = (key, refs)
        if cache_key not in protect_cache:
            protect_cache[cache_key] = _referenced(conn, refs)
        key_sets.append((cols.index(key), protect_cache[cache_key]))
    intent_days = protect_cache[f"intent_days_{day_guard}"] if day_guard else None
    si = cols.index("station_id") if day_guard else None
    ei = cols.index("event_date") if day_guard else None
    ti = cols.index(column) if column else None

    bound = _scan_bound(conn, table, column)
    triggers = _delete_triggers(conn, table)
    stats = {"table": table, "time_column": column, "scan_bound_rowid": bound,
             "deleted_rows": 0, "deleted_payload_bytes": 0, "kept_referenced": 0,
             "kept_intent_day": 0, "batches": 0, "max_lock_sec": 0.0}
    export_path = export_dir / f"{table}.jsonl.gz"
    manifest_path = export_dir / "manifest.jsonl"
    select = (
        f'SELECT rowid, * FROM "{table}" WHERE rowid>? AND rowid<=?'
        " ORDER BY rowid LIMIT ?"
    )
    last = -1
    batch: list[tuple] = []
    batch_bytes = 0

    def flush() -> None:
        nonlocal batch, batch_bytes
        if not batch:
            return
        ids = [r[0] for r in batch]
        if not apply:
            stats["deleted_rows"] += len(ids)
            stats["deleted_payload_bytes"] += batch_bytes
            stats["batches"] += 1
            batch, batch_bytes = [], 0
            return
        lines = [
            json.dumps({"table": table, "rowid": r[0],
                        "row": {c: _encode(v) for c, v in zip(cols, r[1:])}},
                       ensure_ascii=False, separators=(",", ":"))
            for r in batch
        ]
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        if apply:
            if _free_disk(export_dir) < MIN_FREE_DISK_BYTES:
                raise SystemExit("free disk below 5 GiB; stopping before the next batch")
            _wait_for_wal(db, conn)
            with open(export_path, "ab") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as gz:
                    gz.write(payload)
                raw.flush()
                os.fsync(raw.fileno())
            queued = time.monotonic()
            conn.execute("BEGIN IMMEDIATE")
            # Held time starts once the lock is ours; the wait before it is
            # the resident's own write, not this batch's.
            started = time.monotonic()
            stats["max_wait_sec"] = max(stats.get("max_wait_sec", 0.0), started - queued)
            try:
                for name, _sql in triggers:
                    conn.execute(f'DROP TRIGGER "{name}"')
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    conn.execute(
                        f'DELETE FROM "{table}" WHERE rowid IN ({",".join("?" * len(chunk))})',
                        chunk,
                    )
                for _name, sql in triggers:
                    conn.execute(sql)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            held = time.monotonic() - started
            stats["max_lock_sec"] = max(stats["max_lock_sec"], held)
            present = {
                str(r[0]) for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                    (table,),
                )
            }
            missing = [name for name, _ in triggers if name not in present]
            if missing:
                raise SystemExit(f"triggers missing after commit on {table}: {missing}")
            with open(manifest_path, "a", encoding="utf-8") as mf:
                mf.write(json.dumps({
                    "table": table, "first_rowid": ids[0], "last_rowid": ids[-1],
                    "rows": len(ids), "payload_sha256": digest,
                    "export": export_path.name, "committed_at_utc": _now(),
                    "lock_sec": round(held, 3),
                }) + "\n")
                mf.flush()
                os.fsync(mf.fileno())
            time.sleep(max(PAUSE_SEC, held * pause_factor))
        stats["deleted_rows"] += len(ids)
        stats["deleted_payload_bytes"] += len(payload)
        stats["batches"] += 1
        if stats["batches"] % 20 == 0:
            log(f"  {table}: {stats['deleted_rows']} rows, "
                f"{stats['deleted_payload_bytes'] / 2**30:.2f} GiB, "
                f"max lock {stats['max_lock_sec']:.2f}s, wal {_wal_bytes(db) / 2**20:.0f} MiB")
        batch, batch_bytes = [], 0

    while True:
        rows = conn.execute(select, (last, bound, 500)).fetchall()
        if not rows:
            break
        last = rows[-1][0]
        for r in rows:
            values = r[1:]
            if ti is not None:
                stamp = values[ti]
                if stamp is None or str(stamp) >= CUTOFF:
                    continue
            if any(values[idx] in keep for idx, keep in key_sets):
                stats["kept_referenced"] += 1
                continue
            if intent_days is not None and (values[si], values[ei]) in intent_days:
                stats["kept_intent_day"] += 1
                continue
            width = sum(len(v) if isinstance(v, (str, bytes)) else 8 for v in values)
            batch.append(r)
            batch_bytes += width
            if batch_bytes >= batch_bytes_limit or len(batch) >= BATCH_ROWS:
                flush()
    flush()
    stats["state"] = "applied" if apply else "planned"
    return stats


def restore(db: Path, export_dir: Path, tables: list[str] | None, *,
            pause_factor: float = 1.0, batch_bytes_limit: int = BATCH_BYTES) -> None:
    """Put exported rows back, yielding the write lock the way the retirement did."""
    conn = sqlite3.connect(db, timeout=60, isolation_level=None)
    conn.execute("PRAGMA journal_size_limit=67108864")
    for path in sorted(export_dir.glob("*.jsonl.gz")):
        table = path.name[: -len(".jsonl.gz")]
        if tables and table not in tables:
            continue
        restored = 0
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            batch: list = []
            size = 0
            for line in fh:
                doc = json.loads(line)
                row = {k: _decode(v) for k, v in doc["row"].items()}
                batch.append((doc["rowid"], row))
                size += len(line)
                if size >= batch_bytes_limit or len(batch) >= BATCH_ROWS:
                    _wait_for_wal(db, conn)
                    held = _insert_retrying(conn, table, batch)
                    before = restored
                    restored += len(batch)
                    batch, size = [], 0
                    time.sleep(max(PAUSE_SEC, held * pause_factor))
                    if restored // 10000 != before // 10000:
                        print(f"  {table}: {restored} rows, last hold {held:.2f}s",
                              file=sys.stderr, flush=True)
            if batch:
                _insert(conn, table, batch)
                restored += len(batch)
        print(f"restored {table}: {restored} rows (duplicates in the export are ignored)", flush=True)


def _insert_retrying(conn, table: str, batch, attempts: int = 20) -> float:
    """``_insert``, waiting out a resident restart that holds the lock past the timeout.

    Measured 2026-09-19 16:57Z: a restart's start-up held the write lock for
    more than the 60s connection timeout, and the restore died on its next
    BEGIN IMMEDIATE.  Nothing had been written, so waiting and retrying is
    exactly the same batch.
    """
    for attempt in range(attempts):
        try:
            return _insert(conn, table, batch)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts - 1:
                raise
            print(f"  {table}: write lock busy ({exc}); retrying in 30s",
                  file=sys.stderr, flush=True)
            time.sleep(30)
    raise AssertionError("unreachable")


def _insert(conn, table: str, batch) -> float:
    """Insert one batch; return how long it held the write lock."""
    if not batch:
        return 0.0
    cols = list(batch[0][1])
    sql = (
        f'INSERT OR IGNORE INTO "{table}" (rowid,{",".join(chr(34) + c + chr(34) for c in cols)})'
        f' VALUES ({",".join("?" * (len(cols) + 1))})'
    )
    # The v16 copy refuses inserts outright; its guard is lifted and put back
    # inside the same transaction, exactly as the retirement lifted the
    # delete guards.
    guards = [
        (str(name), str(text)) for name, text in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        )
        if "BEFORE INSERT" in " ".join(str(text).upper().split())
    ]
    conn.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        for name, _text in guards:
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.executemany(sql, [(rid, *(row[c] for c in cols)) for rid, row in batch])
        for _name, text in guards:
            conn.execute(text)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--tables", nargs="*")
    # Measured 2026-09-19: at factor 1 the envelope pass saturated the disk
    # (~107 MB/s, 9.4k IOPS) and one production cycle ran 11 minutes against
    # a usual 5-9.  Factor 3 yields three times each hold.
    parser.add_argument("--pause-factor", type=float, default=1.0)
    parser.add_argument("--batch-bytes", type=int, default=BATCH_BYTES)
    args = parser.parse_args(argv)
    db = args.db.resolve()
    if args.restore:
        restore(db, args.export_dir, args.tables,
                pause_factor=args.pause_factor, batch_bytes_limit=args.batch_bytes)
        return 0
    args.export_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=60, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_size_limit=67108864")

    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    specs = [s for s in TABLES if not args.tables or s[0] in args.tables]
    # Every protection set is read before anything is deleted, so a later
    # table's guard never depends on what an earlier table kept.
    cache: dict = {
        "intent_days_live": _intent_station_days(conn, live_only=True),
        "intent_days_any": _intent_station_days(conn, live_only=False),
    }
    for _table, _column, protections, _days in specs:
        for key, refs in protections:
            cache.setdefault((key, refs), _referenced(conn, refs))
    log("protection sets: " + ", ".join(
        f"{k[0] if isinstance(k, tuple) else k}={len(v)}" for k, v in cache.items()))
    results = []
    freelist_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    for spec in specs:
        started = time.monotonic()
        res = retire_table(conn, db, spec, apply=args.apply,
                           export_dir=args.export_dir, protect_cache=cache, log=log,
                           pause_factor=args.pause_factor, batch_bytes_limit=args.batch_bytes)
        res["elapsed_sec"] = round(time.monotonic() - started, 1)
        results.append(res)
        log(json.dumps(res))
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    freelist_after = conn.execute("PRAGMA freelist_count").fetchone()[0]
    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "cutoff": CUTOFF,
        "deleted_rows": sum(r.get("deleted_rows", 0) for r in results),
        "deleted_payload_gib": round(sum(r.get("deleted_payload_bytes", 0) for r in results) / 2**30, 2),
        "freelist_gib_before": round(freelist_before * page_size / 2**30, 2),
        "freelist_gib_after": round(freelist_after * page_size / 2**30, 2),
        "tables": results,
    }
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
