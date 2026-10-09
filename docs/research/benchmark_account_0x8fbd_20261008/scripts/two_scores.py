"""Multiclass LogScore (ours, charter 211/240) vs binary LogLoss on one bucket (the benchmark
research's measure): decomposition on the same served decisions, and how the two react to the
same transform (peak ceiling, tail mix)."""
import sys, json, gzip, math, collections
import numpy as np
sys.path.insert(0, '/Users/xiong/weather')
from bayes_basket_target.center_width_calibration import apply_peak_ceiling, apply_tail_mix, served_bucket_axis, TAIL_MIX_SCALE
from bayes_basket_target.materialization import floor_probabilities
rows=[json.loads(l) for l in gzip.open('rows.jsonl.gz','rt')]
F=1e-3
def floored(r, vec):
    f,_=floor_probabilities({k:vec[k] for k in r['keys']}, tuple(k for k in r['keys'] if r['prob'][k]==0.0)); s=sum(f.values()); return {k:v/s for k,v in f.items()}
def bll(p,h): p=min(1-F,max(F,p)); return -(h*math.log(p)+(1-h)*math.log(1-p))
for r in rows:
    r['prob']={k:v for k,v in zip(r['keys'],r['p'])}; r['centers'],r['edges']=served_bucket_axis(r['intervals'],r['keys'])
    r['top_key']=max(r['keys'],key=lambda k:r['prob'][k]); r['mi']=r['keys'].index(r['top_key']); r['ti']=r['keys'].index(r['truth'])
def scores(r, vec):
    f=floored(r, vec); y=r['truth']
    ls=-math.log(max(f[y],1e-12)); ovr=sum(bll(f[k], 1.0 if k==y else 0.0) for k in r['keys']); spread=ovr-ls
    ll_top=bll(f[r['top_key']], 1.0 if r['top_key']==y else 0.0)
    far=[k for k in r['keys'] if abs(r['keys'].index(k)-r['mi'])>=2]
    ll_far=sum(bll(f[k],1.0 if k==y else 0.0) for k in far)
    nb=[k for k in r['keys'] if abs(r['keys'].index(k)-r['mi'])==1]
    ll_nb=sum(bll(f[k],1.0 if k==y else 0.0) for k in nb)
    return dict(ls=ls, ovr=ovr, spread=spread, ll_top=ll_top, ll_far=ll_far, ll_nb=ll_nb, p_y=f[y], p_top=f[r['top_key']])
base=[scores(r, r['prob']) for r in rows]
cl=np.array([hash((r['station'],r['event_date'])) for r in rows])
def boot(d):
    d=np.asarray(d); ids={c:i for i,c in enumerate(set(cl))}; idx=np.array([ids[c] for c in cl]); n=len(ids)
    sums=np.bincount(idx,weights=d,minlength=n); cnt=np.bincount(idx,minlength=n).astype(float)
    rng=np.random.default_rng(20261009); draws=rng.integers(0,n,size=(2000,n))
    m=sums[draws].sum(axis=1)/cnt[draws].sum(axis=1); return float(d.mean()), float(np.percentile(m,2.5)), float(np.percentile(m,97.5))
def mean(key, sel=None):
    v=[b[key] for i,b in enumerate(base) if sel is None or sel[i]]; return sum(v)/len(v)
print("== 1. Decomposition on all served decisions (n=%d): one-vs-rest sum = LogScore + spread penalty ==" % len(rows))
stage=np.array([r['stage'] for r in rows])
for tag,sel in (('ALL',np.ones(len(rows),bool)),('P0',stage=='P0'),('P0-M',stage=='P0-M'),('P1',stage=='P1')):
    print(f"  {tag:5s} LogScore {mean('ls',sel):.4f} | one-vs-rest Σ_k LL_k {mean('ovr',sel):.4f} = LogScore + spread {mean('spread',sel):.4f} | LL on top bucket {mean('ll_top',sel):.4f} | LL on ±1 (sum) {mean('ll_nb',sel):.4f} | LL on |d|≥2 (sum) {mean('ll_far',sel):.4f} | mean p_truth {mean('p_y',sel):.3f}, p_top {mean('p_top',sel):.3f}")
print("\n== 2. Same transform, two rulers (Δ = after − before; station-day bootstrap 95%) ==")
far=np.array([r['hours']>36 for r in rows]); unit=np.array([r['unit'] for r in rows])
def transform_report(name, sel, fn):
    idx=np.where(sel)[0]; d={k:[] for k in ('ls','ovr','ll_top','ll_far','ll_nb')}; keep=[]
    for i in idx:
        v=fn(rows[i])
        if v is None: continue
        a=scores(rows[i],v); b=base[i]; keep.append(i)
        for k in d: d[k].append(a[k]-b[k])
    global cl; cl_all=cl; cl=cl_all[keep]
    out=f"  {name:46s} n={len(keep):5d} |"
    for k,lab in (('ls','ΔLogScore'),('ll_top','ΔLL top'),('ll_nb','ΔLL ±1'),('ll_far','ΔLL far'),('ovr','ΔΣLL')):
        m,lo,hi=boot(d[k]); out+=f" {lab} {m:+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} |"
    cl=cl_all; print(out)
def ceil_fn(c):
    def f(r):
        if r['prob'][r['top_key']]<=c: return None
        v,_=apply_peak_ceiling(r['prob'],ceiling=c,ordered_keys=r['keys'],excluded_buckets=tuple(k for k in r['keys'] if r['prob'][k]==0.0)); return v
    return f
def mix_fn(eps):
    def f(r):
        v,_=apply_tail_mix(r['prob'],weight=eps,scale=TAIL_MIX_SCALE,ordered_keys=r['keys'],key_centers_c=r['centers'],excluded_buckets=tuple(k for k in r['keys'] if r['prob'][k]==0.0),open_edge_bounds_c=r['edges']); return v
    return f
transform_report('ceiling 0.45 on P0 rows with p_top>0.45', stage=='P0', ceil_fn(0.45))
transform_report('ceiling 0.45 on P0 ≤36h rows', (stage=='P0')&~far, ceil_fn(0.45))
transform_report('ceiling 0.53 on P0-M rows with p_top>0.53', stage=='P0-M', ceil_fn(0.53))
transform_report('tail mix ε=0.15 on F stations >36h', (unit=='F')&far, mix_fn(0.15))
transform_report('tail mix ε=0.15 on P0 ≤36h C stations', (stage=='P0')&~far&(unit=='C'), mix_fn(0.15))
print("\n== 3. Two vectors, same LogScore, different binary losses (worked example from the data) ==")
ex=[(i,b) for i,b in enumerate(base) if 0.38<b['p_y']<0.42 and rows[i]['ti']==rows[i]['mi']]
ex.sort(key=lambda t: t[1]['p_top']); lo_=ex[0]; hi_=ex[-1]
for i,b in (lo_,hi_):
    r=rows[i]; f=floored(r,r['prob']); print(f"  {r['station']} {r['event_date']} {r['stage']} {r['band']}: truth={r['truth']} p_truth={b['p_y']:.3f} LogScore={b['ls']:.3f} | top={r['top_key']} p_top={b['p_top']:.3f} | ±1 mass {sum(f[k] for k in r['keys'] if abs(r['keys'].index(k)-r['mi'])==1):.3f} far mass {sum(f[k] for k in r['keys'] if abs(r['keys'].index(k)-r['mi'])>=2):.3f} | ΣLL_k={b['ovr']:.3f} (spread {b['spread']:.3f})")
