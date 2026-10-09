"""Round 17: resolution and conditionality of the cluster's bucket choice.

Data: choice_rows.jsonl.gz (round 14) -- 1,491 first-taker events, each with
the whole ladder at that instant (normalised mid q per bucket), the bucket and
side they took, and the settled bucket.

A. Likelihood-ratio profile.  Truth frequency over the market's probability,
   on an axis centred at the market mode and oriented so the side they picked
   is positive.  Where the excess sits is what they know: on the picked bucket
   only = bucket-precise; spread over the whole side = direction only.
B. Three information models for YES picks, fitted on one half of the dates and
   scored on the other (posterior = market x likelihood, LogS of the truth):
   spike (picked bucket), step (picked side), Gaussian point signal (sigma in
   buckets -> deg C).  Gain is in nats per event over the market.
C. Conditionality: excess at the picked bucket (YES: hit - q; NO: q - hit),
   by observable condition, with station-day bootstrap CIs and both date
   halves separately.  A condition counts only if both halves agree.
"""
import gzip, json, math, random, collections, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = '/Users/xiong/weather/docs/research/benchmark_account_0x8fbd_20261008'
BENCH = sys.argv[1] if len(sys.argv) > 1 else '.'
rows = [json.loads(l) for l in gzip.open(f'{ROOT}/choice_rows.jsonl.gz', 'rt')]
reg = json.load(open(f'{BENCH}/registry.json'))
TZ = {r['station_id']: r['settlement_timezone'] for r in reg}
FLOOR = 1e-3
random.seed(17)

events = []
for r in rows:
    yes = sorted((a for a in r['alts'] if a['side'] == 'Yes'), key=lambda a: a['ord'])
    q = {a['ord']: a['q'] for a in yes}
    p = {a['ord']: a['p'] for a in yes}
    ch = next(a for a in r['alts'] if a['chosen'])
    if r['truth_ord'] not in q:
        continue
    zone = ZoneInfo(TZ.get(r['station'], 'UTC'))
    local = datetime.fromtimestamp(r['ts'], zone)
    tzname = TZ.get(r['station'], '')
    region = ('Asia' if tzname.startswith(('Asia', 'Pacific/Auckland')) else
              'Europe' if tzname.startswith(('Europe', 'Africa')) else
              'Americas' if tzname.startswith('America') else 'other')
    events.append(dict(
        station=r['station'], ed=r['event_date'], ts=r['ts'], lead=r['lead_h'], unit=r['unit'],
        stage=r['stage'], usdc=r['usdc'], wallet=r['wallet'], truth=r['truth_ord'],
        mode=r['mode_ord'], our_mode=r['our_mode_ord'], k=ch['ord'], side=ch['side'],
        d=ch['ord'] - r['mode_ord'], q=q, p=p, price=ch['price'], mom=ch.get('mom'),
        spread=ch.get('spread'), depth=ch.get('depth'), cur_obs=r.get('cur_obs'),
        tmax_today=r.get('tmax_today'), ytruth=r.get('ytruth_c'), truth_c=r.get('truth_c'),
        is_d0=r['is_d0'], local_hour=local.hour, region=region,
        half='H1' if r['event_date'] < '2026-09-30' else 'H2',
        week=r['event_date'][:8] + ('a' if r['event_date'][8:] < '29' else 'b'),
    ))
print(f'events {len(events)}  YES {sum(e["side"]=="Yes" for e in events)}  NO {sum(e["side"]=="No" for e in events)}')
print('halves', collections.Counter(e['half'] for e in events))


def boot(groups, stat, n=1000):
    """Station-day cluster bootstrap of stat(list_of_events)."""
    keys = list(groups)
    vals = []
    for _ in range(n):
        sample = [e for k in (random.choice(keys) for _ in keys) for e in groups[k]]
        v = stat(sample)
        if v is not None:
            vals.append(v)
    vals.sort()
    if not vals:
        return (None, None)
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def by_day(evs):
    g = collections.defaultdict(list)
    for e in evs:
        g[(e['station'], e['ed'])].append(e)
    return g


# ------------------------------------------------------------------ A
print('\n=== A. likelihood-ratio profile (axis: market mode = 0, picked side = +) ===')


def profile(evs, axis, span=range(-4, 6)):
    out = {}
    for u in span:
        f = sum(1 for e in evs if axis(e, e['truth']) == u) / len(evs)
        ex = sum(sum(qv for o, qv in e['q'].items() if axis(e, o) == u) for e in evs) / len(evs)
        out[u] = (f, ex)
    return out


def show(title, evs, axis, mark=None, span=range(-4, 6)):
    if len(evs) < 30:
        return
    prof = profile(evs, axis, span)
    g = by_day(evs)
    print(f'-- {title}  n={len(evs)} station-days={len(g)}')
    print('   u    truth   market   diff     LR    95%CI(diff)')
    for u, (f, ex) in prof.items():
        if ex < 0.005 and f == 0:
            continue
        lo, hi = boot(g, lambda s, u=u: (sum(1 for e in s if axis(e, e['truth']) == u)
                                          - sum(sum(qv for o, qv in e['q'].items() if axis(e, o) == u) for e in s)) / len(s), 600)
        tag = ' <- picked' if mark is not None and u == mark else ''
        print(f'  {u:+d}  {f:6.3f}  {ex:6.3f}  {f-ex:+6.3f}  {f/ex if ex else float("nan"):5.2f}  [{lo:+.3f},{hi:+.3f}]{tag}')


def oriented(e, o):
    s = 1 if e['d'] > 0 else -1
    return s * (o - e['mode'])


def rel_mode(e, o):
    return o - e['mode']


def abs_mode(e, o):
    return abs(o - e['mode'])


Y1 = [e for e in events if e['side'] == 'Yes' and abs(e['d']) == 1]
Y2 = [e for e in events if e['side'] == 'Yes' and abs(e['d']) == 2]
Y0 = [e for e in events if e['side'] == 'Yes' and e['d'] == 0]
N0 = [e for e in events if e['side'] == 'No' and e['d'] == 0]
N1 = [e for e in events if e['side'] == 'No' and abs(e['d']) == 1]
show('YES on mode+-1 (picked = +1)', Y1, oriented, mark=1)
show('YES on mode+-2 (picked = +2)', Y2, oriented, mark=2)
show('YES on the mode (|offset| from mode)', Y0, abs_mode, mark=0, span=range(0, 5))
show('NO on the mode (|offset| from mode)', N0, abs_mode, mark=0, span=range(0, 5))
show('NO on mode+-1 (picked = +1)', N1, oriented, mark=1)

# ------------------------------------------------------------------ B
print('\n=== B. information models for YES picks (cross-fitted by date half) ===')
YES = [e for e in events if e['side'] == 'Yes']


def logs(e, lik):
    w = {o: e['q'][o] * lik(e, o) for o in e['q']}
    z = sum(w.values())
    pt = max(w.get(e['truth'], 0.0) / z, FLOOR)
    return math.log(pt)


def base(e):
    return math.log(max(e['q'][e['truth']], FLOOR))


MODELS = {
    'spike  (picked bucket x a)': (lambda a: (lambda e, o: a[0] if o == e['k'] else 1.0), [[x] for x in (1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0)]),
    'step   (picked side incl. beyond x b)': (lambda a: (lambda e, o: a[0] if (e['d'] != 0 and (o - e['mode']) * (1 if e['d'] > 0 else -1) >= abs(e['d'])) else 1.0), [[x] for x in (1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.5)]),
    'side   (whole side past the mode x b)': (lambda a: (lambda e, o: a[0] if (e['d'] != 0 and (o - e['mode']) * (1 if e['d'] > 0 else -1) > 0) else 1.0), [[x] for x in (1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.5)]),
    'gauss  (point signal at k+delta, sigma)': (lambda a: (lambda e, o: 1.0 + a[2] * math.exp(-((o - e['k'] - a[0] * (1 if e['d'] >= 0 else -1)) ** 2) / (2 * a[1] ** 2))),
                                               [[dl, sg, A] for dl in (-0.5, 0.0, 0.5, 1.0) for sg in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0) for A in (0.25, 0.5, 1.0, 2.0)]),
}


def fit(evs, factory, grid):
    best = None
    for a in grid:
        lik = factory(a)
        g = sum(logs(e, lik) - base(e) for e in evs) / len(evs)
        if best is None or g > best[0]:
            best = (g, a)
    return best


H = {h: [e for e in YES if e['half'] == h] for h in ('H1', 'H2')}
print(f'YES events: H1 {len(H["H1"])}  H2 {len(H["H2"])}')
print(f'  {"model":42s} in-sample(all)  params        out-of-sample gain nats/event: H1->H2  H2->H1')
for name, (factory, grid) in MODELS.items():
    g_all, a_all = fit(YES, factory, grid)
    oos = []
    for tr, te in (('H1', 'H2'), ('H2', 'H1')):
        _, a = fit(H[tr], factory, grid)
        lik = factory(a)
        oos.append(sum(logs(e, lik) - base(e) for e in H[te]) / len(H[te]))
    print(f'  {name:42s} {g_all:+.4f}   {str(a_all):14s}  {oos[0]:+.4f}  {oos[1]:+.4f}')

# sigma in deg C for the gaussian, by unit
for unit, width in (('C', 1.0), ('F', 2 * 5 / 9)):
    ev = [e for e in YES if e['unit'] == unit]
    g, a = fit(ev, MODELS['gauss  (point signal at k+delta, sigma)'][0], MODELS['gauss  (point signal at k+delta, sigma)'][1])
    print(f'  gauss on {unit} stations (n={len(ev)}): gain {g:+.4f}  delta={a[0]} buckets  sigma={a[1]} buckets = {a[1]*width:.2f} C  amplitude={a[2]}')

# ------------------------------------------------------------------ C
print('\n=== C. conditionality of the picked-bucket excess (YES: hit-q, NO: q-hit; prob points) ===')


def excess(e):
    hit = 1.0 if e['truth'] == e['k'] else 0.0
    qk = e['q'][e['k']]
    return (hit - qk) if e['side'] == 'Yes' else (qk - hit)


def mean_ex(evs):
    return sum(excess(e) for e in evs) / len(evs) if evs else None


CONDS = {
    'lead h': lambda e: ('<6' if e['lead'] < 6 else '6-12' if e['lead'] < 12 else '12-24' if e['lead'] < 24 else '24-36' if e['lead'] < 36 else '36-48' if e['lead'] < 48 else '>=48'),
    'local hour': lambda e: f'{(e["local_hour"]//4)*4:02d}-{(e["local_hour"]//4)*4+3:02d}',
    'unit': lambda e: e['unit'],
    'region': lambda e: e['region'],
    'stage': lambda e: e['stage'],
    'mode concentration q_mode': lambda e: (lambda m: '<0.30' if m < 0.30 else '0.30-0.45' if m < 0.45 else '>=0.45')(e['q'][e['mode']]),
    'picked price q_k': lambda e: (lambda x: '<0.10' if x < 0.10 else '0.10-0.25' if x < 0.25 else '0.25-0.45' if x < 0.45 else '>=0.45')(e['q'][e['k']]),
    'first-fill size U': lambda e: '<10' if e['usdc'] < 10 else '10-30' if e['usdc'] < 30 else '30-100' if e['usdc'] < 100 else '>=100',
    'market skew vs pick': lambda e: ('mode' if e['d'] == 0 else
                                      (lambda s: 'with skew' if s > 0.02 else 'against skew' if s < -0.02 else 'flat')(
                                          (e['q'].get(e['mode'] + 1, 0) - e['q'].get(e['mode'] - 1, 0)) * (1 if e['d'] > 0 else -1))),
    'our p vs q at pick': lambda e: (lambda dlt: 'p>q+0.05' if dlt > 0.05 else 'p<q-0.05' if dlt < -0.05 else '|p-q|<=0.05')(e['p'].get(e['k'], 0) - e['q'][e['k']]),
    '1h momentum at pick': lambda e: 'n/a' if e['mom'] is None else ('up' if e['mom'] > 0.01 else 'down' if e['mom'] < -0.01 else 'flat'),
    'spread at pick': lambda e: 'n/a' if e['spread'] is None else ('<=0.01' if e['spread'] <= 0.011 else '0.01-0.03' if e['spread'] <= 0.031 else '>0.03'),
    'yesterday truth vs mode (C)': lambda e: 'n/a' if e['ytruth'] is None or e['truth_c'] is None else e['_ydiff'],
    'D0 observed max vs pick': lambda e: 'not D0' if not e['is_d0'] or e['tmax_today'] is None else e['_obs'],
    'wallet': lambda e: e['wallet'],
    'week': lambda e: e['week'],
    'side x offset': lambda e: f'{e["side"]} d={e["d"]:+d}' if abs(e['d']) <= 2 else f'{e["side"]} |d|>2',
}

# helpers that need the ladder in deg C: centre of the mode bucket from the
# bucket labels is not in the rows, so use the truth bucket/c pair to anchor
# ordinals to deg C per station-day (truth_c sits in bucket truth_ord).
for e in events:
    width = 1.0 if e['unit'] == 'C' else 2 * 5 / 9
    centre = lambda o, e=e, w=width: e['truth_c'] + (o - e['truth']) * w if e['truth_c'] is not None else None
    if e['ytruth'] is not None and e['truth_c'] is not None:
        diff = e['ytruth'] - centre(e['mode'])
        e['_ydiff'] = '<= -2' if diff <= -2 else '-2..-1' if diff <= -1 else '-1..+1' if diff < 1 else '+1..+2' if diff < 2 else '>= +2'
        # orient: is yesterday on the side they picked?
    if e['is_d0'] and e['tmax_today'] is not None and e['truth_c'] is not None:
        lo = centre(e['k']) - width / 2
        hi = centre(e['k']) + width / 2
        t = e['tmax_today']
        e['_obs'] = 'obs max already above pick' if t >= hi else 'obs max inside pick' if t >= lo else 'obs max below pick'


def cond_table(name, fn, evs):
    groups = collections.defaultdict(list)
    for e in evs:
        try:
            groups[fn(e)].append(e)
        except Exception:
            groups['n/a'].append(e)
    print(f'-- {name}')
    print(f'   {"level":28s}    n   excess   95%CI             H1      H2    agree')
    for lvl in sorted(groups, key=str):
        g = groups[lvl]
        if len(g) < 25:
            continue
        m = mean_ex(g)
        lo, hi = boot(by_day(g), mean_ex, 600)
        h1 = mean_ex([e for e in g if e['half'] == 'H1'])
        h2 = mean_ex([e for e in g if e['half'] == 'H2'])
        agree = (h1 is not None and h2 is not None and (h1 > 0) == (h2 > 0))
        sig = '*' if lo is not None and (lo > 0 or hi < 0) else ' '
        print(f'   {str(lvl):28s} {len(g):4d}  {m:+.3f}{sig}  [{lo:+.3f},{hi:+.3f}]  '
              f'{"" if h1 is None else f"{h1:+.3f}":>7s} {"" if h2 is None else f"{h2:+.3f}":>7s}  {"yes" if agree else "NO"}')


print(f'all events: excess {mean_ex(events):+.3f}  YES {mean_ex([e for e in events if e["side"]=="Yes"]):+.3f}  NO {mean_ex([e for e in events if e["side"]=="No"]):+.3f}')
for name, fn in CONDS.items():
    cond_table(name, fn, events)
