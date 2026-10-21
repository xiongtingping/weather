"""Why does the truth land on the far side?  Shoulder mass vs centre.
Joins rungs_v4 (full p/q per rung) with recs_v5 (lean, dmu) and recs_v2 (per-system sides,
market movement, sharpness).  Output shoulder_v6.txt."""
import json, os, collections, statistics, math, random
HERE = os.path.dirname(os.path.abspath(__file__))
out = open(os.path.join(HERE, "shoulder_v6.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
RG = collections.defaultdict(dict)
for l in open(os.path.join(HERE, "rungs_v4.jsonl")):
    g = json.loads(l); RG[(g["fam"], g["st"], g["d"], g["band"])][g["k"]] = g
R5 = {}
for l in open(os.path.join(HERE, "recs_v5.jsonl")):
    r = json.loads(l); R5[(r["fam"], r["st"], r["d"], r["band"])] = r
R2 = {}
for l in open(os.path.join(HERE, "recs_v2.jsonl")):
    r = json.loads(l)
    if not r["first_band"]: continue
    fam = "P0>36h" if r["stage"] == "P0" and r["h"] > 36 else ("P0≤36h" if r["stage"] == "P0" else r["stage"])
    R2[(fam, r["st"], r["d"], r["band"])] = r
def vec(key):
    rg = RG[key]; K = max(rg) + 1
    p = [rg[k]["p"] for k in range(K)]; q = [rg[k]["q"] for k in range(K)]; yi = next(k for k in range(K) if rg[k]["hit"])
    return p, q, yi, rg[0]["qm"], rg[0]["pm"]
def mean(v): s = sum(v); return sum(i*x for i, x in enumerate(v))/s
def shift(p, delta):
    """Move mass along the rung axis by delta rungs (uniform blocks; ends absorb)."""
    K = len(p); out = [0.0]*K
    for i, m in enumerate(p):
        lo = i + delta; a = math.floor(lo); f = lo - a
        for j, w in ((a, 1-f), (a+1, f)):
            if w <= 0: continue
            j = min(max(j, 0), K-1); out[j] += m*w
    return out
def sides(v, qm, s):
    """mass beyond the market mode on the model's side (near) and the opposite (far), and at the mode."""
    near = sum(v[k] for k in range(len(v)) if (k-qm)*s > 0); far = sum(v[k] for k in range(len(v)) if (k-qm)*s < 0)
    return near, far, v[qm]
def ll(p, y): p = min(max(p, 1e-3), 1-1e-3); return -(y*math.log(p) + (1-y)*math.log(1-p))
for fam in ("P0>36h", "P0-M"):
    rows = []
    for key, r in R5.items():
        if r["fam"] != fam or r["model_side"] == 0 or key not in RG: continue
        p, q, yi, qm, pm = vec(key); s = r["model_side"]
        pn, pf, pq = sides(p, qm, s); qn, qf, qq = sides(q, qm, s)
        yn = int((yi-qm)*s > 0); yf = int((yi-qm)*s < 0); ym = int(yi == qm)
        sh = shift(p, mean(q)-mean(p)); sn, sf, sm = sides(sh, qm, s)
        rows.append(dict(key=key, s=s, dmu=abs(r["dmu"]), pn=pn, pf=pf, pq=pq, qn=qn, qf=qf, qq=qq, yn=yn, yf=yf, ym=ym,
                         sn=sn, sf=sf, sm=sm, p=p, q=q, yi=yi, qm=qm, pm=pm, truth_minus_qmean=r["truth_minus_qmean"],
                         r2=R2.get(key)))
    P(f"\n\n==================== {fam}  模型众数≠市场众数 n={len(rows)} ====================")
    P("A. 侧向质量：近侧=市场众数之外、模型倾斜那一边；远侧=另一边。按 |模型均值−市场均值| 分层")
    P("   |Δμ|层        n   | 近侧 模型  市场  实际 | 远侧 模型  市场  实际 | 众数 模型 市场 实际 | 平移到市场中心后的模型: 近侧  远侧  众数")
    v = sorted(x["dmu"] for x in rows); cuts = [v[len(v)//4], v[len(v)//2], v[3*len(v)//4]]
    bins = [("≤%.2f"%cuts[0], lambda x: x["dmu"] <= cuts[0]), ("%.2f–%.2f"%(cuts[0],cuts[1]), lambda x: cuts[0] < x["dmu"] <= cuts[1]),
            ("%.2f–%.2f"%(cuts[1],cuts[2]), lambda x: cuts[1] < x["dmu"] <= cuts[2]), (">%.2f"%cuts[2], lambda x: x["dmu"] > cuts[2]), ("全部", lambda x: True)]
    for nm, c in bins:
        g = [x for x in rows if c(x)]
        m = lambda k: statistics.mean(x[k] for x in g)
        P(f"   {nm:12s} {len(g):4d} |     {m('pn'):.3f} {m('qn'):.3f} {m('yn'):.3f} |     {m('pf'):.3f} {m('qf'):.3f} {m('yf'):.3f} |    {m('pq'):.3f} {m('qq'):.3f} {m('ym'):.3f} |   {m('sn'):.3f} {m('sf'):.3f} {m('sm'):.3f}")
    P("   读法：模型倾斜那边(近侧)模型给的质量 vs 实际；另一边(远侧)模型给的 vs 实际。平移后若远侧仍低于实际→形状/肩部问题，若对上了→只是中心问题。")
    P("\nB. 最优线性池权重 w（侧向二元 LogLoss 最小，p'=w·模型+(1−w)·市场；w<0 = 模型方向反指示）")
    for nm, c in bins:
        g = [x for x in rows if c(x)]
        best = None
        for w in [i/20 for i in range(-10, 31)]:
            L = statistics.mean(ll(w*x["pf"]+(1-w)*x["qf"], x["yf"]) + ll(w*x["pn"]+(1-w)*x["qn"], x["yn"]) for x in g)
            if best is None or L < best[1]: best = (w, L)
        Lm = statistics.mean(ll(x["qf"], x["yf"]) + ll(x["qn"], x["yn"]) for x in g); Lp = statistics.mean(ll(x["pf"], x["yf"]) + ll(x["pn"], x["yn"]) for x in g)
        P(f"   {nm:12s} n={len(g):4d} 最优 w={best[0]:+.2f}  LogLoss 市场 {Lm:.4f} 模型 {Lp:.4f} 最优池 {best[1]:.4f}")
    P("\nC. 模型自身众数两肩（众数±1 的模型质量 vs 实际频率），按倾斜方向对齐：『市场侧肩』= 朝市场那边，『外侧肩』= 背离市场那边")
    for nm, c in bins:
        g = [x for x in rows if c(x) and 0 < x["pm"] < len(x["p"])-1]
        mk = lambda f: statistics.mean(f(x) for x in g)
        P(f"   {nm:12s} n={len(g):4d} 模型众数: 模{mk(lambda x: x['p'][x['pm']]):.3f} 实{mk(lambda x: int(x['yi']==x['pm'])):.3f} | 市场侧肩: 模{mk(lambda x: x['p'][x['pm']-x['s']]):.3f} 市{mk(lambda x: x['q'][x['pm']-x['s']]):.3f} 实{mk(lambda x: int(x['yi']==x['pm']-x['s'])):.3f} | 外侧肩: 模{mk(lambda x: x['p'][x['pm']+x['s']]):.3f} 市{mk(lambda x: x['q'][x['pm']+x['s']]):.3f} 实{mk(lambda x: int(x['yi']==x['pm']+x['s'])):.3f} | 市场侧肩之外(≥2档朝市场): 模{mk(lambda x: sum(x['p'][k] for k in range(len(x['p'])) if (x['pm']-k)*x['s']>=2)):.3f} 实{mk(lambda x: int((x['pm']-x['yi'])*x['s']>=2)):.3f}")
    P("\nD. 真值−市场均值（桶）按：哪些家族站在模型倾斜侧（recs_v2 side_* = 该家族中心在市场均值的模型侧）")
    def bias_ci(g):
        v = [x["truth_minus_qmean"]*(-x["s"]) for x in g]  # + = truth on the FAR side (contrarian), aligned
        if len(v) < 20: return None
        by = collections.defaultdict(list)
        for x in g: by[x["key"][2]].append(x["truth_minus_qmean"]*(-x["s"]))
        days = list(by); rnd = random.Random(1); bs = []
        for _ in range(300):
            s = [z for d in (rnd.choice(days) for _ in days) for z in by[d]]; bs.append(statistics.mean(s))
        bs.sort(); return statistics.mean(v), bs[7], bs[292], len(v)
    def show(name, g):
        r = bias_ci(g)
        if r: P(f"   {name:46s} n={r[3]:4d} 真值在远侧(反向)距离 {r[0]:+.3f} [{r[1]:+.2f},{r[2]:+.2f}]")
    g2 = [x for x in rows if x["r2"]]
    for sysn in ("ifs", "aifs", "gefs", "gem", "icon", "aigefs"):
        show(f"{sysn} 在模型侧", [x for x in g2 if x["r2"].get(f"side_{sysn}") == 1]); show(f"{sysn} 不在模型侧", [x for x in g2 if x["r2"].get(f"side_{sysn}") == 0])
    for ts in ("ifs", "aifs", "gefs", "gem", "icon", "aigefs"):
        show(f"权重最大家族={ts}", [x for x in g2 if x["r2"].get("top_sys") == ts])
    show("权重最大家族在模型侧", [x for x in g2 if x["r2"].get("top_side_p0") == 1]); show("权重最大家族不在模型侧", [x for x in g2 if x["r2"].get("top_side_p0") == 0])
    for lo, hi in ((0, 0.5), (0.5, 0.99), (0.99, 1.01)):
        show(f"六家在模型侧比例 [{lo},{hi})", [x for x in g2 if x["r2"].get("sys_side_frac") is not None and lo <= x["r2"]["sys_side_frac"] < hi])
    P("\nE. 方向条件再切一层（真值在远侧距离，+ 为反向成立）")
    show("市场自首决策起已朝模型移动 (dmq_first·s>0.1)", [x for x in g2 if (x["r2"].get("mkt_toward_p0_first") or 0) > 0.1])
    show("市场自首决策起背离模型 (<−0.1)", [x for x in g2 if (x["r2"].get("mkt_toward_p0_first") or 0) < -0.1])
    show("市场未动 (|Δ|≤0.1)", [x for x in g2 if x["r2"].get("mkt_toward_p0_first") is not None and abs(x["r2"]["mkt_toward_p0_first"]) <= 0.1])
    show("模型比市场锐 (sharp_ratio<0.8)", [x for x in g2 if (x["r2"].get("sharp_ratio") or 9) < 0.8]); show("模型比市场宽 (≥0.8)", [x for x in g2 if (x["r2"].get("sharp_ratio") or 0) >= 0.8])
    show("模型众数刚换 (model_mode_changed=1)", [x for x in g2 if x["r2"].get("model_mode_changed") == 1]); show("模型众数未换", [x for x in g2 if x["r2"].get("model_mode_changed") == 0])
    show("到货龄<1h", [x for x in g2 if (x["r2"].get("age_h") or 9) < 1]); show("到货龄≥1h", [x for x in g2 if (x["r2"].get("age_h") or 0) >= 1])
    if fam == "P0-M":
        show("TAF 在模型侧", [x for x in g2 if x["r2"].get("taf_closer_p0") == 1]); show("TAF 在市场侧", [x for x in g2 if x["r2"].get("taf_closer_p0") == 0])
        show("无 TAF TX", [x for x in g2 if x["r2"].get("taf_closer_p0") is None])
    show("F 站", [x for x in rows if x["r2"] and x["r2"].get("unit") == "F"]); show("C 站", [x for x in rows if x["r2"] and x["r2"].get("unit") == "C"])
    show("模型暖侧", [x for x in rows if x["s"] == 1]); show("模型冷侧", [x for x in rows if x["s"] == -1])
    show("|众数差|=1", [x for x in rows if abs(x["pm"]-x["qm"]) == 1]); show("|众数差|≥2", [x for x in rows if abs(x["pm"]-x["qm"]) >= 2])
