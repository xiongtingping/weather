"""Core finding, pinned down: spread-skill slope 0.41 and pool-size dependence of dispersion.
1 robustness of the slope (subsets, leave-one-station-out, rung-corrected sigma)
2 pool-size effect within the same dates (v2-v7: 2-seat vs 4-seat), and the first>36h panel
3 theoretical LogScore gain available if the spread-skill line were exactly corrected (Gaussian) vs test power
4 the direct fix: per-row lambda_i = sqrt((a+b*sigma_i^2)/sigma_i^2), (a,b) from training dates only,
  applied to the served vector with the production width transport; walk-forward + LODO; charter 211."""
import json, math, pathlib, sys, collections
import numpy as np
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))
from analyze import collect, axis, block_boot, wmean, rng
from bayes_basket_target.center_width_calibration import apply_center_width_calibration, served_bucket_axis, MIN_WIDTH_LAMBDA, MAX_WIDTH_LAMBDA
from bayes_basket_target.materialization import floor_probabilities
cap = {(r['station'], r['date']): r for r in json.load(open(HERE / 'capture.json'))['rows']}
def prep(tag):
    rows = collect(tag)
    if tag == 'first': rows = [r for r in rows if r['hours'] > 36]
    for r in rows:
        r['var'] = max(r['sd']**2 - r['rung']**2 / 12, 0.05)   # rung-corrected served variance
        r['res2'] = (r['truth'] - r['mu'])**2; r['ver'] = int(r['version'].split('_v')[-1]); r['nsys'] = sum(1 for w in r['weights'].values() if w > 0)
    return rows
last = prep('last'); first = prep('first')
f3 = lambda v: '%+.3f [%+.3f,%+.3f]' % tuple(v)
def slope_ci(rows):
    d = [r['date'] for r in rows]; x = np.array([r['var'] for r in rows]); y = np.array([r['res2'] for r in rows])
    def fit(w):
        W = np.asarray(w, float); A = np.vstack([np.ones_like(x), x]).T * np.sqrt(W)[:, None]; return np.linalg.lstsq(A, y * np.sqrt(W), rcond=None)[0]
    s = block_boot(d, lambda w: float(fit(w)[1])); a = block_boot(d, lambda w: float(fit(w)[0])); z2 = block_boot(d, lambda w: wmean(y / x, w))
    return dict(n=len(rows), n_dates=len(set(d)), slope=s, intercept=a, Ez2=z2, mean_var=float(x.mean()), mean_res2=float(y.mean()))
out = {}
print('## 1 spread-skill slope E[res^2] = a + b*sigma^2 (rung-corrected), by subset   [b=1,a=0 if dispersion right]')
sub = {'last all': last, 'last first 15 dates': [r for r in last if r['date'] <= sorted({x['date'] for x in last})[14]], 'last last 15 dates': [r for r in last if r['date'] > sorted({x['date'] for x in last})[14]],
       'last v2-v7': [r for r in last if r['ver'] <= 7], 'last v8-v15': [r for r in last if 8 <= r['ver'] <= 15], 'last v19+': [r for r in last if r['ver'] >= 19], 'last C': [r for r in last if r['unit'] == 'C'], 'last F': [r for r in last if r['unit'] == 'F'],
       'first>36h all': first, 'first>36h v2-v7': [r for r in first if r['ver'] <= 7], 'first>36h v8+': [r for r in first if r['ver'] >= 8]}
out['slope'] = {}
for k, rows in sub.items():
    if len(rows) < 40: continue
    s = slope_ci(rows); out['slope'][k] = s; print('   %-22s n=%4d d=%2d  b %s  a %s  E[z^2] %s  mean var %.2f res2 %.2f' % (k, s['n'], s['n_dates'], f3(s['slope']), f3(s['intercept']), f3(s['Ez2']), s['mean_var'], s['mean_res2']))
# leave-one-station-out slope range (last all)
loso = []
for st in sorted({r['station'] for r in last}):
    rows = [r for r in last if r['station'] != st]; x = np.array([r['var'] for r in rows]); y = np.array([r['res2'] for r in rows]); loso.append(np.polyfit(x, y, 1)[0])
out['slope_leave_one_station_out'] = [float(min(loso)), float(max(loso))]; print('   leave-one-station-out slope range: %.3f .. %.3f' % (min(loso), max(loso)))
print('## 2 dispersion by seated systems within the same period (E[z^2], rung-corrected)')
out['pool'] = {}
for lab, rows in (('last v2-v7', [r for r in last if r['ver'] <= 7]), ('last v8-v15', [r for r in last if 8 <= r['ver'] <= 15]), ('last v19+', [r for r in last if r['ver'] >= 19]), ('first>36h all', first)):
    for n in (2, 3, 4, 5, 6):
        rr = [r for r in rows if r['nsys'] == n]
        if len(rr) < 25: continue
        z = block_boot([r['date'] for r in rr], lambda w, rr=rr: wmean(np.array([r['res2'] / r['var'] for r in rr]), w)); sd = float(np.sqrt(np.mean([r['var'] for r in rr]))); rm = float(np.sqrt(np.mean([r['res2'] for r in rr])))
        out['pool'][f'{lab} n={n}'] = dict(n=len(rr), dates=len({r['date'] for r in rr}), Ez2=z, served_sd=sd, rms=rm); print('   %-16s %d seated  n=%4d d=%2d  E[z^2] %s  served sd %.2f  rms %.2f' % (lab, n, len(rr), len({r['date'] for r in rr}), f3(z), sd, rm))
# 3 theoretical gain if the line were exact: r_i = sigma_i / sigma_target_i ; gain_i = ln r + 1/(2 r^2) - 1/2 (nat, Gaussian)
print('## 3 Gaussian-theory LogScore gain available from exact spread-skill correction (upper bound, continuous)')
out['theory'] = {}
for lab, rows in (('last all', last), ('last v19+', [r for r in last if r['ver'] >= 19]), ('first>36h', first)):
    x = np.array([r['var'] for r in rows]); y = np.array([r['res2'] for r in rows]); a, b = np.polyfit(x, y, 1)[::-1]; tgt = np.maximum(a + b * x, 0.05); rr = np.sqrt(x / tgt); g = np.log(rr) + 1 / (2 * rr**2) - 0.5
    out['theory'][lab] = dict(a=float(a), b=float(b), mean_gain_nat=float(g.mean()), median_r=float(np.median(rr)), r_q10_q90=[float(np.percentile(rr, 10)), float(np.percentile(rr, 90))])
    print('   %-12s a %.2f b %.2f  r=sigma/target median %.2f [q10 %.2f, q90 %.2f]  mean available gain %.4f nat' % (lab, a, b, np.median(rr), *np.percentile(rr, [10, 90]), g.mean()))
# 4 direct fix on the served vector
def rescale_rows(rows, lam_of):
    dB = []; dL = []
    for r, lam in zip(rows, lam_of):
        c = cap[(r['station'], r['date'])]['last' if r in last else 'first']; iv = c['inputs']['bucket_intervals_c_json']; keys = r['keys']; centers, edges = served_bucket_axis(iv, keys)
        p = {k: float(v) for k, v in zip(keys, r['pv'])}; lam = float(min(MAX_WIDTH_LAMBDA, max(MIN_WIDTH_LAMBDA, lam)))
        q, _ = apply_center_width_calibration(p, delta_c=0.0, width_lambda=lam, ordered_keys=keys, key_centers_c=centers, open_edge_bounds_c=edges) if abs(lam - 1) > 1e-9 else (p, None)
        qf, _ = floor_probabilities(q); t = sum(qf.values()); qv = np.array([qf[k] / t for k in keys]); pf, _ = floor_probabilities(p); t0 = sum(pf.values()); pv = np.array([pf[k] / t0 for k in keys])
        y = np.zeros(len(keys)); y[r['idx']] = 1; dL.append(-math.log(qv[r['idx']]) + math.log(pv[r['idx']])); dB.append(float(((qv - y)**2).sum() - ((pv - y)**2).sum()))
    return np.array(dB), np.array(dL)
def boot(v, g):
    u = sorted(set(g)); idx = {x: [i for i, y in enumerate(g) if y == x] for x in u}; o = []
    for _ in range(2000):
        pick = rng.choice(len(u), len(u), replace=True); sel = np.concatenate([idx[u[j]] for j in pick]); o.append(v[sel].mean())
    return [float(v.mean()), float(np.percentile(o, 2.5)), float(np.percentile(o, 97.5))]
def fix_test(rows, label, mode, min_train=10, shrink_w=None):
    dates = sorted({r['date'] for r in rows}); dts = np.array([r['date'] for r in rows]); x = np.array([r['var'] for r in rows]); y = np.array([r['res2'] for r in rows]); lam = np.full(len(rows), np.nan)
    for i, dte in enumerate(dates):
        te = dts == dte
        if mode == 'lodo': tr = ~te
        else:
            if i < min_train: continue
            tr = np.isin(dts, dates[:i])
        if shrink_w is None: a, b = np.polyfit(x[tr], y[tr], 1)[::-1]; tgt = np.maximum(a + b * x[te], 0.05)
        else: m = y[tr].mean(); tgt = (1 - shrink_w) * x[te] + shrink_w * m
        lam[te] = np.sqrt(tgt / x[te])
    ok = np.isfinite(lam); rr = [r for r, o in zip(rows, ok) if o]; dB, dL = rescale_rows(rr, lam[ok]); g = [r['date'] for r in rr]
    res = {'n': len(rr), 'n_dates': len(set(g)), 'd_brier': boot(dB, g), 'd_logscore': boot(dL, g), 'mean_lambda': float(lam[ok].mean()), 'lambda_q10_q90': [float(np.percentile(lam[ok], 10)), float(np.percentile(lam[ok], 90))]}
    res['verdict'] = 'PASS' if res['d_brier'][2] < 0 and res['d_logscore'][2] < 0 else ('WORSE' if res['d_brier'][1] > 0 or res['d_logscore'][1] > 0 else 'FAIL')
    # out-of-sample E[z^2] after the fix, by sigma quartile (does the line flatten?)
    z_after = np.array([r['res2'] / (r['var'] * l**2) for r, l in zip(rr, lam[ok])]); vv = np.array([r['var'] for r in rr]); q = np.percentile(vv, [25, 50, 75]); bins = np.digitize(vv, q)
    res['Ez2_after_by_sigma_quartile'] = [float(z_after[bins == b].mean()) for b in range(4)]; res['Ez2_before_by_sigma_quartile'] = [float((y[ok][bins == b] / vv[bins == b]).mean()) for b in range(4)]
    print('   %-34s %-11s n=%4d d=%2d  lambda mean %.2f [q10 %.2f q90 %.2f]  dBrier %s  dLog %s -> %s | E[z^2] by sigma quartile before %s after %s' % (
        label, mode, res['n'], res['n_dates'], res['mean_lambda'], *res['lambda_q10_q90'], '%+.4f [%+.4f,%+.4f]' % tuple(res['d_brier']), '%+.4f [%+.4f,%+.4f]' % tuple(res['d_logscore']), res['verdict'],
        [round(v, 2) for v in res['Ez2_before_by_sigma_quartile']], [round(v, 2) for v in res['Ez2_after_by_sigma_quartile']]))
    return res
print('## 4 direct fix: lambda_i = sqrt((a + b sigma_i^2)/sigma_i^2) from training dates, production width transport on served vector')
out['fix'] = {}
for lab, rows in (('last all', last), ('last v19+', [r for r in last if r['ver'] >= 19]), ('last last 15 dates', sub['last last 15 dates']), ('first>36h all', first)):
    for mode in ('walkforward', 'lodo'):
        if mode == 'walkforward' and len({r['date'] for r in rows}) < 14: continue
        out['fix'][f'{lab}|{mode}|affine'] = fix_test(rows, lab + ' affine a+b*s2', mode, min_train=10 if len({r['date'] for r in rows}) >= 20 else 3)
print('## 4b alternative: shrink sigma^2 toward the training mean residual^2 with w in {0.25,0.5,0.75} (fixed, no fit beyond the mean)')
for lab, rows in (('last all', last), ('last v19+', [r for r in last if r['ver'] >= 19]), ('first>36h all', first)):
    for w in (0.25, 0.5, 0.75):
        out['fix'][f'{lab}|lodo|shrink{w}'] = fix_test(rows, f'{lab} shrink w={w}', 'lodo', shrink_w=w)
json.dump(out, open(HERE / 'results_core_focus.json', 'w'), indent=1, default=float)
