"""Round 31 part 2 (熊先生 10-11 「和选择其他桶一起联合分析。我要选桶的确定性规则」).

One deterministic rule for the whole first-take choice -- which bucket and
which side -- over every candidate leg (each bucket x YES/NO with a price):
  layer 1  the gate: where it ever buys (coverage of the chosen leg, research | forward)
  layer 2  a points table: integer points per condition, the pick = most points
           (ties: the higher-priced YES first, then the bucket nearer the mode),
           weights from a conditional logit on the research set rounded to integers,
           scored on the forward set (top-1, top-3, side of the mode)
against uniform choice and the LightGBM ceiling of round 26 (forward 38-39% top-1).
Data: replicate_rows.json.gz, 1,491 research events (to 10-07), 612 forward (10-08..10-12).
"""
import collections
import gzip
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

B = Path(sys.argv[1])
E = json.load(gzip.open(B / "replicate_rows.json.gz", "rt"))

COND = [
    # position and side relative to the market mode
    ("YES on the mode", lambda c: c["side"] == "Yes" and c["d"] == 0),
    ("YES one below the mode", lambda c: c["side"] == "Yes" and c["d"] == -1),
    ("YES one above the mode", lambda c: c["side"] == "Yes" and c["d"] == 1),
    ("YES two from the mode", lambda c: c["side"] == "Yes" and abs(c["d"]) == 2),
    ("NO on the mode", lambda c: c["side"] == "No" and c["d"] == 0),
    ("NO one below the mode", lambda c: c["side"] == "No" and c["d"] == -1),
    ("NO one above the mode", lambda c: c["side"] == "No" and c["d"] == 1),
    ("NO two from the mode", lambda c: c["side"] == "No" and abs(c["d"]) == 2),
    # price
    ("YES priced 0.10-0.50", lambda c: c["side"] == "Yes" and 0.10 <= c["price"] < 0.50),
    ("NO priced 0.30-0.75", lambda c: c["side"] == "No" and 0.30 <= c["price"] < 0.75),
    ("price under 0.05 or over 0.90", lambda c: c["price"] < 0.05 or c["price"] > 0.90),
    # the cluster's own book on the station-day
    ("same leg already held", lambda c: c["held_same"]),
    ("other side of this bucket held", lambda c: c["held_opp"]),
    ("this leg was its last trade here", lambda c: c["last_leg"]),
    ("leg agrees with its lean off the mode", lambda c: c["lean_agree"]),
    ("leg against its lean off the mode", lambda c: c["lean_against"]),
    # timing
    ("leg got >1c cheaper in the last hour", lambda c: c["mom"] < -0.01),
    ("leg got >1c dearer in the last hour", lambda c: c["mom"] > 0.01),
    # D0 observation
    ("D0: observed high already in this bucket", lambda c: c["obs_in"]),
    ("D0: observed high already above this bucket", lambda c: c["obs_above"]),
    # our model
    ("our p says the leg is >5c cheap", lambda c: c["edge"] > 0.05),
]


def label_ord(e):
    return {v: int(k) for k, v in e["labels"].items()}


def candidates(e):
    mo = e["mode_ord"]
    lo = label_ord(e)
    prior = sorted(e["prior"], key=lambda x: x[0])
    held = collections.defaultdict(float)
    lean = 0.0
    for ts, lab, side, size in prior:
        o = lo.get(lab)
        if o is None:
            continue
        held[(o, side)] += float(size)
        if o != mo:
            lean += float(size) * (1 if o > mo else -1) * (1 if side == "Yes" else -1)
    last = None
    if prior and prior[-1][1] in lo:
        last = (lo[prior[-1][1]], prior[-1][2])
    cs = {int(k): v for k, v in e["centres"].items()}
    half = e["rung_c"] / 2
    obs = e.get("tmax_today") if e.get("is_d0") else None
    out = []
    for o in e["order"]:
        o = int(o)
        for side in ("Yes", "No"):
            pr = (e["ask"] if side == "Yes" else e["no_price"]).get(str(o))
            if pr is None or not 0.0 < float(pr) < 1.0:
                continue
            pr = float(pr)
            d = o - mo
            direction = 0 if d == 0 else (1 if d > 0 else -1) * (1 if side == "Yes" else -1)
            m = e["mom1h"].get(str(o))
            m = float(m) if m is not None else 0.0
            p = e["p"].get(str(o)) if e.get("p") else None
            edge = ((float(p) if side == "Yes" else 1 - float(p)) - pr) if p is not None else 0.0
            in_b = obs is not None and (cs[o] - half <= obs < cs[o] + half or (o == min(cs) and obs < cs[o] + half)
                                        or (o == max(cs) and obs >= cs[o] - half))
            above = obs is not None and not in_b and obs >= cs[o] + half and o != max(cs)
            out.append({"o": o, "side": side, "d": max(-3, min(3, d)), "price": pr,
                        "held_same": held[(o, side)] > 0, "held_opp": held[(o, "No" if side == "Yes" else "Yes")] > 0,
                        "last_leg": last == (o, side), "lean_agree": lean != 0 and direction != 0 and (direction > 0) == (lean > 0),
                        "lean_against": lean != 0 and direction != 0 and (direction > 0) != (lean > 0),
                        "mom": m if side == "Yes" else -m, "obs_in": in_b, "obs_above": above, "edge": edge,
                        "chosen": o == e["chosen"]["ord"] and side == e["chosen"]["side"], "mode": mo})
    return out


EV = []
for e in E:
    cs = candidates(e)
    if sum(c["chosen"] for c in cs) == 1:
        EV.append((e["set"], cs, e))
res = [x for x in EV if x[0] == "research"]
fwd = [x for x in EV if x[0] == "forward"]
print(f"events with the chosen leg among the priced candidates: research {len(res)}, forward {len(fwd)}; candidates per event {np.mean([len(c) for _, c, _ in EV]):.1f}")

# layer 1: the gate
print("\n== layer 1, the gate: share of chosen legs inside, research | forward")
GATES = [
    ("within two buckets of the mode", lambda c: abs(c["d"]) <= 2),
    ("+ YES priced 0.05-0.60 or NO priced 0.25-0.85", lambda c: abs(c["d"]) <= 2 and ((c["side"] == "Yes" and 0.05 <= c["price"] <= 0.60) or (c["side"] == "No" and 0.25 <= c["price"] <= 0.85))),
    ("+ not a YES the observed high has already passed", lambda c: abs(c["d"]) <= 2 and ((c["side"] == "Yes" and 0.05 <= c["price"] <= 0.60) or (c["side"] == "No" and 0.25 <= c["price"] <= 0.85)) and not (c["side"] == "Yes" and c["obs_above"])),
]
for name, g in GATES:
    cov = [sum(1 for _, cs, _ in s if any(c["chosen"] and g(c) for c in cs)) / len(s) for s in (res, fwd)]
    size = [np.mean([sum(1 for c in cs if g(c)) for _, cs, _ in s]) for s in (res, fwd)]
    print(f"   {name:52s} covers {cov[0]:.3f} | {cov[1]:.3f}   legs left per event {size[0]:.1f} | {size[1]:.1f}")
gate = GATES[-1][1]

# layer 2: conditional logit inside the gate, then integer points
def X(cs):
    return np.array([[1.0 if f(c) else 0.0 for _, f in COND] for c in cs])


train = [(X([c for c in cs if gate(c)]), [i for i, c in enumerate([c for c in cs if gate(c)]) if c["chosen"]]) for _, cs, _ in res]
train = [(x, ch[0]) for x, ch in train if ch]


def nll(w):
    tot, g = 0.0, np.zeros_like(w)
    for x, k in train:
        z = x @ w
        zmax = z.max()
        p = np.exp(z - zmax)
        p /= p.sum()
        tot -= z[k] - zmax - math.log(np.exp(z - zmax).sum())
        g -= x[k] - p @ x
    return tot + 0.5 * 0.1 * float(w @ w), g + 0.1 * w


w = minimize(nll, np.zeros(len(COND)), jac=True, method="L-BFGS-B").x
scale = 4.0 / max(abs(w))
pts = np.round(w * scale).astype(int)


def rank(cs, weights, integer):
    inside = [c for c in cs if gate(c)]
    if not inside:
        return []
    x = X(inside)
    s = x @ weights
    order = sorted(range(len(inside)), key=lambda i: (-s[i], 0 if inside[i]["side"] == "Yes" else 1,
                                                       -inside[i]["price"] if inside[i]["side"] == "Yes" else 0, abs(inside[i]["d"])))
    return [inside[i] for i in order]


def score(sets, weights, integer):
    out = []
    for s in sets:
        t1 = t3 = sidec = n = 0
        for _, cs, e in s:
            r = rank(cs, weights, integer)
            n += 1
            if not r:
                continue
            t1 += r[0]["chosen"]
            t3 += any(c["chosen"] for c in r[:3])
            ch = next(c for c in cs if c["chosen"])

            def lean_of(c):
                return 0 if c["d"] == 0 else (1 if c["d"] > 0 else -1) * (1 if c["side"] == "Yes" else -1)
            sidec += lean_of(r[0]) == lean_of(ch)
        out.append((t1 / n, t3 / n, sidec / n))
    return out


uni = [np.mean([1 / max(1, sum(1 for c in cs if gate(c))) for _, cs, _ in s]) for s in (res, fwd)]
print(f"\n== layer 2, the points table (pick the leg with most points; weights fitted on research, rounded)")
for (name, _), wi, pi in sorted(zip(COND, w, pts), key=lambda t: -t[2]):
    print(f"   {pi:+d}  {name:44s} (logit {wi:+.2f})")
(r1, r3, rs), (f1, f3, fs) = score((res, fwd), w, False)
(ir1, ir3, irs), (if1, if3, ifs) = score((res, fwd), pts.astype(float), True)
print(f"\n   uniform inside the gate: top-1 {uni[0]:.3f} | {uni[1]:.3f}")
print(f"   logit weights:   top-1 {r1:.3f} | {f1:.3f}   top-3 {r3:.3f} | {f3:.3f}   same side of the mode as the pick {rs:.3f} | {fs:.3f}")
print(f"   integer points:  top-1 {ir1:.3f} | {if1:.3f}   top-3 {ir3:.3f} | {if3:.3f}   same side of the mode as the pick {irs:.3f} | {ifs:.3f}")
print("   (round 26 LightGBM with every feature: forward top-1 0.384, top-3 0.768)")

# where the rule is sure: forward hit rate by the margin of points between first and second
print("\n== how sure: forward top-1 by the points margin between the first and second leg")
buck = collections.defaultdict(lambda: [0, 0])
for s, name in ((res, "research"), (fwd, "forward")):
    for _, cs, e in s:
        inside = [c for c in cs if gate(c)]
        if len(inside) < 2:
            continue
        sc = sorted(((X([c]) @ pts)[0], c) for c in inside)[::-1] if False else None
        vals = sorted([(float(X([c])[0] @ pts), i) for i, c in enumerate(inside)], reverse=True)
        r = rank(cs, pts.astype(float), True)
        margin = vals[0][0] - vals[1][0]
        key = (name, "0" if margin == 0 else "1" if margin <= 1 else "2" if margin <= 2 else "3+")
        buck[key][0] += r[0]["chosen"]
        buck[key][1] += 1
for name in ("research", "forward"):
    print(f"   {name:8s} " + "   ".join(f"margin {m}: {buck[(name, m)][0] / buck[(name, m)][1]:.3f} (n={buck[(name, m)][1]})"
                                         for m in ("0", "1", "2", "3+") if buck[(name, m)][1]))

# what the top-ranked leg type is when the rule is right vs wrong (forward)
print("\n== forward: chosen leg type vs the rule's first pick")
conf = collections.Counter()
for _, cs, e in fwd:
    r = rank(cs, pts.astype(float), True)
    ch = next(c for c in cs if c["chosen"])

    def typ(c):
        return ("mode " if c["d"] == 0 else "off ") + c["side"].upper()
    conf[(typ(ch), typ(r[0]) if r else "-")] += 1
for t in ("off YES", "mode YES", "mode NO", "off NO"):
    row = {p: conf[(t, p)] for p in ("off YES", "mode YES", "mode NO", "off NO")}
    n = sum(row.values())
    print(f"   chosen {t:9s} n={n:3d}  rule picks: " + "  ".join(f"{p} {v}" for p, v in row.items()))


# ------------------------------------------------------------ the same rule as an ordered list (first rule that fires decides)
def lst_pick(cs, variant):
    inside = [c for c in cs if gate(c)]
    if not inside:
        return None, "none"
    dead_no = [c for c in inside if c["side"] == "No" and c["obs_above"]]
    if dead_no:
        return min(dead_no, key=lambda c: abs(c["d"])), "R1 D0 dead-bucket NO"
    held = [c for c in inside if c["held_same"]]
    cheap_held = [c for c in held if c["mom"] < -0.01]
    if cheap_held:
        return min(cheap_held, key=lambda c: c["mom"]), "R2 held leg that just got cheaper"
    if held:
        if variant == "last":
            last = [c for c in held if c["last_leg"]]
            if last:
                return last[0], "R3 its last-traded leg again"
        if variant in ("last", "yes_first"):
            ys = [c for c in held if c["side"] == "Yes"]
            if ys:
                return max(ys, key=lambda c: c["price"]), "R3 held YES (dearest)"
        return max(held, key=lambda c: c["price"]), "R3 held leg (dearest)"
    fresh = [c for c in inside if c["side"] == "Yes" and abs(c["d"]) == 1 and 0.10 <= c["price"] < 0.50]
    if fresh:
        return min(fresh, key=lambda c: (c["mom"], -c["price"])), "R4 fresh shoulder YES (cheapened most)"
    r = rank(cs, pts.astype(float), True)
    return (r[0] if r else None), "R5 points table"


print("\n== the ordered list (variant chosen on research only)")
best = None
for variant in ("last", "yes_first", "dearest"):
    hit = sum(1 for _, cs, _ in res if (p := lst_pick(cs, variant)[0]) is not None and p["chosen"]) / len(res)
    print(f"   variant {variant:10s} research top-1 {hit:.3f}")
    if best is None or hit > best[0]:
        best = (hit, variant)
variant = best[1]
for s, name in ((res, "research"), (fwd, "forward")):
    stat = collections.defaultdict(lambda: [0, 0])
    for _, cs, _ in s:
        p, rule = lst_pick(cs, variant)
        stat[rule][1] += 1
        stat[rule][0] += bool(p and p["chosen"])
    n = len(s)
    tot = sum(v[0] for v in stat.values())
    print(f"   {name} (variant {variant}): top-1 {tot / n:.3f}")
    for rule in sorted(stat):
        h, m = stat[rule]
        print(f"     {rule:42s} fires {100 * m / n:5.1f}%  right {h / m:.3f} (n={m})")
