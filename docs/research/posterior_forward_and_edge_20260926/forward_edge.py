#!/usr/bin/env python3
"""PROTOCOL.md: walk-forward L4 beliefs by hours to settlement, and their edge at the real ask.

Population: production's L4 population (``valuation_store.population``), with
each bucket's best ask recovered from the same WS snapshot through the
``mid_sum`` that production's ``market_reference_v1`` computes (captured
beside the call, nothing restated).  Fits: production's ``posterior_store``
design and penalised Newton; beliefs: production's ``posterior_vector_v1``.
Read-only; writes results.json.

    python3 forward_edge.py [population_cache.pkl]
"""
from __future__ import annotations

import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import posterior_store as PS  # noqa: E402
from bayes_basket_target import valuation_store  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import ECONOMIC_HURDLE_V1  # noqa: E402
from bayes_basket_target.basket_kernel.posterior_v1 import posterior_vector_v1  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

BINS = ((0, 12), (12, 24), (24, 36), (36, 48), (48, 60), (60, math.inf))
LABELS = ["0-12h", "12-24h", "24-36h", "36-48h", "48-60h", "60h+"]
MIN_TRAIN_DAYS = 10
DRAWS = 2000
SEED = 20260926
MID_SUM_KEY = "\x00mid_sum"
RULES = {
    "hurdle_5pct": lambda b, a: b >= (1.0 + ECONOMIC_HURDLE_V1) * a,
    "edge_gt_0": lambda b, a: b - a > 0.0,
    "edge_gt_2pt": lambda b, a: b - a > 0.02,
}
ARMS = ("market", "gamma", "full", "model")


def _instant(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def load_population(conn):
    original = valuation_store.market_reference_v1

    def captured(book):
        out = original(book)
        if out.get("state") == "complete":
            out = dict(out)
            spread = dict(out.get("half_spread") or {})
            spread[MID_SUM_KEY] = float(out["mid_sum"])
            out["half_spread"] = spread
        return out

    valuation_store.market_reference_v1 = captured
    rows = []
    try:
        for stage in ("P0", "P0-M", "P1"):
            population, _meta = valuation_store.population(conn, stage_scope=stage, anchored=False, window_days=70)
            for row in population:
                hours = hours_to_settlement_if_known(row["date"], as_of_utc=row["at"],
                                                     settlement_timezone=row.get("settlement_timezone"))
                index = next((i for i, (lo, hi) in enumerate(BINS) if hours is not None and lo <= hours < hi), None)
                if index is None:
                    continue
                keys = list(row["m"])
                m = np.array([float(row["m"][k]) for k in keys])
                p = np.array([float(row["p"].get(k, 0.0) or 0.0) for k in keys])
                if p.sum() <= 0:
                    continue
                floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, p / p.sum())})
                p = np.array([floored[k] for k in keys])
                spread = row["half_spread"]
                mid_sum = float(spread[MID_SUM_KEY])
                ask = np.array([(float(row["m"][k]) + float(spread.get(k, 0.0))) * mid_sum for k in keys])
                ask = np.where(ask >= 1.0 - 1e-9, np.inf, ask)          # bid-only rung: nothing to buy
                rows.append({"stage": stage, "station": row["station"], "date": row["date"],
                             "at": row["at"], "label_known_at": row["label_known_at"], "bin": index,
                             "hours": float(hours), "keys": keys, "m": m, "p": p, "ask": ask,
                             "hit": keys.index(row["label"])})
    finally:
        valuation_store.market_reference_v1 = original
    return rows


def _as_ps_row(r, weight):
    return {"label": str(r["hit"]), "date": r["date"], "weight": weight,
            "m": {str(i): float(v) for i, v in enumerate(r["m"])},
            "p": {str(i): float(v) for i, v in enumerate(r["p"])}}


def _belief(r, theta):
    vector = posterior_vector_v1(
        {str(i): float(v) for i, v in enumerate(r["p"])},
        {str(i): float(v) for i, v in enumerate(r["m"])},
        {"alpha": float(theta[0]), "gamma": float(theta[1]), "w": float(theta[2])})
    return np.array([vector[str(i)] for i in range(len(r["m"]))])


def walk_forward(rows):
    """Per bin, per test day: fit on labels knowable before the day's first decision."""

    tested = []
    for index in range(len(BINS)):
        in_bin = [r for r in rows if r["bin"] == index]
        count = defaultdict(int)
        for r in in_bin:
            count[(r["station"], r["date"])] += 1
        weight = {id(r): 1.0 / count[(r["station"], r["date"])] for r in in_bin}
        by_day = defaultdict(list)
        for r in in_bin:
            by_day[r["date"]].append(r)
        for day in sorted(by_day):
            first = min(_instant(r["at"]) for r in by_day[day])
            train = [r for r in in_bin if _instant(r["label_known_at"]) <= first]
            if len({r["date"] for r in train}) < MIN_TRAIN_DAYS:
                continue
            design = PS._design([_as_ps_row(r, weight[id(r)]) for r in train])
            full = PS._fit_logistic(design, row_weight=1.0, prior_mean=(0.0, 0.0, 0.0))
            gamma = PS._fit_logistic(design, row_weight=1.0, prior_mean=(0.0, 0.0, 0.0),
                                     prior_sd=(PS.PRIOR_SD[0], PS.PRIOR_SD[1], 1e-6))
            for r in by_day[day]:
                tested.append({**r, "weight": weight[id(r)], "theta_full": full.tolist(), "theta_gamma": gamma.tolist(),
                               "train_days": len({t["date"] for t in train}),
                               "b": {"market": r["m"], "gamma": _belief(r, gamma), "full": _belief(r, full), "model": r["p"]}})
        print(LABELS[index], "tested", sum(1 for t in tested if t["bin"] == index), flush=True)
    return tested


def scores(r, arm):
    b, h = r["b"][arm], r["hit"]
    y = np.zeros_like(b)
    y[h] = 1.0
    return -math.log(max(float(b[h]), 1e-12)), float(np.sum((b - y) ** 2))


def trades(r, arm, rule):
    """(pnl, shares, cost) of buying one share of every rung the rule admits."""

    b, ask = r["b"][arm], r["ask"]
    picked = [i for i in range(len(b)) if np.isfinite(ask[i]) and RULES[rule](float(b[i]), float(ask[i]))]
    pnl = sum((1.0 if i == r["hit"] else 0.0) - float(ask[i]) for i in picked)
    return pnl, len(picked), sum(float(ask[i]) for i in picked)


def day_bootstrap(per_day_num, per_day_den, days, rng):
    draws = []
    nd = len(days)
    for _ in range(DRAWS):
        pick = rng.integers(0, nd, nd)
        den = per_day_den[pick].sum()
        draws.append(per_day_num[pick].sum() / den if den > 0 else 0.0)
    return float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def compare_scores(items, base, test, rng):
    days = sorted({r["date"] for r in items})
    di = {d: i for i, d in enumerate(days)}
    num = np.zeros((len(days), 2))
    den = np.zeros(len(days))
    for r in items:
        a, b = scores(r, test), scores(r, base)
        num[di[r["date"]]] += r["weight"] * (np.array(a) - np.array(b))
        den[di[r["date"]]] += r["weight"]
    out = {"station_days": len({(r["station"], r["date"]) for r in items}), "days": len(days)}
    for j, name in enumerate(("logscore", "brier")):
        mean = float(num[:, j].sum() / den.sum())
        lo, hi = day_bootstrap(num[:, j], den, days, rng)
        out[name] = [mean, lo, hi]
    out["both_lower"] = out["logscore"][2] < 0 and out["brier"][2] < 0
    return out


def edge_reading(items, arm, rule, rng):
    """Per share realised edge, station-days weighted equally, day-bootstrap interval."""

    by_sd = defaultdict(lambda: [0.0, 0, 0.0])
    for r in items:
        pnl, shares, cost = trades(r, arm, rule)
        entry = by_sd[(r["station"], r["date"])]
        entry[0] += pnl
        entry[1] += shares
        entry[2] += cost
    traded = {k: v for k, v in by_sd.items() if v[1] > 0}
    if not traded:
        return {"station_days_traded": 0, "shares": 0}
    days = sorted({d for _s, d in traded})
    di = {d: i for i, d in enumerate(days)}
    num = np.zeros(len(days))
    den = np.zeros(len(days))
    for (station, day), (pnl, shares, cost) in traded.items():
        num[di[day]] += pnl / shares
        den[di[day]] += 1.0
    lo, hi = day_bootstrap(num, den, days, rng)
    total_pnl = sum(v[0] for v in traded.values())
    total_cost = sum(v[2] for v in traded.values())
    return {"station_days_traded": len(traded), "station_days_seen": len(by_sd), "days": len(days),
            "shares": int(sum(v[1] for v in traded.values())),
            "edge_per_share": float(num.sum() / den.sum()), "ci": [lo, hi],
            "roi": total_pnl / total_cost if total_cost > 0 else None, "pnl": total_pnl, "cost": total_cost}


def main():
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if cache is not None and cache.exists():
        rows = pickle.loads(cache.read_bytes())
    else:
        conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
        conn.execute("PRAGMA query_only=ON")
        rows = load_population(conn)
        conn.close()
        if cache is not None:
            cache.write_bytes(pickle.dumps(rows))
    print("population", len(rows), flush=True)
    tested = walk_forward(rows)
    rng = np.random.default_rng(SEED)
    result = {"protocol": "PROTOCOL.md", "population": len(rows), "tested": len(tested), "A": {}, "B": {}}
    for index, name in enumerate(LABELS):
        items = [r for r in tested if r["bin"] == index]
        if not items:
            continue
        thetas = np.array([r["theta_full"] for r in items])
        result["A"][name] = {
            "tested_decisions": len(items),
            "theta_full_median": np.median(thetas, axis=0).tolist(),
            "gamma_vs_market": compare_scores(items, "market", "gamma", rng),
            "full_vs_market": compare_scores(items, "market", "full", rng),
            "full_vs_gamma": compare_scores(items, "gamma", "full", rng),
            "model_vs_market": compare_scores(items, "market", "model", rng),
        }
    q1_all = [r for r in tested if r["bin"] in (2, 3, 4, 5)]
    first = {}
    for r in q1_all:
        key = (r["station"], r["date"])
        if key not in first or _instant(r["at"]) < _instant(first[key]["at"]):
            first[key] = r
    q1_first = list(first.values())
    q2 = [r for r in tested if r["bin"] == 0]
    q2b = [r for r in tested if r["bin"] == 1]
    for label, items in (("Q1_24_60h_first_decision", q1_first), ("Q1_24_60h_all_decisions", q1_all),
                         ("Q2_0_12h_all_decisions", q2), ("Q2b_12_24h_all_decisions", q2b)):
        result["B"][label] = {arm: {rule: edge_reading(items, arm, rule, rng) for rule in RULES} for arm in ARMS}
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for name, a in result["A"].items():
        print(name, a["tested_decisions"], "theta(full) median", [round(x, 3) for x in a["theta_full_median"]])
        for key in ("gamma_vs_market", "full_vs_market", "full_vs_gamma", "model_vs_market"):
            c = a[key]
            print(f"   {key}: LS {c['logscore'][0]:+.4f} [{c['logscore'][1]:+.4f},{c['logscore'][2]:+.4f}]"
                  f" Br {c['brier'][0]:+.4f} [{c['brier'][1]:+.4f},{c['brier'][2]:+.4f}] both lower={c['both_lower']}")
    for label, arms in result["B"].items():
        print("==", label)
        for arm, rules in arms.items():
            for rule, e in rules.items():
                if not e.get("shares"):
                    print(f"   {arm:6s} {rule:12s} no trades")
                    continue
                print(f"   {arm:6s} {rule:12s} sd {e['station_days_traded']}/{e['station_days_seen']} shares {e['shares']}"
                      f" edge/share {e['edge_per_share']:+.4f} [{e['ci'][0]:+.4f},{e['ci'][1]:+.4f}] ROI {e['roi']:+.3f}")


if __name__ == "__main__":
    main()
