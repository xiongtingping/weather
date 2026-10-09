"""Round 18: which observable signal is their direction call?

For every candidate signal X (deg C, + = warmer than the market mean):
  agree    share of events (|X| >= tau) where sign(X) is their direction
  X skill  direction score of the rule 'go sign(X)' on the same events:
           truth past the mode on that side minus the other side, minus the
           market's own expectation of that difference
  theirs   their direction score on the same events, split by whether X
           agrees with them.  An input of theirs must agree well above 0.5,
           carry skill of its own, and their skill should sit in the agreeing
           half.
"""
import json, collections, random, sys, math
B = sys.argv[1]
R = json.load(open(f'{B}/direction_rows.json'))
random.seed(18)
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}


def dscore(r, side):
    sd = lambda o: (o - r['mode']) * side
    D = 1 if sd(r['truth']) > 0 else -1 if sd(r['truth']) < 0 else 0
    ED = sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in r['q'].items())
    return D - ED


def boot(rs, f, n=400):
    G = collections.defaultdict(list)
    for r in rs: G[(r['station'], r['ed'])].append(r)
    keys = list(G); vals = []
    for _ in range(n):
        s = [x for k in (random.choice(keys) for _ in keys) for x in G[k]]
        vals.append(sum(f(x) for x in s) / len(s))
    vals.sort()
    return vals[int(.025 * n)], vals[int(.975 * n) - 1]


keys = collections.Counter(k for r in R for k in r['sig'])
print(f'events {len(R)}; their own direction score {sum(dscore(r, r["s"]) for r in R)/len(R):+.3f}')
for tau in (0.3,):
    print(f'\n=== tau = {tau} C  (signals with >= 80 events) ===')
    print(f'   {"signal":40s}   n   agree  95%CI          X-skill [95%CI]          theirs|agree  theirs|disagree')
    res = []
    for k, cnt in keys.items():
        rs = [r for r in R if k in r['sig'] and abs(r['sig'][k]) >= tau]
        if len(rs) < 80:
            continue
        ag = sum(1 for r in rs if (1 if r['sig'][k] > 0 else -1) == r['s']) / len(rs)
        alo, ahi = boot(rs, lambda r: 1.0 if (1 if r['sig'][k] > 0 else -1) == r['s'] else 0.0)
        sk = sum(dscore(r, 1 if r['sig'][k] > 0 else -1) for r in rs) / len(rs)
        slo, shi = boot(rs, lambda r: dscore(r, 1 if r['sig'][k] > 0 else -1))
        A = [r for r in rs if (1 if r['sig'][k] > 0 else -1) == r['s']]
        D = [r for r in rs if (1 if r['sig'][k] > 0 else -1) != r['s']]
        ta = sum(dscore(r, r['s']) for r in A) / len(A) if A else float('nan')
        td = sum(dscore(r, r['s']) for r in D) / len(D) if D else float('nan')
        res.append((ag, k, len(rs), alo, ahi, sk, slo, shi, ta, td, len(A), len(D)))
    for ag, k, n, alo, ahi, sk, slo, shi, ta, td, na, nd in sorted(res, reverse=True):
        print(f'   {k:40s} {n:4d}  {ag:.3f} [{alo:.3f},{ahi:.3f}]  {sk:+.3f} [{slo:+.3f},{shi:+.3f}]   {ta:+.3f} (n={na:3d})  {td:+.3f} (n={nd:3d})')
