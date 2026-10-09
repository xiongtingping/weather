"""Round 21 part 3: forecast shortwave radiation as a direction source
(Open-Meteo previous runs, ECMWF IFS and ICON, fetched 10-10 into prev_sw.json).
rad_level: local 09-16h shortwave sum for D from the run usable at the trade
  (previous_day1 once D-1 08Z is past, else previous_day2 once D-2 08Z is past)
  minus the station's mean over other dates; + = sunnier = warmer.
rad_upd:   previous_day1 minus previous_day2 for D (only when day1 is usable);
  + = the newer run is sunnier.
"""
import json, collections, random, statistics, sys
from datetime import datetime, timezone, timedelta
B = sys.argv[1]
random.seed(21212)
R = json.load(open(f'{B}/direction_rows.json'))
Mm = json.load(open(f'{B}/momentum_M.json'))
SW = json.load(open(f'{B}/prev_sw.json'))
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}
    r['M'] = Mm.get(f'{r["station"]}|{r["ed"]}|{r["ts"]}')
day = collections.defaultdict(dict)  # (st, model) -> date -> (sum1, sum2)
for key, v in SW.items():
    if 'time' not in v:
        continue
    st, model = key.split('|')
    acc = collections.defaultdict(lambda: [0.0, 0.0, 0, 0])
    for t, a, b in zip(v['time'], v['d1'], v['d2']):
        if 9 <= int(t[11:13]) <= 16:
            if a is not None:
                acc[t[:10]][0] += a; acc[t[:10]][2] += 1
            if b is not None:
                acc[t[:10]][1] += b; acc[t[:10]][3] += 1
    day[(st, model)] = {d: (x[0] if x[2] == 8 else None, x[1] if x[3] == 8 else None) for d, x in acc.items()}
for r in R:
    D = datetime.fromisoformat(r['ed']).replace(tzinfo=timezone.utc)
    use1 = r['ts'] >= (D - timedelta(days=1) + timedelta(hours=8)).timestamp()
    use2 = r['ts'] >= (D - timedelta(days=2) + timedelta(hours=8)).timestamp()
    r['rad'] = {}
    for model in ('ecmwf_ifs025', 'icon_global'):
        dd = day.get((r['station'], model), {})
        if r['ed'] not in dd:
            continue
        s1, s2 = dd[r['ed']]
        val, lag = (s1, 1) if use1 and s1 is not None else (s2, 2) if use2 and s2 is not None else (None, None)
        if val is not None:
            others = [x[lag - 1] for d, x in dd.items() if d != r['ed'] and x[lag - 1] is not None]
            if len(others) >= 6:
                sd = statistics.pstdev(others) or 1.0
                r['rad'][f'{model} level'] = (val - statistics.mean(others)) / sd
        if use1 and s1 is not None and s2 is not None:
            r['rad'][f'{model} update'] = (s1 - s2) / 100.0  # in 100 W/m2 summed over 8 hours


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


sgn = lambda x: 1 if x > 0 else -1
TAU = {'level': 0.5, 'update': 2.0}
for sub_name, sub in (('all off-mode', R), ('quiet |M|<0.5', [r for r in R if r['M'] is None or abs(r['M']) < 0.5])):
    print(f'=== {sub_name}: n={len(sub)} ===')
    for k in ('ecmwf_ifs025 level', 'icon_global level', 'ecmwf_ifs025 update', 'icon_global update'):
        tau = TAU[k.split()[1]]
        sel = [dict(r, _x=r['rad'][k]) for r in sub if k in r['rad'] and abs(r['rad'][k]) >= tau]
        if len(sel) < 30:
            print(f'   {k:22s} n={len(sel)}'); continue
        ag = sum(1 for r in sel if sgn(r['_x']) == r['s']) / len(sel)
        alo, ahi = boot(sel, lambda r: 1.0 if sgn(r['_x']) == r['s'] else 0.0)
        sk = sum(dscore(r, sgn(r['_x'])) for r in sel) / len(sel)
        slo, shi = boot(sel, lambda r: dscore(r, sgn(r['_x'])))
        A = [r for r in sel if sgn(r['_x']) == r['s']]; Dd = [r for r in sel if sgn(r['_x']) != r['s']]
        print(f'   {k:22s} n={len(sel):4d} agree {ag:.3f} [{alo:.3f},{ahi:.3f}] skill {sk:+.3f} [{slo:+.3f},{shi:+.3f}]  theirs agree/disagree {sum(dscore(r, r["s"]) for r in A)/max(1,len(A)):+.3f}/{sum(dscore(r, r["s"]) for r in Dd)/max(1,len(Dd)):+.3f}')

print('\n=== robustness of ICON radiation level ===')
k = 'icon_global level'
sel = [r for r in R if k in r['rad'] and abs(r['rad'][k]) >= 0.5]
for hh in ('H1', 'H2'):
    s = [r for r in sel if (r['ed'] < '2026-09-30') == (hh == 'H1')]
    print(f'   {hh}: n={len(s)} agree {sum(1 for r in s if sgn(r["rad"][k])==r["s"])/len(s):.3f} skill {sum(dscore(r, sgn(r["rad"][k])) for r in s)/len(s):+.3f}')
for lo, hi in ((0.5, 1.0), (1.0, 1.5), (1.5, 9)):
    s = [r for r in R if k in r['rad'] and lo <= abs(r['rad'][k]) < hi]
    print(f'   |z| {lo}-{hi}: n={len(s)} agree {sum(1 for r in s if sgn(r["rad"][k])==r["s"])/len(s):.3f} skill {sum(dscore(r, sgn(r["rad"][k])) for r in s)/len(s):+.3f}')
print('   with ECMWF momentum (|M|>=1):')
for lab, f in (('momentum agrees with rad', lambda r: r['M'] is not None and abs(r['M']) >= 1 and sgn(r['M']) == sgn(r['rad'][k])),
               ('momentum opposes rad', lambda r: r['M'] is not None and abs(r['M']) >= 1 and sgn(r['M']) != sgn(r['rad'][k])),
               ('momentum quiet', lambda r: r['M'] is None or abs(r['M']) < 1)):
    s = [r for r in sel if f(r)]
    if s:
        print(f'     {lab:26s} n={len(s):4d} rad skill {sum(dscore(r, sgn(r["rad"][k])) for r in s)/len(s):+.3f}  their agree w/ rad {sum(1 for r in s if sgn(r["rad"][k])==r["s"])/len(s):.3f}  theirs {sum(dscore(r, r["s"]) for r in s)/len(s):+.3f}')
both = [r for r in sel if r['M'] is not None and abs(r['M']) >= 1 and sgn(r['M']) == sgn(r['rad'][k])]
if both:
    print(f'   momentum + rad agree, their direction = that side: {sum(1 for r in both if r["s"]==sgn(r["M"]))/len(both):.3f}; their skill when they follow it {sum(dscore(r, r["s"]) for r in both if r["s"]==sgn(r["M"]))/max(1,sum(1 for r in both if r["s"]==sgn(r["M"]))):+.3f}')
json.dump({f'{r["station"]}|{r["ed"]}|{r["ts"]}': r['rad'].get('icon_global level') for r in R}, open(f'{B}/icon_rad.json', 'w'))
