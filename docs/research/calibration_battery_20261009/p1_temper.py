"""Arm for the P1 late-morning over-claim: temper P1's update toward its root (geometric blend
p' ∝ root^(1-w) * p1^w) on h12_24 rows, w by prefix LogScore (walk-forward), judged by the 240 gate;
also fixed w; also only where P1 raised a bucket by >=0.15 (the over-shooting cases)."""
import json, gzip, math, collections, sys
import numpy as np
exec(open('calib_battery.py').read().split('stage=np.array')[0])
sys.path.insert(0,'/Users/xiong/weather')
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
from datetime import date, timedelta
by=collections.defaultdict(dict)
for r in rows: by[(r['station'],r['event_date'],r['band'])][r['stage']]=r
R=[]
for r in rows:
    if r['stage']!='P1' or r['band']!='h12_24': continue
    root=by[(r['station'],r['event_date'],r['band'])].get('P0-M') or by[(r['station'],r['event_date'],r['band'])].get('P0')
    if not root or root['keys']!=r['keys']: continue
    r['root']=root['f']; R.append(r)
print('P1 h12_24 rows with a root', len(R))
def score(r, vec):
    prob={k:v for k,v in zip(r['keys'],vec)}; excl=tuple(k for k,v in prob.items() if v==0.0)
    return _served_scores(prob,ordered_keys=r['keys'],settled_bucket=r['truth'],excluded_buckets=excl,key_centers_c=[ (lambda lo,hi: (float(hi)-0.5*(1.111 if '°F' in k else 1.0)) if lo is None else (float(lo)+0.5*(1.111 if '°F' in k else 1.0)) if hi is None else (float(lo)+float(hi))/2)(*r['intervals'][k]) for k in r['keys']])
def temper(r,w):
    p=np.array(r['f']); q=np.array(r['root']); v=np.where((p>0)&(q>0), np.exp(w*np.log(np.maximum(p,1e-12))+(1-w)*np.log(np.maximum(q,1e-12))), 0.0)
    s=v.sum(); return list(v/s) if s>0 else list(p)
WG=[1.0,0.9,0.8,0.7,0.6,0.5]
S=np.zeros((len(R),len(WG),3))
for i,r in enumerate(R):
    r['base']=score(r,r['f'])
    for j,w in enumerate(WG): S[i,j]=r['base'] if w==1.0 else score(r,temper(r,w))
ed=np.array([r['event_date'] for r in R]); ka=np.array([r['known_at'] for r in R]); days=sorted(set(ed))
f=lambda v,ci: f"{v:+.4f} [{ci[0]:+.4f},{ci[1]:+.4f}]{'*' if ci and (ci[0]>0 or ci[1]<0) else ''}" if ci else '—'
def gate(sel, cand_idx, label):
    sel=np.asarray(sel)
    if len(sel)<30: return
    g=_proper_score_gate([tuple(R[i]['base']) for i in sel],[tuple(S[i,cand_idx[i]]) for i in sel],[(R[i]['station'],R[i]['event_date']) for i in sel])
    print(f"  {label:44s} n={len(sel):4d} ΔLog {f(g['d_logscore'],g['ci_logscore'])} ΔCRPS {f(g['d_crps'],g['ci_crps'])} ΔBrier {f(g['d_brier'],g['ci_brier'])} -> {'PASS' if g['lower'] else 'no'}")
chosen=np.full(len(R),-1)
for d in days:
    dm=(date.fromisoformat(d)-timedelta(days=2)).isoformat(); idx=np.where((ed<=dm)&(ka<=d+'T00:00:00Z'))[0]
    if len({ed[i] for i in idx})<8: continue
    sel=np.where(ed==d)[0]; chosen[sel]=int(np.argmin(S[idx][:,:,0].mean(axis=0)))
sel=np.where(chosen>=0)[0]; print("== temper P1 toward its root on h12_24 (all rows) =="); gate(sel,chosen,f"walk-forward w (chosen {dict(sorted(collections.Counter(WG[chosen[i]] for i in sel).items()))})")
for w in (0.9,0.8,0.7):
    j=WG.index(w); gate(np.arange(len(R)),np.full(len(R),j),f'fixed w={w}')
print("== only where P1's top bucket rose ≥0.15 over the root (the over-shooters) ==")
big=np.array([i for i,r in enumerate(R) if (max(r['f'])-r['root'][int(np.argmax(r['f']))])>=0.15])
for w in (0.8,0.7,0.6):
    j=WG.index(w); gate(big,np.full(len(R),j),f'fixed w={w} on rose≥0.15 (n={len(big)})')
print("== reliability of the top bucket after tempering w=0.7, h12_24 rows whose P1 top claims 0.5–0.75 ==")
for w in (1.0,0.8,0.7):
    said=[];landed=[]
    for r in R:
        v=r['f'] if w==1.0 else temper(r,w); i=int(np.argmax(r['f']))
        if 0.5<=r['f'][i]<0.75: said.append(v[i]); landed.append(i==r['ti'])
    print(f"  w={w}: n={len(said)} said {np.mean(said):.3f} landed {np.mean(landed):.3f}")
print("\n== segments for fixed w=0.8 and for the conditional rule (temper w=0.7 only where P1's top rose ≥0.15 over the root) ==")
j8=WG.index(0.8); j7=WG.index(0.7)
cond=np.array([j7 if (max(r['f'])-r['root'][int(np.argmax(r['f']))])>=0.15 else 0 for r in R])
for lab,m in (('all',np.ones(len(R),bool)),('09-05..09-21',ed<'2026-09-22'),('09-22..09-30',(ed>='2026-09-22')&(ed<'2026-10-01')),('≥10-01 (trajectory fix)',ed>='2026-10-01')):
    sel=np.where(m)[0]; gate(sel,np.full(len(R),j8),f'fixed w=0.8 {lab}'); gate(sel,cond,f'conditional w=0.7 if rose≥0.15 {lab}')
print("  share of rows where P1's top rose ≥0.15 over root:", round(float(np.mean(cond>0)),3))
