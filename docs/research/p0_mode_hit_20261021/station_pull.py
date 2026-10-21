import json, os, collections, sys
HERE = os.path.dirname(os.path.abspath(__file__))
R = [json.loads(l) for l in open(os.path.join(HERE, "recs_v2.jsonl"))]
FAM = {"P0>36h首决策": lambda r: r["stage"]=="P0" and r["first_band"] and r["h"]>36,
       "P0-M首决策": lambda r: r["stage"]=="P0-M" and r["first_band"],
       "P1首决策": lambda r: r["stage"]=="P1" and r["first_band"]}
out = open(os.path.join(HERE, "station_pull.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
for fam, pred in FAM.items():
    D = [r for r in R if pred(r) and r["dmode"] != 0]
    P(f"\n\n################ {fam}  众数不同 n={len(D)} ################")
    P("站      |  n  模型独中 市场独中 | 模型暖于市场: 模中/市中 | 模型冷于市场: 模中/市中 | 市场错时真值在冷侧比例 A / B | 模型独中 A / B | 模型赢方向一致?")
    by = collections.defaultdict(list)
    for r in D: by[r["st"]].append(r)
    rows_out = []
    for st, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
        po = [r for r in rows if r["p_hit"]]; qo = [r for r in rows if r["q_hit"]]
        warm = [r for r in rows if r["warm"]]; cold = [r for r in rows if not r["warm"]]
        pw, qw = sum(r["p_hit"] for r in warm), sum(r["q_hit"] for r in warm)
        pc, qc = sum(r["p_hit"] for r in cold), sum(r["q_hit"] for r in cold)
        def coldbias(w):
            miss = [r for r in rows if r["win"]==w and not r["q_hit"]]
            if len(miss) < 5: return None
            return sum(1 for r in miss if r["yi"] < r["qm"])/len(miss), len(miss)
        cbA, cbB = coldbias("A"), coldbias("B")
        pA = [r for r in po if r["win"]=="A"]; pB = [r for r in po if r["win"]=="B"]
        nA = len([r for r in rows if r["win"]=="A"]); nB = len([r for r in rows if r["win"]=="B"])
        # model-win direction consistency: fraction of model-only wins on the warm side, A vs B
        def wfrac(ws):
            if len(ws) < 3: return None
            return sum(r["warm"] for r in ws)/len(ws)
        wA, wB = wfrac(pA), wfrac(pB)
        cons = ""
        if wA is not None and wB is not None:
            cons = "同向" if ((wA >= 0.67 and wB >= 0.67) or (wA <= 0.33 and wB <= 0.33)) else "不"
        f = lambda x: "  -  " if x is None else f"{x[0]:4.0%}({x[1]:2d})"
        P(f"{st:7s} | {len(rows):3d}  {len(po):3d}      {len(qo):3d}    |   {pw:2d}/{qw:2d} (n{len(warm):2d})          |   {pc:2d}/{qc:2d} (n{len(cold):2d})          |  {f(cbA)} / {f(cbB)}        | {len(pA):2d}/{nA:2d} , {len(pB):2d}/{nB:2d}      | {wA if wA is None else round(wA,2)} {wB if wB is None else round(wB,2)} {cons}")
        rows_out.append(dict(st=st, n=len(rows), po=len(po), qo=len(qo), pw=pw, qw=qw, nw=len(warm), pc=pc, qc=qc, nc=len(cold)))
    # stations where model-only > market-only on one side in BOTH windows
    P("\n  —— 单侧两窗都「模型独中 ≥ 市场独中」的站·方向（n 各窗≥4）——")
    for st, rows in sorted(by.items()):
        for sidename, flag in (("模型暖于市场", 1), ("模型冷于市场", 0)):
            ok = True; desc = []
            for w in ("A", "B"):
                s = [r for r in rows if r["warm"] == flag and r["win"] == w]
                if len(s) < 4: ok = False; break
                p_, q_ = sum(r["p_hit"] for r in s), sum(r["q_hit"] for r in s)
                desc.append(f"{w}: 模{p_}/市{q_}/n{len(s)}")
                if p_ < q_ or p_ == 0: ok = False
            if ok: P(f"   {st} {sidename}: " + "  ".join(desc))
