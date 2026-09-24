"""Model probability as belief, P0 opens, Bayes updates re-solve (PROTOCOL_MODEL_BELIEF.md).

    python3 replay_model_belief.py OUT.json
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict

import numpy as np

from replay import solve_step, day_boot, W0

RANK = {"P0": 0, "P0-M": 1, "P1": 2}


class Book:
    def __init__(self, names, asks, bids, minorder):
        self.names = names
        self.asks = [asks.get(b) or [] for b in names]
        self.bids = [bids.get(b) or [] for b in names]
        self.minorder = [float(minorder.get(b, 5.0)) for b in names]


def load():
    ov = json.load(open("venue_truth_overrides.json"))["overrides"]
    by_sd = defaultdict(list)
    truth = {}
    for line in gzip.open("p0_events.jsonl.gz", "rt"):
        r = json.loads(line)
        by_sd[(r["st"], r["day"])].append((r["t"], r["stage"], r["belief"], r["m"], r["asks"], r["bids"], r["min_order"]))
    for line in gzip.open("events.jsonl.gz", "rt"):
        r = json.loads(line)
        truth[(r["st"], r["day"])] = ov.get(f"{r['st']}|{r['day']}", r["truth"])
        bk = r["books"].get("5") or r["books"].get("15")
        if not bk:
            continue
        by_sd[(r["st"], r["day"])].append((r["t_ours"], "P1", r["p1"], bk["m"], bk["asks"], bk["bids"], bk["min_order"]))
    return by_sd, truth, ov


def run(by_sd, truth, gamma, allow_sell=True, open_only=False, market_belief=False):
    per_day = defaultdict(list)
    attrib = defaultdict(float)
    pnl = spent = 0.0
    traded_days, traded_sd, n_sd, finals = set(), 0, 0, []
    for key, evs in by_sd.items():
        if key not in truth:
            continue
        t_bucket = truth[key]
        evs = sorted(evs, key=lambda e: e[0])
        opened = False
        top = -1
        cash, hold, any_trade = W0, {}, False
        for t, stage, belief, m, asks, bids, mino in evs:
            if RANK[stage] < top:
                continue
            if not opened and stage != "P0":
                continue
            top = max(top, RANK[stage])
            label = "P0_open" if not opened else ("P0_update" if stage == "P0" else stage)
            if opened and open_only:
                break
            names = [b for b in belief if b in asks]
            if t_bucket not in names:
                names = names + ([t_bucket] if t_bucket in asks else [])
            src = m if market_belief else belief
            if not src:
                opened = True
                continue
            p = np.array([max(float(src.get(b, 0.0)), 0.0) for b in names])
            if p.sum() <= 0:
                opened = True
                continue
            p = p / p.sum()
            h = np.array([hold.get(b, 0.0) for b in names])
            fills = []
            before = cash
            cash, h2, traded = solve_step(p, Book(names, asks, bids, mino), cash, h, gamma, allow_sell=allow_sell, fills=fills)
            for b, v in zip(names, h2):
                hold[b] = float(v)
            for k, shares, cost in fills:
                hit = 1.0 if names[k] == t_bucket else 0.0
                attrib[label] += shares * hit - cost
            spent += max(0.0, before - cash)
            any_trade |= traded
            opened = True
        if not opened:
            continue
        n_sd += 1
        final = cash + hold.get(t_bucket, 0.0)
        finals.append(final)
        per_day[key[1]].append(math.log(max(final, 1e-9) / W0))
        pnl += final - W0
        if any_trade:
            traded_sd += 1
            traded_days.add(key[1])
    mean, lo, hi = day_boot(per_day)
    days = sorted(per_day)
    h1 = [x for d in days[: len(days) // 2] for x in per_day[d]]
    h2 = [x for d in days[len(days) // 2:] for x in per_day[d]]
    return {"mean_log_growth": mean, "lo90": lo, "hi90": hi,
            "first_half": float(np.mean(h1)), "second_half": float(np.mean(h2)),
            "station_days": n_sd, "event_days": len(days), "traded_station_days": traded_sd,
            "traded_event_days": len(traded_days), "pnl_usdc": pnl, "spent_usdc": spent,
            "win_share": float(np.mean(np.array(finals) > W0)) if finals else None,
            "pnl_by_stage": dict(attrib)}


def main():
    by_sd, truth, _ = load()
    arms = {
        "A_model_buy_sell_g4": dict(gamma=4.0),
        "B_model_buy_only_g4": dict(gamma=4.0, allow_sell=False),
        "C_model_open_only_g4": dict(gamma=4.0, open_only=True),
        "D_market_belief_g4": dict(gamma=4.0, market_belief=True),
        "E_model_buy_sell_g8": dict(gamma=8.0),
    }
    out = {}
    for name, kw in arms.items():
        out[name] = run(by_sd, truth, **kw)
        print(name, {k: (round(v, 4) if isinstance(v, float) else ({kk: round(vv, 1) for kk, vv in v.items()} if isinstance(v, dict) else v)) for k, v in out[name].items()}, flush=True)
    json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
