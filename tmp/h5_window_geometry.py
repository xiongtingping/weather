"""H5 follow-up: is the held window really on the model's centre, and does
distance-from-window explain the targets?  Then the decisive control:
matched on distance AND claimed probability, split targets by edge.
"""
import json, pickle, sys, statistics, collections
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import local_close, parse

C = pickle.load(open("tmp/h5_cache.pkl", "rb"))
pos, truth, tz, chains, books = (C["pos"], C["truth"], C["tz"], C["chains"], C["books"])
RANK = {"P1": 2, "P0-M": 1, "P0": 0}
ALIVE = 0.10

def asks_at(k, when):
    h = books.get(k)
    if not h: return {}
    i = bisect_right([x[0] for x in h], when)
    return h[i-1][1] if i else {}

sel = {}
for k, e in pos.items():
    if k not in truth or k not in chains or k not in tz: continue
    close = local_close(k[1], tz[k]); best = None
    for stage, at, env in chains[k]:
        if at < e["opened"] or parse(at) >= close: continue
        if sum(v for b, v in env["probabilities"].items() if b in e["held"]) <= ALIVE:
            continue
        r = RANK.get(stage, 0)
        if best is None or (r, at) >= (best[0], best[1]): best = (r, at, stage, env)
    if best: sel[k] = best

allb, targets = [], []
mode_in_window = 0; tot = 0
for k, (_, at, stage, env) in sel.items():
    p, o = env["probabilities"], env["bucket_ordinals"]
    tb = truth[k][0]
    if tb not in o: continue
    mode = max(p, key=p.get); mo = o[mode]
    held = sorted(o[b] for b in pos[k]["held"] if b in o)
    if not held: continue
    tot += 1
    if held[0] <= mo <= held[-1]: mode_in_window += 1
    asks = asks_at(k, at)
    def dh(x):
        return 0 if held[0] <= x <= held[-1] else (x - held[-1] if x > held[-1] else x - held[0])
    for b, v in p.items():
        if b not in o: continue
        allb.append({"k": k, "b": b, "p": v, "d": o[b]-mo, "dh": dh(o[b]),
                     "won": b == tb, "ask": asks.get(b), "held": b in pos[k]["held"]})
    outside = {b: v for b, v in p.items() if b not in pos[k]["held"] and b in o}
    if not outside: continue
    t = max(outside, key=outside.get); ask = asks.get(t)
    if ask is None: continue
    targets.append({"k": k, "b": t, "p": outside[t], "ask": ask,
                    "edge": outside[t]-ask, "d": o[t]-mo, "dh": dh(o[t]),
                    "won": t == tb, "modew": held[0] <= mo <= held[-1],
                    "nheld": len(held)})

print("=== A. H5 前提检查：持仓窗是不是压在模型众数上？ ===")
print("  站日 n=%d   众数落在持仓窗内: %d (%.0f%%)" % (tot, mode_in_window, 100*mode_in_window/tot))
print("  这 104 个标的里，众数在窗内的站日: %d/%d" % (
    sum(t["modew"] for t in targets), len(targets)))
print("  标的本身就是众数 (d=0，即众数在窗外): %d" % sum(1 for t in targets if t["d"] == 0))
print("  窗宽中位: %.0f 个桶" % statistics.median(t["nheld"] for t in targets))

def rel(name, s):
    if not s:
        print("  %-30s (none)" % name); return
    c = sum(r["p"] for r in s)/len(s); a = sum(r["won"] for r in s)/len(s)
    print("  %-30s n=%-4d 声称 %5.1f%%  实际 %5.1f%%  比 %s" % (
        name, len(s), 100*c, 100*a, ("%.2f" % (a/c)) if c else "—"))

print("\n=== B. 按'离持仓窗边缘的距离'（H5 说的那个几何量）分箱：全部窗外桶 ===")
for d in (1, 2, 3, 4):
    rel("窗外高侧 dh=+%d" % d, [r for r in allb if r["dh"] == d])
for d in (1, 2, 3, 4):
    rel("窗外低侧 dh=-%d" % d, [r for r in allb if r["dh"] == -d])
rel("窗外高侧 dh>=+1", [r for r in allb if r["dh"] >= 1])
rel("窗外低侧 dh<=-1", [r for r in allb if r["dh"] <= -1])
print("  高边际标的的 dh 分布: %s" % dict(sorted(
    collections.Counter(t["dh"] for t in targets if t["edge"] > .25).items())))

print("\n=== C. 决定性对照：|d|>=1 且 声称>=0.15 的桶，按'市场怎么看'切开 ===")
pool = [r for r in allb if abs(r["d"]) >= 1 and r["p"] >= .15]
tgk = {(t["k"], t["b"]): t for t in targets}
for r in pool:
    t = tgk.get((r["k"], r["b"]))
    r["edge"] = t["edge"] if t else ((r["p"] - r["ask"]) if r["ask"] else None)
    r["is_t"] = t is not None
rel("全部", pool)
rel("是标的 & 边际>0.25", [r for r in pool if r["is_t"] and r["edge"] is not None and r["edge"] > .25])
rel("是标的 & 边际<=0.25", [r for r in pool if r["is_t"] and (r["edge"] is None or r["edge"] <= .25)])
rel("非标的 & 边际>0.25", [r for r in pool if not r["is_t"] and r["edge"] is not None and r["edge"] > .25])
rel("非标的 & 边际<=0.25", [r for r in pool if not r["is_t"] and r["edge"] is not None and r["edge"] <= .25])
rel("非标的 & 无盘口", [r for r in pool if not r["is_t"] and r["ask"] is None])

print("\n=== D. 同样的切法，但完全不看'是不是标的'，只看边际（全部桶，含窗内）===")
have = [r for r in allb if r["ask"] is not None]
for r in have: r["e"] = r["p"] - r["ask"]
for nm, lo, hi in (("边际<=0", -9, 0), ("0~0.10", 0, .10),
                   ("0.10~0.25", .10, .25), (">0.25", .25, 9)):
    rel(nm, [r for r in have if lo < r["e"] <= hi])
print("  --- 只看窗外桶 ---")
for nm, lo, hi in (("边际<=0", -9, 0), ("0~0.10", 0, .10),
                   ("0.10~0.25", .10, .25), (">0.25", .25, 9)):
    rel(nm, [r for r in have if not r["held"] and lo < r["e"] <= hi])
