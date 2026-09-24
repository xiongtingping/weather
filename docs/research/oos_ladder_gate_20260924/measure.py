#!/usr/bin/env python3
"""Run the production OOS build read-only under one holdout grid.

    python3 measure.py --as-of 2026-09-24T02:30:00Z --grid market_ladder \
        --stations KLGA,EGLC [--tables-as-of ...]

Writes one gzip JSON line per station into runs/<as_of>_<grid>.jsonl.gz
(appends; a station already present is skipped).  Nothing is written to
the SSOT: the connection is opened read-only and the build performs no
writes.  See PROTOCOL.md.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as O  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
RUNS = HERE / "runs"


def _key(candidate: dict) -> list:
    return [
        candidate["station_id"], candidate["season_bin"],
        candidate["lead_bucket"], candidate["run_cycle"],
        candidate["available_system_set_hash"],
        candidate["run_cycle_by_system_sha256"],
        candidate["settlement_definition"],
        sorted(candidate["selected_system_ids"]),
    ]


def _slim(candidate: dict) -> dict:
    gate = candidate["oos_gate"]
    diag = candidate.get("equal_weight_diagnostic") or {}
    daily = [
        {
            "event_date": row["event_date"],
            "grid": row.get("holdout_grid", O.HOLDOUT_GRID_INTEGER_C),
            "learned": row["learned"],
            "climate": row["climate"],
        }
        for row in diag.get("scheme_daily_scores") or []
    ]
    return {
        "key": _key(candidate),
        "pass": bool(gate["pass"]),
        "path": gate["path"],
        "own_pass": bool((gate.get("own_holdout_gate") or gate).get("pass")),
        "window_day_count": gate.get("window_day_count"),
        "breakage_hit": bool((gate.get("breakage_floor") or {}).get("hit")),
        "oos_metrics": {k: candidate["oos_metrics"][k] for k in ("brier", "nll", "crps")},
        "climate_metrics": {
            k: candidate["climatology_baseline_metrics"][k] for k in ("brier", "nll", "crps")
        },
        "holdout_end_date": candidate["holdout_end_date"],
        "daily": daily,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--grid", required=True, choices=(*O.HOLDOUT_GRIDS, "both"))
    parser.add_argument("--stations", required=True)
    parser.add_argument("--tables-as-of", default=None)
    args = parser.parse_args()
    RUNS.mkdir(exist_ok=True)
    grids = O.HOLDOUT_GRIDS if args.grid == "both" else (args.grid,)
    outs = {g: RUNS / f"{args.as_of.replace(':', '')}_{g}.jsonl.gz" for g in grids}
    done = {g: set() for g in grids}
    for g, out in outs.items():
        if out.exists():
            with gzip.open(out, "rt") as handle:
                done[g] = {json.loads(line)["station"] for line in handle}
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    for station in [s.strip().upper() for s in args.stations.split(",") if s.strip()]:
        # Both grids back to back per station, so a store another session is
        # writing into moves as little as possible between the paired runs.
        for grid in grids:
            if station in done[grid]:
                continue
            started = time.time()
            try:
                report = O.build_native_ensemble_oos(
                    conn,
                    station_id=station,
                    as_of_utc=args.as_of,
                    min_train_days=O.FORWARD_ACCUMULATION_MIN_TRAIN_DAYS,
                    min_holdout_days=O.FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS,
                    tables_as_of_utc=args.tables_as_of,
                    holdout_grid=grid,
                )
                record = {
                    "station": station, "as_of": args.as_of, "grid": grid,
                    "state": "ok",
                    "candidates": [_slim(c) for c in report.get("cross_candidates") or []],
                }
            except Exception as exc:  # noqa: BLE001 -- recorded, never hidden
                record = {
                    "station": station, "as_of": args.as_of, "grid": grid,
                    "state": "error", "error": f"{type(exc).__name__}: {exc}"[:400],
                    "candidates": [],
                }
            record["seconds"] = round(time.time() - started, 1)
            with gzip.open(outs[grid], "at") as handle:
                handle.write(json.dumps(record) + "\n")
            print(f"{station} {grid} {record['state']} {len(record['candidates'])} "
                  f"candidates {record['seconds']}s", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
