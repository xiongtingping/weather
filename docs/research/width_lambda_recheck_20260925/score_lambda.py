#!/usr/bin/env python3
"""PROTOCOL.md: pick lambda* on the training days by LogScore, judge it against 0.75 on the test days.

Reads pass1/*.jsonl.gz (capture_lambda.py: per update, the floored eleven-rung
Brier and LogScore of the as-served vector and of every grid lambda).
Station-day weights, three-day block bootstrap over event days.  Writes
results.json.
"""
from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
GRID = ("0.60", "0.70", "0.75", "0.80", "0.85", "0.90", "1.00")
INCUMBENT = "0.75"
TRAIN = ("2026-07-20", "2026-08-31")
TEST = ("2026-09-01", "2026-09-23")
BLOCK, BOOT = 3, 2000


def load():
    rows = []
    for path in sorted((HERE / "pass1").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def weights(rows):
    count = defaultdict(int)
    for r in rows:
        count[(r["station"], r["event_date"])] += 1
    return np.array([1.0 / count[(r["station"], r["event_date"])] for r in rows]), len(count)


def means(rows):
    w, station_days = weights(rows)
    out = {"updates": len(rows), "station_days": station_days}
    for arm in ("served",) + GRID:
        b = np.array([r["scores"][arm][0] for r in rows])
        n = np.array([r["scores"][arm][1] for r in rows])
        out[arm] = {"brier": float((w * b).sum() / w.sum()), "logscore": float((w * n).sum() / w.sum())}
    return out


def compare(rows, base, test):
    w, station_days = weights(rows)
    db = np.array([r["scores"][test][0] - r["scores"][base][0] for r in rows])
    dn = np.array([r["scores"][test][1] - r["scores"][base][1] for r in rows])
    days = sorted({r["event_date"] for r in rows})
    by_day = defaultdict(list)
    for i, r in enumerate(rows):
        by_day[r["event_date"]].append(i)
    rng = np.random.default_rng(20260925)
    n_blocks = math.ceil(len(days) / BLOCK)
    bb, bn = [], []
    for _ in range(BOOT):
        idx = np.concatenate([by_day[days[(s + j) % len(days)]]
                              for s in rng.integers(0, len(days), n_blocks) for j in range(BLOCK)])
        bb.append(float((w[idx] * db[idx]).sum() / w[idx].sum()))
        bn.append(float((w[idx] * dn[idx]).sum() / w[idx].sum()))
    out = {"base": base, "test": test, "updates": len(rows), "station_days": station_days, "days": len(days),
           "delta": {"brier": float((w * db).sum() / w.sum()), "logscore": float((w * dn).sum() / w.sum())},
           "ci95": {"brier": [float(np.percentile(bb, 2.5)), float(np.percentile(bb, 97.5))],
                    "logscore": [float(np.percentile(bn, 2.5)), float(np.percentile(bn, 97.5))]}}
    out["both_lower"] = out["ci95"]["brier"][1] < 0 and out["ci95"]["logscore"][1] < 0
    return out


def main():
    rows = load()
    train = [r for r in rows if TRAIN[0] <= r["event_date"] <= TRAIN[1]]
    test = [r for r in rows if TEST[0] <= r["event_date"] <= TEST[1]]
    train_means, test_means = means(train), means(test)
    chosen = min(GRID, key=lambda arm: train_means[arm]["logscore"])
    no_table = sum(1 for r in rows if r["served_lambda"] is None)
    result = {
        "protocol": "PROTOCOL.md",
        "updates": len(rows), "updates_without_width_table_then": no_table,
        "served_lambdas": sorted({r["served_lambda"] for r in rows if r["served_lambda"] is not None}),
        "train_means": train_means, "test_means": test_means, "lambda_star": chosen,
        # The pre-registered verdict.
        "verdict": compare(test, INCUMBENT, chosen) if chosen != INCUMBENT else None,
        # Asked 2026-09-25 (「新的模型目标需要这样做吗」): does the layer earn its
        # place by charter 211's scores -- 1.00 is the layer taken out.
        # Reported, not the pre-registered verdict.
        "layer_out_test": compare(test, INCUMBENT, "1.00"),
        "layer_out_all": compare(rows, INCUMBENT, "1.00"),
    }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"updates {len(rows)}  without a width table then {no_table}  served lambdas {result['served_lambdas']}")
    for label, m in (("train", train_means), ("test", test_means)):
        print(f"{label}: {m['station_days']} station-days, {m['updates']} updates")
        for arm in ("served",) + GRID:
            print(f"  {arm:>6s}  Brier {m[arm]['brier']:.5f}  LogScore {m[arm]['logscore']:.5f}")
    print("lambda* (train LogScore):", chosen)
    for key in ("verdict", "layer_out_test", "layer_out_all"):
        c = result[key]
        if c is None:
            print(f"{key}: lambda* is the incumbent")
            continue
        print(f"{key}: {c['test']} vs {c['base']} over {c['station_days']} station-days | "
              f"Brier {c['delta']['brier']:+.5f} [{c['ci95']['brier'][0]:+.5f},{c['ci95']['brier'][1]:+.5f}] | "
              f"LogScore {c['delta']['logscore']:+.5f} [{c['ci95']['logscore'][0]:+.5f},{c['ci95']['logscore'][1]:+.5f}] | "
              f"both lower={c['both_lower']}")


if __name__ == "__main__":
    main()
