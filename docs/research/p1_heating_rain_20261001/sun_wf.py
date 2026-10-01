import json, math, numpy as np
from collections import defaultdict, Counter
S=__file__.rsplit('/',1)[0]
D=json.load(open(S+"/dataset_p1.json")); F=json.load(open(S+"/sun_feat.json"))
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
rows=[]
for r,f in zip(D,F):
    p=np.array(r["p"],float); p/=p.sum()
    rows.append(dict(sd=(r["station"],r["date"]),day=r["date"],w=float(r["weight"]),p=p,c=np.array(r["centers"],float),h=r["hit"],f=f))
def surprise(f,k):
    if not 9<=f["lh"]<=16 or f["fkt"] is None or f[k] is None: return 0
    if f["fkt"]>=T2 and f[k]>=0.8: return -1   # forecast sunny, it clouded: colder
    if f["fkt"]<=T1 and f[k]<=0.3: return +1   # forecast dull, it cleared: warmer
    return 0
ARMS={"signed":[(s,w) for s in (1,2) for w in np.arange(.05,.55,.05)],"widen":[(0,w) for w in np.arange(.1,1.01,.1)]}
def ap(fam,par,p,sg):
    s,w=par; return (1-w)*p+w*(shift(p,sg*s) if fam=="signed" else widen(p))
base={id(r):sc(r["p"],r["c"],r["h"]) for r in rows}
def run(name,k,fam,which=(-1,1),burn=10):
    E={id(r):surprise(r["f"],k) for r in rows}; E={i:v for i,v in E.items() if v in which}
    cand={(id(r),par):sc(ap(fam,par,r["p"],E[id(r)]),r["c"],r["h"]) for r in rows if id(r) in E for par in ARMS[fam]}
    days=sorted({r["day"] for r in rows}); by=defaultdict(list)
    for r in rows: by[r["day"]].append(r)
    delta=defaultdict(lambda: np.zeros(3)); picks=Counter()
    for i,d in enumerate(days):
        if i<burn: continue
        tr=[r for dd in days[:i] for r in by[dd] if id(r) in E]
        if len(tr)<15: continue
        best=min(ARMS[fam],key=lambda par: sum(r["w"]*(cand[(id(r),par)][0]-base[id(r)][0]) for r in tr))
        if sum(r["w"]*(cand[(id(r),best)][0]-base[id(r)][0]) for r in tr)>=0: best=None
        picks[str(best if best is None else (best[0],round(float(best[1]),2)))]+=1
        for r in by[d]:
            delta[r["sd"]]+= 0 if (best is None or id(r) not in E) else r["w"]*(cand[(id(r),best)]-base[id(r)])
    M=np.array(list(delta.values())); n=len(M); aff=(M!=0).any(1).sum()
    rng=np.random.default_rng(0); B=np.array([M[rng.integers(0,n,n)].mean(0) for _ in range(2000)])
    lo,hi=np.percentile(B,2.5,0),np.percentile(B,97.5,0); mu=M.mean(0)
    v="PASS" if (hi[0]<0 and lo[1]<=0 and lo[2]<=0) else "fail"
    print(f"{name:24} {fam:6} 受影响站日{aff:4} | dLog {mu[0]:+.5f} [{lo[0]:+.5f},{hi[0]:+.5f}] dBrier {mu[1]:+.5f} [{lo[1]:+.5f},{hi[1]:+.5f}] dCRPS {mu[2]:+.5f} [{lo[2]:+.5f},{hi[2]:+.5f}] | {v} | {picks.most_common(2)}")
for fam in ("signed","widen"):
    run("实时:两类意外",'obs_mean',fam)
    run("实时:预报晴却阴(冷)",'obs_mean',fam,(-1,))
    run("实时:预报阴却晴(暖)",'obs_mean',fam,(1,))
    run("神谕:午后两类意外",'rest_mean',fam)
