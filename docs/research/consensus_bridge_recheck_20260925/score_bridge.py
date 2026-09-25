#!/usr/bin/env python3
"""PROTOCOL.md verdicts from pass1/*.jsonl.gz (capture_bridge.py).

(甲) bridge parameters: pick on the training days by LogScore at the served
width 0.75, judge against the current (1.5, 2.0, 0.4) on the test days.
(乙) open-edge tail transport against the point rule at the current
parameters, all days.  Station-day weights, three-day block bootstrap.
Writes results.json.
"""
from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
# Only the arm names are needed; read them without importing the replay machinery.
_source = (HERE / "capture_bridge.py").read_text()
NAMES = ["off"] + [f"s{start:.1f}_m{lmax:.1f}" for start in (0.5, 1.0, 1.5, 2.0) for lmax in (0.2, 0.4, 0.6, 0.8)]
assert 'VARIANTS = (("off", None),)' in _source and "for start in (0.5, 1.0, 1.5, 2.0) for lmax in (0.2, 0.4, 0.6, 0.8))" in _source
WIDTHS = (0.70, 0.75, 0.80, 0.85, 0.90)
SERVED_WIDTH = WIDTHS.index(0.75)
CURRENT = NAMES.index("s1.5_m0.4")
TRAIN = ("2026-07-20", "2026-08-31")
TEST = ("2026-09-01", "2026-09-23")
BLOCK, BOOT = 3, 2000


def load():
    rows = []
    for path in sorted((HERE / "pass1").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                arms = [r["point"][key] for key in r["lam_keys"]]         # per variant: 5 widths x [brier, logscore]
                r["arms"] = arms
                r["tail_arm"] = r.get("tail") or arms[CURRENT]
                rows.append(r)
    return rows


def weights(rows):
    count = defaultdict(int)
    for r in rows:
        count[(r["station"], r["event_date"])] += 1
    return np.array([1.0 / count[(r["station"], r["event_date"])] for r in rows]), len(count)


def mean_of(rows, pick):
    w, _ = weights(rows)
    values = np.array([pick(r) for r in rows])
    return (w[:, None] * values).sum(axis=0) / w.sum()


def compare(rows, base, test):
    """test - base, both functions of a row returning (brier, logscore)."""

    w, station_days = weights(rows)
    d = np.array([[t - b for t, b in zip(test(r), base(r))] for r in rows])
    days = sorted({r["event_date"] for r in rows})
    by_day = defaultdict(list)
    for i, r in enumerate(rows):
        by_day[r["event_date"]].append(i)
    rng = np.random.default_rng(20260925)
    n_blocks = math.ceil(len(days) / BLOCK)
    boot = []
    for _ in range(BOOT):
        idx = np.concatenate([by_day[days[(s + j) % len(days)]]
                              for s in rng.integers(0, len(days), n_blocks) for j in range(BLOCK)])
        boot.append((w[idx, None] * d[idx]).sum(axis=0) / w[idx].sum())
    boot = np.array(boot)
    delta = (w[:, None] * d).sum(axis=0) / w.sum()
    lo, hi = np.percentile(boot, 2.5, axis=0), np.percentile(boot, 97.5, axis=0)
    return {"updates": len(rows), "station_days": station_days, "days": len(days),
            "d_brier": float(delta[0]), "ci_brier": [float(lo[0]), float(hi[0])],
            "d_logscore": float(delta[1]), "ci_logscore": [float(lo[1]), float(hi[1])],
            "both_lower": bool(hi[0] < 0 and hi[1] < 0)}


def arm(variant, width):
    return lambda r: r["arms"][variant][width]


def main():
    rows = load()
    train = [r for r in rows if TRAIN[0] <= r["event_date"] <= TRAIN[1]]
    test = [r for r in rows if TEST[0] <= r["event_date"] <= TEST[1]]
    # The served arm must be the current bridge at the served width wherever the
    # served width table was 0.75 (band as served in both).
    checked = [r for r in rows if r["has_width_table"]]
    mismatch = sum(1 for r in checked if r["served"] != r["arms"][CURRENT][SERVED_WIDTH])
    result = {"protocol": "PROTOCOL.md", "updates": len(rows), "train_updates": len(train),
              "test_updates": len(test), "served_arm_check": {"rows": len(checked), "mismatch": mismatch},
              "bridge_on_share": sum(1 for r in rows if r["lam_current"] > 0) / len(rows)}
    # (甲) selection at the served width, on LogScore.
    train_means = {NAMES[v]: mean_of(train, lambda r, v=v: r["arms"][v][SERVED_WIDTH]).tolist() for v in range(len(NAMES))}
    test_means = {NAMES[v]: mean_of(test, lambda r, v=v: r["arms"][v][SERVED_WIDTH]).tolist() for v in range(len(NAMES))}
    chosen = min(range(len(NAMES)), key=lambda v: train_means[NAMES[v]][1])
    joint = min(((v, w) for v in range(len(NAMES)) for w in range(len(WIDTHS))),
                key=lambda vw: mean_of(train, arm(*vw))[1])
    result["jia"] = {
        "train_means_at_0.75": train_means, "test_means_at_0.75": test_means,
        "chosen": NAMES[chosen],
        "verdict": None if chosen == CURRENT else compare(test, arm(CURRENT, SERVED_WIDTH), arm(chosen, SERVED_WIDTH)),
        "joint_chosen": [NAMES[joint[0]], WIDTHS[joint[1]]],
        "joint_vs_current_test": compare(test, arm(CURRENT, SERVED_WIDTH), arm(*joint)),
        "off_vs_current_test": compare(test, arm(CURRENT, SERVED_WIDTH), arm(0, SERVED_WIDTH)),
        "off_vs_current_all": compare(rows, arm(CURRENT, SERVED_WIDTH), arm(0, SERVED_WIDTH)),
    }
    # (乙) tail against point at the current parameters, served width, all days.
    point = arm(CURRENT, SERVED_WIDTH)
    tail = lambda r: r["tail_arm"][SERVED_WIDTH]  # noqa: E731
    on = [r for r in rows if r["lam_current"] > 0]
    mode_open = [r for r in rows if r.get("mode_in_open")]
    result["yi"] = {
        "all": compare(rows, point, tail),
        "bridge_on": compare(on, point, tail),
        "mode_in_open": compare(mode_open, point, tail),
        "bridge_on_updates": len(on), "mode_in_open_updates": len(mode_open),
        "test_only": compare(test, point, tail),
    }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))

    def line(label, c):
        return (f"{label}: {c['station_days']} 站日 | Brier {c['d_brier']:+.5f} [{c['ci_brier'][0]:+.5f},{c['ci_brier'][1]:+.5f}]"
                f" | LogScore {c['d_logscore']:+.5f} [{c['ci_logscore'][0]:+.5f},{c['ci_logscore'][1]:+.5f}] | both lower={c['both_lower']}")

    print(f"updates {len(rows)} (train {len(train)}, test {len(test)}); bridge on {result['bridge_on_share']:.1%}; served-arm check {result['served_arm_check']}")
    print("test-period means at width 0.75 (Brier, LogScore):")
    for name in NAMES:
        tm, te = train_means[name], test_means[name]
        print(f"  {name:10s} train {tm[0]:.5f} {tm[1]:.5f} | test {te[0]:.5f} {te[1]:.5f}")
    print("(甲) chosen on train:", NAMES[chosen])
    if result["jia"]["verdict"]:
        print(line("   chosen vs current (test)", result["jia"]["verdict"]))
    print("   joint chosen:", result["jia"]["joint_chosen"])
    print(line("   joint vs current@0.75 (test)", result["jia"]["joint_vs_current_test"]))
    print(line("   off vs current (test)", result["jia"]["off_vs_current_test"]))
    print(line("   off vs current (all)", result["jia"]["off_vs_current_all"]))
    print("(乙) tail vs point, current parameters, width 0.75:")
    for key in ("all", "test_only", "bridge_on", "mode_in_open"):
        print(line(f"   {key}", result["yi"][key]))


if __name__ == "__main__":
    main()
