#!/usr/bin/env python3
"""What would have to change for the paper's condition dS + D > L to hold here.

Levers, on the paper's per-bucket binary protocol (pure P0 root, both sides):
  A. execution price: taker (walk the ask / NO-ask, the paper's live practice),
     mid, maker ceiling (fill at the bid / NO-bid, every order filled, no adverse
     selection -- an upper bound, not a forecast)
  B. scoring rule: Brier, Log (D.2), logit (C.1), spherical (binary reduction)
  C. ex-ante bet selection, walk-forward by event day (trailing days only, >= 10
     days of history): by grid hour, by side, by margin band, by station; the
     in-sample oracle is printed beside each for contrast
  D. the accuracy the bets would need: cost-weighted hit rate on YES legs vs the
     cost-weighted price paid (break-even), same for NO legs
Writes results_levers.json.

    PYTHONPATH=<repo> python3 levers.py DATASET.jsonl.gz OVERRIDES.json [--vector root|served]
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paper_protocol import DRAWS, GRID, MARGIN_BINS, SEED, load, pooled, size, walk  # noqa: E402

RULES = ("brier", "log_d2", "logit_c1", "spherical")
MIN_TRAIN_DAYS = 10
MIN_TRAIN_BETS = 200
MIN_STATION_BETS = 40


def sph_grad(x):
    return (2.0 * x - 1.0) / math.sqrt(x * x + (1.0 - x) ** 2)


def size_any(rule, p, q):
    if rule == "spherical":
        return abs(sph_grad(p) - sph_grad(q))
    return size(rule, p, q)


def bets_exec(row, rule, mode):
    """The paper's band on the taker prices; fills at taker / mid / maker prices."""

    out = []
    pv = row["pv"]["brier"] if rule == "spherical" else row["pv"][rule]
    for k, p in enumerate(pv):
        asks, bids = row["asks"][k], row["bids"][k]
        hit = 1.0 if k == row["y"] else 0.0
        ask1 = asks[0][0] if asks else None
        bid1 = bids[0][0] if bids else None
        if ask1 is not None and p > ask1:
            side = "YES"
            if mode == "taker":
                w = size_any(rule, p, ask1)
                cost, filled = walk(asks, w)
            else:
                price = bid1 if (mode == "maker" and bid1 is not None) else ((ask1 + bid1) / 2.0 if bid1 is not None else ask1)
                if p <= price:
                    continue
                w = size_any(rule, p, price)
                cost, filled = w * price, w
            if filled <= 0:
                continue
            pnl = filled * hit - cost
        elif bid1 is not None and p < bid1:
            side = "NO"
            if mode == "taker":
                w = size_any(rule, p, bid1)
                cost, filled = walk([[1.0 - a, s] for a, s in bids], w)
            else:
                price_yes = ask1 if (mode == "maker" and ask1 is not None) else ((ask1 + bid1) / 2.0 if ask1 is not None else bid1)
                if p >= price_yes:
                    continue
                w = size_any(rule, p, price_yes)
                cost, filled = w * (1.0 - price_yes), w
            if filled <= 0:
                continue
            pnl = filled * (1.0 - hit) - cost
        else:
            continue
        out.append({"date": row["date"], "station": row["station"], "H": row["H"], "side": side, "k": k,
                    "margin": abs(p - (ask1 if side == "YES" else bid1)), "shares": filled, "cost": cost, "pnl": pnl,
                    "win": hit if side == "YES" else 1.0 - hit, "paid": cost / filled, "dS": 0.0, "D": 0.0, "slip": 0.0})
    return out


def roi(items):
    c = sum(b["cost"] for b in items)
    return (sum(b["pnl"] for b in items) / c) if c > 0 else None


def margin_bin(m):
    for lo, hi in MARGIN_BINS:
        if lo <= m < hi:
            return f"[{lo:.2f},{min(hi, 1.0):.2f})"
    return "other"


def walk_forward(all_bets, key, min_bets, rng, single_best=False):
    """Each event day: keep the categories whose trailing ROI > 0 (or the single best),
    trained on strictly earlier days; report the forward pooled ROI."""

    days = sorted({b["date"] for b in all_bets})
    by_day = defaultdict(list)
    for b in all_bets:
        by_day[b["date"]].append(b)
    kept, tested_days, chosen_log = [], 0, []
    for i, day in enumerate(days):
        if i < MIN_TRAIN_DAYS:
            continue
        train = [b for d in days[:i] for b in by_day[d]]
        cats = defaultdict(list)
        for b in train:
            cats[key(b)].append(b)
        scored = {c: roi(v) for c, v in cats.items() if len(v) >= min_bets and roi(v) is not None}
        if not scored:
            continue
        if single_best:
            best = max(scored, key=scored.get)
            chosen = {best} if scored[best] > 0 else set()
        else:
            chosen = {c for c, r in scored.items() if r > 0}
        tested_days += 1
        chosen_log.append(sorted(str(c) for c in chosen))
        kept.extend(b for b in by_day[day] if key(b) in chosen)
    out = pooled(kept, rng) if kept else {"bets": 0}
    out["tested_days"] = tested_days
    out["chosen_last_day"] = chosen_log[-1] if chosen_log else []
    out["days_with_no_pick"] = sum(1 for c in chosen_log if not c)
    return out


def main():
    vector = sys.argv[sys.argv.index("--vector") + 1] if "--vector" in sys.argv else "root"
    rows, skipped = load(sys.argv[1], sys.argv[2], vector=vector, floor="log_only")
    rng = np.random.default_rng(SEED)
    results = {"vector": vector, "rows": len(rows), "A_execution": {}, "B_rule": {}, "C_selection": {}, "D_accuracy": {}}

    # A + B: execution mode x rule
    per_rule_taker = {}
    for rule in RULES:
        results["A_execution"][rule] = {}
        for mode in ("taker", "mid", "maker"):
            items = [b for r in rows for b in bets_exec(r, rule, mode)]
            results["A_execution"][rule][mode] = pooled(items, rng)
            results["A_execution"][rule][mode]["by_side"] = {
                s: pooled([b for b in items if b["side"] == s], rng) for s in ("YES", "NO")}
            if mode == "taker":
                per_rule_taker[rule] = items
    results["B_rule"] = {rule: results["A_execution"][rule]["taker"]["roi"] for rule in RULES}

    # C: walk-forward selection, on the taker execution, Brier and Log
    for rule in ("brier", "log_d2"):
        items = per_rule_taker[rule]
        sel = {}
        sel["by_H_positive"] = walk_forward(items, lambda b: b["H"], MIN_TRAIN_BETS, rng)
        sel["by_H_single_best"] = walk_forward(items, lambda b: b["H"], MIN_TRAIN_BETS, rng, single_best=True)
        sel["by_side"] = walk_forward(items, lambda b: b["side"], MIN_TRAIN_BETS, rng)
        sel["by_margin_bin"] = walk_forward(items, lambda b: margin_bin(b["margin"]), MIN_TRAIN_BETS, rng)
        sel["by_station"] = walk_forward(items, lambda b: b["station"], MIN_STATION_BETS, rng)
        sel["by_H_x_side"] = walk_forward(items, lambda b: (b["H"], b["side"]), MIN_TRAIN_BETS, rng)
        sel["fair_hours_H>=14"] = pooled([b for b in items if b["H"] >= 14], rng)
        # in-sample oracle for contrast
        oracle = {}
        for name, key in (("H", lambda b: b["H"]), ("side", lambda b: b["side"]),
                          ("margin", lambda b: margin_bin(b["margin"])), ("station", lambda b: b["station"])):
            cats = defaultdict(list)
            for b in items:
                cats[key(b)].append(b)
            pos = {str(c): [len(v), roi(v)] for c, v in cats.items() if roi(v) is not None and roi(v) > 0}
            kept = [b for c, v in cats.items() if roi(v) is not None and roi(v) > 0 for b in v]
            oracle[name] = {"positive_categories": pos, "in_sample_roi": roi(kept), "share_of_cost": (sum(b["cost"] for b in kept) / sum(b["cost"] for b in items)) if kept else 0.0}
        sel["in_sample_oracle"] = oracle
        results["C_selection"][rule] = sel

    # D: accuracy the bets would need (Brier, taker)
    for rule in ("brier", "log_d2"):
        items = per_rule_taker[rule]
        acc = {}
        for side in ("YES", "NO"):
            sub = [b for b in items if b["side"] == side]
            c = sum(b["cost"] for b in sub)
            w = sum(b["shares"] for b in sub)
            hit = sum(b["shares"] * b["win"] for b in sub) / w
            paid = c / w
            acc[side] = {"bets": len(sub), "share_weighted_hit_rate": hit, "share_weighted_price_paid": paid,
                         "gap_points": 100 * (hit - paid), "roi": roi(sub)}
            for lo, hi in MARGIN_BINS:
                bb = [b for b in sub if lo <= b["margin"] < hi]
                if bb:
                    ww = sum(b["shares"] for b in bb)
                    acc[side][f"bin_{lo:.2f}"] = {"bets": len(bb), "hit": sum(b["shares"] * b["win"] for b in bb) / ww,
                                                  "paid": sum(b["cost"] for b in bb) / ww}
        results["D_accuracy"][rule] = acc

    (HERE / "results_levers.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))

    def fmt(x):
        return "—" if not x else f"{100 * x[0]:+.1f}% [{100 * x[1]:+.1f}, {100 * x[2]:+.1f}]"

    print("vector", vector, "rows", len(rows))
    print("\n== A/B execution price x rule (band on taker prices) ==")
    for rule in RULES:
        print(rule, " | ".join(f"{m}: {fmt(results['A_execution'][rule][m]['roi'])} (YES {fmt(results['A_execution'][rule][m]['by_side']['YES'].get('roi'))}, NO {fmt(results['A_execution'][rule][m]['by_side']['NO'].get('roi'))})" for m in ("taker", "mid", "maker")))
    print("\n== C walk-forward selection ==")
    for rule, sel in results["C_selection"].items():
        for name, b in sel.items():
            if name == "in_sample_oracle":
                for k, o in b.items():
                    print(f"  {rule} oracle {k}: in-sample ROI {100 * (o['in_sample_roi'] or 0):+.1f}% on {100 * o['share_of_cost']:.0f}% of cost, positive cats {list(o['positive_categories'].items())[:8]}")
            else:
                print(f"  {rule} {name}: {fmt(b.get('roi'))} bets {b.get('bets')} tested_days {b.get('tested_days', '-')} no-pick days {b.get('days_with_no_pick', '-')} last pick {b.get('chosen_last_day', '-')}")
    print("\n== D accuracy needed ==")
    for rule, acc in results["D_accuracy"].items():
        for side, a in acc.items():
            print(f"  {rule} {side}: hit {100 * a['share_weighted_hit_rate']:.1f}% vs paid {100 * a['share_weighted_price_paid']:.1f}% -> gap {a['gap_points']:+.1f} pts, ROI {100 * a['roi']:+.1f}% | " +
                  " ".join(f"m{k[4:]}: {100 * v['hit']:.0f}/{100 * v['paid']:.0f}" for k, v in a.items() if k.startswith("bin_")))


if __name__ == "__main__":
    main()
