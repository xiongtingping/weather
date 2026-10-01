"""P0-M observed hard bound: proportional renormalisation (served) vs censoring onto the running-high bucket.

Arms on identical pre-mask vectors (root P0 x TAF likelihood):
  A  served: mask the ruled-out buckets, renormalise proportionally (charter 61/150 today).
  B  censor: the ruled-out mass lands on the lowest surviving bucket (the one holding the running high):
     Y = max(H, Z), Z ~ P0 x L_TAF.
  C  w-censor: a share w of the ruled-out mass is censored, 1-w spread proportionally (w walk-forward).
Scores: production _served_scores (LogScore, bucket Brier, ladder CRPS) after the served floor;
station-day bootstrap via production _proper_score_gate (charter 240 ranked verdict).
"""
import json, math, pathlib, sys, collections
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import floor_probabilities
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
from bayes_basket_target.center_width_calibration import served_bucket_axis
D = json.load(open(HERE / 'capture.json')); rows = D['rows']

def landed(r):
    for k in r['keys']:
        lo, hi = r['intervals'][k]
        if (lo is None or r['truth'] >= lo) and (hi is None or r['truth'] < hi): return k
def pre_mask(r):
    L = r['likelihood'] or {}
    return {k: float(r['root'][k]) * float(L.get(k, 1.0)) for k in r['keys']}
def masked(pre, excluded, w):
    """w=0 proportional (served), w=1 full censoring onto the lowest surviving bucket."""
    keys = list(pre); ex = set(excluded); tot = sum(pre.values()); p = {k: v / tot for k, v in pre.items()}
    out_mass = sum(p[k] for k in ex); surv = [k for k in keys if k not in ex]
    floor_key = surv[0]
    q = {k: (0.0 if k in ex else p[k]) for k in keys}
    q[floor_key] += w * out_mass
    t = sum(q.values()); q = {k: v / t for k, v in q.items()}
    return floor_probabilities(q, tuple(ex))[0]
def local_hour(r):
    z = ZoneInfo(r['tz']); d = datetime.fromisoformat(r['decision'].replace('Z', '+00:00')).astimezone(z)
    return (d - datetime.fromisoformat(r['date'] + 'T00:00:00').replace(tzinfo=z)).total_seconds() / 3600
cells = []; bad = 0
for r in rows:
    k = landed(r)
    if k is None: continue
    pre = pre_mask(r); ex = [x for x in r['excluded'] if x in pre]
    # identity check: reconstruct the served vector
    a = masked(pre, ex, 0.0)
    err = max(abs(a[x] - float(r['served'][x])) for x in r['keys'])
    # TAF receipts round the likelihood to 6 dp: APPLIED rows rebuild to ~1e-7.
    if err > 1e-5: bad += 1; continue
    centers, _ = served_bucket_axis(r['intervals'], r['keys'])
    tot = sum(pre.values()); out_mass = sum(pre[x] for x in ex) / tot
    cells.append(dict(r=r, k=k, ex=ex, pre=pre, centers=centers, out_mass=out_mass, hour=local_hour(r),
                      cluster=(r['station'], r['date']), ix=r['keys'].index(k)))
print('cells', len(cells), 'reconstruction mismatches dropped', bad)
def score(c, w):
    q = masked(c['pre'], c['ex'], w)
    ls, b, cr = _served_scores(q, ordered_keys=c['r']['keys'], settled_bucket=c['k'], excluded_buckets=tuple(c['ex']), key_centers_c=c['centers'])
    mean_ix = sum(q[x] * i for i, x in enumerate(c['r']['keys']))
    return (ls, b, cr), c['ix'] - mean_ix
def verdict(sel, w_b, w_a=0.0, label=''):
    base = []; cand = []; cl = []; bias_a = []; bias_b = []
    for c in sel:
        sa, ba = score(c, w_a); sb, bb = score(c, w_b); base.append(sa); cand.append(sb); cl.append(c['cluster']); bias_a.append(ba); bias_b.append(bb)
    g = _proper_score_gate(base, cand, cl)
    print('%-44s cells %5d st-days %4d | dLog %+.4f [%+.4f,%+.4f] dBrier %+.4f [%+.4f,%+.4f] dCRPS %+.4f [%+.4f,%+.4f] | bias(buckets, truth-mean) %+.3f -> %+.3f | lower=%s' % (
        label, g['cells'], g['station_days'], g['d_logscore'], *g['ci_logscore'], g['d_brier'], *g['ci_brier'], g['d_crps'], *g['ci_crps'], np.mean(bias_a), np.mean(bias_b), g['lower']))
    return g
# one cell per station-day-3h block (median decision), as the dashboard samples
blocks = collections.defaultdict(list)
for c in cells: blocks[(c['cluster'], int(c['hour'] // 3))].append(c)
sampled = [sorted(v, key=lambda c: c['hour'])[len(v) // 2] for v in blocks.values()]
excl = [c for c in cells if c['ex']]; excl_s = [c for c in sampled if c['ex']]
print('\n== full censoring (B) vs served (A)')
verdict(excl, 1.0, label='cells with an exclusion (all decisions)')
verdict(excl_s, 1.0, label='cells with an exclusion (3h-block sample)')
verdict(cells, 1.0, label='all P0-M cells (no-exclusion rows identical)')
for lo, hi in ((0, 6), (6, 12), (12, 15), (15, 18), (18, 25)):
    verdict([c for c in excl if lo <= c['hour'] < hi], 1.0, label=f'  local hour {lo:02d}-{hi:02d}')
for lo, hi in ((0, .02), (.02, .1), (.1, .3), (.3, 1.01)):
    verdict([c for c in excl if lo <= c['out_mass'] < hi], 1.0, label=f'  ruled-out mass {lo:.2f}-{hi:.2f}')
for u in ('C', 'F'):
    verdict([c for c in excl if (c['centers'][1] - c['centers'][0] > 1.05) == (u == 'F')], 1.0, label=f'  unit {u}')
for half in (('09-19..09-24', lambda d: d <= '2026-09-24'), ('09-25..09-30', lambda d: d >= '2026-09-25')):
    verdict([c for c in excl if half[1](c['r']['date'])], 1.0, label=f'  event days {half[0]}')
print('\n== w grid (cells with an exclusion)')
for w in (0.25, 0.5, 0.75, 1.0):
    verdict(excl, w, label=f'w={w}')
# walk-forward w: choose w on earlier event days by mean LogScore, apply to next day
print('\n== walk-forward w (choose on all earlier event days by LogScore, min 4 days)')
days = sorted({c['r']['date'] for c in excl}); grid = [0, .25, .5, .75, 1.0]; chosen = {}
for i, d in enumerate(days):
    if i < 4: continue
    tr = [c for c in excl if c['r']['date'] < d]
    chosen[d] = min(grid, key=lambda w: np.mean([score(c, w)[0][0] for c in tr]))
print('chosen w by day', chosen)
wf = [c for c in excl if c['r']['date'] in chosen]
base = [score(c, 0.0)[0] for c in wf]; cand = [score(c, chosen[c['r']['date']])[0] for c in wf]
g = _proper_score_gate(base, cand, [c['cluster'] for c in wf])
print('walk-forward cells %d st-days %d dLog %+.4f %s dBrier %+.4f %s dCRPS %+.4f %s lower=%s' % (g['cells'], g['station_days'], g['d_logscore'], g['ci_logscore'], g['d_brier'], g['ci_brier'], g['d_crps'], g['ci_crps'], g['lower']))
# example
for c in excl:
    r = c['r']
    if r['station'] == 'KMIA' and r['date'] == '2026-09-29' and c['hour'] > 16:
        a = masked(c['pre'], c['ex'], 0.0); b = masked(c['pre'], c['ex'], 1.0)
        print('\nKMIA 09-29 local %.1fh running high %.2fC truth %.2fC (%s)' % (c['hour'], r['running_high'], r['truth'], c['k']))
        for k in r['keys']:
            if a[k] > 0.001 or b[k] > 0.001: print('   %-18s served %.3f  censored %.3f' % (k, a[k], b[k]))
        break
json.dump(dict(cells=len(cells), excluded_cells=len(excl), chosen_w=chosen), open(HERE / 'results_meta.json', 'w'))
