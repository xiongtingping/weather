"""Round 17 part 3: control-corrected resolution.

The Gaussian gain of part 2 mixes their information with the market being too
wide on these days (the same kernel centred on the mode also gains).  Here the
information is the difference between centring on their pick and centring on
the mirror bucket (mode - d), same kernel, same events -- whatever the market's
own width does cancels.  Also the direction score: truth past the mode on the
picked side minus on the other side, against the market's expectation of it.
"""
import gzip, json, math, random, collections, sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = '/Users/xiong/weather/docs/research/benchmark_account_0x8fbd_20261008'
BENCH = sys.argv[1]
rows = [json.loads(l) for l in gzip.open(f'{ROOT}/choice_rows.jsonl.gz', 'rt')]
TZ = {r['station_id']: r['settlement_timezone'] for r in json.load(open(f'{BENCH}/registry.json'))}
FLOOR = 1e-3
random.seed(17017)
E = []
for r in rows:
    yes = sorted((a for a in r['alts'] if a['side'] == 'Yes'), key=lambda a: a['ord'])
    q = {a['ord']: a['q'] for a in yes}
    if r['truth_ord'] not in q:
        continue
    ch = next(a for a in r['alts'] if a['chosen'])
    tzn = TZ.get(r['station'], '')
    E.append(dict(station=r['station'], ed=r['event_date'], lead=r['lead_h'], unit=r['unit'], stage=r['stage'],
                  truth=r['truth_ord'], mode=r['mode_ord'], k=ch['ord'], side=ch['side'], d=ch['ord'] - r['mode_ord'],
                  q=q, p={a['ord']: a['p'] for a in yes}, usdc=r['usdc'], half='H1' if r['event_date'] < '2026-09-30' else 'H2',
                  region=('Asia' if tzn.startswith(('Asia', 'Pacific/Auckland')) else 'Europe' if tzn.startswith(('Europe', 'Africa')) else 'Americas'),
                  hour=datetime.fromtimestamp(r['ts'], ZoneInfo(tzn or 'UTC')).hour, ytruth=r.get('ytruth_c'), truth_c=r.get('truth_c')))
s = lambda e: 1 if e['d'] > 0 else -1
OFFY = [e for e in E if e['side'] == 'Yes' and e['d'] != 0]
OFFN = [e for e in E if e['side'] == 'No' and e['d'] != 0]


def gauss_logs(e, centre, sigma):
    w = {o: e['q'][o] * math.exp(-((o - centre) ** 2) / (2 * sigma ** 2)) for o in e['q']}
    z = sum(w.values())
    return math.log(max(w.get(e['truth'], 0) / z, FLOOR))


def info(e, sigma):
    """nats: kernel at the pick minus the same kernel at the mirror bucket."""
    mirror = e['mode'] - e['d']
    return gauss_logs(e, e['k'], sigma) - gauss_logs(e, mirror, sigma)


def dir_excess(e):
    """YES off-mode: truth past the mode on the picked side minus other side, vs market."""
    side = lambda o: (o - e['mode']) * s(e)
    D = (1 if side(e['truth']) > 0 else -1 if side(e['truth']) < 0 else 0)
    ED = sum(v * (1 if side(o) > 0 else -1 if side(o) < 0 else 0) for o, v in e['q'].items())
    return D - ED


def dir_excess_no(e):
    """NO off-mode: they say 'not this neighbour' -> expect truth on the other side."""
    side = lambda o: -(o - e['mode']) * s(e)
    D = (1 if side(e['truth']) > 0 else -1 if side(e['truth']) < 0 else 0)
    ED = sum(v * (1 if side(o) > 0 else -1 if side(o) < 0 else 0) for o, v in e['q'].items())
    return D - ED


def boot(evs, stat, n=800):
    G = collections.defaultdict(list)
    for e in evs: G[(e['station'], e['ed'])].append(e)
    keys = list(G); vals = []
    for _ in range(n):
        smp = [x for k in (random.choice(keys) for _ in keys) for x in G[k]]
        vals.append(stat(smp))
    vals.sort()
    return vals[int(0.025 * n)], vals[int(0.975 * n) - 1]


mean = lambda f: (lambda evs: sum(f(e) for e in evs) / len(evs))
print('=== resolution: pick-centred minus mirror-centred Gaussian (YES off-mode, nats/event) ===')
print(f'   n={len(OFFY)}')
for sg in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
    m = mean(lambda e, sg=sg: info(e, sg))
    lo, hi = boot(OFFY, m, 500)
    h = {hh: m([e for e in OFFY if e['half'] == hh]) for hh in ('H1', 'H2')}
    print(f'   sigma={sg:4.2f} bkt  info {m(OFFY):+.4f}  [{lo:+.4f},{hi:+.4f}]  H1 {h["H1"]:+.4f} H2 {h["H2"]:+.4f}')

print('\n=== direction score (truth past mode on picked side minus other side, minus market expectation) ===')
for lab, evs, f in (('YES off-mode', OFFY, dir_excess), ('NO off-mode (other side)', OFFN, dir_excess_no)):
    m = mean(f); lo, hi = boot(evs, m)
    print(f'   {lab:26s} n={len(evs)}  excess {m(evs):+.3f} [{lo:+.3f},{hi:+.3f}]  H1 {m([e for e in evs if e["half"]=="H1"]):+.3f} H2 {m([e for e in evs if e["half"]=="H2"]):+.3f}')
# in probability terms: P(picked side) vs market
ps = sum(1 for e in OFFY if (e['truth'] - e['mode']) * s(e) > 0) / len(OFFY)
po = sum(1 for e in OFFY if (e['truth'] - e['mode']) * s(e) < 0) / len(OFFY)
qs = sum(sum(v for o, v in e['q'].items() if (o - e['mode']) * s(e) > 0) for e in OFFY) / len(OFFY)
qo = sum(sum(v for o, v in e['q'].items() if (o - e['mode']) * s(e) < 0) for e in OFFY) / len(OFFY)
print(f'   YES off-mode: truth on picked side {ps:.3f} (market {qs:.3f}), other side {po:.3f} (market {qo:.3f}), at mode {1-ps-po:.3f} (market {1-qs-qo:.3f})')

print('\n=== conditionality of the direction score (YES+NO off-mode pooled; both halves must agree) ===')
POOL = [(e, dir_excess(e)) for e in OFFY] + [(e, dir_excess_no(e)) for e in OFFN]
def ytrend(e):
    if e['ytruth'] is None or e['truth_c'] is None: return 'n/a'
    w = 1.0 if e['unit'] == 'C' else 10 / 9
    y_ord = e['truth'] + (e['ytruth'] - e['truth_c']) / w
    mean_ord = sum(o * v for o, v in e['q'].items())
    c = abs(y_ord - mean_ord)
    return '<1 bkt' if c < 1 else '1-2 bkt' if c < 2 else '>=2 bkt'
CONDS = {
    'lead h': lambda e: '<12' if e['lead'] < 12 else '12-24' if e['lead'] < 24 else '24-48' if e['lead'] < 48 else '>=48',
    'stage': lambda e: e['stage'],
    'region': lambda e: e['region'],
    'unit': lambda e: e['unit'],
    'q_mode': lambda e: '<0.30' if e['q'][e['mode']] < 0.30 else '0.30-0.40' if e['q'][e['mode']] < 0.40 else '0.40-0.50' if e['q'][e['mode']] < 0.50 else '>=0.50',
    'local hour': lambda e: f'{(e["hour"]//6)*6:02d}-{(e["hour"]//6)*6+5:02d}',
    'size U': lambda e: '<30' if e['usdc'] < 30 else '30-100' if e['usdc'] < 100 else '>=100',
    'our p side': lambda e: (lambda dp: 'p agrees (p_k>q_k)' if dp > 0.02 else 'p disagrees (p_k<q_k)' if dp < -0.02 else 'p~q')(
        (e['p'].get(e['k'], 0) - e['q'][e['k']]) * (1 if e['side'] == 'Yes' else -1)),
    'yesterday vs market mean': ytrend,
    'side': lambda e: e['side'],
}
def table(name, fn):
    g = collections.defaultdict(list)
    for e, x in POOL: g[fn(e)].append((e, x))
    print(f'-- {name}')
    for lvl in sorted(g, key=str):
        items = g[lvl]
        if len(items) < 30: continue
        evs = [dict(e, _x=x) for e, x in items]
        m = lambda L: sum(z['_x'] for z in L) / len(L)
        lo, hi = boot(evs, m, 500)
        h1 = [z for z in evs if z['half'] == 'H1']; h2 = [z for z in evs if z['half'] == 'H2']
        a = m(h1) if len(h1) >= 10 else None; b = m(h2) if len(h2) >= 10 else None
        agree = a is not None and b is not None and (a > 0) == (b > 0)
        sig = '*' if lo > 0 or hi < 0 else ' '
        print(f'   {str(lvl):24s} n={len(evs):4d}  {m(evs):+.3f}{sig} [{lo:+.3f},{hi:+.3f}]  H1 {"  n/a " if a is None else f"{a:+.3f}"} H2 {"  n/a " if b is None else f"{b:+.3f}"}  {"agree" if agree else "-"}')
for n, f in CONDS.items():
    table(n, f)
