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

from bayes_basket_target.market_ingress import (  # noqa: E402
    depth_head_levels, fold_depth_tail, tick_outward,
)

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
#: One invocation copies for this long, commits, and hands back ``PARTIAL``;
#: the next resumes from the target's own rowids.  Kept well under the ten
#: minutes a foreground call is allowed.
DEFAULT_BUDGET_SECONDS = 420.0
#: Chunks are resized to take about this long each.
CHUNK_TARGET_SECONDS = 10.0
FINGERPRINT_SCHEMA = "ssot_compact_source_fingerprint_v1"
#: What a staging file carries besides the rows (see ``stage``).
STAGE_SCHEMA_TABLE = "__rebuild_schema"
STAGE_SEQUENCE_TABLE = "__rebuild_sequence"
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
    staged = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (STAGE_SCHEMA_TABLE,),
    ).fetchone()
    query = (
        f'SELECT type,name,sql FROM main."{STAGE_SCHEMA_TABLE}" ORDER BY position' if staged else
        "SELECT type,name,sql FROM main.sqlite_master"
        " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
    )
    for kind, name, sql in conn.execute(query):
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
    keep = depth_head_levels()
    for entry in buckets:
        if not isinstance(entry, dict):
            continue
        try:
            tick = float(entry.get("price_tick_size") or 0.0)
        except (TypeError, ValueError):
            tick = 0.0
        for side, ascending in (("ask_levels", True), ("bid_levels", False)):
            levels = entry.get(side)
            if not isinstance(levels, list) or not levels:
                continue
            folded = fold_depth_tail(levels, ascending=ascending, tick=tick)
            if len(folded) != len(levels):
                entry[side] = folded
                changed = True
                continue
            # Rows the first cut of charter 168 folded at write: exactly
            # ``keep + 1`` levels, the last one off the tick grid, which the
            # kernel refuses the whole bucket for.  A raw ladder cannot be off
            # its own grid -- the tick is derived from that ladder -- so an
            # off-grid last rung here is the fold's and nothing else's.  It is
            # moved outward exactly as the corrected fold would have placed it.
            if tick > 0.0 and len(levels) == keep + 1 and keep > 0:
                last = levels[-1]
                try:
                    price = float(last.get("price"))
                    before = float(levels[-2].get("price"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if abs(price / tick - round(price / tick)) <= 1e-7:
                    continue
                placed = tick_outward(price, tick, ascending=ascending)
                if (placed > before if ascending else placed < before) and 0.0 < placed <= 1.0:
                    entry[side] = [*levels[:-1], {**last, "price": placed}]
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


def _open_source(path: Path) -> sqlite3.Connection:
    """The store being copied, opened so that it provably cannot change.

    ``immutable=1`` takes no lock and makes no ``-shm``: the copy cannot write
    to the file it reads, and a reader that died mid-run leaves nothing behind
    on the volume the backup lives on.
    """

    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=300.0)


def _sidecar(target: Path) -> Path:
    return target.with_name(target.name + ".building.json")


def fingerprint(source: Path) -> dict[str, object]:
    """What the source holds, taken once where reading it is cheap.

    The build reads its source off a USB spinning disk, where every extra pass
    over 55 GiB costs the better part of an hour with production stopped.  The
    counts and digests the build and the proof compare against are taken here,
    from the internal copy, before it is deleted -- and ``cmp`` has already
    shown the two files are the same bytes, so what is true of one is true of
    the other.
    """

    conn = _open_source(source)
    try:
        objects = _objects(conn)
        tables = [name for name, _sql in objects["table"]]
        counts = {n: int(conn.execute(f'SELECT COUNT(*) FROM main."{n}"').fetchone()[0]) for n in tables}
        max_rowid = {n: conn.execute(f'SELECT MAX(rowid) FROM main."{n}"').fetchone()[0] for n in tables}
        digests = {
            n: _digest(conn, "main", n, _columns(conn, "main", n))
            for n in DIGEST_TABLES if n in counts
        }
        has_sequence = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
        ).fetchone()
        sequence = dict(conn.execute("SELECT name, seq FROM sqlite_sequence")) if has_sequence else {}
    finally:
        conn.close()
    return {
        "schema": FINGERPRINT_SCHEMA, "taken_at_utc": _utc(),
        "source": str(source), "source_bytes": source.stat().st_size,
        "objects": {kind: sorted(n for n, _sql in listed) for kind, listed in objects.items()},
        "counts": counts, "max_rowid": max_rowid, "digests": digests, "sequence": sequence,
    }


def _copy_rows(
    conn: sqlite3.Connection, tables: list[str], state: dict, *,
    deadline: float, t0: float, transform: bool, on_table,
) -> str | None:
    """Copy every table from ``main`` to ``out`` in rowid order, resumably.

    Each table's progress is ``MAX(rowid)`` on the target, committed a chunk
    at a time.  Returns the table it stopped in when the budget ran out, or
    None when everything is across.  ``transform`` applies the fold and the
    dedupe; a staged source has had them applied already.
    """

    for name in tables:
        cols = _columns(conn, "main", name)
        listed = ",".join(f'"{c}"' for c in cols)
        marks = ",".join("?" for _ in cols)
        fold_at = cols.index(FOLD_COLUMN) if (
            transform and name == FOLD_TABLE and FOLD_COLUMN in cols) else None
        dedupe_at = cols.index(DEDUPE_COLUMN) if (
            transform and name == DEDUPE_TABLE and DEDUPE_COLUMN in cols) else None
        transformed = fold_at is not None or dedupe_at is not None
        src_max = conn.execute(f'SELECT MAX(rowid) FROM main."{name}"').fetchone()[0]
        if src_max is None:
            continue
        rows_per_chunk = 2000 if transformed else 20000
        copied_here = 0
        while True:
            last = conn.execute(f'SELECT MAX(rowid) FROM out."{name}"').fetchone()[0]
            if last is not None and last >= src_max:
                break
            if time.time() >= deadline:
                return name
            where = "" if last is None else "WHERE rowid>? "
            args: tuple = () if last is None else (last,)
            began = time.time()
            if not transformed:
                cur = conn.execute(
                    f'INSERT INTO out."{name}"(rowid,{listed})'
                    f' SELECT rowid,{listed} FROM main."{name}" {where}ORDER BY rowid LIMIT ?',
                    args + (rows_per_chunk,),
                )
                moved = cur.rowcount
            else:
                rows = conn.execute(
                    f'SELECT rowid,{listed} FROM main."{name}" {where}ORDER BY rowid LIMIT ?',
                    args + (rows_per_chunk,),
                ).fetchall()
                rebuilt = []
                for row in rows:
                    values = list(row)
                    at = (fold_at if fold_at is not None else dedupe_at) + 1   # +1: rowid leads
                    changed = (_fold_payload if fold_at is not None else _dedupe_payload)(values[at])
                    if changed is not values[at]:
                        state["folded_snapshots" if fold_at is not None else "deduped_preselections"] += 1
                    values[at] = changed
                    rebuilt.append(values)
                conn.executemany(
                    f'INSERT INTO out."{name}"(rowid,{listed}) VALUES(?,{marks})', rebuilt,
                )
                moved = len(rebuilt)
            conn.commit()
            copied_here += moved
            took = max(time.time() - began, 1e-3)
            ceiling = 5000 if transformed else 500000
            rows_per_chunk = int(min(ceiling, max(500, rows_per_chunk * CHUNK_TARGET_SECONDS / took)))
        if copied_here:
            on_table()
            print(f"  {name:56} +{copied_here:>10,}  {time.time()-t0:>6.0f}s", flush=True)
    return None


def _is_staged(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (STAGE_SCHEMA_TABLE,),
    ).fetchone() is not None


def _source_sequence(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    if _is_staged(conn):
        return [tuple(r) for r in conn.execute(f'SELECT name, seq FROM main."{STAGE_SEQUENCE_TABLE}"')]
    if conn.execute("SELECT 1 FROM main.sqlite_master WHERE name='sqlite_sequence'").fetchone():
        return [tuple(r) for r in conn.execute("SELECT name, seq FROM main.sqlite_sequence")]
    return []


def stage(
    *, source: Path, staging: Path, force: bool = False,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
) -> dict[str, object]:
    """Pass one of two: the source's rows, transformed, onto the slow volume.

    Why two passes.  The internal disk holds the source or the rebuilt store,
    never both, and the other volume is a USB spinning disk that manages ~83
    random I/Os a second.  Read the source off it and the tables that grew
    interleaved with everything else (42.7k rows a minute for the inventory
    snapshots) cost hours; write the target to it and every TEXT PRIMARY KEY
    is an index updated at a random page per row.  So the rows go across
    twice, sequentially both times: here from the internal source into
    staging tables that have columns and nothing else -- no types, no keys,
    no indexes -- so every insert is an append; then ``build`` reads each
    staged table front to back (it was written in one run, so it lies in one
    run) and assembles the real schema on the internal disk.

    The staging file carries the source's schema (``__rebuild_schema``) and
    AUTOINCREMENT counters (``__rebuild_sequence``) so ``build`` can take it
    as its source unchanged.
    """

    receipt: dict[str, object] = {
        "schema": SCHEMA, "started_at_utc": _utc(), "pass": "stage",
        "source": str(source), "staging": str(staging), "state": "STARTED",
    }
    running = _residents_running()
    if running and not force:
        receipt["state"] = "REFUSED_RESIDENT_RUNNING"
        receipt["running"] = running
        return receipt
    sidecar = _sidecar(staging)
    if staging.exists() and not sidecar.exists():
        receipt["state"] = "REFUSED_TARGET_EXISTS"
        return receipt
    t0 = time.time()
    deadline = t0 + float(budget_seconds)
    source_bytes = source.stat().st_size
    src = _open_source(source)
    try:
        if _is_staged(src):
            receipt["state"] = "REFUSED_SOURCE_IS_STAGING"
            return receipt
        page_size = int(src.execute("PRAGMA page_size").fetchone()[0])
        ordered = [
            (str(k), str(n), str(q)) for k, n, q in src.execute(
                "SELECT type,name,sql FROM main.sqlite_master"
                " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            )
        ]
        tables = [n for k, n, _q in ordered if k == "table"]
        columns = {n: _columns(src, "main", n) for n in tables}
        sequence = _source_sequence(src)
    finally:
        src.close()

    if sidecar.exists():
        state = json.loads(sidecar.read_text())
        if state.get("source") != str(source) or state.get("source_bytes") != source_bytes:
            receipt["state"] = "REFUSED_SOURCE_CHANGED"
            return receipt
    else:
        staging.parent.mkdir(parents=True, exist_ok=True)
        st = sqlite3.connect(str(staging), timeout=300.0)
        try:
            st.execute(f"PRAGMA page_size={page_size}")
            st.execute(f'CREATE TABLE "{STAGE_SCHEMA_TABLE}"'
                       "(position INTEGER PRIMARY KEY, type TEXT, name TEXT, sql TEXT)")
            st.executemany(f'INSERT INTO "{STAGE_SCHEMA_TABLE}"(type,name,sql) VALUES(?,?,?)', ordered)
            st.execute(f'CREATE TABLE "{STAGE_SEQUENCE_TABLE}"(name TEXT, seq INTEGER)')
            st.executemany(f'INSERT INTO "{STAGE_SEQUENCE_TABLE}"(name,seq) VALUES(?,?)', sequence)
            # Columns and nothing else.  No declared type means no affinity,
            # so each value is stored exactly as the source stored it; the
            # real affinity is applied again, idempotently, on assembly.
            for name in tables:
                st.execute(f'CREATE TABLE "{name}"(' + ",".join(f'"{c}"' for c in columns[name]) + ")")
            st.commit()
        finally:
            st.close()
        state = {
            "source": str(source), "source_bytes": source_bytes,
            "started_at_utc": receipt["started_at_utc"], "runs": 0,
            "folded_snapshots": 0, "deduped_preselections": 0,
        }
    state["runs"] = int(state.get("runs", 0)) + 1

    def _save() -> None:
        sidecar.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    _save()
    conn = _open_source(source)
    try:
        conn.execute("PRAGMA main.cache_size=-262144")         # 256 MiB of the fast side
        conn.execute("ATTACH DATABASE ? AS out", (str(staging),))
        conn.execute("PRAGMA out.journal_mode=TRUNCATE")
        conn.execute("PRAGMA out.synchronous=OFF")
        conn.execute("PRAGMA out.cache_size=-65536")           # appends need little
        stopped = _copy_rows(conn, tables, state, deadline=deadline, t0=t0,
                             transform=True, on_table=_save)
    finally:
        conn.close()
    receipt["runs"] = state["runs"]
    receipt["run_seconds"] = round(time.time() - t0, 1)
    receipt["staging_bytes"] = staging.stat().st_size
    receipt["folded_snapshots"] = state["folded_snapshots"]
    receipt["deduped_preselections"] = state["deduped_preselections"]
    if stopped is not None:
        _save()
        receipt["state"] = "PARTIAL"
        receipt["stopped_at"] = stopped
        return receipt
    state["staged_at_utc"] = _utc()
    (staging.with_name(staging.name + ".staged.json")).write_text(
        json.dumps(state, ensure_ascii=False, indent=2))
    sidecar.unlink()
    receipt["state"] = "STAGED"
    return receipt


def build(
    *, source: Path, target: Path, force: bool = False,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    expected: dict[str, object] | None = None,
) -> dict[str, object]:
    """Write the compact copy, resumably.  Nothing is swapped; the source is untouched.

    Progress lives in the target itself: every table is copied in rowid order
    and committed a chunk at a time, so each table's ``MAX(rowid)`` is exactly
    how far it got.  A run that runs out of budget, or is killed, hands back
    ``PARTIAL`` and the next run carries on from there, losing at most the
    chunk in flight -- which the rollback journal undoes on the next open.
    """

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
    sidecar = _sidecar(target)
    if target.exists() and not sidecar.exists():
        # Finished, or not ours: either way there is nothing here to resume.
        receipt["state"] = "REFUSED_TARGET_EXISTS"
        return receipt

    t0 = time.time()
    deadline = t0 + float(budget_seconds)
    src = _open_source(source)
    try:
        objects = _objects(src)
        page_size = int(src.execute("PRAGMA page_size").fetchone()[0])
        staged = _is_staged(src)
        sequence = _source_sequence(src)
    finally:
        src.close()
    tables = [name for name, _sql in objects["table"]]
    source_bytes = source.stat().st_size

    if sidecar.exists():
        state = json.loads(sidecar.read_text())
        if state.get("source") != str(source) or state.get("source_bytes") != source_bytes:
            receipt["state"] = "REFUSED_SOURCE_CHANGED"
            receipt["sidecar"] = state
            return receipt
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        out = sqlite3.connect(str(target), timeout=300.0)
        try:
            out.execute(f"PRAGMA page_size={page_size}")
            # Tables first, WITHOUT their indexes or triggers.  An index built
            # row by row is both slower and looser than one built once at the
            # end, and a trigger that fires during the copy would rewrite the
            # history it is meant to protect.
            for _name, sql in objects["table"]:
                out.execute(sql)
            out.commit()
        finally:
            out.close()
        state = {
            "source": str(source), "source_bytes": source_bytes,
            "started_at_utc": receipt["started_at_utc"], "runs": 0,
            "folded_snapshots": 0, "deduped_preselections": 0,
        }
    state["runs"] = int(state.get("runs", 0)) + 1

    def _save() -> None:
        sidecar.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def _partial(where: str) -> dict[str, object]:
        _save()
        receipt["state"] = "PARTIAL"
        receipt["stopped_at"] = where
        receipt["run_seconds"] = round(time.time() - t0, 1)
        receipt["target_bytes"] = target.stat().st_size
        receipt["runs"] = state["runs"]
        return receipt

    _save()
    # One connection: the source as ``main`` and the target attached.  The
    # target's page cache is the one that matters -- a TEXT PRIMARY KEY is an
    # implicit index maintained on every insert -- and it has to be set on the
    # ATTACHED schema: an unqualified ``cache_size`` only sizes ``main``, which
    # is how the second attempt thrashed a 2 MiB cache while believing it had
    # 2 GiB.  Sized for an 8 GiB machine: the last attempt was OOM-killed.
    conn = _open_source(source)
    try:
        conn.execute("PRAGMA main.cache_size=-65536")          # 64 MiB: read once, in order
        conn.execute("ATTACH DATABASE ? AS out", (str(target),))
        receipt["source_staged"] = staged
        conn.execute("PRAGMA out.journal_mode=TRUNCATE")       # a kill mid-chunk rolls back
        conn.execute("PRAGMA out.synchronous=OFF")             # a failed build is deleted
        conn.execute("PRAGMA out.cache_size=-1048576")         # 1 GiB
        stopped = _copy_rows(conn, tables, state, deadline=deadline, t0=t0,
                             transform=not staged, on_table=_save)
        if stopped is not None:
            return _partial(stopped)

        # Every table is across.  Counts, against the fingerprint when there
        # is one (it was taken where counting is cheap), else the source.
        if not state.get("counts_ok"):
            want = dict((expected or {}).get("counts") or {})
            bad = {}
            for name in tables:
                have = int(conn.execute(f'SELECT COUNT(*) FROM out."{name}"').fetchone()[0])
                need = int(want[name]) if name in want else int(
                    conn.execute(f'SELECT COUNT(*) FROM main."{name}"').fetchone()[0]
                )
                if have != need:
                    bad[name] = [need, have]
            if bad:
                _save()
                receipt["state"] = "FAILED_ROW_COUNT"
                receipt["mismatched"] = bad
                return receipt
            state["counts_ok"] = True
            # AUTOINCREMENT remembers the largest id ever handed out, not the
            # largest still present; copying rows only teaches the target the
            # latter, and the next insert would reissue a deleted row's id.
            for seq_name, seq in sequence:
                cur = conn.execute(
                    "UPDATE out.sqlite_sequence SET seq=MAX(seq, ?) WHERE name=?", (seq, seq_name),
                )
                if cur.rowcount == 0:
                    conn.execute("INSERT INTO out.sqlite_sequence(name, seq) VALUES(?, ?)", (seq_name, seq))
            conn.commit()
            _save()
    finally:
        conn.close()

    # Indexes, views, triggers -- on the target alone, each committed on its
    # own so a stop between two of them resumes at the next.
    out = sqlite3.connect(str(target), timeout=300.0)
    try:
        out.execute("PRAGMA journal_mode=TRUNCATE")
        out.execute("PRAGMA synchronous=OFF")
        out.execute("PRAGMA cache_size=-1048576")
        present = {str(r[0]) for r in out.execute("SELECT name FROM sqlite_master")}
        # Triggers last of all: the append-only guards must not see the copy.
        for kind in ("index", "view", "trigger"):
            for name, sql in objects[kind]:
                if name in present:
                    continue
                if time.time() >= deadline:
                    out.close()
                    return _partial(f"{kind}:{name}")
                began = time.time()
                out.execute(sql)
                out.commit()
                if kind == "index" and time.time() - began > 5:
                    print(f"  index {name:50} {time.time()-began:>6.0f}s", flush=True)
        out.execute("PRAGMA journal_mode=WAL")
    finally:
        out.close()
    receipt["folded_snapshots"] = state["folded_snapshots"]
    receipt["deduped_preselections"] = state["deduped_preselections"]
    receipt["runs"] = state["runs"]
    receipt["run_seconds"] = round(time.time() - t0, 1)
    receipt["state"] = "BUILT"
    sidecar.unlink()
    receipt["target_bytes"] = target.stat().st_size
    receipt["source_bytes"] = source_bytes
    receipt["finished_at_utc"] = _utc()
    return receipt


def _fold_check(src: sqlite3.Connection, dst: sqlite3.Connection, sample: int = 3000) -> dict:
    """Did the fold keep the depth and lose only the rungs past the head?"""

    from bayes_basket_target.basket_kernel.contracts import BucketObservation, ContractError

    def _kernel(entry: object) -> str | None:
        # A stored snapshot carries the book, not a belief: ``probability`` is
        # attached downstream, and without a stand-in every bucket stops at
        # ``bucket_probability_must_be_finite_number`` before the ladder is
        # read -- identically before and after, so the check would pass on
        # nothing.  The stand-in lets the parse reach the rungs.
        if isinstance(entry, dict) and entry.get("probability") is None:
            entry = {**entry, "probability": 0.0}
        try:
            BucketObservation.from_mapping(entry)  # type: ignore[arg-type]
        except ContractError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}:{exc}"
        return None

    out = {"sampled": 0, "shares_conserved": True, "max_depth_before": 0,
           "max_depth_after": 0, "worst_share_delta": 0.0, "rows_folded": 0,
           "buckets_parsed": 0, "kernel_new_rejections": 0, "kernel_repaired": 0,
           "kernel_rejection_examples": []}
    try:
        # The newest rows are the ones production folded at write (and the
        # first cut left off the grid); a stride across the whole table
        # covers the history the rebuild folds for the first time.
        ids = [r[0] for r in src.execute(
            f'SELECT rowid FROM "{FOLD_TABLE}" ORDER BY rowid DESC LIMIT {int(sample)}'
        )]
        lo, hi = src.execute(f'SELECT MIN(rowid), MAX(rowid) FROM "{FOLD_TABLE}"').fetchone()
        if lo is not None:
            step = max(1, (hi - lo) // 1000)
            for point in range(lo, hi, step):
                hit = src.execute(
                    f'SELECT rowid FROM "{FOLD_TABLE}" WHERE rowid>=? ORDER BY rowid LIMIT 1', (point,)
                ).fetchone()
                if hit:
                    ids.append(hit[0])
        ids = sorted(set(ids))
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
            # The kernel is the reader that matters: a rebuilt bucket must
            # parse wherever the stored one did, and a stored one refused
            # only for an off-grid rung must now parse.
            err_a, err_b = _kernel(entry_a), _kernel(entry_b)
            out["buckets_parsed"] += 1
            if err_b is not None and (err_b != err_a or err_b == "bucket_price_tick_mismatch"):
                out["kernel_new_rejections"] += 1
                if len(out["kernel_rejection_examples"]) < 5:
                    out["kernel_rejection_examples"].append([rowid, err_a, err_b])
            elif err_a == "bucket_price_tick_mismatch" and err_b is None:
                out["kernel_repaired"] += 1
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


def verify(
    *, source: Path, target: Path, expected: dict[str, object] | None = None,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
) -> dict[str, object]:
    """Prove the copy before anything is swapped.

    SQLite's own integrity check, table by table (each verdict is kept, so a
    run that runs out of budget resumes at the next table); the schema
    objects; every table's row count; a row-for-row digest of the tables
    production cannot be wrong about.  Counts and digests are compared with
    the source ``fingerprint`` when one is given -- taken where reading is
    cheap -- else read from the source.  The folded column is compared on its
    DEPTH, not its bytes: the fold is meant to change those bytes and to leave
    the shares alone.
    """

    receipt: dict[str, object] = {"schema": SCHEMA, "checked_at_utc": _utc()}
    t0 = time.time()
    deadline = t0 + float(budget_seconds)
    progress_path = target.with_name(target.name + ".verify.json")
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"integrity": {}}
    src = _open_source(source)
    dst = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=300.0)
    try:
        dst.execute("PRAGMA cache_size=-524288")
        src_objs = _objects(src)
        tables = [name for name, _sql in src_objs["table"]]
        integrity = progress["integrity"]
        for name in tables:
            if name in integrity:
                continue
            if time.time() >= deadline:
                progress_path.write_text(json.dumps(progress, indent=2))
                receipt["state"] = "PARTIAL"
                receipt["integrity_checked"] = f"{len(integrity)}/{len(tables)}"
                return receipt
            integrity[name] = [str(r[0]) for r in dst.execute(f'PRAGMA integrity_check("{name}")')]
            progress_path.write_text(json.dumps(progress, indent=2))
        bad_integrity = {n: v for n, v in integrity.items() if v != ["ok"]}
        receipt["integrity_check"] = "ok" if not bad_integrity else bad_integrity
        receipt["integrity_tables"] = len(integrity)
        receipt["foreign_key_check"] = len(dst.execute("PRAGMA foreign_key_check").fetchall())

        want = expected or {}
        dst_objs = _objects(dst)
        src_names = want.get("objects") or {k: sorted(n for n, _ in v) for k, v in src_objs.items()}
        receipt["objects"] = {kind: [len(src_names[kind]), len(dst_objs[kind])] for kind in src_names}
        receipt["objects_match"] = all(
            sorted(src_names[k]) == sorted(n for n, _ in dst_objs[k]) for k in src_names
        )
        want_counts = dict(want.get("counts") or {})
        bad_counts = {}
        for name in tables:
            a = int(want_counts[name]) if name in want_counts else int(
                src.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            )
            b = int(dst.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            if a != b:
                bad_counts[name] = [a, b]
        receipt["row_counts_match"] = not bad_counts
        receipt["tables_counted"] = len(tables)
        receipt["bad_counts"] = bad_counts
        want_seq = dict(want.get("sequence") or {})
        if not want_seq and src.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            want_seq = dict(src.execute("SELECT name, seq FROM sqlite_sequence"))
        have_seq = dict(dst.execute("SELECT name, seq FROM sqlite_sequence")) if want_seq else {}
        receipt["sequence_match"] = all(have_seq.get(n) == v for n, v in want_seq.items())
        # A named table absent from the SOURCE is nothing to check; absent from
        # the target only, or different, is a failure.  ``digests_checked`` is
        # reported so a run that silently checked none is visible rather than
        # passing on an empty ``all([])``.
        want_digests = dict(want.get("digests") or {})
        present = set(tables)
        digests = {}
        for name in DIGEST_TABLES:
            if name not in present:
                continue
            cols = _columns(src, "main", name)
            if not cols:
                continue
            source_digest = want_digests.get(name) or _digest(src, "main", name, cols)
            digests[name] = source_digest == _digest(dst, "main", name, cols)
        receipt["digests"] = digests
        receipt["digests_checked"] = len(digests)
        receipt["digests_match"] = all(digests.values())

        # The invariant the fold exists to keep.  Bytes are MEANT to differ, so
        # comparing them would fail by design; what must not move is the depth
        # the sizing layer reads -- ``sum(level.shares)`` per bucket side.
        # Sampled: parsing 595k snapshots twice would cost more than the build.
        receipt["fold"] = _fold_check(src, dst)
        receipt["ok"] = bool(
            not bad_integrity
            and len(integrity) == len(tables)
            and receipt["foreign_key_check"] == 0
            and receipt["objects_match"]
            and receipt["row_counts_match"]
            and receipt["sequence_match"]
            and receipt["digests_match"]
            and receipt["digests_checked"] > 0
            and receipt["fold"]["shares_conserved"]
            and receipt["fold"]["sampled"] > 0
            and receipt["fold"]["kernel_new_rejections"] == 0
            and receipt["fold"]["buckets_parsed"] > 0
            and receipt["fold"]["max_depth_after"] <= 6
        )
        receipt["state"] = "VERIFIED" if receipt["ok"] else "FAILED"
        receipt["verify_seconds"] = round(time.time() - t0, 1)
    finally:
        src.close(); dst.close()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SSOT)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--fingerprint-out", type=Path,
                        help="write the source's counts/digests here and stop")
    parser.add_argument("--expect", type=Path,
                        help="a fingerprint to compare against instead of re-reading the source")
    parser.add_argument("--stage", action="store_true",
                        help="pass one: source rows, transformed, into a key-less staging file at --target")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--force", action="store_true",
                        help="build with the residents up; the copy will be behind")
    args = parser.parse_args()
    out: dict[str, object] = {}
    if args.fingerprint_out:
        t0 = time.time()
        doc = fingerprint(args.source)
        args.fingerprint_out.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
        print(json.dumps({"fingerprint": str(args.fingerprint_out), "tables": len(doc["counts"]),
                          "rows": sum(doc["counts"].values()), "digests": len(doc["digests"]),
                          "seconds": round(time.time() - t0, 1)}, ensure_ascii=False))
        return 0
    if args.target is None:
        parser.error("--target is required")
    expected = json.loads(args.expect.read_text()) if args.expect else None
    if expected is not None and expected.get("source_bytes") != args.source.stat().st_size:
        parser.error("the fingerprint was taken from a file of a different size")
    if args.stage:
        out["stage"] = stage(source=args.source, staging=args.target, force=args.force,
                             budget_seconds=args.budget_seconds)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0 if out["stage"].get("state") in {"STAGED", "PARTIAL"} else 1
    if args.build:
        out["build"] = build(source=args.source, target=args.target, force=args.force,
                             budget_seconds=args.budget_seconds, expected=expected)
        if out["build"].get("state") != "BUILT":
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
            return 0 if out["build"].get("state") == "PARTIAL" else 1
    if args.verify:
        out["verify"] = verify(source=args.source, target=args.target, expected=expected,
                               budget_seconds=args.budget_seconds)
    if not out:
        out["residents_running"] = _residents_running()
        out["source_bytes"] = args.source.stat().st_size if args.source.exists() else None
        out["target_exists"] = args.target.exists()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out.get("verify", {}).get("ok", True) or out.get("verify", {}).get("state") == "PARTIAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
