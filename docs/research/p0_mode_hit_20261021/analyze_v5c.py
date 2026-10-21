import json, os, collections, statistics, random
HERE = os.path.dirname(os.path.abspath(__file__))
R5 = {}
for l in open(os.path.join(HERE, "recs_v5.jsonl")):
    r = json.loads(l); r["odd"] = (int(r["d"][-2:]) + (31 if r["d"][5:7]=="10" else 0)) % 2; R5[(r["fam"], r["st"], r["d"], r["band"])] = r
RG = collections.defaultdict(dict)
for l in open(os.path.join(HERE, "rungs_v4.jsonl")):
    g = json.loads(l); RG[(g["fam"], g["st"], g["d"], g["band"])][g["k"]] = g
out = open(os.path.join(HERE, "analyze_v5c.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
def wk(d): return d[:7]+"w"+str((int(d[-2:])-1)//7+1)
def rung(r, k):
    rg = RG.get((r["fam"], r["st"], r["d"], r["band"])); return rg.get(k) if rg else None
def yes_pnl(r, k):
    g = rung(r, k); return None if (g is None or g.get("ask") is None) else (g["hit"] - g["ask"], g["ask"])
def no_pnl(r, k):
    g = rung(r, k); return None if (g is None or g.get("bid") is None) else (g["bid"] - g["hit"], 1 - g["bid"])
def boot(vals_by_day, B=500):
    days = list(vals_by_day); rnd = random.Random(5); v = []
    for _ in range(B):
        s = [x for d in (rnd.choice(days) for _ in days) for x in vals_by_day[d]]; v.append(statistics.mean(s))
    v.sort(); return v[int(.025*B)], v[int(.975*B)]
def summarize(name, pairs):
    """pairs: list of (rec, (pnl, cost))"""
    pairs = [(r, x) for r, x in pairs if x is not None]
    if len(pairs) < 15: P(f"   {name:40s} n={len(pairs)} (<15)"); return
    by = collections.defaultdict(list)
    for r, (p, c) in pairs: by[r["d"]].append(p)
    lo, hi = boot(by); m = statistics.mean(p for _, (p, c) in pairs); cost = statistics.mean(c for _, (p, c) in pairs)
    sp = {"A":[p for r,(p,c) in pairs if r["win"]=="A"], "B":[p for r,(p,c) in pairs if r["win"]=="B"], "奇":[p for r,(p,c) in pairs if r["odd"]], "偶":[p for r,(p,c) in pairs if not r["odd"]], "9月":[p for r,(p,c) in pairs if r["d"][5:7]=="09"], "10月":[p for r,(p,c) in pairs if r["d"][5:7]=="10"]}
    star = "★" if all(statistics.mean(v) > 0 for k, v in sp.items() if len(v) >= 10) else " "
    P(f" {star} {name:40s} n={len(pairs):4d} {m:+6.1%}/份 [{lo:+.1%},{hi:+.1%}] 成本{cost:.2f} 资本回报{m/cost:+.1%}  ({' '.join(f'{k}{statistics.mean(v):+.1%}' for k,v in sp.items() if len(v)>=10)})")
def weekly(name, pairs):
    pairs = [(r, x) for r, x in pairs if x is not None]; by = collections.defaultdict(list)
    for r, (p, c) in pairs: by[wk(r["d"])].append(p)
    P(f"   逐周 {name}: " + " | ".join(f"{k} {statistics.mean(v):+.1%}(n{len(v)})" for k, v in sorted(by.items())))
def stations(name, pairs, top=8):
    pairs = [(r, x) for r, x in pairs if x is not None]; by = collections.defaultdict(list)
    for r, (p, c) in pairs: by[r["st"]].append(p)
    tot = sum(p for _, (p, c) in pairs)
    P(f"   按站 {name}（站数 {len(by)}，正站 {sum(1 for v in by.values() if statistics.mean(v) > 0)}）: " + ", ".join(f"{k} {statistics.mean(v):+.0%}(n{len(v)})" for k, v in sorted(by.items(), key=lambda kv: -sum(kv[1]))[:top]) + f" | 去掉贡献最大 3 站后均值 {statistics.mean([p for r,(p,c) in pairs if r['st'] not in [k for k,_ in sorted(by.items(), key=lambda kv: -sum(kv[1]))[:3]]]):+.1%}")

P("################ A. P0-M：模型众数在市场暖侧 → 买市场众数冷侧一档 (qm−1) @卖一 ################")
D = [r for r in R5.values() if r["fam"] == "P0-M"]
warm = [r for r in D if r["model_side"] == 1]
cell = [(r, yes_pnl(r, r["qm"]-1)) for r in warm]
summarize("主格：模型暖侧 → YES qm−1", cell); weekly("主格", cell); stations("主格", cell)
summarize("对照：全部 P0-M → YES qm−1", [(r, yes_pnl(r, r["qm"]-1)) for r in D])
summarize("对照：模型同侧 → YES qm−1", [(r, yes_pnl(r, r["qm"]-1)) for r in D if r["model_side"] == 0])
summarize("对照：模型冷侧 → YES qm−1", [(r, yes_pnl(r, r["qm"]-1)) for r in D if r["model_side"] == -1])
summarize("对照：模型暖侧 → YES qm+1(模型侧)", [(r, yes_pnl(r, r["qm"]+1)) for r in warm])
summarize("对照：模型暖侧 → YES qm", [(r, yes_pnl(r, r["qm"])) for r in warm])
summarize("镜像：模型冷侧 → YES qm+1", [(r, yes_pnl(r, r["qm"]+1)) for r in D if r["model_side"] == -1])
P("   —— 分层 ——")
for nm, cond in (("|众数差|=1", lambda r: r["pm"]-r["qm"] == 1), ("|众数差|≥2", lambda r: r["pm"]-r["qm"] >= 2),
                 ("|均值差|≤0.4", lambda r: abs(r["dmu"]) <= 0.4), ("0.4<|均值差|≤0.8", lambda r: 0.4 < abs(r["dmu"]) <= 0.8), ("|均值差|>0.8", lambda r: abs(r["dmu"]) > 0.8),
                 ("时段 36-48h", lambda r: r["band"] == "36-48h"), ("时段 24-36h", lambda r: r["band"] == "24-36h"), ("时段 12-24h", lambda r: r["band"] == "12-24h"), ("时段 <12h", lambda r: r["band"] in ("6-12h", "<6h")),
                 ("C 站", lambda r: r["unit"] == "C"), ("F 站", lambda r: r["unit"] == "F"),
                 ("市场众数价 <0.4", lambda r: r["q_qm"] < 0.4), ("市场众数价 ≥0.4", lambda r: r["q_qm"] >= 0.4),
                 ("qm−1 卖一 <0.2", lambda r: (rung(r, r["qm"]-1) or {}).get("ask") is not None and rung(r, r["qm"]-1)["ask"] < 0.2), ("qm−1 卖一 ≥0.2", lambda r: (rung(r, r["qm"]-1) or {}).get("ask") is not None and rung(r, r["qm"]-1)["ask"] >= 0.2),
                 ("TAF 在暖侧", lambda r: r.get("taf_side") == 1), ("TAF 同侧/冷侧", lambda r: r.get("taf_side") in (0, -1)), ("无 TAF", lambda r: r.get("taf_side") is None),
                 ("季节 DESCENT", lambda r: r.get("season") == "DESCENT"), ("季节 非DESCENT", lambda r: r.get("season") != "DESCENT")):
    summarize(f"  模型暖侧 ∧ {nm}", [(r, yes_pnl(r, r["qm"]-1)) for r in warm if cond(r)])
P("   —— P1 同格 ——")
D1 = [r for r in R5.values() if r["fam"] == "P1"]
summarize("P1 模型暖侧 → YES qm−1", [(r, yes_pnl(r, r["qm"]-1)) for r in D1 if r["model_side"] == 1])
summarize("P1 模型冷侧 → YES qm+1", [(r, yes_pnl(r, r["qm"]+1)) for r in D1 if r["model_side"] == -1])
P("   —— P0>36h 同格 ——")
D0 = [r for r in R5.values() if r["fam"] == "P0>36h"]
summarize("P0 模型暖侧 → YES qm−1", [(r, yes_pnl(r, r["qm"]-1)) for r in D0 if r["model_side"] == 1])
summarize("P0 模型冷侧 → YES qm+1", [(r, yes_pnl(r, r["qm"]+1)) for r in D0 if r["model_side"] == -1])

P("\n################ B. P0>36h：分歧大 (|均值差|>0.76) → 对模型众数买 NO @1−买一 ################")
far = [r for r in D0 if r["model_side"] != 0 and abs(r["dmu"]) > 0.76]
cellB = [(r, no_pnl(r, r["pm"])) for r in far]
summarize("主格：|Δμ|>0.76 → NO 模型众数", cellB); weekly("主格B", cellB); stations("主格B", cellB)
for thr in (0.5, 0.6, 0.76, 1.0, 1.3):
    summarize(f"  |Δμ|>{thr} → NO 模型众数", [(r, no_pnl(r, r["pm"])) for r in D0 if r["model_side"] != 0 and abs(r["dmu"]) > thr])
summarize("对照：|Δμ|≤0.76 → NO 模型众数", [(r, no_pnl(r, r["pm"])) for r in D0 if r["model_side"] != 0 and abs(r["dmu"]) <= 0.76])
summarize("对照：全部众数不同 → NO 模型众数", [(r, no_pnl(r, r["pm"])) for r in D0 if r["model_side"] != 0])
summarize("对照：|Δμ|>0.76 → NO 市场众数", [(r, no_pnl(r, r["qm"])) for r in far])
for nm, cond in (("模型暖侧", lambda r: r["model_side"] == 1), ("模型冷侧", lambda r: r["model_side"] == -1), ("|众数差|=1", lambda r: abs(r["pm"]-r["qm"]) == 1), ("|众数差|≥2", lambda r: abs(r["pm"]-r["qm"]) >= 2),
                 ("36-48h", lambda r: r["band"] == "36-48h"), ("48-60h", lambda r: r["band"] == "48-60h"), ("60h+", lambda r: r["band"] == "60h+"), ("C 站", lambda r: r["unit"] == "C"), ("F 站", lambda r: r["unit"] == "F"),
                 ("模型众数买一 <0.2", lambda r: (rung(r, r["pm"]) or {}).get("bid") is not None and rung(r, r["pm"])["bid"] < 0.2), ("模型众数买一 ≥0.2", lambda r: (rung(r, r["pm"]) or {}).get("bid") is not None and rung(r, r["pm"])["bid"] >= 0.2)):
    summarize(f"  |Δμ|>0.76 ∧ {nm} → NO 模型众数", [(r, no_pnl(r, r["pm"])) for r in far if cond(r)])
P("   —— P0-M / P1 同格 ——")
summarize("P0-M |Δμ|>0.76 → NO 模型众数", [(r, no_pnl(r, r["pm"])) for r in D if r["model_side"] != 0 and abs(r["dmu"]) > 0.76])
summarize("P1 |Δμ|>0.76 → NO 模型众数", [(r, no_pnl(r, r["pm"])) for r in D1 if r["model_side"] != 0 and abs(r["dmu"]) > 0.76])
