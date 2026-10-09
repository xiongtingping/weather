"""P1's 0.5-0.75 claims land at 0.51 (n=701).  Where: hours band, local hour, unit, before/after the P1 trajectory
fix (10-01), the claimed bucket vs the P0-M root's mode, and what P0-M said on the same bucket; P1 reliability in
every bin at h12_24 vs h0_12; and a width arm (lambda>1 = widen) on P1 h12_24 under the 240 gate."""
import json, gzip, math, collections, sys
import numpy as np
exec(open('calib_battery.py').read().split('stage=np.array')[0])
sys.path.insert(0,'/Users/xiong/weather')
from bayes_basket_target.center_width_calibration import apply_center_width_calibration, served_bucket_axis
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
from datetime import date, timedelta
by=collections.defaultdict(dict)
for r in rows: by[(r['station'],r['event_date'],r['band'])][r['stage']]=r
P1=[r for r in rows if r['stage']=='P1']
def claims(R, lo=0.5, hi=0.75):
    out=[]
    for r in R:
        for i,p in enumerate(r['f']):
            if lo<=p<hi: out.append((r,i,p,i==r['ti']))
    return out
def line(tag,C):
    if len(C)<30: return
    print(f"  {tag:34s} n={len(C):4d} said {np.mean([p for _,_,p,_ in C]):.3f} landed {np.mean([h for *_,h in C]):.3f} (gap {np.mean([h for *_,h in C])-np.mean([p for _,_,p,_ in C]):+.3f})")
print("== P1 buckets claiming 0.50–0.75: said vs landed by slice ==")
line('all P1',claims(P1))
for b in ('h0_12','h12_24','h24_36'): line(f'band {b}',claims([r for r in P1 if r['band']==b]))
for lo,hi in ((12,15),(15,18),(18,24)): line(f'hours {lo}-{hi}',claims([r for r in P1 if lo<=r['hours']<hi]))
for lo,hi in ((0,6),(6,8),(8,10),(10,12),(12,24)): line(f'local hour {lo}-{hi}',claims([r for r in P1 if lo<=r['lhour']<hi]))
for u in ('C','F'): line(f'unit {u}',claims([r for r in P1 if r['unit']==u]))
for lo,hi,lab in (('2026-09-05','2026-09-22','09-05..09-21'),('2026-09-22','2026-10-01','09-22..09-30'),('2026-10-01','2026-10-09','≥10-01 (trajectory fix)')): line(lab,claims([r for r in P1 if lo<=r['event_date']<hi]))
print("\n== the same bucket in the P0-M root (same station-day-band): what did P0-M say, and P1's move ==")
C=claims([r for r in P1 if r['band']=='h12_24'])
pairs=[]
for r,i,p,h in C:
    root=by[(r['station'],r['event_date'],r['band'])].get('P0-M') or by[(r['station'],r['event_date'],r['band'])].get('P0')
    if not root or len(root['f'])!=len(r['f']): continue
    pairs.append((p,root['f'][i],h,i==int(np.argmax(root['f'])),i==int(np.argmax(r['f']))))
print(f"  n={len(pairs)} | P1 said {np.mean([a for a,*_ in pairs]):.3f} | root said on same bucket {np.mean([b for _,b,*_ in pairs]):.3f} | landed {np.mean([h for _,_,h,*_ in pairs]):.3f} | bucket is root's mode {np.mean([m for *_,m,_ in pairs]):.2f}, is P1's mode {np.mean([m for *_,m in pairs]):.2f}")
for lab,sel in (('P1 raised it ≥0.15 over root',lambda a,b:a-b>=0.15),('P1 raised 0.05–0.15',lambda a,b:0.05<=a-b<0.15),('P1 within ±0.05 of root',lambda a,b:abs(a-b)<0.05),('P1 lowered it',lambda a,b:a-b<=-0.05)):
    P=[x for x in pairs if sel(x[0],x[1])]
    if len(P)>=30: print(f"   {lab:30s} n={len(P):3d} P1 said {np.mean([a for a,*_ in P]):.3f} root said {np.mean([b for _,b,*_ in P]):.3f} landed {np.mean([h for _,_,h,*_ in P]):.3f}")
print("\n== P1 reliability, every bin, h12_24 vs h0_12 ==")
bins=[(0,.05),(.05,.15),(.15,.3),(.3,.5),(.5,.75),(.75,.9),(.9,1.01)]
for b in ('h12_24','h0_12'):
    R=[r for r in P1 if r['band']==b]; s=f"  {b}:"
    for lo,hi in bins:
        Cb=claims(R,lo,hi)
        if len(Cb)>=50: s+=f" [{lo:.2f}-{hi:.2f}) {np.mean([p for _,_,p,_ in Cb]):.3f}->{np.mean([h for *_,h in Cb]):.3f} (n={len(Cb)})"
    print(s)
    z=[r['z'] for r in R]; print(f"     mean z {np.mean(z):+.3f} E[z²] {np.mean(np.square(z)):.2f} PIT deciles {' '.join(f'{x:.2f}' for x in np.histogram([r['pit'] for r in R],bins=[0,.1,.2,.3,.4,.5,.6,.7,.8,.9,1.0001])[0]/len(R))}")
print("\n== arm: widen P1 h12_24 by lambda>1 (production transport), walk-forward by prefix LogScore, 240 gate ==")
for r in P1:
    r['prob']={k:v for k,v in zip(r['keys'],r['p'])}; r['centers'],r['edges']=served_bucket_axis(r['intervals'],r['keys']); r['excl']=tuple(k for k,v in r['prob'].items() if v==0.0)
    r['base']=_served_scores(r['prob'],ordered_keys=r['keys'],settled_bucket=r['truth'],excluded_buckets=r['excl'],key_centers_c=r['centers'])
LG=[1.0,1.1,1.2,1.3,1.4,1.5]
R=[r for r in P1 if r['band']=='h12_24']; S=np.zeros((len(R),len(LG),3))
for i,r in enumerate(R):
    for j,l in enumerate(LG):
        if l==1.0: S[i,j]=r['base']; continue
        v,_=apply_center_width_calibration(r['prob'],delta_c=0.0,width_lambda=l,ordered_keys=r['keys'],key_centers_c=r['centers'],excluded_buckets=r['excl'],open_edge_bounds_c=r['edges'])
        S[i,j]=_served_scores(v,ordered_keys=r['keys'],settled_bucket=r['truth'],excluded_buckets=r['excl'],key_centers_c=r['centers'])
ed=np.array([r['event_date'] for r in R]); ka=np.array([r['known_at'] for r in R]); days=sorted(set(ed)); chosen=np.full(len(R),-1)
for d in days:
    dm=(date.fromisoformat(d)-timedelta(days=2)).isoformat(); idx=np.where((ed<=dm)&(ka<=d+'T00:00:00Z'))[0]
    if len({ed[i] for i in idx})<8: continue
    sel=np.where(ed==d)[0]; chosen[sel]=int(np.argmin(S[idx][:,:,0].mean(axis=0)))
sel=np.where(chosen>=0)[0]
g=_proper_score_gate([tuple(R[i]['base']) for i in sel],[tuple(S[i,chosen[i]]) for i in sel],[(R[i]['station'],R[i]['event_date']) for i in sel])
f=lambda v,ci: f"{v:+.4f} [{ci[0]:+.4f},{ci[1]:+.4f}]{'*' if ci and (ci[0]>0 or ci[1]<0) else ''}" if ci else '—'
print(f"  n={len(sel)} ΔLog {f(g['d_logscore'],g['ci_logscore'])} ΔCRPS {f(g['d_crps'],g['ci_crps'])} ΔBrier {f(g['d_brier'],g['ci_brier'])} -> {'PASS' if g['lower'] else 'no'} | λ chosen {dict(sorted(collections.Counter(LG[chosen[i]] for i in sel).items()))}")
for lam in (1.1,1.2,1.3):
    j=LG.index(lam); g=_proper_score_gate([tuple(r['base']) for r in R],[tuple(S[i,j]) for i in range(len(R))],[(r['station'],r['event_date']) for r in R])
    print(f"  fixed λ={lam}: n={len(R)} ΔLog {f(g['d_logscore'],g['ci_logscore'])} ΔCRPS {f(g['d_crps'],g['ci_crps'])} ΔBrier {f(g['d_brier'],g['ci_brier'])} -> {'PASS' if g['lower'] else 'no'}")
