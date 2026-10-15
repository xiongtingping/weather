"""Side of the market mode: who is right, P1 / its root P0-M / the P0 under it / the market, by local hour."""
import gzip, json, sys, numpy as np, collections
from datetime import datetime, timezone
sys.path.insert(0, '../obs_minus_forecast_station_direction_20261015'); import analyze as A
rows = [json.loads(l) for l in gzip.open('joined.jsonl.gz','rt')]
def utc(s): return datetime.fromisoformat(s.replace('Z','+00:00'))
R = []
for r in rows:
    order, c, w = A.centres(r['intervals'])
    def vec(p):
        if not p or set(p) != set(order): return None
        v = np.array([float(p[b]) for b in order]); return v/v.sum()
    m = r['market']['0']; q = vec(m['q'])
    if q is None: continue
    mi = order.index(m['mode']) if m['mode'] in order else int(np.argmax(q))
    T = r['truth']; ti = next(i for i,b in enumerate(order) if (r['intervals'][b][0] is None or r['intervals'][b][0] <= T) and (r['intervals'][b][1] is None or T < r['intervals'][b][1]))
    def side(v): return None if v is None else float(np.sign(v[mi+1:].sum() - v[:mi].sum()))
    def lean(v): return None if v is None else float(v[mi+1:].sum() - v[:mi].sum())
    def mean(v): return None if v is None else float((v*c).sum())
    p1 = vec(r['p1']); root = vec(r['root']['p']) if r['root'] else None; p0 = vec(r['p0']['p']) if r['p0'] else None
    dec = utc(r['decided_at'])
    R.append(dict(st=r['station'], day=r['day'], lh=r['local_hour'], unit=r['unit'], truth_side=np.sign(ti-mi), w=w, mode_c=c[mi], T=T,
        s_p1=side(p1), s_root=side(root), s_p0=side(p0), s_mk=side(q), l_p1=lean(p1), l_root=lean(root), l_p0=lean(p0), l_mk=lean(q),
        mu_p1=mean(p1), mu_root=mean(root), mu_p0=mean(p0), mu_mk=mean(q), mode_p_p1=p1[mi], mode_p_mk=q[mi],
        root_stage=r['root']['stage'] if r['root'] else None, root_age_h=(dec-utc(r['root']['decided_at'])).total_seconds()/3600 if r['root'] else None,
        p0_age_h=(dec-utc(r['p0']['decided_at'])).total_seconds()/3600 if r['p0'] else None,
        p0_runs=r['p0']['run_init_by_system'] if r['p0'] else None, taf=r['root']['taf'] if r['root'] else None))
json.dump(R, gzip.open('decomposed.json.gz','wt'), default=float)
def hit(key, sel):
    s = [x for x in sel if x[key] is not None and x[key] != 0 and x['truth_side'] != 0]
    return (np.mean([x[key]==x['truth_side'] for x in s]) if s else float('nan')), len(s)
print('rows', len(R), 'root stages', collections.Counter(x['root_stage'] for x in R))
for lo,hi in ((7,10),(10,13),(13,16),(16,20)):
    sel=[x for x in R if lo<=x['lh']<hi]
    print(f"{lo:2d}-{hi:2d}h", ' '.join(f"{k}={hit(k,sel)[0]:.3f}(n{hit(k,sel)[1]})" for k in ('s_p0','s_root','s_p1','s_mk')),
          f"root_age_med {np.median([x['root_age_h'] for x in sel if x['root_age_h'] is not None]):.1f}h p0_age_med {np.median([x['p0_age_h'] for x in sel if x['p0_age_h'] is not None]):.1f}h")
# disagreement rows: P1 opposite market, truth off mode
dis=[x for x in R if x['s_p1'] and x['s_mk'] and x['s_p1']!=x['s_mk'] and x['truth_side']!=0]
print('P1 vs market opposite (truth off mode):', len(dis), 'market right', np.mean([x['s_mk']==x['truth_side'] for x in dis]).round(3))
print('  of these, root same side as P1:', np.mean([x['s_root']==x['s_p1'] for x in dis if x['s_root'] is not None]).round(3),
      ' P0 same side as P1:', np.mean([x['s_p0']==x['s_p1'] for x in dis if x['s_p0'] is not None]).round(3))
print('  P1 lean warm share:', np.mean([x['s_p1']>0 for x in dis]).round(3))
mor=[x for x in R if 7<=x['lh']<13 and x['mu_p0'] is not None]
for k in ('mu_p0','mu_root','mu_p1','mu_mk'):
    print(f'  morning mean error {k}: bias {np.mean([x[k]-x["T"] for x in mor]):+.3f}  MAE {np.mean([abs(x[k]-x["T"]) for x in mor]):.3f}')
print('  morning mean P1-market', np.mean([x['mu_p1']-x['mu_mk'] for x in mor]).round(3), 'P0-market', np.mean([x['mu_p0']-x['mu_mk'] for x in mor]).round(3), 'root-market', np.mean([x['mu_root']-x['mu_mk'] for x in mor if x['mu_root'] is not None]).round(3))
