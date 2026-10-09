"""Round 19 part 2: an unfitted ECMWF momentum composite.

M = sum over available components of (component / its own std), where the
components are AIFS ENS newest update, IFS ENS newest update, HRES newest
update and AIFS single two-run change (newest minus two runs ago).  No weight
is fitted.  Reported: agreement with them, own skill, their skill split, and
whether it reaches the events the round-18 composite left silent.
"""
import json, sqlite3, collections, random, statistics, sys
from datetime import datetime
B = sys.argv[1]
random.seed(1919)
R = json.load(open(f'{B}/direction_rows.json'))
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}
hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
LAG = {'ECMWF_AIFS_ENS_GLOBAL': 6.3, 'ECMWF_IFS_ENS_OPEN_PF50': 8.2}
nat = collections.defaultdict(dict)
for st in sorted({r['station'] for r in R}):
    for tg, sysn, ri, mu in hot.execute(
            "select target_local_date, ensemble_system_id, native_run_init_utc, avg(raw_tmax_c) from calibration_native_ensemble_historical_member_run "
            "where station_id=? and target_local_date between '2026-09-20' and '2026-10-09' and qc_status='ACCEPTED' and native_run_init_utc is not null "
            "and ensemble_system_id in ('ECMWF_AIFS_ENS_GLOBAL','ECMWF_IFS_ENS_OPEN_PF50') group by 1,2,3", (st,)):
        nat[(st, tg)][(sysn, ri)] = mu
P = json.load(open(f'{B}/points.json'))
pts = collections.defaultdict(list)
for p in P:
    if p.get('complete'):
        pts[(p['station'], p['target'], p['system'])].append((datetime.fromisoformat(p['knowable'].replace('Z', '+00:00')).timestamp(), p['run_init'], float(p['tmax'])))


def series(r, src):
    if src in LAG:
        lst = sorted((ri, mu) for (s2, ri), mu in nat.get((r['station'], r['ed']), {}).items() if s2 == src
                     and datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG[src] + 1) * 3600 <= r['ts'])
        return [mu for _, mu in lst]
    seen = {}
    for kt, ri, tm in sorted(pts.get((r['station'], r['ed'], src), [])):
        if kt <= r['ts']:
            seen[ri] = tm
    return [seen[k] for k in sorted(seen)]


COMP = {
    'AIFS ENS d1': lambda r: (lambda v: v[-1] - v[-2] if len(v) >= 2 else None)(series(r, 'ECMWF_AIFS_ENS_GLOBAL')),
    'IFS ENS d1': lambda r: (lambda v: v[-1] - v[-2] if len(v) >= 2 else None)(series(r, 'ECMWF_IFS_ENS_OPEN_PF50')),
    'HRES d1': lambda r: (lambda v: v[-1] - v[-2] if len(v) >= 2 else None)(series(r, 'ECMWF_IFS_9KM_RELAY_POINT')),
    'AIFS single d2': lambda r: (lambda v: v[-1] - v[-3] if len(v) >= 3 else None)(series(r, 'ECMWF_AIFS_SINGLE_POINT')),
}
for r in R:
    r['c'] = {k: f(r) for k, f in COMP.items()}
SD = {k: statistics.pstdev([r['c'][k] for r in R if r['c'][k] is not None]) for k in COMP}
print('component std (C):', {k: round(v, 3) for k, v in SD.items()})
for r in R:
    zs = [r['c'][k] / SD[k] for k in COMP if r['c'][k] is not None]
    r['M'] = sum(zs) if zs else None
    r['nM'] = len(zs)


def dscore(r, side):
    sd = lambda o: (o - r['mode']) * side
    D = 1 if sd(r['truth']) > 0 else -1 if sd(r['truth']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in r['q'].items())


def boot(rows, f, n=400):
    G = collections.defaultdict(list)
    for x in rows: G[(x['station'], x['ed'])].append(x)
    keys = list(G); vals = []
    for _ in range(n):
        s = [y for k in (random.choice(keys) for _ in keys) for y in G[k]]
        vals.append(sum(f(y) for y in s) / len(s))
    vals.sort()
    return vals[int(.025 * n)], vals[int(.975 * n) - 1]


sgn = lambda x: 1 if x > 0 else -1
print(f'their skill on all {len(R)} off-mode events: {sum(dscore(r, r["s"]) for r in R)/len(R):+.3f}')
for tau in (0.0, 0.5, 1.0, 1.5, 2.0):
    sel = [r for r in R if r['M'] is not None and abs(r['M']) >= max(tau, 1e-9)]
    ag = sum(1 for r in sel if sgn(r['M']) == r['s']) / len(sel)
    alo, ahi = boot(sel, lambda r: 1.0 if sgn(r['M']) == r['s'] else 0.0)
    sk = sum(dscore(r, sgn(r['M'])) for r in sel) / len(sel)
    slo, shi = boot(sel, lambda r: dscore(r, sgn(r['M'])))
    A = [r for r in sel if sgn(r['M']) == r['s']]; D = [r for r in sel if sgn(r['M']) != r['s']]
    h = {hh: [r for r in sel if (r['ed'] < '2026-09-30') == (hh == 'H1')] for hh in ('H1', 'H2')}
    hs = ' '.join(f'{hh} {sum(1 for r in v if sgn(r["M"])==r["s"])/len(v):.2f}/{sum(dscore(r, sgn(r["M"])) for r in v)/len(v):+.3f}' for hh, v in h.items() if v)
    print(f'|M|>={tau:3.1f}: n={len(sel):4d} agree {ag:.3f} [{alo:.3f},{ahi:.3f}] M-skill {sk:+.3f} [{slo:+.3f},{shi:+.3f}]  theirs agree {sum(dscore(r, r["s"]) for r in A)/len(A):+.3f} (n={len(A)}) / disagree {sum(dscore(r, r["s"]) for r in D)/len(D):+.3f} (n={len(D)})  {hs}')
# events left silent by round-18 composite
FAM = ['natd:ECMWF_AIFS_ENS_GLOBAL', 'natd:ECMWF_IFS_ENS_OPEN_PF50', 'ptd:ECMWF_IFS_9KM_RELAY_POINT', 'ptd:ECMWF_AIFS_SINGLE_POINT']
def fam(r):
    v = [r['sig'][k] for k in FAM if k in r['sig']]
    return sum(v) if v else None
silent = [r for r in R if fam(r) is None or abs(fam(r)) < 0.3]
s2 = [r for r in silent if r['M'] is not None and abs(r['M']) >= 1.0]
if s2:
    print(f'\nround-18 silent events {len(silent)}: momentum |M|>=1 on {len(s2)}; agree {sum(1 for r in s2 if sgn(r["M"])==r["s"])/len(s2):.3f}; M-skill {sum(dscore(r, sgn(r["M"])) for r in s2)/len(s2):+.3f}; theirs {sum(dscore(r, r["s"]) for r in s2)/len(s2):+.3f}')
json.dump({f'{r["station"]}|{r["ed"]}|{r["ts"]}': r['M'] for r in R}, open(f'{B}/momentum_M.json', 'w'))
