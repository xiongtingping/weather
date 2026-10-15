"""Does choosing / weighting systems per station by their shoulder skill (walk-forward) beat an equal combination?
Unit = (station, day, lead): each system's newest run's walk-forward-debiased member mean c_s.  Reference bucket = bucket of the
equal mean of c_s; rows with truth in m-1 / m+1.  Combined position scores: equal mean; walk-forward station-best system; walk-forward
station weights ~ max(AUC_s - 0.5, 0) on earlier days (>= 8 days, else equal)."""
import gzip, json, math
from collections import defaultdict
import numpy as np
from analyze import auc, auc_ci, to_unit, bucket
rows = [json.loads(l) for l in gzip.open('members.jsonl.gz', 'rt')]
for r in rows: r['mean'] = float(np.mean(r['vals']))
hist = defaultdict(list); fleet = defaultdict(list)
for r in rows:
    hist[(r['sys'], r['st'], r['lead'], r['cycle'])].append((r['day'], r['truth'] - r['mean'])); fleet[(r['sys'], r['lead'])].append((r['day'], r['truth'] - r['mean']))
def prior(lst, day, k):
    v = [e for d, e in lst if d < day]; return float(np.mean(v)) if len({d for d, _ in lst if d < day}) >= k and v else None
newest = {}
for r in rows:
    key = (r['st'], r['day'], r['lead'], r['sys'])
    if key not in newest or (r['run'] or '') > (newest[key]['run'] or ''): newest[key] = r
units = defaultdict(dict)
for (st, d, ld, s), r in newest.items():
    b = prior(hist[(s, st, ld, r['cycle'])], d, 5)
    if b is None: b = prior(fleet[(s, ld)], d, 5)
    if b is None: continue
    units[(st, d, ld)][s] = (to_unit(r['mean'] + b, r['unit']), r['unit'], to_unit(r['truth'], r['unit']))
recs = []
for (st, d, ld), m in units.items():
    if len(m) < 3: continue
    unit = next(iter(m.values()))[1]; tu = next(iter(m.values()))[2]
    ceq = float(np.mean([v[0] for v in m.values()]))
    km, cen, w = bucket(ceq, unit); kt, _, _ = bucket(tu, unit)
    if abs(kt - km) != 1: continue
    recs.append({'st': st, 'day': d, 'lead': ld, 'y': float(kt == km + 1), 'off': {s: (v[0] - cen) / w for s, v in m.items()}})
print('units with >=3 systems, truth in a shoulder of the equal-mean bucket:', len(recs))
days = sorted({x['day'] for x in recs})
def sys_auc_prior(st, day):
    pr = [x for x in recs if x['st'] == st and x['day'] < day]
    if len({x['day'] for x in pr}) < 8: return None
    out = {}
    for s in {s for x in pr for s in x['off']}:
        sel = [x for x in pr if s in x['off']]
        if len(sel) >= 15: out[s] = auc([x['off'][s] for x in sel], [x['y'] for x in sel])
    return out
cache = {}
eq, best, wtd, ys, ds = [], [], [], [], []
for x in recs:
    k = (x['st'], x['day'])
    if k not in cache: cache[k] = sys_auc_prior(*k)
    a = cache[k]
    if a is None: continue
    e = float(np.mean(list(x['off'].values())))
    avail = {s: v for s, v in a.items() if s in x['off'] and not math.isnan(v)}
    if not avail: continue
    bs = max(avail, key=avail.get)
    wts = {s: max(v - 0.5, 0.0) for s, v in avail.items()}
    tw = sum(wts.values())
    eq.append(e); best.append(x['off'][bs]); wtd.append(sum(wts[s] * x['off'][s] for s in wts) / tw if tw > 0 else e); ys.append(x['y']); ds.append(x['day'])
print('walk-forward evaluated units', len(ys))
for lab, s in (('equal mean of systems', eq), ('station-best system (walk-forward)', best), ('station AUC-weighted (walk-forward)', wtd)):
    a, lo, hi = auc_ci(s, ys, ds); print(f'  {lab:38s} AUC {a:.3f} [{lo:.3f},{hi:.3f}]')
# paired difference by day bootstrap
eq, best, wtd, ys, ds = map(np.array, (eq, best, wtd, ys, ds)); ud = np.unique(ds); rng = np.random.default_rng(7)
for lab, s in (('best - equal', best), ('weighted - equal', wtd)):
    diffs = []
    for _ in range(300):
        pick = rng.choice(ud, len(ud)); idx = np.concatenate([np.where(ds == d)[0] for d in pick])
        diffs.append(auc(s[idx], ys[idx]) - auc(eq[idx], ys[idx]))
    print(f'  {lab:20s} {auc(s, ys) - auc(eq, ys):+.3f} [{np.quantile(diffs, .025):+.3f},{np.quantile(diffs, .975):+.3f}]')
