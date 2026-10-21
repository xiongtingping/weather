import json, os, collections, statistics, random, math
HERE = os.path.dirname(os.path.abspath(__file__))
RG = [json.loads(l) for l in open(os.path.join(HERE, "rungs_v4.jsonl"))]
WD = [json.loads(l) for l in open(os.path.join(HERE, "windows_v4.jsonl"))]
for r in RG + WD: r["odd"] = (int(r["d"][-2:]) + (31 if r["d"][5:7]=="10" else 0)) % 2
out = open(os.path.join(HERE, "analyze_v4.txt"), "w")
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
def m(rows, k): return statistics.mean(r[k] for r in rows)
def yes_pnl(rows):  # buy YES at ask, per share
    g = [r for r in rows if r.get("ask") is not None]; return statistics.mean(r["hit"]-r["ask"] for r in g)
def no_pnl(rows):   # buy NO at 1-bid: pays 1 if not hit → (1-hit) - (1-bid) = bid - hit
    g = [r for r in rows if r.get("bid") is not None]; return statistics.mean(r["bid"]-r["hit"] for r in g)
def line(name, rows, f, extra=""):
    if len(rows) < 30: P(f"   {name:36s} n<30"); return None
    s4 = splits(rows); lo, hi = boot(rows, f); v = f(rows)
    P(f"   {name:36s} n={len(rows):5d} {v:+6.1%} [{lo:+.1%},{hi:+.1%}] 四分割({' '.join(f'{k}{f(x):+.1%}' for k,x in s4.items() if len(x)>=10)}) {extra}")
    return v, min(f(x) for k, x in s4.items() if len(x) >= 10)
survivors = []
for fam in ("P0>36h", "P0-M", "P1"):
    R = [r for r in RG if r["fam"] == fam]
    P(f"\n\n==================== {fam}：逐档（离市场众数的偏移）n决策={len({(r['st'],r['d'],r['band']) for r in R})} ====================")
    P("   位置        n档    模型均质量  市场均质量  实际频率 | 模型高估档(lr>0)数 买YES@卖一 P&L | 模型低估档(lr<0)数 买NO@1−买一 P&L")
    for pos in ("tail_lo", "-3", "-2", "-1", "0", "1", "2", "3", "tail_hi"):
        g = [r for r in R if r["pos"] == pos]
        if len(g) < 30: continue
        ov = [r for r in g if r["lr"] > 0.1 and r.get("ask") is not None]; un = [r for r in g if r["lr"] < -0.1 and r.get("bid") is not None]
        P(f"   {pos:10s} {len(g):6d}   {m(g,'p'):6.3f}     {m(g,'q'):6.3f}     {m(g,'hit'):6.3f} | {len(ov):5d}  {yes_pnl(ov) if ov else float('nan'):+6.1%} | {len(un):5d}  {no_pnl(un) if un else float('nan'):+6.1%}")
    P("\n   —— 模型高估档（lr>0.1）买 YES@卖一，按位置 × 四分割 ——")
    for pos in ("tail_lo", "-3", "-2", "-1", "0", "1", "2", "3", "tail_hi"):
        g = [r for r in R if r["pos"] == pos and r["lr"] > 0.1 and r.get("ask") is not None]
        res = line(f"YES {pos}", g, yes_pnl)
        if res and res[1] > 0: survivors.append((fam, "YES", pos, len(g), res[0]))
    P("   —— 模型低估档（lr<−0.1）买 NO@1−买一，按位置 × 四分割 ——")
    for pos in ("tail_lo", "-3", "-2", "-1", "0", "1", "2", "3", "tail_hi"):
        g = [r for r in R if r["pos"] == pos and r["lr"] < -0.1 and r.get("bid") is not None]
        res = line(f"NO {pos}", g, no_pnl)
        if res and res[1] > 0: survivors.append((fam, "NO", pos, len(g), res[0]))
    P("   —— 高估幅度分档（lr 三分位，lr>0 的档）买 YES ——")
    ov = [r for r in R if r["lr"] > 0 and r.get("ask") is not None]
    v = sorted(r["lr"] for r in ov); q1, q2 = v[len(v)//3], v[2*len(v)//3]
    for nm, g in ((f"lr≤{q1:.2f}", [r for r in ov if r["lr"] <= q1]), ("中", [r for r in ov if q1 < r["lr"] <= q2]), (f"lr>{q2:.2f}", [r for r in ov if r["lr"] > q2])):
        res = line(f"YES {nm}", g, yes_pnl, f"模型{m(g,'p'):.3f} 市场{m(g,'q'):.3f} 卖一{m(g,'ask'):.3f} 实际{m(g,'hit'):.3f}")
        if res and res[1] > 0: survivors.append((fam, "YES", nm, len(g), res[0]))
    P("   —— 按卖一价位分档（lr>0.1 的档）买 YES ——")
    ov = [r for r in R if r["lr"] > 0.1 and r.get("ask") is not None]
    for lo_, hi_ in ((0, .1), (.1, .2), (.2, .35), (.35, .5), (.5, 1)):
        g = [r for r in ov if lo_ <= r["ask"] < hi_]
        res = line(f"YES 卖一[{lo_},{hi_})", g, yes_pnl, f"模型{m(g,'p') if g else 0:.3f} 实际{m(g,'hit') if g else 0:.3f}")
        if res and res[1] > 0: survivors.append((fam, "YES", f"ask[{lo_},{hi_})", len(g), res[0]))
    P("   —— 按买一价位分档（lr<−0.1 的档）买 NO ——")
    un = [r for r in R if r["lr"] < -0.1 and r.get("bid") is not None]
    for lo_, hi_ in ((0, .05), (.05, .15), (.15, .3), (.3, .5), (.5, 1)):
        g = [r for r in un if lo_ <= r["bid"] < hi_]
        res = line(f"NO 买一[{lo_},{hi_})", g, no_pnl, f"模型{m(g,'p') if g else 0:.3f} 实际{m(g,'hit') if g else 0:.3f}")
        if res and res[1] > 0: survivors.append((fam, "NO", f"bid[{lo_},{hi_})", len(g), res[0]))
    # fresh run
    if fam == "P0>36h":
        P("   —— 新一轮刚到（到货龄<1h）∧ 模型众数刚换 ——")
        dec = {}
        for r in R:
            dec.setdefault((r["st"], r["d"], r["band"]), r)
        fresh = [r for r in dec.values() if r["mode_changed"] and r.get("age_h") is not None and r["age_h"] < 1 and not r["agree"]]
        stale = [r for r in dec.values() if not r["mode_changed"] and not r["agree"]]
        for nm, g in (("刚换∧<1h", fresh), ("没换", stale)):
            if len(g) < 15: P(f"   {nm} n<15"); continue
            ph = statistics.mean(int(r["pm"]==r["yi"]) for r in g); qh = statistics.mean(int(r["qm"]==r["yi"]) for r in g)
            P(f"   {nm:14s} n={len(g)} 模型众数命中{ph:.1%} 市场{qh:.1%} 差{ph-qh:+.1%}")
    # swap pairs
    P("   —— 两边互为第二（抛硬币对）——")
    bydec = collections.defaultdict(list)
    for r in R: bydec[(r["st"], r["d"], r["band"])].append(r)
    sw = []
    for key, rr in bydec.items():
        if rr[0]["agree"]: continue
        pm, qm = rr[0]["pm"], rr[0]["qm"]
        p2 = sorted(rr, key=lambda r: -r["p"])[1]["k"]; q2 = sorted(rr, key=lambda r: -r["q"])[1]["k"]
        if p2 == qm and q2 == pm:
            a = {r["k"]: r for r in rr}; sw.append(dict(win=rr[0]["win"], d=rr[0]["d"], odd=rr[0]["odd"], ph=int(pm==rr[0]["yi"]), qh=int(qm==rr[0]["yi"]), ask_pm=a[pm].get("ask"), ask_qm=a[qm].get("ask"), p_pm=a[pm]["p"], q_qm=a[qm]["q"]))
    if len(sw) >= 30:
        s4 = splits(sw)
        P(f"   互为第二 n={len(sw)} 模型众数命中{m(sw,'ph'):.1%} 市场{m(sw,'qh'):.1%} 差{m(sw,'ph')-m(sw,'qh'):+.1%} 四分割({' '.join(f'{k}{m(x,'ph')-m(x,'qh'):+.0%}' for k,x in s4.items())}) | 买模型桶@卖一 {statistics.mean(r['ph']-r['ask_pm'] for r in sw if r['ask_pm'] is not None):+.1%} 买市场桶@卖一 {statistics.mean(r['qh']-r['ask_qm'] for r in sw if r['ask_qm'] is not None):+.1%}")
P("\n\n==================== 窗口／成组买入（买入所有档@卖一，赢 1）====================")
for fam in ("P0>36h", "P0-M", "P1"):
    W = [w for w in WD if w["fam"] == fam]
    P(f"\n--- {fam} ---")
    P("   窗口          n     模型质量M  市场中价Q  卖一和A  实际命中  P&L=命中−A  四分割")
    for wn in ("pm±0", "pm±1", "pm,pm+1", "pm-1,pm", "qm±1", "pair{pm,qm}", "pm+beyond", "pm..qm", "beyond_only"):
        g = [w for w in W if w["wname"] == wn]
        if len(g) < 30: continue
        s4 = splits(g); pnl = lambda x: statistics.mean(w["hit"]-w["A"] for w in x)
        P(f"   {wn:12s} {len(g):5d}   {m(g,'M'):6.3f}    {m(g,'Q'):6.3f}    {m(g,'A'):6.3f}   {m(g,'hit'):6.3f}   {pnl(g):+6.1%}    ({' '.join(f'{k}{pnl(x):+.1%}' for k,x in s4.items())})")
        # condition: model mass minus ask-sum terciles
        g2 = sorted(g, key=lambda w: w["M"]-w["A"]); n = len(g2); hi = g2[2*n//3:]
        res = line(f"   {wn} M−A 最高三分位", hi, pnl, f"M{m(hi,'M'):.3f} A{m(hi,'A'):.3f} 命中{m(hi,'hit'):.3f}")
        if res and res[1] > 0: survivors.append((fam, "WIN", wn+" M−A高", len(hi), res[0]))
        pos = [w for w in g if w["M"] > w["A"] + 0.05]
        res = line(f"   {wn} M>A+0.05", pos, pnl, f"M{m(pos,'M') if pos else 0:.3f} A{m(pos,'A') if pos else 0:.3f} 命中{m(pos,'hit') if pos else 0:.3f}")
        if res and res[1] > 0: survivors.append((fam, "WIN", wn+" M>A+.05", len(pos), res[0]))
P("\n\n==================== 四分割同正的格 ====================")
for s in survivors: P("  ", s)
