"""Round 21 addendum (10-10): is "ECMWF momentum |M|>=1 with the ICON daytime
shortwave anomaly on the same sign" a conditional advantage over the market,
in the user's own measure d = log b(y) - log q(y)?

b is the market ladder q (mids, normalised) tilted toward the momentum side:
b_o ~ q_o * exp(lam * side * sign(o - mode)).  One parameter, so the reading
is the side-level mass shift the direction score sees, priced as a proper
log score.

In-sample (default): lam fitted leave-one-event-day-out.  Forward
(``--fixed-lambda``): lam frozen at the registered value, no fitting.
Intervals resample event days (stations on one day share the runs).

Instances are built by momentum_rule_all.py's own code (everything before its
first report), with the date window replaced -- the rule is not rewritten here.

    python3 momentum_sun_tilt.py <bench dir> 2026-09-22 2026-10-08
    python3 momentum_sun_tilt.py <bench dir> 2026-10-09 2026-10-25 --fixed-lambda 0.33
"""
import argparse, collections, math, random
from datetime import date
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument('bench')
ap.add_argument('start')
ap.add_argument('end')
ap.add_argument('--fixed-lambda', type=float, default=None)
args = ap.parse_args()

src = (Path(__file__).with_name('momentum_rule_all.py')).read_text()
src = src[:src.index("print('=== ECMWF momentum M as a rule")]
days = (date.fromisoformat(args.end) - date.fromisoformat(args.start)).days + 1
old_dates = "DATES = [(date(2026, 9, 22) + timedelta(days=i)).isoformat() for i in range(17)]"
old_sql = "between '2026-09-22' and '2026-10-08'"
assert src.count(old_dates) == 1 and src.count(old_sql) == 1
s, e = date.fromisoformat(args.start), date.fromisoformat(args.end)
src = src.replace(old_dates, f"DATES = [(date({s.year}, {s.month}, {s.day}) + timedelta(days=i)).isoformat() for i in range({days})]")
src = src.replace(old_sql, f"between '{args.start}' and '{args.end}'")
ns = {'__name__': 'momentum_rule_all_instances'}
import sys
sys.argv = [sys.argv[0], args.bench]
exec(compile(src, 'momentum_rule_all.py', 'exec'), ns)
inst, ev = ns['inst'], ns['ev']

sg = lambda x: 1 if x > 0 else -1
rows = []
for r in inst:
    if r['rad'] is None or abs(r['M']) < 1:
        continue
    out, tot = r['lad']
    q = {o: (bid + ask) / 2 / tot for bk, (o, bid, ask) in out.items()}
    tord = next((o for bk, (o, bid, ask) in out.items() if bk == r['truth']), None)
    e_ = ev(r, sg(r['M']))
    if tord is None or e_ is None:
        continue
    rows.append(dict(ed=r['ed'], side=sg(r['M']), same=sg(r['M']) == sg(r['rad']), q=q,
                     mode=max(q, key=q.get), truth=tord, score=e_['score'], taker=e_.get('taker')))


def d_of(r, lam):
    m, side = r['mode'], r['side']
    z = sum(v * math.exp(lam * side * ((o > m) - (o < m))) for o, v in r['q'].items())
    return lam * side * ((r['truth'] > m) - (r['truth'] < m)) - math.log(z)


def fit(sub):
    return max((k / 100 for k in range(121)), key=lambda lam: sum(d_of(r, lam) for r in sub))


def boot(pairs, n=2000):
    random.seed(1010)
    by = collections.defaultdict(list)
    for d, v in pairs:
        by[d].append(v)
    ks = list(by)
    res = sorted(sum(x) / len(x) for x in ([v for d in (random.choice(ks) for _ in ks) for v in by[d]] for _ in range(n)))
    return res[int(.025 * n)], res[int(.975 * n) - 1]


def report(label, sub):
    if not sub:
        print(f'{label}: no instances')
        return
    if args.fixed_lambda is not None:
        d = [(r['ed'], d_of(r, args.fixed_lambda)) for r in sub]
        how = f'lam fixed {args.fixed_lambda:.2f}'
    else:
        d = []
        for day in sorted({r['ed'] for r in sub}):
            lam = fit([r for r in sub if r['ed'] != day])
            d += [(r['ed'], d_of(r, lam)) for r in sub if r['ed'] == day]
        how = f'lam LODO (full-sample {fit(sub):.2f})'
    parts = []
    for name, pairs, sc in (('d nat', d, 1), ('direction', [(r['ed'], r['score']) for r in sub], 1),
                            ('taker c/share', [(r['ed'], r['taker']) for r in sub if r['taker'] is not None], 100)):
        lo, hi = boot(pairs)
        parts.append(f"{name} {sc * sum(v for _, v in pairs) / len(pairs):+.4f} [{sc * lo:+.4f},{sc * hi:+.4f}]")
    byday = collections.defaultdict(float)
    for day, v in d:
        byday[day] += v
    mk = sum(sum(v for o, v in r['q'].items() if (o - r['mode']) * r['side'] > 0) for r in sub) / len(sub)
    real = sum(1 for r in sub if (r['truth'] - r['mode']) * r['side'] > 0) / len(sub)
    print(f"{label:22s} n={len(sub):4d} days={len(byday):2d} {how} | " + ' | '.join(parts)
          + f" | d>0 days {sum(1 for v in byday.values() if v > 0)}/{len(byday)} | P(momentum side) market {mk:.3f} real {real:.3f}")


print(f'window {args.start}..{args.end}  instances with |M|>=1 and ICON known: {len(rows)}')
report('|M|>=1, ICON same', [r for r in rows if r['same']])
report('|M|>=1, ICON opposite', [r for r in rows if not r['same']])
report('|M|>=1, all', rows)
