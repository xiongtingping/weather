#!/usr/bin/env python3
"""PROTOCOL_floor.md: the 2x2 re-scored on the served arithmetic with charter 224's floor.

Each update's four served vectors are folded onto one eleven-rung open ladder
(centre = the rounded mean of the four arms' means, independent of truth),
floored by production's floor_probabilities and scored.  Writes
results_floor.json.
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

ARMS = ("mom_off", "ll_off", "mom_on", "ll_on")
COMPARISONS = {"A_216_under_219": ("mom_on", "ll_on"), "B_219_under_216": ("ll_off", "ll_on"),
               "C_219_under_moment": ("mom_off", "mom_on"), "D_both_vs_old_production": ("mom_off", "ll_on")}
HALF = 5
BLOCK, BOOT = 3, 2000


def fold(vec, centre):
    rungs = [0.0] * (2 * HALF + 1)
    for key, value in vec.items():
        index = min(max(int(key) - centre, -HALF), HALF) + HALF
        rungs[index] += float(value)
    total = sum(rungs)
    return [v / total for v in rungs]


def load():
    rows = []
    for path in sorted((HERE / "pass1").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                centre = int(round(sum(r[a]["mean"] for a in ARMS) / len(ARMS)))
                truth = min(max(math.floor(r["actual"] + 0.5) - centre, -HALF), HALF) + HALF
                scores = {}
                for arm in ARMS:
                    ladder = fold(r[arm]["vec"], centre)
                    floored, _ = floor_probabilities({str(i): v for i, v in enumerate(ladder)})
                    p = [floored[str(i)] for i in range(2 * HALF + 1)]
                    brier = sum((v - (1.0 if i == truth else 0.0)) ** 2 for i, v in enumerate(p))
                    scores[arm] = (brier, -math.log(max(p[truth], 1e-12)), p[truth])
                rows.append({"station": r["station"], "day": r["event_date"], "pool": len(r["pool"]), "scores": scores})
    return rows


def compare(rows, base, test, label):
    count = defaultdict(int)
    for r in rows:
        count[(r["station"], r["day"])] += 1
    w = np.array([1.0 / count[(r["station"], r["day"])] for r in rows])
    db = np.array([r["scores"][test][0] - r["scores"][base][0] for r in rows])
    dn = np.array([r["scores"][test][1] - r["scores"][base][1] for r in rows])
    days = sorted({r["day"] for r in rows})
    by_day = defaultdict(list)
    for i, r in enumerate(rows):
        by_day[r["day"]].append(i)
    rng = np.random.default_rng(20260925)
    n_blocks = math.ceil(len(days) / BLOCK)
    bb, bn = [], []
    for _ in range(BOOT):
        idx = np.concatenate([by_day[days[(s + j) % len(days)]] for s in rng.integers(0, len(days), n_blocks) for j in range(BLOCK)])
        bb.append(float((w[idx] * db[idx]).sum() / w[idx].sum())); bn.append(float((w[idx] * dn[idx]).sum() / w[idx].sum()))
    mean = lambda arm, j: float(sum(wi * r["scores"][arm][j] for wi, r in zip(w, rows)) / w.sum())  # noqa: E731
    out = {"label": label, "updates": len(rows), "station_days": len(count), "days": len(days),
           "base": {"brier": mean(base, 0), "nll": mean(base, 1)}, "test": {"brier": mean(test, 0), "nll": mean(test, 1)},
           "delta": {"brier": float((w * db).sum() / w.sum()), "nll": float((w * dn).sum() / w.sum())},
           "ci95": {"brier": [float(np.percentile(bb, 2.5)), float(np.percentile(bb, 97.5))],
                    "nll": [float(np.percentile(bn, 2.5)), float(np.percentile(bn, 97.5))]}}
    out["holds"] = out["ci95"]["brier"][1] < 0 and out["ci95"]["nll"][1] < 0
    return out


def main():
    rows = load()
    zeros = {arm: sum(1 for r in rows if r["scores"][arm][2] <= 0.0) for arm in ARMS}
    segments = {"pooled": rows, "before_134": [r for r in rows if r["day"] < "2026-09-14"],
                "after_134": [r for r in rows if r["day"] >= "2026-09-14"], "pool_3plus": [r for r in rows if r["pool"] >= 3]}
    result = {"protocol": "PROTOCOL_floor.md", "zero_truth_probability_updates": zeros, "comparisons": {}}
    for name, (base, test) in COMPARISONS.items():
        result["comparisons"][name] = {seg: compare(recs, base, test, seg) for seg, recs in segments.items()}
    (HERE / "results_floor.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("zero-P(truth) updates after the floor:", zeros)
    for name, segs in result["comparisons"].items():
        for seg, c in segs.items():
            print(f"{name:26s} {seg:11s} {c['station_days']:5d} 站日 | Brier {c['base']['brier']:.5f}->{c['test']['brier']:.5f} "
                  f"({c['delta']['brier']:+.5f} [{c['ci95']['brier'][0]:+.5f},{c['ci95']['brier'][1]:+.5f}]) | LogScore {c['base']['nll']:.4f}->{c['test']['nll']:.4f} "
                  f"({c['delta']['nll']:+.4f} [{c['ci95']['nll'][0]:+.4f},{c['ci95']['nll'][1]:+.4f}]) | holds={c['holds']}", flush=True)


if __name__ == "__main__":
    main()
