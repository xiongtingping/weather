"""Round 21: the unabsorbed gap.

X_h = (model change over the last h hours) - (market change over the same h),
model change per system = newest-run member mean available at t minus the
newest available at t-h (same system, so bias cancels), averaged over the
systems present at both times; market change = m(t) - m(t-h) from WS
snapshots.  + means the models have moved warmer than the market has.
Systems: ECMWF family (AIFS ENS, IFS ENS natives; HRES, AIFS single points)
and, separately, all six natives.
"""
import json, sqlite3, bisect, collections, random, statistics, sys
from datetime import datetime, timezone, timedelta
B = sys.argv[1]
random.seed(21)
R = json.load(open(f'{B}/direction_rows.json'))
Mm = json.load(open(f'{B}/momentum_M.json'))
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}
    r['M'] = Mm.get(f'{r["station"]}|{r["ed"]}|{r["ts"]}')
reg = json.load(open(f'{B}/registry.json'))
CEN = {}
for x in reg:
    lo, hi, unit = x['lower_c'], x['upper_c'], x['settlement_unit']
    step = 1.0 if unit == 'C' else 2.0
    c = hi - step / 2 if lo is None else lo + step / 2 if hi is None else (lo + hi) / 2
    CEN[(x['station_id'], x['event_date'], x['outcome'])] = c if unit == 'C' else (c - 32) * 5 / 9
hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
cold = sqlite3.connect('file:/Volumes/weather_data/weather_cold/ssot_rows/basket_official_ws_snapshot.sqlite?mode=ro&immutable=1', uri=True)
LAG = {'ECMWF_IFS_ENS_OPEN_PF50': 8.2, 'NOAA_GEFS_05': 4.6, 'DWD_ICON_GLOBAL_EPS': 3.6, 'ECCC_GEM_GLOBAL_ENS': 5.5,
       'ECMWF_AIFS_ENS_GLOBAL': 6.3, 'NOAA_AIGEFS_025': 7.2}
COLD = {}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def cold_idx(ed):
    if ed not in COLD:
        COLD[ed] = collections.defaultdict(list)
        for st, oa, rid in cold.execute("select station_id, observed_at, rowid from basket_official_ws_snapshot where event_date=? and state='COMMITTED'", (ed,)):
            COLD[ed][st].append((oa, rid))
        for v in COLD[ed].values():
            v.sort()
    return COLD[ed]


def mmean(st, ed, ts):
    hi_iso, lo_iso = iso(ts), iso(ts - 1800)
    row = None
    if ed >= '2026-10-01':
        row = hot.execute("select buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? and observed_at>=? order by observed_at desc limit 1", (st, ed, hi_iso, lo_iso)).fetchone()
    else:
        lst = cold_idx(ed).get(st, [])
        keys = [x[0] for x in lst]
        i = bisect.bisect_right(keys, hi_iso) - 1
        if i >= 0 and lst[i][0] >= lo_iso:
            row = cold.execute("select buckets_json from basket_official_ws_snapshot where rowid=?", (lst[i][1],)).fetchone()
    if not row:
        return None
    tot, s = 0.0, 0.0
    for b in json.loads(row[0]):
        if b.get('bid_price') is not None and b.get('best_ask') is not None and 0 < b['best_ask'] - b['bid_price'] < 0.2:
            c = CEN.get((st, ed, b['bucket']))
            if c is None:
                return None
            m = (b['bid_price'] + b['best_ask']) / 2
            tot += c * m; s += m
    return tot / s if 0.85 <= s <= 1.2 else None


nat = {}


def natives(st, ed):
    if (st, ed) not in nat:
        d = collections.defaultdict(dict)
        for sysn, ri, mu in hot.execute("select ensemble_system_id, native_run_init_utc, avg(raw_tmax_c) from calibration_native_ensemble_historical_member_run "
                                        "where station_id=? and target_local_date=? and qc_status='ACCEPTED' and native_run_init_utc is not null group by 1,2", (st, ed)):
            if sysn in LAG:
                d[sysn][ri] = mu
        nat[(st, ed)] = d
    return nat[(st, ed)]


P = json.load(open(f'{B}/points.json'))
pts = collections.defaultdict(list)
for p in P:
    if p.get('complete') and p['system'] in ('ECMWF_IFS_9KM_RELAY_POINT', 'ECMWF_AIFS_SINGLE_POINT'):
        pts[(p['station'], p['target'], p['system'])].append((datetime.fromisoformat(p['knowable'].replace('Z', '+00:00')).timestamp(), p['run_init'], float(p['tmax'])))
for v in pts.values():
    v.sort()


def newest(st, ed, sysn, ts):
    if sysn in LAG:
        ok = [(ri, mu) for ri, mu in natives(st, ed).get(sysn, {}).items() if datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG[sysn] + 1) * 3600 <= ts]
        return max(ok)[1] if ok else None
    seen = {}
    for kt, ri, tm in pts.get((st, ed, sysn), []):
        if kt <= ts:
            seen[ri] = tm
    return seen[max(seen)] if seen else None


FAM = ['ECMWF_AIFS_ENS_GLOBAL', 'ECMWF_IFS_ENS_OPEN_PF50', 'ECMWF_IFS_9KM_RELAY_POINT', 'ECMWF_AIFS_SINGLE_POINT']
ALLN = list(LAG)
for r in R:
    r['gap'] = {}
    for h in (6, 12, 24, 36):
        m_then = mmean(r['station'], r['ed'], r['ts'] - h * 3600)
        if m_then is None:
            continue
        dmkt = r['m'] - m_then
        for lab, systems in (('ECMWF family', FAM), ('all natives', ALLN)):
            ch = []
            for s in systems:
                a, b = newest(r['station'], r['ed'], s, r['ts']), newest(r['station'], r['ed'], s, r['ts'] - h * 3600)
                if a is not None and b is not None:
                    ch.append(a - b)
            if ch:
                r['gap'][f'{lab} {h}h'] = statistics.mean(ch) - dmkt
                r['gap'][f'{lab} {h}h model-only'] = statistics.mean(ch)
        r['gap'][f'market only {h}h'] = dmkt


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
keys = sorted({k for r in R for k in r['gap']})
for sub_name, sub in (('all off-mode', R), ('quiet |M|<0.5', [r for r in R if r['M'] is None or abs(r['M']) < 0.5])):
    print(f'=== {sub_name} (|X|>=0.3 C) ===')
    for k in keys:
        sel = [dict(r, _x=r['gap'][k]) for r in sub if k in r['gap'] and abs(r['gap'][k]) >= 0.3]
        if len(sel) < 40:
            continue
        ag = sum(1 for r in sel if sgn(r['_x']) == r['s']) / len(sel)
        alo, ahi = boot(sel, lambda r: 1.0 if sgn(r['_x']) == r['s'] else 0.0)
        sk = sum(dscore(r, sgn(r['_x'])) for r in sel) / len(sel)
        slo, shi = boot(sel, lambda r: dscore(r, sgn(r['_x'])))
        A = [r for r in sel if sgn(r['_x']) == r['s']]; D = [r for r in sel if sgn(r['_x']) != r['s']]
        print(f'   {k:32s} n={len(sel):4d} agree {ag:.3f} [{alo:.3f},{ahi:.3f}] skill {sk:+.3f} [{slo:+.3f},{shi:+.3f}]  theirs agree/disagree {sum(dscore(r, r["s"]) for r in A)/max(1,len(A)):+.3f}/{sum(dscore(r, r["s"]) for r in D)/max(1,len(D)):+.3f}')
json.dump({f'{r["station"]}|{r["ed"]}|{r["ts"]}': r['gap'] for r in R}, open(f'{B}/gap_signals.json', 'w'))
