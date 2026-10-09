"""Ours vs the market under the two rulers on the neutral baseline (1,480 station-days x 6 moments):
the whole-ladder LogScore gap, and the binary LogLoss gap bucket by bucket (market mode, our mode,
neighbours, far), on all station-days and on the cluster-traded ones."""
import json, math, collections
import numpy as np
B=json.load(open('baseline3.json')); E=json.load(open('events_enriched.json'))
traded={(e['station'],e['event_date']) for e in E}
F=1e-3
def bll(p,h): p=min(1-F,max(F,p)); return -(h*math.log(p)+(1-h)*math.log(1-p))
def norm_floor(p, keys):
    v={k:max(F,float(p.get(k,0.0))) for k in keys}; s=sum(v.values()); return {k:x/s for k,x in v.items()}
rec=[]
for key,v in B.items():
    st,ed=key.split('|'); tb=v.get('settled_bucket')
    if not tb: continue
    for mom,m in v.items():
        if not isinstance(m,dict) or not m.get('ours') or not m.get('market_q'): continue
        o=m['ours']; ordn=o['ord']; keys=sorted(ordn,key=lambda k:ordn[k])
        if tb not in ordn: continue
        q=m['market_q']
        if sum(q.values())<=0: continue
        p=norm_floor(o['p'],keys); qq=norm_floor(q,keys)
        mo=max(keys,key=lambda k:p[k]); mq=m.get('market_mode') or max(keys,key=lambda k:qq[k])
        if mq not in ordn: continue
        rec.append(dict(st=st,ed=ed,mom=mom,stage=o['stage'],p=p,q=qq,keys=keys,ordn=ordn,truth=tb,mo=mo,mq=mq,traded=(st,ed) in traded))
print('moments with both vectors:',len(rec))
cl=np.array([hash((r['st'],r['ed'])) for r in rec])
def boot(d):
    d=np.asarray([np.nan if x is None else x for x in d],dtype=float); ok=~np.isnan(d); d=d[ok]; c=cl[ok]
    ids={x:i for i,x in enumerate(set(c))}; idx=np.array([ids[x] for x in c]); n=len(ids)
    sums=np.bincount(idx,weights=d,minlength=n); cnt=np.bincount(idx,minlength=n).astype(float)
    rng=np.random.default_rng(1); draws=rng.integers(0,n,size=(2000,n)); m=sums[draws].sum(axis=1)/cnt[draws].sum(axis=1)
    return float(d.mean()), float(np.percentile(m,2.5)), float(np.percentile(m,97.5))
def ruler(sel, tag):
    global cl
    R=[r for r,s in zip(rec,sel) if s]; cl_all=cl; cl=cl_all[np.where(sel)[0]]
    dls=[-math.log(r['p'][r['truth']])+math.log(r['q'][r['truth']]) for r in R]
    def dbin(pick):
        out=[]
        for r in R:
            k=pick(r)
            if k is None: out.append(None); continue
            h=1.0 if k==r['truth'] else 0.0; out.append(bll(r['p'][k],h)-bll(r['q'][k],h))
        return out
    def pos(r,off):
        i=r['ordn'][r['mq']]+off
        return next((k for k in r['keys'] if r['ordn'][k]==i),None)
    parts={'whole-ladder ΔLogScore':dls,'binary ΔLL on market mode':dbin(lambda r:r['mq']),'on our mode':dbin(lambda r:r['mo']),
           'on mkt mode +1':dbin(lambda r:pos(r,1)),'on mkt mode −1':dbin(lambda r:pos(r,-1)),'on mkt mode ±2 (sum both)':None}
    far=[]
    for r in R:
        s=0.0
        for off in (-2,2):
            k=pos(r,off)
            if k: h=1.0 if k==r['truth'] else 0.0; s+=bll(r['p'][k],h)-bll(r['q'][k],h)
        far.append(s)
    parts['on mkt mode ±2 (sum both)']=far
    line=f"  {tag:28s} n={len(R):5d} |"
    for lab,d in parts.items():
        m,lo,hi=boot(d); line+=f" {lab} {m:+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} |"
    cl=cl_all; print(line)
    # where each ruler says who is better
    print(f"      ours better share: LogScore {np.mean([d<0 for d in dls]):.2f} | binary on mkt mode {np.mean([d<0 for d in parts['binary ΔLL on market mode'] if d is not None]):.2f} | on our mode {np.mean([d<0 for d in parts['on our mode'] if d is not None]):.2f}")
sel_all=np.ones(len(rec),bool); stage=np.array([r['stage'] for r in rec]); tr=np.array([r['traded'] for r in rec])
print("\n== Ours − market under two rulers (negative = ours better) ==")
ruler(sel_all,'ALL'); 
for s in ('P0','P0-M','P1'): ruler(stage==s,s)
ruler(tr,'cluster-traded station-days'); ruler(~tr,'untouched station-days')
agree=np.array([r['mo']==r['mq'] for r in rec]); ruler(agree,'our mode = market mode'); ruler(~agree,'modes differ')
