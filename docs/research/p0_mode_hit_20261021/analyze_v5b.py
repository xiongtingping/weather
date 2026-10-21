"""Instrument-level P&L for the contrarian directional cells: join rungs_v4
(per-rung bid/ask/hit) to recs_v5 (conditions)."""
import json, os, collections, statistics, random
HERE = os.path.dirname(os.path.abspath(__file__))
R5 = {}
for l in open(os.path.join(HERE, "recs_v5.jsonl")):
    r = json.loads(l); R5[(r["fam"], r["st"], r["d"], r["band"])] = r
RG = collections.defaultdict(dict)
for l in open(os.path.join(HERE, "rungs_v4.jsonl")):
    g = json.loads(l); RG[(g["fam"], g["st"], g["d"], g["band"])][g["k"]] = g
out = open(os.path.join(HERE, "analyze_v5b.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
for r in R5.values(): r["odd"] = (int(r["d"][-2:]) + (31 if r["d"][5:7]=="10" else 0)) % 2; r["month"] = r["d"][5:7]
def instruments(r):
    """s_model = side of the model's mode vs market mode (+1 warm); fade side = -s_model."""
    key = (r["fam"], r["st"], r["d"], r["band"]); rg = RG.get(key)
    if not rg: return None
    K, qm, pm, yi = r["K"], r["qm"], r["pm"], r["yi"]; s = r["model_side"]
    if s == 0: return None
    f = -s
    def yes(ks):
        ks = [k for k in ks if 0 <= k < K]
        if not ks or any(k not in rg or rg[k].get("ask") is None for k in ks): return None
        return sum(rg[k]["hit"] for k in ks) - sum(rg[k]["ask"] for k in ks)
    def no(ks):  # buy NO on each at 1-bid; each pays 1 if not hit → sum(bid_k - hit_k)
        ks = [k for k in ks if 0 <= k < K]
        if not ks or any(k not in rg or rg[k].get("bid") is None for k in ks): return None
        return sum(rg[k]["bid"] - rg[k]["hit"] for k in ks)
    def yes_mid(ks):
        ks = [k for k in ks if 0 <= k < K]
        if not ks or any(k not in rg for k in ks): return None
        return sum(rg[k]["hit"] for k in ks) - sum(rg[k]["q"] for k in ks)
    return {"YES 市场众数@卖一": yes([qm]), "YES 市场众数(中价,不可成交)": yes_mid([qm]),
            "YES 反侧一档@卖一": yes([qm+f]), "YES 众数+反侧一档@卖一": yes([qm, qm+f]), "YES 反侧两档@卖一": yes([qm+f, qm+2*f]),
            "NO 模型众数@1−买一": no([pm]), "NO 模型侧一档(qm+s)@1−买一": no([qm+s]), "NO 模型侧三档@1−买一": no([qm+s, qm+2*s, qm+3*s]),
            "YES 模型众数@卖一(对照)": yes([pm]), "NO 反侧一档(对照)": no([qm+f])}
def splits(rows): return {"A":[r for r in rows if r["win"]=="A"], "B":[r for r in rows if r["win"]=="B"], "奇":[r for r in rows if r["odd"]], "偶":[r for r in rows if not r["odd"]], "9月":[r for r in rows if r["month"]=="09"], "10月":[r for r in rows if r["month"]=="10"]}
def boot(vals_by_day, B=400):
    days = list(vals_by_day); rnd = random.Random(2); v = []
    for _ in range(B):
        s = [x for d in (rnd.choice(days) for _ in days) for x in vals_by_day[d]]; v.append(statistics.mean(s))
    v.sort(); return v[int(.025*B)], v[int(.975*B)]
def report(name, rows):
    rows = [r for r in rows if r["model_side"] != 0]
    ins = [(r, instruments(r)) for r in rows]; ins = [(r, i) for r, i in ins if i]
    if len(ins) < 30: P(f"\n### {name}: n<30"); return
    P(f"\n### {name}  n={len(ins)}  真值−市场均值 {statistics.mean(r['truth_minus_qmean'] for r,_ in ins):+.3f}桶")
    for inst in ins[0][1]:
        vals = [(r, i[inst]) for r, i in ins if i[inst] is not None]
        if len(vals) < 30: continue
        by = collections.defaultdict(list)
        for r, v in vals: by[r["d"]].append(v)
        lo, hi = boot(by); m = statistics.mean(v for _, v in vals)
        s6 = splits([r for r, _ in vals]); vmap = {id(r): v for r, v in vals}
        parts = " ".join(f"{k}{statistics.mean(vmap[id(r)] for r in x):+.1%}" for k, x in s6.items() if len(x) >= 10)
        star = "★" if all(statistics.mean(vmap[id(r)] for r in x) > 0 for k, x in s6.items() if k in ("A","B","奇","偶") and len(x) >= 10) else " "
        P(f"   {star} {inst:28s} n={len(vals):4d} {m:+6.1%}/份 [{lo:+.1%},{hi:+.1%}]  ({parts})")
for fam in ("P0>36h", "P0-M", "P1"):
    D = [r for r in R5.values() if r["fam"] == fam]
    P(f"\n\n==================== {fam} ====================")
    report("模型众数在市场冷侧（反侧=暖）", [r for r in D if r["model_side"] == -1])
    report("模型众数在市场暖侧（反侧=冷）", [r for r in D if r["model_side"] == 1])
    report("两侧合并（按反侧对齐）", [r for r in D if r["model_side"] != 0])
    # magnitude layering
    for nm, cond in (("|众数差|=1", lambda r: abs(r["pm"]-r["qm"]) == 1), ("|众数差|≥2", lambda r: abs(r["pm"]-r["qm"]) >= 2)):
        report(f"两侧合并 ∧ {nm}", [r for r in D if r["model_side"] != 0 and cond(r)])
    dm = [r for r in D if r["model_side"] != 0]
    v = sorted(abs(r["dmu"]) for r in dm); q1, q2 = v[len(v)//3], v[2*len(v)//3]
    report(f"两侧合并 ∧ |均值差|≤{q1:.2f}", [r for r in dm if abs(r["dmu"]) <= q1]); report(f"两侧合并 ∧ |均值差|>{q2:.2f}", [r for r in dm if abs(r["dmu"]) > q2])
    if fam == "P0>36h":
        for b in ("36-48h", "48-60h", "60h+"): report(f"两侧合并 ∧ 时段 {b}", [r for r in dm if r["band"] == b])
    if fam == "P0-M":
        report("TAF 在市场暖侧（反侧=冷）", [r for r in D if r.get("taf_side") == 1 and r["model_side"] == 1])
        report("TAF 在市场冷侧（反侧=暖）", [r for r in D if r.get("taf_side") == -1 and r["model_side"] == -1])
    for reg in ("CN", "EU", "US", "JPKR"):
        report(f"两侧合并 ∧ 地区 {reg}", [r for r in dm if r["region"] == reg])
