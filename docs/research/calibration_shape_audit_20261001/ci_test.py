"""Core proposition (user, 2026-10-01):  Y  _||_  X_raw(six systems)  |  P0 ?
Given the served P0, does the full raw information of the six systems still improve the out-of-sample prediction of truth?
Arms (Gaussian predictive integrated onto the market ladder, 1e-3 floor; LogScore, Brier, CRPS, RMSE):
  P0v   served P0 bucket vector (as served)
  M0    P0 only, recalibrated on training dates: mu' = mu_P0 + a, sigma' = c*sigma_P0
  M1    M0 + ridge(X_raw) on the centre            <- the proposition: does X_raw add beyond P0?
  M1h   M1 + heteroscedastic sigma from X_raw (log-linear ridge on |residual|)
  M3    raw six only (no P0): mu' = ridge(raw means), sigma' = training residual sd    <- does fusion lose information?
LODO by date and strict walk-forward (train on earlier dates only, >=10).  Date-block bootstrap; charter 211 on Brier AND LogScore."""
import json, math, pathlib, sys, collections
import numpy as np
from scipy import stats
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))
from analyze import axis
from bayes_basket_target.materialization import floor_probabilities
rng = np.random.default_rng(20261001)
SYS = ['ECMWF_IFS_ENS_OPEN_PF50', 'DWD_ICON_GLOBAL_EPS', 'NOAA_GEFS_05', 'ECCC_GEM_GLOBAL_ENS', 'ECMWF_AIFS_ENS_GLOBAL', 'NOAA_AIGEFS_025']
rows = json.load(open(HERE / 'direction_data.json')); cap = {(r['station'], r['date']): r for r in json.load(open(HERE / 'capture.json'))['rows']}
data = []
for r in rows:
    e = cap[(r['station'], r['date'])]['last']; keys, lo, hi, rung = axis(e['inputs']['bucket_intervals_c_json'])
    pv = np.array([float(e['probability'].get(k, 0)) for k in keys]); pv /= pv.sum()
    hit = next(j for j, (l, h) in enumerate(zip(lo, hi)) if (l is None or r['truth'] >= l) and (h is None or r['truth'] < h))
    sy = r['systems']; avail = [s for s in SYS if s in sy]
    if len(avail) < 2: continue
    pool = float(np.mean([sy[s]['mean'] for s in avail]))
    x = {}
    for s in SYS:
        if s in sy: x[f'm_{s}'] = sy[s]['mean'] - r['mu']; x[f'sd_{s}'] = sy[s]['sd']; x[f'tr_{s}'] = (sy[s]['mean'] - sy[s]['prev_mean']) if sy[s].get('prev_mean') is not None else 0.0; x[f'miss_{s}'] = 0.0
        else: x[f'm_{s}'] = pool - r['mu']; x[f'sd_{s}'] = float(np.mean([sy[t]['sd'] for t in avail])); x[f'tr_{s}'] = 0.0; x[f'miss_{s}'] = 1.0
    x['pool_minus_mu'] = pool - r['mu']; x['between_sd'] = float(np.std([sy[s]['mean'] for s in avail])); x['within_sd'] = float(np.mean([sy[s]['sd'] for s in avail])); x['n_sys'] = float(len(avail))
    x['sigma_p0'] = r['sd']; x['mu_p0'] = r['mu']
    raw_only = {f'r_{s}': (sy[s]['mean'] if s in sy else pool) for s in SYS} | {f'rsd_{s}': x[f'sd_{s}'] for s in SYS} | {f'rmiss_{s}': x[f'miss_{s}'] for s in SYS} | {'r_between': x['between_sd']}
    data.append(dict(date=r['date'], station=r['station'], truth=r['truth'], mu=r['mu'], sd=r['sd'], e=r['e'], x=x, raw=raw_only, keys=keys, lo=lo, hi=hi, pv=pv, hit=hit, ver=int(r['version'].split('_v')[-1]), six=len(avail) == 6))
dates = sorted({d['date'] for d in data}); dts = np.array([d['date'] for d in data]); e = np.array([d['e'] for d in data]); T = np.array([d['truth'] for d in data]); MU = np.array([d['mu'] for d in data]); SD = np.array([d['sd'] for d in data])
XK = sorted(data[0]['x']); RK = sorted(data[0]['raw']); X = np.array([[d['x'][k] for k in XK] for d in data]); R = np.array([[d['raw'][k] for k in RK] for d in data])

def gauss_bucket(mu, sd, d):
    lo = np.array([-np.inf if l is None else l for l in d['lo']]); hi = np.array([np.inf if h is None else h for h in d['hi']])
    p = stats.norm.cdf(hi, mu, sd) - stats.norm.cdf(lo, mu, sd); p = np.maximum(p, 0); return p / p.sum() if p.sum() > 0 else np.ones(len(p)) / len(p)
def score_vec(p, d):
    f, _ = floor_probabilities({k: float(v) for k, v in zip(d['keys'], p)}); q = np.array([f[k] for k in d['keys']]); q /= q.sum(); y = np.zeros(len(q)); y[d['hit']] = 1
    return -math.log(q[d['hit']]), float(((q - y)**2).sum())
def crps_gauss(mu, sd, y):
    z = (y - mu) / sd; return sd * (z * (2 * stats.norm.cdf(z) - 1) + 2 * stats.norm.pdf(z) - 1 / math.sqrt(math.pi))
def ridge_fit(A, y, lam):
    m, s = A.mean(0), A.std(0) + 1e-9; As = (A - m) / s; Z = np.hstack([As, np.ones((len(y), 1))]); w = np.linalg.solve(Z.T @ Z + lam * np.diag([1] * As.shape[1] + [0]), Z.T @ y); return m, s, w
def ridge_pred(A, fit): m, s, w = fit; return np.hstack([(A - m) / s, np.ones((len(A), 1))]) @ w

def run(train, test, lam=5.0):
    """Return dict arm -> (mu', sd') arrays for test rows."""
    out = {}
    a = e[train].mean(); c = math.sqrt(np.mean((e[train] - a)**2 / SD[train]**2)); out['M0'] = (MU[test] + a, c * SD[test])
    f = ridge_fit(X[train], e[train], lam); pe = ridge_pred(X[test], f); res_tr = e[train] - ridge_pred(X[train], f); c1 = math.sqrt(np.mean(res_tr**2 / SD[train]**2)); out['M1'] = (MU[test] + pe, c1 * SD[test])
    fh = ridge_fit(X[train], np.log(np.abs(res_tr) + 0.05), lam); lh = ridge_pred(X[test], fh); lh_tr = ridge_pred(X[train], fh); scale = math.sqrt(np.mean(res_tr**2 / np.exp(2 * lh_tr))); out['M1h'] = (MU[test] + pe, np.maximum(scale * np.exp(lh), 0.3))
    f3 = ridge_fit(R[train], T[train], lam); p3 = ridge_pred(R[test], f3); s3 = math.sqrt(np.mean((T[train] - ridge_pred(R[train], f3))**2)); out['M3'] = (p3, np.full(test.sum(), s3))
    return out
ARMS = ['P0v', 'M0', 'M1', 'M1h', 'M3']
def evaluate(mask_eval, mode, label, min_train=10):
    sc = {a: {'ls': np.full(len(data), np.nan), 'br': np.full(len(data), np.nan), 'crps': np.full(len(data), np.nan), 'se': np.full(len(data), np.nan)} for a in ARMS}
    for i, dte in enumerate(dates):
        te = dts == dte
        if mode == 'lodo': tr = ~te
        else:
            if i < min_train: continue
            tr = np.isin(dts, dates[:i])
        arms = run(tr, te); idx = np.where(te)[0]
        for a in ARMS:
            for j, k in enumerate(idx):
                d = data[k]
                if a == 'P0v': p = d['pv']; mu_, sd_ = d['mu'], d['sd']
                else: mu_, sd_ = arms[a][0][j], arms[a][1][j]; p = gauss_bucket(mu_, sd_, d)
                ls, br = score_vec(p, d); sc[a]['ls'][k] = ls; sc[a]['br'][k] = br; sc[a]['crps'][k] = crps_gauss(mu_, sd_, d['truth']); sc[a]['se'][k] = (mu_ - d['truth'])**2
    m = mask_eval & np.isfinite(sc['M0']['ls'])
    def boot(v):
        g = dts[m]; u = sorted(set(g)); idx = {x: np.where(g == x)[0] for x in u}; out = []
        for _ in range(2000):
            pick = rng.choice(u, len(u), replace=True); sel = np.concatenate([idx[x] for x in pick]); out.append(v[sel].mean())
        return [float(v.mean()), float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]
    res = {'label': label, 'n': int(m.sum()), 'n_dates': len(set(dts[m])), 'levels': {a: {k: float(np.nanmean(sc[a][k][m])) for k in ('ls', 'br', 'crps')} | {'rmse': float(math.sqrt(np.nanmean(sc[a]['se'][m])))} for a in ARMS}}
    comps = {}
    for a, b in (('M1', 'M0'), ('M1h', 'M0'), ('M3', 'M0'), ('M0', 'P0v'), ('M1', 'P0v'), ('M3', 'P0v')):
        dl = boot(sc[a]['ls'][m] - sc[b]['ls'][m]); db = boot(sc[a]['br'][m] - sc[b]['br'][m]); dc = boot(sc[a]['crps'][m] - sc[b]['crps'][m])
        comps[f'{a} vs {b}'] = dict(d_logscore=dl, d_brier=db, d_crps=dc, verdict_211='PASS' if dl[2] < 0 and db[2] < 0 else ('WORSE' if dl[1] > 0 or db[1] > 0 else 'FAIL'))
    res['comparisons'] = comps; return res
report = {}
v19 = np.array([d['ver'] >= 19 for d in data]); six = np.array([d['six'] for d in data]); allm = np.ones(len(data), bool)
for mode in ('lodo', 'walkforward'):
    for lab, m in (('all', allm), ('v19+', v19), ('six as-of', six), ('last 15 dates', np.isin(dts, dates[15:]))):
        report[f'{mode}:{lab}'] = evaluate(m, mode, f'{mode} {lab}')
json.dump(report, open(HERE / 'results_ci.json', 'w'), indent=1, default=float)
f3 = lambda v: '%+.4f [%+.4f,%+.4f]' % tuple(v)
for k, r in report.items():
    print('##', k, 'n', r['n'], 'dates', r['n_dates'])
    print('   levels: ' + ' | '.join('%s LS %.3f Br %.3f CRPS %.3f RMSE %.2f' % (a, v['ls'], v['br'], v['crps'], v['rmse']) for a, v in r['levels'].items()))
    for c, v in r['comparisons'].items(): print('   %-10s dLog %s dBrier %s dCRPS %s -> %s' % (c, f3(v['d_logscore']), f3(v['d_brier']), f3(v['d_crps']), v['verdict_211']))
