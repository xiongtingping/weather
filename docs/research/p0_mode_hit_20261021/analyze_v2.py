#!/usr/bin/env python3
"""Exhaustive slice search, all stages.  Families x views; each feature cut into
terciles (numeric) or categories; a cell survives only if model-hit minus
market-hit is > 0 in all four splits (window A, window B, odd days, even days).
Also reports the real-cost edge: model-hit minus the ask at the model's mode."""
import json, os, random, collections, statistics, math, sys
HERE = os.path.dirname(os.path.abspath(__file__))
R = [json.loads(l) for l in open(os.path.join(HERE, "recs_v2.jsonl"))]
for r in R:
    r["dayn"] = int(r["d"][-2:]) + (31 if r["d"][5:7] == "10" else 0)
    r["odd"] = r["dayn"] % 2
    r["edge_mid"] = r["p_hit"] - r["q_at_pm"]
    r["edge_ask"] = (r["p_hit"] - r["ask_pm"]) if r.get("ask_pm") is not None else None
FAM = {
 "P0>36h 首决策": lambda r: r["stage"]=="P0" and r["first_band"] and r["h"]>36,
 "P0≤36h 首决策": lambda r: r["stage"]=="P0" and r["first_band"] and r["h"]<=36,
 "P0 末决策": lambda r: r["stage"]=="P0" and r["last_stage"],
 "P0-M 首决策": lambda r: r["stage"]=="P0-M" and r["first_band"],
 "P0-M 末决策": lambda r: r["stage"]=="P0-M" and r["last_stage"],
 "P1 首决策(各时段)": lambda r: r["stage"]=="P1" and r["first_band"],
 "P1 末决策": lambda r: r["stage"]=="P1" and r["last_stage"],
}
SKIP = {"win","st","d","stage","t","pv","yi","pm","qm","p_hit","q_hit","logp","logq","first_band","last_stage","dayn","odd","edge_mid","edge_ask","top_sys","taf_mode","taf_rev","band","mean_p","mean_q","mode_temp","runhigh_bucket","region","season","run_cycle","unit"}
CAT = ["unit","region","season","band","run_cycle","top_sys","taf_mode","warm","absdmode","qm_edge","pm_edge","n_sys","mkt_mode_changed","model_mode_changed","mkt_moved_1h","p0_closer_persist","taf_closer_p0","taf_bucket_is_pm","taf_bucket_is_qm","pm_is_runhigh","qm_is_runhigh","pm_above_runhigh","qm_above_runhigh","top_side_p0","side_ifs","side_gefs","side_gem","side_icon","side_aifs","side_aigefs","dec_wday","event_wday","n_mkt_modes_seen","mode_stable_n","local_hour"]
def rate(rows, k): return sum(r[k] for r in rows)/len(rows)
def diff(rows): return rate(rows, "p_hit") - rate(rows, "q_hit")
def boot(rows, f, B=300, seed=1):
    days = sorted({r["d"] for r in rows}); by = collections.defaultdict(list)
    for r in rows: by[r["d"]].append(r)
    rnd = random.Random(seed); v = []
    for _ in range(B):
        s = [x for d in (rnd.choice(days) for _ in days) for x in by[d]]
        if s: v.append(f(s))
    v.sort(); return v[int(.025*len(v))], v[int(.975*len(v))]
def splits(rows):
    return {"A": [r for r in rows if r["win"]=="A"], "B": [r for r in rows if r["win"]=="B"],
            "奇": [r for r in rows if r["odd"]], "偶": [r for r in rows if not r["odd"]]}
def ea(rows):
    v = [r["edge_ask"] for r in rows if r["edge_ask"] is not None]; return statistics.mean(v) if v else float("nan")
tests = 0; survivors = []; near = []
summary = open(os.path.join(HERE, "slices_v2.txt"), "w")
def P(*a):
    print(*a); print(*a, file=summary)
for fam, pred in FAM.items():
    rows = [r for r in R if pred(r)]
    if len(rows) < 100: continue
    P(f"\n\n==================== {fam} ====================")
    sp = splits(rows)
    lo, hi = boot(rows, diff)
    P(f"全部 n={len(rows)} 模型众数命中 {rate(rows,'p_hit'):.1%} 市场 {rate(rows,'q_hit'):.1%} 差 {diff(rows):+.1%} [{lo:+.1%},{hi:+.1%}] | " + " ".join(f"{k}:{diff(v):+.1%}(n{len(v)})" for k, v in sp.items() if v))
    D = [r for r in rows if r["dmode"] != 0]
    if len(D) < 60: P("众数不同样本不足"); continue
    spd = splits(D); lo, hi = boot(D, diff)
    P(f"众数不同 n={len(D)} ({len(D)/len(rows):.0%}) 模型 {rate(D,'p_hit'):.1%} 市场 {rate(D,'q_hit'):.1%} 差 {diff(D):+.1%} [{lo:+.1%},{hi:+.1%}] 买模型桶edge(中价) {statistics.mean(r['edge_mid'] for r in D):+.1%} (卖一) {ea(D):+.1%} | " + " ".join(f"{k}:{diff(v):+.1%}(n{len(v)})" for k, v in spd.items() if v))
    feats = [k for k in D[0].keys() if k not in SKIP] + [k for k in CAT if k not in D[0]]
    feats = sorted(set(k for k in feats if sum(1 for r in D if r.get(k) is not None) >= 60))
    fam_cells = []
    for k in feats:
        have = [r for r in D if r.get(k) is not None]
        if k in CAT or isinstance(have[0][k], str) or len({r[k] for r in have}) <= 6:
            vals = sorted({r[k] for r in have}, key=str); groups = [(str(v), [r for r in have if r[k] == v]) for v in vals]
        else:
            v = sorted(r[k] for r in have); q1, q2 = v[len(v)//3], v[2*len(v)//3]
            groups = [(f"低≤{q1:.3g}", [r for r in have if r[k] <= q1]), ("中", [r for r in have if q1 < r[k] <= q2]), (f"高>{q2:.3g}", [r for r in have if r[k] > q2])]
        for g, sub in groups:
            if len(sub) < 40: continue
            s4 = splits(sub)
            if any(len(v) < 15 for v in s4.values()): continue
            tests += 1
            d4 = {kk: diff(v) for kk, v in s4.items()}; e4 = {kk: ea(v) for kk, v in s4.items()}
            cell = dict(fam=fam, feat=k, grp=g, n=len(sub), p=rate(sub,"p_hit"), q=rate(sub,"q_hit"), d=diff(sub), dmin=min(d4.values()), d4=d4,
                        e=ea(sub), emin=min(e4.values()), e4=e4)
            fam_cells.append(cell)
            if cell["dmin"] > 0: survivors.append(cell)
            elif cell["emin"] > 0: near.append(cell)
    fam_cells.sort(key=lambda c: -c["dmin"])
    P(f"  检验格数 {len(fam_cells)}；按四分割最小差排序前 12：")
    for c in fam_cells[:12]:
        P(f"   {c['feat']}={c['grp']:<12} n={c['n']:4d} 模型{c['p']:5.1%} 市{c['q']:5.1%} 差{c['d']:+6.1%} 四分割最小{c['dmin']:+6.1%} ({' '.join(f'{k}{v:+.0%}' for k,v in c['d4'].items())}) 卖一edge{c['e']:+6.1%} 最小{c['emin']:+6.1%}")
    P(f"  四分割同为「卖一edge>0」的格：")
    for c in sorted([c for c in fam_cells if c["emin"] > 0], key=lambda c: -c["emin"])[:12]:
        P(f"   {c['feat']}={c['grp']:<12} n={c['n']:4d} 模型{c['p']:5.1%} 市{c['q']:5.1%} 差{c['d']:+6.1%} 卖一edge{c['e']:+6.1%} 四分割 ({' '.join(f'{k}{v:+.0%}' for k,v in c['e4'].items())})")
P(f"\n\n总检验格数 {tests}；四分割同为「模型命中>市场命中」的格 {len(survivors)}；期望假阳性(独立、各分割p=.5)≈{tests/16:.1f}")
for c in survivors:
    P(f"  {c['fam']} {c['feat']}={c['grp']} n={c['n']} 差{c['d']:+.1%} 四分割({' '.join(f'{k}{v:+.0%}' for k,v in c['d4'].items())}) 卖一edge{c['e']:+.1%}")
json.dump(dict(tests=tests, survivors=survivors, edge_only=near), open(os.path.join(HERE, "cells_v2.json"), "w"), ensure_ascii=False)
