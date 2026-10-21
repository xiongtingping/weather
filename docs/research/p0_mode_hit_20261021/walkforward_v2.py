import json, os, collections, statistics, random, itertools
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
HERE = os.path.dirname(os.path.abspath(__file__))
R = [json.loads(l) for l in open(os.path.join(HERE, "recs_v2.jsonl"))]
for r in R: r["odd"] = (int(r["d"][-2:]) + (31 if r["d"][5:7]=="10" else 0)) % 2
FAM = {"P0>36h 首决策": lambda r: r["stage"]=="P0" and r["first_band"] and r["h"]>36, "P0 末决策": lambda r: r["stage"]=="P0" and r["last_stage"],
       "P0-M 首决策": lambda r: r["stage"]=="P0-M" and r["first_band"], "P0-M 末决策": lambda r: r["stage"]=="P0-M" and r["last_stage"],
       "P1 首决策": lambda r: r["stage"]=="P1" and r["first_band"]}
SKIP = {"win","st","d","stage","t","pv","yi","pm","qm","p_hit","q_hit","logp","logq","first_band","last_stage","odd","top_sys","taf_mode","taf_rev","band","region","season","run_cycle","unit","runhigh_bucket","truth_minus_y1"}
def rate(rows,k): return sum(r[k] for r in rows)/len(rows)
def diff(rows): return rate(rows,"p_hit")-rate(rows,"q_hit")
def boot(rows, B=300):
    days = sorted({r["d"] for r in rows}); by = collections.defaultdict(list)
    for r in rows: by[r["d"]].append(r)
    rnd = random.Random(1); v = []
    for _ in range(B):
        s = [x for d in (rnd.choice(days) for _ in days) for x in by[d]]; v.append(diff(s))
    v.sort(); return v[int(.025*len(v))], v[int(.975*len(v))]
out = open(os.path.join(HERE, "walkforward_v2.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
for fam, pred in FAM.items():
    D = [r for r in R if pred(r) and r["dmode"] != 0]
    A = [r for r in D if r["win"]=="A"]; B = [r for r in D if r["win"]=="B"]
    feats = sorted(k for k in set().union(*[r.keys() for r in D[:2000]]) if k not in SKIP and sum(1 for r in D if isinstance(r.get(k),(int,float))) >= 0.5*len(D))
    def X(rows): return np.array([[(r[k] if isinstance(r.get(k),(int,float)) else np.nan) for k in feats] for r in rows], float)
    XA, XB = X(A), X(B); med = np.nanmedian(XA, 0); XA = np.where(np.isnan(XA), med, XA); XB = np.where(np.isnan(XB), med, XB)
    yA = np.array([r["p_hit"]-r["q_hit"] for r in A])  # -1,0,1 → predict P(p_hit) and P(q_hit)
    mp = GradientBoostingClassifier(n_estimators=200, max_depth=2, learning_rate=0.04, subsample=0.8, random_state=0).fit(XA, [r["p_hit"] for r in A])
    mq = GradientBoostingClassifier(n_estimators=200, max_depth=2, learning_rate=0.04, subsample=0.8, random_state=0).fit(XA, [r["q_hit"] for r in A])
    sB = mp.predict_proba(XB)[:,1]-mq.predict_proba(XB)[:,1]; sA = mp.predict_proba(XA)[:,1]-mq.predict_proba(XA)[:,1]
    P(f"\n== {fam}: 众数不同 A n={len(A)} B n={len(B)} 特征数 {len(feats)} ==")
    for lab, s, rows in [("A样本内", sA, A), ("B前推", sB, B)]:
        o = np.argsort(-s); n = len(rows)
        for q, (a, b) in [("最好10%", (0, n//10)), ("最好25%", (0, n//4)), ("最差25%", (3*n//4, n))]:
            sub = [rows[i] for i in o[a:b]]; lo, hi = boot(sub)
            P(f"   {lab} {q}: n={len(sub)} 模型{rate(sub,'p_hit'):5.1%} 市{rate(sub,'q_hit'):5.1%} 差{diff(sub):+6.1%} [{lo:+.0%},{hi:+.0%}]")
        pos = [rows[i] for i in range(n) if s[i] > 0]
        if len(pos) >= 15: lo, hi = boot(pos); P(f"   {lab} 预测>0: n={len(pos)} 差{diff(pos):+6.1%} [{lo:+.0%},{hi:+.0%}]")
    imp = sorted(zip(mp.feature_importances_, feats), reverse=True)[:6]; P("   重要性:", [(f, round(float(i),3)) for i, f in imp])
    # pairwise cells: select in A, validate in B
    top = [f for _, f in sorted(zip(mp.feature_importances_+mq.feature_importances_, feats), reverse=True)[:25]]
    bins = {}
    for k in top:
        v = sorted(r[k] for r in A if isinstance(r.get(k),(int,float)))
        if len(set(v)) <= 3: bins[k] = [(str(x), (lambda r, k=k, x=x: r.get(k) == x)) for x in sorted(set(v))]
        else:
            q1, q2 = v[len(v)//3], v[2*len(v)//3]
            bins[k] = [("低", lambda r, k=k, q1=q1: isinstance(r.get(k),(int,float)) and r[k] <= q1), ("中", lambda r, k=k, q1=q1, q2=q2: isinstance(r.get(k),(int,float)) and q1 < r[k] <= q2), ("高", lambda r, k=k, q2=q2: isinstance(r.get(k),(int,float)) and r[k] > q2)]
    sel = []; ntest = 0
    for k1, k2 in itertools.combinations(top, 2):
        for g1, f1 in bins[k1]:
            for g2, f2 in bins[k2]:
                a = [r for r in A if f1(r) and f2(r)]
                if len(a) < 40: continue
                ntest += 1
                if diff(a) > 0.05:
                    b = [r for r in B if f1(r) and f2(r)]
                    if len(b) >= 20: sel.append((k1, g1, k2, g2, len(a), diff(a), len(b), diff(b)))
    if sel:
        posB = sum(1 for s in sel if s[7] > 0); allB = [r for s in sel for r in B if bins and True] 
        P(f"   两特征格: A 里检验 {ntest} 格, A 差>+5% 且 B n≥20 的 {len(sel)} 格 → B 里仍为正 {posB} 格 ({posB/len(sel):.0%}); B 均差 {statistics.mean(s[7] for s in sel):+.1%} (A 均差 {statistics.mean(s[5] for s in sel):+.1%})")
        for s in sorted(sel, key=lambda s: -s[7])[:5]: P(f"     {s[0]}={s[1]} ∧ {s[2]}={s[3]}: A n={s[4]} {s[5]:+.1%} → B n={s[6]} {s[7]:+.1%}")
    else: P(f"   两特征格: A 里检验 {ntest} 格, 无 A 差>+5% 且 B 足样本的格")
# weekly trend P0>36h first
P("\n== 周走势 (众数不同, 模型命中−市场命中) ==")
for fam, pred in FAM.items():
    D = [r for r in R if pred(r) and r["dmode"] != 0]
    wk = collections.defaultdict(list)
    for r in D: wk[r["d"][:7] + "w" + str((int(r["d"][-2:])-1)//7+1)].append(r)
    P(f"  {fam}: " + " | ".join(f"{k} {diff(v):+.0%}(n{len(v)})" for k, v in sorted(wk.items())))
# mode_stable_n neighbours in P0-M first
D = [r for r in R if r["stage"]=="P0-M" and r["first_band"] and r["dmode"] != 0]
P("\n== P0-M 首决策 mode_stable_n 逐值 ==")
for v in range(0, 25):
    sub = [r for r in D if r.get("mode_stable_n") == v]
    if len(sub) >= 15: P(f"  {v:2d}: n={len(sub):4d} 差{diff(sub):+6.1%}")
