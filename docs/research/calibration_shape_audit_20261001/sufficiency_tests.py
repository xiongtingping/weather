"""Three sufficiency tests of the served P0 (user ranking, 2026-10-01):
 1. FULL VECTORS   Y _||_ {X_1..X_6 full bucket vectors} | P0 ?   low-capacity residual model on delta-logit p_k
 2. CONDITIONAL    Y _||_ state | P0 ?                          residual model: state x {sign(k-mode), |k-mode|, log p_k}
 3. TRAJECTORY     Y _||_ run trajectory (last 5 runs) | P0 ?    same residual form + continuous shift
All: log q_k = log p0_k + Phi_k . theta, L2, fitted by multinomial likelihood; walk-forward (train on earlier dates only) and LODO;
scored on the ladder with the production 1e-3 floor; date-block bootstrap; charter 211 = Brier AND LogScore 95% upper bound < 0."""
import json, math, pathlib, sys, collections
import numpy as np
from scipy import optimize
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))
from analyze import axis
from bayes_basket_target.materialization import floor_probabilities
from bayes_basket_target.native_ensemble_p0 import _interval_probability
rng = np.random.default_rng(20261001); FLOOR = 1e-3
SYS = ['ECMWF_IFS_ENS_OPEN_PF50', 'DWD_ICON_GLOBAL_EPS', 'NOAA_GEFS_05', 'ECCC_GEM_GLOBAL_ENS', 'ECMWF_AIFS_ENS_GLOBAL', 'NOAA_AIGEFS_025']
SOUTH = {'FACT', 'NZWN', 'SAEZ', 'SBGR'}
cap = {(r['station'], r['date']): r for r in json.load(open(HERE / 'capture.json'))['rows']}
dd = {(r['station'], r['date']): r for r in json.load(open(HERE / 'direction_data.json'))}
tj = {(r['station'], r['date']): r for r in json.load(open(HERE / 'trajectory_data.json'))}
rows = []
for key, r in cap.items():
    e = r['last']
    if 'rejected' in e or key not in dd: continue
    keys, lo, hi, rung = axis(e['inputs']['bucket_intervals_c_json']); iv = e['inputs']['bucket_intervals_c_json']
    p0 = np.array([float(e['probability'].get(k, 0)) for k in keys]); p0 /= p0.sum(); f, _ = floor_probabilities(dict(zip(keys, p0))); p0f = np.array([f[k] for k in keys]); p0f /= p0f.sum()
    hit = next(j for j, (l, h) in enumerate(zip(lo, hi)) if (l is None or r['truth_c'] >= l) and (h is None or r['truth_c'] < h))
    d = dd[key]; mode = int(np.argmax(p0)); K = len(keys)
    rows.append(dict(key=key, date=r['date'], station=r['station'], keys=keys, iv=iv, lo=lo, hi=hi, rung=rung, p0=p0f, logp0=np.log(p0f), hit=hit, mode=mode, K=K, truth=r['truth_c'],
                     mu=d['mu'], sd=d['sd'], e=d['e'], ver=int(e['posterior_version'].split('_v')[-1]), hours=d['hours'], unit=d['unit'], clearness_z=d['clearness_z'], rad=d.get('radiation_shift_c'),
                     sysprob=e['inputs'].get('native_system_probabilities_json'), weights=e['inputs'].get('system_weights_json') or {}, members=e.get('members') or {}, systems=d['systems'], traj=tj[key]['traj'] if key in tj else {}))
dates = sorted({r['date'] for r in rows})
st_mu = collections.defaultdict(list)
for r in rows: st_mu[r['station']].append(r['mu'])
st_mu = {s: float(np.mean(v)) for s, v in st_mu.items()}

def score(q, r):
    q = np.maximum(q, FLOOR); q = q / q.sum(); y = np.zeros(r['K']); y[r['hit']] = 1; return -math.log(q[r['hit']]), float(((q - y)**2).sum())
def fit_theta(Phis, logp0s, hits, lam):
    F = Phis[0].shape[1]
    def f(theta):
        nll = 0.0; g = np.zeros(F)
        for Phi, lp, h in zip(Phis, logp0s, hits):
            z = lp + Phi @ theta; z -= z.max(); q = np.exp(z); q /= q.sum(); nll -= math.log(q[h]); g += Phi.T @ (q - np.eye(len(q))[h])
        return nll + lam * (theta**2).sum(), g + 2 * lam * theta
    return optimize.minimize(f, np.zeros(F), jac=True, method='L-BFGS-B').x
def run_test(sub, phi_fn, label, lam=3.0, min_train=10, modes=('walkforward', 'lodo')):
    Phis = [phi_fn(r) for r in sub]; keep = [i for i, P in enumerate(Phis) if P is not None]; sub = [sub[i] for i in keep]; Phis = [Phis[i] for i in keep]
    F = Phis[0].shape[1]; dts = np.array([r['date'] for r in sub]); ud = sorted(set(dts)); out = {'label': label, 'n': len(sub), 'n_dates': len(ud), 'n_params': F}
    # standardise features across rows (per column) using all rows' scale (scale only, no information leak about y)
    allPhi = np.vstack(Phis); sc = allPhi.std(0) + 1e-9; Phis = [P / sc for P in Phis]
    for mode in modes:
        dL = np.full(len(sub), np.nan); dB = np.full(len(sub), np.nan)
        for i, dte in enumerate(ud):
            te = np.where(dts == dte)[0]
            if mode == 'lodo': tr = np.where(dts != dte)[0]
            else:
                if i < min_train: continue
                tr = np.where(np.isin(dts, ud[:i]))[0]
            theta = fit_theta([Phis[j] for j in tr], [sub[j]['logp0'] for j in tr], [sub[j]['hit'] for j in tr], lam)
            for j in te:
                z = sub[j]['logp0'] + Phis[j] @ theta; z -= z.max(); q = np.exp(z) / np.exp(z).sum(); l1, b1 = score(q, sub[j]); l0, b0 = score(sub[j]['p0'], sub[j]); dL[j] = l1 - l0; dB[j] = b1 - b0
        m = np.isfinite(dL)
        def boot(v, mask):
            g = dts[mask]; u = sorted(set(g)); idx = {x: np.where(g == x)[0] for x in u}; o = []
            for _ in range(2000):
                pick = rng.choice(u, len(u), replace=True); sel = np.concatenate([idx[x] for x in pick]); o.append(v[mask][sel].mean())
            return [float(v[mask].mean()), float(np.percentile(o, 2.5)), float(np.percentile(o, 97.5))]
        res = {}
        for name, mask in (('all', m), ('v19+', m & np.array([r['ver'] >= 19 for r in sub]))):
            if mask.sum() < 30: res[name] = {'n': int(mask.sum())}; continue
            l = boot(dL, mask); b = boot(dB, mask); res[name] = dict(n=int(mask.sum()), n_dates=len(set(dts[mask])), d_logscore=l, d_brier=b, verdict='PASS' if l[2] < 0 and b[2] < 0 else ('WORSE' if l[1] > 0 or b[1] > 0 else 'FAIL'))
        out[mode] = res
    return out
f3 = lambda v: '%+.4f [%+.4f,%+.4f]' % tuple(v)
def show(o):
    print('## %s  n=%d dates=%d params=%d' % (o['label'], o['n'], o['n_dates'], o['n_params']))
    for mode in ('walkforward', 'lodo'):
        if mode not in o: continue
        for sub, v in o[mode].items():
            if 'd_logscore' in v: print('   %-11s %-5s n=%4d d=%2d  dLog %s  dBrier %s -> %s' % (mode, sub, v['n'], v['n_dates'], f3(v['d_logscore']), f3(v['d_brier']), v['verdict']))
            else: print('   %-11s %-5s n=%d (too few)' % (mode, sub, v['n']))
report = {}
# ---------- 1. FULL VECTORS ----------
def logvec(v, keys): x = np.array([float(v.get(k, 0)) for k in keys]); x = np.maximum(x / max(x.sum(), 1e-12), FLOOR); return np.log(x / x.sum())
def phi_full(r, source='lineage', neighbours=True):
    vecs = {}
    if source == 'lineage':
        if not r['sysprob']: return None
        for s in SYS:
            if s in r['sysprob'] and r['weights'].get(s, 0) > 0: vecs[s] = logvec(r['sysprob'][s], r['keys'])
    else:
        for s in SYS:
            m = r['members'].get(s)
            if not m or r['weights'].get(s, 0) <= 0 or m['n'] < 10: continue
            vals = np.array(m['values'])
            if source == 'raw_shape_on_p0':   # pure shape: recentre members on the served centre
                vals = vals - vals.mean() + r['mu']
            elif source == 'raw_debiased':    # centre+shape: walk-forward station x system mean bias from earlier dates
                prior = [x for x in rows if x['station'] == r['station'] and x['date'] < r['date'] and x['members'].get(s) and x['weights'].get(s, 0) > 0]
                if len(prior) < 5: continue
                bias = float(np.mean([x['truth'] - np.mean(x['members'][s]['values']) for x in prior])); vals = vals + bias
            pr = _interval_probability(list(vals), {k: r['iv'][k] for k in r['keys']}); vecs[s] = logvec(pr, r['keys'])
    if len(vecs) < 2: return None
    K = r['K']; cols = []
    for s in SYS:
        if s in vecs:
            diff = vecs[s] - r['logp0']; cols.append(diff)
            if neighbours: cols.append(np.concatenate([[diff[0]], diff[:-1]])); cols.append(np.concatenate([diff[1:], [diff[-1]]]))
        else:
            cols.append(np.zeros(K)); 
            if neighbours: cols.append(np.zeros(K)); cols.append(np.zeros(K))
    # pooled version of the same (weighted mean of log-ratios) to give the model a low-dimensional route
    pooled = sum(r['weights'][s] * (vecs[s] - r['logp0']) for s in vecs) / sum(r['weights'][s] for s in vecs); cols.append(pooled)
    return np.column_stack(cols)
report['full_lineage_vectors'] = run_test(rows, lambda r: phi_full(r, 'lineage'), '1 FULL calibrated single-system vectors (lineage, 09-20+) incl. +-1 bucket', lam=3.0, min_train=4)
report['full_lineage_vectors_samebucket'] = run_test(rows, lambda r: phi_full(r, 'lineage', neighbours=False), '1 FULL lineage vectors, same-bucket log-ratio only', lam=3.0, min_train=4)
report['full_raw_shape'] = run_test(rows, lambda r: phi_full(r, 'raw_shape_on_p0'), '1 FULL raw-member KDE recentred on P0 (pure shape, all rows)', lam=3.0)
report['full_raw_debiased'] = run_test(rows, lambda r: phi_full(r, 'raw_debiased'), '1 FULL raw-member KDE + walk-forward station bias (centre+shape)', lam=3.0)
for k in ('full_lineage_vectors', 'full_lineage_vectors_samebucket', 'full_raw_shape', 'full_raw_debiased'): show(report[k])
# ---------- 2. CONDITIONAL CALIBRATION ----------
def states(r):
    return dict(clearness_z=r['clearness_z'] if r['clearness_z'] is not None else 0.0, rad=r['rad'] or 0.0, sd_rungs=r['sd'] / r['rung'], between=float(np.std([v['mean'] for v in r['systems'].values()])) if len(r['systems']) >= 2 else 0.0,
                within=float(np.mean([v['sd'] for v in r['systems'].values()])) if r['systems'] else 0.0, hours=r['hours'], unit_F=float(r['unit'] == 'F'), south=float(r['station'] in SOUTH),
                anomaly=r['mu'] - st_mu[r['station']], n_sys=float(len(r['weights'])))
STATE_KEYS = list(states(rows[0]))
def phi_state(r, which=None):
    s = states(r); K = r['K']; k = np.arange(K); sgn = np.sign(k - r['mode']); dist = np.abs(k - r['mode']); cols = []
    for name in (which or STATE_KEYS):
        v = s[name]; cols += [v * sgn, v * dist, v * r['logp0']]
    return np.column_stack(cols)
report['state_all'] = run_test(rows, lambda r: phi_state(r), '2 CONDITIONAL: all states x {sign(k-mode), |k-mode|, log p_k}', lam=3.0)
show(report['state_all'])
for name in STATE_KEYS:
    report[f'state_{name}'] = run_test(rows, lambda r, n=name: phi_state(r, [n]), f'2 CONDITIONAL: {name} only', lam=1.0); show(report[f'state_{name}'])
# descriptive conditional reliability: bins of p_k x state terciles -> hit rate (date-block CI)
def cond_reliability(name):
    vals = np.array([states(r)[name] for r in rows]); q = np.percentile(vals, [33.3, 66.7]); tab = {}
    for lo_, hi_, lab in ((0.2, 0.4, 'p 0.2-0.4'), (0.4, 0.7, 'p 0.4-0.7')):
        for t, (a, b) in enumerate(((-np.inf, q[0]), (q[0], q[1]), (q[1], np.inf))):
            hits = []; preds = []; dts_ = []
            for r, v in zip(rows, vals):
                if not (a <= v < b): continue
                for k in range(r['K']):
                    if lo_ <= r['p0'][k] < hi_: hits.append(float(r['hit'] == k)); preds.append(r['p0'][k]); dts_.append(r['date'])
            if len(hits) < 30: continue
            hits = np.array(hits); dts_ = np.array(dts_); u = sorted(set(dts_)); idx = {x: np.where(dts_ == x)[0] for x in u}; o = []
            for _ in range(1000):
                pick = rng.choice(u, len(u), replace=True); sel = np.concatenate([idx[x] for x in pick]); o.append(hits[sel].mean())
            tab[f'{lab} | {name} tercile {t+1}'] = dict(n=len(hits), mean_pred=float(np.mean(preds)), hit_rate=float(hits.mean()), ci=[float(np.percentile(o, 2.5)), float(np.percentile(o, 97.5))])
    return tab
report['conditional_reliability'] = {n: cond_reliability(n) for n in ('clearness_z', 'rad', 'sd_rungs', 'between', 'hours', 'anomaly')}
print('## conditional reliability (hit rate vs mean predicted, by state tercile)')
for n, tab in report['conditional_reliability'].items():
    for k, v in tab.items(): print('   %-38s n=%5d pred %.3f  hit %.3f [%.3f,%.3f]' % (k, v['n'], v['mean_pred'], v['hit_rate'], *v['ci']))
# ---------- 3. TRAJECTORY ----------
def traj_feats(r):
    f = {}; seqs = {}
    for s, seq in r['traj'].items():
        if len(seq) >= 3: seqs[s] = np.array([x['mean'] for x in seq])
    if not seqs: return None
    for s in ('ECMWF_IFS_ENS_OPEN_PF50', 'NOAA_GEFS_05'):
        if s in seqs:
            m = seqs[s]; t = np.arange(len(m)); slope = np.polyfit(t, m, 1)[0]; acc = (m[-1] - 2 * m[-2] + m[-3]) if len(m) >= 3 else 0.0; var = m.std(); diffs = np.diff(m)
            pers = float(np.mean(np.sign(diffs) == np.sign(diffs[-1]))) if len(diffs) and diffs[-1] != 0 else 0.0
            f[f'{s}_slope'] = slope; f[f'{s}_acc'] = acc; f[f'{s}_runsd'] = var; f[f'{s}_persist'] = pers; f[f'{s}_range'] = m.max() - m.min()
            for i in range(1, 4): f[f'{s}_lag{i}'] = (m[-1 - i] - r['mu']) if len(m) > i else 0.0
        else:
            for n_ in ('slope', 'acc', 'runsd', 'persist', 'range', 'lag1', 'lag2', 'lag3'): f[f'{s}_{n_}'] = 0.0
    # cross-model convergence: between-system sd now vs one run earlier (systems with >=2 runs)
    now = [r['traj'][s][-1]['mean'] for s in r['traj'] if len(r['traj'][s]) >= 2]; prev = [r['traj'][s][-2]['mean'] for s in r['traj'] if len(r['traj'][s]) >= 2]
    f['conv'] = (np.std(now) - np.std(prev)) if len(now) >= 2 else 0.0
    f['pool_slope'] = float(np.mean([np.polyfit(np.arange(len(m)), m, 1)[0] for m in seqs.values()]))
    return f
TK = None
def phi_traj(r):
    global TK
    f = traj_feats(r)
    if f is None: return None
    if TK is None: TK = sorted(f)
    K = r['K']; k = np.arange(K); sgn = np.sign(k - r['mode']); dist = np.abs(k - r['mode']); cols = []
    for n_ in TK: cols += [f[n_] * sgn, f[n_] * dist]
    return np.column_stack(cols)
report['trajectory'] = run_test(rows, phi_traj, '3 TRAJECTORY: last-5-run features (IFS, GEFS, pooled) x {sign(k-mode), |k-mode|}', lam=3.0); show(report['trajectory'])
# continuous: walk-forward ridge of e on trajectory features -> OOS R2
tr_rows = [r for r in rows if traj_feats(r) is not None]; Xt = np.array([[traj_feats(r)[k] for k in TK] for r in tr_rows]); et = np.array([r['e'] for r in tr_rows]); dt_ = np.array([r['date'] for r in tr_rows]); ud = sorted(set(dt_)); pred = np.full(len(et), np.nan)
for i, dte in enumerate(ud):
    if i < 10: continue
    te = dt_ == dte; tr = np.isin(dt_, ud[:i]); m, s = Xt[tr].mean(0), Xt[tr].std(0) + 1e-9; Z = np.hstack([(Xt[tr] - m) / s, np.ones((tr.sum(), 1))]); w = np.linalg.solve(Z.T @ Z + 5 * np.eye(Z.shape[1]), Z.T @ et[tr]); pred[te] = np.hstack([(Xt[te] - m) / s, np.ones((te.sum(), 1))]) @ w
ok = np.isfinite(pred); report['trajectory_walkforward_R2_on_e'] = float(1 - ((et[ok] - pred[ok])**2).mean() / ((et[ok] - et[ok].mean())**2).mean()); print('## trajectory walk-forward R2 on e: %.3f (n=%d)' % (report['trajectory_walkforward_R2_on_e'], ok.sum()))
json.dump(report, open(HERE / 'results_sufficiency.json', 'w'), indent=1, default=float)
