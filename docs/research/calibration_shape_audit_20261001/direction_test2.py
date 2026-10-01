"""Task 2 deepening + Task 3: which feature carries the direction signal, is it stable date by date, and does
shifting the served P0 by a LODO-predicted delta_mu pass charter 211 (LogScore AND Brier both lower, 95%)."""
import json, math, pathlib, collections, itertools, sys
import numpy as np
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / 'docs/research/model_market_gap_decomposition_20260926'))
from direction_test import features, cls, evaluate, FAMILIES, rng
from analyze import axis
import decompose as D
from bayes_basket_target.materialization import floor_probabilities
rows = json.load(open(HERE / 'direction_data.json')); cap = {(r['station'], r['date']): r for r in json.load(open(HERE / 'capture.json'))['rows']}
data = []
for r in rows:
    f = features(r, False)
    if f is None: continue
    data.append(dict(date=r['date'], station=r['station'], e=r['e'], y=cls(r['e']), f=f, r=r))
keys_all = [k for k in data[0]['f'] if not k.startswith('_')]
F1 = [k for k in keys_all if FAMILIES['F1 structure (positions, pairs, camps, dispersion)'](k)]
dates = sorted({d['date'] for d in data}); dts = np.array([d['date'] for d in data]); e = np.array([d['e'] for d in data])
out = {}
# 1. single-feature LODO and leave-one-feature-out
out['single_feature'] = {k: evaluate(data, [k], k)['d_logloss'] for k in F1}
out['F1_minus_rawpool'] = evaluate(data, [k for k in F1 if k != 'rawpool_minus_mu'], 'F1 - rawpool')['d_logloss']
out['F1_minus_rawpool_minus_between'] = evaluate(data, [k for k in F1 if k not in ('rawpool_minus_mu', 'between_sd', 'within_sd')], 'positions+pairs only')['d_logloss']
out['rawpool_plus_trend'] = evaluate(data, ['rawpool_minus_mu', 'pool_trend'], 'rawpool+trend')['d_logloss']
# 2. binned e vs rawpool_minus_mu and vs pool_trend
def binned(k, edges):
    v = np.array([d['f'][k] for d in data]); res = []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (v >= a) & (v < b)
        if m.sum() < 15: continue
        res.append(dict(range=[a, b], n=int(m.sum()), mean_e=float(e[m].mean()), p_warm=float((e[m] > .5).mean()), p_cold=float((e[m] < -.5).mean()), mean_x=float(v[m].mean())))
    return res
out['e_by_rawpool_minus_mu'] = binned('rawpool_minus_mu', [-9, -1.5, -1.0, -0.75, -0.5, -0.25, 0, 0.5, 9])
out['e_by_pool_trend'] = binned('pool_trend', [-9, -0.5, -0.3, -0.1, 0.1, 0.3, 0.5, 9])
out['corr_e_rawpool'] = float(np.corrcoef(e, [d['f']['rawpool_minus_mu'] for d in data])[0, 1]); out['corr_e_trend'] = float(np.corrcoef(e, [d['f']['pool_trend'] for d in data])[0, 1])
# what is rawpool_minus_mu made of (rows with calibrated centres): calibrated-vs-raw per system, weighted calibrated mean vs served mu
comp = []
for d in data:
    r = d['r']; cen = r['centers']; w = r['weights']
    if not cen: continue
    S = [s for s in r['systems'] if s in cen and w.get(s, 0) > 0]
    if len(S) < 2: continue
    raw = {s: r['systems'][s]['mean'] for s in S}; tw = sum(w[s] for s in S)
    calw = sum(w[s] * cen[s] for s in S) / tw; raww = sum(w[s] * raw[s] for s in S) / tw
    comp.append(dict(e=d['e'], bias_corr=calw - raww, served_minus_cal=r['mu'] - calw, raw_minus_mu=raww - r['mu'], rad=r.get('radiation_shift_c') or 0.0))
if comp:
    A = {k: np.array([c[k] for c in comp]) for k in comp[0]}
    out['decomposition_of_rawpool_minus_mu'] = dict(n=len(comp), mean_bias_correction_c=float(A['bias_corr'].mean()), mean_served_minus_weighted_calibrated=float(A['served_minus_cal'].mean()),
        corr_e_bias_corr=float(np.corrcoef(A['e'], A['bias_corr'])[0, 1]), corr_e_served_minus_cal=float(np.corrcoef(A['e'], A['served_minus_cal'])[0, 1]), corr_e_rad=float(np.corrcoef(A['e'], A['rad'])[0, 1]),
        corr_e_raw_minus_mu=float(np.corrcoef(A['e'], A['raw_minus_mu'])[0, 1]))
# 3. date-by-date stability of the F1 classifier and of the raw-pool rule
def lodo_per_date(keys):
    X = np.array([[d['f'][k] for k in keys] for d in data]); y = np.array([d['y'] for d in data]); per = {}
    from direction_test import fit_mlogit, predict
    for dte in dates:
        te = dts == dte; tr = ~te; m, s = X[tr].mean(0), X[tr].std(0) + 1e-9; Xs = (X - m) / s; prior = np.bincount(y[tr], minlength=3) / tr.sum()
        W = fit_mlogit(Xs[tr], y[tr], 1.0); P = np.clip(predict(W, Xs[te]), 1e-6, 1); P /= P.sum(1, keepdims=True)
        per[dte] = float((-np.log(P[np.arange(te.sum()), y[te]]) + np.log(prior[y[te]])).mean())
    return per
pd_ = lodo_per_date(F1); vals = np.array(list(pd_.values()))
out['F1_per_date_d_logloss'] = dict(n_dates_better=int((vals < 0).sum()), n_dates=len(vals), first_half=float(vals[:15].mean()), second_half=float(vals[15:].mean()), per_date={k: round(v, 4) for k, v in pd_.items()})
# 4. Task 3: shift the served vector by LODO-predicted delta_mu; score with production floor; 211 gate (date-block and station-day both)
def ridge_pred(keys, lam=5.0):
    X = np.array([[d['f'][k] for k in keys] for d in data]); pred = np.zeros(len(e))
    for dte in dates:
        te = dts == dte; tr = ~te; m, s = X[tr].mean(0), X[tr].std(0) + 1e-9; Xs = (X - m) / s
        A = np.hstack([Xs[tr], np.ones((tr.sum(), 1))]); w = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ e[tr]); pred[te] = np.hstack([Xs[te], np.ones((te.sum(), 1))]) @ w
    return pred
def shifted_scores(pred, scale):
    dB = []; dL = []; keys_sd = []
    for d, dm in zip(data, pred):
        r = d['r']; c = cap[(r['station'], r['date'])]['last']; iv = c['inputs']['bucket_intervals_c_json']; keys, lo, hi, rung = axis(iv)
        centers = [(l + h) / 2 if l is not None and h is not None else (h - 0.5 if l is None else l + 0.5) for l, h in zip(lo, hi)]
        open_edges = (hi[0] if lo[0] is None else None, lo[-1] if hi[-1] is None else None)
        p = D.as_vec(c['probability'], keys); hit = next(i for i, (l, h) in enumerate(zip(lo, hi)) if (l is None or r['truth'] >= l) and (h is None or r['truth'] < h))
        q = D.as_vec(D.shift_tail(dict(zip(keys, p)), delta_c=float(scale * dm), keys=keys, centers=centers, open_edges=open_edges), keys) if abs(scale * dm) > 1e-9 else p
        l1, b1 = D.score(q, keys, hit); l0, b0 = D.score(p, keys, hit); dB.append(b1 - b0); dL.append(l1 - l0); keys_sd.append((r['station'], r['date']))
    dB = np.array(dB); dL = np.array(dL)
    def boot(group):
        u = sorted(set(group)); idx = {g: [i for i, x in enumerate(group) if x == g] for g in u}; oB = []; oL = []
        for _ in range(2000):
            pick = rng.choice(len(u), len(u), replace=True); sel = np.concatenate([idx[u[j]] for j in pick]); oB.append(dB[sel].mean()); oL.append(dL[sel].mean())
        return [float(dB.mean()), float(np.percentile(oB, 2.5)), float(np.percentile(oB, 97.5))], [float(dL.mean()), float(np.percentile(oL, 2.5)), float(np.percentile(oL, 97.5))]
    bD, lD = boot(list(dts)); bS, lS = boot(keys_sd)
    return dict(scale=scale, mean_abs_shift_c=float(np.abs(scale * pred).mean()), d_brier_date=bD, d_logscore_date=lD, d_brier_stationday=bS, d_logscore_stationday=lS,
                verdict_211_date='PASS' if bD[2] < 0 and lD[2] < 0 else 'FAIL', verdict_211_stationday='PASS' if bS[2] < 0 and lS[2] < 0 else 'FAIL',
                oos_R2=float(1 - ((e - pred)**2).mean() / ((e - e.mean())**2).mean()), oos_rmse_e_after=float(np.sqrt(((e - scale * pred)**2).mean())), rmse_e_before=float(np.sqrt((e**2).mean())))
task3 = {}
for name, keys in (('ridge_F1', F1), ('ridge_rawpool_only', ['rawpool_minus_mu']), ('ridge_rawpool_trend', ['rawpool_minus_mu', 'pool_trend']), ('ridge_F3', [k for k in keys_all if FAMILIES['F3 F2 + context (hours, unit, clearness, sd, level)'](k)])):
    pred = ridge_pred(keys)
    task3[name] = {str(s): shifted_scores(pred, s) for s in (1.0, 0.5)}
# reference: shift toward market mean (oracle-ish information outside the model), half-way
mk = [i for i, d in enumerate(data) if d['r']['market_mean'] is not None]
sub = [data[i] for i in mk]; predm = np.array([d['r']['market_mean'] - d['r']['mu'] for d in sub])
data_backup = data; data = sub; dts_b = dts; dts = np.array([d['date'] for d in sub]); e_b = e; e = np.array([d['e'] for d in sub])
task3['REFERENCE_shift_toward_market'] = {str(s): shifted_scores(predm, s) for s in (1.0, 0.5)}
data = data_backup; dts = dts_b; e = e_b
out['task3_shift_tests'] = task3
json.dump(out, open(HERE / 'results_direction2.json', 'w'), indent=1, default=float)
f3 = lambda v: '%+.4f [%+.4f,%+.4f]' % tuple(v)
print('## single-feature LODO d_logloss (F1 features):'); [print('   %-22s %s' % (k, f3(v))) for k, v in sorted(out['single_feature'].items(), key=lambda kv: kv[1][0])]
print('## F1 minus rawpool:', f3(out['F1_minus_rawpool']), ' positions+pairs only:', f3(out['F1_minus_rawpool_minus_between']), ' rawpool+trend:', f3(out['rawpool_plus_trend']))
print('## e by rawpool_minus_mu (corr %.3f):' % out['corr_e_rawpool']); [print('   ', x) for x in out['e_by_rawpool_minus_mu']]
print('## e by pool_trend (corr %.3f):' % out['corr_e_trend']); [print('   ', x) for x in out['e_by_pool_trend']]
print('## decomposition of rawpool_minus_mu:', out.get('decomposition_of_rawpool_minus_mu'))
print('## F1 per-date stability:', {k: v for k, v in out['F1_per_date_d_logloss'].items() if k != 'per_date'})
print('## TASK 3 shift tests (211 gate):')
for name, arms in task3.items():
    for s, v in arms.items():
        print('   %-32s scale %s  |shift| %.2fC  R2 %.3f rmse_e %.3f->%.3f | dBrier date %s dLog date %s -> %s | station-day %s' % (name, s, v['mean_abs_shift_c'], v['oos_R2'], v['rmse_e_before'], v['oos_rmse_e_after'], f3(v['d_brier_date']), f3(v['d_logscore_date']), v['verdict_211_date'], v['verdict_211_stationday']))
