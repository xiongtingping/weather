import os, math, statistics, collections, random, pickle
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "model_side_v10b.py")).read().split('for fam in ("P0>36h","P0-M"):')[0]
src = src.replace('out = open(os.path.join(HERE, "model_side_v10b.txt"), "w")', 'out = open(os.path.join(HERE, "model_side_v10c.txt"), "w")')
exec(src)
rs=[r for r in recs if r["fam"]=="P0>36h" and r["F"] is not None]; days=sorted({r["d"] for r in rs}); ev=[]
for d in days:
    prior=[r for r in rs if r["d"]<d]
    if len({r["d"] for r in prior})<8: continue
    bias=statistics.mean(r["y"]-r["F"] for r in prior); o=ols([(r["F"]+bias-r["mc"],r["y"]-r["mc"]) for r in prior]); kap=o[1] if o else 0.0
    for r in rs:
        if r["d"]==d: r["kap"]=kap; r["bias"]=bias; ev.append(r)
fn=lambda r: r["mc"]+r["kap"]*(r["F"]+r["bias"]-r["mc"])
P("P0>36h 稳健性：")
show("  奇数日",[r for r in ev if int(r["d"][-2:])%2],fn); show("  偶数日",[r for r in ev if not int(r["d"][-2:])%2],fn)
show("  前半(≤10-10)",[r for r in ev if r["d"]<="2026-10-10"],fn); show("  后半(>10-10)",[r for r in ev if r["d"]>"2026-10-10"],fn)
by=collections.defaultdict(list)
for r in ev: by[r["d"]].append(r)
dl={d: statistics.mean(-math.log(floor_norm(shift(r["p"],(fn(r)-r["mc"])/r["rung"]))[r["yi"]])+math.log(r["p"][r["yi"]]) for r in g) for d,g in by.items()}
P("  逐日 ΔLogScore: " + " | ".join(f"{d[5:]} {v:+.3f}(n{len(by[d])})" for d,v in sorted(dl.items())))
P(f"  改善天数 {sum(1 for v in dl.values() if v<0)}/{len(dl)}")
