"""H3 measurement: how much does P1's likelihood ratio move each bucket, as a
function of how far that bucket sits above the running high / the centre?

Input: tmp/h3_cases.json (built by tmp/h3_far_tail_likelihood.py) -- one row
per settled station-day with a paper position, stage-aware belief, lead>=0,
plus the REAL proof's likelihood_ratio_by_bucket (not a re-implementation).
"""
import json, math, statistics
from collections import Counter, defaultdict

rows = json.load(open("tmp/h3_cases.json"))
for r in rows:
    r["excluded"] = set(r["excluded"]); r["held"] = set(r["held"])
    r["edge"] = r["p_t"] - r["ask"]

BINS = (("<=0", lambda e: e <= 0), ("0~0.10", lambda e: 0 < e <= .10),
        ("0.10-0.25", lambda e: .10 < e <= .25), (">0.25", lambda e: e > .25))

print("=== 1  这些行里 P1 的软似然到底开了没有")
print("  边际档        n    strength=0(似然恒等)   strength中位   模式")
for name, f in BINS:
    sel = [r for r in rows if f(r["edge"])]
    z = [r for r in sel if r["strength"] == 0.0]
    print("  %-11s %3d   %3d (%3.0f%%)          %.3f       %s" % (
        name, len(sel), len(z), 100*len(z)/len(sel),
        statistics.median(r["strength"] for r in sel),
        Counter(r["mode"].split("_SOFT")[0] for r in sel).most_common(1)[0][0][:34]))

print("\n=== 2  似然比的实际取值（只看未被硬下界砍掉的桶）")
allb = []
for r in rows:
    for b, v in r["lr"].items():
        if b in r["excluded"]:
            continue
        c = r["centers"].get(b)
        if c is None:
            continue
        allb.append({"lr": float(v), "d": c - r["rh"], "s": r["strength"],
                     "p": r["p"][b], "prior": (r["prior"] or {}).get(b),
                     "is_t": b == r["target"], "won": b == r["truth"],
                     "edge": r["edge"]})
on = [x for x in allb if x["s"] > 0]
off = [x for x in allb if x["s"] == 0]
print("  似然恒等(=1.0)的桶: %d / %d = %.0f%%" % (
    len(off), len(allb), 100*len(off)/len(allb)))
print("  似然真的动了的桶: %d，取值区间 [%.3f, %.3f]" % (
    len(on), min(x["lr"] for x in on), max(x["lr"] for x in on)))

print("\n=== 3  H3 的核心问句：似然比随'离已测高点多远'怎么变（只看 strength>0）")
print("  离running high    n    似然比均值   =该行下限的占比   раw比(还原strength前)")
for lo, hi in ((-99, 0), (0, 1), (1, 2), (2, 3), (3, 5), (5, 99)):
    sel = [x for x in on if lo <= x["d"] < hi]
    if not sel:
        continue
    raw = [x["lr"] ** (1.0 / x["s"]) for x in sel]
    at_floor = sum(1 for v in raw if v <= 0.2501)
    print("  %+5.1f ~ %+5.1f°C  %4d    %.3f        %3.0f%%            中位 %.3f" % (
        lo, hi, len(sel), statistics.mean(x["lr"] for x in sel),
        100*at_floor/len(sel), statistics.median(raw)))

print("\n  同一行内似然比的最大/最小之比（这一步能把分布重排多少）")
sp = []
for r in rows:
    if r["strength"] <= 0:
        continue
    vals = [float(v) for b, v in r["lr"].items() if b not in r["excluded"]]
    if len(vals) > 1 and min(vals) > 0:
        sp.append(max(vals) / min(vals))
if sp:
    print("    n=%d  中位 %.2f×  最大 %.2f×  （理论上限 16^strength）" % (
        len(sp), statistics.median(sp), max(sp)))

print("\n=== 4  目标桶（模型最看好的窗外桶）在哪儿")
print("  边际档       n   在已测高点之上   离高点中位   目标桶似然比中位   后验/先验中位")
for name, f in BINS:
    sel = [r for r in rows if f(r["edge"])]
    ds, lrs, pp = [], [], []
    above = 0
    for r in sel:
        c = r["centers"].get(r["target"])
        if c is None:
            continue
        ds.append(c - r["rh"])
        above += c > r["rh"]
        lrs.append(float(r["lr"].get(r["target"], 1.0)))
        pr = (r["prior"] or {}).get(r["target"])
        if pr:
            pp.append(r["p"][r["target"]] / pr)
    print("  %-10s %3d   %3.0f%%          %+.1f°C       %.3f            %.2f" % (
        name, len(sel), 100*above/max(len(ds),1),
        statistics.median(ds), statistics.median(lrs),
        statistics.median(pp) if pp else float("nan")))

print("\n=== 5  边际>0.25 的 19 例逐条")
print("  站/日            目标   高点  目标中心  Δ°C  strength  似然比  先验→后验   真值")
for r in sorted([r for r in rows if r["edge"] > .25], key=lambda x: -x["edge"]):
    c = r["centers"].get(r["target"])
    pr = (r["prior"] or {}).get(r["target"])
    print("  %-16s %-6s %4.0f  %6.1f  %+4.1f  %.3f    %.3f   %.3f→%.3f  %s" % (
        r["k"][0] + " " + r["k"][1][5:], r["target"], r["rh"],
        c if c is not None else float("nan"),
        (c - r["rh"]) if c is not None else float("nan"),
        r["strength"], float(r["lr"].get(r["target"], 1.0)),
        pr if pr else float("nan"), r["p"][r["target"]], r["truth"]))
