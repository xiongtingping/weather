import json, os, collections, statistics, math, random
HERE = os.path.dirname(os.path.abspath(__file__))
R = [json.loads(l) for l in open(os.path.join(HERE, "recs_v2.jsonl"))]
FAM = {"P0>36h首决策": lambda r: r["stage"]=="P0" and r["first_band"] and r["h"]>36,
       "P0-M首决策": lambda r: r["stage"]=="P0-M" and r["first_band"], "P1首决策": lambda r: r["stage"]=="P1" and r["first_band"],
       "三阶段合并": lambda r: r["first_band"] and (r["stage"]!="P0" or r["h"]>36)}
out = open(os.path.join(HERE, "station_bias_holdout.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
def bias(rows, key):
    v = [r[key]-r["yi"] for r in rows]
    if len(v) < 8: return None
    m = statistics.mean(v); se = statistics.stdev(v)/math.sqrt(len(v))
    return m, (m/se if se else 0.0), len(v)
def summ(s):
    if not s: return "n=0"
    ea = [r["p_hit"]-r["ask_pm"] for r in s if r.get("ask_pm") is not None]
    days = len({(r["st"], r["d"]) for r in s})
    return f"n={len(s)}(站日{days}) 模型命中 {statistics.mean(r['p_hit'] for r in s):.1%} 市场命中 {statistics.mean(r['q_hit'] for r in s):.1%} 差 {statistics.mean(r['p_hit']-r['q_hit'] for r in s):+.1%} 按卖一买模型桶 {statistics.mean(ea):+.1%}"
def boot_ci(s, B=500):
    days = sorted({r["d"] for r in s}); by = collections.defaultdict(list)
    for r in s: by[r["d"]].append(r)
    rnd = random.Random(3); v = []; e = []
    for _ in range(B):
        x = [z for d in (rnd.choice(days) for _ in days) for z in by[d]]
        v.append(statistics.mean(z["p_hit"]-z["q_hit"] for z in x)); e.append(statistics.mean(z["p_hit"]-z["ask_pm"] for z in x if z.get("ask_pm") is not None))
    v.sort(); e.sort(); return f"差CI[{v[int(.025*B)]:+.0%},{v[int(.975*B)]:+.0%}] edgeCI[{e[int(.025*B)]:+.0%},{e[int(.975*B)]:+.0%}]"
for fam, pred in FAM.items():
    rows = [r for r in R if pred(r)]
    P(f"\n######## {fam} ########")
    for fit, test in (("A", "B"), ("B", "A")):
        for tthr in (1.5, 2.0):
            by = collections.defaultdict(list)
            for r in rows:
                if r["win"] == fit: by[r["st"]].append(r)
            bst = {}
            for st, rr in by.items():
                b = bias(rr, "mean_q")
                if b and abs(b[1]) > tthr: bst[st] = 1 if b[0] > 0 else 0  # market warm-biased → 1
            opp = [r for r in rows if r["win"]==test and r["dmode"]!=0 and r["st"] in bst and r["warm"] != bst[r["st"]]]
            same = [r for r in rows if r["win"]==test and r["dmode"]!=0 and r["st"] in bst and r["warm"] == bst[r["st"]]]
            P(f"  {fit}选偏差站(|t|>{tthr}) {len(bst)}站 {sorted((s,'暖' if v else '冷') for s,v in bst.items())}")
            P(f"    {test}验 模型在反侧: {summ(opp)} {boot_ci(opp) if len(opp)>=10 else ''}")
            P(f"    {test}验 模型在同侧: {summ(same)}")
    # also: market bias measured on the SAME window but excluding the decision's own day (leave-one-day-out) — forward-ish per station
    P("  —— 逐站滚动：只用该站该日之前的首决策算市场偏差（t>1.5, n≥8），模型在反侧 ——")
    by = collections.defaultdict(list)
    for r in sorted(rows, key=lambda r: r["d"]): by[r["st"]].append(r)
    opp, same, nob = [], [], 0
    for st, rr in by.items():
        for i, r in enumerate(rr):
            prev = [x for x in rr if x["d"] < r["d"]]
            b = bias(prev, "mean_q")
            if not b or abs(b[1]) <= 1.5: nob += 1; continue
            if r["dmode"] == 0: continue
            (opp if r["warm"] != (1 if b[0] > 0 else 0) else same).append(r)
    P(f"    反侧: {summ(opp)} {boot_ci(opp) if len(opp)>=10 else ''}")
    P(f"    同侧: {summ(same)}")
    oA = [r for r in opp if r["win"]=="A"]; oB = [r for r in opp if r["win"]=="B"]
    P(f"    反侧分窗 A: {summ(oA)} | B: {summ(oB)}")
# day lists for key stations, P0>36h
P("\n######## 逐日：KATL / EPWA / CYYZ / KORD / ZGSZ，P0>36h 首决策（众数不同）########")
for st in ("KATL", "EPWA", "CYYZ", "KORD", "ZGSZ"):
    rr = sorted([r for r in R if r["st"]==st and r["stage"]=="P0" and r["first_band"] and r["h"]>36 and r["dmode"]!=0], key=lambda r: (r["d"], r["h"]))
    P(f"\n  {st}: {len(rr)} 行")
    P("   日期        距结算  模型众数 市场众数 真值  模型暖/冷  结果        p_pm  q_qm  q@模型桶 卖一@模型桶")
    for r in rr:
        res = "模型独中" if r["p_hit"] else ("市场独中" if r["q_hit"] else "都不中")
        P(f"   {r['d']}  {r['h']:5.0f}h   {r['pm']:3d}      {r['qm']:3d}     {r['yi']:3d}   {'暖' if r['warm'] else '冷'}      {res:5s}   {r['p_pm']:.2f}  {r['q_qm']:.2f}   {r['q_at_pm']:.2f}    {r.get('ask_pm') if r.get('ask_pm') is None else round(r['ask_pm'],2)}")
