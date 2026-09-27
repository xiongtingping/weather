#!/usr/bin/env python3
"""Post-hoc, report only: the Proper line's first decision against the first fuller-pool decision.

The first P0 decision of a station-day is usually a two-family pool (see
REPORT.md).  Per station-day, pair the first update more than 36h out with
the first update that is still more than 36h out and seats at least four
families, and compare their exact scores at today's lambda 0.75.  This mixes
pool size with a few hours' lead; it is what "wait for a fuller pool, still
inside the Proper window" would have bought on the model side, nothing about
prices.

    python3 first_vs_fuller_pool.py   -> results_fuller.json and a printed summary
"""

from __future__ import annotations

import json
import math
from collections import defaultdict

import numpy as np

import far_lead as F
from pool_tier import pool_sizes

FULL = 4


def main():
    rows = F.load()
    size = pool_sizes(rows)
    exact = np.array([r["exact"] for r in rows])[:, F.BASE]
    by_sd = defaultdict(list)
    for i, r in enumerate(rows):
        if r["h"] > F.FAR_H:
            by_sd[(r["station"], r["day"])].append(i)
    result = {}
    for label, lo, hi in (("test_0901_0923", *F.TEST), ("all_days", "0000", "9999")):
        pairs, waits, no_fuller = [], [], 0
        for (station, day), idx in by_sd.items():
            if not lo <= day <= hi:
                continue
            idx.sort(key=lambda i: rows[i]["at"])
            first = idx[0]
            fuller = next((i for i in idx if size[i] >= FULL), None)
            if fuller is None:
                no_fuller += 1
                continue
            pairs.append((day, first, fuller))
            waits.append(rows[first]["h"] - rows[fuller]["h"])
        days = sorted({d for d, _f, _g in pairs})
        by_day = defaultdict(list)
        for j, (d, _f, _g) in enumerate(pairs):
            by_day[d].append(j)
        first_s = np.array([exact[f] for _d, f, _g in pairs])
        full_s = np.array([exact[g] for _d, _f, g in pairs])
        rng = np.random.default_rng(F.SEED)
        n_blocks = math.ceil(len(days) / F.BLOCK)
        boots = []
        for _ in range(F.BOOT):
            j = np.concatenate([by_day[days[(s + b) % len(days)]]
                                for s in rng.integers(0, len(days), n_blocks) for b in range(F.BLOCK)])
            boots.append((full_s[j] - first_s[j]).mean(axis=0))
        boots = np.array(boots)
        same = sum(1 for _d, f, g in pairs if f == g)
        out = {
            "station_days_with_far_updates": sum(1 for (s, d) in by_sd if lo <= d <= hi),
            "paired": len(pairs), "already_full_at_first": same, "never_full_inside_window": no_fuller,
            "wait_hours_p25_p50_p75": [float(np.percentile(waits, q)) for q in (25, 50, 75)],
            "first": {"brier": float(first_s[:, 0].mean()), "logscore": float(first_s[:, 1].mean())},
            "fuller": {"brier": float(full_s[:, 0].mean()), "logscore": float(full_s[:, 1].mean())},
            "delta_ci95": {k: [float(np.percentile(boots[:, s], 2.5)), float(np.percentile(boots[:, s], 97.5))]
                           for s, k in ((0, "brier"), (1, "logscore"))},
        }
        result[label] = out
        print(f"== {label}: {out['paired']} paired station-days ({same} already >=4 at first; "
              f"{no_fuller} never >=4 inside the window); wait p25/p50/p75 "
              f"{'/'.join(f'{w:.1f}' for w in out['wait_hours_p25_p50_p75'])} h")
        for k in ("brier", "logscore"):
            d = out["fuller"][k] - out["first"][k]
            print(f"   {k}: first {out['first'][k]:.4f} -> fuller {out['fuller'][k]:.4f} ({d:+.4f} "
                  f"[{out['delta_ci95'][k][0]:+.4f},{out['delta_ci95'][k][1]:+.4f}])")
    (F.HERE / "results_fuller.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
