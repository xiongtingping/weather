"""Round 20: forward check of the ECMWF momentum on the cluster's trades the
copy-follow lane has recorded since 2026-10-08 16:20Z (copy_follow_source_trades,
taker BUYs, all 8 wallets).  Episodes: first fill per (station, date, bucket,
side), a new episode after 30 minutes without a fill.  Direction from the WS
snapshot at the fill (off-mode only).  Momentum M exactly as round 19 (same
component scales, natives by publication schedule + 1 h, points by knowable
stamps).  Agreement needs no truth; skill is scored where truth has settled.
"""
import json, sqlite3, collections, sys, statistics
from datetime import datetime, timezone
SD = {'AIFS ENS d1': 0.369, 'IFS ENS d1': 0.383, 'HRES d1': 0.676, 'AIFS single d2': 0.473}
LAG = {'ECMWF_AIFS_ENS_GLOBAL': 6.3, 'ECMWF_IFS_ENS_OPEN_PF50': 8.2}
hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
labels = {}
for st, ed, b in hot.execute("select station_id,event_date,settled_bucket from basket_station_day_settlement_label l where event_date>='2026-10-07' and truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)"):
    labels[(st, ed)] = b
T = hot.execute("select source_ts, station_id, event_date, bucket, outcome_side, usdc_size, source_user from copy_follow_source_trades "
                "where event_date>='2026-10-08' and order_action='BUY' and taker=1 and station_id is not null order by source_ts").fetchall()
groups = collections.defaultdict(list)
for ts, st, ed, bk, side, usdc, user in T:
    groups[(st, ed, bk, side)].append((ts, usdc or 0.0, user))
episodes = []
for key, fills in groups.items():
    last = None
    for ts, usdc, user in fills:
        if last is None or ts - last > 1800:
            episodes.append(dict(st=key[0], ed=key[1], bucket=key[2], side=key[3], ts=ts, usdc=usdc))
        last = ts
print(f'taker BUY fills {len(T)}, first-fill episodes {len(episodes)}', file=sys.stderr)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def ladder(st, ed, ts):
    row = hot.execute("select buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? and observed_at>=? order by observed_at desc limit 1",
                      (st, ed, iso(ts), iso(ts - 1800))).fetchone()
    if not row:
        return None
    out = {}
    for b in json.loads(row[0]):
        if b.get('bid_price') is not None and b.get('best_ask') is not None and 0 < b['best_ask'] - b['bid_price'] < 0.2:
            out[b['bucket']] = (b['ordinal'], (b['bid_price'] + b['best_ask']) / 2)
    s = sum(m for _, m in out.values())
    if len(out) < 3 or not 0.85 <= s <= 1.2:
        return None
    return {bk: (o, m / s) for bk, (o, m) in out.items()}


nat_cache, pt_cache = {}, {}


def natives(st, ed):
    if (st, ed) not in nat_cache:
        d = collections.defaultdict(dict)
        for sysn, ri, mu in hot.execute("select ensemble_system_id, native_run_init_utc, avg(raw_tmax_c) from calibration_native_ensemble_historical_member_run "
                                        "where station_id=? and target_local_date=? and qc_status='ACCEPTED' and native_run_init_utc is not null "
                                        "and ensemble_system_id in ('ECMWF_AIFS_ENS_GLOBAL','ECMWF_IFS_ENS_OPEN_PF50') group by 1,2", (st, ed)):
            d[sysn][ri] = mu
        nat_cache[(st, ed)] = d
    return nat_cache[(st, ed)]


def points(st, ed):
    if (st, ed) not in pt_cache:
        d = collections.defaultdict(list)
        for sysn, ri, tm, kt in hot.execute("select ensemble_system_id, run_init_utc, forecast_tmax_c, knowable_at_utc from calibration_regional_point_run "
                                            "where station_id=? and target_local_date=? and remaining_peak_complete=1 and ensemble_system_id in ('ECMWF_IFS_9KM_RELAY_POINT','ECMWF_AIFS_SINGLE_POINT')", (st, ed)):
            d[sysn].append((datetime.fromisoformat(kt.replace('Z', '+00:00')).timestamp(), ri, tm))
        pt_cache[(st, ed)] = d
    return pt_cache[(st, ed)]


def momentum(st, ed, ts):
    comps = {}
    nat = natives(st, ed)
    for sysn, name in (('ECMWF_AIFS_ENS_GLOBAL', 'AIFS ENS d1'), ('ECMWF_IFS_ENS_OPEN_PF50', 'IFS ENS d1')):
        v = [mu for ri, mu in sorted(nat.get(sysn, {}).items()) if datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG[sysn] + 1) * 3600 <= ts]
        if len(v) >= 2:
            comps[name] = v[-1] - v[-2]
    pts = points(st, ed)
    for sysn, name, k in (('ECMWF_IFS_9KM_RELAY_POINT', 'HRES d1', 2), ('ECMWF_AIFS_SINGLE_POINT', 'AIFS single d2', 3)):
        seen = {}
        for kt, ri, tm in sorted(pts.get(sysn, [])):
            if kt <= ts:
                seen[ri] = tm
        v = [seen[x] for x in sorted(seen)]
        if len(v) >= k:
            comps[name] = v[-1] - v[-k]
    return (sum(v / SD[n] for n, v in comps.items()), comps) if comps else (None, comps)


rows = []
for e in episodes:
    lad = ladder(e['st'], e['ed'], e['ts'])
    if not lad or e['bucket'] not in lad:
        continue
    q = {o: p for bk, (o, p) in lad.items()}
    mode = max(q, key=q.get)
    k = lad[e['bucket']][0]
    d = k - mode
    if d == 0:
        continue
    s = (1 if d > 0 else -1) * (1 if e['side'] == 'Yes' else -1)
    M, comps = momentum(e['st'], e['ed'], e['ts'])
    truth = labels.get((e['st'], e['ed']))
    tord = lad[truth][0] if truth in lad else None
    rows.append(dict(e, s=s, d=d, M=M, n=len(comps), q=q, mode=mode, tord=tord))
print(f'off-mode episodes with a ladder: {len(rows)}; with momentum: {sum(1 for r in rows if r["M"] is not None)}; with truth: {sum(1 for r in rows if r["tord"] is not None)}')


def dscore(r, side):
    sd = lambda o: (o - r['mode']) * side
    D = 1 if sd(r['tord']) > 0 else -1 if sd(r['tord']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in r['q'].items())


sgn = lambda x: 1 if x > 0 else -1
for tau in (0.5, 1.0, 1.5):
    sel = [r for r in rows if r['M'] is not None and abs(r['M']) >= tau]
    if not sel:
        continue
    ag = sum(1 for r in sel if sgn(r['M']) == r['s']) / len(sel)
    # binomial standard error
    se = (ag * (1 - ag) / len(sel)) ** 0.5
    byd = collections.Counter(r['ed'] for r in sel)
    print(f'|M|>={tau}: n={len(sel)} agree {ag:.3f} (+-{1.96*se:.3f})  by event date {dict(sorted(byd.items()))}')
    sc = [r for r in sel if r['tord'] is not None]
    if len(sc) >= 10:
        A = [r for r in sc if sgn(r['M']) == r['s']]; D = [r for r in sc if sgn(r['M']) != r['s']]
        print(f'     scored n={len(sc)}: their skill {sum(dscore(r, r["s"]) for r in sc)/len(sc):+.3f}  M skill {sum(dscore(r, sgn(r["M"])) for r in sc)/len(sc):+.3f}  '
              f'their agree/disagree {sum(dscore(r, r["s"]) for r in A)/max(1,len(A)):+.3f} (n={len(A)}) / {sum(dscore(r, r["s"]) for r in D)/max(1,len(D)):+.3f} (n={len(D)})')
json.dump([{k: v for k, v in r.items() if k != 'q'} for r in rows], open('/private/tmp/claude-501/-Users-xiong-weather/8e8e0e0f-2636-45a0-a0af-fe34e79a733b/scratchpad/bench/forward_rows.json', 'w'))
