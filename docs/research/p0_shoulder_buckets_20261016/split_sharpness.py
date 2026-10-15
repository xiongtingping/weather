"""Is the shoulder split under-confident at each step?  logit(y) = a + b * logit(split); b > 1 = the step ranks right but says it too softly.
Day-block bootstrap; plus a walk-forward recalibration of the served split (fit a, b on earlier days) -> split log loss."""
import gzip, json, numpy as np
rows = json.load(gzip.open('pipeline_rows.json.gz', 'rt'))
sh = [x for x in rows if abs(x['ti'] - x['m']) == 1 and 0 < x['m'] < len(x['served']) - 1]
def pool(x, k):
    ws = {s: float(x['weights'].get(s, 0.0)) for s in x['systems']}; t = sum(ws.values()) or 1
    return sum(ws[s] * np.array(x['systems'][s]['vecs'][k]) for s in ws) / t
def split(v, m): return (v[m + 1] + 1e-12) / (v[m + 1] + v[m - 1] + 2e-12)
def lg(p): p = np.clip(p, 1e-3, 1 - 1e-3); return np.log(p / (1 - p))
def fit(z, y, ridge=1e-3):
    b = np.zeros(2); X = np.vstack([np.ones_like(z), z]).T
    for _ in range(60):
        p = 1 / (1 + np.exp(-X @ b)); g = X.T @ (y - p) - ridge * b; H = (X * (p * (1 - p))[:, None]).T @ X + ridge * np.eye(2); b += np.linalg.solve(H, g)
    return b
y = np.array([float(x['ti'] == x['m'] + 1) for x in sh]); days = np.array([x['day'] for x in sh]); ud = sorted(set(days))
cands = {'pool C_bias': lambda x: pool(x, 'C_bias'), 'pool D_bias_spread': lambda x: pool(x, 'D_bias_spread'),
         'pool B_raw_kernel': lambda x: pool(x, 'B_raw_kernel'), 'served P0': lambda x: np.array(x['served'])}
for s in sorted({s for x in rows for s in x['systems']}):
    cands[f'{s} D_bias_spread'] = (lambda x, s=s: np.array(x['systems'][s]['vecs']['D_bias_spread']) if s in x['systems'] else None)
    cands[f'{s} C_bias'] = (lambda x, s=s: np.array(x['systems'][s]['vecs']['C_bias']) if s in x['systems'] else None)
rng = np.random.default_rng(7)
out = {}
for name, f in cands.items():
    vs = [f(x) for x in sh]; ok = np.array([v is not None for v in vs])
    z = np.array([lg(split(v, x['m'])) if v is not None else 0 for v, x in zip(vs, sh)])[ok]; yy = y[ok]; dd = days[ok]
    a, b = fit(z, yy)
    bs = []
    for _ in range(500):
        pick = rng.choice(ud, len(ud)); idx = np.concatenate([np.where(dd == d)[0] for d in pick])
        bs.append(fit(z[idx], yy[idx])[1])
    lo, hi = np.quantile(bs, [0.025, 0.975])
    out[name] = {'a': a, 'b': b, 'b_lo': lo, 'b_hi': hi, 'n': int(ok.sum())}
    print(f'{name:40s} n{ok.sum():5d}  slope b {b:5.2f} [{lo:4.2f},{hi:4.2f}]  intercept {a:+.2f}')
# walk-forward recalibration of served split
z = np.array([lg(split(np.array(x['served']), x['m'])) for x in sh]); ll0 = []; ll1 = []
for d in ud:
    tr = days < d; te = days == d
    if len(set(days[tr])) < 10: continue
    a, b = fit(z[tr], y[tr]); p1 = 1 / (1 + np.exp(-(a + b * z[te]))); p0 = 1 / (1 + np.exp(-z[te]))
    ll0 += list(-(y[te] * np.log(p0) + (1 - y[te]) * np.log(1 - p0))); ll1 += list(-(y[te] * np.log(p1) + (1 - y[te]) * np.log(1 - p1)))
print(f'served split walk-forward: raw logloss {np.mean(ll0):.4f}  recalibrated {np.mean(ll1):.4f}  (n{len(ll0)}; coin 0.6931)')
out['served_walk_forward'] = {'raw': float(np.mean(ll0)), 'recal': float(np.mean(ll1)), 'n': len(ll0)}
json.dump(out, open('readings_split_sharpness.json', 'w'), indent=1, default=float)
