#!/usr/bin/env python3
"""Rebuild the SSOT compactly onto another volume, folding the book's tail.

User ruling 2026-09-21 (「全库重建到外置盘」).  Two things at once:

* **Compaction.**  The store is 55.05 GiB of which about 37.3 is row data;
  the rest is 404 indexes and page slack, and 3.53 GiB is already free pages
  that in-place VACUUM cannot reclaim (it wants the database's own size in
  scratch and the volume has eight GiB).  A fresh database written in rowid
  order with its indexes built at the end has neither the slack nor the
  freelist.
* **The historical fold.**  Charter 168 folds each book's tail at write, but
  only for new rows.  Applied to the 595k stored snapshots the same way --
  five levels verbatim, the rest as one level carrying their shares at their
  share-weighted price -- that column goes to about 44% of itself.

What this does NOT do is swap.  It writes a new file and proves it; putting
it in place is ``--swap``, a separate run with its own checks, because the
two failures are different: a bad build costs time, a bad swap costs the
store.

The build requires the residents stopped.  Any row written after the read
begins is simply not in the copy, and there is no way to notice that later:
the copy is internally consistent and quietly behind.  The script refuses to
start while the writer is running rather than producing one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.market_ingress import fold_depth_tail  # noqa: E402

SCHEMA = "ssot_compact_rebuild_receipt_v1"
DEFAULT_SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
#: The one column rewritten on the way through, and how.
FOLD_TABLE = "basket_official_ws_snapshot"
FOLD_COLUMN = "buckets_json"
#: The second rewrite: ``basket_p0_preselection_plan`` stores the same
#: ``delta_v2_audit`` object twice in every row -- once at
#: ``compiled_plan.delta_v2_audit`` and once at ``compiled_plan.plan
#: .delta_v2_audit`` -- because ``runtime`` copies the outer one up out of the
#: inner one and then stores the inner plan as well.  4,353 bytes a row across
#: 119,596 rows, about 0.46 GiB, byte-identical in 300 of 300 sampled rows.
#: The inner copy is dropped here and the reader
#: (``p0_preselection._decision_semantic_projection``) falls back to the outer
#: one; measured over 1,204 rows spanning the table, 1,184 carried both with
#: none differing, 20 carried only the outer, and NONE carried only the inner,
#: which is what makes this lossless.  ``plan_sha256`` hashes the projection,
#: not this column, and 1,507 stored rows reproduce their stored hash under
#: the changed reader.  New rows are still written with both: stopping that
#: means separating the in-memory plan from the stored one, which is a change
#: of its own and not one to bundle into a rebuild.
DEDUPE_TABLE = "basket_p0_preselection_plan"
DEDUPE_COLUMN = "plan_json"
#: Rows per transaction on the target.  Large enough that the commit cost
#: disappears, small enough that the target's WAL never holds a whole table.
BATCH = 2000
#: Tables whose digest is compared row for row after the copy.  Every table's
#: COUNT is checked; a full digest of 24.3M rows would double the build, so
#: the ones production cannot be wrong about are named here.
DIGEST_TABLES = (
    "basket_policy_training_fact",
    "basket_policy_training_label",
    "basket_target_order_intents",
    "basket_target_order_reconciliations",
    "auto_trade_decisions",
    "basket_station_day_settlement_label",
    "calibration_station_daily_high_truth",
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _residents_running() -> list[str]:
    """Which of the writing residents are up.  The build cannot run beside one."""

    import subprocess

    try:
        listing = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001
        return ["launchctl_unavailable"]
    running = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[2] in {
            "com.weather.weather_basket_yes", "com.weather.market_ws",
        }:
            running.append(parts[2])
    return running


def _objects(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """Every schema object, split by kind and kept in creation-safe order."""

    out: dict[str, list[tuple[str, str]]] = {"table": [], "index": [], "trigger": [], "view": []}
    for kind, name, sql in conn.execute(
        "SELECT type,name,sql FROM main.sqlite_master"
        " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
    ):
        if str(kind) in out:
            out[str(kind)].append((str(name), str(sql)))
    return out


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _digest(conn: sqlite3.Connection, schema: str, table: str, columns: list[str]) -> str:
    digest = hashlib.sha256()
    listed = ",".join(f'"{c}"' for c in columns)
    for row in conn.execute(f'SELECT rowid,{listed} FROM {schema}."{table}" ORDER BY rowid'):
        digest.update(json.dumps(list(row), default=str, ensure_ascii=False).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _fold_payload(raw: str | None) -> str | None:
    """Charter 168's fold, applied to one stored snapshot.

    Anything that does not parse, or that the fold leaves alone, comes back
    byte for byte: a rebuild is not the place to discover a new encoding.
    """

    if not raw:
        return raw
    try:
        buckets = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(buckets, list):
        return raw
    changed = False
    for entry in buckets:
        if not isinstance(entry, dict):
            continue
        for side, ascending in (("ask_levels", True), ("bid_levels", False)):
            levels = entry.get(side)
            if not isinstance(levels, list) or not levels:
                continue
            folded = fold_depth_tail(levels, ascending=ascending)
            if len(folded) != len(levels):
                entry[side] = folded
                changed = True
    if not changed:
        return raw
    # DEFAULT separators: that is what this column is stored with (verified
    # against a stored row, which reproduces byte for byte this way and is
    # 604 bytes shorter under compact ones).  Matching the writer keeps a
    # rebuilt row indistinguishable from one the ingress wrote.
    return json.dumps(buckets, ensure_ascii=False, sort_keys=True)


def _dedupe_payload(raw: str | None) -> str | None:
    """Drop the inner ``delta_v2_audit`` when it is the outer one verbatim.

    Only when both are present AND identical: a row whose two copies differed
    would be one this rewrite does not understand, and it comes back
    untouched.  So does anything that does not parse.
    """

    if not raw:
        return raw
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    compiled = document.get("compiled_plan") if isinstance(document, dict) else None
    if not isinstance(compiled, dict):
        return raw
    plan = compiled.get("plan")
    if not isinstance(plan, dict) or "delta_v2_audit" not in plan:
        return raw
    outer = compiled.get("delta_v2_audit")
    if not isinstance(outer, dict):
        return raw
    canon = lambda o: json.dumps(o, sort_keys=True, ensure_ascii=False, default=str)
    if canon(outer) != canon(plan["delta_v2_audit"]):
        return raw
    del plan["delta_v2_audit"]
    # Compact separators, because that is what this column is stored with.
    # ``buckets_json`` beside it uses the DEFAULT ones, and re-serialising
    # either in the other's form rewrites every row for nothing: measured on
    # a stored row, plan_json is 76,333 bytes compact and 79,323 with the
    # default separators, so the wrong choice here gave back two thirds of
    # what the dedupe saved.
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def build(*, source: Path, target: Path, force: bool = False) -> dict[str, object]:
    """Write the compact copy.  Nothing is swapped and the source is untouched."""

    receipt: dict[str, object] = {
        "schema": SCHEMA, "started_at_utc": _utc(),
        "source": str(source), "target": str(target), "state": "STARTED",
    }
    running = _residents_running()
    if running and not force:
        receipt["state"] = "REFUSED_RESIDENT_RUNNING"
        receipt["running"] = running
        receipt["reason"] = (
            "a row written after the read begins is simply absent from the copy,"
            " and the copy looks consistent either way; stop the residents first"
        )
        return receipt
    if target.exists():
        receipt["state"] = "REFUSED_TARGET_EXISTS"
        return receipt

    t0 = time.time()
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=300.0)
    conn.execute("PRAGMA busy_timeout=300000")
    try:
        objects = _objects(conn)
        tables = [name for name, _sql in objects["table"]]
        counts = {
            name: int(conn.execute(f'SELECT COUNT(*) FROM main."{name}"').fetchone()[0])
            for name in tables
        }
        receipt["tables"] = len(tables)
        receipt["indexes"] = len(objects["index"])
        receipt["triggers"] = len(objects["trigger"])
        receipt["views"] = len(objects["view"])
        receipt["source_rows"] = sum(counts.values())

        target.parent.mkdir(parents=True, exist_ok=True)
        out = sqlite3.connect(str(target), timeout=300.0)
        try:
            out.execute("PRAGMA journal_mode=OFF")     # nothing to recover: a
            out.execute("PRAGMA synchronous=OFF")      # failed build is deleted
            out.execute(f"PRAGMA page_size={conn.execute('PRAGMA page_size').fetchone()[0]}")
            # Tables first, WITHOUT their indexes or triggers.  An index built
            # row by row is both slower and looser than one built once at the
            # end, and a trigger that fires during the copy would rewrite the
            # history it is meant to protect.
            for _name, sql in objects["table"]:
                out.execute(sql)
            out.commit()

            copied: dict[str, int] = {}
            folded_rows = 0
            deduped_rows = 0
            for name in tables:
                cols = _columns(conn, "main", name)
                listed = ",".join(f'"{c}"' for c in cols)
                marks = ",".join("?" for _ in cols)
                fold_at = cols.index(FOLD_COLUMN) if (
                    name == FOLD_TABLE and FOLD_COLUMN in cols
                ) else None
                dedupe_at = cols.index(DEDUPE_COLUMN) if (
                    name == DEDUPE_TABLE and DEDUPE_COLUMN in cols
                ) else None
                cursor = conn.execute(f'SELECT {listed} FROM main."{name}" ORDER BY rowid')
                written = 0
                while True:
                    chunk = cursor.fetchmany(BATCH)
                    if not chunk:
                        break
                    if fold_at is not None:
                        rebuilt = []
                        for row in chunk:
                            values = list(row)
                            folded = _fold_payload(values[fold_at])
                            if folded is not values[fold_at]:
                                folded_rows += 1
                            values[fold_at] = folded
                            rebuilt.append(values)
                        chunk = rebuilt
                    elif dedupe_at is not None:
                        rebuilt = []
                        for row in chunk:
                            values = list(row)
                            trimmed = _dedupe_payload(values[dedupe_at])
                            if trimmed is not values[dedupe_at]:
                                deduped_rows += 1
                            values[dedupe_at] = trimmed
                            rebuilt.append(values)
                        chunk = rebuilt
                    out.executemany(f'INSERT INTO "{name}"({listed}) VALUES({marks})', chunk)
                    out.commit()
                    written += len(chunk)
                copied[name] = written
                print(f"  {name:56} {written:>10,}  {time.time()-t0:>6.0f}s", flush=True)
            receipt["copied_rows"] = sum(copied.values())
            receipt["folded_snapshots"] = folded_rows
            receipt["deduped_preselections"] = deduped_rows

            mismatched = {n: (counts[n], copied.get(n, -1)) for n in tables if counts[n] != copied.get(n)}
            if mismatched:
                receipt["state"] = "FAILED_ROW_COUNT"
                receipt["mismatched"] = mismatched
                return receipt

            print(f"  building {len(objects['index'])} indexes…", flush=True)
            for _name, sql in objects["index"]:
                out.execute(sql)
            out.commit()
            for _name, sql in objects["view"]:
                out.execute(sql)
            # Triggers last of all: the append-only guards must not see the copy.
            for _name, sql in objects["trigger"]:
                out.execute(sql)
            out.commit()
            out.execute("PRAGMA journal_mode=WAL")
        finally:
            out.close()
        receipt["build_seconds"] = round(time.time() - t0, 1)
        receipt["state"] = "BUILT"
    finally:
        conn.close()
    receipt["target_bytes"] = target.stat().st_size
    receipt["source_bytes"] = source.stat().st_size
    receipt["finished_at_utc"] = _utc()
    return receipt


def _fold_check(src: sqlite3.Connection, dst: sqlite3.Connection, sample: int = 3000) -> dict:
    """Did the fold keep the depth and lose only the rungs past the head?"""

    out = {"sampled": 0, "shares_conserved": True, "max_depth_before": 0,
           "max_depth_after": 0, "worst_share_delta": 0.0, "rows_folded": 0}
    try:
        ids = [r[0] for r in src.execute(
            f'SELECT rowid FROM "{FOLD_TABLE}" ORDER BY rowid DESC LIMIT {int(sample)}'
        )]
    except sqlite3.Error:
        return out
    for rowid in ids:
        a = src.execute(f'SELECT "{FOLD_COLUMN}" FROM "{FOLD_TABLE}" WHERE rowid=?', (rowid,)).fetchone()
        b = dst.execute(f'SELECT "{FOLD_COLUMN}" FROM "{FOLD_TABLE}" WHERE rowid=?', (rowid,)).fetchone()
        if not a or not b:
            out["shares_conserved"] = False
            continue
        try:
            before, after = json.loads(a[0]), json.loads(b[0])
        except (TypeError, ValueError):
            continue
        out["sampled"] += 1
        folded_here = False
        for entry_a, entry_b in zip(before, after):
            for side in ("ask_levels", "bid_levels"):
                la = entry_a.get(side) or []
                lb = entry_b.get(side) or []
                if not la:
                    continue
                out["max_depth_before"] = max(out["max_depth_before"], len(la))
                out["max_depth_after"] = max(out["max_depth_after"], len(lb))
                if len(lb) != len(la):
                    folded_here = True
                sa = sum(float(x.get("shares") or 0.0) for x in la)
                sb = sum(float(x.get("shares") or 0.0) for x in lb)
                delta = abs(sa - sb)
                out["worst_share_delta"] = max(out["worst_share_delta"], delta)
                if delta > 1e-6:
                    out["shares_conserved"] = False
                # The touch is what every price reads: it must be untouched.
                if lb and (abs(float(la[0]["price"]) - float(lb[0]["price"])) > 1e-12
                           or abs(float(la[0]["shares"]) - float(lb[0]["shares"])) > 1e-9):
                    out["shares_conserved"] = False
        out["rows_folded"] += 1 if folded_here else 0
    return out


def verify(*, source: Path, target: Path) -> dict[str, object]:
    """Prove the copy before anything is swapped.

    Three checks, cheapest first: SQLite's own integrity check, every table's
    row count, and a row-for-row digest of the tables production cannot be
    wrong about.  The folded column is compared on its DEPTH, not its bytes:
    the fold is meant to change those bytes and to leave the shares alone.
    """

    receipt: dict[str, object] = {"schema": SCHEMA, "checked_at_utc": _utc()}
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=300.0)
    dst = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=300.0)
    try:
        receipt["integrity_check"] = dst.execute("PRAGMA integrity_check").fetchone()[0]
        receipt["foreign_key_check"] = len(dst.execute("PRAGMA foreign_key_check").fetchall())
        src_objs, dst_objs = _objects(src), _objects(dst)
        receipt["objects"] = {
            kind: [len(src_objs[kind]), len(dst_objs[kind])] for kind in src_objs
        }
        receipt["objects_match"] = all(
            sorted(n for n, _ in src_objs[k]) == sorted(n for n, _ in dst_objs[k])
            for k in src_objs
        )
        bad_counts = {}
        for name, _sql in src_objs["table"]:
            a = int(src.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            b = int(dst.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            if a != b:
                bad_counts[name] = [a, b]
        receipt["row_counts_match"] = not bad_counts
        receipt["bad_counts"] = bad_counts
        # A named table absent from the SOURCE is nothing to check; absent from
        # the target only, or different, is a failure.  ``digests_checked`` is
        # reported so a run that silently checked none is visible rather than
        # passing on an empty ``all([])``.
        present = {name for name, _sql in src_objs["table"]}
        digests = {}
        for name in DIGEST_TABLES:
            if name not in present:
                continue
            cols = _columns(src, "main", name)
            if not cols:
                continue
            digests[name] = _digest(src, "main", name, cols) == _digest(dst, "main", name, cols)
        receipt["digests"] = digests
        receipt["digests_checked"] = len(digests)
        receipt["digests_match"] = all(digests.values())

        # The invariant the fold exists to keep.  Bytes are MEANT to differ, so
        # comparing them would fail by design; what must not move is the depth
        # the sizing layer reads -- ``sum(level.shares)`` per bucket side.
        # Sampled: parsing 595k snapshots twice would cost more than the build.
        receipt["fold"] = _fold_check(src, dst)
        receipt["ok"] = bool(
            receipt["integrity_check"] == "ok"
            and receipt["foreign_key_check"] == 0
            and receipt["objects_match"]
            and receipt["row_counts_match"]
            and receipt["digests_match"]
            and receipt["fold"]["shares_conserved"]
            and receipt["fold"]["max_depth_after"] <= 6
        )
    finally:
        src.close(); dst.close()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SSOT)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="build with the residents up; the copy will be behind")
    args = parser.parse_args()
    out: dict[str, object] = {}
    if args.build:
        out["build"] = build(source=args.source, target=args.target, force=args.force)
        if out["build"].get("state") != "BUILT":
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 1
    if args.verify:
        out["verify"] = verify(source=args.source, target=args.target)
    if not out:
        out["residents_running"] = _residents_running()
        out["source_bytes"] = args.source.stat().st_size if args.source.exists() else None
        out["target_exists"] = args.target.exists()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out.get("verify", {}).get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
