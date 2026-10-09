"""Round 21 part 2 (user 10-10 「加入实时云雨、辐射或者taf等信息」): weather-state
signals as direction candidates, all as of the trade instant.  Sign: + = warmer
than the market (or than the station's usual), so cloud/rain are entered with
a minus sign.

TAF (calibration_forward_taf_capture, event_date = D, received <= trade):
  taf_tx       newest TX + station LOO bias (truth - TX) - market mean
  taf_tx_upd   newest TX - previous TX
  taf_cloud    -(newest cloud fraction - station mean over other dates)
  taf_rain     -(newest precip probability - station mean)
  taf_cloud_upd / taf_rain_upd   -(newest - previous)
Hourly precip forecast versions (Open-Meteo, captured <= trade, local_date D):
  pfc_before   -(rain before the peak hour - station mean)
  pfc_wet      -(wet hours - station mean)
  pfc_upd      -(newest rain total - previous capture's)
Live METAR (archive raw text, first seen <= trade, last 3 h):
  obs_cloud    -(max layer cover - station mean at that local hour band)
  obs_rain     -1 if precipitation is being reported now, else 0 (n/a: none)
  obs_anom     observed temp now - ECMWF IFS previous_day1 forecast for that
               hour (Open-Meteo), minus the station's mean of that difference
"""
import json, sqlite3, bisect, collections, random, statistics, re, sys
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
B = sys.argv[1]
random.seed(2121)
R = json.load(open(f'{B}/direction_rows.json'))
Mm = json.load(open(f'{B}/momentum_M.json'))
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}
    r['M'] = Mm.get(f'{r["station"]}|{r["ed"]}|{r["ts"]}')
reg = json.load(open(f'{B}/registry.json'))
TZ = {x['station_id']: x['settlement_timezone'] for x in reg}
ssot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
ssot.execute('PRAGMA query_only=1')
arch = sqlite3.connect('file:/Users/xiong/weather/data/archive.db?mode=ro', uri=True, timeout=30)
arch.execute('PRAGMA query_only=1')
labels = {}
for st, ed, t in ssot.execute("select station_id,event_date,settled_tmax_c from basket_station_day_settlement_label l where truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)"):
    labels[(st, ed)] = float(t)
ts_of = lambda s: datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
stations = sorted({r['station'] for r in R})

# ---------------------------------------------------------------- TAF
taf = collections.defaultdict(list)
for st, ed, rec, tx, txa, cc, pp in ssot.execute("select station_id, event_date, received_at_utc, tx_c, tx_available, cloud_cover_fraction, precipitation_probability "
                                                 "from calibration_forward_taf_capture where event_date between '2026-09-01' and '2026-10-09'"):
    taf[(st, ed)].append((ts_of(rec), tx if txa else None, cc, pp))
for v in taf.values():
    v.sort()
# station climatologies of TAF cloud/rain (final capture per date) and TX bias
taf_final = {k: v[-1] for k, v in taf.items()}
def loo_mean(pairs, ed):
    vals = [x for d, x in pairs if d != ed and x is not None]
    return statistics.mean(vals) if len(vals) >= 4 else None
cl_by_st = collections.defaultdict(list); rn_by_st = collections.defaultdict(list); txb_by_st = collections.defaultdict(list)
for (st, ed), (t, tx, cc, pp) in taf_final.items():
    cl_by_st[st].append((ed, cc)); rn_by_st[st].append((ed, pp))
    if tx is not None and (st, ed) in labels:
        txb_by_st[st].append((ed, labels[(st, ed)] - tx))

# ---------------------------------------------------------------- precip forecast versions
pfc = collections.defaultdict(list)
for st, ld, cap, tot, before, wet in ssot.execute("select station_id, local_date, captured_at_utc, precip_sum_mm, precip_before_peak_mm, wet_hours from calibration_station_forecast_precip_hourly_version "
                                                  "where local_date between '2026-09-01' and '2026-10-09'"):
    pfc[(st, ld)].append((ts_of(cap), tot, before, wet))
for v in pfc.values():
    v.sort()
pb_by_st = collections.defaultdict(list); pw_by_st = collections.defaultdict(list)
for (st, ld), v in pfc.items():
    pb_by_st[st].append((ld, v[-1][2])); pw_by_st[st].append((ld, v[-1][3]))

# ---------------------------------------------------------------- METAR
COVER = {'FEW': 0.19, 'SCT': 0.44, 'BKN': 0.75, 'OVC': 1.0, 'VV': 1.0}
RAIN = re.compile(r'(?:^|\s)[+-]?(?:VC)?(?:TS|SH|FZ)?(?:RA|DZ|SN|GR|GS|PL|SG|UP)+(?:\s|$)')
metar = {}
for st in stations:
    rows = []
    for valid, seen, raw in arch.execute("select valid_at, first_seen_at, raw_text from reported_trajectory_observations where station_id=? and valid_at between '2026-09-15' and '2026-10-09' and raw_text is not null", (st,)):
        try:
            body = raw.split(' RMK')[0]
            cov = 0.0 if re.search(r'\b(CAVOK|NSC|SKC|CLR|NCD)\b', body) else max([COVER[m] for m in re.findall(r'\b(FEW|SCT|BKN|OVC|VV)\d{3}', body)] or [0.0])
            rain = 1 if RAIN.search(' ' + body.split(' Q')[0].split(' A3')[0] + ' ') else 0
            tm = re.search(r'\s(M?\d{2})/(M?\d{2})?\s', body)
            temp = None if not tm else float(tm.group(1).replace('M', '-'))
            rows.append((ts_of(seen or valid), ts_of(valid), cov, rain, temp))
        except Exception:
            continue
    rows.sort()
    metar[st] = rows
# station mean cover by 6-hour local band
cov_mean = collections.defaultdict(list)
for st, rows in metar.items():
    z = ZoneInfo(TZ[st])
    for seen, valid, cov, rain, temp in rows:
        cov_mean[(st, datetime.fromtimestamp(valid, z).hour // 6)].append(cov)
cov_mean = {k: statistics.mean(v) for k, v in cov_mean.items() if len(v) >= 20}
# ECMWF IFS previous_day1 hourly forecast for the current hour (Open-Meteo)
OM = json.load(open(f'{B}/prev_global.json'))
fc_hour = {}
for st in stations:
    v = OM.get(f'{st}|ecmwf_ifs025')
    if v and 'time' in v:
        fc_hour[st] = {t: x for t, x in zip(v['time'], v['d1']) if x is not None}
anom_hist = collections.defaultdict(list)
for st, rows in metar.items():
    z = ZoneInfo(TZ[st]); fh = fc_hour.get(st, {})
    for seen, valid, cov, rain, temp in rows:
        key = datetime.fromtimestamp(valid, z).strftime('%Y-%m-%dT%H:00')
        if temp is not None and key in fh:
            anom_hist[st].append((key[:10], temp - fh[key]))

for r in R:
    st, ed, ts = r['station'], r['ed'], r['ts']
    g = r.setdefault('wx', {})
    v = [x for x in taf.get((st, ed), []) if x[0] <= ts]
    if v:
        t0, tx, cc, pp = v[-1]
        b = loo_mean(txb_by_st[st], ed)
        if tx is not None and b is not None:
            g['taf_tx'] = tx + b - r['m']
        txs = [x[1] for x in v if x[1] is not None]
        if len(txs) >= 2:
            g['taf_tx_upd'] = txs[-1] - txs[-2]
        mc = loo_mean(cl_by_st[st], ed); mr = loo_mean(rn_by_st[st], ed)
        if cc is not None and mc is not None:
            g['taf_cloud'] = -(cc - mc)
        if pp is not None and mr is not None:
            g['taf_rain'] = -(pp - mr)
        if len(v) >= 2:
            if v[-1][2] is not None and v[-2][2] is not None:
                g['taf_cloud_upd'] = -(v[-1][2] - v[-2][2])
            if v[-1][3] is not None and v[-2][3] is not None:
                g['taf_rain_upd'] = -(v[-1][3] - v[-2][3])
    w = [x for x in pfc.get((st, ed), []) if x[0] <= ts]
    if w:
        mb = loo_mean(pb_by_st[st], ed); mw = loo_mean(pw_by_st[st], ed)
        if w[-1][2] is not None and mb is not None:
            g['pfc_before'] = -(w[-1][2] - mb)
        if w[-1][3] is not None and mw is not None:
            g['pfc_wet'] = -(w[-1][3] - mw)
        if len(w) >= 2 and w[-1][1] is not None and w[-2][1] is not None:
            g['pfc_upd'] = -(w[-1][1] - w[-2][1])
    rows = metar.get(st, [])
    i = bisect.bisect_right([x[0] for x in rows], ts) - 1
    if i >= 0 and ts - rows[i][1] <= 3 * 3600:
        seen, valid, cov, rain, temp = rows[i]
        z = ZoneInfo(TZ[st])
        cm = cov_mean.get((st, datetime.fromtimestamp(valid, z).hour // 6))
        if cm is not None:
            g['obs_cloud'] = -(cov - cm)
        g['obs_rain'] = -float(rain) if rain else None
        key = datetime.fromtimestamp(valid, z).strftime('%Y-%m-%dT%H:00')
        fh = fc_hour.get(st, {})
        if temp is not None and key in fh:
            hist = [a for d, a in anom_hist[st] if d != ed]
            if len(hist) >= 30:
                g['obs_anom'] = (temp - fh[key]) - statistics.mean(hist)
    g = {k: x for k, x in g.items() if x is not None}
    r['wx'] = g


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


TAU = {'taf_tx': 0.3, 'taf_tx_upd': 0.3, 'taf_cloud': 0.15, 'taf_rain': 0.1, 'taf_cloud_upd': 0.1, 'taf_rain_upd': 0.1,
       'pfc_before': 0.5, 'pfc_wet': 1.0, 'pfc_upd': 0.5, 'obs_cloud': 0.2, 'obs_rain': 0.5, 'obs_anom': 0.7}
sgn = lambda x: 1 if x > 0 else -1
for sub_name, sub in (('all off-mode', R), ('quiet |M|<0.5', [r for r in R if r['M'] is None or abs(r['M']) < 0.5]),
                      ('lead < 18h', [r for r in R if r['lead'] < 18])):
    print(f'=== {sub_name}: n={len(sub)} ===')
    for k, tau in TAU.items():
        sel = [dict(r, _x=r['wx'][k]) for r in sub if k in r['wx'] and abs(r['wx'][k]) >= tau]
        if len(sel) < 30:
            print(f'   {k:14s} n={len(sel)}'); continue
        ag = sum(1 for r in sel if sgn(r['_x']) == r['s']) / len(sel)
        alo, ahi = boot(sel, lambda r: 1.0 if sgn(r['_x']) == r['s'] else 0.0)
        sk = sum(dscore(r, sgn(r['_x'])) for r in sel) / len(sel)
        slo, shi = boot(sel, lambda r: dscore(r, sgn(r['_x'])))
        A = [r for r in sel if sgn(r['_x']) == r['s']]; D = [r for r in sel if sgn(r['_x']) != r['s']]
        print(f'   {k:14s} n={len(sel):4d} agree {ag:.3f} [{alo:.3f},{ahi:.3f}] skill {sk:+.3f} [{slo:+.3f},{shi:+.3f}]  theirs agree/disagree {sum(dscore(r, r["s"]) for r in A)/max(1,len(A)):+.3f}/{sum(dscore(r, r["s"]) for r in D)/max(1,len(D)):+.3f}')
json.dump({f'{r["station"]}|{r["ed"]}|{r["ts"]}': r['wx'] for r in R}, open(f'{B}/wx_signals.json', 'w'))
