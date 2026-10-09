"""Round 17 part 2: refine the information models and separate the conditions.

1. YES picks: pure Gaussian likelihood (an independent observation centred at
   the picked bucket + delta, width sigma), tempered Gaussian with a wider
   amplitude grid, and a centre-shift model (the market ladder moved s buckets
   toward the picked side).  Cross-fitted by date half.
2. NO on the mode: a dispersion model (market ladder tempered q^tau, tau<1 =
   wider) against a sharpening one; cross-fitted.
3. Multivariable: excess ~ condition dummies (linear probability, station-day
   cluster bootstrap) to tell lead / stage / region / unit / concentration /
   yesterday-change apart.
4. Per-condition Gaussian width: does the precision change, or only how often
   a pick is informative?
5. Persistence: on days the market expects a change from yesterday, do they
   pick toward yesterday's high or away, and which wins?
"""
import gzip, json, math, random, collections, sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = '/Users/xiong/weather/docs/research/benchmark_account_0x8fbd_20261008'
BENCH = sys.argv[1]
rows = [json.loads(l) for l in gzip.open(f'{ROOT}/choice_rows.jsonl.gz', 'rt')]
TZ = {r['station_id']: r['settlement_timezone'] for r in json.load(open(f'{BENCH}/registry.json'))}
FLOOR = 1e-3
random.seed(1717)
E = []
for r in rows:
    yes = sorted((a for a in r['alts'] if a['side'] == 'Yes'), key=lambda a: a['ord'])
    q = {a['ord']: a['q'] for a in yes}
    if r['truth_ord'] not in q:
        continue
    ch = next(a for a in r['alts'] if a['chosen'])
    tzn = TZ.get(r['station'], '')
    width = 1.0 if r['unit'] == 'C' else 10 / 9
    e = dict(station=r['station'], ed=r['event_date'], lead=r['lead_h'], unit=r['unit'], stage=r['stage'],
             truth=r['truth_ord'], mode=r['mode_ord'], k=ch['ord'], side=ch['side'], d=ch['ord'] - r['mode_ord'],
             q=q, p={a['ord']: a['p'] for a in yes}, usdc=r['usdc'], ytruth=r.get('ytruth_c'), truth_c=r.get('truth_c'),
             width=width, half='H1' if r['event_date'] < '2026-09-30' else 'H2',
             region=('Asia' if tzn.startswith(('Asia', 'Pacific/Auckland')) else 'Europe' if tzn.startswith(('Europe', 'Africa')) else 'Americas'),
             hour=datetime.fromtimestamp(r['ts'], ZoneInfo(tzn or 'UTC')).hour)
    # market mean in buckets, and yesterday's high on the bucket axis
    e['mean'] = sum(o * v for o, v in q.items())
    if e['ytruth'] is not None and e['truth_c'] is not None:
        e['y_ord'] = e['truth'] + (e['ytruth'] - e['truth_c']) / width
    else:
        e['y_ord'] = None
    E.append(e)
YES = [e for e in E if e['side'] == 'Yes']
N0 = [e for e in E if e['side'] == 'No' and e['d'] == 0]
sgn = lambda e: 1 if e['d'] > 0 else (-1 if e['d'] < 0 else 0)


def logs_post(e, w):
    z = sum(w.values())
    return math.log(max(w.get(e['truth'], 0.0) / z, FLOOR))


def base(e):
    return math.log(max(e['q'][e['truth']], FLOOR))


def shifted(e, s):
    """Market ladder moved s buckets along the picked direction (linear interp)."""
    dirn = sgn(e) or 1
    shift = s * dirn
    out = {}
    for o in e['q']:
        src = o - shift
        lo = math.floor(src)
        f = src - lo
        out[o] = (1 - f) * e['q'].get(lo, 0.0) + f * e['q'].get(lo + 1, 0.0) + 1e-9
    return out


MODELS = {
    'pure gauss L=N(k+delta, sigma)': (lambda a: (lambda e: {o: e['q'][o] * math.exp(-((o - e['k'] - a[0] * (sgn(e) or 1)) ** 2) / (2 * a[1] ** 2)) for o in e['q']}),
                                       [[dl, sg] for dl in (-0.5, -0.25, 0, 0.25, 0.5) for sg in (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0)]),
    'tempered gauss L=1+A N(k,sigma)': (lambda a: (lambda e: {o: e['q'][o] * (1 + a[1] * math.exp(-((o - e['k']) ** 2) / (2 * a[0] ** 2))) for o in e['q']}),
                                        [[sg, A] for sg in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0) for A in (0.5, 1, 2, 3, 4, 6, 8, 12)]),
    'centre shift s buckets toward pick': (lambda a: (lambda e: shifted(e, a[0])), [[s] for s in (0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0)]),
}


def fit(evs, fac, grid):
    best = None
    for a in grid:
        f = fac(a)
        g = sum(logs_post(e, f(e)) - base(e) for e in evs) / len(evs)
        if best is None or g > best[0]:
            best = (g, a)
    return best


def xfit(evs, fac, grid):
    H = {h: [e for e in evs if e['half'] == h] for h in ('H1', 'H2')}
    out = []
    for tr, te in (('H1', 'H2'), ('H2', 'H1')):
        if len(H[tr]) < 20 or len(H[te]) < 20:
            out.append(None); continue
        _, a = fit(H[tr], fac, grid)
        f = fac(a)
        out.append(sum(logs_post(e, f(e)) - base(e) for e in H[te]) / len(H[te]))
    return out


print('=== 1. YES information models (nats/event over market; out-of-sample H1->H2, H2->H1) ===')
for subset, evs in (('all YES', YES), ('YES off-mode (|d|>=1)', [e for e in YES if e['d'] != 0]), ('YES on mode', [e for e in YES if e['d'] == 0])):
    print(f'-- {subset} n={len(evs)}')
    for name, (fac, grid) in MODELS.items():
        g, a = fit(evs, fac, grid)
        o = xfit(evs, fac, grid)
        print(f'   {name:38s} in {g:+.4f} params {a}   oos {"/".join("n/a" if x is None else f"{x:+.4f}" for x in o)}')
# pure gauss sigma in deg C per unit, off-mode
for unit in ('C', 'F'):
    evs = [e for e in YES if e['d'] != 0 and e['unit'] == unit]
    g, a = fit(evs, *MODELS['pure gauss L=N(k+delta, sigma)'])
    g2, a2 = fit(evs, *MODELS['centre shift s buckets toward pick'])
    w = 1.0 if unit == 'C' else 10 / 9
    print(f'   {unit} off-mode n={len(evs)}: pure gauss sigma={a[1]} bkt = {a[1]*w:.2f} C (delta {a[0]}), gain {g:+.4f};  shift s={a2[0]} bkt = {a2[0]*w:.2f} C, gain {g2:+.4f}')

print('\n=== 2. NO on the mode: dispersion (q^tau) ===')
def temper(e, tau):
    return {o: max(v, 1e-6) ** tau for o, v in e['q'].items()}
TG = [[t] for t in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25)]
g, a = fit(N0, lambda a: (lambda e: temper(e, a[0])), TG)
print(f'   NO-mode n={len(N0)}: best tau={a[0]} (<1 = wider than market) in-sample {g:+.4f}  oos {xfit(N0, lambda a: (lambda e: temper(e, a[0])), TG)}')
ctrl = [e for e in E if e['side'] == 'Yes' and e['d'] == 0]
g, a = fit(ctrl, lambda a: (lambda e: temper(e, a[0])), TG)
print(f'   control YES-mode n={len(ctrl)}: best tau={a[0]} in-sample {g:+.4f}')
dist = collections.Counter(abs(e['truth'] - e['mode']) for e in N0)
mkt = collections.defaultdict(float)
for e in N0:
    for o, v in e['q'].items():
        mkt[abs(o - e['mode'])] += v / len(N0)
print('   |truth-mode| distribution, NO-mode events vs market:', ' '.join(f'{k}:{dist[k]/len(N0):.3f}/{mkt[k]:.3f}' for k in range(0, 4)))
print('   NO-mode q_mode mean', round(sum(e['q'][e['mode']] for e in N0) / len(N0), 3), 'vs all events', round(sum(e['q'][e['mode']] for e in E) / len(E), 3))

print('\n=== 3. multivariable: excess ~ conditions (OLS, station-day bootstrap CI) ===')
def excess(e):
    hit = 1.0 if e['truth'] == e['k'] else 0.0
    return (hit - e['q'][e['k']]) if e['side'] == 'Yes' else (e['q'][e['k']] - hit)
def feats(e):
    ych = None if e['y_ord'] is None else abs(e['y_ord'] - e['mean'])
    return {
        'const': 1.0,
        'lead 12-48h': 1.0 if 12 <= e['lead'] < 48 else 0.0,
        'lead >=48h': 1.0 if e['lead'] >= 48 else 0.0,
        'lead <6h': 1.0 if e['lead'] < 6 else 0.0,
        'stage P1': 1.0 if e['stage'] == 'P1' else 0.0,
        'Asia': 1.0 if e['region'] == 'Asia' else 0.0,
        'Europe': 1.0 if e['region'] == 'Europe' else 0.0,
        'F unit': 1.0 if e['unit'] == 'F' else 0.0,
        'q_mode<0.35': 1.0 if e['q'][e['mode']] < 0.35 else 0.0,
        'q_mode>=0.45': 1.0 if e['q'][e['mode']] >= 0.45 else 0.0,
        'YES on mode': 1.0 if (e['side'] == 'Yes' and e['d'] == 0) else 0.0,
        'our p>q+0.05': 1.0 if e['p'].get(e['k'], 0) - e['q'][e['k']] > 0.05 else 0.0,
        '|yday-mean|>=1.5 bkt': 0.0 if ych is None else (1.0 if ych >= 1.5 else 0.0),
        'size>=100U': 1.0 if e['usdc'] >= 100 else 0.0,
    }
NAMES = list(feats(E[0]).keys())
def ols(evs):
    X = [[feats(e)[n] for n in NAMES] for e in evs]; y = [excess(e) for e in evs]
    k = len(NAMES)
    A = [[sum(X[i][a] * X[i][b] for i in range(len(X))) for b in range(k)] for a in range(k)]
    v = [sum(X[i][a] * y[i] for i in range(len(X))) for a in range(k)]
    for a in range(k): A[a][a] += 1e-6
    # gaussian elimination
    M = [A[i][:] + [v[i]] for i in range(k)]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(M[r][c])); M[c], M[piv] = M[piv], M[c]
        for r in range(k):
            if r != c and M[c][c]:
                f = M[r][c] / M[c][c]
                M[r] = [M[r][j] - f * M[c][j] for j in range(k + 1)]
    return [M[i][k] / M[i][i] for i in range(k)]
beta = ols(E)
G = collections.defaultdict(list)
for e in E: G[(e['station'], e['ed'])].append(e)
keys = list(G); bs = []
for _ in range(400):
    s = [e for kk in (random.choice(keys) for _ in keys) for e in G[kk]]
    bs.append(ols(s))
H = {h: ols([e for e in E if e['half'] == h]) for h in ('H1', 'H2')}
print(f'   {"term":24s}  coef    95%CI            H1      H2')
for i, n in enumerate(NAMES):
    col = sorted(b[i] for b in bs)
    lo, hi = col[int(0.025 * len(col))], col[int(0.975 * len(col)) - 1]
    sig = '*' if lo > 0 or hi < 0 else ' '
    print(f'   {n:24s} {beta[i]:+.3f}{sig} [{lo:+.3f},{hi:+.3f}]  {H["H1"][i]:+.3f} {H["H2"][i]:+.3f}')

print('\n=== 4. pure-gauss width by condition (YES off-mode) ===')
fac, grid = MODELS['pure gauss L=N(k+delta, sigma)']
OFF = [e for e in YES if e['d'] != 0]
for name, fn in (('lead', lambda e: '12-48h' if 12 <= e['lead'] < 48 else '<12h' if e['lead'] < 12 else '>=48h'),
                 ('region', lambda e: e['region']),
                 ('q_mode', lambda e: '<0.35' if e['q'][e['mode']] < 0.35 else '>=0.35'),
                 ('stage', lambda e: e['stage'])):
    grp = collections.defaultdict(list)
    for e in OFF: grp[fn(e)].append(e)
    for lvl, evs in sorted(grp.items()):
        if len(evs) < 40: continue
        g, a = fit(evs, fac, grid)
        print(f'   {name:7s} {lvl:9s} n={len(evs):4d}  sigma={a[1]:.2f} bkt  delta={a[0]:+.2f}  gain {g:+.4f}  oos {"/".join("n/a" if x is None else f"{x:+.4f}" for x in xfit(evs, fac, grid))}')

print('\n=== 5. persistence: YES off-mode picks when the market expects a change ===')
rows5 = [e for e in OFF if e['y_ord'] is not None and abs(e['y_ord'] - e['mean']) >= 1.0]
toward = [e for e in rows5 if (e['y_ord'] - e['mean']) * sgn(e) > 0]
away = [e for e in rows5 if (e['y_ord'] - e['mean']) * sgn(e) < 0]
def hr(evs): return (sum(1 for e in evs if e['truth'] == e['k']) / len(evs), sum(e['q'][e['k']] for e in evs) / len(evs)) if evs else (None, None)
print(f'   expected change >=1 bucket: n={len(rows5)}; picked toward yesterday {len(toward)} ({len(toward)/max(1,len(rows5)):.0%}), away {len(away)}')
for lab, evs in (('toward yesterday (persistence)', toward), ('away from yesterday (with the change)', away)):
    h, qq = hr(evs)
    if h is not None:
        print(f'   {lab:38s} n={len(evs):3d} hit {h:.3f} vs q {qq:.3f}  excess {h-qq:+.3f}  H1/H2 {hr([e for e in evs if e["half"]=="H1"])[0] and hr([e for e in evs if e["half"]=="H1"])[0]-hr([e for e in evs if e["half"]=="H1"])[1]:+.3f}/{hr([e for e in evs if e["half"]=="H2"])[0]-hr([e for e in evs if e["half"]=="H2"])[1]:+.3f}')
# what the market itself does on those days: truth relative to market mean, toward yesterday?
tow_truth = sum(1 for e in rows5 if (e['truth'] - e['mean']) * (e['y_ord'] - e['mean']) > 0) / len(rows5)
print(f'   on those days truth lands on yesterday side of the market mean: {tow_truth:.3f}')
