"""What if the calibration objective were binary LogLoss?  Same served vectors, same transform
families (temperature, peak ceiling, tail mix); the parameter is chosen walk-forward by four
objectives -- multiclass LogScore, one-vs-rest sum of binary LogLoss, binary LogLoss on the top
bucket, binary LogLoss on mode±1 -- and every choice is judged under both rulers and the 240 gate."""
import sys, json, gzip, math, collections, time
import numpy as np
sys.path.insert(0, '/Users/xiong/weather')
from bayes_basket_target.center_width_calibration import apply_peak_ceiling, apply_tail_mix, served_bucket_axis, TAIL_MIX_SCALE, TAIL_MIX_WEIGHT_GRID
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
from bayes_basket_target.materialization import floor_probabilities
from datetime import date, timedelta
t0=time.time(); F=1e-3
rows=[json.loads(l) for l in gzip.open('rows.jsonl.gz','rt')]
for r in rows:
    r['prob']={k:v for k,v in zip(r['keys'],r['p'])}; r['centers'],r['edges']=served_bucket_axis(r['intervals'],r['keys'])
    r['excl']=tuple(k for k,v in r['prob'].items() if v==0.0); r['top_key']=max(r['keys'],key=lambda k:r['prob'][k]); r['mi']=r['keys'].index(r['top_key'])
    r['far']=r['hours']>36
N=len(rows)
def bll(p,h): p=min(1-F,max(F,p)); return -(h*math.log(p)+(1-h)*math.log(1-p))
def allscores(r, vec):
    ls,brier,crps=_served_scores(vec,ordered_keys=r['keys'],settled_bucket=r['truth'],excluded_buckets=r['excl'],key_centers_c=r['centers'])
    f,_=floor_probabilities({k:vec[k] for k in r['keys']}, r['excl']); s=sum(f.values()); f={k:v/s for k,v in f.items()}
    y=r['truth']; ovr=sum(bll(f[k],1.0 if k==y else 0.0) for k in r['keys'])
    top=bll(f[r['top_key']],1.0 if r['top_key']==y else 0.0)
    pm1=sum(bll(f[k],1.0 if k==y else 0.0) for k in r['keys'] if abs(r['keys'].index(k)-r['mi'])<=1)
    return (ls,brier,crps,ovr,top,pm1)
def temp(r,tau):
    if tau==1.0: return r['prob']
    v={k:(p**tau if p>0 else 0.0) for k,p in r['prob'].items()}; s=sum(v.values()); return {k:x/s for k,x in v.items()}
def ceil(r,c):
    if c is None or r['prob'][r['top_key']]<=c: return r['prob']
    v,_=apply_peak_ceiling(r['prob'],ceiling=c,ordered_keys=r['keys'],excluded_buckets=r['excl']); return v
def mix(r,e):
    if e==0.0: return r['prob']
    v,_=apply_tail_mix(r['prob'],weight=e,scale=TAIL_MIX_SCALE,ordered_keys=r['keys'],key_centers_c=r['centers'],excluded_buckets=r['excl'],open_edge_bounds_c=r['edges']); return v
FAMILIES={'temperature τ':([0.6,0.7,0.8,0.9,1.0,1.1,1.2,1.3,1.4],temp,1.0),
          'peak ceiling c':([None,0.40,0.45,0.50,0.55,0.60,0.65,0.70],ceil,None),
          'tail mix ε':(list(TAIL_MIX_WEIGHT_GRID),mix,0.0)}
base=np.array([allscores(r,r['prob']) for r in rows])
ed=np.array([r['event_date'] for r in rows]); ka=np.array([r['known_at'] for r in rows]); stage=np.array([r['stage'] for r in rows]); far=np.array([r['far'] for r in rows]); unit=np.array([r['unit'] for r in rows])
days=sorted(set(ed)); groups=[(stage==s)&m for s in ('P0','P0-M','P1') for m in (far,~far)]
OBJ={'A multiclass LogScore':0,'B Σ_k binary LL':3,'C binary LL on top bucket':4,'D binary LL on mode±1':5}
def run_family(name):
    grid,fn,ident=FAMILIES[name]
    S=np.zeros((N,len(grid),6))
    for i,r in enumerate(rows):
        for j,g in enumerate(grid): S[i,j]=base[i] if g==ident else allscores(r,fn(r,g))
    print(f"\n== family: {name} (grid {grid}) ==   [{time.time()-t0:.0f}s]")
    for oname,oc in OBJ.items():
        chosen=np.full(N,-2)
        for gmask in groups:
            for d in days:
                dm=(date.fromisoformat(d)-timedelta(days=2)).isoformat(); pm=gmask&(ed<=dm)&(ka<=d+'T00:00:00Z'); idx=np.where(pm)[0]
                if len({ed[i] for i in idx})<8: continue
                sel=np.where(gmask&(ed==d))[0]
                if not len(sel): continue
                means=S[idx][:,:,oc].mean(axis=0); j=int(np.argmin(means)); chosen[sel]=j
        sel=np.where(chosen>=0)[0]
        cand=[tuple(S[i,chosen[i],:3]) for i in sel]; bas=[tuple(base[i,:3]) for i in sel]; cl=[(rows[i]['station'],rows[i]['event_date']) for i in sel]
        g=_proper_score_gate(bas,cand,cl)
        # the two rulers and the other binaries
        dl=np.array([S[i,chosen[i],0]-base[i,0] for i in sel]); dovr=np.array([S[i,chosen[i],3]-base[i,3] for i in sel]); dtop=np.array([S[i,chosen[i],4]-base[i,4] for i in sel]); dpm=np.array([S[i,chosen[i],5]-base[i,5] for i in sel])
        picks=collections.Counter(grid[chosen[i]] for i in sel)
        f=lambda v,ci: f"{v:+.4f} [{ci[0]:+.4f},{ci[1]:+.4f}]{'*' if ci and (ci[0]>0 or ci[1]<0) else ' '}" if ci else f"{v:+.4f}"
        print(f"  objective {oname:28s} | chosen: {dict(sorted(picks.items(), key=lambda kv: (kv[0] is None, kv[0] if kv[0] is not None else 0)))}")
        print(f"      240 gate vs served: ΔLogScore {f(g['d_logscore'],g['ci_logscore'])} ΔCRPS {f(g['d_crps'],g['ci_crps'])} ΔBrier {f(g['d_brier'],g['ci_brier'])} -> {'PASS' if g['lower'] else 'no'} | ΔΣLL {dovr.mean():+.4f} | Δtop LL {dtop.mean():+.4f} | Δmode±1 LL {dpm.mean():+.4f} | n={len(sel)}")
for fam in FAMILIES: run_family(fam)
# shape readout: what does each objective do to the peak and the tails on P0 rows (temperature family)
print("\n== temperature chosen by each objective, by group (P0 far / P0 near / P0-M far / P0-M near / P1) -- the direction each ruler pulls ==")
grid,fn,ident=FAMILIES['temperature τ']
S=np.zeros((N,len(grid),6))
for i,r in enumerate(rows):
    for j,g in enumerate(grid): S[i,j]=base[i] if g==ident else allscores(r,fn(r,g))
for oname,oc in OBJ.items():
    line=f"  {oname:28s}:"
    for gname,gmask in (('P0 >36h',(stage=='P0')&far),('P0 ≤36h',(stage=='P0')&~far),('P0-M >36h',(stage=='P0-M')&far),('P0-M ≤36h',(stage=='P0-M')&~far),('P1',stage=='P1')):
        idx=np.where(gmask)[0]; means=S[idx][:,:,oc].mean(axis=0); line+=f" {gname} τ*={grid[int(np.argmin(means))]}"
    print(line)
print('done', round(time.time()-t0,1),'s')
