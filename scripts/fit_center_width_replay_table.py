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
# How many days before a date may train it.  The unbounded version made every
# later date pay for the whole history: one grid point transforms every
# training cell, the grid is 627 points, and a single search over 3,662 cells
# measured 54 seconds.  It is also the discipline the store already keeps --
# a fit must not reach back past arithmetic nobody serves -- applied to a
# table whose whole purpose is to say what could have been known that morning.
DEFAULT_TRAIN_DAYS = 10


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path,
                        default=ROOT / "data" / "bayes_basket_target_ssot.db")
    parser.add_argument(
        "--since",
        help=(
            "Earliest decision timestamp to build cells from.  Every date "
            "still trains only on days earlier than itself, so the table "
            "stays as-of correct; this bounds how far back the warm-up "
            "reaches and is what keeps the build affordable."
        ),
    )
    parser.add_argument(
        "--train-days", type=int, default=DEFAULT_TRAIN_DAYS,
        help="Days of history each date may train on (0 = every earlier day).",
    )
    parser.add_argument(
        "--stages", default="P0,P0-M,P1",
        help="Comma-separated stages to fit (default all three).",
    )
    parser.add_argument(
        "--out-control", type=Path, default=None,
        help=(
            "Where to write the unfloored control arm.  One walk produces "
            "both tables, because both arms need the same cells and the same "
            "as-of ordering; building them separately would pay for that "
            "twice and risk them disagreeing about anything but the floor."
        ),
    )
    parser.add_argument("--out", type=Path,
                        default=ROOT / "data" / "runtime"
                        / "bayes_basket_target_nwp"
                        / "center_width_replay_table.json")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120.0)
    truth = _truth(conn)
    floored_table: dict[str, dict[str, dict[str, float]]] = {}
    control_table: dict[str, dict[str, dict[str, float]]] = {}
    for stage in [s.strip() for s in args.stages.split(",") if s.strip()]:
        by_day = serving_cells_by_day(
            conn, truth, stage=stage, min_decision_at_utc=args.since,
        )
        days = sorted(by_day)
        floored_dates: dict[str, dict[str, float]] = {}
        control_dates: dict[str, dict[str, float]] = {}
        # The floor's verdict walks forward one day at a time.  Each date is
        # judged by how the two arms fitted on *earlier* days scored on the
        # days after them -- a prequential comparison, so the verdict a date
        # gets could have been computed that morning and never sees itself.
        # The alternative, re-splitting the prior days and refitting both arms
        # for every date, is two extra grid searches a day for the same
        # answer.
        preq_pooled = 0.0
        preq_floored = 0.0
        preq_cells = 0
        for index, day in enumerate(days):
            prior = days[:index]
            if args.train_days > 0:
                prior = prior[-args.train_days:]
            train = [c for d in prior for c in by_day[d]]
            if len(train) < MIN_TRAIN_CELLS:
                continue
            pooled_delta, pooled_lam, _ = _search(train)
            floor_delta, floor_lam, _ = _search(
                train, sigma_floor_rungs=SIGMA_FLOOR_RUNGS
            )
            today = by_day[day]
            # The verdict in force this morning, from days already walked.
            admitted = (
                preq_cells > 0
                and (preq_pooled - preq_floored) / preq_cells
                > MIN_FORWARD_NLL_GAIN
            )
            identity_nll = _mean_nll(today, delta_c=0.0, width_lambda=1.0)
            pooled_nll = _mean_nll(
                today, delta_c=pooled_delta, width_lambda=pooled_lam
            )
            floored_nll = _mean_nll(
                today, delta_c=floor_delta, width_lambda=floor_lam,
                sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
            )
            control_dates[day] = {
                "delta_c": pooled_delta,
                "width_lambda": pooled_lam,
                "sigma_floor_rungs": 0.0,
                "train_cells": len(train),
                "identity_nll": identity_nll,
                "fitted_nll": pooled_nll,
            }
            floored_dates[day] = {
                "delta_c": floor_delta if admitted else pooled_delta,
                "width_lambda": floor_lam if admitted else pooled_lam,
                "sigma_floor_rungs": SIGMA_FLOOR_RUNGS if admitted else 0.0,
                "train_cells": len(train),
                "identity_nll": identity_nll,
                "fitted_nll": floored_nll if admitted else pooled_nll,
                "sigma_floor_admitted": admitted,
            }
            preq_pooled += pooled_nll * len(today)
            preq_floored += floored_nll * len(today)
            preq_cells += len(today)
            print(
                "   %s  pooled Δ=%+.2f λ=%.3f  floored Δ=%+.2f λ=%.3f"
                "  地板=%s  train %5d  当日 NLL 恒等 %.4f / 池化 %.4f / 地板 %.4f"
                % (day, pooled_delta, pooled_lam, floor_delta, floor_lam,
                   "on" if admitted else "off", len(train),
                   identity_nll, pooled_nll, floored_nll),
                flush=True,
            )
        floored_table[stage] = floored_dates
        control_table[stage] = control_dates
        preq = (
            (preq_pooled - preq_floored) / preq_cells if preq_cells else 0.0
        )
        print("%-5s %d 个日期有标量；前推累计 地板−池化 NLL %+.4f (%d cells)"
              % (stage, len(floored_dates), preq, preq_cells), flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"schema": "center_width_replay_table_v1",
                    "sigma_floor_arm": "on",
                    "train_days": args.train_days,
                    "since": args.since,
                    "stages": floored_table},
                   indent=2, sort_keys=True), encoding="utf-8")
    print("\n写入", args.out)
    if args.out_control is not None:
        args.out_control.parent.mkdir(parents=True, exist_ok=True)
        args.out_control.write_text(
            json.dumps({"schema": "center_width_replay_table_v1",
                        "sigma_floor_arm": "off",
                        "train_days": args.train_days,
                        "since": args.since,
                        "stages": control_table},
                       indent=2, sort_keys=True), encoding="utf-8")
        print("写入", args.out_control)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
