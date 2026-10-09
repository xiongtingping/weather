"""Arm: a centre shift delta (°C) per stage x lead band, fitted walk-forward on prefix LogScore, applied through
the production width transport (apply_center_width_calibration, width_lambda=1) -- the lane's own residual pair
with delta made band-conditional.  Judged by the charter 240 gate per band and on all rows."""
import sys, json, gzip, math, collections, time
import numpy as np
sys.path.insert(0,'/Users/xiong/weather')
from bayes_basket_target.center_width_calibration import apply_center_width_calibration, served_bucket_axis
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
from datetime import date, timedelta
t0=time.time()
rows=[json.loads(l) for l in gzip.open('rows.jsonl.gz','rt')]
for r in rows:
    r['prob']={k:v for k,v in zip(r['keys'],r['p'])}; r['centers'],r['edges']=served_bucket_axis(r['intervals'],r['keys'])
    r['excl']=tuple(k for k,v in r['prob'].items() if v==0.0)
    r['base']=_served_scores(r['prob'],ordered_keys=r['keys'],settled_bucket=r['truth'],excluded_buckets=r['excl'],key_centers_c=r['centers'])
N=len(rows); DGRID=[round(-1.0+0.1*i,1) for i in range(15)]  # -1.0 .. +0.4
def shifted(r,d):
    if d==0.0: return r['prob']
    v,_=apply_center_width_calibration(r['prob'],delta_c=d,width_lambda=1.0,ordered_keys=r['keys'],key_centers_c=r['centers'],excluded_buckets=r['excl'],open_edge_bounds_c=r['edges']); return v
S=np.zeros((N,len(DGRID),3)); bad=0
for i,r in enumerate(rows):
    for j,d in enumerate(DGRID):
        try: S[i,j]=r['base'] if d==0.0 else _served_scores(shifted(r,d),ordered_keys=r['keys'],settled_bucket=r['truth'],excluded_buckets=r['excl'],key_centers_c=r['centers'])
        except ValueError: S[i,j]=r['base']; bad+=1
print('grid scored',round(time.time()-t0),'s; transform errors',bad)
ed=np.array([r['event_date'] for r in rows]); ka=np.array([r['known_at'] for r in rows]); stage=np.array([r['stage'] for r in rows]); band=np.array([r['band'] for r in rows]); unit=np.array([r['unit'] for r in rows])
days=sorted(set(ed))
def walk(groups):
    chosen=np.full(N,-2)
    for g in groups:
        for d in days:
            dm=(date.fromisoformat(d)-timedelta(days=2)).isoformat(); pm=g&(ed<=dm)&(ka<=d+'T00:00:00Z'); idx=np.where(pm)[0]
            if len({ed[i] for i in idx})<8: continue
            sel=np.where(g&(ed==d))[0]
            if not len(sel): continue
            means=S[idx][:,:,0].mean(axis=0); chosen[sel]=int(np.argmin(means))
    return chosen
def gate(mask,chosen,label):
    sel=np.where(mask&(chosen>=0))[0]
    if len(sel)<30: return
    base=[tuple(rows[i]['base']) for i in sel]; cand=[tuple(S[i,chosen[i]]) for i in sel]; cl=[(rows[i]['station'],rows[i]['event_date']) for i in sel]
    g=_proper_score_gate(base,cand,cl); f=lambda v,ci: f"{v:+.4f} [{ci[0]:+.4f},{ci[1]:+.4f}]{'*' if (ci[0]>0 or ci[1]<0) else ' '}" if ci else '—'
    picks=collections.Counter(DGRID[chosen[i]] for i in sel)
    print(f"  {label:30s} n={len(sel):5d}/{g['station_days']:4d}d | ΔLog {f(g['d_logscore'],g['ci_logscore'])} ΔCRPS {f(g['d_crps'],g['ci_crps'])} ΔBrier {f(g['d_brier'],g['ci_brier'])} -> {'PASS' if g['lower'] else 'no'} | δ chosen {dict(sorted(picks.items()))}")
print("\n== δ per stage × band (walk-forward by prefix LogScore) ==")
groups=[(stage==s)&(band==b) for s in ('P0','P0-M','P1') for b in ('h0_12','h12_24','h24_36','h36_48','h48+')]
ch=walk(groups)
for s in ('P0','P0-M','P1'):
    for b in ('h0_12','h12_24','h24_36','h36_48','h48+'): gate((stage==s)&(band==b),ch,f'{s} {b}')
    gate(stage==s,ch,f'{s} all bands')
gate(np.ones(N,bool),ch,'ALL rows'); gate(ed>='2026-10-01',ch,'ALL, current segment ≥10-01'); gate(unit=='F',ch,'ALL, F stations'); gate(unit=='C',ch,'ALL, C stations')
print("\n== δ per stage only (the lane's current granularity), for comparison ==")
ch2=walk([stage==s for s in ('P0','P0-M','P1')])
for s in ('P0','P0-M','P1'): gate(stage==s,ch2,f'{s} (stage-level δ)')
gate(np.ones(N,bool),ch2,'ALL rows (stage-level δ)')
print("\n== fixed δ = −0.5°C on P0-M h0_12 only, and −0.1 on P0-M h12_24 (no fitting; the battery's point estimates) ==")
chf=np.full(N,-2); j5=DGRID.index(-0.5); j1=DGRID.index(-0.1)
chf[(stage=='P0-M')&(band=='h0_12')]=j5; chf[(stage=='P0-M')&(band=='h12_24')]=j1
gate((stage=='P0-M')&(band=='h0_12'),chf,'P0-M h0_12 δ=−0.5'); gate((stage=='P0-M')&(band=='h12_24'),chf,'P0-M h12_24 δ=−0.1')
for lo,hi,lab in (('2026-09-05','2026-09-22','first half'),('2026-09-22','2026-10-09','second half')): gate((stage=='P0-M')&(band=='h0_12')&(ed>=lo)&(ed<hi),chf,f'P0-M h0_12 δ=−0.5 {lab}')
