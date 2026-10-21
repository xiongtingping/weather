import json, os, random, collections, statistics, math
HERE = os.path.dirname(os.path.abspath(__file__))
D = json.load(open(os.path.join(HERE, "disagree.json")))
def boot(rows, f, B=400, seed=1):
    days = sorted({r["d"] for r in rows}); by = collections.defaultdict(list)
    for r in rows: by[r["d"]].append(r)
    rnd = random.Random(seed); vals = []
    for _ in range(B):
        s = [x for d in (rnd.choice(days) for _ in days) for x in by[d]]
        if s: vals.append(f(s))
    vals.sort(); return vals[int(.025*len(vals))], vals[int(.975*len(vals))]
def rate(rows, k): return sum(r[k] for r in rows)/len(rows)
def diff(rows): return rate(rows, "p_hit") - rate(rows, "q_hit")
def edge(rows): return rate(rows, "p_hit") - statistics.mean(r["q_at_pm"] for r in rows)
def cell(rows):
    if len(rows) < 15: return None
    lo, hi = boot(rows, diff); elo, ehi = boot(rows, edge, seed=2)
    return dict(n=len(rows), p=rate(rows,"p_hit"), q=rate(rows,"q_hit"), d=diff(rows), lo=lo, hi=hi, e=edge(rows), elo=elo, ehi=ehi)
def fmt(c):
    if c is None: return "n<15"
    return f"n={c['n']:4d} P0 {c['p']:5.1%} 市 {c['q']:5.1%} 差{c['d']:+6.1%}[{c['lo']:+.0%},{c['hi']:+.0%}] 买P0桶edge {c['e']:+6.1%}[{c['elo']:+.0%},{c['ehi']:+.0%}]"
NUM = ["h","p_pm","q_qm","q_at_pm","p_at_qm","q_second","dmu","absdmu","sd_p","sd_q","ent_q","sys_sd","sys_side_frac","n_sys",
       "spread_qm","spread_pm","maxsp","ms","bridge_gap","clear","clear_anom","age_h","gap_q","gap_p","sharp_ratio"]
CAT = ["unit","warm","qm_edge","absdmode","band","season","n_sys"]
hits = []
def report(name, groups):
    print(f"\n### {name}")
    for g, rows in groups:
        A = [r for r in rows if r["win"]=="A"]; B = [r for r in rows if r["win"]=="B"]
        ca, cb, cc = cell(A), cell(B), cell(rows)
        print(f"  {str(g):22s} 全 {fmt(cc)}\n  {'':22s} A {fmt(ca)}\n  {'':22s} B {fmt(cb)}")
        if ca and cb and ca["d"] > 0 and cb["d"] > 0: hits.append((name, g, cc, ca, cb))
for k in CAT:
    vals = sorted({r[k] for r in D if r.get(k) is not None}, key=str)
    report(k, [(v, [r for r in D if r.get(k) == v]) for v in vals])
for k in NUM:
    rows = [r for r in D if r.get(k) is not None]
    if len(rows) < 60: continue
    v = sorted(r[k] for r in rows); q1, q2 = v[len(v)//3], v[2*len(v)//3]
    report(k, [(f"低 ≤{q1:.3g}", [r for r in rows if r[k] <= q1]), (f"中", [r for r in rows if q1 < r[k] <= q2]), (f"高 >{q2:.3g}", [r for r in rows if r[k] > q2])])
# stations
report("station", [(s, [r for r in D if r["st"]==s]) for s in sorted({r["st"] for r in D})])
print("\n\n=== 两窗同为正(P0命中>市场命中) 的格 ===")
for name, g, cc, ca, cb in hits:
    print(f"{name}={g}: 全{fmt(cc)} | A差{ca['d']:+.1%} B差{cb['d']:+.1%}")
print("\n=== 两窗同为 买P0桶edge>0 的格 ===")
for k in CAT + NUM:
    pass
