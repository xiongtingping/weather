#!/usr/bin/env python3
"""Is the maker ceiling (+2.3% Log / +4.4% Brier) reachable?  Adverse-selection-aware
maker replay on the paper's per-bucket protocol, pure P0, both sides.

At grid hour t a passive order rests per the paper's band (YES at the bid when
p > ask, NO at the NO-bid = 1 - ask when p < bid), sized against the resting price.
It fills only if the market trades through it by the next grid hour:
  strict : YES fills if ask(t+1) <= our bid(t);   NO fills if bid(t+1) >= our ask(t)
  touch  : YES fills if bid(t+1) <= our bid(t);   NO fills if ask(t+1) >= our ask(t)
Filled legs are held to settlement; unfilled orders are cancelled (the paper's D.7
cancels at the next cycle).  Beside it: the same orders at the taker price and the
frictionless ceiling (every order filled at the resting price).  Also the terminal
truth-conditional check: how often the fills land on buckets the market then
abandoned.  Writes results_maker_fill_replay.json.

    PYTHONPATH=<repo> python3 maker_fill_replay.py DATASET.jsonl.gz OVERRIDES.json [--vector root|served]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paper_protocol import SEED, load, pooled, size  # noqa: E402

RULES = ("brier", "log_d2", "logit_c1")


def main():
    vector = sys.argv[sys.argv.index("--vector") + 1] if "--vector" in sys.argv else "root"
    rows, _skipped = load(sys.argv[1], sys.argv[2], vector=vector, floor="log_only")
    rng = np.random.default_rng(SEED)
    by_sd = defaultdict(list)
    for r in rows:
        by_sd[(r["station"], r["date"])].append(r)
    results = {"vector": vector, "station_days": len(by_sd), "rules": {}}
    for rule in RULES:
        orders = []          # every resting order with its fill outcomes
        for (station, day), items in by_sd.items():
            items = sorted(items, key=lambda r: -r["H"])
            for a, b in zip(items, items[1:]):
                if a["keys"] != b["keys"]:
                    continue
                y = a["y"]
                for k, p in enumerate(a["pv"][rule]):
                    asks, bids = a["asks"][k], a["bids"][k]
                    nasks, nbids = b["asks"][k], b["bids"][k]
                    ask1 = asks[0][0] if asks else None
                    bid1 = bids[0][0] if bids else None
                    nask1 = nasks[0][0] if nasks else None
                    nbid1 = nbids[0][0] if nbids else None
                    hit = 1.0 if k == y else 0.0
                    if ask1 is not None and p > ask1 and bid1 is not None:
                        rest = bid1                                # passive YES at the bid
                        w = size(rule, p, rest)
                        orders.append({"date": day, "station": station, "H": a["H"], "side": "YES", "shares": w,
                                       "rest_cost": w * rest, "rest_pnl": w * (hit - rest),
                                       "taker_cost": w * ask1, "taker_pnl": w * (hit - ask1),
                                       "fill_strict": nask1 is not None and nask1 <= rest + 1e-12,
                                       "fill_touch": nbid1 is not None and nbid1 <= rest + 1e-12,
                                       "hit": hit, "next_mid_down": (nask1 is not None and nbid1 is not None and (nask1 + nbid1) / 2 < (ask1 + bid1) / 2)})
                    elif bid1 is not None and p < bid1 and ask1 is not None:
                        rest_yes = ask1                            # passive NO at the NO-bid = 1 - ask
                        w = size(rule, p, rest_yes)
                        orders.append({"date": day, "station": station, "H": a["H"], "side": "NO", "shares": w,
                                       "rest_cost": w * (1.0 - rest_yes), "rest_pnl": w * ((1.0 - hit) - (1.0 - rest_yes)),
                                       "taker_cost": w * (1.0 - bid1), "taker_pnl": w * ((1.0 - hit) - (1.0 - bid1)),
                                       "fill_strict": nbid1 is not None and nbid1 >= rest_yes - 1e-12,
                                       "fill_touch": nask1 is not None and nask1 >= rest_yes - 1e-12,
                                       "hit": hit, "next_mid_down": (nask1 is not None and nbid1 is not None and (nask1 + nbid1) / 2 < (ask1 + bid1) / 2)})
        out = {"orders": len(orders)}

        def read(subset, cost_key, pnl_key, label):
            items = [{"date": o["date"], "cost": o[cost_key], "pnl": o[pnl_key], "dS": 0.0, "D": 0.0, "slip": 0.0,
                      "shares": o["shares"], "win": o["hit"] if o["side"] == "YES" else 1.0 - o["hit"]} for o in subset]
            out[label] = pooled(items, rng) if items else {"bets": 0}
            out[label]["share_of_orders"] = len(subset) / max(len(orders), 1)
            out[label]["share_of_rest_cost"] = (sum(o["rest_cost"] for o in subset) / sum(o["rest_cost"] for o in orders)) if orders else 0.0

        read(orders, "taker_cost", "taker_pnl", "all_at_taker")
        read(orders, "rest_cost", "rest_pnl", "ceiling_all_filled_at_rest")
        for mode in ("strict", "touch"):
            filled = [o for o in orders if o[f"fill_{mode}"]]
            read(filled, "rest_cost", "rest_pnl", f"maker_{mode}_filled")
            read([o for o in orders if not o[f"fill_{mode}"]], "rest_cost", "rest_pnl", f"maker_{mode}_unfilled_would_have")
            for side in ("YES", "NO"):
                read([o for o in filled if o["side"] == side], "rest_cost", "rest_pnl", f"maker_{mode}_filled_{side}")
        # adverse selection in one number: hit rate of filled vs unfilled YES orders
        for side in ("YES", "NO"):
            sub = [o for o in orders if o["side"] == side]
            f = [o for o in sub if o["fill_strict"]]
            u = [o for o in sub if not o["fill_strict"]]
            out[f"{side}_hit_rate_filled_vs_unfilled_strict"] = [
                (sum(o["hit"] for o in f) / len(f)) if f else None, (sum(o["hit"] for o in u) / len(u)) if u else None]
        results["rules"][rule] = out
    (HERE / "results_maker_fill_replay.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))

    def fmt(b):
        x = b.get("roi") if b else None
        return "—" if not x else f"{100 * x[0]:+.1f}% [{100 * x[1]:+.1f}, {100 * x[2]:+.1f}]"

    print("vector", vector, "station-days", len(by_sd))
    for rule, out in results["rules"].items():
        print(f"\n== {rule}: {out['orders']} resting orders ==")
        for label in ("all_at_taker", "ceiling_all_filled_at_rest", "maker_strict_filled", "maker_strict_unfilled_would_have",
                      "maker_touch_filled", "maker_touch_unfilled_would_have", "maker_strict_filled_YES", "maker_strict_filled_NO",
                      "maker_touch_filled_YES", "maker_touch_filled_NO"):
            b = out[label]
            print(f"  {label:36s} ROI {fmt(b):28s} orders {b.get('bets', 0):6d} ({100 * b['share_of_orders']:.0f}% of orders, {100 * b['share_of_rest_cost']:.0f}% of cost)")
        for side in ("YES", "NO"):
            v = out[f"{side}_hit_rate_filled_vs_unfilled_strict"]
            print(f"  {side} bucket hit rate: filled(strict) {100 * (v[0] or 0):.1f}% vs unfilled {100 * (v[1] or 0):.1f}%")


if __name__ == "__main__":
    main()
