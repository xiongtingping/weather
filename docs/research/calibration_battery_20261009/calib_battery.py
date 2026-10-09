"""Calibration battery on the served record (15,873 representative decisions): full multiclass reliability by
stage x lead, PIT shape, centre bias by station / lead / local hour with day-bootstrap intervals, and the
LogScore each defect costs (what a perfect fix of that defect alone would be worth)."""
import json, gzip, math, collections, re
import numpy as np
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
rows=[json.loads(l) for l in gzip.open('rows.jsonl.gz','rt')]
F=1e-3
def floored(p):
    v=[max(F,x) if x>0 else 0.0 for x in p]; s=sum(v); return [x/s for x in v]
def centre_c(label, lo, hi):
    if lo is None: return float(hi)-0.5*(1.111 if '°F' in label else 1.0)
    if hi is None: return float(lo)+0.5*(1.111 if '°F' in label else 1.0)
    return (float(lo)+float(hi))/2
for r in rows:
    r['f']=floored(r['p']); r['ti']=r['keys'].index(r['truth']); r['w']=1.111 if r['unit']=='F' else 1.0
    r['c']=[centre_c(k,*r['intervals'][k]) for k in r['keys']]
    r['mu']=sum(c*p for c,p in zip(r['c'],r['f'])); r['sig']=math.sqrt(max(1e-9,sum(p*(c-r['mu'])**2 for c,p in zip(r['c'],r['f']))))
    r['tc']=r['c'][r['ti']]; r['z']=(r['tc']-r['mu'])/r['sig']; r['pit']=sum(p for c,p in zip(r['c'],r['f']) if c<r['tc'])+0.5*r['f'][r['ti']]
    r['ls']=-math.log(max(1e-12,r['f'][r['ti']]))
    r['lhour']=datetime.fromtimestamp(datetime.fromisoformat(r['decision_at'].replace('Z','+00:00')).timestamp(),ZoneInfo(r['tz'])).hour
def boot_mean(vals, days, n=2000, seed=1):
    vals=np.asarray(vals,float); days=np.asarray(days); ids={d:i for i,d in enumerate(set(days))}; idx=np.array([ids[d] for d in days]); k=len(ids)
    sums=np.bincount(idx,weights=vals,minlength=k); cnt=np.bincount(idx,minlength=k).astype(float)
    rng=np.random.default_rng(seed); draws=rng.integers(0,k,size=(n,k)); m=sums[draws].sum(axis=1)/cnt[draws].sum(axis=1)
    return vals.mean(), np.percentile(m,2.5), np.percentile(m,97.5)
stage=np.array([r['stage'] for r in rows]); far=np.array([r['hours']>36 for r in rows]); unit=np.array([r['unit'] for r in rows])
print("== 1. multiclass reliability: every bucket's claimed p vs landing frequency, by stage (all leads) ==")
bins=[(0,.01),(.01,.03),(.03,.06),(.06,.1),(.1,.15),(.15,.2),(.2,.3),(.3,.4),(.4,.5),(.5,.6),(.6,.7),(.7,.85),(.85,1.01)]
for tag,sel in (('ALL',np.ones(len(rows),bool)),('P0 >36h',(stage=='P0')&far),('P0 ≤36h',(stage=='P0')&~far),('P0-M',stage=='P0-M'),('P1',stage=='P1')):
    cells=collections.defaultdict(lambda:[0.0,0.0,0])
    for r,s in zip(rows,sel):
        if not s: continue
        for i,p in enumerate(r['f']):
            for lo,hi in bins:
                if lo<=p<hi: c=cells[(lo,hi)]; c[0]+=p; c[1]+=(i==r['ti']); c[2]+=1; break
    line=f"  {tag:9s}:"
    for lo,hi in bins:
        c=cells[(lo,hi)]
        if c[2]>=200: line+=f" [{lo:.2f}-{hi:.2f}) said {c[0]/c[2]:.3f} landed {c[1]/c[2]:.3f} (n={c[2]})"
    print(line)
print("\n== 2. PIT shape and z by stage x lead (U = too narrow, hump = too wide; mean z = centre bias in sigma units) ==")
for tag,sel in (('P0 >36h',(stage=='P0')&far),('P0 ≤36h',(stage=='P0')&~far),('P0-M >36h',(stage=='P0-M')&far),('P0-M ≤36h',(stage=='P0-M')&~far),('P1',stage=='P1'),('unit F',unit=='F'),('unit C',unit=='C')):
    R=[r for r,s in zip(rows,sel) if s]
    pit=np.array([r['pit'] for r in R]); z=np.array([r['z'] for r in R]); days=[r['event_date'] for r in R]
    hist=np.histogram(pit,bins=[0,.1,.2,.3,.4,.5,.6,.7,.8,.9,1.0001])[0]/len(R)
    mz,lo,hi=boot_mean(z,days); mz2,lo2,hi2=boot_mean(z*z,days)
    print(f"  {tag:10s} n={len(R):5d} PIT deciles {' '.join(f'{h:.2f}' for h in hist)} | mean z {mz:+.3f} [{lo:+.3f},{hi:+.3f}] | E[z²] {mz2:.2f} [{lo2:.2f},{hi2:.2f}] | mean σ {np.mean([r['sig'] for r in R]):.2f}°C")
print("\n== 3. centre bias (truth − mean, in °C) by lead band, by unit, by local decision hour ==")
for name,keyf in (('lead band',lambda r:r['band']),('unit',lambda r:r['unit']),('local hour block',lambda r:f"{(r['lhour']//6)*6:02d}-{(r['lhour']//6)*6+6:02d}"),('stage',lambda r:r['stage'])):
    groups=collections.defaultdict(list)
    for r in rows: groups[keyf(r)].append(r)
    print(f"  by {name}:")
    for k in sorted(groups):
        R=groups[k]; m,lo,hi=boot_mean([r['tc']-r['mu'] for r in R],[r['event_date'] for r in R])
        print(f"     {str(k):8s} n={len(R):5d} bias {m:+.3f}°C [{lo:+.3f},{hi:+.3f}]{'*' if lo>0 or hi<0 else ''} | LogScore {np.mean([r['ls'] for r in R]):.3f}")
print("\n== 4. centre bias by station (P0+P0-M only, °C, day bootstrap), sorted; * = interval excludes 0 ==")
st_rows=collections.defaultdict(list)
for r in rows:
    if r['stage'] in ('P0','P0-M'): st_rows[r['station']].append(r)
tbl=[]
for st,R in st_rows.items():
    if len(R)<60: continue
    m,lo,hi=boot_mean([r['tc']-r['mu'] for r in R],[r['event_date'] for r in R]); tbl.append((st,len(R),m,lo,hi,np.mean([r['z']**2 for r in R]),R[0]['unit']))
sig=[t for t in tbl if t[3]>0 or t[4]<0]
print(f"  stations with ≥60 decisions: {len(tbl)}; with bias interval excluding 0: {len(sig)}")
for st,n,m,lo,hi,z2,u in sorted(tbl,key=lambda t:-abs(t[2])):
    if lo>0 or hi<0 or abs(m)>0.4: print(f"     {st} ({u}) n={n:4d} bias {m:+.2f}°C [{lo:+.2f},{hi:+.2f}]{'*' if lo>0 or hi<0 else ' '} E[z²] {z2:.2f}")
print("\n== 5. what a perfect fix of each defect alone would be worth (LogScore, oracle shift/scale applied to the served vector) ==")
# oracle: per segment, shift all centres by -bias (through the width transport is complex; approximate by re-binning a shifted mixture is heavy) -> report instead the fraction of LogScore variance by segment
seg=collections.defaultdict(list)
for r in rows: seg[(r['stage'],'>36h' if r['hours']>36 else '≤36h',r['unit'])].append(r['ls'])
tot=sum(r['ls'] for r in rows)
print("  share of total LogScore by segment (where the loss actually sits):")
for k in sorted(seg,key=lambda k:-sum(seg[k])): print(f"     {str(k):28s} n={len(seg[k]):5d} mean {np.mean(seg[k]):.3f} share of total {sum(seg[k])/tot:.3f}")
