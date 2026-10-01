import json, statistics as stt
from collections import defaultdict
from datetime import date
R=json.load(open(__file__.rsplit('/',1)[0]+"/rows.json"))
def q(xs,ps=(0.1,0.25,0.5,0.75,0.9)):
    xs=sorted(xs); n=len(xs)
    return [xs[min(n-1,int(p*n))] for p in ps]
# raw physical effect: truth vs same station's dry-heating-window days within +-7d
P0=[r for r in R if r["stage"]=="P0"]
by=defaultdict(list)
for r in P0: by[r["station"]].append(r)
for r in P0:
    d=date.fromisoformat(r["date"])
    ref=[o["truth"] for o in by[r["station"]] if o["wet_hours"]==0 and o is not r and abs((date.fromisoformat(o["date"])-d).days)<=7]
    r["raw"]=r["truth"]-stt.median(ref) if len(ref)>=4 else None
def cellname(r):
    f="模型说雨" if (r["fc_heat_mm"] or 0)>=1.0 else ("模型说干" if (r["fc_heat_mm"] or 0)<0.2 else None)
    o="站上没下" if r["wet_hours"]==0 else ("下1小时" if r["wet_hours"]==1 else "下≥2小时")
    return f and f"{f}·{o}"
def table(rows, key, title):
    print("\n==",title)
    print(f"{'格':16}{'n':>5} | {'真值−中心 p10 p25 p50 p75 p90':^34} | {'≤−2°C':>6}{'≥+2°C':>6} | {'模型给真值桶 p50':>8} {'市场 p50':>7} | 原始效应p50/p25")
    g=defaultdict(list)
    for r in rows:
        c=cellname(r)
        if c: g[c].append(r)
    for c in sorted(g):
        rs=g[c]; res=[r["truth"]-r[key] for r in rs]
        raw=[r["raw"] for r in rs if r.get("raw") is not None]
        rq=q(raw,(0.5,0.25)) if len(raw)>=5 else ["-","-"]
        print(f"{c:16}{len(rs):5} | "+" ".join(f"{x:+5.1f}" for x in q(res))+f" | {sum(x<=-2 for x in res)/len(rs):6.0%}{sum(x>=2 for x in res)/len(rs):6.0%} | {stt.median(r['p_hit'] for r in rs):8.3f} {stt.median(r['m_hit'] for r in rs):7.3f} | "+(" / ".join(f"{x:+.1f}" for x in rq) if raw and len(raw)>=5 else "-"))
table(P0,"mu","P0 首决策：真值 − P0 中心（°C）")
table(P0,"mc","P0 首决策：真值 − 市场中心（°C）")
P1=[r for r in R if r["stage"]=="P1"]
table(P1,"mu","P1（当地11–15时、峰前）：真值 − P1 中心；雨=09时到决策时刻")
# ASOS mm dose
print("\n== P0 美国ASOS 峰前加热窗实测毫米（09时→最高温时刻）")
for lo,hi in ((0,0.01),(0.01,2),(2,10),(10,999)):
    rs=[r for r in P0 if r["obs_mm"] is not None and lo<=r["obs_mm"]<hi]
    if len(rs)<3: continue
    res=[r["truth"]-r["mu"] for r in rs]; raw=[r["raw"] for r in rs if r.get("raw") is not None]
    print(f"[{lo},{hi}) mm n={len(rs)} 真值−P0 "+" ".join(f"{x:+.1f}" for x in q(res))+(f" | 原始 p50 {stt.median(raw):+.1f}" if raw else ""))
