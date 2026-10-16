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
        if not store.artifact_is_current(store.load_active(stage=stage))
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
            continue
        finally:
            conn.close()
        if yielded["yielded"]:
            print(f"realisation layer {stage}: yielded to the writer (WAL past the line); previous kept", flush=True)
            continue
        artifact["build_sec"] = round(time.perf_counter() - started, 1)
        path = store.write_artifact(artifact)
        gate = artifact.get("gate") or {}
        print(f"realisation layer {stage}: " + json.dumps({
            "path": str(path), "state": gate.get("state"), "decisions": artifact["population"].get("decisions"),
            "scored_days": gate.get("scored_days"), "cells_outside": gate.get("cells_outside"),
            "cells_allowed": gate.get("cells_allowed"), "build_sec": artifact["build_sec"],
        }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
