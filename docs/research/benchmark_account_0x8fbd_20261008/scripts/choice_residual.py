"""Where the information hides: (a) are their atypical picks (low choice-model probability) the ones that win?
(b) is 'fade the last hour's move' profitable on its own, for everyone, or only when they take it?
(c) does size mark the atypical picks?  (d) persistence (yesterday's truth bucket) as a standalone rule."""
import json, gzip, math, collections
import numpy as np
from scipy.optimize import minimize
exec(open('choice_fit.py').read().split("print('events',len(data)")[0])  # reuse loaders/features/build (no printing)
allX=np.vstack([d[1] for d in data]); mu=allX.mean(axis=0); sd=allX.std(axis=0)+1e-9
def nll(beta, D, lam=1e-3):
    tot=0.0; g=np.zeros_like(beta)
    for r,X,ch in D:
        Z=(X-mu)/sd; u=Z@beta; u-=u.max(); e=np.exp(u); P=e/e.sum(); tot-=math.log(max(P[ch],1e-12)); g-=Z[ch]-P@Z
    return tot/len(D)+lam*beta@beta, g/len(D)+2*lam*beta
train=[d for d in data if d[0]['event_date']<'2026-10-01']; test=[d for d in data if d[0]['event_date']>='2026-10-01']
beta=minimize(nll,np.zeros(len(FEATS)),args=(train,),jac=True,method='L-BFGS-B').x
def mean(x): return sum(x)/len(x) if x else float('nan')
def outcome(r,a):
    win=(a['ord']==r['truth_ord']) if a['side']=='Yes' else (a['ord']!=r['truth_ord']); return win, win/a['price']-1
print("== (a) their picks by how typical the choice model found them (test ≥10-01) ==")
recs=[]
for r,X,ch in test:
    Z=(X-mu)/sd; u=Z@beta; u-=u.max(); P=np.exp(u); P/=P.sum(); rank=int((P>P[ch]).sum())+1
    win,ret=outcome(r,r['alts'][ch]); recs.append(dict(p=P[ch],rank=rank,win=win,ret=ret,usdc=r['usdc'],side=r['alts'][ch]['side'],price=r['alts'][ch]['price']))
for lo,hi,lab in ((1,1,'rank 1 (model agrees)'),(2,3,'rank 2–3'),(4,8,'rank 4–8'),(9,99,'rank ≥9 (atypical)')):
    R=[x for x in recs if lo<=x['rank']<=hi]
    if R: print(f"  {lab:24s} n={len(R):3d} hit {mean([x['win'] for x in R]):.3f} return {mean([x['ret'] for x in R])*100:+.1f}% | median size {np.median([x['usdc'] for x in R]):.0f} U | mean price {mean([x['price'] for x in R]):.2f} | NO share {mean([x['side']=='No' for x in R]):.2f}")
print("\n== (c) size vs typicality (test): correlation of log size with choice-model probability of their pick ==")
print(f"  corr(log usdc, log P_model) = {np.corrcoef([math.log(x['usdc']+1) for x in recs],[math.log(max(x['p'],1e-6)) for x in recs])[0,1]:+.3f}")
for lo,hi,lab in ((0,10,'size <10 U'),(10,50,'10–50 U'),(50,1e9,'≥50 U')):
    R=[x for x in recs if lo<=x['usdc']<hi]
    if R: print(f"  {lab:12s} n={len(R):3d} hit {mean([x['win'] for x in R]):.3f} return {mean([x['ret'] for x in R])*100:+.1f}% | model P of their pick {mean([x['p'] for x in R]):.3f} | rank≥4 share {mean([x['rank']>=4 for x in R]):.2f}")
print("\n== (b) 'fade the last hour's move' as a rule for everyone (all events, all alternatives with a 60-min mid) ==")
grp=collections.defaultdict(lambda: {'their':[], 'others':[]})
for r,X,ch in data:
    for j,a in enumerate(r['alts']):
        if a['mom'] is None: continue
        m='down' if a['mom']<-0.01 else 'up' if a['mom']>0.01 else 'flat'
        if abs(a['d_mode'])>2: continue
        win,ret=outcome(r,a); grp[(a['side'],m)]['their' if j==ch else 'others'].append((win,ret,a['price']))
for k in sorted(grp):
    t=grp[k]['their']; o=grp[k]['others']
    if len(o)<40: continue
    print(f"  {str(k):16s} their picks n={len(t):3d} hit {mean([x[0] for x in t]):.3f} ret {mean([x[1] for x in t])*100 if t else float('nan'):+.1f}% | NOT picked n={len(o):5d} hit {mean([x[0] for x in o]):.3f} ret {mean([x[1] for x in o])*100:+.1f}% (mean price {mean([x[2] for x in o]):.2f})")
print("\n== (d) persistence: buy YES on yesterday's-truth bucket (and +1) at the ask, everyone vs their picks (all events, |d_mode|≤2) ==")
for off in (-1,0,1):
    t=[];o=[]
    for r,X,ch in data:
        if r['ytruth_c'] is None: continue
        w=1.111 if r['unit']=='F' else 1.0
        for j,a in enumerate(r['alts']):
            if a['side']!='Yes' or abs(a['d_mode'])>2: continue
            c=centre_c(a['bucket'],r['unit'])
            if c is None or round((c-r['ytruth_c'])/w)!=off: continue
            (t if j==ch else o).append(outcome(r,a)+(a['price'],))
    print(f"  centre − yesterday truth = {off:+d}: their picks n={len(t):3d} hit {mean([x[0] for x in t]):.3f} ret {mean([x[1] for x in t])*100 if t else float('nan'):+.1f}% | not picked n={len(o):4d} hit {mean([x[0] for x in o]):.3f} ret {mean([x[1] for x in o])*100:+.1f}% price {mean([x[2] for x in o]):.2f}")
print("\n== (e) within their favourite cells (YES mode±1 priced 0.1–0.5; NO mode/−1 priced 0.3–0.7): picked vs not picked ==")
for lab,cond in (('YES mode±1 @0.1–0.5',lambda a:a['side']=='Yes' and abs(a['d_mode'])==1 and 0.1<=a['price']<0.5),('NO mode/−1 @0.3–0.7',lambda a:a['side']=='No' and a['d_mode'] in (0,-1) and 0.3<=a['price']<0.7),('YES mode @0.2–0.6',lambda a:a['side']=='Yes' and a['d_mode']==0 and 0.2<=a['price']<0.6)):
    t=[];o=[]
    for r,X,ch in data:
        for j,a in enumerate(r['alts']):
            if cond(a): (t if j==ch else o).append(outcome(r,a)+(a['price'],))
    print(f"  {lab:22s} picked n={len(t):3d} hit {mean([x[0] for x in t]):.3f} ret {mean([x[1] for x in t])*100:+.1f}% price {mean([x[2] for x in t]):.2f} | not picked n={len(o):4d} hit {mean([x[0] for x in o]):.3f} ret {mean([x[1] for x in o])*100:+.1f}% price {mean([x[2] for x in o]):.2f}")
