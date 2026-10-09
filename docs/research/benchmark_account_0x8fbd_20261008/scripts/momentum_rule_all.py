"""Round 19 part 3: the unfitted ECMWF momentum M as our own rule on every
station-day.  At t = each AIFS ENS run availability + 1 h (6-60 h before the
local day ends), M from the four components as of t (natives by lag schedule
+1 h, points by knowable stamps), component std from round 19 part 2.
Trades at the WS snapshot at t: YES on the mode neighbour on the M side at
best ask, and as a maker at best bid + 0.01 assumed filled (upper bound).
Direction score as before.  Also their silent-event skill with a CI.
"""
import json, sqlite3, bisect, collections, random, statistics, sys
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
B = sys.argv[1]
random.seed(19019)
SD = {'AIFS ENS d1': 0.369, 'IFS ENS d1': 0.383, 'HRES d1': 0.676, 'AIFS single d2': 0.473}
reg = json.load(open(f'{B}/registry.json'))
TZ = {x['station_id']: x['settlement_timezone'] for x in reg}
hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
cold = sqlite3.connect('file:/Volumes/weather_data/weather_cold/ssot_rows/basket_official_ws_snapshot.sqlite?mode=ro&immutable=1', uri=True)
labels = {}
for st, ed, b in hot.execute("select station_id,event_date,settled_bucket from basket_station_day_settlement_label l where truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)"):
    labels[(st, ed)] = b
LAG = {'ECMWF_AIFS_ENS_GLOBAL': 6.3, 'ECMWF_IFS_ENS_OPEN_PF50': 8.2}
P = json.load(open(f'{B}/points.json'))
pts = collections.defaultdict(list)
for p in P:
    if p.get('complete') and p['system'] in ('ECMWF_IFS_9KM_RELAY_POINT', 'ECMWF_AIFS_SINGLE_POINT'):
        pts[(p['station'], p['target'], p['system'])].append((datetime.fromisoformat(p['knowable'].replace('Z', '+00:00')).timestamp(), p['run_init'], float(p['tmax'])))
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


inst = []
DATES = [(date(2026, 9, 22) + timedelta(days=i)).isoformat() for i in range(17)]
for st in sorted({x['station_id'] for x in reg if x['event_date'] in DATES}):
    zone = ZoneInfo(TZ[st])
    nat = collections.defaultdict(dict)
    for tg, sysn, ri, mu in hot.execute(
            "select target_local_date, ensemble_system_id, native_run_init_utc, avg(raw_tmax_c) from calibration_native_ensemble_historical_member_run "
            "where station_id=? and target_local_date between '2026-09-22' and '2026-10-08' and qc_status='ACCEPTED' and native_run_init_utc is not null "
            "and ensemble_system_id in ('ECMWF_AIFS_ENS_GLOBAL','ECMWF_IFS_ENS_OPEN_PF50') group by 1,2,3", (st,)):
        nat[(tg, sysn)][ri] = mu
    for tg in DATES:
        if (st, tg) not in labels:
            continue
        day_end = datetime.combine(date.fromisoformat(tg) + timedelta(days=1), datetime.min.time(), tzinfo=zone).timestamp()
        for ri in sorted(nat.get((tg, 'ECMWF_AIFS_ENS_GLOBAL'), {})):
            t = datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG['ECMWF_AIFS_ENS_GLOBAL'] + 1) * 3600
            lead = (day_end - t) / 3600
            if not 6 <= lead <= 60:
                continue
            comps = {}
            for sysn, name in (('ECMWF_AIFS_ENS_GLOBAL', 'AIFS ENS d1'), ('ECMWF_IFS_ENS_OPEN_PF50', 'IFS ENS d1')):
                v = [mu for r2, mu in sorted(nat.get((tg, sysn), {}).items()) if datetime.fromisoformat(r2.replace('Z', '+00:00')).timestamp() + (LAG[sysn] + 1) * 3600 <= t]
                if len(v) >= 2:
                    comps[name] = v[-1] - v[-2]
            for sysn, name, k in (('ECMWF_IFS_9KM_RELAY_POINT', 'HRES d1', 2), ('ECMWF_AIFS_SINGLE_POINT', 'AIFS single d2', 3)):
                seen = {}
                for kt, r2, tm in sorted(pts.get((st, tg, sysn), [])):
                    if kt <= t:
                        seen[r2] = tm
                v = [seen[x] for x in sorted(seen)]
                if len(v) >= k:
                    comps[name] = v[-1] - v[-k]
            if not comps:
                continue
            M = sum(v / SD[n] for n, v in comps.items())
            lad = ladder(st, tg, t)
            if not lad:
                continue
            inst.append(dict(st=st, ed=tg, t=t, lead=lead, M=M, n=len(comps), lad=lad, truth=labels[(st, tg)],
                             unit='F' if '°F' in next(iter(lad[0])) else 'C'))
print('instances', len(inst), file=sys.stderr)

# ---- round 21: ICON forecast radiation anomaly (Open-Meteo previous runs), as of t
SW = json.load(open(f'{B}/prev_sw.json'))
RAD = {}
for key, v in SW.items():
    if 'time' not in v or not key.endswith('|icon_global'):
        continue
    st = key.split('|')[0]
    acc = collections.defaultdict(lambda: [0.0, 0.0, 0, 0])
    for tt, a, b2 in zip(v['time'], v['d1'], v['d2']):
        if 9 <= int(tt[11:13]) <= 16:
            if a is not None: acc[tt[:10]][0] += a; acc[tt[:10]][2] += 1
            if b2 is not None: acc[tt[:10]][1] += b2; acc[tt[:10]][3] += 1
    RAD[st] = {d: (x[0] if x[2] == 8 else None, x[1] if x[3] == 8 else None) for d, x in acc.items()}
for r in inst:
    dd = RAD.get(r['st'], {})
    r['rad'] = None
    if r['ed'] not in dd:
        continue
    D = datetime.fromisoformat(r['ed']).replace(tzinfo=timezone.utc)
    use1 = r['t'] >= (D - timedelta(days=1) + timedelta(hours=8)).timestamp()
    use2 = r['t'] >= (D - timedelta(days=2) + timedelta(hours=8)).timestamp()
    s1, s2 = dd[r['ed']]
    val, lag = (s1, 0) if use1 and s1 is not None else (s2, 1) if use2 and s2 is not None else (None, None)
    if val is None:
        continue
    others = [x[lag] for d2, x in dd.items() if d2 != r['ed'] and x[lag] is not None]
    if len(others) >= 6:
        r['rad'] = (val - statistics.mean(others)) / (statistics.pstdev(others) or 1.0)


def ev(r, side=None):
    side = side if side is not None else (1 if r['M'] > 0 else -1)
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
    res = dict(score=D - ED)
    for bk, (o, bid, ask) in out.items():
        if o == mode + side:
            hit = 1.0 if tord == o else 0.0
            res['taker'] = hit - ask
            res['maker'] = hit - min(bid + 0.01, ask)
            res['q_nb'] = q[o]
    return res


def boot(vals_by, n=400):
    keys = list(vals_by); out = []
    for _ in range(n):
        s = [v for k in (random.choice(keys) for _ in keys) for v in vals_by[k]]
        out.append(sum(s) / len(s))
    out.sort()
    return out[int(.025 * n)], out[int(.975 * n) - 1]


print('=== ECMWF momentum M as a rule on every station-day (AIFS ENS availability + 1h) ===')
for label, flt in (('|M|>=1.0', lambda r: abs(r['M']) >= 1.0), ('|M|>=1.5', lambda r: abs(r['M']) >= 1.5), ('|M|>=2.0', lambda r: abs(r['M']) >= 2.0),
                   ('|M|>=1.5, C, lead 12-48h', lambda r: abs(r['M']) >= 1.5 and r['unit'] == 'C' and 12 <= r['lead'] < 48),
                   ('|M|>=1.5, neighbour priced 0.10-0.45', lambda r: abs(r['M']) >= 1.5)):
    rows = [(r, ev(r)) for r in inst if flt(r)]
    rows = [(r, e) for r, e in rows if e is not None and (label != '|M|>=1.5, neighbour priced 0.10-0.45' or (e.get('q_nb') is not None and 0.10 <= e['q_nb'] <= 0.45))]
    parts = []
    for key, sc in (('score', 1), ('taker', 100), ('maker', 100)):
        G = collections.defaultdict(list)
        for r, e in rows:
            if e.get(key) is not None:
                G[(r['st'], r['ed'])].append(e[key])
        vals = [v for g in G.values() for v in g]
        if not vals:
            continue
        lo, hi = boot(G)
        h1 = [e[key] for r, e in rows if e.get(key) is not None and r['ed'] < '2026-09-30']
        h2 = [e[key] for r, e in rows if e.get(key) is not None and r['ed'] >= '2026-09-30']
        parts.append(f'{key} {sc*sum(vals)/len(vals):+.3f} [{sc*lo:+.3f},{sc*hi:+.3f}] H1/H2 {sc*sum(h1)/max(1,len(h1)):+.3f}/{sc*sum(h2)/max(1,len(h2)):+.3f}')
    print(f'   {label:40s} n={len(rows):5d}  ' + ' | '.join(parts))

print('=== round 21: ICON radiation anomaly and the momentum combination on every station-day ===')
def rule_report(label, rows_sides):
    rows = [(r, ev(r, sd)) for r, sd in rows_sides]
    rows = [(r, e) for r, e in rows if e is not None]
    parts = []
    for key, sc in (('score', 1), ('taker', 100), ('maker', 100)):
        G = collections.defaultdict(list)
        for r, e in rows:
            if e.get(key) is not None:
                G[(r['st'], r['ed'])].append(e[key])
        vals = [v for g in G.values() for v in g]
        if not vals:
            continue
        lo, hi = boot(G)
        h1 = [e[key] for r, e in rows if e.get(key) is not None and r['ed'] < '2026-09-30']
        h2 = [e[key] for r, e in rows if e.get(key) is not None and r['ed'] >= '2026-09-30']
        parts.append(f'{key} {sc*sum(vals)/len(vals):+.3f} [{sc*lo:+.3f},{sc*hi:+.3f}] H1/H2 {sc*sum(h1)/max(1,len(h1)):+.3f}/{sc*sum(h2)/max(1,len(h2)):+.3f}')
    print(f'   {label:44s} n={len(rows):5d}  ' + ' | '.join(parts))
sg = lambda x: 1 if x > 0 else -1
rule_report('ICON rad |z|>=1.0', [(r, sg(r['rad'])) for r in inst if r['rad'] is not None and abs(r['rad']) >= 1.0])
rule_report('ICON rad |z|>=1.5', [(r, sg(r['rad'])) for r in inst if r['rad'] is not None and abs(r['rad']) >= 1.5])
rule_report('M>=1 and ICON rad z>=0.5 same side', [(r, sg(r['M'])) for r in inst if r['rad'] is not None and abs(r['M']) >= 1 and abs(r['rad']) >= 0.5 and sg(r['M']) == sg(r['rad'])])
rule_report('M>=1.5 and ICON rad z>=1 same side', [(r, sg(r['M'])) for r in inst if r['rad'] is not None and abs(r['M']) >= 1.5 and abs(r['rad']) >= 1 and sg(r['M']) == sg(r['rad'])])
rule_report('M>=1 and ICON rad opposite', [(r, sg(r['M'])) for r in inst if r['rad'] is not None and abs(r['M']) >= 1 and abs(r['rad']) >= 0.5 and sg(r['M']) != sg(r['rad'])])

# their silent-event skill with CI
R = json.load(open(f'{B}/direction_rows.json'))
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}
Mm = json.load(open(f'{B}/momentum_M.json'))
def dscore(r, side):
    sd = lambda o: (o - r['mode']) * side
    D = 1 if sd(r['truth']) > 0 else -1 if sd(r['truth']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in r['q'].items())
quiet = [r for r in R if Mm.get(f'{r["station"]}|{r["ed"]}|{r["ts"]}') is None or abs(Mm[f'{r["station"]}|{r["ed"]}|{r["ts"]}']) < 0.5]
G = collections.defaultdict(list)
for r in quiet: G[(r['station'], r['ed'])].append(dscore(r, r['s']))
lo, hi = boot(G, 600)
print(f'\ntheir skill where momentum is quiet (|M|<0.5 or none): n={len(quiet)} {sum(v for g in G.values() for v in g)/len(quiet):+.3f} [{lo:+.3f},{hi:+.3f}]')
for lab, f in (('lead <12h', lambda r: r['lead'] < 12), ('lead 12-48h', lambda r: 12 <= r['lead'] < 48), ('lead >=48h', lambda r: r['lead'] >= 48)):
    sub = [r for r in quiet if f(r)]
    if sub:
        print(f'   {lab:12s} n={len(sub):4d} {sum(dscore(r, r["s"]) for r in sub)/len(sub):+.3f}')
