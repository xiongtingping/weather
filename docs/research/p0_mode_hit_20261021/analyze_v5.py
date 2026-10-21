import json, os, collections, statistics, random
HERE = os.path.dirname(os.path.abspath(__file__))
R = [json.loads(l) for l in open(os.path.join(HERE, "recs_v5.jsonl"))]
for r in R: r["odd"] = (int(r["d"][-2:]) + (31 if r["d"][5:7]=="10" else 0)) % 2
out = open(os.path.join(HERE, "analyze_v5.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
def splits(rows): return {"A":[r for r in rows if r["win"]=="A"], "B":[r for r in rows if r["win"]=="B"], "奇":[r for r in rows if r["odd"]], "偶":[r for r in rows if not r["odd"]]}
def boot(rows, f, B=300):
    days = sorted({r["d"] for r in rows}); by = collections.defaultdict(list)
    for r in rows: by[r["d"]].append(r)
    rnd = random.Random(1); v = []
    for _ in range(B):
        s = [x for d in (rnd.choice(days) for _ in days) for x in by[d]]
        try: v.append(f(s))
        except Exception: pass
    v.sort(); return v[int(.025*len(v))], v[int(.975*len(v))]
def bias(rows): return statistics.mean(r["truth_minus_qmean"] for r in rows)   # + = truth warmer than market mean
def pab(rows): return statistics.mean(int(r["truth_side"] > 0) for r in rows)
def pbe(rows): return statistics.mean(int(r["truth_side"] < 0) for r in rows)
def pnl_ab(rows):
    g = [r for r in rows if r["ab3"]["A"] is not None and r["ab3"]["n"] > 0]; return statistics.mean(r["ab3"]["hit"]-r["ab3"]["A"] for r in g)
def pnl_be(rows):
    g = [r for r in rows if r["be3"]["A"] is not None and r["be3"]["n"] > 0]; return statistics.mean(r["be3"]["hit"]-r["be3"]["A"] for r in g)
surv = []
def cell(name, rows, fam):
    if len(rows) < 40: return
    s4 = splits(rows)
    if any(len(v) < 12 for v in s4.values()): return
    lo, hi = boot(rows, bias)
    b4 = {k: bias(v) for k, v in s4.items()}
    same_sign = (all(x > 0.08 for x in b4.values()) or all(x < -0.08 for x in b4.values()))
    pa, pb_ = pnl_ab(rows), pnl_be(rows); a4 = {k: pnl_ab(v) for k, v in s4.items()}; be4 = {k: pnl_be(v) for k, v in s4.items()}
    flag = ""
    if same_sign: flag += " ★偏置四分割同号"
    if all(x > 0 for x in a4.values()): flag += " ★买暖侧四分割同正"
    if all(x > 0 for x in be4.values()): flag += " ★买冷侧四分割同正"
    P(f"   {name:36s} n={len(rows):4d} 真值−市场均值 {bias(rows):+.3f}桶[{lo:+.2f},{hi:+.2f}] ({' '.join(f'{k}{v:+.2f}' for k,v in b4.items())}) | P暖侧 实{pab(rows):.2f}/市{statistics.mean(r['Qab'] for r in rows):.2f}/模{statistics.mean(r['Mab'] for r in rows):.2f}  P冷侧 实{pbe(rows):.2f}/市{statistics.mean(r['Qbe'] for r in rows):.2f}/模{statistics.mean(r['Mbe'] for r in rows):.2f} | 买暖侧3档@卖一 {pa:+.1%}({' '.join(f'{k}{v:+.0%}' for k,v in a4.items())}) 买冷侧3档 {pb_:+.1%}({' '.join(f'{k}{v:+.0%}' for k,v in be4.items())}){flag}")
    if flag: surv.append((fam, name, len(rows), bias(rows), (lo, hi), pa, pb_, flag))
def terc(rows, k, fam, name=None):
    have = [r for r in rows if isinstance(r.get(k), (int, float))]
    if len(have) < 120: return
    v = sorted(r[k] for r in have)
    if len(set(v)) <= 5:
        for x in sorted(set(v)): cell(f"{name or k}={x}", [r for r in have if r[k]==x], fam)
    else:
        q1, q2 = v[len(v)//3], v[2*len(v)//3]
        cell(f"{name or k} 低≤{q1:.2f}", [r for r in have if r[k]<=q1], fam); cell(f"{name or k} 中", [r for r in have if q1<r[k]<=q2], fam); cell(f"{name or k} 高>{q2:.2f}", [r for r in have if r[k]>q2], fam)
for fam in ("P0>36h", "P0-M", "P1"):
    D = [r for r in R if r["fam"] == fam]
    P(f"\n\n==================== {fam} n={len(D)} ====================")
    cell("全部", D, fam)
    P("--- 模型侧向 ---")
    for s in (1, 0, -1): cell(f"模型众数在市场{'暖' if s>0 else ('冷' if s<0 else '同')}侧", [r for r in D if r["model_side"]==s], fam)
    terc(D, "dmu", fam, "模型均值−市场均值"); terc(D, "side_gap", fam, "模型侧向质量差(暖−冷)对市场"); terc(D, "skew_m", fam, "模型自身偏斜"); terc(D, "sys_above_frac", fam, "六家在市场均值暖侧比例")
    cell("六家全在暖侧", [r for r in D if r.get("sys_above_frac")==1.0], fam); cell("六家全在冷侧", [r for r in D if r.get("sys_above_frac")==0.0], fam)
    P("--- 市场自身 ---")
    terc(D, "skew_q", fam, "市场偏斜(暖−冷质量)"); terc(D, "q_qm", fam, "市场众数价")
    cell("市场众数贴暖边缘", [r for r in D if r["qm_edge_hi"]], fam); cell("市场众数贴冷边缘", [r for r in D if r["qm_edge_lo"]], fam)
    P("--- 站点市场偏差（只用 ≤D−2 已结算日）---")
    cell("站市场偏暖 t>1.5", [r for r in D if (r.get("st_bias_t") or 0) > 1.5], fam); cell("站市场偏冷 t<−1.5", [r for r in D if (r.get("st_bias_t") or 0) < -1.5], fam)
    cell("站市场偏暖 t>2.5", [r for r in D if (r.get("st_bias_t") or 0) > 2.5], fam); cell("站市场偏冷 t<−2.5", [r for r in D if (r.get("st_bias_t") or 0) < -2.5], fam)
    terc(D, "last_err", fam, "该站最近已结算日市场错(+暖)")
    for s in (1, -1): cell(f"最近已结算日市场{'偏暖' if s>0 else '偏冷'}", [r for r in D if r.get("last_err_sign")==s], fam)
    P("--- 持续性 / 天气 / 季节 / 地区 ---")
    for s in (1, 0, -1): cell(f"昨日真值在市场众数{'暖' if s>0 else ('冷' if s<0 else '同')}侧", [r for r in D if r.get("y1_side")==s], fam)
    terc(D, "y1_minus_qm", fam, "昨日真值−市场众数"); terc(D, "clear_anom", fam, "日照距平")
    for s in sorted({r["season"] for r in D if r.get("season")}): cell(f"季节 {s}", [r for r in D if r.get("season")==s], fam)
    for s in ("C", "F"): cell(f"单位 {s}", [r for r in D if r["unit"]==s], fam)
    for s in sorted({r["region"] for r in D}): cell(f"地区 {s}", [r for r in D if r["region"]==s], fam)
    for b in sorted({r["band"] for r in D}): cell(f"时段 {b}", [r for r in D if r["band"]==b], fam)
    if fam == "P0-M":
        P("--- TAF ---")
        for s in (1, 0, -1): cell(f"TAF TX 在市场众数{'暖' if s>0 else ('冷' if s<0 else '同')}侧", [r for r in D if r.get("taf_side")==s], fam)
        terc(D, "taf_minus_qm", fam, "TAF−市场众数")
    if fam == "P1":
        terc(D, "runhigh_minus_qm", fam, "已报高−市场众数")
    P("--- 交互：季节 × 模型侧 ---")
    for s in sorted({r["season"] for r in D if r.get("season")}):
        for ms in (1, -1): cell(f"{s} ∧ 模型在{'暖' if ms>0 else '冷'}侧", [r for r in D if r.get("season")==s and r["model_side"]==ms], fam)
    P("--- 交互：站偏差 × 模型侧 ---")
    for bs, bn in ((1, "站市场偏暖"), (-1, "站市场偏冷")):
        for ms in (1, -1): cell(f"{bn}(t>1.5) ∧ 模型在{'暖' if ms>0 else '冷'}侧", [r for r in D if (r.get("st_bias_t") or 0)*bs > 1.5 and r["model_side"]==ms], fam)
    P("--- 交互：昨日侧 × 模型侧 ---")
    for ys in (1, -1):
        for ms in (1, -1): cell(f"昨日在{'暖' if ys>0 else '冷'}侧 ∧ 模型在{'暖' if ms>0 else '冷'}侧", [r for r in D if r.get("y1_side")==ys and r["model_side"]==ms], fam)
P("\n\n==================== 带 ★ 的格 ====================")
for s in surv: P("  ", s)
