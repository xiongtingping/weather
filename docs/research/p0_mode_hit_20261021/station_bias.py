import json, os, collections, statistics, math
HERE = os.path.dirname(os.path.abspath(__file__))
R = [json.loads(l) for l in open(os.path.join(HERE, "recs_v2.jsonl"))]
FAM = {"P0>36h首决策": lambda r: r["stage"]=="P0" and r["first_band"] and r["h"]>36,
       "P0-M首决策": lambda r: r["stage"]=="P0-M" and r["first_band"], "P1首决策": lambda r: r["stage"]=="P1" and r["first_band"]}
out = open(os.path.join(HERE, "station_bias.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
def mse(v): 
    if len(v) < 2: return (float("nan"), float("nan"))
    m = statistics.mean(v); return m, statistics.stdev(v)/math.sqrt(len(v))
for fam, pred in FAM.items():
    rows = [r for r in R if pred(r)]
    by = collections.defaultdict(list)
    for r in rows: by[r["st"]].append(r)
    P(f"\n\n######## {fam}: 逐站市场/模型中心偏差（均值桶 − 真值桶，+ 为偏暖；桶单位）########")
    P("站     | 市场偏差 A (t)      市场偏差 B (t)     | 模型偏差 A (t)      模型偏差 B (t)     | 市场两窗同号且|t|>1.5?")
    stable = []
    for st, rr in sorted(by.items()):
        res = {}
        for w in ("A", "B"):
            s = [r for r in rr if r["win"] == w]
            q = [r["mean_q"]-r["yi"] for r in s]; p = [r["mean_p"]-r["yi"] for r in s]
            mq, sq = mse(q); mp, sp = mse(p)
            res[w] = (len(s), mq, mq/sq if sq else float("nan"), mp, mp/sp if sp else float("nan"))
        a, b = res["A"], res["B"]
        flag = ""
        if a[0] >= 8 and b[0] >= 8 and a[1]*b[1] > 0 and abs(a[2]) > 1.5 and abs(b[2]) > 1.5:
            flag = "是 → 市场偏" + ("暖" if a[1] > 0 else "冷"); stable.append((st, "暖" if a[1] > 0 else "冷"))
        P(f"{st:6s} | {a[1]:+.2f} ({a[2]:+.1f}) n{a[0]:3d}   {b[1]:+.2f} ({b[2]:+.1f}) n{b[0]:3d}  | {a[3]:+.2f} ({a[4]:+.1f})        {b[3]:+.2f} ({b[4]:+.1f})        | {flag}")
    P(f"  市场两窗同号显著偏差的站: {stable}")
    # holdout: select station x direction on A, evaluate on B
    D = [r for r in rows if r["dmode"] != 0]
    P(f"\n  —— 站×方向格：窗 A 选（模型独中≥市场独中 且 模型独中≥3），窗 B 验（众数不同行）——")
    sel = []
    for st in by:
        for flag in (1, 0):
            a = [r for r in D if r["st"]==st and r["warm"]==flag and r["win"]=="A"]
            if len(a) < 5: continue
            pa, qa = sum(r["p_hit"] for r in a), sum(r["q_hit"] for r in a)
            if pa >= qa and pa >= 3: sel.append((st, flag, len(a), pa, qa))
    Bsel = [r for r in D if r["win"]=="B" and any(r["st"]==s and r["warm"]==f for s, f, *_ in sel)]
    Ball = [r for r in D if r["win"]=="B"]
    def summ(s):
        if not s: return "n=0"
        ea = [r["p_hit"]-r["ask_pm"] for r in s if r.get("ask_pm") is not None]
        return f"n={len(s)} 模型命中 {statistics.mean(r['p_hit'] for r in s):.1%} 市场命中 {statistics.mean(r['q_hit'] for r in s):.1%} 差 {statistics.mean(r['p_hit']-r['q_hit'] for r in s):+.1%} 按卖一买模型桶 {statistics.mean(ea):+.1%}"
    P(f"   A 选出 {len(sel)} 格: " + ", ".join(f"{s}{'暖' if f else '冷'}({p}/{q}/n{n})" for s, f, n, p, q in sel))
    P(f"   B 里这些格合计: {summ(Bsel)}")
    P(f"   B 里其余:        {summ([r for r in Ball if r not in Bsel])}")
    # reverse: select on B, evaluate on A
    sel2 = []
    for st in by:
        for flag in (1, 0):
            b = [r for r in D if r["st"]==st and r["warm"]==flag and r["win"]=="B"]
            if len(b) < 5: continue
            pb, qb = sum(r["p_hit"] for r in b), sum(r["q_hit"] for r in b)
            if pb >= qb and pb >= 3: sel2.append((st, flag))
    Asel = [r for r in D if r["win"]=="A" and any(r["st"]==s and r["warm"]==f for s, f in sel2)]
    P(f"   反向：B 选出 {len(sel2)} 格 → A 里合计: {summ(Asel)}")
    # market-bias-side rule: model on the side opposite to the market's stable bias
    if stable:
        side = {st: (0 if d == "暖" else 1) for st, d in stable}  # market warm-biased → model colder wins?
        for w in ("A", "B"):
            s = [r for r in D if r["win"]==w and r["st"] in side and r["warm"]==side[r["st"]]]
            P(f"   「模型站在市场偏差的反侧」{w}: {summ(s)}")
            s2 = [r for r in D if r["win"]==w and r["st"] in side and r["warm"]!=side[r["st"]]]
            P(f"   「模型站在市场偏差的同侧」{w}: {summ(s2)}")
