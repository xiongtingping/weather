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

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--cutoff", default=None, help="as-of instant (UTC ISO); default now")
    args = parser.parse_args(argv)
    if not args.force and store.artifact_is_current(store.load_active()):
        print("realisation layer: current, kept", flush=True)
        return 0
    started = time.perf_counter()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA query_only=ON")
    try:
        artifact = store.refit(conn, cutoff_utc=args.cutoff)
    finally:
        conn.close()
    artifact["build_sec"] = round(time.perf_counter() - started, 1)
    path = store.write_artifact(artifact)
    gate = artifact.get("gate") or {}
    print("realisation layer: " + json.dumps({
        "path": str(path), "state": gate.get("state"), "decisions": artifact["population"].get("decisions"),
        "scored_days": gate.get("scored_days"), "cells_outside": gate.get("cells_outside"),
        "cells_allowed": gate.get("cells_allowed"), "build_sec": artifact["build_sec"],
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
