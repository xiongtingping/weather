"""Round 19: the residual of the direction call.

A. Two-run trend: newest minus two runs ago, and 'both last updates the same
   way', for AIFS ENS / IFS ENS (native, lag schedule +1h) and HRES / AIFS
   single points (strict stamps).
B. Market-error persistence: the market's error on earlier days at the same
   station, err(D-k) = truth(D-k) - market mean of D-k at its local 00:00, for
   k = 2 (fully known before any D-1 trade) and k = 1 where known by the trade;
   plus the D-1 partial: observed max so far on the trade's local day minus
   that day's market mean at its local 00:00 (trades after local 15:00).
   Agreement with them, own skill, and on every station-day the day-to-day
   autocorrelation of the market's own error.
"""
import json, sqlite3, bisect, collections, random, sys, statistics
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
B = sys.argv[1]
random.seed(19)
R = json.load(open(f'{B}/direction_rows.json'))
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}
reg = json.load(open(f'{B}/registry.json'))
TZ = {x['station_id']: x['settlement_timezone'] for x in reg}
CEN = {}
for x in reg:
    lo, hi, unit = x['lower_c'], x['upper_c'], x['settlement_unit']
    step = 1.0 if unit == 'C' else 2.0
    c = hi - step / 2 if lo is None else lo + step / 2 if hi is None else (lo + hi) / 2
    CEN[(x['station_id'], x['event_date'], x['outcome'])] = c if unit == 'C' else (c - 32) * 5 / 9
hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
cold = sqlite3.connect('file:/Volumes/weather_data/weather_cold/ssot_rows/basket_official_ws_snapshot.sqlite?mode=ro&immutable=1', uri=True)
labels = {}
for st, ed, t in hot.execute("select station_id,event_date,settled_tmax_c from basket_station_day_settlement_label l where truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)"):
    labels[(st, ed)] = float(t)


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


def report(label, sigfn, tau=0.3):
    sel = []
    for r in R:
        x = sigfn(r)
        if x is not None and abs(x) >= tau:
            sel.append(dict(r, _x=x))
    if len(sel) < 50:
        print(f'   {label:52s} n={len(sel)}'); return
    ag = sum(1 for x in sel if sgn(x['_x']) == x['s']) / len(sel)
    alo, ahi = boot(sel, lambda x: 1.0 if sgn(x['_x']) == x['s'] else 0.0)
    sk = sum(dscore(x, sgn(x['_x'])) for x in sel) / len(sel)
    slo, shi = boot(sel, lambda x: dscore(x, sgn(x['_x'])))
    A = [x for x in sel if sgn(x['_x']) == x['s']]; Dd = [x for x in sel if sgn(x['_x']) != x['s']]
    ta = sum(dscore(x, x['s']) for x in A) / max(1, len(A)); td = sum(dscore(x, x['s']) for x in Dd) / max(1, len(Dd))
    print(f'   {label:52s} n={len(sel):4d} agree {ag:.3f} [{alo:.3f},{ahi:.3f}] skill {sk:+.3f} [{slo:+.3f},{shi:+.3f}] theirs agree/disagree {ta:+.3f}/{td:+.3f}')


# ---------------------------------------------------------------- A. two-run trend
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
for v in pts.values():
    v.sort()


def series(r, src):
    if src in LAG:
        lst = sorted((ri, mu) for (s2, ri), mu in nat.get((r['station'], r['ed']), {}).items() if s2 == src
                     and datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG[src] + 1) * 3600 <= r['ts'])
        return [mu for _, mu in lst]
    lst = [x for x in pts.get((r['station'], r['ed'], src), []) if x[0] <= r['ts']]
    seen = {}
    for kt, ri, tm in lst:
        seen[ri] = tm
    return [seen[k] for k in sorted(seen)]


print('=== A. two-run trend ===')
for src in ('ECMWF_AIFS_ENS_GLOBAL', 'ECMWF_IFS_ENS_OPEN_PF50', 'ECMWF_IFS_9KM_RELAY_POINT', 'ECMWF_AIFS_SINGLE_POINT'):
    report(f'{src} newest - previous', lambda r, s=src: (lambda v: v[-1] - v[-2] if len(v) >= 2 else None)(series(r, s)))
    report(f'{src} newest - two runs ago', lambda r, s=src: (lambda v: v[-1] - v[-3] if len(v) >= 3 else None)(series(r, s)))
    report(f'{src} previous update only (one run back)', lambda r, s=src: (lambda v: v[-2] - v[-3] if len(v) >= 3 else None)(series(r, s)))
    report(f'{src} both last updates same way (sum)', lambda r, s=src: (lambda v: (v[-1] - v[-3]) if len(v) >= 3 and (v[-1] - v[-2]) * (v[-2] - v[-3]) > 0 else None)(series(r, s)), tau=0.0)

# ---------------------------------------------------------------- B. market-error persistence
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


def market_mean(st, ed, ts):
    hi_iso, lo_iso = iso(ts), iso(ts - 3 * 3600)
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
    mids = {}
    for b in json.loads(row[0]):
        if b.get('bid_price') is not None and b.get('best_ask') is not None and 0 < b['best_ask'] - b['bid_price'] < 0.2:
            c = CEN.get((st, ed, b['bucket']))
            if c is None:
                return None
            mids[c] = (b['bid_price'] + b['best_ask']) / 2
    s = sum(mids.values())
    if len(mids) < 3 or not 0.85 <= s <= 1.2:
        return None
    return sum(c * v / s for c, v in mids.items())


ERR = {}


def err(st, ed):
    """truth(ed) - market mean of ed at its local 00:00."""
    if (st, ed) in ERR:
        return ERR[(st, ed)]
    t = labels.get((st, ed))
    v = None
    if t is not None:
        z = ZoneInfo(TZ[st])
        t0 = datetime.combine(date.fromisoformat(ed), datetime.min.time(), tzinfo=z).timestamp()
        m = market_mean(st, ed, t0)
        if m is not None:
            v = t - m
    ERR[(st, ed)] = v
    return v


def prev(ed, k):
    return (date.fromisoformat(ed) - timedelta(days=k)).isoformat()


print('\n=== B. market-error persistence (their events) ===')
report('err(D-2): market error two days earlier', lambda r: err(r['station'], prev(r['ed'], 2)))
def err_d1_if_known(r):
    z = ZoneInfo(TZ[r['station']])
    end_d1 = datetime.combine(date.fromisoformat(r['ed']), datetime.min.time(), tzinfo=z).timestamp()
    return err(r['station'], prev(r['ed'], 1)) if r['ts'] >= end_d1 else None
report('err(D-1) where D-1 had ended by the trade', err_d1_if_known)
report('mean of err(D-2), err(D-3)', lambda r: (lambda a, b: (a + b) / 2 if a is not None and b is not None else None)(err(r['station'], prev(r['ed'], 2)), err(r['station'], prev(r['ed'], 3))))

print('\n=== B2. market error autocorrelation on every station-day (local-midnight market) ===')
pairs = []
stations = sorted({x['station_id'] for x in reg})
days = [(date(2026, 9, 21) + timedelta(days=i)).isoformat() for i in range(19)]
for st in stations:
    for ed in days:
        a = err(st, prev(ed, 1)); b = err(st, ed)
        if a is not None and b is not None:
            pairs.append((st, ed, a, b))
for lag in (1, 2):
    pp = []
    for st in stations:
        for ed in days:
            a = err(st, prev(ed, lag)); b = err(st, ed)
            if a is not None and b is not None:
                pp.append((a, b))
    if len(pp) > 30:
        xa = [a for a, b in pp]; xb = [b for a, b in pp]
        ma, mb = statistics.mean(xa), statistics.mean(xb)
        cov = sum((a - ma) * (b - mb) for a, b in pp) / len(pp)
        corr = cov / (statistics.pstdev(xa) * statistics.pstdev(xb))
        same = sum(1 for a, b in pp if abs(a) >= 0.5 and abs(b) >= 0.5 and (a > 0) == (b > 0)) / max(1, sum(1 for a, b in pp if abs(a) >= 0.5 and abs(b) >= 0.5))
        print(f'   lag {lag} day: n={len(pp)} corr {corr:+.3f}  same sign when both |err|>=0.5C: {same:.3f}  mean err {mb:+.3f}C')
