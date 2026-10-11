"""Round 31 (熊先生 10-11 「簇对标在什么条件下选众数yes，什么时侯选no？有确定性结论吗」).

The 2,103 first-taker events (research 1,491 up to 10-07, forward 612 from
10-08; replicate_rows.json.gz).  Leg type of the first take, relative to the
market mode at that minute: mode YES / mode NO / off-mode YES / off-mode NO.

(1) On the mode bucket: what decides YES vs NO.  Candidate conditions, each a
    table research vs forward: the cluster's own position on that bucket before
    the event (YES / NO / none), the mode's price q, its lead over the second
    bucket, its one-hour move, D0 observed high against the mode, lead time,
    our p against q, size.  Then single rules and a depth-2 tree fitted on the
    research set and scored on the forward set against the base rate.
(2) Where NO goes and why: NO first takes on a bucket the cluster already
    holds YES on (exit/merge) vs fresh NO; position of fresh NO.
(3) What each leg type earned (per share at its price, settled events).
"""
import collections
import gzip
import json
import math
import sys
from pathlib import Path

B = Path(sys.argv[1])
E = json.load(gzip.open(B / "replicate_rows.json.gz", "rt"))


def held(e, ord_, side):
    lab = e["labels"][str(ord_)]
    return sum(float(x[3]) for x in e["prior"] if x[1] == lab and x[2] == side)


def leg(e):
    c, mo = e["chosen"], e["mode_ord"]
    on = c["ord"] == mo
    return ("mode " if on else "off ") + ("YES" if c["side"] == "Yes" else "NO")


rows = []
for e in E:
    mo = e["mode_ord"]
    q = {int(k): v for k, v in e["q"].items()}
    others = sorted((v for k, v in q.items() if k != mo), reverse=True)
    obs_rel = None
    if e.get("is_d0") and e.get("tmax_today") is not None:
        cs = {int(k): v for k, v in e["centres"].items()}
        half = e["rung_c"] / 2
        o = min(cs, key=lambda k: abs(cs[k] - e["tmax_today"]))
        if e["tmax_today"] < cs[min(cs)] - half:
            o = min(cs)
        obs_rel = o - mo
    hy, hn = held(e, mo, "Yes"), held(e, mo, "No")
    # the cluster's lean before the event: YES held above vs below the mode (NO counts the other way)
    lean = 0.0
    for x in e["prior"]:
        o = next((int(k) for k, v in e["labels"].items() if v == x[1]), None)
        if o is None or o == mo:
            continue
        s = 1 if o > mo else -1
        lean += float(x[3]) * s * (1 if x[2] == "Yes" else -1)
    rows.append({
        "set": e["set"], "ed": e["ed"], "leg": leg(e), "side": e["chosen"]["side"], "on_mode": e["chosen"]["ord"] == mo,
        "q": q[mo], "lead2": q[mo] - (others[0] if others else 0.0), "mom": float(e["mom1h"].get(str(mo)) or 0.0),
        "obs_rel": obs_rel, "is_d0": bool(e.get("is_d0")), "lead_h": float(e["lead"]), "usdc": float(e["usdc"]),
        "pq": (float(e["p"][str(mo)]) - q[mo]) if e.get("p") and e["p"].get(str(mo)) is not None else float("nan"),
        "hold": "holds YES" if hy > 0 and hy >= hn else "holds NO" if hn > 0 else "none",
        "lean": lean, "truth_ord": e.get("truth_ord"), "chosen_ord": e["chosen"]["ord"], "mode_ord": mo,
        "price": float((e["ask"] if e["chosen"]["side"] == "Yes" else e["no_price"]).get(str(e["chosen"]["ord"])) or float("nan")),
        "unit": e["unit"], "M": e.get("M"),
    })

print("== leg type of the first take")
for s in ("research", "forward"):
    c = collections.Counter(r["leg"] for r in rows if r["set"] == s)
    n = sum(c.values())
    print(f"   {s:8s} n={n}  " + "  ".join(f"{k} {100 * v / n:.1f}%" for k, v in sorted(c.items())))

M_ = [r for r in rows if r["on_mode"]]
print(f"\n== (1) on the mode bucket: share NO (research | forward), n in brackets.  overall research {sum(r['side'] == 'No' for r in M_ if r['set'] == 'research') / max(1, sum(r['set'] == 'research' for r in M_)):.2f}"
      f" forward {sum(r['side'] == 'No' for r in M_ if r['set'] == 'forward') / max(1, sum(r['set'] == 'forward' for r in M_)):.2f}")


def table(name, fn, cats):
    cells = []
    for lab, test in cats:
        out = []
        for s in ("research", "forward"):
            sel = [r for r in M_ if r["set"] == s and test(fn(r))]
            out.append(f"{sum(r['side'] == 'No' for r in sel) / len(sel):.2f} ({len(sel)})" if sel else "-")
        cells.append(f"{lab}: {out[0]} | {out[1]}")
    print(f"   {name:24s} " + "   ".join(cells))


table("own position on it", lambda r: r["hold"], [("holds YES", lambda v: v == "holds YES"), ("holds NO", lambda v: v == "holds NO"), ("none", lambda v: v == "none")])
table("mode price q", lambda r: r["q"], [("<0.35", lambda v: v < 0.35), ("0.35-0.55", lambda v: 0.35 <= v < 0.55), (">=0.55", lambda v: v >= 0.55)])
table("lead over 2nd", lambda r: r["lead2"], [("<0.10", lambda v: v < 0.10), ("0.10-0.30", lambda v: 0.10 <= v < 0.30), (">=0.30", lambda v: v >= 0.30)])
table("1h move of mode YES", lambda r: r["mom"], [("fell >1c", lambda v: v < -0.01), ("flat", lambda v: -0.01 <= v <= 0.01), ("rose >1c", lambda v: v > 0.01)])
table("D0 observed high", lambda r: r["obs_rel"], [("below mode", lambda v: v is not None and v < 0), ("in mode", lambda v: v == 0), ("above mode", lambda v: v is not None and v > 0), ("not D0", lambda v: v is None)])
table("hours to settlement", lambda r: r["lead_h"], [("<12", lambda v: v < 12), ("12-24", lambda v: 12 <= v < 24), ("24-48", lambda v: 24 <= v < 48), (">=48", lambda v: v >= 48)])
table("our p - q", lambda r: r["pq"], [("p<q-0.1", lambda v: v < -0.1), ("|p-q|<=0.1", lambda v: abs(v) <= 0.1), ("p>q+0.1", lambda v: v > 0.1)])
table("size", lambda r: r["usdc"], [("<20U", lambda v: v < 20), ("20-100U", lambda v: 20 <= v < 100), (">=100U", lambda v: v >= 100)])
table("lean off the mode before", lambda r: r["lean"], [("none", lambda v: v == 0), ("some", lambda v: v != 0)])

# single rules and a depth-2 tree: fit on research, score on forward
FEATS = {
    "holds YES": lambda r: r["hold"] == "holds YES",
    "holds NO": lambda r: r["hold"] == "holds NO",
    "q>=0.45": lambda r: r["q"] >= 0.45, "q>=0.55": lambda r: r["q"] >= 0.55, "q>=0.35": lambda r: r["q"] >= 0.35,
    "lead2>=0.2": lambda r: r["lead2"] >= 0.2, "lead2>=0.3": lambda r: r["lead2"] >= 0.3,
    "mom>0.01": lambda r: r["mom"] > 0.01, "mom<-0.01": lambda r: r["mom"] < -0.01, "mom>0": lambda r: r["mom"] > 0,
    "obs in mode": lambda r: r["obs_rel"] == 0, "obs above mode": lambda r: r["obs_rel"] is not None and r["obs_rel"] > 0,
    "lead<24h": lambda r: r["lead_h"] < 24, "p<q": lambda r: r["pq"] < 0, "usdc>=100": lambda r: r["usdc"] >= 100,
}
res = [r for r in M_ if r["set"] == "research"]
fwd = [r for r in M_ if r["set"] == "forward"]


def acc(rule, sel):
    return sum((("No" if rule(r) else "Yes") == r["side"]) for r in sel) / len(sel)


base_side = "No" if sum(r["side"] == "No" for r in res) >= len(res) / 2 else "Yes"
print(f"\n   base rate (always {base_side}): research {sum(r['side'] == base_side for r in res) / len(res):.3f}  forward {sum(r['side'] == base_side for r in fwd) / len(fwd):.3f}")
print("   single rules 'NO if <feature> else YES' (and the reverse), research -> forward accuracy:")
scored = []
for name, f in FEATS.items():
    for neg in (False, True):
        rule = (lambda r, f=f: not f(r)) if neg else f
        scored.append((acc(rule, res), acc(rule, fwd), ("not " if neg else "") + name))
for a, b, n in sorted(scored, reverse=True)[:8]:
    print(f"     NO if {n:20s} research {a:.3f}  forward {b:.3f}")


def best_split(sel, used=()):
    best = None
    for name, f in FEATS.items():
        if name in used:
            continue
        L = [r for r in sel if f(r)]
        R = [r for r in sel if not f(r)]
        if len(L) < 15 or len(R) < 15:
            continue
        sL = "No" if sum(r["side"] == "No" for r in L) >= len(L) / 2 else "Yes"
        sR = "No" if sum(r["side"] == "No" for r in R) >= len(R) / 2 else "Yes"
        a = (sum(r["side"] == sL for r in L) + sum(r["side"] == sR for r in R)) / len(sel)
        if best is None or a > best[0]:
            best = (a, name, f, sL, sR)
    return best


a1, n1, f1, _, _ = best_split(res)
leaves = {}
for branch, test in ((True, f1), (False, lambda r, f1=f1: not f1(r))):
    sub = [r for r in res if test(r)]
    sp = best_split(sub, (n1,))
    if sp:
        leaves[branch] = sp
print(f"\n   depth-2 tree fitted on research: first split '{n1}'")


def tree(r):
    br = f1(r)
    a, n, f, sL, sR = leaves[br]
    return sL if f(r) else sR


for br, (a, n, f, sL, sR) in leaves.items():
    print(f"     {n1}={br}: then '{n}' -> {sL} / else {sR}")
print(f"   tree accuracy: research {sum(tree(r) == r['side'] for r in res) / len(res):.3f}  forward {sum(tree(r) == r['side'] for r in fwd) / len(fwd):.3f}")

# (2) NO first takes: exit (already holds YES on that bucket) vs fresh
print("\n== (2) NO first takes")
for s in ("research", "forward"):
    nos = [r for r in rows if r["set"] == s and r["side"] == "No"]
    ex = [r for r in nos if r["hold"] == "holds YES"] if False else None
    exit_ = []
    fresh = []
    for e, r in zip([x for x in E if x["set"] == s and x["chosen"]["side"] == "No"], nos):
        (exit_ if held(e, e["chosen"]["ord"], "Yes") > 0 else fresh).append(r)
    pos = collections.Counter(max(-2, min(2, r["chosen_ord"] - r["mode_ord"])) for r in fresh)
    nf = max(1, len(fresh))
    print(f"   {s:8s} NO first takes {len(nos)}: on a bucket it already holds YES on {len(exit_)} ({100 * len(exit_) / max(1, len(nos)):.0f}%);"
          f" fresh {len(fresh)} -> position vs mode " + "  ".join(f"{k:+d}: {100 * v / nf:.0f}%" for k, v in sorted(pos.items())))

# (3) what each leg type earned
print("\n== (3) per-share result at its own price (settled events), research | forward")
for lg in ("mode YES", "mode NO", "off YES", "off NO"):
    cells = []
    for s in ("research", "forward"):
        sel = [r for r in rows if r["set"] == s and r["leg"] == lg and r["truth_ord"] is not None and r["price"] == r["price"]]
        if not sel:
            cells.append("-")
            continue
        win = [(1.0 if (r["truth_ord"] == r["chosen_ord"]) == (r["side"] == "Yes") else 0.0) for r in sel]
        pnl = [w - r["price"] for w, r in zip(win, sel)]
        cells.append(f"n={len(sel)} price {sum(r['price'] for r in sel) / len(sel):.3f} hit {sum(win) / len(win):.3f} -> {100 * sum(pnl) / len(pnl):+.1f} c")
    print(f"   {lg:9s} {cells[0]}   |   {cells[1]}")

print("\n== (4) the two strongest conditions together: share NO on the mode bucket, research | forward")
for hold in ("holds YES", "holds NO", "none"):
    cells = []
    for lab, test in (("fell >1c", lambda v: v < -0.01), ("flat", lambda v: -0.01 <= v <= 0.01), ("rose >1c", lambda v: v > 0.01)):
        out = []
        for s in ("research", "forward"):
            sel = [r for r in M_ if r["set"] == s and r["hold"] == hold and test(r["mom"])]
            out.append(f"{sum(r['side'] == 'No' for r in sel) / len(sel):.2f} ({len(sel)})" if sel else "-")
        cells.append(f"{lab}: {out[0]} | {out[1]}")
    print(f"   {hold:10s} " + "   ".join(cells))
print("   D0 only, observed high vs mode x 1h move:")
for lab_o, to in (("obs below mode", lambda v: v is not None and v < 0), ("obs in mode", lambda v: v == 0)):
    cells = []
    for lab, test in (("fell/flat", lambda v: v <= 0.01), ("rose >1c", lambda v: v > 0.01)):
        out = []
        for s in ("research", "forward"):
            sel = [r for r in M_ if r["set"] == s and to(r["obs_rel"]) and test(r["mom"])]
            out.append(f"{sum(r['side'] == 'No' for r in sel) / len(sel):.2f} ({len(sel)})" if sel else "-")
        cells.append(f"{lab}: {out[0]} | {out[1]}")
    print(f"   {lab_o:15s} " + "   ".join(cells))
