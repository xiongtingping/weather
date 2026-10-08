"""Taker replay on real fills (secondary to backtest.py's proper-score verdict).

Protocol (fixed 2026-10-08 before the replay was run):
* A taker BUY of YES (resp. NO) at price x, time tau, is evidence that YES
  (resp. NO) could be bought at x at tau.  We would have bought 1 share at the
  same x when p_model - x - fee(x) >= MARGIN, p_model being the model's
  probability for that side at the latest 12Z decision point <= tau.
* fee(x) = 0.05 * x * (1 - x) per share (venue taker fee, as in the L4 replays).
* At most one fill per (bucket, side, decision day): the first qualifying trade.
* PnL per share = payout - x - fee.  Primary: YES side, forecast arm, MARGIN
  0.05.  Reported alongside: NO side, climatology arm, MARGIN 0.02 / 0.10.
* Interval: event-block bootstrap (10,000 reps) of total PnL / total cost.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import pathlib
from collections import defaultdict

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent


def fee(x):
    return 0.05 * x * (1.0 - x)


def load():
    events = json.load(open(HERE / "events.json"))
    rows = [json.loads(l) for l in gzip.open(HERE / "rows.jsonl.gz", "rt")]
    with gzip.open(HERE / "raw" / "trades.json.gz", "rt") as f:
        trades = json.load(f)
    return events, rows, trades


def bucket_order(e):
    import backtest as b
    edges = [b.parse_bucket(m["label"]) for m in e["markets"]]
    order = list(np.argsort([lo for lo, _ in edges]))
    return [e["markets"][i] for i in order]


def run(events, rows, trades, arm="fc", side="Yes", margin=0.05):
    by_day = {(r["event"], r["date"]): r for r in rows}
    fills = []
    for e in events:
        if not e["closed"]:
            continue
        markets = bucket_order(e)
        for k, m in enumerate(markets):
            seen = set()
            ts = sorted(trades.get(m["condition_id"], []), key=lambda t: t["timestamp"])
            for t in ts:
                if t["side"] != "BUY" or t["outcome"] != side:
                    continue
                tau = dt.datetime.fromtimestamp(t["timestamp"], dt.timezone.utc)
                d = tau.date() if tau.hour >= 12 else tau.date() - dt.timedelta(days=1)
                r = by_day.get((e["title"], d.isoformat()))
                if r is None or (k, d) in seen:
                    continue
                p_yes = r[f"p_{arm}"][k]
                p = p_yes if side == "Yes" else 1.0 - p_yes
                x = float(t["price"])
                if p - x - fee(x) < margin:
                    continue
                seen.add((k, d))
                win = (r["k_true"] == k) == (side == "Yes")
                fills.append({"event": e["title"], "city": r["city"], "date": d.isoformat(),
                              "day_of_month": r["day_of_month"], "bucket": m["label"], "side": side,
                              "price": x, "p_model": round(p, 4), "win": win,
                              "pnl": (1.0 if win else 0.0) - x - fee(x), "cost": x + fee(x)})
    return fills


def summarise(fills, rng, label):
    if not fills:
        return {"label": label, "fills": 0}
    by_ev = defaultdict(list)
    for f in fills:
        by_ev[f["event"]].append(f)
    names = list(by_ev)
    pnl = np.array([sum(f["pnl"] for f in by_ev[n]) for n in names])
    cost = np.array([sum(f["cost"] for f in by_ev[n]) for n in names])
    reps = []
    for _ in range(10000):
        i = rng.integers(0, len(names), len(names))
        reps.append(pnl[i].sum() / cost[i].sum())
    lo, hi = np.percentile(reps, [2.5, 97.5])
    return {"label": label, "fills": len(fills), "events": len(names),
            "pnl_per_share": round(float(pnl.sum() / len(fills)), 4),
            "roi": round(float(pnl.sum() / cost.sum()), 4), "roi_ci95": [round(float(lo), 4), round(float(hi), 4)],
            "win_rate": round(float(np.mean([f["win"] for f in fills])), 3),
            "mean_price": round(float(np.mean([f["price"] for f in fills])), 3),
            "events_positive": int((pnl > 0).sum())}


def main():
    import sys
    sys.path.insert(0, str(HERE))
    events, rows, trades = load()
    rng = np.random.default_rng(20261008)
    out = []
    primary = run(events, rows, trades, "fc", "Yes", 0.05)
    out.append(summarise(primary, rng, "PRIMARY fc YES margin0.05"))
    for city in ("Seattle", "NYC"):
        out.append(summarise([f for f in primary if f["city"] == city], rng, f"  fc YES 0.05 {city}"))
    for lo, hi in ((-7, 0), (1, 10), (11, 20), (21, 31)):
        out.append(summarise([f for f in primary if lo <= f["day_of_month"] <= hi], rng,
                             f"  fc YES 0.05 day {lo}..{hi}"))
    out.append(summarise([f for f in primary if f["price"] < 0.9], rng, "  fc YES 0.05 price<0.90"))
    for arm, side, margin in (("fc", "No", 0.05), ("clim", "Yes", 0.05), ("clim", "No", 0.05),
                              ("fc", "Yes", 0.02), ("fc", "Yes", 0.10)):
        out.append(summarise(run(events, rows, trades, arm, side, margin), rng, f"{arm} {side} margin{margin}"))
    with gzip.open(HERE / "fills_primary.jsonl.gz", "wt") as f:
        for x in primary:
            f.write(json.dumps(x) + "\n")
    json.dump({"protocol": __doc__, "results": out}, open(HERE / "replay.json", "w"), indent=1, ensure_ascii=False)
    for o in out:
        print(o)


if __name__ == "__main__":
    main()
