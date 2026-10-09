"""Round 18 part 2: the run-to-run update direction.

1. Robustness of the native update signal to the publication-lag schedule:
   recompute newest-minus-previous with +0/+1/+2/+3 h added to every lag.
2. Agreement and skill by time since the newest run became available.
3. ECMWF-family composite: AIFS ENS, IFS ENS (native, lagged), HRES and AIFS
   single (points, strict knowable stamps): sign of the summed update.
"""
import json, sqlite3, collections, random, sys
from datetime import datetime, timezone, date
B = sys.argv[1]
R = json.load(open(f'{B}/direction_rows.json'))
random.seed(1818)
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}
hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
LAG = {'ECMWF_IFS_ENS_OPEN_PF50': 8.2, 'NOAA_GEFS_05': 4.6, 'DWD_ICON_GLOBAL_EPS': 3.6, 'ECCC_GEM_GLOBAL_ENS': 5.5,
       'ECMWF_AIFS_ENS_GLOBAL': 6.3, 'NOAA_AIGEFS_025': 7.2}
nat = collections.defaultdict(dict)
for st in sorted({r['station'] for r in R}):
    for tg, sysn, ri, mu in hot.execute(
            "select target_local_date, ensemble_system_id, native_run_init_utc, avg(raw_tmax_c) from calibration_native_ensemble_historical_member_run "
            "where station_id=? and target_local_date between '2026-09-20' and '2026-10-09' and qc_status='ACCEPTED' and native_run_init_utc is not null "
            "and ensemble_system_id in ('ECMWF_AIFS_ENS_GLOBAL','ECMWF_IFS_ENS_OPEN_PF50') group by 1,2,3", (st,)):
        nat[(st, tg)][(sysn, ri)] = mu


def dscore(r, side):
    sd = lambda o: (o - r['mode']) * side
    D = 1 if sd(r['truth']) > 0 else -1 if sd(r['truth']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in r['q'].items())


def boot(rs, f, n=400):
    G = collections.defaultdict(list)
    for r in rs: G[(r['station'], r['ed'])].append(r)
    keys = list(G); vals = []
    for _ in range(n):
        s = [x for k in (random.choice(keys) for _ in keys) for x in G[k]]
        vals.append(sum(f(x) for x in s) / len(s))
    vals.sort()
    return vals[int(.025 * n)], vals[int(.975 * n) - 1]


def update(r, sysn, extra_h):
    runs = sorted((ri, mu) for (s2, ri), mu in nat.get((r['station'], r['ed']), {}).items() if s2 == sysn
                  and datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG[sysn] + extra_h) * 3600 <= r['ts'])
    if len(runs) < 2:
        return None, None
    avail = datetime.fromisoformat(runs[-1][0].replace('Z', '+00:00')).timestamp() + LAG[sysn] * 3600
    return runs[-1][1] - runs[-2][1], (r['ts'] - avail) / 3600


def summarize(label, rs, sigfn, tau=0.3):
    sel = [(r, sigfn(r)) for r in rs]
    sel = [(r, x) for r, x in sel if x is not None and abs(x) >= tau]
    if len(sel) < 40:
        print(f'   {label:44s} n={len(sel)} (too few)'); return
    ag = sum(1 for r, x in sel if (1 if x > 0 else -1) == r['s']) / len(sel)
    rows = [dict(r, _x=x) for r, x in sel]
    alo, ahi = boot(rows, lambda z: 1.0 if (1 if z['_x'] > 0 else -1) == z['s'] else 0.0)
    sk = sum(dscore(r, 1 if x > 0 else -1) for r, x in sel) / len(sel)
    slo, shi = boot(rows, lambda z: dscore(z, 1 if z['_x'] > 0 else -1))
    h = {hh: [(r, x) for r, x in sel if (r['ed'] < '2026-09-30') == (hh == 'H1')] for hh in ('H1', 'H2')}
    hs = {hh: (sum(1 for r, x in v if (1 if x > 0 else -1) == r['s']) / len(v), sum(dscore(r, 1 if x > 0 else -1) for r, x in v) / len(v)) if v else (float('nan'), float('nan')) for hh, v in h.items()}
    print(f'   {label:44s} n={len(sel):4d} agree {ag:.3f} [{alo:.3f},{ahi:.3f}]  skill {sk:+.3f} [{slo:+.3f},{shi:+.3f}]  H1 {hs["H1"][0]:.2f}/{hs["H1"][1]:+.3f}  H2 {hs["H2"][0]:.2f}/{hs["H2"][1]:+.3f}')


print('=== 1. native update vs publication-lag margin (agree / own skill) ===')
for sysn in ('ECMWF_AIFS_ENS_GLOBAL', 'ECMWF_IFS_ENS_OPEN_PF50'):
    for extra in (0, 1, 2, 3, 6):
        summarize(f'{sysn} lag+{extra}h', R, lambda r, s=sysn, x=extra: update(r, s, x)[0])

print('\n=== 2. by hours since the newest AIFS ENS run became available ===')
for lo, hi in ((0, 3), (3, 6), (6, 12), (12, 24), (24, 99)):
    rs = [r for r in R if (lambda u: u[1] is not None and lo <= u[1] < hi)(update(r, 'ECMWF_AIFS_ENS_GLOBAL', 0))]
    summarize(f'AIFS ENS update, {lo}-{hi}h after availability', rs, lambda r: update(r, 'ECMWF_AIFS_ENS_GLOBAL', 0)[0])

print('\n=== 3. ECMWF-family composite (sum of available updates; tau on the sum) ===')
def comp(r, parts, extra=1):
    vals = []
    for p in parts:
        if p.startswith('nat:'):
            v, _ = update(r, p[4:], extra)
        else:
            v = r['sig'].get(p)
        if v is not None:
            vals.append(v)
    return sum(vals) if vals else None
FAM = ['nat:ECMWF_AIFS_ENS_GLOBAL', 'nat:ECMWF_IFS_ENS_OPEN_PF50', 'ptd:ECMWF_IFS_9KM_RELAY_POINT', 'ptd:ECMWF_AIFS_SINGLE_POINT']
summarize('composite (lag+1h)', R, lambda r: comp(r, FAM, 1))
summarize('composite points only (strict stamps)', R, lambda r: comp(r, FAM[2:], 0))
summarize('composite natives only (lag+1h)', R, lambda r: comp(r, FAM[:2], 1))
for tau in (0.5, 1.0):
    summarize(f'composite (lag+1h) |sum|>={tau}', R, lambda r: comp(r, FAM, 1), tau=tau)
# coverage of their events by the composite and their skill inside / outside
sel = [r for r in R if (lambda c: c is not None and abs(c) >= 0.3)(comp(r, FAM, 1))]
agree = [r for r in sel if (1 if comp(r, FAM, 1) > 0 else -1) == r['s']]
print(f'\n   their events covered by |composite|>=0.3: {len(sel)}/{len(R)}; composite agrees with them on {len(agree)}')
for lab, rs in (('their skill where composite agrees', agree), ('their skill where composite disagrees', [r for r in sel if r not in agree]),
                ('their skill where composite silent', [r for r in R if r not in sel])):
    print(f'   {lab:42s} n={len(rs):4d}  {sum(dscore(r, r["s"]) for r in rs)/len(rs):+.3f}')
