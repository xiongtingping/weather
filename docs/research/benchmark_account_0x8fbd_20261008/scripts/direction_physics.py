"""Round 22: which model's, which variable's daytime anomaly carries direction
skill?  Open-Meteo previous runs (fetched 10-10, prev_vars.json): shortwave,
cloud cover, relative humidity, 10 m wind, dew point for ICON, ECMWF IFS, GFS,
UKMO, JMA GSM, ARPEGE.  Daytime = local 09-16 h mean of the run usable at the
trade (previous_day1 after D-1 08Z, else previous_day2 after D-2 08Z), z-scored
within station over other dates (same lag).  Signs: radiation +, cloud -,
humidity -, wind -, dew point + (all + = warmer).  Consensus = mean z over
the models present.
"""
import json, collections, random, statistics, sys
from datetime import datetime, timezone, timedelta
B = sys.argv[1]
random.seed(22)
R = json.load(open(f'{B}/direction_rows.json'))
Mm = json.load(open(f'{B}/momentum_M.json'))
PV = json.load(open(f'{B}/prev_vars.json'))
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}
    r['M'] = Mm.get(f'{r["station"]}|{r["ed"]}|{r["ts"]}')
VARS = {'shortwave_radiation': 1, 'cloud_cover': -1, 'relative_humidity_2m': -1, 'wind_speed_10m': -1, 'dew_point_2m': 1}
MODELS = ['icon_global', 'ecmwf_ifs025', 'gfs_global', 'ukmo_global_deterministic_10km', 'jma_gsm', 'meteofrance_arpege_world']
daily = {}
for key, v in PV.items():
    if 'time' not in v:
        continue
    for var in VARS:
        acc = collections.defaultdict(lambda: [[], []])
        for i, t in enumerate(v['time']):
            if 9 <= int(t[11:13]) <= 16:
                for j, lab in enumerate(('d1', 'd2')):
                    x = (v[var][lab] or [None] * len(v['time']))[i]
                    if x is not None:
                        acc[t[:10]][j].append(x)
        daily[(key, var)] = {d: tuple(statistics.mean(a) if len(a) == 8 else None for a in pair) for d, pair in acc.items()}
for r in R:
    D = datetime.fromisoformat(r['ed']).replace(tzinfo=timezone.utc)
    lag = 0 if r['ts'] >= (D - timedelta(days=1) + timedelta(hours=8)).timestamp() else 1 if r['ts'] >= (D - timedelta(days=2) + timedelta(hours=8)).timestamp() else None
    r['phys'] = {}
    if lag is None:
        continue
    for m in MODELS:
        for var, sg in VARS.items():
            dd = daily.get((f'{r["station"]}|{m}', var))
            if not dd or r['ed'] not in dd or dd[r['ed']][lag] is None:
                continue
            others = [x[lag] for d, x in dd.items() if d != r['ed'] and x[lag] is not None]
            if len(others) < 6:
                continue
            sd = statistics.pstdev(others)
            if sd <= 0:
                continue
            r['phys'][f'{m}:{var}'] = sg * (dd[r['ed']][lag] - statistics.mean(others)) / sd
    for var in VARS:
        zs = [r['phys'][f'{m}:{var}'] for m in MODELS if f'{m}:{var}' in r['phys']]
        if len(zs) >= 3:
            r['phys'][f'consensus:{var}'] = statistics.mean(zs)


def dscore(r, side):
    sd = lambda o: (o - r['mode']) * side
    D = 1 if sd(r['truth']) > 0 else -1 if sd(r['truth']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in r['q'].items())


def boot(rows, f, n=300):
    G = collections.defaultdict(list)
    for x in rows: G[(x['station'], x['ed'])].append(x)
    keys = list(G); vals = []
    for _ in range(n):
        s = [y for k in (random.choice(keys) for _ in keys) for y in G[k]]
        vals.append(sum(f(y) for y in s) / len(s))
    vals.sort()
    return vals[int(.025 * n)], vals[int(.975 * n) - 1]


sgn = lambda x: 1 if x > 0 else -1
def table(sub_name, sub, keys, tau=0.5):
    print(f'=== {sub_name}: n={len(sub)} (|z|>={tau}) ===')
    res = []
    for k in keys:
        sel = [dict(r, _x=r['phys'][k]) for r in sub if k in r['phys'] and abs(r['phys'][k]) >= tau]
        if len(sel) < 60:
            continue
        ag = sum(1 for r in sel if sgn(r['_x']) == r['s']) / len(sel)
        sk = sum(dscore(r, sgn(r['_x'])) for r in sel) / len(sel)
        slo, shi = boot(sel, lambda r: dscore(r, sgn(r['_x'])))
        h = [sum(dscore(r, sgn(r['_x'])) for r in sel if (r['ed'] < '2026-09-30') == (hh == 'H1')) / max(1, sum(1 for r in sel if (r['ed'] < '2026-09-30') == (hh == 'H1'))) for hh in ('H1', 'H2')]
        A = [r for r in sel if sgn(r['_x']) == r['s']]; Dd = [r for r in sel if sgn(r['_x']) != r['s']]
        res.append((sk, k, len(sel), ag, slo, shi, h, sum(dscore(r, r['s']) for r in A) / max(1, len(A)), sum(dscore(r, r['s']) for r in Dd) / max(1, len(Dd))))
    for sk, k, n, ag, slo, shi, h, ta, td in sorted(res, reverse=True):
        star = '*' if slo > 0 or shi < 0 else ' '
        print(f'   {k:46s} n={n:4d} agree {ag:.3f} skill {sk:+.3f}{star} [{slo:+.3f},{shi:+.3f}] H1/H2 {h[0]:+.3f}/{h[1]:+.3f}  theirs agree/disagree {ta:+.3f}/{td:+.3f}')
keys = sorted({k for r in R for k in r['phys']})
table('all off-mode', R, keys)
table('quiet |M|<0.5', [r for r in R if r['M'] is None or abs(r['M']) < 0.5], [k for k in keys if k.startswith(('consensus', 'icon', 'ecmwf'))])
json.dump({f'{r["station"]}|{r["ed"]}|{r["ts"]}': r['phys'] for r in R}, open(f'{B}/phys_signals.json', 'w'))
