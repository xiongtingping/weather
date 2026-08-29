"""H4: how strong is the rejection of "0 of 19" really?

Exact Poisson-binomial under the model's own per-case claims; concentration by
station and date; jackknife the biggest contributor; cluster-conservative
bounds; Clopper-Pearson (bootstrap degenerates at zero successes).
"""
import json, math, random, statistics
from collections import Counter

rows = json.load(open("tmp/h4_cohort.json"))
tail = [r for r in rows if r["edge"] > .25]
posedge = [r for r in rows if r["edge"] > 0]

def pb(ps):
    """Exact Poisson-binomial pmf over the number of successes."""
    d = [1.0]
    for p in ps:
        nd = [0.0] * (len(d) + 1)
        for i, v in enumerate(d):
            nd[i] += v * (1 - p)
            nd[i + 1] += v * p
        d = nd
    return d

def report(name, sel):
    ps = [r["p"] for r in sel]
    hits = sum(r["won"] for r in sel)
    d = pb(ps)
    exp = sum(ps)
    p_at_most = sum(d[:hits + 1])
    print("\n== %s  n=%d" % (name, len(sel)))
    print("   claimed per case: min %.3f  median %.3f  max %.3f  sum(expected hits) %.2f"
          % (min(ps), statistics.median(ps), max(ps), exp))
    print("   observed hits %d" % hits)
    print("   P(exactly %d) = %.5f   P(<=%d) = %.5f   => %.2f sigma-equivalent, 1/%.0f"
          % (hits, d[hits], hits, p_at_most,
             abs(_z(p_at_most)), 1 / max(p_at_most, 1e-300)))
    return d, p_at_most

def _z(p):
    # two-sided-free: one-sided normal quantile of p
    if p <= 0: return float("inf")
    lo, hi = 0.0, 40.0
    for _ in range(200):
        m = (lo + hi) / 2
        if 0.5 * math.erfc(m / math.sqrt(2)) > p: lo = m
        else: hi = m
    return (lo + hi) / 2

print("=== 1. EXACT POISSON-BINOMIAL UNDER THE MODEL'S OWN CLAIMS")
report("edge > 0.25", tail)
report("edge > 0 (all three positive bands)", posedge)
report("edge <= 0 (control: does the same test flag it?)",
       [r for r in rows if r["edge"] <= 0])

print("\n=== 2. CONCENTRATION OF THE 19")
st = Counter(r["station"] for r in tail)
dt = Counter(r["date"] for r in tail)
print("   distinct stations %d  distinct dates %d" % (len(st), len(dt)))
print("   by station:", dict(st.most_common()))
print("   by date   :", dict(dt.most_common()))
print("   date span : %s .. %s" % (min(dt), max(dt)))

print("\n=== 3. JACKKNIFE: drop each station entirely")
for s, n in st.most_common():
    rest = [r for r in tail if r["station"] != s]
    ps = [r["p"] for r in rest]
    d = pb(ps)
    print("   drop %-5s (-%d) -> n=%2d  claimed mean %3.0f%%  hits %d  P(0)=%.4f"
          % (s, n, len(rest), 100 * statistics.mean(ps),
             sum(r["won"] for r in rest), d[0]))
print("   drop each DATE:")
for s, n in dt.most_common()[:5]:
    rest = [r for r in tail if r["date"] != s]
    d = pb([r["p"] for r in rest])
    print("   drop %-12s (-%d) -> n=%2d  P(0)=%.4f" % (s, n, len(rest), d[0]))

print("\n=== 4. CLUSTER-CONSERVATIVE BOUND (weather is correlated within a date)")
for key in ("station", "date"):
    g = {}
    for r in tail:
        g.setdefault(r[key], []).append(r["p"])
    # worst case: each cluster is one perfectly-correlated draw at its max p
    d = pb([max(v) for v in g.values()])
    print("   collapse to one draw per %-7s -> clusters=%2d  P(0 in all)=%.4f"
          % (key, len(g), d[0]))

print("\n=== 5. INTERVAL FOR THE HIT RATE (0/19)")
n, k = len(tail), sum(r["won"] for r in tail)
hi = 1 - (0.025) ** (1 / n) if k == 0 else None
print("   Clopper-Pearson 95%% upper bound on the true rate: %.1f%%" % (100 * hi))
random.seed(7)
bs = [sum(random.choice(tail)["won"] for _ in range(n)) / n for _ in range(20000)]
print("   nonparametric bootstrap of the hit rate: 2.5%%=%.3f 97.5%%=%.3f"
      " (degenerate by construction at 0 successes -- not informative)"
      % (sorted(bs)[500], sorted(bs)[19500]))

print("\n=== 6. IS THE PATTERN MONOTONE ACROSS BANDS, OR ONE BAND?")
def band(lo, hi):
    return [r for r in rows if (r["edge"] <= hi if lo is None else lo < r["edge"] <= hi)]
for lab, lo, hi in (("<=0", None, 0), ("0-0.10", 0, .10),
                    ("0.10-0.25", .10, .25), (">0.25", .25, 9)):
    s = band(lo, hi)
    d = pb([r["p"] for r in s])
    print("   %-10s n=%2d  claimed %4.1f  observed %d  P(<=obs)=%.4f"
          % (lab, len(s), sum(r["p"] for r in s), sum(r["won"] for r in s),
             sum(d[:sum(r["won"] for r in s) + 1])))
# rank test: does higher edge predict a worse outcome?
obs = sum(1 for r in rows if r["edge"] > 0 and r["won"])
allr = rows[:]
random.seed(11)
cnt = 0
for _ in range(20000):
    random.shuffle(allr)
    wins = [r["won"] for r in allr]
    edges = [r["edge"] for r in rows]
    c = sum(w for w, e in zip(wins, edges) if e > 0)
    cnt += (c <= obs)
print("   permutation: P(positive-edge group gets <=%d hits | outcomes shuffled"
      " across all 104) = %.4f" % (obs, cnt / 20000))
