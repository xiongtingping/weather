"""Round 18 part 3: is 'go with the newest ECMWF-family update' a public rule
with edge on every station-day, not only on the cluster's events?

Instances: every station x event date (09-22..10-08) x every AIFS ENS and IFS
ENS run whose availability (run_init + lag + 1 h margin) falls 6-60 h before
the end of the local event day.  At t = availability:
  delta    newest minus previous run member mean (deg C)
  m(t)     market mean from the WS snapshot at or just before t
  drift    sign(delta) x (m(t+h) - m(t)) for h = 1, 3, 6, 12 (market catching up)
  pre      sign(delta) x (m(t) - m(t-6h)) (already absorbed before t?)
  score    direction score of side sign(delta) vs the market at t
  trade    YES on the market-mode neighbour on the delta side at best ask:
           hit - ask (cents)
"""
import json, sqlite3, bisect, collections, random, sys, statistics
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
B = sys.argv[1]
random.seed(18018)
reg = json.load(open(f'{B}/registry.json'))
TZ = {r['station_id']: r['settlement_timezone'] for r in reg}
CEN = {}
for r in reg:
    lo, hi, unit = r['lower_c'], r['upper_c'], r['settlement_unit']
    step = 1.0 if unit == 'C' else 2.0
    c = hi - step / 2 if lo is None else lo + step / 2 if hi is None else (lo + hi) / 2
    CEN[(r['station_id'], r['event_date'], r['outcome'])] = c if unit == 'C' else (c - 32) * 5 / 9
hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
cold = sqlite3.connect('file:/Volumes/weather_data/weather_cold/ssot_rows/basket_official_ws_snapshot.sqlite?mode=ro&immutable=1', uri=True)
labels = {}
for st, ed, b, t in hot.execute("select station_id,event_date,settled_bucket,settled_tmax_c from basket_station_day_settlement_label l where truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)"):
    labels[(st, ed)] = b
LAG = {'ECMWF_AIFS_ENS_GLOBAL': 6.3, 'ECMWF_IFS_ENS_OPEN_PF50': 8.2}
MARGIN = 1.0
DATES = [(date(2026, 9, 22) + timedelta(days=i)).isoformat() for i in range(17)]
stations = sorted({r['station_id'] for r in reg if r['event_date'] in DATES})
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


def ladder(st, ed, ts):
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
    out = {}
    for b in json.loads(row[0]):
        if b.get('bid_price') is not None and b.get('best_ask') is not None and 0 < b['best_ask'] - b['bid_price'] < 0.2:
            out[b['bucket']] = (b['ordinal'], b['bid_price'], b['best_ask'])
    s = sum((bid + ask) / 2 for _, bid, ask in out.values())
    if len(out) < 3 or not 0.85 <= s <= 1.2:
        return None
    return out, s


def mean_of(st, ed, lad):
    out, s = lad
    tot = 0.0
    for bk, (o, bid, ask) in out.items():
        c = CEN.get((st, ed, bk))
        if c is None:
            return None
        tot += (bid + ask) / 2 / s * c
    return tot


inst = []
for st in stations:
    zone = ZoneInfo(TZ[st])
    rows = collections.defaultdict(dict)
    for tg, sysn, ri, mu in hot.execute(
            "select target_local_date, ensemble_system_id, native_run_init_utc, avg(raw_tmax_c) from calibration_native_ensemble_historical_member_run "
            "where station_id=? and target_local_date between '2026-09-22' and '2026-10-08' and qc_status='ACCEPTED' and native_run_init_utc is not null "
            "and ensemble_system_id in ('ECMWF_AIFS_ENS_GLOBAL','ECMWF_IFS_ENS_OPEN_PF50') group by 1,2,3", (st,)):
        rows[(tg, sysn)][ri] = mu
    for (tg, sysn), runs in rows.items():
        if (st, tg) not in labels:
            continue
        day_end = datetime.combine(date.fromisoformat(tg) + timedelta(days=1), datetime.min.time(), tzinfo=zone).timestamp()
        order = sorted(runs)
        for i in range(1, len(order)):
            ri, rp = order[i], order[i - 1]
            gap = (datetime.fromisoformat(ri.replace('Z', '+00:00')) - datetime.fromisoformat(rp.replace('Z', '+00:00'))).total_seconds() / 3600
            if gap > 12:
                continue
            t = datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG[sysn] + MARGIN) * 3600
            lead = (day_end - t) / 3600
            if not 6 <= lead <= 60:
                continue
            delta = runs[ri] - runs[rp]
            lad = ladder(st, tg, t)
            if not lad:
                continue
            m0 = mean_of(st, tg, lad)
            if m0 is None:
                continue
            rec = dict(st=st, ed=tg, sys=sysn, t=t, lead=lead, delta=delta, m0=m0, lad=lad, truth=labels[(st, tg)],
                       unit='F' if '°F' in next(iter(lad[0])) else 'C')
            for h in (1, 3, 6, 12, -6):
                l2 = ladder(st, tg, t + h * 3600)
                if l2:
                    m2 = mean_of(st, tg, l2)
                    if m2 is not None:
                        rec[f'm{h}'] = m2
            inst.append(rec)
print('instances', len(inst), collections.Counter(r['sys'] for r in inst), file=sys.stderr)


def evaluate(r, tau):
    if abs(r['delta']) < tau:
        return None
    side = 1 if r['delta'] > 0 else -1
    out, s = r['lad']
    q = {o: (bid + ask) / 2 / s for bk, (o, bid, ask) in out.items()}
    ords = {bk: o for bk, (o, bid, ask) in out.items()}
    mode = max(q, key=q.get)
    tord = ords.get(r['truth'])
    if tord is None:
        return None
    sd = lambda o: (o - mode) * side
    D = 1 if sd(tord) > 0 else -1 if sd(tord) < 0 else 0
    ED = sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in q.items())
    nb = mode + side
    trade = None
    for bk, (o, bid, ask) in out.items():
        if o == nb:
            trade = (1.0 if tord == nb else 0.0) - ask
    res = dict(score=D - ED, trade=trade)
    for h in (1, 3, 6, 12):
        if f'm{h}' in r:
            res[f'drift{h}'] = side * (r[f'm{h}'] - r['m0'])
    if 'm-6' in r:
        res['pre'] = side * (r['m0'] - r['m-6'])
    return res


def boot(vals_by_key, n=500):
    keys = list(vals_by_key)
    out = []
    for _ in range(n):
        s = [v for k in (random.choice(keys) for _ in keys) for v in vals_by_key[k]]
        out.append(sum(s) / len(s))
    out.sort()
    return out[int(.025 * n)], out[int(.975 * n) - 1]


def report(label, rs, tau=0.3):
    ev = [(r, evaluate(r, tau)) for r in rs]
    ev = [(r, e) for r, e in ev if e is not None]
    if len(ev) < 50:
        print(f'   {label:40s} n={len(ev)}'); return
    parts = []
    for key in ('score', 'trade', 'pre', 'drift1', 'drift3', 'drift6', 'drift12'):
        G = collections.defaultdict(list)
        for r, e in ev:
            if e.get(key) is not None:
                G[(r['st'], r['ed'])].append(e[key])
        vals = [v for g in G.values() for v in g]
        if not vals:
            continue
        lo, hi = boot(G, 300)
        h1 = [e[key] for r, e in ev if e.get(key) is not None and r['ed'] < '2026-09-30']
        h2 = [e[key] for r, e in ev if e.get(key) is not None and r['ed'] >= '2026-09-30']
        sc = 100 if key in ('trade',) else 1
        parts.append(f'{key} {sc*sum(vals)/len(vals):+.3f}[{sc*lo:+.3f},{sc*hi:+.3f}] H {sc*sum(h1)/max(1,len(h1)):+.3f}/{sc*sum(h2)/max(1,len(h2)):+.3f}')
    print(f'   {label:40s} n={len(ev):5d}  ' + '\n' + ' ' * 50 + ('\n' + ' ' * 50).join(parts))


print('=== newest-run update rule on every station-day (direction score; trade = cents/share at ask; drift in deg C toward delta) ===')
for sysn in ('ECMWF_AIFS_ENS_GLOBAL', 'ECMWF_IFS_ENS_OPEN_PF50'):
    rs = [r for r in inst if r['sys'] == sysn]
    report(f'{sysn} |delta|>=0.3', rs)
    report(f'{sysn} |delta|>=0.7', rs, tau=0.7)
    report(f'{sysn} C stations |delta|>=0.3', [r for r in rs if r['unit'] == 'C'])
    report(f'{sysn} lead 12-48h |delta|>=0.3', [r for r in rs if 12 <= r['lead'] < 48])
json.dump([{k: v for k, v in r.items() if k != 'lad'} for r in inst], open(f'{B}/update_instances.json', 'w'))
