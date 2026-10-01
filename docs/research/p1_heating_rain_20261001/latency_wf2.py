import json, math, numpy as np
from collections import defaultdict, Counter
S=__file__.rsplit('/',1)[0]
D=json.load(open(S+"/dataset_p1.json")); L=json.load(open(S+"/latency_p1.json"))
FLOOR=1e-3
def fl(p): q=np.maximum(p,FLOOR); return q/q.sum()
def shift(p,s):
    q=np.zeros_like(p)
    for i,x in enumerate(p): q[max(0,i-s)]+=x
    return q
def widen(p):
    q=np.convolve(p,[.25,.5,.25],mode="same"); q[0]+=p[0]*.25; q[-1]+=p[-1]*.25; return q/q.sum()
def sc(p,c,h):
    q=fl(p); y=np.zeros_like(q); y[h]=1
    return np.array([-math.log(q[h]), ((q-y)**2).sum(), ((np.cumsum(q)[:-1]-np.cumsum(y)[:-1])**2*np.diff(c)).sum()])
rows=[]
for r,o in zip(D,L):
    p=np.array(r["p"],float); p/=p.sum()
    rows.append(dict(sd=(r["station"],r["date"]),day=r["date"],w=float(r["weight"]),p=p,c=np.array(r["centers"],float),h=r["hit"],o=o))
ARMS={"cold":[(s,w) for s in (1,2) for w in np.arange(.05,.55,.05)],"widen":[(0,w) for w in np.arange(.1,1.01,.1)]}
def ap(f,par,p): s,w=par; return (1-w)*p+w*(shift(p,s) if f=="cold" else widen(p))
base={id(r):sc(r["p"],r["c"],r["h"]) for r in rows}
def run(name,elig,fam,burn=10):
    E=[r for r in rows if r["o"] and 9<=r["o"]["lh"]<=16 and elig(r["o"])]
    cand={(id(r),par):sc(ap(fam,par,r["p"]),r["c"],r["h"]) for r in E for par in ARMS[fam]}
    ids={id(r) for r in E}
    days=sorted({r["day"] for r in rows}); by=defaultdict(list)
    for r in rows: by[r["day"]].append(r)
    delta=defaultdict(lambda: np.zeros(3)); picks=Counter()
    for i,d in enumerate(days):
        if i<burn: continue
        tr=[r for dd in days[:i] for r in by[dd] if id(r) in ids]
        if len(tr)<15: continue
        best=min(ARMS[fam],key=lambda par: sum(r["w"]*(cand[(id(r),par)][0]-base[id(r)][0]) for r in tr))
        if sum(r["w"]*(cand[(id(r),best)][0]-base[id(r)][0]) for r in tr)>=0: best=None
        picks[str(best if best is None else (best[0],round(float(best[1]),2)))]+=1
        for r in by[d]:
            delta[r["sd"]]+= 0 if (best is None or id(r) not in ids) else r["w"]*(cand[(id(r),best)]-base[id(r)])
    M=np.array(list(delta.values())); n=len(M); aff=(M!=0).any(1).sum()
    rng=np.random.default_rng(0); B=np.array([M[rng.integers(0,n,n)].mean(0) for _ in range(2000)])
    lo,hi=np.percentile(B,2.5,0),np.percentile(B,97.5,0); mu=M.mean(0)
    v="PASS" if (hi[0]<0 and lo[1]<=0 and lo[2]<=0) else "fail"
    print(f"{name:26} {fam:5} 受影响站日{aff:4} | dLog {mu[0]:+.5f} [{lo[0]:+.5f},{hi[0]:+.5f}] dBrier {mu[1]:+.5f} [{lo[1]:+.5f},{hi[1]:+.5f}] dCRPS {mu[2]:+.5f} | {v} | {picks.most_common(3)}")
for fam in ("cold","widen"):
    run("A 实时:最新报在下",lambda o:o["now"],fam)
    run("A' 实时:在下且报龄≥30min",lambda o:o["now"] and o["age_min"]>=30,fam)
    run("B 神谕:看不见的新雨",lambda o:(not o["now"]) and o["next_rain"],fam)
    run("C 神谕:在下且下一报仍下",lambda o:o["now"] and o["next_rain"],fam)
    run("D 神谕:下一报有雨(全部)",lambda o:o["next_rain"],fam)
