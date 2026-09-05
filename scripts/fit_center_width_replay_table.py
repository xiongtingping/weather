#!/usr/bin/env python3
"""One centre/width scalar per event date, fitted only on earlier days.

The serving artifact refuses every date at or before its own training cutoff,
which is right for production and useless for a replay: every historical day
sits inside it.  This gives each day the scalars it could have had that
morning and nothing later, so a replay measures a rule that could actually
have been run.  The search, the grid and the cell builder are the store's own.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from bayes_basket_target.center_width_store import (  # noqa: E402
    FORWARD_WINDOW_FRACTION,
    MIN_FORWARD_NLL_GAIN,
    SIGMA_FLOOR_RUNGS,
    _mean_nll,
    _search,
    _truth,
    serving_cells_by_day,
)

MIN_TRAIN_CELLS = 300


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path,
                        default=ROOT / "data" / "bayes_basket_target_ssot.db")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "data" / "runtime"
                        / "bayes_basket_target_nwp"
                        / "center_width_replay_table.json")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120.0)
    truth = _truth(conn)
    table: dict[str, dict[str, dict[str, float]]] = {}
    for stage in ("P0", "P0-M", "P1"):
        by_day = serving_cells_by_day(conn, truth, stage=stage)
        days = sorted(by_day)
        per_date: dict[str, dict[str, float]] = {}
        for day in days:
            train = [c for d in days if d < day for c in by_day[d]]
            if len(train) < MIN_TRAIN_CELLS:
                continue
            # The width floor is admitted by the same rule production uses
            # and on the same shape of evidence: the prior days are split
            # train/forward, both arms are fitted on the earlier part, and
            # the floor is taken only where it beats the pooled candidate on
            # the held-out part.  A replay day therefore gets the floor if
            # and only if the layer would have admitted it that morning.
            prior = [d for d in days if d < day]
            cut = max(1, int(len(prior) * (1.0 - FORWARD_WINDOW_FRACTION)))
            inner_train = [c for d in prior[:cut] for c in by_day[d]]
            inner_forward = [
                c for d in (prior[cut:] or prior[-1:]) for c in by_day[d]
            ]
            floor_rungs = 0.0
            if inner_train and inner_forward:
                pooled = _search(inner_train)
                floored = _search(
                    inner_train, sigma_floor_rungs=SIGMA_FLOOR_RUNGS
                )
                gain = _mean_nll(
                    inner_forward,
                    delta_c=pooled[0], width_lambda=pooled[1],
                ) - _mean_nll(
                    inner_forward,
                    delta_c=floored[0], width_lambda=floored[1],
                    sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
                )
                if gain > MIN_FORWARD_NLL_GAIN:
                    floor_rungs = SIGMA_FLOOR_RUNGS
            delta, lam, _ = _search(train, sigma_floor_rungs=floor_rungs)
            per_date[day] = {
                "delta_c": delta,
                "width_lambda": lam,
                "sigma_floor_rungs": floor_rungs,
                "train_cells": len(train),
                "identity_nll": _mean_nll(
                    by_day[day], delta_c=0.0, width_lambda=1.0),
                "fitted_nll": _mean_nll(
                    by_day[day], delta_c=delta, width_lambda=lam,
                    sigma_floor_rungs=floor_rungs),
            }
        table[stage] = per_date
        print("%-5s %d 个日期有标量" % (stage, len(per_date)))
        for day in sorted(per_date)[-3:]:
            e = per_date[day]
            print("   %s  Δ=%+.2f λ=%.3f 地板=%.2f  训练 cell %5d"
                  "  当日 NLL %.4f→%.4f"
                  % (day, e["delta_c"], e["width_lambda"],
                     e["sigma_floor_rungs"], e["train_cells"],
                     e["identity_nll"], e["fitted_nll"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"schema": "center_width_replay_table_v1", "stages": table},
                   indent=2, sort_keys=True), encoding="utf-8")
    print("\n写入", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
