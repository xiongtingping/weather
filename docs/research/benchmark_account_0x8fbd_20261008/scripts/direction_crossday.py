"""Round 20 part 2: cross-day relative pricing.

X = (model mean D - model mean D-1) - (market mean D - market mean D-1), with
both model means from the same newest run of the same system (bias cancels)
and both market means from WS snapshots at the trade instant.  Also against
D+1.  + means the D market is too cold relative to its neighbour day.
"""
import json, gzip, sqlite3, bisect, collections, statistics, sys
from datetime import datetime, timezone, timedelta, date
B = sys.argv[1]
ROOT = '/Users/xiong/weather/docs/research/benchmark_account_0x8fbd_20261008'
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
LAG = {'ECMWF_AIFS_ENS_GLOBAL': 6.3, 'ECMWF_IFS_ENS_OPEN_PF50': 8.2, 'NOAA_GEFS_05': 4.6, 'DWD_ICON_GLOBAL_EPS': 3.6}
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


runs = {}


def model_means(st, tg):
    if (st, tg) not in runs:
        d = {}
        for sysn, ri, mu in hot.execute("select ensemble_system_id, native_run_init_utc, avg(raw_tmax_c) from calibration_native_ensemble_historical_member_run "
                                        "where station_id=? and target_local_date=? and qc_status='ACCEPTED' and native_run_init_utc is not null group by 1,2", (st, tg)):
            if sysn in LAG:
                d[(sysn, ri)] = mu
        runs[(st, tg)] = d
    return runs[(st, tg)]


ev = []
Mm = json.load(open(f'{B}/momentum_M.json'))
for l in gzip.open(f'{ROOT}/choice_rows.jsonl.gz', 'rt'):
    r = json.loads(l)
    yes = sorted((a for a in r['alts'] if a['side'] == 'Yes'), key=lambda a: a['ord'])
    ch = next(a for a in r['alts'] if a['chosen'])
    d = ch['ord'] - r['mode_ord']
    if d == 0 or r['truth_ord'] not in {a['ord'] for a in yes}:
        continue
    ev.append(dict(st=r['station'], ed=r['event_date'], ts=r['ts'], lead=r['lead_h'], mode=r['mode_ord'], truth=r['truth_ord'],
                   q={a['ord']: a['q'] for a in yes}, s=(1 if d > 0 else -1) * (1 if ch['side'] == 'Yes' else -1), M=Mm.get(f'{r["station"]}|{r["event_date"]}|{r["ts"]}')))
for e in ev:
    e['x'] = {}
    mD = mmean(e['st'], e['ed'], e['ts'])
    if mD is None:
        continue
    for k, lab in ((-1, 'prev day'), (1, 'next day')):
        other = (date.fromisoformat(e['ed']) + timedelta(days=k)).isoformat()
        mO = mmean(e['st'], other, e['ts'])
        if mO is None:
            continue
        a, b = model_means(e['st'], e['ed']), model_means(e['st'], other)
        for sysn in LAG:
            common = sorted(ri for (s2, ri) in a if s2 == sysn and (s2, ri) in b and datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG[sysn] + 1) * 3600 <= e['ts'])
            if not common:
                continue
            ri = common[-1]
            e['x'][f'{lab}:{sysn}'] = (a[(sysn, ri)] - b[(sysn, ri)]) - (mD - mO)
    if e['x']:
        for lab in ('prev day', 'next day'):
            v = [x for k2, x in e['x'].items() if k2.startswith(lab)]
            if len(v) >= 2:
                e['x'][f'{lab}:mean'] = statistics.mean(v)


def dscore(e, side):
    sd = lambda o: (o - e['mode']) * side
    D = 1 if sd(e['truth']) > 0 else -1 if sd(e['truth']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in e['q'].items())


sgn = lambda x: 1 if x > 0 else -1
keys = sorted({k for e in ev for k in e['x']})
for sub_name, sub in (('all off-mode', ev), ('quiet |M|<0.5', [e for e in ev if e['M'] is None or abs(e['M']) < 0.5])):
    print(f'=== {sub_name} ===')
    for k in keys:
        sel = [(e, e['x'][k]) for e in sub if k in e['x'] and abs(e['x'][k]) >= 0.4]
        if len(sel) < 30:
            continue
        ag = sum(1 for e, x in sel if sgn(x) == e['s']) / len(sel)
        sk = sum(dscore(e, sgn(x)) for e, x in sel) / len(sel)
        A = [e for e, x in sel if sgn(x) == e['s']]; D = [e for e, x in sel if sgn(x) != e['s']]
        print(f'   {k:40s} n={len(sel):4d} agree {ag:.3f}  cross-day skill {sk:+.3f}  theirs agree/disagree {sum(dscore(e, e["s"]) for e in A)/max(1,len(A)):+.3f}/{sum(dscore(e, e["s"]) for e in D)/max(1,len(D)):+.3f}')
