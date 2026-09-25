#!/usr/bin/env python3
"""Charter 228 before landing: the old and the new width lane on the same P0 evidence.

Read-only.  Loads P0's width-lane cells once (the production loader, the
sweep's own --since bound), then runs ``fit_center_width`` and
``fit_station_width`` of both versions of ``center_width_store`` on them:

    old  = the file at the commit before charter 228 (``--old-rev``)
    new  = the file in the working tree

Nothing is journaled or activated.  Writes results.json beside this script.

    python3 compare.py --old-rev 0e413e20
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pickle
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
import bayes_basket_target  # noqa: E402,F401
from bayes_basket_target import center_width_store as current  # noqa: E402


def load_version(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"bayes_basket_target.{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-rev", required=True)
    parser.add_argument("--since", default="2026-08-27T09:20:00Z")    # the sweep's bound
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    floor = current._evidence_floor(conn, stage="P0", caller_bound=args.since)
    by_day = current.serving_cells_by_day(conn, current._truth(conn), stage="P0", min_decision_at_utc=floor)
    by_day = pickle.loads(pickle.dumps(by_day))
    old_source = subprocess.run(
        ["git", "show", f"{args.old_rev}:bayes_basket_target/center_width_store.py"],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout
    old_path = Path(tempfile.mkdtemp()) / "cw_old.py"
    old_path.write_text(old_source)
    out = {"as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "evidence_floor": floor,
           "days": len(by_day), "cells": sum(len(v) for v in by_day.values())}
    for name, path in (("cw_old", old_path), ("cw_new", Path(current.__file__))):
        module = load_version(name, path)
        module.serving_cells_by_day = lambda *a, **k: by_day
        module._evidence_floor = lambda conn, *, stage, caller_bound: floor
        module._truth = lambda conn: {}
        module.serving_arithmetic.watchdog = lambda conn, *, stage: {"stubbed": True}
        t0 = time.time()
        with module._round_memo():
            fitted = module.fit_center_width(conn, stage="P0", min_decision_at_utc=args.since)
        t1 = time.time()
        with module._round_memo():
            station = module.fit_station_width(conn, stage="P0", min_decision_at_utc=args.since)
        out[name] = {"fit": fitted, "station": station, "seconds": [round(t1 - t0, 1), round(time.time() - t1, 1)]}
        print(f"{name}: vetoed={fitted['vetoed']} admitted={fitted.get('admitted')} serves "
              f"delta={fitted['delta_c']} lambda={fitted['width_lambda']} ({out[name]['seconds']} s)", flush=True)
    conn.close()
    (HERE / "results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
