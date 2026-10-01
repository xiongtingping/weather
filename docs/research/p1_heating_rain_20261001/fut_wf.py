"""Oracle 'advance knowledge' arms, strict walk-forward, charter 240 verdict."""
import json, math, numpy as np
from collections import defaultdict, Counter
S=__file__.rsplit('/',1)[0]
FLOOR=1e-3; T1,T2=0.50,0.63
def fl(p): q=np.maximum(p,FLOOR); return q/q.sum()
def shift(p,s):
    q=np.zeros_like(p); n=len(p)
    for i,x in enumerate(p): q[min(n-1,max(0,i+s))]+=x
    return q
def widen(p):
    q=np.convolve(p,[.25,.5,.25],mode="same"); q[0]+=p[0]*.25; q[-1]+=p[-1]*.25; return q/q.sum()
def sc(p,c,h):
    q=fl(p); y=np.zeros_like(q); y[h]=1
    return np.array([-math.log(q[h]), ((q-y)**2).sum(), ((np.cumsum(q)[:-1]-np.cumsum(y)[:-1])**2*np.diff(c)).sum()])
ARMS={"shift":[(s,w) for s in (1,2) for w in np.arange(.05,.55,.05)],"widen":[(0,w) for w in np.arange(.1,1.01,.1)]}
def ap(fam,par,p,sg): s,w=par; return (1-w)*p+w*(shift(p,sg*s) if fam=="shift" else widen(p))
def load(stage):
    D=json.load(open(f"{S}/dataset_{stage}.json")); F=json.load(open(f"{S}/fut_{stage}.json")); rows=[]
    for r,f in zip(D,F):
        p=np.array(r["p"],float); p/=p.sum()
        rows.append(dict(sd=(r["station"],r["date"]),day=r["date"],w=float(r["weight"]),p=p,c=np.array(r["centers"],float),h=r["hit"],f=f))
    for r in rows: r["b"]=sc(r["p"],r["c"],r["h"])
    return rows
def run(rows,name,sign,fam,burn=10):
    E={}
    for r in rows:
        if r["f"] is None: continue
        g=sign(r["f"])
        if g: E[id(r)]=g
    cand={(id(r),par):sc(ap(fam,par,r["p"],E[id(r)]),r["c"],r["h"]) for r in rows if id(r) in E for par in ARMS[fam]}
    days=sorted({r["day"] for r in rows}); by=defaultdict(list)
    for r in rows: by[r["day"]].append(r)
    delta=defaultdict(lambda: np.zeros(3)); picks=Counter()
    for i,d in enumerate(days):
        if i<burn: continue
        tr=[r for dd in days[:i] for r in by[dd] if id(r) in E]
        if len(tr)<15: continue
        best=min(ARMS[fam],key=lambda par: sum(r["w"]*(cand[(id(r),par)][0]-r["b"][0]) for r in tr))
        if sum(r["w"]*(cand[(id(r),best)][0]-r["b"][0]) for r in tr)>=0: best=None
        picks[str(best if best is None else (best[0],round(float(best[1]),2)))]+=1
        for r in by[d]:
            delta[r["sd"]]+= 0 if (best is None or id(r) not in E) else r["w"]*(cand[(id(r),best)]-r["b"])
    M=np.array(list(delta.values())); n=len(M); aff=(M!=0).any(1).sum()
    rng=np.random.default_rng(0); B=np.array([M[rng.integers(0,n,n)].mean(0) for _ in range(2000)])
    lo,hi=np.percentile(B,2.5,0),np.percentile(B,97.5,0); mu=M.mean(0)
    v="PASS" if (hi[0]<0 and lo[1]<=0 and lo[2]<=0) else "fail"
    print(f"{name:30} {fam:5} 受影响站日{aff:4}/{n} | dLog {mu[0]:+.5f} [{lo[0]:+.5f},{hi[0]:+.5f}] dBrier {mu[1]:+.5f} [{lo[1]:+.5f},{hi[1]:+.5f}] dCRPS {mu[2]:+.5f} | {v} | {picks.most_common(2)}",flush=True)
if __name__=="__main__":
    P0=load("p0")
    print("== P0（提前完全知道当天 09时→最高温时刻 的天气）")
    arms0=[("雨≥2小时→冷",lambda f:-1 if f["rain_h"]>=2 else 0),
           ("雨≥1小时→冷",lambda f:-1 if f["rain_h"]>=1 else 0),
           ("预报晴却下雨≥1h→冷",lambda f:-1 if f["rain_h"]>=1 and f["kt"] is not None and f["kt"]>=T2 else 0),
           ("云量:阴→冷/晴→暖",lambda f:0 if f["cloud"] is None else (-1 if f["cloud"]>=0.8 else (1 if f["cloud"]<=0.2 else 0))),
           ("云量意外:预报晴却阴/预报阴却晴",lambda f:0 if f["cloud"] is None or f["kt"] is None else (-1 if f["kt"]>=T2 and f["cloud"]>=0.8 else (1 if f["kt"]<=T1 and f["cloud"]<=0.3 else 0)))]
    for nm,sg in arms0:
        for fam in ("shift","widen"): run(P0,nm,sg,fam)
    P1=load("p1")
    print("== P1（决策在当地9–14时、峰前；提前完全知道决策后到最高温时刻的天气）")
    ok=lambda f: f["before_peak"] and 9<=f["lh"]<=14
    arms1=[("将下雨≥1小时→冷",lambda f:-1 if ok(f) and f["rain_h"]>=1 else 0),
           ("将转阴→冷/将转晴→暖",lambda f:0 if not ok(f) or f["cloud"] is None else (-1 if f["cloud"]>=0.8 else (1 if f["cloud"]<=0.2 else 0))),
           ("云量意外(对预报Kt)",lambda f:0 if not ok(f) or f["cloud"] is None else (-1 if f["kt"]>=T2 and f["cloud"]>=0.8 else (1 if f["kt"]<=T1 and f["cloud"]<=0.3 else 0)))]
    for nm,sg in arms1:
        for fam in ("shift","widen"): run(P1,nm,sg,fam)
