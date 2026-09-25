#!/usr/bin/env python3
"""PROTOCOL.md: the three quantities of log-score proper betting (arXiv 2607.06166)
on production's L4 population.

    s_k = log(p_k / q_k);   profit = s . (1_y - q) = log(p_y / q_y) + KL(q || p)

Reads the population cache written by posterior_forward_and_edge_20260926
(``forward_edge.load_population``: p floored on the contract rungs, q the
normalised mid, per-rung best ask recovered through mid_sum, inf on bid-only
rungs).  No SSOT access.  Writes results.json beside this file.

    python3 measure.py POPULATION.pkl
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
LABELS = ["0-12h", "12-24h", "24-36h", "36-48h", "48-60h", "60h+"]
STAGES = ("P0", "P0-M", "P1")
DRAWS = 2000
SEED = 20260926
SPLIT = "2026-09-14"
FLOOR = 1.0e-3


def per_row(r):
    p, m, y, ask = r["p"], r["m"], int(r["hit"]), r["ask"]
    onehot = np.zeros_like(m)
    onehot[y] = 1.0
    s = np.log(p / m)
    dS = float(math.log(p[y] / m[y]))
    D = float(np.sum(m * np.log(m / p)))
    profit = float(s @ (onehot - m))
    assert abs(profit - (dS + D)) < 1e-9, (profit, dS + D)
    # pure-YES shift (charter: no BUY NO): s' = s - min s >= 0, bought at the ask
    sp = s - s.min()
    finite = np.isfinite(ask)
    executable = bool(np.all(finite | (sp <= 1e-12)))
    cost_yes = float(np.sum(sp[finite] * ask[finite]))
    profit_yes = float(sp[y] - cost_yes) if executable else None
    # paper 3.3.4 spread version, YES legs only: buy log(p_k/ask_k) where p_k > ask_k
    legs = [(k, math.log(p[k] / ask[k])) for k in range(len(p)) if finite[k] and p[k] > ask[k]]
    profit_sp = float(sum(sz * ((1.0 if k == y else 0.0) - ask[k]) for k, sz in legs))
    cost_sp = float(sum(sz * ask[k] for k, sz in legs))
    # Brier-proper comparison (same signs, linear size)
    profit_brier = float((2.0 * (p - m)) @ (onehot - m))
    floored = p <= FLOOR * 1.0001
    return {
        "dS": dS, "D": D, "profit": profit, "profit_yes": profit_yes, "cost_yes": cost_yes,
        "executable": executable, "profit_sp": profit_sp, "cost_sp": cost_sp, "legs_sp": len(legs),
        "profit_brier": profit_brier, "gross": float(np.abs(s).sum()), "max_leg": float(np.abs(s).max()),
        "long": float(s[s > 0].sum()), "short": float(-s[s < 0].sum()),
        "gross_floor": float(np.abs(s[floored]).sum()), "n_floor": int(floored.sum()),
        "truth_floored": bool(floored[y]), "s_y": float(s[y]), "p_y": float(p[y]), "m_y": float(m[y]),
        "sum_ask": float(ask[finite].sum()) if executable else None,
    }


def wmean_ci(items, field, rng, weight_key="w"):
    """Weighted mean of a per-row field with an event-day bootstrap interval."""

    use = [d for d in items if d.get(field) is not None]
    if not use:
        return None
    days = sorted({d["date"] for d in use})
    index = {day: i for i, day in enumerate(days)}
    di = np.array([index[d["date"]] for d in use])
    w = np.array([d[weight_key] for d in use])
    v = np.array([float(d[field]) for d in use])
    s = np.bincount(di, weights=w * v, minlength=len(days))
    t = np.bincount(di, weights=w, minlength=len(days))
    boots = []
    for _ in range(DRAWS):
        pick = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
        boots.append(float(pick @ s / (pick @ t)))
    return [float(s.sum() / t.sum()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]


def ratio(items, num, den):
    use = [d for d in items if d.get(num) is not None and d.get(den) is not None]
    a = sum(d["w"] * d[num] for d in use)
    b = sum(d["w"] * d[den] for d in use)
    return float(a / b) if b else None


def quantiles(items, field):
    v = np.array([float(d[field]) for d in items if d.get(field) is not None])
    if not len(v):
        return None
    return {"min": float(v.min()), "p5": float(np.percentile(v, 5)), "median": float(np.median(v)),
            "p95": float(np.percentile(v, 95)), "max": float(v.max())}


def first_decisions(items):
    best = {}
    for d in items:
        key = (d["station"], d["date"])
        if key not in best or d["at"] < best[key]["at"]:
            best[key] = d
    return [dict(d, w=1.0) for d in best.values()]


def once_per_station_day(items):
    count = defaultdict(int)
    for d in items:
        count[(d["station"], d["date"])] += 1
    return [dict(d, w=1.0 / count[(d["station"], d["date"])]) for d in items]


def block(items, seed):
    rng = np.random.default_rng(seed)
    exe = [d for d in items if d["executable"]]
    out = {
        "decisions": len(items), "station_days": len({(d["station"], d["date"]) for d in items}),
        "days": len({d["date"] for d in items}),
        "score_gap_dS": wmean_ci(items, "dS", rng),
        "divergence_D": wmean_ci(items, "D", rng),
        "profit_frictionless": wmean_ci(items, "profit", rng),
        "profit_brier_proper": wmean_ci(items, "profit_brier", rng),
        "executable_decisions": len(exe),
        "profit_pure_yes_at_ask": wmean_ci(exe, "profit_yes", rng),
        "profit_frictionless_on_executable": wmean_ci(exe, "profit", rng),
        "cost_pure_yes_per_decision": wmean_ci(exe, "cost_yes", rng),
        "roi_pure_yes": ratio(exe, "profit_yes", "cost_yes"),
        "sum_ask_mean": wmean_ci(exe, "sum_ask", rng),
        "profit_spread_yes_legs": wmean_ci(items, "profit_sp", rng),
        "cost_spread_yes_legs": wmean_ci(items, "cost_sp", rng),
        "roi_spread_yes_legs": ratio(items, "profit_sp", "cost_sp"),
        "legs_spread_mean": wmean_ci(items, "legs_sp", rng),
        "gross_exposure": wmean_ci(items, "gross", rng),
        "max_leg": wmean_ci(items, "max_leg", rng),
        "long_units": wmean_ci(items, "long", rng),
        "short_units": wmean_ci(items, "short", rng),
        "gross_on_floored_rungs": wmean_ci(items, "gross_floor", rng),
        "floored_rungs_mean": wmean_ci(items, "n_floor", rng),
        "truth_on_floored_rung_share": wmean_ci(items, "truth_floored", rng),
        "profit_quantiles": quantiles(items, "profit"),
        "profit_pure_yes_quantiles": quantiles(exe, "profit_yes"),
    }
    if out["profit_frictionless_on_executable"] and out["profit_pure_yes_at_ask"]:
        out["liquidity_loss_L"] = out["profit_frictionless_on_executable"][0] - out["profit_pure_yes_at_ask"][0]
    return out


def momentum(rows, seed):
    """Paper 3.4 B: does the market move toward the model?  s_t . (q_{t+1} - q_t) over
    consecutive decisions of the same station-day, station-days weighted equally."""

    rng = np.random.default_rng(seed)
    by_sd = defaultdict(list)
    for r in rows:
        by_sd[(r["station"], r["date"])].append(r)
    pairs = []
    for (station, day), items in by_sd.items():
        items.sort(key=lambda r: r["at"])
        steps = []
        for a, b in zip(items, items[1:]):
            if a["keys"] != b["keys"]:
                continue
            s = np.log(a["p"] / a["m"])
            onehot = np.zeros_like(a["m"])
            onehot[a["hit"]] = 1.0
            steps.append({
                "station": station, "date": day, "bin": a["bin"],
                "toward_model": float(s @ (b["m"] - a["m"])),
                "market_toward_truth": float((onehot - a["m"]) @ (b["m"] - a["m"])),
                "gap_next_minus_now": float(np.sum(np.abs(b["m"] - a["p"])) - np.sum(np.abs(a["m"] - a["p"]))),
            })
        for st in steps:
            st["w"] = 1.0 / len(steps)
        pairs.extend(steps)
    out = {"pairs": len(pairs), "station_days": len({(d["station"], d["date"]) for d in pairs}),
           "toward_model": wmean_ci(pairs, "toward_model", rng),
           "market_toward_truth": wmean_ci(pairs, "market_toward_truth", rng),
           "l1_gap_change": wmean_ci(pairs, "gap_next_minus_now", rng),
           "share_toward_model": wmean_ci([dict(d, pos=float(d["toward_model"] > 0)) for d in pairs], "pos", rng),
           "by_bin": {}}
    for index, label in enumerate(LABELS):
        sub = [d for d in pairs if d["bin"] == index]
        if sub:
            out["by_bin"][label] = {"pairs": len(sub), "toward_model": wmean_ci(sub, "toward_model", rng),
                                    "share_toward_model": wmean_ci([dict(d, pos=float(d["toward_model"] > 0)) for d in sub], "pos", rng)}
    return out


def worst(items, n=12):
    ranked = sorted(items, key=lambda d: d["profit"])[:n]
    return [{k: d[k] for k in ("stage", "station", "date", "hours", "profit", "dS", "D", "p_y", "m_y", "truth_floored")} for d in ranked]


def main():
    rows = pickle.loads(Path(sys.argv[1]).read_bytes())
    recs = []
    for r in rows:
        d = per_row(r)
        d.update({"stage": r["stage"], "station": r["station"], "date": r["date"], "at": r["at"],
                  "bin": int(r["bin"]), "hours": float(r["hours"])})
        recs.append(d)
    identity_max = max(abs(d["profit"] - (d["dS"] + d["D"])) for d in recs)
    results = {"decisions": len(recs), "identity_max_abs_error": identity_max, "by_stage_first": {},
               "by_stage_all": {}, "by_bin_all": {}, "by_stage_first_since_0914": {}, "by_bin_all_since_0914": {}}
    for stage in STAGES:
        items = [d for d in recs if d["stage"] == stage]
        results["by_stage_first"][stage] = block(first_decisions(items), SEED)
        results["by_stage_all"][stage] = block(once_per_station_day(items), SEED + 1)
        results["by_stage_first_since_0914"][stage] = block(first_decisions([d for d in items if d["date"] >= SPLIT]), SEED + 2)
    for index, label in enumerate(LABELS):
        items = [d for d in recs if d["bin"] == index]
        results["by_bin_all"][label] = block(once_per_station_day(items), SEED + 3)
        results["by_bin_all_since_0914"][label] = block(once_per_station_day([d for d in items if d["date"] >= SPLIT]), SEED + 4)
    results["all_first_by_stage_pooled"] = block(
        [d for stage in STAGES for d in first_decisions([x for x in recs if x["stage"] == stage])], SEED + 5)
    results["momentum"] = momentum(rows, SEED + 6)
    results["worst_events_first_decisions"] = worst(
        [d for stage in STAGES for d in first_decisions([x for x in recs if x["stage"] == stage])])
    (HERE / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))

    def fmt(x):
        return "—" if x is None else f"{x[0]:+.3f} [{x[1]:+.3f}, {x[2]:+.3f}]"

    print("identity max abs error", identity_max)
    print("\n== by stage, first decision per station-day ==")
    print("stage | sd/days | dS | D | dS+D | pure-YES@ask (exec n) | L | ROI yes | spread-YES legs | Brier-proper | gross | on-floor gross | min profit / p5")
    for key in ("by_stage_first", "by_stage_first_since_0914"):
        print("--", key)
        for stage, b in results[key].items():
            print(f"{stage} | {b['station_days']}/{b['days']} | {fmt(b['score_gap_dS'])} | {fmt(b['divergence_D'])} | "
                  f"{fmt(b['profit_frictionless'])} | {fmt(b['profit_pure_yes_at_ask'])} ({b['executable_decisions']}) | "
                  f"{b.get('liquidity_loss_L', float('nan')):+.3f} | {b['roi_pure_yes'] if b['roi_pure_yes'] is None else round(b['roi_pure_yes'], 3)} | "
                  f"{fmt(b['profit_spread_yes_legs'])} roi {None if b['roi_spread_yes_legs'] is None else round(b['roi_spread_yes_legs'], 3)} | "
                  f"{fmt(b['profit_brier_proper'])} | {b['gross_exposure'][0]:.2f} | {b['gross_on_floored_rungs'][0]:.2f} | "
                  f"{b['profit_quantiles']['min']:+.2f} / {b['profit_quantiles']['p5']:+.2f}")
    print("\n== by hours-to-settlement, all decisions, station-day weighted ==")
    for key in ("by_bin_all", "by_bin_all_since_0914"):
        print("--", key)
        for label, b in results[key].items():
            print(f"{label} | {b['station_days']}/{b['days']} | {fmt(b['score_gap_dS'])} | {fmt(b['divergence_D'])} | "
                  f"{fmt(b['profit_frictionless'])} | {fmt(b['profit_pure_yes_at_ask'])} ({b['executable_decisions']}) | "
                  f"{b.get('liquidity_loss_L', float('nan')):+.3f} | {b['roi_pure_yes'] if b['roi_pure_yes'] is None else round(b['roi_pure_yes'], 3)} | "
                  f"{fmt(b['profit_spread_yes_legs'])} | {fmt(b['profit_brier_proper'])} | {b['gross_exposure'][0]:.2f} | {b['gross_on_floored_rungs'][0]:.2f}")
    print("\n== momentum ==")
    print(json.dumps(results["momentum"], indent=1)[:3000])
    print("\n== worst events ==")
    for e in results["worst_events_first_decisions"]:
        print(e)


if __name__ == "__main__":
    main()
