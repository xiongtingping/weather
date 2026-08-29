"""How far off would the claim have to be for 0/19 to be unremarkable, and
were the misses near-misses?  Also: was the market's 1% consistent with 0/19?
"""
import json, math, statistics
from collections import Counter
rows = json.load(open("tmp/h4_cohort.json"))
tail = [r for r in rows if r["edge"] > .25]
ps = [r["p"] for r in tail]
n = len(tail)

# uniform shrink factor c applied to every claim: P(0) = prod(1-c*p_i)
def p0(c):
    v = 1.0
    for p in ps:
        v *= (1 - min(c * p, 1.0))
    return v
lo, hi = 0.0, 1.0
for _ in range(200):
    m = (lo + hi) / 2
    if p0(m) > 0.05: lo = m
    else: hi = m
c = (lo + hi) / 2
print("=== how big is the overclaim, quantified")
print("   claims must be multiplied by %.2f (mean claim %3.0f%% -> %3.0f%%)"
      " before 0/19 stops being a 5%%-level surprise"
      % (c, 100 * statistics.mean(ps), 100 * c * statistics.mean(ps)))
lo, hi = 0.0, 1.0
for _ in range(200):
    m = (lo + hi) / 2
    if p0(m) > 0.50: lo = m
    else: hi = m
print("   multiplied by %.2f (-> %3.0f%%) for 0/19 to be the MEDIAN outcome"
      % ((lo + hi) / 2, 100 * (lo + hi) / 2 * statistics.mean(ps)))

print("\n=== was the market's price consistent with 0/19?")
asks = [r["ask"] for r in tail]
v = 1.0
for a in asks: v *= (1 - a)
print("   mean ask %4.1f%%  expected hits %.2f  P(0 | market) = %.3f"
      % (100 * statistics.mean(asks), sum(asks), v))

print("\n=== were the 19 near-misses? (bucket distance target vs settled)")
d = Counter(abs(r["t_ord"] - r["truth_ord"]) for r in tail)
print("   |target - settled| :", dict(sorted(d.items())))
print("   target ABOVE settled: %d / %d" %
      (sum(1 for r in tail if r["t_ord"] > r["truth_ord"]), n))
print("   median lead at the decision: %.2f h before local midnight"
      % statistics.median(r["lead_h"] for r in tail))

print("\n=== same test on the model's good news, as a sanity check")
band = [r for r in rows if r["p"] >= 0.5]
if band:
    v = 1.0
    dist = [1.0]
    for p in [r["p"] for r in band]:
        nd = [0.0] * (len(dist) + 1)
        for i, x in enumerate(dist):
            nd[i] += x * (1 - p); nd[i + 1] += x * p
        dist = nd
    k = sum(r["won"] for r in band)
    print("   cohort rows claiming >=50%%: n=%d expected %.1f observed %d P(<=obs)=%.3f"
          % (len(band), sum(r["p"] for r in band), k, sum(dist[:k + 1])))
