"""Round 17 part 4: where their signal sits and how wide it is, net of the
market being too wide.  Posterior = market x N(mode + a*d, sigma) for off-mode
YES picks; a=0 is the control (sharpening about the mode, no direction), a=1
centres on the pick.  Information = best (a, sigma) minus best sigma at a=0,
cross-fitted by date half.  Also the realised centre shift along the picked
direction, in buckets and deg C.
"""
import gzip, json, math, collections, sys
from zoneinfo import ZoneInfo
ROOT = '/Users/xiong/weather/docs/research/benchmark_account_0x8fbd_20261008'
BENCH = sys.argv[1]
TZ = {r['station_id']: r['settlement_timezone'] for r in json.load(open(f'{BENCH}/registry.json'))}
FLOOR = 1e-3
E = []
for l in gzip.open(f'{ROOT}/choice_rows.jsonl.gz', 'rt'):
    r = json.loads(l)
    yes = sorted((a for a in r['alts'] if a['side'] == 'Yes'), key=lambda a: a['ord'])
    q = {a['ord']: a['q'] for a in yes}
    ch = next(a for a in r['alts'] if a['chosen'])
    if r['truth_ord'] not in q or ch['side'] != 'Yes' or ch['ord'] == r['mode_ord']:
        continue
    tzn = TZ.get(r['station'], '')
    E.append(dict(truth=r['truth_ord'], mode=r['mode_ord'], d=ch['ord'] - r['mode_ord'], q=q, unit=r['unit'], lead=r['lead_h'],
                  region=('Asia' if tzn.startswith(('Asia', 'Pacific/Auckland')) else 'Europe' if tzn.startswith(('Europe', 'Africa')) else 'Americas'),
                  half='H1' if r['event_date'] < '2026-09-30' else 'H2', width=1.0 if r['unit'] == 'C' else 10 / 9))
A = [-0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5]
S = [0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 10.0]


def gain(evs, a, sg):
    tot = 0.0
    for e in evs:
        c = e['mode'] + a * e['d']
        w = {o: v * math.exp(-((o - c) ** 2) / (2 * sg ** 2)) for o, v in e['q'].items()}
        z = sum(w.values())
        tot += math.log(max(w[e['truth']] / z, FLOOR)) - math.log(max(e['q'][e['truth']], FLOOR))
    return tot / len(evs)


def best(evs, As):
    return max(((gain(evs, a, sg), a, sg) for a in As for sg in S))


def report(label, evs):
    if len(evs) < 60:
        return
    g, a, sg = best(evs, A)
    g0, _, sg0 = best(evs, [0.0])
    H = {h: [e for e in evs if e['half'] == h] for h in ('H1', 'H2')}
    oos = []
    for tr, te in (('H1', 'H2'), ('H2', 'H1')):
        _, a1, s1 = best(H[tr], A)
        _, _, s0 = best(H[tr], [0.0])
        oos.append(gain(H[te], a1, s1) - gain(H[te], 0.0, s0))
    w = sum(e['width'] for e in evs) / len(evs)
    shift = sum((1 if e['d'] > 0 else -1) * (e['truth'] - sum(o * v for o, v in e['q'].items())) for e in evs) / len(evs)
    print(f'{label:22s} n={len(evs):4d}  centre a={a:+.2f} (x distance mode->pick)  sigma={sg:.2f} bkt = {sg*w:.2f} C  '
          f'info net of sharpening {g-g0:+.4f} nats (a=0 best sigma {sg0})  oos {oos[0]:+.4f}/{oos[1]:+.4f}  '
          f'realised shift toward pick {shift:+.2f} bkt = {shift*w:+.2f} C')


report('all off-mode YES', E)
for unit in ('C', 'F'):
    report(f'unit {unit}', [e for e in E if e['unit'] == unit])
for reg in ('Asia', 'Europe', 'Americas'):
    report(f'region {reg}', [e for e in E if e['region'] == reg])
report('lead <12h', [e for e in E if e['lead'] < 12])
report('lead 12-48h', [e for e in E if 12 <= e['lead'] < 48])
report('market q_mode<0.35', [e for e in E if e['q'][e['mode']] < 0.35])
report('market q_mode>=0.35', [e for e in E if e['q'][e['mode']] >= 0.35])
