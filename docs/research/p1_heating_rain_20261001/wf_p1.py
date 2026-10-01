"""P1 rain arm, strict walk-forward. Pre-registered primary: k=2 wet hours (09:00 local -> decision-15min),
decision local hour 9..16, cold-shift mixture p'=(1-w)p+w*shift(p,-s), (s,w) fit on closed days < D by LogScore.
Verdict per charter 240: dLog upper<0, dCRPS lower<=0, dBrier lower<=0, station-day bootstrap 2000."""
import json, math, random, sys
import numpy as np
from collections import defaultdict
S=__file__.rsplit('/',1)[0]
D=json.load(open(S+"/dataset_p1.json")); F=json.load(open(S+"/feat_p1.json"))
FLOOR=1e-3
def floor(p):
    q=np.maximum(p,FLOOR); return q/q.sum()
def shift(p,s):
    q=np.zeros_like(p)
    for i,x in enumerate(p): q[max(0,i-s)]+=x
    return q
def widen(p):
    k=np.array([0.25,0.5,0.25]); q=np.convolve(p,k,mode="same")
    q[0]+=p[0]*0.25; q[-1]+=p[-1]*0.25; return q/q.sum()
rows=[]
for r,f in zip(D,F):
    p=np.array(r["p"],float); p=p/p.sum()
    c=np.array(r["centers"],float); h=r["hit"]
    rows.append(dict(sd=(r["station"],r["date"]),day=r["date"],w=float(r["weight"]),p=p,c=c,h=h,
                     lh=f["local_hour"],wet=f["wet_hours"]))
def scores(p,c,h):
    q=floor(p); y=np.zeros_like(q); y[h]=1
    F_=np.cumsum(q)[:-1]; H=np.cumsum(y)[:-1]; dc=np.diff(c)
    return -math.log(q[h]), float(((q-y)**2).sum()), float(((F_-H)**2*dc).sum())
ARMS={"cold":[(s,w) for s in (1,2) for w in np.arange(0.05,0.55,0.05)], "widen":[(0,w) for w in np.arange(0.1,1.01,0.1)]}
def apply(fam,par,p):
    s,w=par
    return (1-w)*p+w*(shift(p,s) if fam=="cold" else widen(p))
def run(k,fam,hours=(9,16),burn=10):
    elig=lambda r: r["wet"]>=k and hours[0]<=r["lh"]<=hours[1]
    days=sorted({r["day"] for r in rows})
    byday=defaultdict(list)
    for r in rows: byday[r["day"]].append(r)
    base={id(r):scores(r["p"],r["c"],r["h"]) for r in rows}
    cand={(id(r),par):scores(apply(fam,par,r["p"]),r["c"],r["h"]) for r in rows if elig(r) for par in ARMS[fam]}
    delta=defaultdict(lambda: np.zeros(3)); picks=[]
    for i,dday in enumerate(days):
        if i<burn: continue
        train=[r for d in days[:i] for r in byday[d] if elig(r)]
        if len(train)<20: continue
        best=min(ARMS[fam],key=lambda par: sum(r["w"]*(cand[(id(r),par)][0]-base[id(r)][0]) for r in train))
        if sum(r["w"]*(cand[(id(r),best)][0]-base[id(r)][0]) for r in train)>=0: best=None
        picks.append((dday,best))
        for r in byday[dday]:
            dd=np.zeros(3) if (best is None or not elig(r)) else r["w"]*(np.array(cand[(id(r),best)])-np.array(base[id(r)]))
            delta[r["sd"]]+=dd
    sds=list(delta); M=np.array([delta[s] for s in sds]); n=len(sds)
    aff=np.array([np.any(delta[s]!=0) for s in sds])
    rng=np.random.default_rng(0); boots=[]
    for _ in range(2000):
        idx=rng.integers(0,n,n); boots.append(M[idx].mean(0))
    B=np.array(boots); lo,hi=np.percentile(B,2.5,0),np.percentile(B,97.5,0)
    # day-block bootstrap
    dd=defaultdict(lambda: np.zeros(3)); 
    for s in sds: dd[s[1]]+=delta[s]
    DM=np.array(list(dd.values())); nd=len(DM); Bd=np.array([DM[rng.integers(0,nd,nd)].sum(0)/n for _ in range(2000)])
    mean=M.mean(0)
    verdict = hi[0]<0 and lo[2]<=0 and lo[1]<=0
    print(f"k={k} {fam:5} hours={hours} station-days={n} affected={aff.sum()} test-days={len({s[1] for s in sds})}")
    for j,name in enumerate(("LogScore","Brier","CRPS")):
        print(f"   d{name:8} {mean[j]:+.5f} [{lo[j]:+.5f},{hi[j]:+.5f}]  day-block [{np.percentile(Bd[:,j],2.5):+.5f},{np.percentile(Bd[:,j],97.5):+.5f}]  per-affected {M[aff,j].mean() if aff.any() else 0:+.4f}")
    from collections import Counter
    print("   picks", Counter(str(b if b is None else (b[0],round(float(b[1]),2))) for _,b in picks).most_common(4), " verdict:", "PASS" if verdict else "fail")
for k in (2,1,3):
    for fam in ("cold","widen"):
        run(k,fam)
run(2,"cold",hours=(9,23))
