"""Round 40e: is there direction in F plus the adjacent buckets' mass?
On the cluster's pre-formation first YES that land one bucket off F: how often
does each signal point to the right neighbour (position of F in its bucket;
members' corrected mass above vs below; ensemble mass; our P0 mass; ECMWF
momentum; the cluster's station history), alone and combined in a
leave-one-day-out logistic; the resulting two-bucket cover (F + predicted
neighbour), by halves of the event days; and whether any signal strength makes
the neighbour more likely than F itself.
"""
import math
import statistics
import sys
from pathlib import Path

import numpy as np

here = Path(__file__)
src = here.with_name("cluster_pick_model_r40.py").read_text()
ns = {"__name__": "r40", "__file__": str(here.with_name("cluster_pick_model_r40.py"))}
exec(compile(src.split("all_events = len(events)")[0], "r40_head", "exec"), ns)
events = ns["events"]


def signals(ev):
    lo, hi = ev["edges"][ev["fb"]]
    u = (ev["f"] - lo) / (hi - lo) - 0.5
    up = sum(v for j, v in ev["cvotes"].items() if j > ev["fb"]) / max(1, ev["nmem"])
    dn = sum(v for j, v in ev["cvotes"].items() if j < ev["fb"]) / max(1, ev["nmem"])
    e_up = sum(p for j, p in ev["ensp"].items() if j > ev["fb"])
    e_dn = sum(p for j, p in ev["ensp"].items() if j < ev["fb"])
    p_up = sum(p for j, p in ev["p0"].items() if j > ev["fb"])
    p_dn = sum(p for j, p in ev["p0"].items() if j < ev["fb"])
    return {"position": u, "member mass": up - dn, "ensemble mass": e_up - e_dn, "P0 mass": p_up - p_dn,
            "momentum": ev["M"], "history": ev["hist"]}


off = [ev for ev in events if abs(ev["choice"] - ev["fb"]) == 1]
n_all = len(events)
same = sum(ev["choice"] == ev["fb"] for ev in events)
print(f"{n_all} picks: = F {same}, one off {len(off)} (up {sum(ev['choice'] > ev['fb'] for ev in off)}, down {sum(ev['choice'] < ev['fb'] for ev in off)}), further {n_all - same - len(off)}")
for name in ("position", "member mass", "ensemble mass", "P0 mass", "momentum", "history"):
    right = sum(1 for ev in off if signals(ev)[name] != 0 and (signals(ev)[name] > 0) == (ev["choice"] > ev["fb"]))
    told = sum(1 for ev in off if signals(ev)[name] != 0)
    print(f"   {name:14s} points the right way on {right}/{told} = {right / max(1, told):.1%} of the one-off picks it has a sign for")

names = ["position", "member mass", "ensemble mass", "P0 mass", "momentum", "history"]
X = np.array([[signals(ev)[k] for k in names] for ev in off])
Y = np.array([1.0 if ev["choice"] > ev["fb"] else 0.0 for ev in off])
days = sorted({ev["ed"] for ev in off})


def fit(x, y, l2=1.0):
    w = np.zeros(x.shape[1] + 1)
    xb = np.hstack([x, np.ones((len(x), 1))])
    for _ in range(2000):
        p = 1 / (1 + np.exp(-xb @ w))
        w -= 0.1 * (xb.T @ (p - y) / len(y) + l2 * np.r_[w[:-1], 0] / len(y))
    return w


pred = {}
for d in days:
    tr = [i for i, ev in enumerate(off) if ev["ed"] != d]
    w = fit(X[tr], Y[tr])
    for i, ev in enumerate(off):
        if ev["ed"] == d:
            pred[id(ev)] = 1 if np.r_[X[i], 1] @ w > 0 else -1
right = sum(1 for ev in off if (pred[id(ev)] > 0) == (ev["choice"] > ev["fb"]))
print(f"   combined (leave-one-day-out)  points the right way on {right}/{len(off)} = {right / len(off):.1%}")
w_all = fit(X, Y)
print("   combined weights:", {k: round(float(v), 2) for k, v in zip(names + ["bias"], w_all)})

# Two-bucket cover: F + neighbour chosen by position alone (no fitted part), by halves.
mid = days[len(days) // 2]
for label, sub in (("all", events), (f"days < {mid}", [e for e in events if e["ed"] < mid]),
                   (f"days >= {mid}", [e for e in events if e["ed"] >= mid])):
    n = max(1, len(sub))
    s = sum(e["choice"] == e["fb"] for e in sub)
    pos = sum(e["choice"] in (e["fb"], e["fb"] + (1 if signals(e)["position"] >= 0 else -1)) for e in sub)
    down = sum(e["choice"] in (e["fb"], e["fb"] - 1) for e in sub)
    print(f"   {label:16s} n={len(sub):3d}  F {s / n:.1%}  F+lower neighbour {down / n:.1%}  F+neighbour by position {pos / n:.1%}")

# Single bucket: does a strong lean ever make the neighbour likelier than F?
for lo_u, hi_u in ((0.35, 0.45), (0.45, 0.5)):
    for sign in (1, -1):
        sub = [e for e in events if lo_u <= sign * signals(e)["position"] < hi_u]
        if not sub:
            continue
        n = len(sub)
        nb = sum(e["choice"] == e["fb"] + sign for e in sub)
        print(f"   F within {0.5 - hi_u:.2f}-{0.5 - lo_u:.2f} of its {'upper' if sign > 0 else 'lower'} edge (n {n:2d}): = F {sum(e['choice'] == e['fb'] for e in sub) / n:.0%}, = that neighbour {nb / n:.0%}")
