#!/usr/bin/env python3
"""Charter 254: refit the realisation layer and write its artifact.

Reads the SSOT ``mode=ro`` and writes ``data/realisation_layer/active.json``
(and a dated copy).  Run hourly by the board readings job
(``scripts/board_readings_sweep.sh``); it fits only when the artifact is older
than ``realisation_store.REFIT_MAX_AGE_HOURS`` or was fitted by other code.

    python3 scripts/refit_realisation_layer.py           # fit if due
    python3 scripts/refit_realisation_layer.py --force   # fit now
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import realisation_store as store  # noqa: E402
from bayes_basket_target.resource_bounds import install_wal_yield  # noqa: E402

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
#: A stage killed by its timeout is not offered again for this long
#: (2026-10-17: P0 and P1 each ran their full 1,800 s and were killed, were
#: due again the next hour, and held the shared SSOT reader turn ~1.5 h an
#: hour while the research render queued behind them).  A stage marks its
#: start here and clears the mark when it ends; a mark older than
#: ``KILLED_AFTER_SEC`` means the run was killed.
ATTEMPT_DIR = ROOT / "data" / "runtime" / "realisation_refit_attempts"
KILLED_AFTER_SEC = 25 * 60
KILLED_BACKOFF_SEC = 6 * 3600


def _killed_recently(stage: str, now: float | None = None) -> bool:
    mark = ATTEMPT_DIR / f"{stage}.started"
    try:
        started = mark.stat().st_mtime
    except OSError:
        return False
    age = (time.time() if now is None else now) - started
    return KILLED_AFTER_SEC <= age < KILLED_AFTER_SEC + KILLED_BACKOFF_SEC


def due_stages() -> list[str]:
    """The stages whose artifact is not current, the stalest first (a stage
    with none at all before any that has one).

    2026-10-16: the three stages ran in one process under one timeout, in
    registry order, and from 10-14 P0 alone outlasted it whenever the machine
    was busy (207 s idle, 1,589 s under swap): P0-M's artifact stopped at
    10-12, P1's at 10-10.  The board readings job now runs each due stage as
    its own call with its own timeout, in this order, so a slow stage costs
    only itself and the oldest artifact is renewed first.
    """

    def fitted_at(stage: str) -> str:
        artifact = store.load_active(stage=stage)
        return str((artifact or {}).get("fitted_at_utc") or "")

    due = [
        stage for stage in store.STAGES
        if not store.artifact_is_current(store.load_active(stage=stage)) and not _killed_recently(stage)
    ]
    return sorted(due, key=lambda stage: (fitted_at(stage), store.STAGES.index(stage)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--cutoff", default=None, help="as-of instant (UTC ISO); default now")
    parser.add_argument("--stage", action="append", choices=store.STAGES,
                        help="refit only these belief stages (default: every stage)")
    parser.add_argument("--list-due", action="store_true",
                        help="print the stages due a refit, stalest first, and stop")
    args = parser.parse_args(argv)
    if args.list_due:
        for stage in due_stages():
            print(stage)
        return 0
    # Charter 231 补记二: one fit per belief stage, each refitted when its own
    # artifact is due.
    for stage in (args.stage or store.STAGES):
        if not args.force and store.artifact_is_current(store.load_active(stage=stage)):
            print(f"realisation layer {stage}: current, kept", flush=True)
            continue
        started = time.perf_counter()
        ATTEMPT_DIR.mkdir(parents=True, exist_ok=True)
        mark = ATTEMPT_DIR / f"{stage}.started"
        mark.touch()
        try:
            _refit_stage(args, stage, started)
        finally:
            mark.unlink(missing_ok=True)
    return 0


def _refit_stage(args: argparse.Namespace, stage: str, started: float) -> None:
    """One stage's fit, written unless it yielded to the writer."""
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA query_only=ON")
    # A long reader beside production (2026-10-16): it yields once the
    # WAL is past the reader line, and the stage keeps its artifact.
    yielded = install_wal_yield(conn, args.db)
    try:
        artifact = store.refit(conn, cutoff_utc=args.cutoff, stage=stage)
    except sqlite3.OperationalError:
        if not yielded["yielded"]:
            raise
        print(f"realisation layer {stage}: yielded to the writer (WAL past the line); previous kept", flush=True)
        return
    finally:
        conn.close()
    if yielded["yielded"]:
        print(f"realisation layer {stage}: yielded to the writer (WAL past the line); previous kept", flush=True)
        return
    artifact["build_sec"] = round(time.perf_counter() - started, 1)
    path = store.write_artifact(artifact)
    gate = artifact.get("gate") or {}
    print(f"realisation layer {stage}: " + json.dumps({
        "path": str(path), "state": gate.get("state"), "decisions": artifact["population"].get("decisions"),
        "scored_days": gate.get("scored_days"), "cells_outside": gate.get("cells_outside"),
        "cells_allowed": gate.get("cells_allowed"), "build_sec": artifact["build_sec"],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
