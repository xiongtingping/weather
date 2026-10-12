"""Round 40d: the cluster's pick as bucket(F + e), e ~ Normal(mu, sigma) in
bucket widths -- maximum likelihood over the pre-formation first YES; the
single-bucket match that sigma allows (picking the most likely bucket given
F's position), and the match table by F's position in its bucket.
"""
import math
import sys
from pathlib import Path

here = Path(__file__)
src = here.with_name("cluster_pick_model_r40.py").read_text()
ns = {"__name__": "r40", "__file__": str(here.with_name("cluster_pick_model_r40.py"))}
exec(compile(src.split("all_events = len(events)")[0], "r40_head", "exec"), ns)
events = ns["events"]


def cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def loglik(mu, sigma):
    total = 0.0
    for ev in events:
        lo, hi = ev["edges"][ev["choice"]]
        w = ev["width"]
        a = ((lo - ev["f"]) / w - mu) / sigma
        b = ((hi - ev["f"]) / w - mu) / sigma
        total += math.log(max(1e-9, cdf(b) - cdf(a)))
    return total


best = max(((loglik(mu / 100, s / 100), mu / 100, s / 100) for mu in range(-40, 41, 2) for s in range(20, 151, 2)))
_, mu, sigma = best
# Best single-bucket rule under the fitted law: the bucket holding most of F+e.
hit = 0
for ev in events:
    w = ev["width"]
    probs = []
    for j, (lo, hi) in enumerate(ev["edges"]):
        a = ((lo - ev["f"]) / w - mu) / sigma
        b = ((hi - ev["f"]) / w - mu) / sigma
        probs.append(cdf(b) - cdf(a))
    hit += max(range(len(probs)), key=lambda j: probs[j]) == ev["choice"]
print(f"fitted e ~ Normal(mu {mu:+.2f}, sigma {sigma:.2f}) bucket widths over {len(events)} picks;"
      f" best single bucket under it matches {hit / len(events):.1%} (in sample)")
for target in (0.6, 0.7, 0.8, 0.9):
    # sigma that would let a centred F match at this rate on average over positions
    for s in [x / 100 for x in range(5, 150)]:
        avg = sum(cdf((1 - u) / s) - cdf(-u / s) for u in [k / 100 + 0.005 for k in range(100)]) / 100
        if avg <= target:
            print(f"   a {target:.0%} single-bucket match needs sigma <= {s:.2f} bucket widths (~{s:.2f} C on C stations)")
            break
bins = [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.0)]
for lo_b, hi_b in bins:
    sub = []
    for ev in events:
        lo, hi = ev["edges"][ev["fb"]]
        u = (ev["f"] - lo) / (hi - lo)
        if lo_b <= u < hi_b or (hi_b == 1.0 and u >= 1.0):
            sub.append(ev)
    n = max(1, len(sub))
    print(f"   F at {lo_b:.1f}-{hi_b:.1f} of its bucket (n {len(sub):3d}): cluster = F {sum(e['choice'] == e['fb'] for e in sub) / n:.0%},"
          f" one up {sum(e['choice'] == e['fb'] + 1 for e in sub) / n:.0%}, one down {sum(e['choice'] == e['fb'] - 1 for e in sub) / n:.0%}")
