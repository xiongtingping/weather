"""Round 20: bucket-level momentum and model-error persistence.

A. Bucket-level momentum from raw members: dp_b = p_new(b) - p_prev(b) for
   AIFS ENS and IFS ENS, newest two runs published by the trade (+1 h margin).
   For their picks: is the picked bucket the one whose probability rose most
   on its side?  YES picks vs the mirror bucket and vs the same-side bucket at
   the other distance; NO picks vs the mirror.  Skill: does dp at a bucket
   predict truth there beyond the market (excess hit by dp sign)?
B. Model-error persistence: e(D-1) = truth(D-1) - member mean for D-1 of the
   run that was the newest ~36 h before D-1 ended (same lead as the D trade),
   per system; sign(e) as a direction for D.  Their events, quiet events.
"""
import json, gzip, sqlite3, collections, random, sys, statistics, math
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
B = sys.argv[1]
ROOT = '/Users/xiong/weather/docs/research/benchmark_account_0x8fbd_20261008'
random.seed(20)
reg = json.load(open(f'{B}/registry.json'))
TZ = {x['station_id']: x['settlement_timezone'] for x in reg}
BOUNDS = collections.defaultdict(dict)   # (st, ed) -> outcome -> (lo, hi, unit)
for x in reg:
    BOUNDS[(x['station_id'], x['event_date'])][x['outcome']] = (x['lower_c'], x['upper_c'], x['settlement_unit'])
hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
labels = {}
for st, ed, t in hot.execute("select station_id,event_date,settled_tmax_c from basket_station_day_settlement_label l where truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)"):
    labels[(st, ed)] = float(t)
LAG = {'ECMWF_AIFS_ENS_GLOBAL': 6.3, 'ECMWF_IFS_ENS_OPEN_PF50': 8.2}
SYS = tuple(LAG)
ev = []
for l in gzip.open(f'{ROOT}/choice_rows.jsonl.gz', 'rt'):
    r = json.loads(l)
    yes = sorted((a for a in r['alts'] if a['side'] == 'Yes'), key=lambda a: a['ord'])
    ch = next(a for a in r['alts'] if a['chosen'])
    if r['truth_ord'] not in {a['ord'] for a in yes}:
        continue
    ev.append(dict(st=r['station'], ed=r['event_date'], ts=r['ts'], lead=r['lead_h'], side=ch['side'], k=ch['ord'], mode=r['mode_ord'],
                   d=ch['ord'] - r['mode_ord'], truth=r['truth_ord'], q={a['ord']: a['q'] for a in yes}, bk={a['ord']: a['bucket'] for a in yes}))
Mm = json.load(open(f'{B}/momentum_M.json'))
mem = collections.defaultdict(lambda: collections.defaultdict(list))  # (st, tg) -> (sys, ri) -> [members]


def load(st, tg):
    key = (st, tg)
    if key not in mem:
        mem[key]  # create
        for sysn, ri, v in hot.execute("select ensemble_system_id, native_run_init_utc, raw_tmax_c from calibration_native_ensemble_historical_member_run "
                                       "where station_id=? and target_local_date=? and qc_status='ACCEPTED' and native_run_init_utc is not null "
                                       "and ensemble_system_id in ('ECMWF_AIFS_ENS_GLOBAL','ECMWF_IFS_ENS_OPEN_PF50')", (st, tg)):
            mem[key][(sysn, ri)].append(v)
    return mem[key]


def bucket_of(st, ed, v, bkmap):
    b = BOUNDS[(st, ed)]
    for o, name in bkmap.items():
        lo, hi, unit = b.get(name, (None, None, None))
        if unit is None:
            continue
        x = round(v if unit == 'C' else v * 9 / 5 + 32)
        if (lo is None or x >= lo) and (hi is None or x <= hi):
            return o
    return None


def probs(st, ed, members, bkmap):
    c = collections.Counter(bucket_of(st, ed, v, bkmap) for v in members)
    n = len(members)
    return {o: c.get(o, 0) / n for o in bkmap}


def newest_two(st, ed, ts, sysn):
    runs = load(st, ed)
    ok = sorted(ri for (s2, ri) in runs if s2 == sysn and datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG[sysn] + 1) * 3600 <= ts)
    if len(ok) < 2:
        return None
    return runs[(sysn, ok[-1])], runs[(sysn, ok[-2])]


print('=== A. bucket-level momentum ===')
stats = collections.defaultdict(list)
skill_rows = []
for e in ev:
    for sysn in SYS:
        pair = newest_two(e['st'], e['ed'], e['ts'], sysn)
        if not pair:
            continue
        pn, pp = probs(e['st'], e['ed'], pair[0], e['bk']), probs(e['st'], e['ed'], pair[1], e['bk'])
        dp = {o: pn[o] - pp[o] for o in e['bk']}
        if e['d'] != 0:
            mirror = e['mode'] - e['d']
            other = e['mode'] + (1 if e['d'] > 0 else -1) * (2 if abs(e['d']) == 1 else 1)
            if mirror in dp:
                stats[(sysn, e['side'], 'picked > mirror')].append(1.0 if dp[e['k']] > dp[mirror] else 0.5 if dp[e['k']] == dp[mirror] else 0.0)
            if e['side'] == 'Yes' and other in dp:
                stats[(sysn, e['side'], 'picked > same-side other distance')].append(1.0 if dp[e['k']] > dp[other] else 0.5 if dp[e['k']] == dp[other] else 0.0)
            stats[(sysn, e['side'], 'dp at picked > 0')].append(1.0 if dp[e['k']] > 0 else 0.5 if dp[e['k']] == 0 else 0.0)
        for o in e['bk']:
            if abs(dp[o]) >= 0.06:
                skill_rows.append((sysn, e['st'], e['ed'], 1 if dp[o] > 0 else -1, (1.0 if o == e['truth'] else 0.0) - e['q'][o]))
for key in sorted(stats):
    v = stats[key]
    print(f'   {key[0]:26s} {key[1]:3s} {key[2]:36s} n={len(v):4d} share {sum(v)/len(v):.3f}')
print('   bucket-level skill: excess hit (truth - market q) at buckets whose member share moved >= 6 points')
for sysn in SYS:
    for sg in (1, -1):
        v = [x for s2, st, ed, g, x in skill_rows if s2 == sysn and g == sg]
        if v:
            print(f'   {sysn:26s} dp {"up" if sg>0 else "down"}: n={len(v):5d} excess {statistics.mean(v):+.3f}')

print('\n=== B. model-error persistence ===')
def run_newest_before(st, tg, cutoff, sysn):
    runs = load(st, tg)
    ok = sorted(ri for (s2, ri) in runs if s2 == sysn and datetime.fromisoformat(ri.replace('Z', '+00:00')).timestamp() + (LAG[sysn] + 1) * 3600 <= cutoff)
    return statistics.mean(runs[(sysn, ok[-1])]) if ok else None
def dscore(e, side):
    sd = lambda o: (o - e['mode']) * side
    D = 1 if sd(e['truth']) > 0 else -1 if sd(e['truth']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in e['q'].items())
OFF = [e for e in ev if e['d'] != 0]
for e in OFF:
    e['s'] = (1 if e['d'] > 0 else -1) * (1 if e['side'] == 'Yes' else -1)
    e['M'] = Mm.get(f'{e["st"]}|{e["ed"]}|{e["ts"]}')
    prev = (date.fromisoformat(e['ed']) - timedelta(days=1)).isoformat()
    t_prev = labels.get((e['st'], prev))
    e['perr'] = {}
    if t_prev is None:
        continue
    zone = ZoneInfo(TZ[e['st']])
    end_prev = datetime.combine(date.fromisoformat(prev) + timedelta(days=1), datetime.min.time(), tzinfo=zone).timestamp()
    lead_trade = e['lead'] * 3600
    for sysn in SYS:
        mu = run_newest_before(e['st'], prev, end_prev - lead_trade, sysn)
        if mu is not None:
            e['perr'][sysn] = t_prev - mu
sgn = lambda x: 1 if x > 0 else -1
for sub_name, sub in (('all off-mode', OFF), ('quiet |M|<0.5', [e for e in OFF if e['M'] is None or abs(e['M']) < 0.5])):
    for sysn in SYS + ('mean of both',):
        f = (lambda e, s=sysn: e['perr'].get(s)) if sysn != 'mean of both' else (lambda e: statistics.mean(e['perr'].values()) if len(e['perr']) == 2 else None)
        # raw members are cold-biased: use the error relative to the station's own mean error (LOO over other events of the station)
        rows = [(e, f(e)) for e in sub if f(e) is not None]
        by_st = collections.defaultdict(list)
        for e, x in rows: by_st[e['st']].append((e['ed'], x))
        sel = []
        for e, x in rows:
            others = [v for d2, v in by_st[e['st']] if d2 != e['ed']]
            if len(others) >= 3:
                xa = x - statistics.mean(others)
                if abs(xa) >= 0.5:
                    sel.append((e, xa))
        if len(sel) < 30:
            print(f'   {sub_name:14s} {sysn:26s} n={len(sel)}'); continue
        ag = sum(1 for e, x in sel if sgn(x) == e['s']) / len(sel)
        sk = sum(dscore(e, sgn(x)) for e, x in sel) / len(sel)
        print(f'   {sub_name:14s} {sysn:26s} n={len(sel):4d} agree {ag:.3f}  persistence skill {sk:+.3f}  theirs {sum(dscore(e, e["s"]) for e, x in sel)/len(sel):+.3f}')
