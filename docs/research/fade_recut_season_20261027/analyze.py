#!/usr/bin/env python3
"""PROTOCOL.md sections 2-4 on samples.jsonl -> analyze.txt.  Run once."""
import collections, json, os, random, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
SPLIT = "2026-10-12"  # discovery <= SPLIT < confirmation
FEE = 0.05
STAKE = 5.0
FAMS, SEASONS, SIDES = ("far", "near"), ("PEAK", "DESCENT", "TROUGH", "ASCENT", "ALLYEAR"), (1, -1)
TOOLS = ("Y_qm", "Y_opp1", "Y_opp2", "Y_mod1", "N_pm", "N_qm")
out = open(os.path.join(HERE, "analyze.txt"), "w")


def P(*a):
    print(*a); print(*a, file=out)


def mode(v):
    return max(range(len(v)), key=lambda i: (v[i], -i))


def leg(r, tool, qm, pm, s):
    k = {"Y_qm": qm, "Y_opp1": qm - s, "Y_opp2": qm - 2 * s, "Y_mod1": qm + s, "N_pm": pm, "N_qm": qm}[tool]
    if not 0 <= k < len(r["names"]):
        return None
    bid, ask, bid_sz, ask_sz = r["book"][k]
    hit_yes = r["y"] == r["names"][k]
    if tool.startswith("Y"):
        if ask is None or ask_sz is None or ask_sz < STAKE / ask:
            return None
        price, hit = ask, hit_yes
    else:
        if bid is None or bid_sz is None or bid_sz < STAKE / (1 - bid):
            return None
        price, hit = 1 - bid, not hit_yes
    fee = FEE * price * (1 - price)
    return float(hit) - price - fee, price + fee


def boot(rows, B=2000, seed=7):
    by = collections.defaultdict(list)
    for r, (pnl, _) in rows:
        by[r["d"]].append(pnl)
    days = sorted(by); rnd = random.Random(seed); v = []
    for _ in range(B):
        xs = [x for d in (rnd.choice(days) for _ in days) for x in by[d]]
        v.append(statistics.mean(xs))
    v.sort()
    return v[int(.025 * B)], v[int(.975 * B)]


def read(rows):
    if not rows:
        return None
    m = statistics.mean(p for _, (p, _) in rows); c = statistics.mean(c for _, (_, c) in rows)
    lo, hi = boot(rows) if len({r["d"] for r, _ in rows}) >= 2 else (float("nan"),) * 2
    return {"n": len(rows), "days": len({r["d"] for r, _ in rows}), "m": m, "lo": lo, "hi": hi, "cost": c}


def fmt(x):
    return "—" if x is None else f"n{x['n']:4d}/{x['days']:2d}天 {x['m']:+6.1%}/份 [{x['lo']:+.1%},{x['hi']:+.1%}] 成本{x['cost']:.2f}"


S = [json.loads(l) for l in open(os.path.join(HERE, "samples.jsonl"))]
strata = collections.defaultdict(list)
for r in S:
    qm, pm = mode(r["q"]), mode(r["p"])
    s = (pm > qm) - (pm < qm)
    if s:
        strata[(r["fam"], r["season"], s)].append((r, qm, pm, s))

P(f"样本 {len(S)} 决策；事件日 {min(r['d'] for r in S)}..{max(r['d'] for r in S)}；切分 ≤{SPLIT} 发现 / 之后确认")
P("\n## 规模（先算规模，PROTOCOL 4）")
judgeable = []
for fam in FAMS:
    for se in SEASONS:
        for s in SIDES:
            rs = strata.get((fam, se, s), [])
            d1 = len({r["d"] for r, *_ in rs if r["d"] <= SPLIT}); d2 = len({r["d"] for r, *_ in rs if r["d"] > SPLIT})
            ok = len(rs) >= 60 and d1 >= 8 and d2 >= 8
            if ok:
                judgeable.append((fam, se, s))
            P(f"  {fam:4s} {se:7s} {'暖' if s > 0 else '冷'}  n={len(rs):4d}  发现{d1:2d}天 确认{d2:2d}天  {'可判' if ok else '不可判'}")

P("\n## 可判层 × 六工具")
cands, n_cells = [], 0
for key in judgeable:
    fam, se, s = key
    P(f"\n### {fam} {se} 模型{'暖' if s > 0 else '冷'}侧")
    for tool in TOOLS:
        n_cells += 1
        rows = [(r, x) for r, qm, pm, s_ in strata[key] if (x := leg(r, tool, qm, pm, s_)) is not None]
        disc = read([(r, x) for r, x in rows if r["d"] <= SPLIT])
        conf = read([(r, x) for r, x in rows if r["d"] > SPLIT])
        full = read(rows)
        cand = bool(disc and disc["m"] > 0 and disc["lo"] > 0)
        P(f"  {tool:7s} 发现 {fmt(disc)} | 确认 {fmt(conf)} | 全窗 {fmt(full)}{'  ←候选' if cand else ''}")
        if cand:
            cands.append((key, tool, rows, conf))

P(f"\n## 多重比较：可判格 {n_cells} 个，发现段下界>0 {len(cands)} 个，零假设期望 {n_cells * 0.025:.1f} 个")
P("\n## 候选判定")
if not cands:
    P("  无候选。")
for (fam, se, s), tool, rows, conf in cands:
    by_st = collections.defaultdict(float)
    for r, (p, _) in rows:
        by_st[r["st"]] += p
    top3 = [k for k, _ in sorted(by_st.items(), key=lambda kv: -kv[1])[:3]]
    rest = [p for r, (p, _) in rows if r["st"] not in top3]
    rest_m = statistics.mean(rest) if rest else float("nan")
    ok = bool(conf and conf["m"] > 0 and conf["lo"] > 0 and rest_m > 0)
    P(f"  {fam} {se} {'暖' if s > 0 else '冷'} {tool}: 确认 {fmt(conf)}; 去贡献最大3站({','.join(top3)}) {rest_m:+.1%}/份 → {'通过' if ok else '不通过'}")
