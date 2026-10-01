#!/usr/bin/env python3
"""PROTOCOL_pool.md: a separate width lambda for thin pools (<=3 seated families).

Same data, scoring and bootstrap as far_lead.py (imported, not copied); only
the tier changes -- the seated family count instead of the hours to
settlement.  Exact scores only: the fidelity gate of PROTOCOL.md failed, so
no calibration reading is rebuilt here.

    python3 pool_tier.py       -> results_pool.json and a printed summary
"""

from __future__ import annotations

import gzip
import json

import numpy as np

import far_lead as F

THIN = 3
SIZES = (2, 3, 4, 5, 6)


def pool_sizes(rows):
    size = {}
    for path in sorted(F.LAMBDA_DIR.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                size[(r["station"], r["event_date"], r["at"])] = int(r["pool"])
    return np.array([size[(r["station"], r["day"], r["at"])] for r in rows])


def walk_forward(rows, exact_ls, member):
    """lambda(d) per test day on the tier's rows whose truth was known before day d's first decision."""

    at = np.array([F._utc(r["at"]).timestamp() for r in rows])
    known = np.array([r["truth_known"] for r in rows])
    day = np.array([r["day"] for r in rows])
    chosen = {}
    for d in sorted({x for x in day if F.TEST[0] <= x <= F.TEST[1]}):
        idx = np.nonzero((known < at[day == d].min()) & member)[0]
        w = F.weights(rows, idx)
        scores = (w[:, None] * exact_ls[idx]).sum(axis=0) / w.sum()
        best = min(range(len(F.GRID)), key=lambda g: (round(scores[g], 12), abs(float(F.GRID[g]) - 0.75)))
        chosen[d] = {"lambda": F.GRID[best], "train_updates": int(len(idx)),
                     "train_logscore_by_lambda": {F.GRID[g]: round(float(scores[g]), 5) for g in range(len(F.GRID))}}
    return chosen


def compare(rows, idx, B, T):
    return {name: F.mean_diff(rows, idx, B[idx, s], T[idx, s]) for s, name in ((0, "brier"), (1, "logscore"))}


def main():
    rows = F.load()
    n = len(rows)
    exact = np.array([r["exact"] for r in rows])
    size = pool_sizes(rows)
    in_test = np.array([F.TEST[0] <= r["day"] <= F.TEST[1] for r in rows])
    far = np.array([r["h"] > F.FAR_H for r in rows])
    first = np.array([bool(r.get("first")) for r in rows])
    thin = size <= THIN
    B = exact[:, F.BASE]

    lam_small = walk_forward(rows, exact[:, :, 1], thin)
    g_p = np.full(n, F.BASE)
    for i in np.nonzero(in_test & thin)[0]:
        g_p[i] = F.GRID.index(lam_small[rows[i]["day"]]["lambda"])
    P = exact[np.arange(n), g_p]

    lam_by_size = {s: walk_forward(rows, exact[:, :, 1], size == s) for s in SIZES}
    g_5 = np.full(n, F.BASE)
    for i in np.nonzero(in_test)[0]:
        g_5[i] = F.GRID.index(lam_by_size[int(size[i])][rows[i]["day"]]["lambda"])
    P5 = exact[np.arange(n), g_5]

    panel = np.nonzero(in_test & thin)[0]
    verdict = compare(rows, panel, B, P)
    for s, name in ((0, "brier"), (1, "logscore")):
        verdict[name]["halves"] = []
        for lo, hi in F.HALVES:
            half = np.array([i for i in panel if lo <= rows[i]["day"] <= hi])
            wh = F.weights(rows, half)
            verdict[name]["halves"].append(float((wh * (P[half, s] - B[half, s])).sum() / wh.sum()))
    holds = all(verdict[k]["delta"] < 0 and verdict[k]["ci95"][1] < 0 and all(x < 0 for x in verdict[k]["halves"])
                for k in ("brier", "logscore"))
    all_test = np.nonzero(in_test)[0]
    first_far = np.nonzero(in_test & far & first)[0]
    thick = np.nonzero(in_test & ~thin)[0]
    result = {
        "panel_updates": int(len(panel)),
        "panel_station_days": len({(rows[i]["station"], rows[i]["day"]) for i in panel}),
        "verdict_panel": verdict,
        "verdict": "HOLDS" if holds else "DOES_NOT_HOLD",
        "lambda_small_by_day": lam_small,
        "all_test": compare(rows, all_test, B, P),
        "first_decision_far": compare(rows, first_far, B, P),
        "thick_panel_max_abs_diff": float(np.abs(P[thick] - B[thick]).max()),
        "arm_P5": {
            "lambda_by_size_by_day": {str(s): {d: v["lambda"] for d, v in lam_by_size[s].items()} for s in SIZES},
            "all_test": compare(rows, all_test, B, P5),
            "thin_panel": compare(rows, panel, B, P5),
            "first_decision_far": compare(rows, first_far, B, P5),
        },
    }
    (F.HERE / "results_pool.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))

    print(f"thin panel: {result['panel_updates']} updates, {result['panel_station_days']} station-days")
    print("lambda_small by day:", {d: v["lambda"] for d, v in lam_small.items()})
    for k, v in verdict.items():
        print(f"P vs B thin panel {k}: {v['base']:.5f}->{v['test']:.5f} ({v['delta']:+.5f} "
              f"[{v['ci95'][0]:+.5f},{v['ci95'][1]:+.5f}]) halves {[round(x, 5) for x in v['halves']]}")
    print("VERDICT:", result["verdict"])
    for label in ("all_test", "first_decision_far"):
        for k, v in result[label].items():
            print(f"{label} {k}: {v['base']:.5f}->{v['test']:.5f} ({v['delta']:+.5f} [{v['ci95'][0]:+.5f},{v['ci95'][1]:+.5f}])")
    print("thick panel max |P-B|:", result["thick_panel_max_abs_diff"])
    print("P5 lambda by size (distinct values over test days):",
          {s: sorted(set(v.values())) for s, v in result["arm_P5"]["lambda_by_size_by_day"].items()})
    for label in ("all_test", "thin_panel", "first_decision_far"):
        for k, v in result["arm_P5"][label].items():
            print(f"P5 {label} {k}: {v['base']:.5f}->{v['test']:.5f} ({v['delta']:+.5f} [{v['ci95'][0]:+.5f},{v['ci95'][1]:+.5f}])")


if __name__ == "__main__":
    main()
