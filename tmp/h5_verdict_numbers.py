"""H5 verdict: can distance-conditional calibration alone predict 0/17?
Plus the centre-bias number behind the high/low asymmetry."""
import json, pickle, sys, statistics, collections, math
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import local_close, parse

C = pickle.load(open("tmp/h5_cache.pkl", "rb"))
pos, truth, tz, chains, books = (C["pos"], C["truth"], C["tz"], C["chains"], C["books"])
RANK = {"P1": 2, "P0-M": 1, "P0": 0}; ALIVE = 0.10
def asks_at(k, w):
    h = books.get(k)
    if not h: return {}
    i = bisect_right([x[0] for x in h], w); return h[i-1][1] if i else {}
sel = {}
for k, e in pos.items():
    if k not in truth or k not in chains or k not in tz: continue
    close = local_close(k[1], tz[k]); best = None
    for stage, at, env in chains[k]:
        if at < e["opened"] or parse(at) >= close: continue
        if sum(v for b, v in env["probabilities"].items() if b in e["held"]) <= ALIVE: continue
        r = RANK.get(stage, 0)
        if best is None or (r, at) >= (best[0], best[1]): best = (r, at, stage, env)
    if best: sel[k] = best

allb, targets, bias = [], [], []
for k, (_, at, stage, env) in sel.items():
    p, o = env["probabilities"], env["bucket_ordinals"]; tb = truth[k][0]
    if tb not in o: continue
    mode = max(p, key=p.get); mo = o[mode]
    bias.append(o[tb] - mo)
    asks = asks_at(k, at)
    for b, v in p.items():
        if b in o: allb.append({"p": v, "d": o[b]-mo, "won": b == tb})
    outside = {b: v for b, v in p.items() if b not in pos[k]["held"] and b in o}
    if not outside: continue
    t = max(outside, key=outside.get); a = asks.get(t)
    if a is None: continue
    targets.append({"p": outside[t], "edge": outside[t]-a, "d": o[t]-mo, "won": t == tb})

def ratio(pred):
    s = [r for r in allb if pred(r)]
    c = sum(r["p"] for r in s); a = sum(r["won"] for r in s)
    return (a/c if c else 0.0), len(s), c, a

print("=== E. 距离条件校准能不能自己预测出 0/17？ ===")
bins = {0: lambda r: r["d"] == 0, 1: lambda r: abs(r["d"]) == 1,
        2: lambda r: abs(r["d"]) == 2, 3: lambda r: abs(r["d"]) == 3,
        4: lambda r: abs(r["d"]) >= 4}
rt = {}
for b, f in bins.items():
    rr, n, c, a = ratio(f); rt[b] = rr
    print("  |d|=%s 修正系数 %.2f (n=%d, 声称和 %.1f, 实际 %d)" % (
        "4+" if b == 4 else b, rr, n, c, a))
hot = [t for t in targets if t["edge"] > .25]
exp_raw = sum(t["p"] for t in hot)
exp_cal = sum(t["p"] * rt[min(abs(t["d"]), 4)] for t in hot)
obs = sum(t["won"] for t in hot)
print("  高边际标的 n=%d：直接按声称的期望命中 %.2f 个；按'距离修正后'的期望命中 %.2f 个；实际 %d 个"
      % (len(hot), exp_raw, exp_cal, obs))
def pois(lam, k=0): return math.exp(-lam)
print("  若距离修正就是全部真相，观测到 0 命中的概率 ≈ %.4f (Poisson λ=%.2f)"
      % (pois(exp_cal), exp_cal))
print("  |d|>=3 的高边际标的只有 %d/%d 个 -> 远尾几何最多解释 %.0f%%"
      % (sum(1 for t in hot if abs(t["d"]) >= 3), len(hot),
         100*sum(1 for t in hot if abs(t["d"]) >= 3)/len(hot)))

print("\n=== F. 高/低不对称的来源：众数本身偏热 ===")
print("  真值序号 - 众数序号：均值 %+.2f 桶，中位 %+.0f，n=%d" % (
    statistics.mean(bias), statistics.median(bias), len(bias)))
print("  分布 %s" % dict(sorted(collections.Counter(bias).items())))
print("  真值落在众数下方 %.0f%%，上方 %.0f%%" % (
    100*sum(1 for b in bias if b < 0)/len(bias),
    100*sum(1 for b in bias if b > 0)/len(bias)))
