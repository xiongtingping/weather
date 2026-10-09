"""Round 18: one row per off-mode first-taker event with their direction and
every candidate direction signal as of the trade instant.

Direction axis: +1 = warmer than the market mode.  YES off-mode picks point to
sign(d); NO off-mode picks to the other side, -sign(d).

Signals (deg C, + = warmer than the market mean m at the trade):
  pt:<system>   8 point forecasts (points.json, knowable <= ts), station x
                system x lead-day bias removed leave-one-date-out
  ptd:<system>  the same system's latest minus previous run (bias-free)
  om:<model>    Open-Meteo previous_day1/2 daily max (d1 once D-1 08Z is
                past, else d2 once D-2 08Z is past), bias removed LOO
  nat:<system>  native ensemble member mean of the newest run published by
                ts (publication-lag schedule, not archive_query_at_utc --
                backfilled history stamps the backfill instant), bias LOO
  natd:<system> newest minus previous published run of that system
  drift_<h>h    market mean now minus h hours ago (WS snapshots)
  clim          ERA5 same-day normal (+-7 days, 2017-2025) + station offset
                (LOO) minus m
  yday          yesterday's settled high minus m
  served        our served mean minus m
  skew          market mean minus market-mode centre
"""
import gzip, json, sqlite3, bisect, collections, statistics, sys, math
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

ROOT = '/Users/xiong/weather/docs/research/benchmark_account_0x8fbd_20261008'
B = sys.argv[1]
reg = json.load(open(f'{B}/registry.json'))
TZ = {r['station_id']: r['settlement_timezone'] for r in reg}
CEN = {}
for r in reg:
    lo, hi, unit = r['lower_c'], r['upper_c'], r['settlement_unit']
    step = 1.0 if unit == 'C' else 2.0
    if lo is None:
        c = hi - step / 2
    elif hi is None:
        c = lo + step / 2
    else:
        c = (lo + hi) / 2
    CEN[(r['station_id'], r['event_date'], r['outcome'])] = c if unit == 'C' else (c - 32) * 5 / 9

hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
labels = {}
for st, ed, t in hot.execute("select station_id,event_date,settled_tmax_c from basket_station_day_settlement_label l where truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)"):
    labels[(st, ed)] = float(t)

# ---------------------------------------------------------------- events
ev = []
for l in gzip.open(f'{ROOT}/choice_rows.jsonl.gz', 'rt'):
    r = json.loads(l)
    yes = sorted((a for a in r['alts'] if a['side'] == 'Yes'), key=lambda a: a['ord'])
    ch = next(a for a in r['alts'] if a['chosen'])
    d = ch['ord'] - r['mode_ord']
    if d == 0 or r['truth_ord'] not in {a['ord'] for a in yes}:
        continue
    cen = {a['ord']: CEN.get((r['station'], r['event_date'], a['bucket'])) for a in yes}
    if any(v is None for v in cen.values()):
        continue
    s = (1 if d > 0 else -1) * (1 if ch['side'] == 'Yes' else -1)
    m = sum(a['q'] * cen[a['ord']] for a in yes)
    ev.append(dict(station=r['station'], ed=r['event_date'], ts=r['ts'], lead=r['lead_h'], unit=r['unit'], side=ch['side'],
                   d=d, s=s, mode=r['mode_ord'], truth=r['truth_ord'], q={a['ord']: a['q'] for a in yes}, cen=cen, m=m,
                   mode_c=cen[r['mode_ord']], truth_c=r['truth_c'], ytruth=r.get('ytruth_c'), stage=r['stage'],
                   served=sum(a['p'] * cen[a['ord']] for a in yes), usdc=r['usdc']))
print('off-mode events', len(ev), file=sys.stderr)
for e in ev:
    e['sig'] = {'skew': e['m'] - e['mode_c'], 'served': e['served'] - e['m']}
    if e['ytruth'] is not None:
        e['sig']['yday'] = e['ytruth'] - e['m']


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


# ---------------------------------------------------------------- point forecasts
P = json.load(open(f'{B}/points.json'))
pts = collections.defaultdict(list)  # (st, target, sys) -> [(knowable_ts, run_init, tmax)]
for p in P:
    if not p.get('complete'):
        continue
    try:
        kt = datetime.fromisoformat(p['knowable'].replace('Z', '+00:00')).timestamp()
        ri = datetime.fromisoformat(p['run_init'].replace('Z', '+00:00'))
    except Exception:
        continue
    pts[(p['station'], p['target'], p['system'])].append((kt, ri, float(p['tmax'])))
for v in pts.values():
    v.sort()
# bias table: station x system x lead-day -> list of (target, truth - tmax) using the last run of each lead day
bias_pts = collections.defaultdict(list)
for (st, tg, sysn), v in pts.items():
    t = labels.get((st, tg))
    if t is None:
        continue
    byld = {}
    for kt, ri, tm in v:
        ld = (date.fromisoformat(tg) - ri.date()).days
        byld[ld] = tm
    for ld, tm in byld.items():
        bias_pts[(st, sysn, ld)].append((tg, t - tm))


def loo(table, key, tg):
    vals = [b for g, b in table.get(key, []) if g != tg]
    return statistics.mean(vals) if len(vals) >= 5 else None


for e in ev:
    for sysn in {k[2] for k in pts if k[0] == e['station'] and k[1] == e['ed']}:
        v = [x for x in pts[(e['station'], e['ed'], sysn)] if x[0] <= e['ts']]
        if not v:
            continue
        kt, ri, tm = v[-1]
        ld = (date.fromisoformat(e['ed']) - ri.date()).days
        b = loo(bias_pts, (e['station'], sysn, ld), e['ed'])
        if b is not None:
            e['sig'][f'pt:{sysn}'] = tm + b - e['m']
        prev = [x for x in v[:-1] if x[1] < ri]
        if prev:
            e['sig'][f'ptd:{sysn}'] = tm - prev[-1][2]
print('points done', file=sys.stderr)

# ---------------------------------------------------------------- Open-Meteo previous runs
OM = json.load(open(f'{B}/prev_global.json'))
OM.update({k: v for k, v in json.load(open(f'{B}/prev_regional.json')).items() if 'error' not in v})
om_daily = {}  # (st, model) -> {local_date: (max_d1, max_d2)}
for key, v in OM.items():
    if 'time' not in v:
        continue
    st, model = key.split('|')
    dd = collections.defaultdict(lambda: [None, None])
    for t, a, b2 in zip(v['time'], v.get('d1') or [], v.get('d2') or []):
        day = t[:10]
        if a is not None:
            dd[day][0] = a if dd[day][0] is None else max(dd[day][0], a)
        if b2 is not None:
            dd[day][1] = b2 if dd[day][1] is None else max(dd[day][1], b2)
    om_daily[(st, model)] = dict(dd)
bias_om = collections.defaultdict(list)
for (st, model), dd in om_daily.items():
    for day, (a, b2) in dd.items():
        t = labels.get((st, day))
        if t is None:
            continue
        if a is not None:
            bias_om[(st, model, 1)].append((day, t - a))
        if b2 is not None:
            bias_om[(st, model, 2)].append((day, t - b2))
for e in ev:
    D = datetime.fromisoformat(e['ed']).replace(tzinfo=timezone.utc)
    use1 = e['ts'] >= (D - timedelta(days=1) + timedelta(hours=8)).timestamp()
    use2 = e['ts'] >= (D - timedelta(days=2) + timedelta(hours=8)).timestamp()
    for (st, model), dd in om_daily.items():
        if st != e['station'] or e['ed'] not in dd:
            continue
        a, b2 = dd[e['ed']]
        if use1 and a is not None:
            val, lag = a, 1
        elif use2 and b2 is not None:
            val, lag = b2, 2
        else:
            continue
        b = loo(bias_om, (st, model, lag), e['ed'])
        if b is not None:
            e['sig'][f'om:{model}'] = val + b - e['m']
print('open-meteo done', file=sys.stderr)

# ---------------------------------------------------------------- native ensembles
LAG = {'ECMWF_IFS_ENS_OPEN_PF50': 8.2, 'NOAA_GEFS_05': 4.6, 'DWD_ICON_GLOBAL_EPS': 3.6, 'ECCC_GEM_GLOBAL_ENS': 5.5,
       'ECMWF_AIFS_ENS_GLOBAL': 6.3, 'NOAA_AIGEFS_025': 7.2}
nat = collections.defaultdict(dict)  # (st, target) -> {(system, run_init): mean}
stations = sorted({e['station'] for e in ev})
for st in stations:
    for tg, sysn, ri, mu in hot.execute(
            "select target_local_date, ensemble_system_id, native_run_init_utc, avg(raw_tmax_c) "
            "from calibration_native_ensemble_historical_member_run where station_id=? and target_local_date between '2026-08-20' and '2026-10-09' "
            "and qc_status='ACCEPTED' and native_run_init_utc is not null group by 1,2,3", (st,)):
        if sysn in LAG:
            nat[(st, tg)][(sysn, ri)] = mu
print('native pulled', len(nat), file=sys.stderr)
bias_nat = collections.defaultdict(list)
for (st, tg), runs in nat.items():
    t = labels.get((st, tg))
    if t is None:
        continue
    for (sysn, ri), mu in runs.items():
        ld = (date.fromisoformat(tg) - date.fromisoformat(ri[:10])).days
        bias_nat[(st, sysn, ld, ri[11:13])].append((tg, t - mu))
for e in ev:
    runs = nat.get((e['station'], e['ed']), {})
    by = collections.defaultdict(list)
    for (sysn, ri), mu in runs.items():
        avail = datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + LAG[sysn] * 3600
        if avail <= e['ts']:
            by[sysn].append((ri, mu))
    for sysn, lst in by.items():
        lst.sort()
        ri, mu = lst[-1]
        ld = (date.fromisoformat(e['ed']) - date.fromisoformat(ri[:10])).days
        b = loo(bias_nat, (e['station'], sysn, ld, ri[11:13]), e['ed'])
        if b is not None:
            e['sig'][f'nat:{sysn}'] = mu + b - e['m']
        if len(lst) >= 2:
            e['sig'][f'natd:{sysn}'] = mu - lst[-2][1]
print('native done', file=sys.stderr)

# ---------------------------------------------------------------- climatology
for st in stations:
    clim = collections.defaultdict(list)
    for ld, hi in hot.execute("select local_date, daily_high_c from calibration_station_same_day_climatology where station_id=? and local_date<'2026-01-01'", (st,)):
        clim[ld[5:]].append(hi)
    def normal(day):
        dt = date.fromisoformat(day)
        vals = []
        for k in range(-7, 8):
            vals += clim.get((dt + timedelta(days=k)).isoformat()[5:], [])
        return statistics.mean(vals) if vals else None
    offs = [(tg, labels[(st, tg)] - normal(tg)) for (s2, tg) in labels if s2 == st and normal(tg) is not None and tg >= '2026-08-20']
    for e in ev:
        if e['station'] != st:
            continue
        n = normal(e['ed'])
        o = [b for g, b in offs if g != e['ed']]
        if n is not None and len(o) >= 5:
            e['sig']['clim'] = n + statistics.mean(o) - e['m']
print('climatology done', file=sys.stderr)

# ---------------------------------------------------------------- market drift
cold = sqlite3.connect('file:/Volumes/weather_data/weather_cold/ssot_rows/basket_official_ws_snapshot.sqlite?mode=ro&immutable=1', uri=True)
COLD = {}


def cold_idx(ed):
    if ed not in COLD:
        COLD[ed] = collections.defaultdict(list)
        for st, oa, rid in cold.execute("select station_id, observed_at, rowid from basket_official_ws_snapshot where event_date=? and state='COMMITTED'", (ed,)):
            COLD[ed][st].append((oa, rid))
        for v in COLD[ed].values():
            v.sort()
    return COLD[ed]


def ladder_mean(st, ed, ts, cen):
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
    Bk = json.loads(row[0])
    mids = {}
    for b in Bk:
        if b.get('bid_price') is not None and b.get('best_ask') is not None and 0 < b['best_ask'] - b['bid_price'] < 0.2:
            mids[b['ordinal']] = (b['bid_price'] + b['best_ask']) / 2
    s = sum(mids.values())
    if len(mids) < 3 or not 0.85 <= s <= 1.2 or any(o not in cen for o in mids):
        return None
    return sum(v / s * cen[o] for o, v in mids.items())


for i, e in enumerate(ev):
    for h in (1, 3, 6, 12, 24):
        past = ladder_mean(e['station'], e['ed'], e['ts'] - h * 3600, e['cen'])
        if past is not None:
            e['sig'][f'drift_{h}h'] = e['m'] - past
print('drift done', file=sys.stderr)

out = []
for e in ev:
    out.append({k: e[k] for k in ('station', 'ed', 'ts', 'lead', 'unit', 'side', 'd', 's', 'mode', 'truth', 'q', 'm', 'mode_c', 'truth_c', 'stage', 'usdc', 'sig')})
    out[-1]['q'] = {str(k): v for k, v in e['q'].items()}
json.dump(out, open(f'{B}/direction_rows.json', 'w'))
print('wrote', len(out), file=sys.stderr)
