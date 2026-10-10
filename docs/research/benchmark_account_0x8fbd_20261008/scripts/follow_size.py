"""Round 23 (熊先生 10-10 「大单增加预算」): which "big" can the copy-follow
lane see when it acts, and does it carry the return?

Universe: round 10's follow events (cluster first taker fill, entry = mid 10
minutes later + 0.02, hold to settlement) inside the lane's window (C stations,
12-48 h to local noon), joined to the cluster's own fills (cluster_events.json):
  f1   = cluster USDC in the first fill's second (what the lane sees at ingest)
  c60  = cluster USDC on the token within 60 s of the first fill
  c300 = within 300 s
  tot  = the whole episode (known only afterwards; reference)
Then: ROI by size band, overall and inside charter 266's tiers; and a
cash-constrained simulation of the three tiers with a size multiplier.
Forward: the lane's own record of the cluster's taker fills since 10-08
(copy_follow_source_trades), settled episodes scored at the cluster's price.
"""
import bisect
import collections
import json
import random
import sqlite3
import sys

B = sys.argv[1]
random.seed(2323)
COST = 0.02
R = json.load(open(f'{B}/follow_rows.json'))
C = json.load(open(f'{B}/cluster_events.json'))['fills']
fills = collections.defaultdict(list)
for f in C:
    fills[(f['cid'], f['outcome'])].append((int(f['ts']), float(f['size']) * float(f['price'])))
for v in fills.values():
    v.sort()
for r in R:
    lst = fills.get((r['cid'], r['outcome']), [])
    ts = [t for t, _ in lst]
    i = bisect.bisect_left(ts, int(r['ts']))
    r['f1'] = sum(u for t, u in lst[i:] if t == int(r['ts']))
    r['c60'] = sum(u for t, u in lst[i:] if t <= int(r['ts']) + 60)
    r['c300'] = sum(u for t, u in lst[i:] if t <= int(r['ts']) + 300)
    r['tot'] = r['usdc']


def roi(r):
    return (r['hit'] - (r['own'] + COST)) / (r['own'] + COST)


def tier(r):
    if r['mom'] == 'agree':
        return 'momentum'
    if r['mom'] == 'disagree':
        return None
    if r['m4'] == 'agree':
        return 'model'
    if r['m4'] == 'disagree':
        return None
    return 'probability' if r['pf'] else None


W = [r for r in R if r['inwin']]
for r in W:
    r['tier'] = tier(r)
print(f'follow events in window (C, 12-48h): {len(W)}; joined to a first fill: {sum(1 for r in W if r["f1"] > 0)}')


def boot(rs, n=600):
    G = collections.defaultdict(list)
    for r in rs:
        G[r['ed']].append(roi(r))
    keys = list(G)
    vals = []
    for _ in range(n):
        s = [x for k in (random.choice(keys) for _ in keys) for x in G[k]]
        vals.append(sum(s) / len(s))
    vals.sort()
    return 100 * vals[int(.025 * n)], 100 * vals[int(.975 * n) - 1]


def show(label, rs):
    if len(rs) < 25:
        print(f'   {label:44s} n={len(rs)}')
        return
    m = 100 * sum(roi(r) for r in rs) / len(rs)
    lo, hi = boot(rs)
    h = {hh: [r for r in rs if r['half'] == hh] for hh in ('H1', 'H2')}
    hv = {hh: (100 * sum(roi(r) for r in v) / len(v) if v else float('nan')) for hh, v in h.items()}
    days = collections.defaultdict(list)
    for r in rs:
        days[r['ed']].append(roi(r))
    pos = sum(1 for v in days.values() if sum(v) > 0)
    print(f'   {label:44s} n={len(rs):4d}  ROI {m:+6.1f}% [{lo:+6.1f},{hi:+6.1f}]  H1/H2 {hv["H1"]:+6.1f}/{hv["H2"]:+6.1f}%  days+ {pos}/{len(days)}')


for key, name in (('f1', 'first fill (same second)'), ('c60', 'cluster USDC within 60 s'), ('c300', 'within 300 s'), ('tot', 'whole episode (afterwards)')):
    vals = sorted(r[key] for r in W)
    t1, t2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    print(f'\n-- {name}: tertile cuts {t1:.1f} / {t2:.1f} U')
    show('lowest third', [r for r in W if r[key] < t1])
    show('middle third', [r for r in W if t1 <= r[key] < t2])
    show('highest third', [r for r in W if r[key] >= t2])
    for lo, hi in ((0, 10), (10, 30), (30, 100), (100, 1e9)):
        show(f'{lo}-{hi if hi < 1e9 else "inf"} U', [r for r in W if lo <= r[key] < hi])

print('\n-- inside charter 266 tiers, by first fill (f1) and by 60 s cumulative (c60)')
for key in ('f1', 'c60'):
    for t in ('momentum', 'model', 'probability'):
        rs = [r for r in W if r['tier'] == t]
        for lo, hi in ((0, 30), (30, 1e9)):
            show(f'{t:11s} {key} {lo}-{hi if hi < 1e9 else "inf"} U', [r for r in rs if lo <= r[key] < hi])
    show(f'all tiers   {key} <30 U', [r for r in W if r['tier'] and r[key] < 30])
    show(f'all tiers   {key} >=30 U', [r for r in W if r['tier'] and r[key] >= 30])
    show(f'all tiers   {key} >=100 U', [r for r in W if r['tier'] and r[key] >= 100])

# ---- cash-constrained simulation (start 355 U, time order, skip when short, hold lead + 12 h)
print('\n-- cash-constrained: three tiers 3:2:1 (disagreeing skipped) x size multiplier on big legs, start 355 U')
EQ0 = 355.0
MULT = {'momentum': 3.0, 'model': 2.0, 'probability': 1.0}
legs_all = sorted((r for r in W if r['tier']), key=lambda r: r['ts'])
t_start = legs_all[0]['ts']
days_n = len({r['ed'] for r in R})


def simulate(b, key, cut, big):
    cash = EQ0
    open_pos = []
    prof = 0.0
    taken = skipped = 0
    area = 0.0
    last = t_start
    locked = 0.0
    ph = {'H1': 0.0, 'H2': 0.0}
    for r in legs_all:
        t = r['ts']
        open_pos.sort()
        while open_pos and open_pos[0][0] <= t:
            te, s, pay = open_pos.pop(0)
            area += locked * (te - last)
            last = te
            locked -= s
            cash += pay
        area += locked * (t - last)
        last = t
        s = b * MULT[r['tier']] * (big if (key and r[key] >= cut) else 1.0)
        if cash >= s:
            cash -= s
            locked += s
            taken += 1
            pay = s * (1 + roi(r))
            prof += pay - s
            ph[r['half']] += pay - s
            open_pos.append((t + (r['lead'] + 12) * 3600, s, pay))
        else:
            skipped += 1
    open_pos.sort()
    for te, s, pay in open_pos:
        area += locked * (te - last)
        last = te
        locked -= s
    return prof, area / ((last - t_start) * EQ0), taken, skipped, ph


for b in (2.0, 3.0):
    p0 = simulate(b, None, 0, 1.0)
    print(f'   base {b:.0f}U, no size multiplier: profit {p0[0]:+7.1f}U  H1/H2 {p0[4]["H1"]:+6.1f}/{p0[4]["H2"]:+6.1f}  utilisation {100*p0[1]:4.0f}%  taken {p0[2]} skipped {p0[3]}')
    for key in ('f1', 'c60'):
        for cut in (30.0, 50.0, 100.0):
            for big in (1.5, 2.0, 3.0):
                p = simulate(b, key, cut, big)
                print(f'   base {b:.0f}U, {key}>={cut:.0f}U x{big:.1f}: profit {p[0]:+7.1f}U  H1/H2 {p[4]["H1"]:+6.1f}/{p[4]["H2"]:+6.1f}  utilisation {100*p[1]:4.0f}%  taken {p[2]} skipped {p[3]}')

# ---- forward: the lane's record of the cluster's taker fills, settled episodes at the cluster's price
print('\n-- forward (copy_follow_source_trades since 10-08, settled), episodes = same token within 3600 s, scored at the first fill price')
hot = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=30)
hot.execute('PRAGMA query_only=1')
labels = {(st, ed): b for st, ed, b in hot.execute(
    "select station_id,event_date,settled_bucket from basket_station_day_settlement_label l where event_date>='2026-10-08' and "
    "truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)")}
rows = hot.execute(
    "select source_ts, token_id, station_id, event_date, bucket, outcome_side, price, size from copy_follow_source_trades "
    "where order_action='BUY' and taker=1 and station_id is not null and event_date>='2026-10-08' order by source_ts").fetchall()
eps = []
cur = {}
for ts, tok, st, ed, b, side, price, size in rows:
    e = cur.get(tok)
    if e is None or ts - e['last'] > 3600:
        e = dict(ts=ts, st=st, ed=ed, b=b, side=side, price=price, f1=0.0, c60=0.0, tot=0.0, last=ts)
        cur[tok] = e
        eps.append(e)
    u = price * size
    if ts == e['ts']:
        e['f1'] += u
    if ts <= e['ts'] + 60:
        e['c60'] += u
    e['tot'] += u
    e['last'] = ts
done = [e for e in eps if (e['st'], e['ed']) in labels]
for e in done:
    hit = labels[(e['st'], e['ed'])] == e['b']
    e['hit'] = (1.0 if hit else 0.0) if e['side'] == 'YES' else (0.0 if hit else 1.0)
print(f'   episodes {len(eps)}, settled {len(done)}, event dates {sorted({e["ed"] for e in done})}')
for key in ('f1', 'c60', 'tot'):
    for lo, hi in ((0, 10), (10, 30), (30, 100), (100, 1e9)):
        sel = [e for e in done if lo <= e[key] < hi]
        if not sel:
            continue
        r = sum((e['hit'] - e['price']) / e['price'] for e in sel) / len(sel)
        print(f'   {key:4s} {lo:>3}-{hi if hi < 1e9 else "inf":>4} U: n={len(sel):3d}  equal-stake ROI at their first price {100*r:+6.1f}%  hit {sum(e["hit"] for e in sel)/len(sel):.3f}  price {sum(e["price"] for e in sel)/len(sel):.3f}')
