"""Is the 19 unusually concentrated, and does real within-date correlation
survive the null?  Measure the intra-date agreement on the full 104-case
cohort, then re-run the null with a shared date factor at that strength.
"""
import json, math, random, statistics
from collections import Counter

rows = json.load(open("tmp/h4_cohort.json"))
tail = [r for r in rows if r["edge"] > .25]

print("=== A. IS THE TAIL GROUP CONCENTRATED RELATIVE TO THE COHORT ITSELF?")
ad, td = Counter(r["date"] for r in rows), Counter(r["date"] for r in tail)
astat, tstat = Counter(r["station"] for r in rows), Counter(r["station"] for r in tail)
print("   all 104: %d dates, %d stations | tail 19: %d dates, %d stations"
      % (len(ad), len(astat), len(td), len(tstat)))
print("   date   all / tail")
for d in sorted(ad):
    print("     %-11s %3d / %2d" % (d, ad[d], td.get(d, 0)))
# how surprising is the tail's date spread if 19 were drawn at random from 104?
random.seed(3)
pool = [r["date"] for r in rows]
sp = []
for _ in range(20000):
    sp.append(len(set(random.sample(pool, len(tail)))))
print("   distinct dates in a random 19 of 104: mean %.1f  observed %d  "
      "P(<=obs)=%.3f" % (statistics.mean(sp), len(td),
                         sum(1 for x in sp if x <= len(td)) / len(sp)))
ps = [r["station"] for r in rows]
sp2 = [len(set(random.sample(ps, len(tail)))) for _ in range(20000)]
print("   distinct stations in a random 19 of 104: mean %.1f  observed %d  "
      "P(<=obs)=%.3f" % (statistics.mean(sp2), len(tstat),
                         sum(1 for x in sp2 if x <= len(tstat)) / len(sp2)))

print("\n=== B. MEASURED WITHIN-DATE OUTCOME CORRELATION (all 104)")
byd = {}
for r in rows:
    byd.setdefault(r["date"], []).append(r)
# Pearson correlation over all same-date pairs of (won - p) residuals
pairs = []
for d, g in byd.items():
    for i in range(len(g)):
        for j in range(i + 1, len(g)):
            a, b = g[i], g[j]
            pairs.append(((a["won"] - a["p"]) / math.sqrt(a["p"] * (1 - a["p"])),
                          (b["won"] - b["p"]) / math.sqrt(b["p"] * (1 - b["p"]))))
rho = (sum(x * y for x, y in pairs) / len(pairs)) if pairs else 0.0
print("   same-date residual pairs n=%d  mean product (ICC estimate) = %+.3f"
      % (len(pairs), rho))
cross = []
keys = list(byd)
for _ in range(len(pairs)):
    d1, d2 = random.sample(keys, 2)
    a, b = random.choice(byd[d1]), random.choice(byd[d2])
    cross.append(((a["won"] - a["p"]) / math.sqrt(a["p"] * (1 - a["p"]))) *
                 ((b["won"] - b["p"]) / math.sqrt(b["p"] * (1 - b["p"]))))
print("   cross-date control: mean product = %+.3f" % statistics.mean(cross))

print("\n=== C. NULL WITH A SHARED DATE FACTOR AT THE MEASURED STRENGTH")
def sim(icc, trials=200000):
    g = {}
    for r in tail:
        g.setdefault(r["date"], []).append(r["p"])
    z = {}
    zero = 0
    for _ in range(trials):
        f = {d: random.gauss(0, 1) for d in g}
        hits = 0
        for d, plist in g.items():
            for p in plist:
                # Gaussian copula: latent = sqrt(icc)*date + sqrt(1-icc)*idio
                lat = math.sqrt(icc) * f[d] + math.sqrt(max(0.0, 1 - icc)) * random.gauss(0, 1)
                thr = _ppf(p)
                if lat < thr:
                    hits += 1
        zero += (hits == 0)
    return zero / trials

def _ppf(p):
    lo, hi = -10.0, 10.0
    for _ in range(80):
        m = (lo + hi) / 2
        if 0.5 * math.erfc(-m / math.sqrt(2)) < p: lo = m
        else: hi = m
    return (lo + hi) / 2

for icc in (0.0, max(rho, 0.0), 0.15, 0.30, 0.50):
    print("   ICC=%.2f -> P(0 of 19 | model's own claims) = %.4f" % (icc, sim(icc, 60000)))
