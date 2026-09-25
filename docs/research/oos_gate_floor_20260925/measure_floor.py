#!/usr/bin/env python3
"""Run the production OOS build read-only with the gate's ladder scoring floored or not.

    python3 measure_floor.py --as-of 2026-09-25T09:00:00Z --stations KLGA,EGLC

One gzip JSON line per station per mode into runs/<as_of>_<mode>.jsonl.gz
(a station already present is skipped).  "on" replaces _ladder_metrics in
this process only (PROTOCOL.md).
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import native_ensemble_oos as O  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

_spec = importlib.util.spec_from_file_location("m213", ROOT / "docs" / "research" / "oos_ladder_gate_20260924" / "measure.py")
M213 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M213)
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
RUNS = HERE / "runs"
ORIGINAL = O._ladder_metrics


def floored_ladder_metrics(probability, actual, ladder):
    ordered = list(ladder["ordered"])
    vector, _ = floor_probabilities({key: float(probability.get(key, 0.0)) for key in ordered})
    return ORIGINAL(vector, actual, ladder)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--stations", required=True)
    args = parser.parse_args()
    RUNS.mkdir(exist_ok=True)
    modes = ("off", "on")
    outs = {m: RUNS / f"{args.as_of.replace(':', '')}_{m}.jsonl.gz" for m in modes}
    done = {m: set() for m in modes}
    for m, out in outs.items():
        if out.exists():
            with gzip.open(out, "rt") as handle:
                done[m] = {json.loads(line)["station"] for line in handle}
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    for station in [s.strip().upper() for s in args.stations.split(",") if s.strip()]:
        for mode in modes:
            if station in done[mode]:
                continue
            O._ladder_metrics = floored_ladder_metrics if mode == "on" else ORIGINAL
            started = time.time()
            try:
                report = O.build_native_ensemble_oos(
                    conn, station_id=station, as_of_utc=args.as_of,
                    min_train_days=O.FORWARD_ACCUMULATION_MIN_TRAIN_DAYS,
                    min_holdout_days=O.FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS,
                )
                record = {"station": station, "as_of": args.as_of, "mode": mode, "state": "ok",
                          "candidates": [M213._slim(c) for c in report.get("cross_candidates") or []]}
            except Exception as exc:  # noqa: BLE001 -- recorded, never hidden
                record = {"station": station, "as_of": args.as_of, "mode": mode, "state": "error",
                          "error": f"{type(exc).__name__}: {exc}"[:400], "candidates": []}
            finally:
                O._ladder_metrics = ORIGINAL
            record["seconds"] = round(time.time() - started, 1)
            with gzip.open(outs[mode], "at") as handle:
                handle.write(json.dumps(record) + "\n")
            print(f"{station} {mode} {record['state']} {len(record['candidates'])} candidates {record['seconds']}s", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
