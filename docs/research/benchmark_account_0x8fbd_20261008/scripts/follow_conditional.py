"""Round 17 part 5: does the conditionality of their information carry into
copy-follow P&L?  Same pricing as round 10.2: enter 10 minutes after the
cluster's first taker fill at the 1-minute mid + cost, hold to settlement;
base filter = our served p > entry price.  Stratified by the conditions round
17 found (region, unit, market concentration, lead, first-fill size, offset),
both date halves separately.  Cents per share.
"""
import json, glob, bisect, collections, sys, random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
B = sys.argv[1]
random.seed(171717)
E = json.load(open(f'{B}/events_enriched.json'))
TZ = {r['station_id']: r['settlement_timezone'] for r in json.load(open(f'{B}/registry.json'))}
H = {}
for p in glob.glob(f'{B}/hist_b*.json') + [f'{B}/hist.json']:
    H.update(json.load(open(p)))
S = {cid: ([h['t'] for h in hs], [h['p'] for h in hs]) for cid, hs in H.items()}


def mid_at(cid, ts, maxage=1800):
    if cid not in S:
        return None
    T, P = S[cid]
    i = bisect.bisect_right(T, ts) - 1
    return P[i] if i >= 0 and ts - T[i] <= maxage else None


R = []
for e in E:
    if not e['first_taker'] or e['truth_ord'] is None:
        continue
    m10 = mid_at(e['cid'], e['ts'] + 600, 900)
    if m10 is None:
        continue
    yes = e['outcome'] == 'Yes'
    own = m10 if yes else 1 - m10
    hit = 1.0 if ((e['truth_ord'] == e['my_ord']) if yes else (e['truth_ord'] != e['my_ord'])) else 0.0
    ours = None
    if e.get('p0') and e['p0'].get('p_my') is not None:
        ours = e['p0']['p_my'] if yes else 1 - e['p0']['p_my']
    tzn = TZ.get(e['station'], '')
    mk = e.get('market') or {}
    R.append(dict(st=e['station'], ed=e['event_date'], side=e['outcome'], own=own, hit=hit, ours=ours, usdc=e['usdc'],
                  lead=e['lead_h'], unit=e['unit'], qm=mk.get('q_mode'), d=mk.get('d'),
                  region=('Asia' if tzn.startswith(('Asia', 'Pacific/Auckland')) else 'Europe' if tzn.startswith(('Europe', 'Africa')) else 'Americas'),
                  half='H1' if e['event_date'] < '2026-09-30' else 'H2'))
COST = 0.02
print(f'events with a mid 10 min after the first fill: {len(R)}')


def pnl(r):
    return r['hit'] - (r['own'] + COST)


def mean(rs):
    return sum(pnl(r) for r in rs) / len(rs) if rs else None


def boot(rs, n=600):
    G = collections.defaultdict(list)
    for r in rs: G[(r['st'], r['ed'])].append(r)
    keys = list(G); vals = []
    for _ in range(n):
        s = [x for k in (random.choice(keys) for _ in keys) for x in G[k]]
        vals.append(mean(s))
    vals.sort()
    return vals[int(.025 * n)], vals[int(.975 * n) - 1]


def line(label, rs):
    if len(rs) < 40:
        return
    m = mean(rs); lo, hi = boot(rs)
    h1 = mean([r for r in rs if r['half'] == 'H1']); h2 = mean([r for r in rs if r['half'] == 'H2'])
    days = collections.defaultdict(list)
    for r in rs: days[r['ed']].append(pnl(r))
    pos = sum(1 for v in days.values() if sum(v) > 0)
    agree = h1 is not None and h2 is not None and h1 > 0 and h2 > 0
    print(f'   {label:34s} n={len(rs):4d}  {100*m:+5.1f}c [{100*lo:+5.1f},{100*hi:+5.1f}]  H1 {100*h1 if h1 is not None else float("nan"):+5.1f} H2 {100*h2 if h2 is not None else float("nan"):+5.1f}  days+ {pos}/{len(days)}  {"BOTH+" if agree else ""}')


F = [r for r in R if r['ours'] is not None and r['ours'] > r['own'] + COST]
print('\n-- reference')
line('all (no filter)', R)
line('round-10 filter: our p > entry', F)
print('\n-- within the round-10 filter, by condition')
for name, fn in (
        ('region', lambda r: r['region']),
        ('unit', lambda r: r['unit']),
        ('market q_mode', lambda r: 'n/a' if r['qm'] is None else ('<0.35' if r['qm'] < 0.35 else '0.35-0.45' if r['qm'] < 0.45 else '>=0.45')),
        ('lead h', lambda r: '<12' if r['lead'] < 12 else '12-48' if r['lead'] < 48 else '>=48'),
        ('first-fill size', lambda r: '<30U' if r['usdc'] < 30 else '30-100U' if r['usdc'] < 100 else '>=100U'),
        ('side x offset', lambda r: f'{r["side"]} {"mode" if r["d"]==0 else "off-mode" if r["d"] is not None else "n/a"}')):
    print(f'  {name}')
    g = collections.defaultdict(list)
    for r in F: g[fn(r)].append(r)
    for lvl in sorted(g, key=str):
        line(str(lvl), g[lvl])
print('\n-- candidate composite rules (applied on top of the round-10 filter)')
RULES = {
    'drop F stations': lambda r: r['unit'] == 'C',
    'drop <12h': lambda r: r['lead'] >= 12,
    'drop F and <12h': lambda r: r['unit'] == 'C' and r['lead'] >= 12,
    'drop YES on the mode': lambda r: not (r['side'] == 'Yes' and r['d'] == 0),
    'q_mode<0.45 only': lambda r: r['qm'] is not None and r['qm'] < 0.45,
    'C, >=12h, not YES-mode': lambda r: r['unit'] == 'C' and r['lead'] >= 12 and not (r['side'] == 'Yes' and r['d'] == 0),
}
for lab, fn in RULES.items():
    kept = [r for r in F if fn(r)]
    dropped = [r for r in F if not fn(r)]
    line(f'keep: {lab}', kept)
    line(f'  dropped by it', dropped)

# ---- market concentration from the round-14 ladder rows (joined on station, date, time)
import gzip
ROOT = '/Users/xiong/weather/docs/research/benchmark_account_0x8fbd_20261008'
L = {}
for l in gzip.open(f'{ROOT}/choice_rows.jsonl.gz', 'rt'):
    c = json.loads(l)
    yes = [a for a in c['alts'] if a['side'] == 'Yes']
    ch = next(a for a in c['alts'] if a['chosen'])
    L[(c['station'], c['event_date'], c['ts'])] = (max(a['q'] for a in yes), ch['ord'] - c['mode_ord'], ch['side'])
for r, e in zip(R, [e for e in E if e['first_taker'] and e['truth_ord'] is not None and mid_at(e['cid'], e['ts'] + 600, 900) is not None]):
    hit = L.get((e['station'], e['event_date'], e['ts']))
    r['qm2'], r['d2'], r['side2'] = hit if hit else (None, None, None)
F2 = [r for r in R if r['ours'] is not None and r['ours'] > r['own'] + COST and r['qm2'] is not None]
print(f'\n-- market concentration from the full ladder (joined: {len(F2)} of {len(F)} filtered events)')
for lab, fn in (('q_mode<0.35', lambda r: r['qm2'] < 0.35), ('q_mode 0.35-0.45', lambda r: 0.35 <= r['qm2'] < 0.45), ('q_mode>=0.45', lambda r: r['qm2'] >= 0.45),
                ('YES on mode', lambda r: r['side'] == 'Yes' and r['d2'] == 0), ('YES off mode', lambda r: r['side'] == 'Yes' and r['d2'] != 0),
                ('NO on mode', lambda r: r['side'] == 'No' and r['d2'] == 0), ('NO off mode', lambda r: r['side'] == 'No' and r['d2'] != 0)):
    line(lab, [r for r in F2 if fn(r)])
line('C, >=12h, not YES-mode (joined subset)', [r for r in F2 if r['unit'] == 'C' and r['lead'] >= 12 and not (r['side'] == 'Yes' and r['d2'] == 0)])
line('  + q_mode<0.45', [r for r in F2 if r['unit'] == 'C' and r['lead'] >= 12 and not (r['side'] == 'Yes' and r['d2'] == 0) and r['qm2'] < 0.45])

# ---- round 18: split by whether the ECMWF-family newest update agrees with their direction
DR = {(d['station'], d['ed'], d['ts']): d for d in json.load(open(f'{B}/direction_rows.json'))}
FAM = ['natd:ECMWF_AIFS_ENS_GLOBAL', 'natd:ECMWF_IFS_ENS_OPEN_PF50', 'ptd:ECMWF_IFS_9KM_RELAY_POINT', 'ptd:ECMWF_AIFS_SINGLE_POINT']
evs_ft = [e for e in E if e['first_taker'] and e['truth_ord'] is not None and mid_at(e['cid'], e['ts'] + 600, 900) is not None]
for r, e in zip(R, evs_ft):
    d = DR.get((e['station'], e['event_date'], e['ts']))
    r['fam'] = None
    if d:
        v = [d['sig'][k] for k in FAM if k in d['sig']]
        if v and abs(sum(v)) >= 0.3:
            r['fam'] = 'agree' if (1 if sum(v) > 0 else -1) == d['s'] else 'disagree'
        else:
            r['fam'] = 'silent'
print('\n-- round 18: ECMWF-family newest update vs their direction (off-mode events joined)')
for lab, flt in (('all follow events', lambda r: True), ('within round-10 filter', lambda r: r['ours'] is not None and r['ours'] > r['own'] + COST)):
    for st_ in ('agree', 'disagree', 'silent'):
        line(f'{lab}: update {st_}', [r for r in R if r.get('fam') == st_ and flt(r)])
line('round-10 filter + C + >=12h + update agrees', [r for r in R if r.get('fam') == 'agree' and r['ours'] is not None and r['ours'] > r['own'] + COST and r['unit'] == 'C' and r['lead'] >= 12])
line('round-10 filter + C + >=12h + update not disagreeing', [r for r in R if r.get('fam') in ('agree', 'silent') and r['ours'] is not None and r['ours'] > r['own'] + COST and r['unit'] == 'C' and r['lead'] >= 12])

# ---- round 19: split by the unfitted ECMWF momentum M (direction_momentum.py)
MM = json.load(open(f'{B}/momentum_M.json'))
for r, e in zip(R, evs_ft):
    d = DR.get((e['station'], e['event_date'], e['ts']))
    m = MM.get(f'{e["station"]}|{e["event_date"]}|{e["ts"]}')
    r['mom'] = None
    if d is not None and m is not None:
        r['mom'] = 'quiet' if abs(m) < 1.0 else ('agree' if (1 if m > 0 else -1) == d['s'] else 'disagree')
print('\n-- round 19: ECMWF momentum |M|>=1 vs their direction (off-mode events joined)')
for lab, flt in (('all follow events', lambda r: True), ('within round-10 filter', lambda r: r['ours'] is not None and r['ours'] > r['own'] + COST)):
    for st_ in ('agree', 'disagree', 'quiet'):
        line(f'{lab}: momentum {st_}', [r for r in R if r.get('mom') == st_ and flt(r)])
line('round-10 filter + C + >=12h + momentum agrees', [r for r in R if r.get('mom') == 'agree' and r['ours'] is not None and r['ours'] > r['own'] + COST and r['unit'] == 'C' and r['lead'] >= 12])

# ---- round 21: ICON forecast radiation anomaly with the momentum
RAD = json.load(open(f'{B}/icon_rad.json'))
for r, e in zip(R, evs_ft):
    d = DR.get((e['station'], e['event_date'], e['ts']))
    z = RAD.get(f'{e["station"]}|{e["event_date"]}|{e["ts"]}')
    r['rad_ag'] = None
    if d is not None and z is not None and abs(z) >= 0.5:
        r['rad_ag'] = (1 if z > 0 else -1) == d['s']
print('\n-- round 21: ICON radiation anomaly vs their direction, with momentum (off-mode events joined)')
line('rad agrees', [r for r in R if r.get('rad_ag') is True])
line('rad disagrees', [r for r in R if r.get('rad_ag') is False])
line('momentum agrees and rad agrees', [r for r in R if r.get('mom') == 'agree' and r.get('rad_ag') is True])
line('momentum agrees and rad disagrees', [r for r in R if r.get('mom') == 'agree' and r.get('rad_ag') is False])
line('momentum quiet and rad agrees', [r for r in R if r.get('mom') == 'quiet' and r.get('rad_ag') is True])
line('momentum quiet and rad disagrees', [r for r in R if r.get('mom') == 'quiet' and r.get('rad_ag') is False])
line('momentum disagrees (any rad)', [r for r in R if r.get('mom') == 'disagree'])
line('either agrees, neither opposes', [r for r in R if (r.get('mom') == 'agree' and r.get('rad_ag') is not False) or (r.get('mom') == 'quiet' and r.get('rad_ag') is True)])
