"""Downstream: what censoring at P0-M does to P1, which roots on the P0-M envelope.

For each closed-day P1 cell rooted on a P0-M (one per station-day-3h block):
  root_A = the served P0-M vector (proportional mask)
  root_B = the same P0-M recomputed with censoring (P0 x L_TAF, ruled-out mass onto the running-high bucket)
Two views of P1:
  fixed-f : the stored exp f (log update as served) on each root -- isolates the root.
  refit-f : production compile_direct_update with today's artifact on each root -- f reacts to the
            root features (root_rise_mass, root_mean_minus_high_c) as serving would.
Scores via production _served_scores; station-day bootstrap via production _proper_score_gate.
"""
import json, math, pathlib, sys
import numpy as np
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import floor_probabilities
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
from bayes_basket_target.center_width_calibration import served_bucket_axis
from bayes_basket_target.p1_direct_update import compile_direct_update
from bayes_basket_target.p1_observation_likelihood import load_artifact
ART = load_artifact(); print('artifact', ART.get('content_sha256'), 'trained_through', (ART.get('direct_update') or {}).get('trained_through'))
D = json.load(open(HERE / 'capture_p1.json')); rows = D['rows']
def geometry(iv):
    widths = [b[1] - b[0] for b in iv.values() if b[0] is not None and b[1] is not None]; step = min(widths)
    cen = {}; up = {}
    for k, (lo, hi) in iv.items():
        cen[k] = (lo + hi) / 2 if lo is not None and hi is not None else (hi - step / 2 if hi is not None else lo + step / 2); up[k] = hi
    return cen, up
def p0m_vector(r, w):
    L = r['taf_L'] or {}; pre = {k: float(r['p0'][k]) * float(L.get(k, 1.0)) for k in r['keys']}
    ex = set(r['p0m_excluded']); t = sum(pre.values()); p = {k: v / t for k, v in pre.items()}
    out = sum(p[k] for k in ex); surv = [k for k in r['keys'] if k not in ex]
    q = {k: (0.0 if k in ex else p[k]) for k in r['keys']}
    if surv: q[surv[0]] += w * out
    t = sum(q.values()); q = {k: v / t for k, v in q.items()}
    return floor_probabilities(q, tuple(ex))[0]
def p1_from(root, lik, ex):
    q = {k: (0.0 if k in ex else float(root[k]) * float(lik.get(k, 0.0))) for k in root}
    t = sum(q.values()); q = {k: v / t for k, v in q.items()}
    return floor_probabilities(q, tuple(ex))[0]
def landed(r):
    for k in r['keys']:
        lo, hi = r['intervals'][k]
        if (lo is None or r['truth'] >= lo) and (hi is None or r['truth'] < hi): return k
out = []; skipped = {'root_mismatch': 0, 'p1_mismatch': 0, 'refit_fail': 0, 'no_land': 0}
for r in rows:
    k = landed(r)
    if k is None: skipped['no_land'] += 1; continue
    rootA = p0m_vector(r, 0.0)
    if max(abs(rootA[x] - r['p0m'][x]) for x in r['keys']) > 1e-5: skipped['root_mismatch'] += 1; continue
    rootB = p0m_vector(r, 1.0); ex = set(r['p1_excluded']); rec = r['p1_receipt']
    lik = rec['likelihood_by_bucket']
    a0 = p1_from(r['p0m'], lik, ex)
    if max(abs(a0[x] - r['p1'][x]) for x in r['keys']) > 1e-4: skipped['p1_mismatch'] += 1; continue
    bfix = p1_from(rootB, lik, ex)
    cen, up = geometry(r['intervals'])
    feats = dict(rec['features'])
    try:
        kw = dict(bucket_ordinals=r['ords'], bucket_centers_c=cen, bucket_upper_bounds_c=up, running_high_c=float(rec['running_high_c']),
                  running_high_bucket=rec['running_high_bucket'], hard_excluded_buckets=tuple(ex), station_id=r['station'])
        la, _ = compile_direct_update(feats, ART, root_probabilities=r['p0m'], **kw)
        lb, _ = compile_direct_update(feats, ART, root_probabilities=rootB, **kw)
    except Exception as e:
        skipped['refit_fail'] += 1; continue
    arf = p1_from(r['p0m'], la, ex); brf = p1_from(rootB, lb, ex)
    centers, _ = served_bucket_axis(r['intervals'], r['keys'])
    sc = lambda q: _served_scores(q, ordered_keys=r['keys'], settled_bucket=k, excluded_buckets=tuple(ex), key_centers_c=centers)
    mean = lambda q: sum(q[x] * i for i, x in enumerate(r['keys']))
    ix = r['keys'].index(k)
    same_bucket = bool(r['p0m_excluded']) and [x for x in r['keys'] if x not in set(r['p0m_excluded'])][0] == rec['running_high_bucket']
    out.append(dict(cluster=(r['station'], r['date']), hour=r['hour'], affected=same_bucket, tv=0.5 * sum(abs(rootA[x] - rootB[x]) for x in r['keys']),
                    s=dict(a0=sc(a0), bfix=sc(bfix), arf=sc(arf), brf=sc(brf)), bias=dict(a0=ix - mean(a0), bfix=ix - mean(bfix), arf=ix - mean(arf), brf=ix - mean(brf))))
print('cells', len(out), 'skipped', skipped, 'affected (P0-M floor bucket == P1 running-high bucket)', sum(o['affected'] for o in out))
def rep(sel, a, b, label):
    g = _proper_score_gate([o['s'][a] for o in sel], [o['s'][b] for o in sel], [o['cluster'] for o in sel])
    print('%-46s cells %4d st-days %3d | dLog %+.4f [%+.4f,%+.4f] dBrier %+.4f [%+.4f,%+.4f] dCRPS %+.4f [%+.4f,%+.4f] | bias %+.3f -> %+.3f | lower=%s' % (
        label, g['cells'], g['station_days'], g['d_logscore'], *g['ci_logscore'], g['d_brier'], *g['ci_brier'], g['d_crps'], *g['ci_crps'],
        np.mean([o['bias'][a] for o in sel]), np.mean([o['bias'][b] for o in sel]), g['lower']))
aff = [o for o in out if o['affected']]
print('\n== fixed f (served exp f on each root)')
rep(out, 'a0', 'bfix', 'all P1 cells rooted on P0-M')
rep(aff, 'a0', 'bfix', 'affected cells')
print('\n== refit f (today\'s artifact, f reacts to the root)')
rep(out, 'arf', 'brf', 'all P1 cells rooted on P0-M')
rep(aff, 'arf', 'brf', 'affected cells')
for lo, hi in ((0, 12), (12, 15), (15, 18), (18, 25)):
    rep([o for o in aff if lo <= o['hour'] < hi], 'arf', 'brf', f'  affected, local hour {lo:02d}-{hi:02d}')
print('\nsanity: refit-f current root vs served P1 (artifact drift only)')
rep(out, 'a0', 'arf', 'served -> today-artifact recompute')
print('root TV distance on affected cells: median %.3f, q90 %.3f' % (np.median([o['tv'] for o in aff]), np.quantile([o['tv'] for o in aff], .9)))
