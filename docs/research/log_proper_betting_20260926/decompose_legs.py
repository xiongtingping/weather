#!/usr/bin/env python3
"""Reading added after the protocol's numbers were in (labelled so in REPORT.md):
where the frictionless log-proper profit comes from, leg by leg, and what the
pure-YES liquidity loss of today's P0 would be at the served population's books.

Legs: rungs where the model sits at the charter-224 floor (short legs of size
log(0.001 / q_k)), other short legs (p_k < q_k), long legs (p_k > q_k).
D = KL(q||p) is split the same way by rung.

Today's P0 has no asks in its cache; its per-rung spread (ask - mid) is taken
from the served population's decision on the same station-day nearest in time
(|dt| <= 3 h, same rungs).  No SSOT access.  Writes results_legs.json.

    python3 decompose_legs.py POPULATION.pkl DECOMPOSE_CACHE.pkl
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

from measure import FLOOR, LABELS, SEED, SPLIT, STAGES, first_decisions, once_per_station_day, wmean_ci  # noqa: E402

MAX_DT_H = 3.0


def _instant(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def legs(p, m, y):
    onehot = np.zeros_like(m)
    onehot[y] = 1.0
    s = np.log(p / m)
    pay = onehot - m
    floored = p <= FLOOR * 1.0001
    short = (s < 0) & ~floored
    long_ = s > 0
    d = m * np.log(m / p)
    return {
        "profit": float(s @ pay),
        "profit_floor_legs": float(s[floored] @ pay[floored]),
        "profit_short_legs": float(s[short] @ pay[short]),
        "profit_long_legs": float(s[long_] @ pay[long_]),
        "D": float(d.sum()), "D_floor": float(d[floored].sum()), "D_other": float(d[~floored].sum()),
        "shift": float(-s.min()), "shift_on_floor": bool(floored[int(np.argmin(s))]),
        "m_on_floor": float(m[floored].sum()), "hit_floor": float(onehot[floored].sum()),
        "units_floor": float(-s[floored].sum()), "units_short": float(-s[short].sum()), "units_long": float(s[long_].sum()),
    }


def block(items, seed):
    rng = np.random.default_rng(seed)
    out = {"decisions": len(items), "station_days": len({(d["station"], d["date"]) for d in items})}
    for f in ("profit", "profit_floor_legs", "profit_short_legs", "profit_long_legs", "D", "D_floor", "D_other",
              "shift", "shift_on_floor", "m_on_floor", "hit_floor", "units_floor", "units_short", "units_long",
              "L_proxy", "profit_yes_proxy"):
        out[f] = wmean_ci(items, f, rng)
    return out


def main():
    served = pickle.loads(Path(sys.argv[1]).read_bytes())
    recs = []
    spread_index = defaultdict(list)
    for r in served:
        d = legs(r["p"], r["m"], int(r["hit"]))
        d.update({"stage": r["stage"], "station": r["station"], "date": r["date"], "at": r["at"], "bin": int(r["bin"])})
        recs.append(d)
        spread_index[(r["station"], r["date"])].append((_instant(r["at"]), tuple(r["keys"]), r["ask"] - r["m"]))
    results = {"served_by_stage_first": {}, "served_by_bin_all": {}}
    for stage in STAGES:
        results["served_by_stage_first"][stage] = block(first_decisions([d for d in recs if d["stage"] == stage]), SEED)
    for index, label in enumerate(LABELS):
        results["served_by_bin_all"][label] = block(once_per_station_day([d for d in recs if d["bin"] == index]), SEED + 1)
    del served, recs

    cache = pickle.loads(Path(sys.argv[2]).read_bytes())
    today = []
    unmatched = 0
    for r in cache[2]:
        if r.get("reading") != "today_p0":
            continue
        keys = list(r["keys"])
        p = np.array([float(v) for v in r["p"]])
        m = np.array([float(v) for v in r["m"]])
        if p.sum() <= 0:
            continue
        floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, p / p.sum())})
        p = np.array([floored[k] for k in keys])
        m = m / m.sum()
        d = legs(p, m, int(r["hit"]))
        d.update({"station": r["station"], "date": r["date"], "at": r["at"], "bin": int(r["bin"])})
        when = _instant(r["at"])
        best = None
        for at, ks, spread in spread_index.get((r["station"], r["date"]), ()):
            dt = abs((at - when).total_seconds()) / 3600.0
            if ks == tuple(keys) and dt <= MAX_DT_H and (best is None or dt < best[0]):
                best = (dt, spread)
        if best is not None and np.all(np.isfinite(best[1])):
            s = np.log(p / m)
            sp = s - s.min()
            d["L_proxy"] = float(np.sum(sp * best[1]))
            d["profit_yes_proxy"] = d["profit"] - d["L_proxy"]
        else:
            unmatched += 1
        today.append(d)
    del cache
    results["today_p0_unmatched_books"] = unmatched
    results["today_p0_by_bin_all"] = {}
    for index, label in enumerate(LABELS):
        items = [d for d in today if d["bin"] == index]
        if items:
            results["today_p0_by_bin_all"][label] = block(once_per_station_day(items), SEED + 2)
    opening = [d for d in today if 2 <= d["bin"] <= 4]
    results["today_p0_opening_24_60h"] = block(once_per_station_day(opening), SEED + 3)
    results["today_p0_opening_24_60h_since_0914"] = block(once_per_station_day([d for d in opening if d["date"] >= SPLIT]), SEED + 4)
    results["today_p0_opening_24_60h_matched_only"] = block(once_per_station_day([d for d in opening if "L_proxy" in d]), SEED + 5)
    (HERE / "results_legs.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))

    def fmt(x):
        return "—" if x is None else f"{x[0]:+.3f} [{x[1]:+.3f}, {x[2]:+.3f}]"

    for key in ("served_by_stage_first", "served_by_bin_all", "today_p0_by_bin_all"):
        print("--", key)
        print("k | sd | profit | floor legs | short legs | long legs | D | D floor | D other | shift | shift on floor | m on floor | hit floor | units floor/short/long | L proxy | yes proxy")
        for k, b in results[key].items():
            print(f"{k} | {b['station_days']} | {fmt(b['profit'])} | {fmt(b['profit_floor_legs'])} | {fmt(b['profit_short_legs'])} | "
                  f"{fmt(b['profit_long_legs'])} | {b['D'][0]:+.3f} | {b['D_floor'][0]:+.3f} | {b['D_other'][0]:+.3f} | "
                  f"{b['shift'][0]:.2f} | {b['shift_on_floor'][0]:.2f} | {b['m_on_floor'][0]:.3f} | {b['hit_floor'][0]:.3f} | "
                  f"{b['units_floor'][0]:.2f}/{b['units_short'][0]:.2f}/{b['units_long'][0]:.2f} | {fmt(b['L_proxy'])} | {fmt(b['profit_yes_proxy'])}")
    for key in ("today_p0_opening_24_60h", "today_p0_opening_24_60h_since_0914", "today_p0_opening_24_60h_matched_only"):
        b = results[key]
        print(key, b["station_days"], "profit", fmt(b["profit"]), "floor", fmt(b["profit_floor_legs"]), "short", fmt(b["profit_short_legs"]),
              "long", fmt(b["profit_long_legs"]), "D", b["D"][0], "D_floor", b["D_floor"][0], "shift", b["shift"][0],
              "L_proxy", fmt(b["L_proxy"]), "yes_proxy", fmt(b["profit_yes_proxy"]))
    print("unmatched books", unmatched)


if __name__ == "__main__":
    main()
