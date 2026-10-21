import json, os, collections, statistics, random
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
HERE = os.path.dirname(os.path.abspath(__file__))
D = json.load(open(os.path.join(HERE, "disagree.json")))
F = ["h","p_pm","q_qm","q_at_pm","p_at_qm","q_second","dmu","absdmu","sd_p","sd_q","ent_q","sys_sd","sys_side_frac","n_sys",
     "spread_qm","spread_pm","maxsp","ms","bridge_gap","clear","clear_anom","age_h","gap_q","gap_p","sharp_ratio","absdmode","warm","qm_edge"]
def X(rows): return np.array([[(r.get(k) if r.get(k) is not None else np.nan) for k in F] for r in rows], float)
A = [r for r in D if r["win"]=="A"]; B = [r for r in D if r["win"]=="B"]
def boot_diff(rows, B_=400, seed=1):
    days = sorted({r["d"] for r in rows}); by = collections.defaultdict(list)
    for r in rows: by[r["d"]].append(r)
    rnd = random.Random(seed); v = []
    for _ in range(B_):
        s = [x for d in (rnd.choice(days) for _ in days) for x in by[d]]
        v.append(sum(x["p_hit"]-x["q_hit"] for x in s)/len(s))
    v.sort(); return v[int(.025*len(v))], v[int(.975*len(v))]
def ev(name, rows):
    d = statistics.mean(r["p_hit"]-r["q_hit"] for r in rows); e = statistics.mean(r["p_hit"]-r["q_at_pm"] for r in rows)
    lo, hi = boot_diff(rows); print(f"  {name:28s} n={len(rows):4d} P0 {statistics.mean(r['p_hit'] for r in rows):5.1%} 市 {statistics.mean(r['q_hit'] for r in rows):5.1%} 差{d:+6.1%}[{lo:+.0%},{hi:+.0%}] 买P0桶edge{e:+6.1%}")
# target: P0 hit (vs not) and market hit; score = P(p_hit) - P(q_hit) predicted
XA, XB = X(A), X(B)
med = np.nanmedian(XA, 0); XA = np.where(np.isnan(XA), med, XA); XB = np.where(np.isnan(XB), med, XB)
yA_p = np.array([r["p_hit"] for r in A]); yA_q = np.array([r["q_hit"] for r in A])
for nm, mk in [("GBM", lambda: GradientBoostingClassifier(n_estimators=150, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=0)),
               ("Logit", lambda: LogisticRegression(C=0.3, max_iter=2000))]:
    mp, mq = mk().fit(XA, yA_p), mk().fit(XA, yA_q)
    mu, sd = XA.mean(0), XA.std(0)+1e-9
    if nm == "Logit":
        mp, mq = mk().fit((XA-mu)/sd, yA_p), mk().fit((XA-mu)/sd, yA_q)
        sA = mp.predict_proba((XA-mu)/sd)[:,1]-mq.predict_proba((XA-mu)/sd)[:,1]; sB = mp.predict_proba((XB-mu)/sd)[:,1]-mq.predict_proba((XB-mu)/sd)[:,1]
    else:
        sA = mp.predict_proba(XA)[:,1]-mq.predict_proba(XA)[:,1]; sB = mp.predict_proba(XB)[:,1]-mq.predict_proba(XB)[:,1]
    print(f"\n== {nm}: 窗A拟合 → 窗B前推, 按预测(P0命中−市场命中)分位 ==")
    for lab, s, rows in [("A(样本内)", sA, A), ("B(前推)", sB, B)]:
        order = np.argsort(-s); n = len(rows)
        for q, (a, b) in [("最好10%", (0, n//10)), ("最好25%", (0, n//4)), ("中间50%", (n//4, 3*n//4)), ("最差25%", (3*n//4, n))]:
            sub = [rows[i] for i in order[a:b]]; ev(f"{lab} {q} (预测{np.mean(s[order[a:b]]):+.2f})", sub)
        sB_pos = [rows[i] for i in range(n) if s[i] > 0]
        if sB_pos: ev(f"{lab} 预测>0", sB_pos)
    if nm == "GBM":
        imp = sorted(zip(mp.feature_importances_ - 0, F), reverse=True)[:8]
        print("  P0命中模型重要性:", [(f, round(i,3)) for i, f in imp])
# station persistence
print("\n== 站点持续性: 窗A各站差 vs 窗B各站差 ==")
sa = collections.defaultdict(list); sb = collections.defaultdict(list)
for r in A: sa[r["st"]].append(r["p_hit"]-r["q_hit"])
for r in B: sb[r["st"]].append(r["p_hit"]-r["q_hit"])
common = [s for s in sa if s in sb and len(sa[s])>=10 and len(sb[s])>=10]
xa = [statistics.mean(sa[s]) for s in common]; xb = [statistics.mean(sb[s]) for s in common]
print(f"  站数={len(common)} 相关={np.corrcoef(xa, xb)[0,1]:+.2f}")
top = sorted(common, key=lambda s: -statistics.mean(sa[s]))[:8]
ev("A最好8站 → B", [r for r in B if r["st"] in top]); print("   站:", top)
# composites
print("\n== 复合条件 ==")
for nm, cond in [("P0众数=市场第二桶", lambda r: r["q_at_pm"] >= r["q_second"]-1e-9),
                 ("市场近平局(gap_q≤0.064)∧P0众数=市场第二", lambda r: r["gap_q"] <= 0.0636 and r["q_at_pm"] >= r["q_second"]-1e-9),
                 ("市场宽(sd_q>1.5)∧|dμ|≤0.42", lambda r: r["sd_q"] > 1.5 and r["absdmu"] <= 0.418),
                 ("市场众数在边缘桶", lambda r: r["qm_edge"] == 1),
                 ("|dμ|≤0.42∧六家分裂(sys_sd>0.75)", lambda r: r["absdmu"] <= 0.418 and (r.get("sys_sd") or 0) > 0.752),
                 ("P0锐于市场(sharp<0.75)∧|dμ|≤0.42", lambda r: r["sharp_ratio"] is not None and r["sharp_ratio"] < 0.75 and r["absdmu"] <= 0.418)]:
    for lab, rows in [("A", A), ("B", B)]:
        sub = [r for r in rows if cond(r)]
        if len(sub) >= 15: ev(f"{nm} {lab}", sub)
