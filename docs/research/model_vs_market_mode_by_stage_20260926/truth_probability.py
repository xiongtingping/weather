#!/usr/bin/env python3
"""The probability each side put on the bucket that settled, by stage and by
hours to settlement.

熊先生 2026-09-26 追问：「模型在哪个阶段对真值的概率比市场要准？」  Same
population and arithmetic as measure.py (the model's vector on the market's
contract, renormalised and floored by charter 224; the market's normalised mid
vector).  Every station-day counts once per cell.  Read-only; writes
truth_probability_results.json.

    python3 truth_probability.py [cache.pkl]
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure import ROOT, load, prepare  # noqa: E402

DRAWS = 1000
SEED = 20260926
SPLIT = "2026-09-19"
HOURS = ((0, 3), (3, 6), (6, 9), (9, 12), (12, 18), (18, 24), (24, 36), (36, 48), (48, 60), (60, 999))


def once(items):
    count = Counter((d["station"], d["date"]) for d in items)
    return [dict(d, w=1.0 / count[(d["station"], d["date"])]) for d in items]


def mean_ci(items, f, seed):
    days = sorted({d["date"] for d in items})
    index = {day: i for i, day in enumerate(days)}
    di = np.array([index[d["date"]] for d in items])
    w = np.array([d["w"] for d in items])
    v = np.array([float(f(d)) for d in items])
    s = np.bincount(di, weights=w * v, minlength=len(days))
    t = np.bincount(di, weights=w, minlength=len(days))
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(DRAWS):
        pick = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
        boots.append(float(pick @ s / (pick @ t)))
    return [float(s.sum() / t.sum()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]


def cell(items, seed):
    items = once(items)
    if len({d["date"] for d in items}) < 3:
        return None
    w = np.array([d["w"] for d in items])

    def wm(f):
        return float((w * np.array([float(f(d)) for d in items])).sum() / w.sum())

    return {
        "station_days": len({(d["station"], d["date"]) for d in items}),
        "event_days": len({d["date"] for d in items}),
        "p_truth_model": wm(lambda d: math.exp(-d["ls_p"])),
        "p_truth_market": wm(lambda d: math.exp(-d["ls_m"])),
        "p_truth_model_minus_market": mean_ci(items, lambda d: math.exp(-d["ls_p"]) - math.exp(-d["ls_m"]), seed),
        "share_model_higher": wm(lambda d: d["ls_p"] < d["ls_m"]),
        "logscore_model_minus_market": mean_ci(items, lambda d: d["ls_p"] - d["ls_m"], seed + 1),
        "brier_model_minus_market": mean_ci(items, lambda d: d["br_p"] - d["br_m"], seed + 2),
    }


def main():
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if cache is not None and cache.exists():
        rows = pickle.loads(cache.read_bytes())
        rows = rows[0] if isinstance(rows, tuple) else rows
    else:
        rows = load(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro")
    recs = prepare(rows)
    result = {"question": "熊先生 2026-09-26：模型在哪个阶段对真值的概率比市场要准", "periods": {}}
    for p, (period, keep) in enumerate((("all", lambda d: True), ("since_0919", lambda d: d["date"] >= SPLIT))):
        chosen = [d for d in recs if keep(d)]
        out = {s: cell([d for d in chosen if d["stage"] == s], SEED + 100 * p + i)
               for i, s in enumerate(("P0", "P0-M", "P1"))}
        for j, (lo, hi) in enumerate(HOURS):
            out[f"{lo}-{hi}h"] = cell([d for d in chosen if d["hours"] is not None and lo <= d["hours"] < hi],
                                      SEED + 100 * p + 10 + j)
        result["periods"][period] = out
    (HERE / "truth_probability_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for period, out in result["periods"].items():
        for key, row in out.items():
            if row:
                print(period, key, row["station_days"], round(100 * row["p_truth_model"], 1),
                      round(100 * row["p_truth_market"], 1),
                      [round(100 * x, 1) for x in row["p_truth_model_minus_market"]],
                      round(100 * row["share_model_higher"], 1),
                      [round(x, 3) for x in row["logscore_model_minus_market"]])


if __name__ == "__main__":
    main()
