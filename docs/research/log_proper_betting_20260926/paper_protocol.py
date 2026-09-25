#!/usr/bin/env python3
"""PROTOCOL.md, second round: the paper's own empirical protocol (arXiv 2607.06166
Sections 4, C.1, C.2, D.2, D.7) on the replay book.

Each bucket is its own binary market.  Bid-ask band: buy YES when p > ask, buy NO
when p < bid, nothing in between (C.1).  Sizes per D.2 / Table 1: Brier |p - q~|,
Log |log p - log q~| (D.2 wording), plus the strict binary reduction of C.1
|logit p - logit q~|; q~ is the YES-equivalent executed price (ask for YES, bid
for NO).  Fills walk six levels, partial fills count as filled (D.7).  ROI = sum
of P&L / sum of cost staked (Table 3), with dS and D on the same share scale so
dS + D = P&L per bet (exact for Brier and logit).

Offline (4.1): one forecast per market, per grid hour.  Fundamental (Cor. 4):
every grid hour bets, all held to settlement.  Momentum (Cor. 5 / D.7): rebalance
each bucket to the new target every grid hour, buys at the ask, sells at the bid,
hold after the last grid hour (H=4) to settlement; variant skipping buckets whose
price moved < 10c since the last fill.  YES-only versions beside every reading.
No SSOT access.  Writes results_paper_protocol.json.

    PYTHONPATH=<repo> python3 paper_protocol.py DATASET.jsonl.gz OVERRIDES.json [--vector root|served] [--floor log_only|always]
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

GRID = (44, 36, 30, 24, 18, 14, 10, 7, 4)
DRAWS = 2000
SEED = 20260926
RULES = ("brier", "log_d2", "logit_c1")
MARGIN_BINS = ((0.0, 0.05), (0.05, 0.15), (0.15, 0.25), (0.25, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01))
EPS = 1e-9


def logit(x):
    x = min(max(x, EPS), 1.0 - EPS)
    return math.log(x / (1.0 - x))


def size(rule, p, q):
    if rule == "brier":
        return abs(p - q)
    if rule == "log_d2":
        return abs(math.log(p) - math.log(q))
    return abs(logit(p) - logit(q))


def score_terms(rule, p, q, y, w):
    """(dS, D) on the share scale of w so that dS + D = w * (y - q) for a YES-side bet.
    Exact for Brier (s = 4(p-q), profit = (dS+D)/4 per |p-q| share) and logit; for
    the D.2 log wording only the YES coordinate's terms are returned."""

    if rule == "brier":
        dS = -2.0 * (y - p) ** 2 + 2.0 * (y - q) ** 2
        D = 2.0 * (q - p) ** 2
        return dS / 4.0, D / 4.0
    if rule == "logit_c1":
        dS = y * math.log(p / q) + (1 - y) * math.log((1 - p) / (1 - q))
        D = q * math.log(q / p) + (1 - q) * math.log((1 - q) / (1 - p))
        return dS, D
    return y * math.log(p / q), q * math.log(q / p)


def walk(ladder, want):
    cost, left = 0.0, float(want)
    for price, shares in ladder:
        take = min(left, float(shares))
        cost += take * float(price)
        left -= take
        if left <= 1e-12:
            break
    return cost, float(want) - max(left, 0.0)


def no_ladder(bids):
    return [[1.0 - float(price), float(shares)] for price, shares in bids]


def load(dataset, overrides, vector="served", floor="log_only"):
    """vector: which forecast plays the paper's p -- 'root' is the P0 root (the NWP forecast before any
    Bayesian update), 'served' the stage-priority envelope.  floor: 'log_only' floors the vector only for
    the log rules (the paper clips to [eps, 1-eps]; Brier needs no floor), 'always' floors for every rule."""

    ov = json.loads(Path(overrides).read_text())["overrides"]
    rows, skipped = [], defaultdict(int)
    with gzip.open(dataset, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("m_state") != "complete" or not r.get(vector):
                skipped["book_incomplete"] += 1
                continue
            keys = list(r["m"])
            truth = ov.get(f"{r['st']}|{r['day']}", r["truth"])
            if truth not in keys or set(keys) != set(r[vector]):
                skipped["truth_or_rungs"] += 1
                continue
            raw = np.array([float(r[vector][k]) for k in keys])
            if raw.sum() <= 0:
                skipped["zero_vector"] += 1
                continue
            floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, raw / raw.sum())})
            p_floor = [floored[k] for k in keys]
            p_raw = [float(v) for v in raw / raw.sum()]
            rows.append({
                "station": r["st"], "date": r["day"], "H": int(r["H"]), "stage": r["stage"], "keys": keys,
                "pv": {"brier": p_raw if floor == "log_only" else p_floor, "log_d2": p_floor, "logit_c1": p_floor},
                "y": keys.index(truth),
                "asks": [[[float(a), float(b)] for a, b in (r["asks"].get(k) or [])] for k in keys],
                "bids": [[[float(a), float(b)] for a, b in (r["bids"].get(k) or [])] for k in keys],
            })
    return rows, dict(skipped)


def bets(row, rule, yes_only=False):
    """The paper's one-shot bets on this row: list of dicts with side, shares, cost, pnl, dS, D."""

    out = []
    for k, p in enumerate(row["pv"][rule]):
        asks, bids = row["asks"][k], row["bids"][k]
        hit = 1.0 if k == row["y"] else 0.0
        if asks and p > asks[0][0]:
            q = asks[0][0]
            w = size(rule, p, q)
            cost, filled = walk(asks, w)
            if filled <= 0:
                continue
            dS, D = score_terms(rule, p, q, hit, w)
            scale = filled / w
            out.append({"side": "YES", "k": k, "p": p, "q": q, "margin": abs(p - q), "shares": filled, "cost": cost,
                        "pnl": filled * hit - cost, "win": hit, "dS": dS * scale, "D": D * scale, "slip": cost - filled * q})
        elif not yes_only and bids and p < bids[0][0]:
            q = bids[0][0]
            w = size(rule, p, q)
            cost, filled = walk(no_ladder(bids), w)
            if filled <= 0:
                continue
            dS, D = score_terms(rule, p, q, hit, w)
            scale = filled / w
            # NO share pays 1 - hit; the identity is written on the YES coordinate with the sign of s
            out.append({"side": "NO", "k": k, "p": p, "q": q, "margin": abs(p - q), "shares": filled, "cost": cost,
                        "pnl": filled * (1.0 - hit) - cost, "win": 1.0 - hit, "dS": dS * scale, "D": D * scale,
                        "slip": cost - filled * (1.0 - q)})
    return out


def pooled(items, rng, keys=("pnl", "cost", "dS", "D", "slip", "shares", "win")):
    """Ratio-of-sums readings with an event-day bootstrap; items carry 'date'."""

    if not items:
        return {"bets": 0}
    days = sorted({d["date"] for d in items})
    di = {d: i for i, d in enumerate(days)}
    tot = np.zeros((len(days), len(keys)))
    for d in items:
        tot[di[d["date"]]] += [float(d[k]) for k in keys]
    idx = {k: i for i, k in enumerate(keys)}
    n = np.zeros(len(days))
    for d in items:
        n[di[d["date"]]] += 1

    def reading(num, den):
        point = tot[:, idx[num]].sum() / tot[:, idx[den]].sum()
        boots = []
        for _ in range(DRAWS):
            pick = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
            boots.append(float(pick @ tot[:, idx[num]] / (pick @ tot[:, idx[den]])))
        return [float(point), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]

    win_point = tot[:, idx["win"]].sum() / n.sum()
    return {"bets": int(n.sum()), "days": len(days), "cost": float(tot[:, idx["cost"]].sum()),
            "pnl": float(tot[:, idx["pnl"]].sum()), "roi": reading("pnl", "cost"),
            "dS_roi": float(tot[:, idx["dS"]].sum() / tot[:, idx["cost"]].sum()),
            "D_roi": float(tot[:, idx["D"]].sum() / tot[:, idx["cost"]].sum()),
            "slippage_roi": float(tot[:, idx["slip"]].sum() / tot[:, idx["cost"]].sum()),
            "win_rate": float(win_point), "shares": float(tot[:, idx["shares"]].sum())}


def momentum(rows_by_sd, rule, yes_only=False, ten_cent=False):
    """D.7: rebalance every grid hour toward the new target; hold after H=4 to settlement."""

    per_day = []
    for (station, day), items in rows_by_sd.items():
        items = sorted(items, key=lambda r: -r["H"])
        keys = items[0]["keys"]
        pos = np.zeros(len(keys))          # + YES shares, - NO shares
        last_fill = [None] * len(keys)
        buys = sells = 0.0
        for r in items:
            if r["keys"] != keys:
                break
            for k, p in enumerate(r["pv"][rule]):
                asks, bids = r["asks"][k], r["bids"][k]
                ask1 = asks[0][0] if asks else None
                bid1 = bids[0][0] if bids else None
                target = 0.0
                if ask1 is not None and p > ask1:
                    target = size(rule, p, ask1)
                elif (not yes_only) and bid1 is not None and p < bid1:
                    target = -size(rule, p, bid1)
                if ten_cent and last_fill[k] is not None:
                    ref = ask1 if target > 0 else (bid1 if target < 0 else (ask1 if pos[k] > 0 else bid1))
                    if ref is not None and abs(ref - last_fill[k]) < 0.10:
                        continue
                delta = target - pos[k]
                if abs(delta) < 1e-12:
                    continue
                # unwind the part that changes direction or shrinks
                if pos[k] > 0 and delta < 0:
                    sell = min(pos[k], -delta)
                    proceeds, filled = walk(bids, sell)      # sell YES at the bid
                    sells += proceeds
                    pos[k] -= filled
                    delta += filled
                    if filled > 0:
                        last_fill[k] = proceeds / filled
                    if abs(delta) < 1e-12 or filled < sell - 1e-9:
                        continue
                elif pos[k] < 0 and delta > 0:
                    sell = min(-pos[k], delta)
                    proceeds, filled = walk([[1.0 - a, s] for a, s in asks], sell)   # sell NO at NO bid = 1 - ask
                    sells += proceeds
                    pos[k] += filled
                    delta -= filled
                    if filled > 0:
                        last_fill[k] = 1.0 - proceeds / filled
                    if abs(delta) < 1e-12 or filled < sell - 1e-9:
                        continue
                if delta > 0:
                    cost, filled = walk(asks, delta)
                    buys += cost
                    pos[k] += filled
                    if filled > 0:
                        last_fill[k] = cost / filled
                elif delta < 0:
                    cost, filled = walk(no_ladder(bids), -delta)
                    buys += cost
                    pos[k] -= filled
                    if filled > 0:
                        last_fill[k] = 1.0 - cost / filled
        y = items[0]["y"]
        payoff = float(sum(pos[k] * (1.0 if k == y else 0.0) for k in range(len(keys)) if pos[k] > 0)
                       + sum(-pos[k] * (0.0 if k == y else 1.0) for k in range(len(keys)) if pos[k] < 0))
        per_day.append({"date": day, "station": station, "pnl": sells + payoff - buys, "cost": buys,
                        "dS": 0.0, "D": 0.0, "slip": 0.0, "shares": float(np.abs(pos).sum()), "win": 0.0})
    return per_day


def main():
    vector = sys.argv[sys.argv.index("--vector") + 1] if "--vector" in sys.argv else "served"
    floor = sys.argv[sys.argv.index("--floor") + 1] if "--floor" in sys.argv else "log_only"
    rows, skipped = load(sys.argv[1], sys.argv[2], vector=vector, floor=floor)
    rng = np.random.default_rng(SEED)
    by_sd = defaultdict(list)
    for r in rows:
        by_sd[(r["station"], r["date"])].append(r)
    results = {"vector": vector, "floor": floor, "rows": len(rows), "station_days": len(by_sd), "skipped": skipped, "offline_by_H": {}, "fundamental": {},
               "momentum": {}, "persona_bins": {}, "band_share": {}}
    for rule in RULES:
        for yes_only in (False, True):
            tag = rule + ("_yes_only" if yes_only else "")
            all_bets = []
            results["offline_by_H"][tag] = {}
            for H in GRID:
                items = [dict(b, date=r["date"]) for r in rows if r["H"] == H for b in bets(r, rule, yes_only)]
                results["offline_by_H"][tag][str(H)] = pooled(items, rng)
                all_bets.extend(items)
            results["fundamental"][tag] = pooled(all_bets, rng)
            results["fundamental"][tag]["by_side"] = {
                side: pooled([b for b in all_bets if b["side"] == side], rng) for side in ("YES", "NO")}
            results["momentum"][tag] = pooled(momentum(by_sd, rule, yes_only), rng, keys=("pnl", "cost", "dS", "D", "slip", "shares", "win"))
            results["momentum"][tag + "_10c"] = pooled(momentum(by_sd, rule, yes_only, ten_cent=True), rng,
                                                       keys=("pnl", "cost", "dS", "D", "slip", "shares", "win"))
            if not yes_only:
                bins = {}
                for lo, hi in MARGIN_BINS:
                    sub = [b for b in all_bets if lo <= b["margin"] < hi]
                    bins[f"[{lo:.2f},{min(hi, 1.0):.2f})"] = {"share": len(sub) / max(len(all_bets), 1), **pooled(sub, rng)}
                results["persona_bins"][rule] = bins
    # how often the band admits anything, and on which side
    n_yes = n_no = n_band = 0
    for r in rows:
        for k, p in enumerate(r["pv"]["brier"]):
            asks, bids = r["asks"][k], r["bids"][k]
            if asks and p > asks[0][0]:
                n_yes += 1
            elif bids and p < bids[0][0]:
                n_no += 1
            else:
                n_band += 1
    results["band_share"] = {"yes": n_yes, "no": n_no, "no_trade": n_band}
    (HERE / f"results_paper_protocol_{vector}_{floor}.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))

    def fmt(x):
        return "—" if x is None else f"{100 * x[0]:+.1f}% [{100 * x[1]:+.1f}, {100 * x[2]:+.1f}]"

    print("vector", vector, "floor", floor, "rows", len(rows), "station-days", len(by_sd), "skipped", skipped, "band", results["band_share"])
    print("\n== fundamental (every grid hour, held to settlement) ==")
    print("rule | bets | cost | ROI | dS_roi | D_roi | slippage | win | YES-side ROI | NO-side ROI")
    for tag, b in results["fundamental"].items():
        print(f"{tag} | {b['bets']} | {b['cost']:.0f} | {fmt(b['roi'])} | {100 * b['dS_roi']:+.1f} | {100 * b['D_roi']:+.1f} | "
              f"{100 * b['slippage_roi']:+.1f} | {100 * b['win_rate']:.1f}% | {fmt(b['by_side']['YES'].get('roi'))} | {fmt(b['by_side']['NO'].get('roi'))}")
    print("\n== offline, one forecast per market, by grid hour ==")
    for tag, by_h in results["offline_by_H"].items():
        print(tag, " | ".join(f"H{H}: {fmt(b.get('roi'))} n={b['bets']}" for H, b in by_h.items()))
    print("\n== momentum (D.7 rebalancing) ==")
    for tag, b in results["momentum"].items():
        print(f"{tag} | station-days {b['bets']} | cost {b['cost']:.0f} | pnl {b['pnl']:+.1f} | ROI {fmt(b['roi'])}")
    print("\n== persona bins (all bets, both sides) ==")
    for rule, bins in results["persona_bins"].items():
        print(rule, " | ".join(f"{k}: share {100 * b['share']:.0f}% win {100 * b.get('win_rate', 0):.0f}% ROI {fmt(b.get('roi'))}" for k, b in bins.items()))


if __name__ == "__main__":
    main()
