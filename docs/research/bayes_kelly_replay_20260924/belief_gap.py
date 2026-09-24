import gzip, json, math, numpy as np, collections
from datetime import date, timedelta
F=1e-6
ov=json.load(open("venue_truth_overrides.json"))["overrides"]
raw=[json.loads(l) for l in gzip.open("dataset.jsonl.gz","rt")]
rows=[]
for r in raw:
    if not r["m"]: continue
    t=ov.get(f"{r['st']}|{r['day']}",r["truth"])
    names=[b for b in r["served"] if b in r["m"]]
    if t not in names: continue
    # order buckets by the snapshot ladder order (ordinal) using asks dict insertion order as registry order
    order=[b for b in r["asks"] if b in names]
    if len(order)!=len(names): order=names
    s=np.array([max(r["served"][b],0.0) for b in order]); s=s/s.sum()
    m=np.array([r["m"][b] for b in order]); m=m/m.sum()
    rows.append(dict(key=(r["st"],r["day"]),day=r["day"],H=r["H"],bin="far" if r["H"]>=24 else ("mid" if r["H"]>=10 else "near"),s=s,m=m,t=order.index(t),stage=r["stage"]))
def lsc(p,t): return math.log(max(p[t],F))
def recal(p,lam,eps):
    q=np.power(np.maximum(p,1e-12),lam); q=q/q.sum(); u=np.ones_like(q)/len(q); return (1-eps)*q+eps*u
def mom(p):
    k=np.arange(len(p)); mu=(p*k).sum(); sd=math.sqrt(max((p*(k-mu)**2).sum(),1e-4)); return mu,sd
def disc_normal(mu,sd,n):
    edges=np.arange(n+1)-0.5; edges[0]=-np.inf; edges[-1]=np.inf
    from math import erf
    cdf=lambda x: 0.0 if x==-np.inf else (1.0 if x==np.inf else 0.5*(1+erf((x-mu)/(sd*math.sqrt(2)))))
    q=np.array([cdf(edges[i+1])-cdf(edges[i]) for i in range(n)]); q=np.maximum(q,1e-9); return q/q.sum()
LAM=[0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0,1.1]; EPS=[0,0.005,0.01,0.02,0.05,0.1]
days=sorted({r["day"] for r in rows})
out=collections.defaultdict(list)
for d in days:
    cut=(date.fromisoformat(d)-timedelta(days=3)).isoformat()
    for b in ("far","mid","near"):
        tr=[r for r in rows if r["bin"]==b and r["day"]<=cut]
        if len({r["day"] for r in tr})<8: continue
        te=[r for r in rows if r["bin"]==b and r["day"]==d]
        if not te: continue
        best=max(((l,e) for l in LAM for e in EPS), key=lambda le: np.mean([lsc(recal(r["s"],*le),r["t"]) for r in tr]))
        for r in te:
            n=len(r["s"]); mu_s,sd_s=mom(r["s"]); mu_m,sd_m=mom(r["m"])
            out[b].append(dict(day=d,key=r["key"],
                served=lsc(r["s"],r["t"]),market=lsc(r["m"],r["t"]),recal=lsc(recal(r["s"],*best),r["t"]),
                N_model=lsc(disc_normal(mu_s,sd_s,n),r["t"]),N_market=lsc(disc_normal(mu_m,sd_m,n),r["t"]),
                mkt_centre_model_width=lsc(disc_normal(mu_m,sd_s,n),r["t"]),model_centre_mkt_width=lsc(disc_normal(mu_s,sd_m,n),r["t"]),
                s_t=r["s"][r["t"]],m_t=r["m"][r["t"]],
                mode_hit_s=int(np.argmax(r["s"])==r["t"]),mode_hit_m=int(np.argmax(r["m"])==r["t"]),
                dist_s=abs(mu_s-r["t"]),dist_m=abs(mu_m-r["t"]),sd_s=sd_s,sd_m=sd_m,lam=best[0],eps=best[1]))
def sdmean(v,f):
    g=collections.defaultdict(list)
    for x in v: g[x["key"]].append(f(x))
    return float(np.mean([np.mean(a) for a in g.values()]))
for b in ("far","mid","near"):
    v=out[b]; M=sdmean(v,lambda x:x["market"])
    print(f"== {b}: 站日 {len({x['key'] for x in v})}")
    for k in ("served","recal","N_model","N_market","mkt_centre_model_width","model_centre_mkt_width"):
        print(f"   {k:24s} 对数得分 {sdmean(v,lambda x:x[k]):.3f}  对价格 {sdmean(v,lambda x:x[k])-M:+.3f}")
    print(f"   众数命中 模型 {sdmean(v,lambda x:x['mode_hit_s']):.3f} 价格 {sdmean(v,lambda x:x['mode_hit_m']):.3f}; 均值离真值(桶) 模型 {sdmean(v,lambda x:x['dist_s']):.2f} 价格 {sdmean(v,lambda x:x['dist_m']):.2f}; 宽度sd(桶) 模型 {sdmean(v,lambda x:x['sd_s']):.2f} 价格 {sdmean(v,lambda x:x['sd_m']):.2f}")
    gap=[x["served"]-x["market"] for x in v]; tail=[g for x,g in zip(v,gap) if x["s_t"]<0.02]
    print(f"   真值桶概率<0.02 的行占 {sum(1 for x in v if x['s_t']<0.02)/len(v):.1%}，贡献差距 {sum(tail)/sum(gap):.0%}；拟合的 λ,ε 最近 {v[-1]['lam']},{v[-1]['eps']}")
