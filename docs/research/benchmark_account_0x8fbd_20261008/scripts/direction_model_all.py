"""Round 22 part 2: our own direction model, fitted on every station-day
(not on the cluster's picks), cross-fitted by date half.

Instances: AIFS ENS availability + 1 h on every station-day (all_instances.json).
Features (z, + = warmer): ECMWF momentum M; ICON daytime shortwave; ICON
relative humidity (-); dew-point consensus (+); ECMWF 10 m wind (-) -- the
physics from Open-Meteo previous runs usable at t.  Target: direction excess
of the warm side, D - E_q[D], against the market mode at t.  OLS with an
intercept, fitted on one half and scored on the other.  Then the fitted model
on the cluster's events: agreement, its skill there, their skill split.
"""
import json, collections, random, statistics, sys
from datetime import datetime, timezone, timedelta
B = sys.argv[1]
random.seed(2222)
I = json.load(open(f'{B}/all_instances.json'))
PV = json.load(open(f'{B}/prev_vars.json'))
FEAT = [('M', None), ('icon_rad', ('icon_global', 'shortwave_radiation', 1)), ('icon_rh', ('icon_global', 'relative_humidity_2m', -1)),
        ('dew_cons', ('consensus', 'dew_point_2m', 1)), ('ecmwf_wind', ('ecmwf_ifs025', 'wind_speed_10m', -1))]
MODELS = ['icon_global', 'ecmwf_ifs025', 'gfs_global', 'ukmo_global_deterministic_10km', 'jma_gsm', 'meteofrance_arpege_world']
daily = {}
for key, v in PV.items():
    if 'time' not in v:
        continue
    for var in ('shortwave_radiation', 'relative_humidity_2m', 'dew_point_2m', 'wind_speed_10m'):
        acc = collections.defaultdict(lambda: [[], []])
        for i, t in enumerate(v['time']):
            if 9 <= int(t[11:13]) <= 16:
                for j, lab in enumerate(('d1', 'd2')):
                    x = (v[var][lab] or [None] * len(v['time']))[i]
                    if x is not None:
                        acc[t[:10]][j].append(x)
        daily[(key, var)] = {d: tuple(statistics.mean(a) if len(a) == 8 else None for a in pair) for d, pair in acc.items()}


def zval(st, ed, model, var, lag):
    dd = daily.get((f'{st}|{model}', var))
    if not dd or ed not in dd or dd[ed][lag] is None:
        return None
    others = [x[lag] for d, x in dd.items() if d != ed and x[lag] is not None]
    if len(others) < 6:
        return None
    sd = statistics.pstdev(others)
    return (dd[ed][lag] - statistics.mean(others)) / sd if sd > 0 else None


def features(st, ed, ts, M):
    D = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc)
    lag = 0 if ts >= (D - timedelta(days=1) + timedelta(hours=8)).timestamp() else 1 if ts >= (D - timedelta(days=2) + timedelta(hours=8)).timestamp() else None
    f = {'M': (M or 0.0) / 2.0}
    for name, spec in FEAT[1:]:
        model, var, sg = spec
        if lag is None:
            f[name] = 0.0; continue
        if model == 'consensus':
            zs = [zval(st, ed, m, var, lag) for m in MODELS]
            zs = [z for z in zs if z is not None]
            f[name] = sg * statistics.mean(zs) if len(zs) >= 3 else 0.0
        else:
            z = zval(st, ed, model, var, lag)
            f[name] = sg * z if z is not None else 0.0
    return f


def target(q, truth, side=1):
    mode = max(q, key=q.get)
    sd = lambda o: (o - mode) * side
    D = 1 if sd(truth) > 0 else -1 if sd(truth) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in q.items()), mode


rows = []
for x in I:
    q = {int(k): v for k, v in x['q'].items()}
    y, mode = target(q, x['truth'])
    f = features(x['st'], x['ed'], x['t'], x['M'])
    rows.append(dict(x, qd=q, y=y, mode=mode, f=f, half='H1' if x['ed'] < '2026-09-30' else 'H2'))
NAMES = ['const'] + [n for n, _ in FEAT]


def X(r):
    return [1.0] + [r['f'][n] for n, _ in FEAT]


def ols(rs):
    k = len(NAMES)
    A = [[0.0] * k for _ in range(k)]; v = [0.0] * k
    for r in rs:
        x = X(r)
        for i in range(k):
            v[i] += x[i] * r['y']
            for j in range(k):
                A[i][j] += x[i] * x[j]
    for i in range(k): A[i][i] += 1e-6
    M = [A[i] + [v[i]] for i in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda rr: abs(M[rr][c])); M[c], M[p] = M[p], M[c]
        for rr in range(k):
            if rr != c:
                fct = M[rr][c] / M[c][c]
                M[rr] = [M[rr][j] - fct * M[c][j] for j in range(k + 1)]
    return [M[i][k] / M[i][i] for i in range(k)]


def pred(beta, r):
    return sum(b * x for b, x in zip(beta[1:], X(r)[1:]))  # slope part only: the intercept is the market's average bias


fits = {h: ols([r for r in rows if r['half'] == h]) for h in ('H1', 'H2')}
full = ols(rows)
print('coefficients (full / H1 / H2):')
for i, n in enumerate(NAMES):
    print(f'   {n:12s} {full[i]:+.3f}  {fits["H1"][i]:+.3f}  {fits["H2"][i]:+.3f}')
for r in rows:
    other = 'H2' if r['half'] == 'H1' else 'H1'
    r['p_oos'] = pred(fits[other], r)


def evaluate(r, side):
    y, mode = target(r['qd'], r['truth'], side)
    nb = str(mode + side)
    out = dict(score=y)
    if nb in r['quotes']:
        bid, ask = r['quotes'][nb]
        hit = 1.0 if r['truth'] == mode + side else 0.0
        out['taker'] = hit - ask
        out['maker'] = hit - min(bid + 0.01, ask)
    return out


def boot(vals_by, n=400):
    keys = list(vals_by); outv = []
    for _ in range(n):
        s = [v for k in (random.choice(keys) for _ in keys) for v in vals_by[k]]
        outv.append(sum(s) / len(s))
    outv.sort()
    return outv[int(.025 * n)], outv[int(.975 * n) - 1]


def report(label, sel):
    ev = [(r, evaluate(r, 1 if r['_p'] > 0 else -1)) for r in sel]
    parts = []
    for key, sc in (('score', 1), ('taker', 100), ('maker', 100)):
        G = collections.defaultdict(list)
        for r, e in ev:
            if key in e: G[(r['st'], r['ed'])].append(e[key])
        vals = [v for g in G.values() for v in g]
        if not vals: continue
        lo, hi = boot(G)
        h1 = [e[key] for r, e in ev if key in e and r['half'] == 'H1']; h2 = [e[key] for r, e in ev if key in e and r['half'] == 'H2']
        parts.append(f'{key} {sc*sum(vals)/len(vals):+.3f} [{sc*lo:+.3f},{sc*hi:+.3f}] H1/H2 {sc*sum(h1)/max(1,len(h1)):+.3f}/{sc*sum(h2)/max(1,len(h2)):+.3f}')
    print(f'   {label:40s} n={len(sel):5d}  ' + ' | '.join(parts))


print('\n=== out-of-sample on every station-day ===')
ps = sorted(abs(r['p_oos']) for r in rows)
for frac in (1.0, 0.5, 0.25, 0.1):
    cut = ps[int((1 - frac) * len(ps))] if frac < 1 else 0.0
    sel = [dict(r, _p=r['p_oos']) for r in rows if abs(r['p_oos']) >= cut and r['p_oos'] != 0]
    report(f'model, top {int(frac*100)}% |prediction|', sel)
for frac in (0.5, 0.25):
    msorted = sorted(abs(r['M'] or 0) for r in rows)
    cut = msorted[int((1 - frac) * len(msorted))]
    report(f'M only, top {int(frac*100)}% |M|', [dict(r, _p=r['M']) for r in rows if r['M'] and abs(r['M']) >= cut])

# ---- the fitted model on the cluster's events
R = json.load(open(f'{B}/direction_rows.json'))
Mm = json.load(open(f'{B}/momentum_M.json'))
def dscore(r, side):
    q = {int(k): v for k, v in r['q'].items()}
    sd = lambda o: (o - r['mode']) * side
    D = 1 if sd(r['truth']) > 0 else -1 if sd(r['truth']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in q.items())
ev = []
for r in R:
    M = Mm.get(f'{r["station"]}|{r["ed"]}|{r["ts"]}')
    f = features(r['station'], r['ed'], r['ts'], M)
    p = sum(b * x for b, x in zip(full[1:], [f[n] for n, _ in FEAT]))
    ev.append((r, p, M))
print('\n=== the fitted model (all station-days) on the cluster events ===')
ps = sorted(abs(p) for r, p, M in ev)
for frac in (1.0, 0.5, 0.25):
    cut = ps[int((1 - frac) * len(ps))] if frac < 1 else 1e-9
    sel = [(r, p) for r, p, M in ev if abs(p) >= cut]
    ag = sum(1 for r, p in sel if (1 if p > 0 else -1) == r['s']) / len(sel)
    msk = sum(dscore(r, 1 if p > 0 else -1) for r, p in sel) / len(sel)
    A = [r for r, p in sel if (1 if p > 0 else -1) == r['s']]; Dd = [r for r, p in sel if (1 if p > 0 else -1) != r['s']]
    print(f'   top {int(frac*100):3d}%: n={len(sel):4d} agree {ag:.3f}  model skill {msk:+.3f}  theirs {sum(dscore(r, r["s"]) for r, p in sel)/len(sel):+.3f}  theirs agree/disagree {sum(dscore(r, r["s"]) for r in A)/max(1,len(A)):+.3f}/{sum(dscore(r, r["s"]) for r in Dd)/max(1,len(Dd)):+.3f}')
quiet = [(r, p) for r, p, M in ev if M is None or abs(M) < 0.5]
ag = sum(1 for r, p in quiet if p != 0 and (1 if p > 0 else -1) == r['s']) / max(1, sum(1 for r, p in quiet if p != 0))
print(f'   quiet events: n={len(quiet)} agree {ag:.3f}  model skill {sum(dscore(r, 1 if p > 0 else -1) for r, p in quiet if p != 0)/max(1,len(quiet)):+.3f}  theirs {sum(dscore(r, r["s"]) for r, p in quiet)/len(quiet):+.3f}')
json.dump({'coef_full': dict(zip(NAMES, full)), 'coef_H1': dict(zip(NAMES, fits['H1'])), 'coef_H2': dict(zip(NAMES, fits['H2']))}, open(f'{B}/direction_model_coef.json', 'w'))

print('\n=== feature sets and the cluster-like selection (cross-fitted, every station-day) ===')
def fit_pred(names):
    global NAMES
    def Xs(r): return [1.0] + [r['f'][n] for n in names]
    def ols2(rs):
        k = len(names) + 1
        A = [[0.0] * k for _ in range(k)]; v = [0.0] * k
        for r in rs:
            x = Xs(r)
            for i in range(k):
                v[i] += x[i] * r['y']
                for j in range(k): A[i][j] += x[i] * x[j]
        for i in range(k): A[i][i] += 1e-6
        Mx = [A[i] + [v[i]] for i in range(k)]
        for c in range(k):
            p = max(range(c, k), key=lambda rr: abs(Mx[rr][c])); Mx[c], Mx[p] = Mx[p], Mx[c]
            for rr in range(k):
                if rr != c:
                    fct = Mx[rr][c] / Mx[c][c]; Mx[rr] = [Mx[rr][j] - fct * Mx[c][j] for j in range(k + 1)]
        return [Mx[i][k] / Mx[i][i] for i in range(k)]
    fs = {h: ols2([r for r in rows if r['half'] == h]) for h in ('H1', 'H2')}
    for r in rows:
        b = fs['H2' if r['half'] == 'H1' else 'H1']
        r['_p'] = sum(bb * x for bb, x in zip(b[1:], Xs(r)[1:]))
    return fs
def selection(r):
    qm = max(r['qd'].values())
    mode = max(r['qd'], key=r['qd'].get)
    side = 1 if r['_p'] > 0 else -1
    nbq = r['qd'].get(mode + side)
    return qm < 0.40 and nbq is not None and 0.10 <= nbq <= 0.45
for names in (['M'], ['M', 'icon_rad'], ['M', 'icon_rad', 'dew_cons', 'ecmwf_wind'], ['M', 'icon_rad', 'icon_rh', 'dew_cons', 'ecmwf_wind']):
    fs = fit_pred(names)
    lab = '+'.join(names)
    allp = sorted(abs(r['_p']) for r in rows)
    half_cut = allp[len(allp) // 2]
    report(f'{lab}: top 50%', [r for r in rows if abs(r['_p']) >= half_cut and r['_p'] != 0])
    report(f'{lab}: cluster-like selection', [r for r in rows if r['_p'] != 0 and selection(r)])
    report(f'{lab}: selection & top 50%', [r for r in rows if abs(r['_p']) >= half_cut and r['_p'] != 0 and selection(r)])

print('\n=== the four-feature model (M, icon_rad, dew_cons, ecmwf_wind) ===')
F4 = ['M', 'icon_rad', 'dew_cons', 'ecmwf_wind']
fs4 = fit_pred(F4)
def ols_names(rs, names):
    global rows
    saved = [r.get('_p') for r in rows]
    return None
# coefficients by half and full (refit on all rows)
def ols4(rs):
    k = len(F4) + 1
    A = [[0.0] * k for _ in range(k)]; v = [0.0] * k
    for r in rs:
        x = [1.0] + [r['f'][n] for n in F4]
        for i in range(k):
            v[i] += x[i] * r['y']
            for j in range(k): A[i][j] += x[i] * x[j]
    for i in range(k): A[i][i] += 1e-6
    Mx = [A[i] + [v[i]] for i in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda rr: abs(Mx[rr][c])); Mx[c], Mx[p] = Mx[p], Mx[c]
        for rr in range(k):
            if rr != c:
                fct = Mx[rr][c] / Mx[c][c]; Mx[rr] = [Mx[rr][j] - fct * Mx[c][j] for j in range(k + 1)]
    return [Mx[i][k] / Mx[i][i] for i in range(k)]
b4 = ols4(rows)
print('   coef full / H1 / H2:')
for i, n in enumerate(['const'] + F4):
    print(f'     {n:12s} {b4[i]:+.3f}  {fs4["H1"][i]:+.3f}  {fs4["H2"][i]:+.3f}')
ev4 = []
for r in R:
    M = Mm.get(f'{r["station"]}|{r["ed"]}|{r["ts"]}')
    f = features(r['station'], r['ed'], r['ts'], M)
    # out-of-half coefficients for the cluster events too
    b = fs4['H2' if r['ed'] < '2026-09-30' else 'H1']
    p = sum(bb * f[n] for bb, n in zip(b[1:], F4))
    ev4.append((r, p, M))
for frac in (1.0, 0.5):
    ps = sorted(abs(p) for r, p, M in ev4)
    cut = ps[int((1 - frac) * len(ps))] if frac < 1 else 1e-9
    sel = [(r, p) for r, p, M in ev4 if abs(p) >= cut]
    ag = sum(1 for r, p in sel if (1 if p > 0 else -1) == r['s']) / len(sel)
    msk = sum(dscore(r, 1 if p > 0 else -1) for r, p in sel) / len(sel)
    A = [r for r, p in sel if (1 if p > 0 else -1) == r['s']]; Dd = [r for r, p in sel if (1 if p > 0 else -1) != r['s']]
    print(f'   cluster events top {int(frac*100)}%: n={len(sel)} agree {ag:.3f} model skill {msk:+.3f} theirs agree/disagree {sum(dscore(r, r["s"]) for r in A)/max(1,len(A)):+.3f}/{sum(dscore(r, r["s"]) for r in Dd)/max(1,len(Dd)):+.3f}')
json.dump({f'{r["station"]}|{r["ed"]}|{r["ts"]}': p for r, p, M in ev4}, open(f'{B}/model4_pred.json', 'w'))
json.dump({'F4': F4, 'coef_full': b4, 'coef_H1': fs4['H1'], 'coef_H2': fs4['H2']}, open(f'{B}/model4_coef.json', 'w'))
