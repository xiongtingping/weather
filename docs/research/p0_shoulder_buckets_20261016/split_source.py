"""Same-mode morning rows, truth in a shoulder: does P0 hold the warm/cold-shoulder information somewhere its split loses?
Predictors: P0 split p(m+1)/(p(m+1)+p(m-1)); P0 mean offset inside the mode bucket (mu - c_m)/w; P0 median offset;
market split.  AUC and a 1-D logistic fit by walk-forward day (train >=10 days)."""
import gzip, json, sys, numpy as np, collections
sys.path.insert(0, '../obs_minus_forecast_station_direction_20261015'); import analyze as A
from bayes_basket_target.materialization import floor_probabilities
def auc(s, y):
    o = np.argsort(s); r = np.empty(len(s)); r[o] = np.arange(1, len(s) + 1)
    n1 = y.sum(); n0 = len(y) - n1; return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
rows = []
for l in gzip.open('../p0m_morning_side_20261016/joined.jsonl.gz', 'rt'):
    r = json.loads(l)
    if not r.get('p0') or not (7 <= r['local_hour'] < 13): continue
    order, c, w = A.centres(r['intervals'])
    if set(r['p0']['p']) != set(order) or set(r['market']['0']['q']) != set(order): continue
    p = np.array([float(r['p0']['p'][b]) for b in order]); p /= p.sum()
    q = np.array([float(r['market']['0']['q'][b]) for b in order]); q /= q.sum()
    m = int(np.argmax(p))
    if m != int(np.argmax(q)) or m == 0 or m == len(p) - 1: continue
    T = r['truth']; ti = next(i for i, b in enumerate(order) if (r['intervals'][b][0] is None or r['intervals'][b][0] <= T) and (r['intervals'][b][1] is None or T < r['intervals'][b][1]))
    if abs(ti - m) != 1: continue
    mu = (p * c).sum(); cdf = np.cumsum(p); med_i = int(np.searchsorted(cdf, 0.5))
    # median by linear interpolation inside its bucket
    lo_cdf = cdf[med_i - 1] if med_i > 0 else 0.0
    med = c[med_i] - w / 2 + w * (0.5 - lo_cdf) / max(p[med_i], 1e-9)
    rows.append(dict(day=r['day'], st=r['station'], y=float(ti == m + 1),
                     p0_split=p[m + 1] / (p[m + 1] + p[m - 1]), p0_mean_off=(mu - c[m]) / w, p0_median_off=(med - c[m]) / w,
                     mk_split=q[m + 1] / (q[m + 1] + q[m - 1]), mk_mean_off=((q * c).sum() - c[m]) / w,
                     p0_mode_p=p[m], runs=r['p0'].get('run_init_by_system')))
y = np.array([x['y'] for x in rows]); print('rows', len(rows), 'warm share', y.mean().round(3))
for k in ('p0_split', 'p0_mean_off', 'p0_median_off', 'mk_split', 'mk_mean_off'):
    s = np.array([x[k] for x in rows]); print(f'  AUC {k:14s} {auc(s, y):.3f}   corr with p0_split {np.corrcoef(s, [x["p0_split"] for x in rows])[0,1]:+.2f}')
# walk-forward logistic on each single predictor -> log loss
days = np.array([x['day'] for x in rows]); ds = sorted(set(days)); test = [d for d in ds if sum(e < d for e in ds) >= 10]
def wf(key):
    s = np.array([x[key] for x in rows]); ll = []
    for d in test:
        tr = days < d; te = days == d
        X = np.vstack([np.ones(tr.sum()), s[tr]]).T; b = np.zeros(2)
        for _ in range(50):
            pr = 1 / (1 + np.exp(-X @ b)); g = X.T @ (y[tr] - pr) - 0.1 * b; H = (X * (pr * (1 - pr))[:, None]).T @ X + 0.1 * np.eye(2); b += np.linalg.solve(H, g)
        pt = 1 / (1 + np.exp(-(b[0] + b[1] * s[te]))); ll += list(-(y[te] * np.log(pt) + (1 - y[te]) * np.log(1 - pt)))
    return np.mean(ll), len(ll)
base = np.mean([-np.log(0.5)] * 1)
for k in ('p0_split', 'p0_mean_off', 'p0_median_off', 'mk_split'):
    v, n = wf(k); print(f'  walk-forward logloss {k:14s} {v:.4f} (n{n}; coin 0.6931)')
# raw split used as-is
s = np.array([x['p0_split'] for x in rows]); te = np.isin(days, test)
print('  raw P0 split logloss on test', np.mean(-(y[te] * np.log(s[te]) + (1 - y[te]) * np.log(1 - s[te]))).round(4))
s = np.array([x['mk_split'] for x in rows]); print('  raw market split logloss on test', np.mean(-(y[te] * np.log(s[te]) + (1 - y[te]) * np.log(1 - s[te]))).round(4))
