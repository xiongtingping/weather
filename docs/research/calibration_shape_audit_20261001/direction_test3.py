"""Stability of the Task 3 signal: by date half, by version family, and under a strict walk-forward (train only on
earlier dates).  Also: is the carrier (raw pool minus served centre = net bias correction) overshooting in specific systems?"""
import json, math, pathlib, collections, sys
import numpy as np
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / 'docs/research/model_market_gap_decomposition_20260926'))
from direction_test import features, cls, FAMILIES, rng
from analyze import axis
import decompose as D
rows = json.load(open(HERE / 'direction_data.json')); cap = {(r['station'], r['date']): r for r in json.load(open(HERE / 'capture.json'))['rows']}
data = []
for r in rows:
    f = features(r, False)
    if f is None: continue
    data.append(dict(date=r['date'], station=r['station'], e=r['e'], f=f, r=r, ver=int(r['version'].split('_v')[-1])))
keys_all = [k for k in data[0]['f'] if not k.startswith('_')]; F1 = [k for k in keys_all if FAMILIES['F1 structure (positions, pairs, camps, dispersion)'](k)]
dates = sorted({d['date'] for d in data}); dts = np.array([d['date'] for d in data]); e = np.array([d['e'] for d in data]); ver = np.array([d['ver'] for d in data])
def ridge(keys, train_mask, test_mask, lam=5.0):
    X = np.array([[d['f'][k] for k in keys] for d in data]); m, s = X[train_mask].mean(0), X[train_mask].std(0) + 1e-9; Xs = (X - m) / s
    A = np.hstack([Xs[train_mask], np.ones((train_mask.sum(), 1))]); w = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ e[train_mask])
    return np.hstack([Xs[test_mask], np.ones((test_mask.sum(), 1))]) @ w
def lodo(keys):
    pred = np.zeros(len(e))
    for dte in dates: te = dts == dte; pred[te] = ridge(keys, ~te, te)
    return pred
def walkforward(keys, min_train=10):
    pred = np.full(len(e), np.nan)
    for i, dte in enumerate(dates):
        if i < min_train: continue
        te = dts == dte; tr = np.isin(dts, dates[:i]); pred[te] = ridge(keys, tr, te)
    return pred
def deltas(pred, scale, mask):
    dB = []; dL = []; g = []; sd = []
    for i, (d, dm) in enumerate(zip(data, pred)):
        if not mask[i] or not np.isfinite(dm): continue
        r = d['r']; c = cap[(r['station'], r['date'])]['last']; keys, lo, hi, rung = axis(c['inputs']['bucket_intervals_c_json'])
        centers = [(l + h) / 2 if l is not None and h is not None else (h - 0.5 if l is None else l + 0.5) for l, h in zip(lo, hi)]; oe = (hi[0] if lo[0] is None else None, lo[-1] if hi[-1] is None else None)
        p = D.as_vec(c['probability'], keys); hit = next(j for j, (l, h) in enumerate(zip(lo, hi)) if (l is None or r['truth'] >= l) and (h is None or r['truth'] < h))
        q = D.as_vec(D.shift_tail(dict(zip(keys, p)), delta_c=float(scale * dm), keys=keys, centers=centers, open_edges=oe), keys)
        l1, b1 = D.score(q, keys, hit); l0, b0 = D.score(p, keys, hit); dB.append(b1 - b0); dL.append(l1 - l0); g.append(d['date']); sd.append((d['station'], d['date']))
    dB = np.array(dB); dL = np.array(dL)
    def boot(group):
        u = sorted(set(group)); idx = {x: [i for i, y in enumerate(group) if y == x] for x in u}; oB = []; oL = []
        for _ in range(2000):
            pick = rng.choice(len(u), len(u), replace=True); sel = np.concatenate([idx[u[j]] for j in pick]); oB.append(dB[sel].mean()); oL.append(dL[sel].mean())
        return [float(dB.mean()), float(np.percentile(oB, 2.5)), float(np.percentile(oB, 97.5))], [float(dL.mean()), float(np.percentile(oL, 2.5)), float(np.percentile(oL, 97.5))]
    bD, lD = boot(g); bS, lS = boot(sd)
    return dict(n=len(dB), n_dates=len(set(g)), d_brier_date=bD, d_logscore_date=lD, d_brier_sd=bS, d_logscore_sd=lS, verdict_date='PASS' if bD[2] < 0 and lD[2] < 0 else 'FAIL', verdict_sd='PASS' if bS[2] < 0 and lS[2] < 0 else 'FAIL')
out = {}; f3 = lambda v: '%+.4f [%+.4f,%+.4f]' % tuple(v)
half1 = np.isin(dts, dates[:15]); half2 = ~half1; v19 = ver >= 19; old = ver <= 7
pF1 = lodo(F1); pRaw = lodo(['rawpool_minus_mu']); wF1 = walkforward(F1); wRaw = walkforward(['rawpool_minus_mu'])
for name, pred in (('LODO ridge_F1 x0.5', pF1), ('LODO rawpool_only x0.5', pRaw)):
    out[name] = {k: deltas(pred, 0.5, m) for k, m in (('all', np.ones(len(e), bool)), ('first 15 dates', half1), ('last 15 dates', half2), ('v2-v7', old), ('v19+', v19))}
for name, pred in (('WALK-FORWARD ridge_F1 x0.5 (train<test date, >=10 dates)', wF1), ('WALK-FORWARD rawpool_only x0.5', wRaw)):
    ok = np.isfinite(pred); out[name] = {k: deltas(pred, 0.5, m & ok) for k, m in (('all tested', np.ones(len(e), bool)), ('v19+', v19), ('last 10 dates', np.isin(dts, dates[-10:])))}
for name, arms in out.items():
    print('##', name)
    for k, v in arms.items(): print('   %-16s n=%4d d=%2d  dBrier %s  dLog %s -> %s | station-day %s' % (k, v['n'], v['n_dates'], f3(v['d_brier_date']), f3(v['d_logscore_date']), v['verdict_date'], v['verdict_sd']))
# carrier by half / version
x = np.array([d['f']['rawpool_minus_mu'] for d in data])
print('## e vs rawpool_minus_mu (= -(net correction)) by subset: corr, mean x, slope')
for k, m in (('first 15 dates', half1), ('last 15 dates', half2), ('v2-v7', old), ('v8-v15', (ver >= 8) & (ver <= 15)), ('v19+', v19)):
    sl = np.polyfit(x[m], e[m], 1)[0]; print('   %-14s n=%4d corr %+.3f  mean raw-mu %+.2f  slope %+.3f  mean e %+.3f  mean e | raw-mu<-1.5: %+.3f (n=%d) | raw-mu>-0.5: %+.3f (n=%d)' % (k, m.sum(), np.corrcoef(x[m], e[m])[0, 1], x[m].mean(), sl, e[m].mean(), e[m & (x < -1.5)].mean(), (m & (x < -1.5)).sum(), e[m & (x > -0.5)].mean(), (m & (x > -0.5)).sum()))
# per-system correction overshoot (rows with calibrated centres)
per = collections.defaultdict(list)
for d in data:
    r = d['r']
    for s, cval in (r['centers'] or {}).items():
        if s in r['systems'] and r['weights'].get(s, 0) > 0: per[s].append((cval - r['systems'][s]['mean'], d['e'], r['truth'] - cval, d['ver']))
print('## per-system bias correction (calibrated centre - raw member mean): mean, corr(e, corr), truth-calibrated mean')
for s, v in per.items():
    a = np.array(v); print('   %-26s n=%4d  mean corr %+.2f  corr(e,corr) %+.3f  mean(truth-calibrated) %+.3f | v19+: mean corr %+.2f  mean(truth-cal) %+.3f (n=%d)' % (s, len(a), a[:, 0].mean(), np.corrcoef(a[:, 1], a[:, 0])[0, 1], a[:, 2].mean(), a[a[:, 3] >= 19, 0].mean() if (a[:, 3] >= 19).any() else float('nan'), a[a[:, 3] >= 19, 2].mean() if (a[:, 3] >= 19).any() else float('nan'), int((a[:, 3] >= 19).sum())))
json.dump(out, open(HERE / 'results_direction3.json', 'w'), indent=1, default=float)
