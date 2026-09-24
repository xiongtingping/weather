"""Mean-bucket-centred basket window (PROTOCOL_MEAN_WINDOW.md).

    python3 replay_mean_window.py OUT.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict

import numpy as np

from replay import day_boot
from replay_model_belief import load, RANK

STAKE = 10.0
MIN_ORDER = 5.0


def ladder_key(name: str) -> float:
    m = re.search(r"-?\d+(?:\.\d+)?", name)
    v = float(m.group()) if m else 0.0
    low = name.lower()
    if "below" in low:
        return v - 0.5
    if "higher" in low or "above" in low:
        return v + 0.5
    return v


def window(p: dict, ladder: list[str], width: int) -> list[str]:
    probs = np.array([max(float(p.get(b, 0.0)), 0.0) for b in ladder])
    if probs.sum() <= 0:
        return []
    probs /= probs.sum()
    mean = float((probs * np.arange(len(ladder))).sum())
    c = int(round(mean))
    if width == 1:
        idx = [c]
    elif width == 2:
        idx = [c, c + 1] if mean >= c else [c - 1, c]
    else:
        idx = [c - 1, c, c + 1]
    shift = max(0, -min(idx)) - max(0, max(idx) - (len(ladder) - 1))
    return [ladder[i + shift] for i in idx]


def walk(levels, shares):
    """Cost of `shares` walking the levels, or None when the depth is short."""
    left, cost = shares, 0.0
    for price, size in levels or []:
        take = min(size, left)
        cost += take * price
        left -= take
        if left <= 1e-9:
            return cost
    return None


def run(by_sd, truth, width, mode, centre="model"):
    per_day_pnl, per_day_spent = defaultdict(list), defaultdict(list)
    attrib = defaultdict(float)
    hits, n_open, over_one, traded_days = 0, 0, 0, set()
    for key, evs in by_sd.items():
        if key not in truth:
            continue
        tb = truth[key]
        evs = sorted(evs, key=lambda e: e[0])
        top, seen_p0, opened = -1, False, False
        n, held, cost_basis, spent, pnl = 0, defaultdict(float), 0.0, 0.0, 0.0
        for t, stage, belief, m, asks, bids, mino in evs:
            if RANK[stage] < top:
                continue
            top = max(top, RANK[stage])
            seen_p0 |= stage == "P0"
            if not seen_p0:
                continue
            src = m if centre == "market" else belief
            if not src:
                continue
            ladder = sorted([b for b in asks if asks.get(b)], key=ladder_key)
            if len(ladder) < 3:
                continue
            win = window(src, ladder, width)
            if not win:
                continue
            label = ("open" if not opened else ("P0_update" if stage == "P0" else stage))
            if not opened:
                if sum(asks[b][0][0] for b in win) >= 1.0:
                    continue
                best_n, best_cost = 0, 0.0
                for cand in range(int(MIN_ORDER), 400):
                    costs = [walk(asks[b], cand) for b in win]
                    if any(c is None for c in costs) or sum(costs) > STAKE:
                        break
                    best_n, best_cost = cand, sum(costs)
                if best_n < MIN_ORDER:
                    continue
                n = best_n
                for b in win:
                    c = walk(asks[b], n)
                    held[b] += n
                    spent += c
                    attrib[label] += n * (1.0 if b == tb else 0.0) - c
                opened = True
                n_open += 1
                traded_days.add(key[1])
                continue
            if mode == "open_only":
                break
            for b in win:
                need = n - held[b]
                if need < MIN_ORDER:
                    continue
                c = walk(asks.get(b), need)
                if c is None:
                    continue
                held[b] += need
                spent += c
                attrib[label] += need * (1.0 if b == tb else 0.0) - c
            if mode == "recentre":
                for b in list(held):
                    if b in win or held[b] < MIN_ORDER:
                        continue
                    proceeds = walk(bids.get(b), held[b])
                    if proceeds is None:
                        continue
                    attrib[label] += proceeds - held[b] * (1.0 if b == tb else 0.0)
                    spent -= proceeds
                    held[b] = 0.0
        if not opened:
            continue
        final_payout = held.get(tb, 0.0)
        pnl = final_payout - spent
        per_day_pnl[key[1]].append(pnl)
        per_day_spent[key[1]].append(spent)
        hits += 1 if held.get(tb, 0.0) > 0 else 0
        over_one += 1 if spent / n > 1.0 else 0
    days = sorted(per_day_pnl)
    mean, lo, hi = day_boot(per_day_pnl)
    rng = np.random.default_rng(7)
    rois = []
    for _ in range(2000):
        pick = rng.choice(len(days), len(days))
        p = sum(sum(per_day_pnl[days[i]]) for i in pick)
        s = sum(sum(per_day_spent[days[i]]) for i in pick)
        rois.append(p / s if s else 0.0)
    tot_p = sum(sum(v) for v in per_day_pnl.values())
    tot_s = sum(sum(v) for v in per_day_spent.values())
    half = lambda ds: (sum(sum(per_day_pnl[d]) for d in ds) / max(1e-9, sum(sum(per_day_spent[d]) for d in ds)))
    return {"station_days_opened": n_open, "event_days": len(days),
            "pnl_usdc": tot_p, "spent_usdc": tot_s, "roi": tot_p / tot_s if tot_s else None,
            "roi_lo90": float(np.percentile(rois, 5)), "roi_hi90": float(np.percentile(rois, 95)),
            "roi_first_half": half(days[: len(days) // 2]), "roi_second_half": half(days[len(days) // 2:]),
            "pnl_per_sd": mean, "pnl_per_sd_lo90": lo, "pnl_per_sd_hi90": hi,
            "hit_rate": hits / n_open if n_open else None,
            "cost_per_share_over_1": over_one / n_open if n_open else None,
            "pnl_by_stage": dict(attrib)}


def main():
    by_sd, truth, _ = load()
    arms = [
        ("W3_add_only (主口径)", 3, "add_only", "model"),
        ("W3_recentre", 3, "recentre", "model"),
        ("W3_open_only", 3, "open_only", "model"),
        ("W2_add_only", 2, "add_only", "model"),
        ("W1_add_only", 1, "add_only", "model"),
        ("W3_add_only_market_centre (参考)", 3, "add_only", "market"),
        ("W3_open_only_market_centre (参考)", 3, "open_only", "market"),
    ]
    out = {}
    for name, w, mode, centre in arms:
        out[name] = run(by_sd, truth, w, mode, centre)
        r = out[name]
        print(f"{name}: 开仓站日 {r['station_days_opened']} 事件日 {r['event_days']} 回报率 {r['roi']:+.3f} [{r['roi_lo90']:+.3f},{r['roi_hi90']:+.3f}] 前/后 {r['roi_first_half']:+.3f}/{r['roi_second_half']:+.3f} 盈亏 {r['pnl_usdc']:+.1f} 花费 {r['spent_usdc']:.1f} 命中 {r['hit_rate']:.1%} 均份成本>1 {r['cost_per_share_over_1']:.1%} 归因 { {k: round(v,1) for k,v in r['pnl_by_stage'].items()} }", flush=True)
    json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
