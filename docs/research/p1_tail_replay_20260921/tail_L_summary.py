import json
import math
import random
import sys
from collections import defaultdict

PATH = sys.argv[1]
recs = [json.loads(l) for l in open(PATH) if l.strip()]
VARIANTS = ("mask", "S", "M", "g1", "ga")
FLOOR = 1e-4
random.seed(20260921)


def boot(by_day, stat, n=2000):
    days = sorted(by_day)
    if len(days) < 3:
        return (float("nan"), float("nan"))
    vals = []
    for _ in range(n):
        pick = [random.choice(days) for _ in days]
        v = stat([by_day[d] for d in pick])
        if v is not None:
            vals.append(v)
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1])


def logscore(rs, name):
    by = defaultdict(list)
    for r in rs:
        v = r["v"].get(name) or {}
        if "q_truth" not in v:
            continue
        by[r["day"]].append(math.log(max(v["q_truth"], FLOOR)) - math.log(max(r["m_truth"], FLOOR)))
    flat = [x for d in by.values() for x in d]
    if not flat:
        return None
    mean = sum(flat) / len(flat)
    lo, hi = boot(by, lambda groups: (lambda f: sum(f) / len(f))([x for g in groups for x in g]))
    return mean, lo, hi, len(flat)


def trades(rs, name):
    rs = sorted(rs, key=lambda r: (r["station"], r["day"], r["report"]))
    bought = defaultdict(set)
    out = []
    for r in rs:
        key = (r["station"], r["day"])
        for t in (r["v"].get(name) or {}).get("takes", []):
            if t["bucket"] in bought[key] or len(bought[key]) >= 2:
                continue
            bought[key].add(t["bucket"])
            out.append({**t, "day": r["day"], "station": r["station"], "delay": r["delay_min"],
                        "pnl": t["shares"] * t["hit"] - t["cost"]})
    return out


def money(ts):
    if not ts:
        return None
    by = defaultdict(list)
    for t in ts:
        by[t["day"]].append(t)
    cost = sum(t["cost"] for t in ts)
    pnl = sum(t["pnl"] for t in ts)
    lo, hi = boot(by, lambda g: (lambda f: sum(t["pnl"] for t in f) / sum(t["cost"] for t in f))([t for x in g for t in x]))
    days_pos = sum(1 for d in by.values() if sum(t["pnl"] for t in d) > 0)
    return {"n": len(ts), "per_usd": pnl / cost, "ci": (lo, hi), "hit": sum(t["hit"] for t in ts) / len(ts),
            "price": sum(t["price"] for t in ts) / len(ts), "days_pos": days_pos, "days": len(by),
            "pnl": pnl, "cost": cost}


def table(title, rs):
    print(f"\n=== {title}  (records {len(rs)}, days {len({r['day'] for r in rs})}, "
          f"median delay {sorted(r['delay_min'] for r in rs)[len(rs)//2] if rs else 0:.1f} min)")
    print(f"{'L':5} {'logscore vs market':>30} {'trades':>6} {'per $':>7} {'95% CI':>17} {'hit':>5} {'price':>6} {'days+':>6}")
    for name in VARIANTS:
        ls = logscore(rs, name)
        m = money(trades(rs, name))
        lstxt = f"{ls[0]:+.4f} [{ls[1]:+.4f},{ls[2]:+.4f}]" if ls else "-"
        if m:
            print(f"{name:5} {lstxt:>30} {m['n']:6d} {m['per_usd']:+7.3f} [{m['ci'][0]:+.3f},{m['ci'][1]:+.3f}] "
                  f"{m['hit']:5.2f} {m['price']:6.2f} {m['days_pos']:3d}/{m['days']:<2d}")
        else:
            print(f"{name:5} {lstxt:>30} {0:6d}")


for lag in (1, 8):
    table(f"stream: first book >= report+{lag} min", [r for r in recs if r["lag"] == lag])
for lo, hi in ((0, 5), (5, 10), (10, 16), (16, 30)):
    table(f"pooled streams, actual delay [{lo},{hi}) min", [r for r in recs if lo <= r["delay_min"] < hi])
table("stream 1, days 09-05..09-17", [r for r in recs if r["lag"] == 1 and r["day"] <= "2026-09-17"])
table("stream 1, days 09-18..09-20", [r for r in recs if r["lag"] == 1 and r["day"] >= "2026-09-18"])

# How often S and M disagree on firing, and what the disagreements did.
print("\n=== S vs M firing disagreements, stream 1")
s = {(t["station"], t["day"], t["bucket"]): t for t in trades([r for r in recs if r["lag"] == 1], "S")}
m = {(t["station"], t["day"], t["bucket"]): t for t in trades([r for r in recs if r["lag"] == 1], "M")}
only_s = [s[k] for k in s.keys() - m.keys()]
only_m = [m[k] for k in m.keys() - s.keys()]
both = [s[k] for k in s.keys() & m.keys()]
for label, ts in (("both", both), ("only S", only_s), ("only M", only_m)):
    mm = money(ts)
    if mm:
        print(f"{label:7} n={mm['n']:4d} per$ {mm['per_usd']:+.3f} [{mm['ci'][0]:+.3f},{mm['ci'][1]:+.3f}] hit {mm['hit']:.2f} price {mm['price']:.2f}")
    else:
        print(f"{label:7} n=0")
