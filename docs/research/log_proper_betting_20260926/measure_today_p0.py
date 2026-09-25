#!/usr/bin/env python3
"""PROTOCOL.md, second population: today's P0 arithmetic (band-forward vectors of
band_layer_recheck_20260925, six systems, charters 216+219, width 0.75) against the
last committed WS book within 30 minutes before the update, as cached by
model_market_gap_decomposition_20260926 (decompose_cache.pkl, element 2).  The
cache has no asks, so only the frictionless three quantities are read.  The
model vector is floored with production's charter-224 floor before scoring, as
that study did.  No SSOT access.  Writes results_today_p0.json.

    python3 measure_today_p0.py DECOMPOSE_CACHE.pkl
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

from measure import DRAWS, FLOOR, LABELS, SEED, SPLIT, once_per_station_day, quantiles, wmean_ci  # noqa: E402


def per_row(r):
    keys = list(r["keys"])
    p = np.array([float(v) for v in r["p"]])
    m = np.array([float(v) for v in r["m"]])
    if p.sum() <= 0:
        return None
    floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, p / p.sum())})
    p = np.array([floored[k] for k in keys])
    m = m / m.sum()
    y = int(r["hit"])
    onehot = np.zeros_like(m)
    onehot[y] = 1.0
    s = np.log(p / m)
    dS = float(math.log(p[y] / m[y]))
    D = float(np.sum(m * np.log(m / p)))
    profit = float(s @ (onehot - m))
    assert abs(profit - (dS + D)) < 1e-9
    at_floor = p <= FLOOR * 1.0001
    return {"station": r["station"], "date": r["date"], "at": r["at"], "bin": int(r["bin"]),
            "dS": dS, "D": D, "profit": profit, "profit_brier": float((2.0 * (p - m)) @ (onehot - m)),
            "gross": float(np.abs(s).sum()), "max_leg": float(np.abs(s).max()),
            "gross_floor": float(np.abs(s[at_floor]).sum()), "n_floor": int(at_floor.sum()),
            "truth_floored": bool(at_floor[y]), "p_y": float(p[y]), "m_y": float(m[y])}


def block(items, seed):
    rng = np.random.default_rng(seed)
    return {
        "decisions": len(items), "station_days": len({(d["station"], d["date"]) for d in items}),
        "days": len({d["date"] for d in items}),
        "score_gap_dS": wmean_ci(items, "dS", rng), "divergence_D": wmean_ci(items, "D", rng),
        "profit_frictionless": wmean_ci(items, "profit", rng),
        "profit_brier_proper": wmean_ci(items, "profit_brier", rng),
        "gross_exposure": wmean_ci(items, "gross", rng), "max_leg": wmean_ci(items, "max_leg", rng),
        "gross_on_floored_rungs": wmean_ci(items, "gross_floor", rng),
        "floored_rungs_mean": wmean_ci(items, "n_floor", rng),
        "truth_on_floored_rung_share": wmean_ci(items, "truth_floored", rng),
        "profit_quantiles": quantiles(items, "profit"),
    }


def main():
    cache = pickle.loads(Path(sys.argv[1]).read_bytes())
    rows = [r for r in cache[2] if r.get("reading") == "today_p0"]
    recs = [d for d in (per_row(r) for r in rows) if d is not None]
    results = {"decisions": len(recs), "dates": [min(d["date"] for d in recs), max(d["date"] for d in recs)],
               "by_bin_all": {}, "by_bin_all_since_0914": {}, "by_bin_all_since_0815": {}}
    for index, label in enumerate(LABELS):
        items = [d for d in recs if d["bin"] == index]
        if not items:
            continue
        results["by_bin_all"][label] = block(once_per_station_day(items), SEED)
        results["by_bin_all_since_0815"][label] = block(once_per_station_day([d for d in items if d["date"] >= "2026-08-15"]), SEED + 1)
        results["by_bin_all_since_0914"][label] = block(once_per_station_day([d for d in items if d["date"] >= SPLIT]), SEED + 2)
    opening = [d for d in recs if 2 <= d["bin"] <= 4]
    results["opening_24_60h"] = block(once_per_station_day(opening), SEED + 3)
    results["opening_24_60h_since_0914"] = block(once_per_station_day([d for d in opening if d["date"] >= SPLIT]), SEED + 4)
    (HERE / "results_today_p0.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))

    def fmt(x):
        return "—" if x is None else f"{x[0]:+.3f} [{x[1]:+.3f}, {x[2]:+.3f}]"

    print("today_p0 decisions", len(recs), results["dates"])
    print("bin | sd/days | dS | D | dS+D | Brier-proper | gross | on-floor gross | truth on floor | min / p5")
    for key in ("by_bin_all", "by_bin_all_since_0914"):
        print("--", key)
        for label, b in results[key].items():
            print(f"{label} | {b['station_days']}/{b['days']} | {fmt(b['score_gap_dS'])} | {fmt(b['divergence_D'])} | "
                  f"{fmt(b['profit_frictionless'])} | {fmt(b['profit_brier_proper'])} | {b['gross_exposure'][0]:.2f} | "
                  f"{b['gross_on_floored_rungs'][0]:.2f} | {b['truth_on_floored_rung_share'][0]:.3f} | "
                  f"{b['profit_quantiles']['min']:+.2f} / {b['profit_quantiles']['p5']:+.2f}")
    for key in ("opening_24_60h", "opening_24_60h_since_0914"):
        b = results[key]
        print(key, f"| {b['station_days']}/{b['days']} | {fmt(b['score_gap_dS'])} | {fmt(b['divergence_D'])} | "
              f"{fmt(b['profit_frictionless'])} | {fmt(b['profit_brier_proper'])} | gross {b['gross_exposure'][0]:.2f} "
              f"| floor {b['gross_on_floored_rungs'][0]:.2f} | truth-on-floor {b['truth_on_floored_rung_share'][0]:.3f}")


if __name__ == "__main__":
    main()
