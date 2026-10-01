"""Task 1+2: is the DIRECTION of the P0 centre error predictable from what the six systems say at P0 time?
Three classes: cold e<-0.5C / normal |e|<=0.5 / warm e>+0.5C.  Leave-one-date-out multinomial logistic (L2) per
feature family vs the training-fold class prior; multiclass log loss and Brier; date-block bootstrap on the
difference.  A-priori rules read as conditional frequencies with date-block CIs.  Market mean = reference arm only."""
import json, math, pathlib, collections, itertools
import numpy as np
from scipy import optimize
HERE = pathlib.Path(__file__).resolve().parent; rng = np.random.default_rng(20261001)
rows = json.load(open(HERE / 'direction_data.json'))
PHYS = ['ECMWF_IFS_ENS_OPEN_PF50', 'DWD_ICON_GLOBAL_EPS', 'NOAA_GEFS_05', 'ECCC_GEM_GLOBAL_ENS']; AI = ['ECMWF_AIFS_ENS_GLOBAL', 'NOAA_AIGEFS_025']
SHORT = {'ECMWF_IFS_ENS_OPEN_PF50': 'IFS', 'DWD_ICON_GLOBAL_EPS': 'ICON', 'NOAA_GEFS_05': 'GEFS', 'ECCC_GEM_GLOBAL_ENS': 'GEM', 'ECMWF_AIFS_ENS_GLOBAL': 'AIFS', 'NOAA_AIGEFS_025': 'AIGEFS'}
def cls(e): return 2 if e > 0.5 else 0 if e < -0.5 else 1

def features(r, six=False):
    S = PHYS + (AI if six else []); sy = r['systems']
    if any(s not in sy for s in S): return None
    mu = {s: sy[s]['mean'] for s in S}; pool = float(np.mean(list(mu.values()))); f = {}
    for s in S: f[f'd_{SHORT[s]}'] = mu[s] - pool
    for a, b in itertools.combinations(S, 2): f[f'{SHORT[a]}-{SHORT[b]}'] = mu[a] - mu[b]
    f['between_sd'] = float(np.std(list(mu.values()))); f['within_sd'] = float(np.mean([sy[s]['sd'] for s in S]))
    f['rawpool_minus_mu'] = pool - r['mu']
    order = sorted(S, key=lambda s: mu[s]); f['_rank'] = '<'.join(SHORT[s] for s in order)
    f['euro_minus_na'] = (mu['ECMWF_IFS_ENS_OPEN_PF50'] + mu['DWD_ICON_GLOBAL_EPS']) / 2 - (mu['NOAA_GEFS_05'] + mu['ECCC_GEM_GLOBAL_ENS']) / 2
    if six: f['ai_minus_phys'] = float(np.mean([mu[s] for s in AI]) - np.mean([mu[s] for s in PHYS]))
    tr = [sy[s]['mean'] - sy[s]['prev_mean'] for s in S if sy[s].get('prev_mean') is not None]
    f['pool_trend'] = float(np.mean(tr)) if tr else 0.0; f['trend_missing'] = float(not tr)
    for s in S: f[f'tr_{SHORT[s]}'] = (sy[s]['mean'] - sy[s]['prev_mean']) if sy[s].get('prev_mean') is not None else 0.0
    f['hours'] = r['hours']; f['unit_F'] = float(r['unit'] == 'F'); f['clearness_z'] = r['clearness_z'] if r['clearness_z'] is not None else 0.0
    f['clearness_missing'] = float(r['clearness_z'] is None); f['sd_served'] = r['sd']; f['mu_level'] = r['mu']
    f['_market_dir'] = None if r['market_mean'] is None else r['market_mean'] - r['mu']
    return f
FAMILIES = {
    'F1 structure (positions, pairs, camps, dispersion)': lambda k: k.startswith('d_') or '-' in k or k in ('between_sd', 'within_sd', 'euro_minus_na', 'ai_minus_phys', 'rawpool_minus_mu'),
    'F2 F1 + run-to-run trend': lambda k: FAMILIES['F1 structure (positions, pairs, camps, dispersion)'](k) or k.startswith('tr_') or k in ('pool_trend', 'trend_missing'),
    'F3 F2 + context (hours, unit, clearness, sd, level)': lambda k: FAMILIES['F2 F1 + run-to-run trend'](k) or k in ('hours', 'unit_F', 'clearness_z', 'clearness_missing', 'sd_served', 'mu_level'),
    'F4 F3 + station one-hot': lambda k: FAMILIES['F3 F2 + context (hours, unit, clearness, sd, level)'](k) or k.startswith('st_'),
    'R market direction (reference only)': lambda k: k == 'market_dir',
}

def fit_mlogit(X, y, l2=1.0):
    n, p = X.shape; K = 3
    def nll(w):
        W = w.reshape(K, p + 1); Z = X @ W[:, :p].T + W[:, p]; Z -= Z.max(1, keepdims=True); L = Z - np.log(np.exp(Z).sum(1, keepdims=True))
        return -L[np.arange(n), y].sum() + 0.5 * l2 * (W[:, :p]**2).sum()
    def grad(w):
        W = w.reshape(K, p + 1); Z = X @ W[:, :p].T + W[:, p]; Z -= Z.max(1, keepdims=True); P = np.exp(Z); P /= P.sum(1, keepdims=True)
        Y = np.zeros_like(P); Y[np.arange(n), y] = 1; G = (P - Y); gw = G.T @ X + l2 * W[:, :p]; gb = G.sum(0)
        return np.hstack([gw, gb[:, None]]).ravel()
    w = optimize.minimize(nll, np.zeros(K * (p + 1)), jac=grad, method='L-BFGS-B').x
    return w.reshape(K, p + 1)
def predict(W, X):
    p = X.shape[1]; Z = X @ W[:, :p].T + W[:, p]; Z -= Z.max(1, keepdims=True); P = np.exp(Z); return P / P.sum(1, keepdims=True)

def evaluate(data, keys, label, l2=1.0):
    dates = sorted({d['date'] for d in data}); y = np.array([d['y'] for d in data]); X = np.array([[d['f'][k] for k in keys] for d in data], float)
    P = np.zeros((len(data), 3)); B = np.zeros((len(data), 3))
    for dte in dates:
        te = np.array([d['date'] == dte for d in data]); tr = ~te
        mu_, sd_ = X[tr].mean(0), X[tr].std(0) + 1e-9; Xs = (X - mu_) / sd_
        prior = np.bincount(y[tr], minlength=3) / tr.sum(); B[te] = prior
        W = fit_mlogit(Xs[tr], y[tr], l2); P[te] = predict(W, Xs[te])
    P = np.clip(P, 1e-6, 1); P /= P.sum(1, keepdims=True)
    ll = -np.log(P[np.arange(len(y)), y]); llb = -np.log(B[np.arange(len(y)), y])
    Y = np.zeros_like(P); Y[np.arange(len(y)), y] = 1; br = ((P - Y)**2).sum(1); brb = ((B - Y)**2).sum(1)
    dts = np.array([d['date'] for d in data]); ud = sorted(set(dts))
    def boot(v):
        idx = {u: np.where(dts == u)[0] for u in ud}; out = []
        for _ in range(2000):
            pick = rng.choice(ud, len(ud), replace=True); sel = np.concatenate([idx[u] for u in pick]); out.append(v[sel].mean())
        return [float(v.mean()), float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]
    nn = y != 1; acc = float((P[nn].argmax(1) == y[nn]).mean()) if nn.any() else None
    # directional accuracy among warm/cold rows: model picks warm if P(warm)>P(cold)
    dir_acc = float(((P[nn, 2] > P[nn, 0]) == (y[nn] == 2)).mean()) if nn.any() else None
    base_dir = float(max((y[nn] == 2).mean(), (y[nn] == 0).mean())) if nn.any() else None
    return dict(label=label, n=len(y), n_dates=len(ud), n_features=len(keys), class_freq=np.bincount(y, minlength=3).tolist(),
                logloss_model=float(ll.mean()), logloss_prior=float(llb.mean()), d_logloss=boot(ll - llb), d_brier=boot(br - brb),
                direction_acc_on_warm_cold=dir_acc, majority_direction_acc=base_dir, n_warm_cold=int(nn.sum()))

def rule(data, cond, label):
    sel = [d for d in data if cond(d)]
    if len(sel) < 20: return dict(label=label, n=len(sel))
    y = np.array([d['y'] for d in sel]); dts = np.array([d['date'] for d in sel]); ud = sorted(set(dts)); idx = {u: np.where(dts == u)[0] for u in ud}
    def boot(v):
        out = []
        for _ in range(2000):
            pick = rng.choice(ud, len(ud), replace=True); s = np.concatenate([idx[u] for u in pick]); out.append(v[s].mean())
        return [float(v.mean()), float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]
    allp = np.array([d['y'] for d in data])
    return dict(label=label, n=len(sel), n_dates=len(ud), p_warm=boot((y == 2).astype(float)), p_cold=boot((y == 0).astype(float)),
                base_warm=float((allp == 2).mean()), base_cold=float((allp == 0).mean()), mean_e=boot(np.array([d['e'] for d in sel])))

def main():
    rep = {}
    for six in (False, True):
        data = []
        for r in rows:
            f = features(r, six)
            if f is None: continue
            stations = sorted({x['station'] for x in rows})
            for s in stations: f[f'st_{s}'] = float(r['station'] == s)
            if f['_market_dir'] is not None: f['market_dir'] = f['_market_dir']
            data.append(dict(date=r['date'], station=r['station'], e=r['e'], y=cls(r['e']), f=f, market=f['_market_dir']))
        tag = 'six_systems' if six else 'four_physics_systems'; rep[tag] = {'n': len(data), 'n_dates': len({d['date'] for d in data}), 'class_freq_cold_normal_warm': np.bincount([d['y'] for d in data], minlength=3).tolist(),
                                                                       'mean_e': float(np.mean([d['e'] for d in data])), 'rank_patterns_top': collections.Counter(d['f']['_rank'] for d in data).most_common(6)}
        allkeys = [k for k in data[0]['f'] if not k.startswith('_')]
        res = {}
        for fam, pred in FAMILIES.items():
            keys = [k for k in allkeys if pred(k)]
            sub = data if fam[0] != 'R' else [d for d in data if d['market'] is not None]
            if not keys or len(sub) < 100: continue
            res[fam] = evaluate(sub, keys, fam, l2=1.0 if fam[0] != 'R' else 0.1)
        rep[tag]['lodo'] = res
        # continuous: LODO ridge of e on F3 features, out-of-sample R^2
        keys = [k for k in allkeys if FAMILIES['F3 F2 + context (hours, unit, clearness, sd, level)'](k)]
        X = np.array([[d['f'][k] for k in keys] for d in data]); e = np.array([d['e'] for d in data]); dts = np.array([d['date'] for d in data]); pred = np.zeros(len(e))
        for dte in sorted(set(dts)):
            te = dts == dte; tr = ~te; m, s = X[tr].mean(0), X[tr].std(0) + 1e-9; Xs = (X - m) / s
            A = np.hstack([Xs[tr], np.ones((tr.sum(), 1))]); w = np.linalg.solve(A.T @ A + 5.0 * np.eye(A.shape[1]), A.T @ e[tr]); pred[te] = np.hstack([Xs[te], np.ones((te.sum(), 1))]) @ w
        rep[tag]['lodo_ridge_R2_on_e'] = float(1 - ((e - pred)**2).mean() / ((e - e.mean())**2).mean())
        # a-priori rules
        R = []
        g = lambda d, k: d['f'].get(k, 0.0)
        R.append(rule(data, lambda d: g(d, 'IFS-GEFS') >= 0.5, 'IFS above GEFS by >=0.5C'))
        R.append(rule(data, lambda d: g(d, 'IFS-GEFS') <= -0.5, 'IFS below GEFS by >=0.5C'))
        R.append(rule(data, lambda d: g(d, 'euro_minus_na') >= 0.5, 'European pair above N.American pair by >=0.5C'))
        R.append(rule(data, lambda d: g(d, 'euro_minus_na') <= -0.5, 'European pair below N.American pair by >=0.5C'))
        R.append(rule(data, lambda d: g(d, 'd_IFS') >= 0.5, 'IFS warmest, >=0.5C above raw pool'))
        R.append(rule(data, lambda d: g(d, 'd_IFS') <= -0.5, 'IFS coldest, >=0.5C below raw pool'))
        R.append(rule(data, lambda d: g(d, 'pool_trend') >= 0.3, 'pool warmed >=0.3C since previous run'))
        R.append(rule(data, lambda d: g(d, 'pool_trend') <= -0.3, 'pool cooled >=0.3C since previous run'))
        R.append(rule(data, lambda d: g(d, 'rawpool_minus_mu') >= 0.5, 'raw member pool >=0.5C above served centre'))
        R.append(rule(data, lambda d: g(d, 'rawpool_minus_mu') <= -0.5, 'raw member pool >=0.5C below served centre'))
        R.append(rule(data, lambda d: g(d, 'between_sd') >= 0.8, 'between-system sd >=0.8C (disagreement day)'))
        if six:
            R.append(rule(data, lambda d: g(d, 'ai_minus_phys') >= 0.5, 'AI camp (AIFS,AIGEFS) >=0.5C above physics camp'))
            R.append(rule(data, lambda d: g(d, 'ai_minus_phys') <= -0.5, 'AI camp >=0.5C below physics camp'))
        R.append(rule([d for d in data if d['market'] is not None], lambda d: d['market'] >= 0.5, 'REFERENCE: market mean >=0.5C above P0 centre'))
        R.append(rule([d for d in data if d['market'] is not None], lambda d: d['market'] <= -0.5, 'REFERENCE: market mean >=0.5C below P0 centre'))
        rep[tag]['rules'] = R
    json.dump(rep, open(HERE / 'results_direction.json', 'w'), indent=1, default=float)
    f3 = lambda v: '%+.4f [%+.4f,%+.4f]' % tuple(v)
    for tag, t in rep.items():
        print('##', tag, 'n', t['n'], 'dates', t['n_dates'], 'classes cold/normal/warm', t['class_freq_cold_normal_warm'], 'mean e %.3f' % t['mean_e'], 'LODO ridge R2 on e: %.3f' % t['lodo_ridge_R2_on_e'])
        print('   rank patterns:', t['rank_patterns_top'])
        for fam, e in t['lodo'].items():
            print('   %-55s feats=%2d  logloss %.4f vs prior %.4f  d=%s  dBrier=%s  dir.acc warm/cold %.2f (majority %.2f, n=%d)' % (fam, e['n_features'], e['logloss_model'], e['logloss_prior'], f3(e['d_logloss']), f3(e['d_brier']), e['direction_acc_on_warm_cold'], e['majority_direction_acc'], e['n_warm_cold']))
        print('   rules (P warm / P cold vs base):')
        for R in t['rules']:
            if 'p_warm' not in R: print('      %-50s n=%d (too few)' % (R['label'], R['n'])); continue
            print('      %-50s n=%4d d=%2d  P(warm) %s  P(cold) %s  base %.2f/%.2f  mean e %s' % (R['label'], R['n'], R['n_dates'], f3(R['p_warm']), f3(R['p_cold']), R['base_warm'], R['base_cold'], f3(R['mean_e'])))
if __name__ == '__main__': main()
