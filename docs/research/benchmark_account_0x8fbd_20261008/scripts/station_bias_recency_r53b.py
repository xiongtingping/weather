"""Round 53b: the one calibration family that moved -- a shorter / recency-weighted
station bias (round 53: window 14 or half-life 10-20 days at lambda 0.5 gave
50.7% vs production F 45.8%, both halves, truth hits +2-3 points).
(a) finer grid: window 7/10/14/21/30/60 and half-life 5/7/10/14/20/30 days x
    lambda 0.4/0.5/0.6/0.75 -- agreement, hits; the recipe chosen leave one
    event day out (honest number).
(b) mechanism: does the cluster's money centre lean with the RECENT bias beyond
    the 60-day one?  centre - F_base on (corr_recent - corr_60), day-block
    bootstrap.
(c) on the truth, every station-day 09-24..10-13 with >= 3 members (not only
    the cluster's picks): MAE and bucket hits of each recipe against the
    60-day mean, paired, both halves -- is it simply a better F?
"""
import collections
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

here = Path(__file__)
src = here.with_name("station_calibration_methods_r53.py").read_text()
ns = {"__name__": "r53", "__file__": str(here.with_name("station_calibration_methods_r53.py"))}
exec(compile(src.split("# fleet residual shape")[0], "r53_head", "exec"), ns)
events, bucket, H, truth, fleet_c, offset_centre, member_bias, paired = (ns[k] for k in ("events", "bucket", "H", "truth", "fleet_c", "offset_centre", "member_bias", "paired"))
B = Path(sys.argv[1])
src45 = here.with_name("cluster_money_target_r45.py").read_text()
ns45 = {"__name__": "r45", "__file__": str(here.with_name("cluster_money_target_r45.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src45.split('print(f"{len(rows)} of {len(events)}')[0], "r45_head", "exec"), ns45)
rows45 = ns45["rows"]
FR_ALL = ns45["ns"]["FR_ALL"]
edges_of = ns45["ns"]["edges_of"]
centre_1h = {(r["ev"]["st"], r["ev"]["ed"]): r["win"]["1 h"]["centre"] for r in rows45 if "1 h" in r["win"]}

RECIPES = {}
for w in (7, 10, 14, 21, 30, 60):
    RECIPES[f"window {w}"] = {"window": w}
for h in (5, 7, 10, 14, 20, 30):
    RECIPES[f"half-life {h}"] = {"kind": f"ew{h}", "window": 90}
LAMS = (0.4, 0.5, 0.6, 0.75)

used = [ev for ev in events if (ev["st"], date.fromisoformat(ev["ed"])) in H and len(H[(ev["st"], date.fromisoformat(ev["ed"]))]) >= 3]
days = sorted({ev["ed"] for ev in used})
mid = days[len(days) // 2]
cache = {}


def centre_for(ev, name, lam):
    key = (id(ev), name, lam)
    if key not in cache:
        st, D = ev["st"], date.fromisoformat(ev["ed"])
        cache[key] = offset_centre(st, D, H[(st, D)], lam, fleet_c(D), **RECIPES[name])
    return cache[key]


print(f"(a) {len(used)} picks; production F = cluster {statistics.mean(ev['fb'] == ev['choice'] for ev in used):.1%}, hits {statistics.mean(ev['fb'] == ev['truth'] for ev in used if ev['truth'] is not None):.1%}")
print(f"    {'recipe':14s} " + " ".join(f"{'lam ' + str(l):>22s}" for l in LAMS))
grid = {}
for name in RECIPES:
    cells = []
    for lam in LAMS:
        same = [bucket(ev["edges"], centre_for(ev, name, lam)) == ev["choice"] for ev in used]
        hits = [bucket(ev["edges"], centre_for(ev, name, lam)) == ev["truth"] for ev in used if ev["truth"] is not None]
        h1 = statistics.mean(s for s, ev in zip(same, used) if ev["ed"] < mid) - statistics.mean(ev["fb"] == ev["choice"] for ev in used if ev["ed"] < mid)
        h2 = statistics.mean(s for s, ev in zip(same, used) if ev["ed"] >= mid) - statistics.mean(ev["fb"] == ev["choice"] for ev in used if ev["ed"] >= mid)
        grid[(name, lam)] = statistics.mean(same)
        cells.append(f"{statistics.mean(same):5.1%}/{statistics.mean(hits):5.1%}{'**' if h1 > 0 and h2 > 0 else '  '}")
    print(f"    {name:14s} " + " ".join(f"{c:>22s}" for c in cells))
# honest: recipe x lambda chosen leave one event day out
picks, chosen = [], collections.Counter()
for ev in used:
    tr = [x for x in used if x["ed"] != ev["ed"]]
    best = max(((name, lam) for name in RECIPES for lam in LAMS),
               key=lambda k: (sum(bucket(x["edges"], centre_for(x, k[0], k[1])) == x["choice"] for x in tr), -k[1]))
    chosen[best] += 1
    picks.append(bucket(ev["edges"], centre_for(ev, best[0], best[1])))
same = [p == ev["choice"] for p, ev in zip(picks, used)]
base = [ev["fb"] == ev["choice"] for ev in used]
tr_ = [(p, ev) for p, ev in zip(picks, used) if ev["truth"] is not None]
print(f"    LODO-chosen recipe: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)[1]};"
      f" hits {statistics.mean(p == ev['truth'] for p, ev in tr_):.1%} vs F {statistics.mean(ev['fb'] == ev['truth'] for _p, ev in tr_):.1%}; chosen {dict(chosen.most_common(4))}")

# (b) mechanism
print("\n(b) the cluster's money centre against the recent-minus-60-day station correction")
rng = np.random.default_rng(53)
for name in ("window 14", "half-life 10", "half-life 20"):
    xs, ys, dd = [], [], []
    for ev in used:
        key = (ev["st"], ev["ed"])
        if key not in centre_1h:
            continue
        st, D = ev["st"], date.fromisoformat(ev["ed"])
        mem = H[(st, D)]
        raw = statistics.median(mem.values())
        corr60 = statistics.median(mem[m] + b for m, b in member_bias(st, D, mem).items()) - raw
        corr_r = statistics.median(mem[m] + b for m, b in member_bias(st, D, mem, **RECIPES[name]).items()) - raw
        f_base = raw + fleet_c(D) + 0.5 * corr60
        xs.append([corr_r - corr60, corr60, 1.0])
        ys.append(centre_1h[key] - f_base)
        dd.append(ev["ed"])
    X, y, dd = np.array(xs), np.array(ys), np.array(dd)
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    ud = sorted(set(dd))
    bs = []
    for _ in range(600):
        idx = np.concatenate([np.where(dd == d)[0] for d in rng.choice(ud, size=len(ud), replace=True)])
        bs.append(np.linalg.lstsq(X[idx], y[idx], rcond=None)[0][0])
    print(f"    {name:12s}: centre - F_base = {b[0]:+.2f} [{np.percentile(bs, 2.5):+.2f}, {np.percentile(bs, 97.5):+.2f}] x (corr_recent - corr_60) {b[1]:+.2f} x corr_60 {b[2]:+.2f};"
          f" sd of (corr_recent - corr_60) {X[:, 0].std():.2f} C, n {len(y)}")

# (c) on the truth, every station-day
print("\n(c) every station-day 09-24..10-13 with >= 3 members and a truth: recipes at lambda 0.5 against the 60-day mean")
A = []
for r in FR_ALL:
    if r.get("truth_c") is None or len(r["labels"]) < 5:
        continue
    st, D = r["st"], date.fromisoformat(r["ed"])
    if (st, D) not in H or len(H[(st, D)]) < 3:
        continue
    A.append({"st": st, "D": D, "ed": r["ed"], "y": float(r["truth_c"]), "edges": edges_of(r)})
daysA = sorted({a["ed"] for a in A})
midA = daysA[len(daysA) // 2]
by_day = {d: [i for i, a in enumerate(A) if a["ed"] == d] for d in daysA}
ref = [offset_centre(a["st"], a["D"], H[(a["st"], a["D"])], 0.5, fleet_c(a["D"])) for a in A]
ref_hit = [bucket(a["edges"], c) == bucket(a["edges"], a["y"]) for a, c in zip(A, ref)]
print(f"    n {len(A)}, {len(daysA)} event days; 60-day mean: MAE {np.mean([abs(a['y'] - c) for a, c in zip(A, ref)]):.4f}, hits {statistics.mean(ref_hit):.1%}")
for name in RECIPES:
    if name == "window 60":
        continue
    cs = [offset_centre(a["st"], a["D"], H[(a["st"], a["D"])], 0.5, fleet_c(a["D"]), **RECIPES[name]) for a in A]
    dm = np.array([abs(a["y"] - c) - abs(a["y"] - r0) for a, c, r0 in zip(A, cs, ref)])
    bs = [dm[np.concatenate([by_day[d] for d in rng.choice(daysA, size=len(daysA), replace=True)])].mean() for _ in range(600)]
    hit = [bucket(a["edges"], c) == bucket(a["edges"], a["y"]) for a, c in zip(A, cs)]
    h1 = statistics.mean(h for h, a in zip(hit, A) if a["ed"] < midA) - statistics.mean(h for h, a in zip(ref_hit, A) if a["ed"] < midA)
    h2 = statistics.mean(h for h, a in zip(hit, A) if a["ed"] >= midA) - statistics.mean(h for h, a in zip(ref_hit, A) if a["ed"] >= midA)
    print(f"    {name:12s}: MAE {dm.mean():+.4f} [{np.percentile(bs, 2.5):+.4f}, {np.percentile(bs, 97.5):+.4f}]; hits {statistics.mean(hit):.1%} vs {statistics.mean(ref_hit):.1%} {paired(hit, ref_hit)[1]} halves {h1:+.3f}/{h2:+.3f}")
