"""Round 49b (熊先生 10-12 「既然显著，为什么没有改善？你计算有误吧」).

Round 49 reported F 0.39 C warm on cloudy days (forecast daytime cloud > 70%,
day3) over 717 station-days, "significant", yet a nine-feature physics
regression left F's MAE and hits unchanged.  Checked here, plainly:
(a) the cloudy days' error distribution: mean, median, quantiles, share of
    big cold misses; the interval by a day-block bootstrap (station-days on
    one date share weather) next to the naive one;
(b) in sample: F shifted by s on cloudy days only, s = 0..-0.6 -- MAE and
    bucket hits (what a mean shift buys when the errors are skewed);
(c) out of sample, leave one event day out: the shift learned on the other
    days -- their cloudy mean, their cloudy median, and a one-feature line
    truth - F = a + b cloud -- MAE and hits against F;
(d) the round 49 regression refitted to print its cloud coefficient (bug check).
"""
import math
import statistics
import sys
from datetime import date
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("cluster_money_target_r45.py").read_text()
ns = {"__name__": "r45", "__file__": str(here.with_name("cluster_money_target_r45.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split('print(f"{len(rows)} of {len(events)}')[0], "r45_head", "exec"), ns)
bucket, prod_f, FR_ALL = ns["bucket"], ns["prod_f"], ns["FR_ALL"]
edges_of = ns["ns"]["edges_of"]
src49 = here.with_name("physics_vs_cluster_r49.py").read_text()
ns49 = {"B": B, "json": __import__("json"), "math": math, "statistics": statistics, "date": date,
        "timedelta": __import__("datetime").timedelta}
exec(compile(src49[src49.index("PH = json.load"):src49.index("def paired")] + src49[src49.index("def day_value"):src49.index("KEYS = [")],
             "r49_defs", "exec"), ns49)
phys = ns49["phys"]
KEYS = ["cloud", "sw", "precip", "cloud_anom", "sw_anom", "precip_anom", "cloud_chg", "sw_chg", "precip_chg"]


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


allr = []
for r in FR_ALL:
    if r.get("truth_c") is None or len(r["labels"]) < 5:
        continue
    e = prod_f(int(r["listed"]) + 300)["station_days"].get(f"{r['st']}|{r['ed']}")
    if not e or e.get("state") != "READY":
        continue
    p = phys(r["st"], date.fromisoformat(r["ed"]), "previous_day3")
    if not p:
        continue
    allr.append({"st": r["st"], "f": e["f_c"], "p": p, "y": float(r["truth_c"]), "edges": edges_of(r), "ed": r["ed"]})
err = np.array([a["y"] - a["f"] for a in allr])
cloudy = np.array([a["p"]["cloud"] > 70 for a in allr])
print(f"{len(allr)} station-days, {cloudy.sum()} cloudy, {len({a['ed'] for a in allr})} event days")

# (a)
e = err[cloudy]
print("\n(a) cloudy days' truth - F (C)")
print(f"    mean {e.mean():+.3f}, median {np.median(e):+.3f}, quantiles 10/25/75/90%: "
      + ", ".join(f"{np.percentile(e, q):+.2f}" for q in (10, 25, 75, 90)) + f"; share < -2 C {np.mean(e < -2):.1%}, > +2 C {np.mean(e > 2):.1%}")
print(f"    other days: mean {err[~cloudy].mean():+.3f}, median {np.median(err[~cloudy]):+.3f}")
print(f"    naive 95% interval of the cloudy mean: +-{1.96 * e.std() / len(e) ** 0.5:.3f}")
days = sorted({a["ed"] for a in allr})
rng = np.random.default_rng(1)
bm, bmd, bdiff = [], [], []
by_day = {d: [i for i, a in enumerate(allr) if a["ed"] == d] for d in days}
for _ in range(2000):
    idx = [i for d in rng.choice(days, size=len(days), replace=True) for i in by_day[d]]
    sel = np.array(idx)
    c = cloudy[sel]
    if c.sum() == 0 or (~c).sum() == 0:
        continue
    bm.append(err[sel][c].mean())
    bmd.append(np.median(err[sel][c]))
    bdiff.append(err[sel][c].mean() - err[sel][~c].mean())
print(f"    day-block bootstrap 95%: cloudy mean [{np.percentile(bm, 2.5):+.3f}, {np.percentile(bm, 97.5):+.3f}],"
      f" cloudy median [{np.percentile(bmd, 2.5):+.3f}, {np.percentile(bmd, 97.5):+.3f}],"
      f" cloudy - other mean [{np.percentile(bdiff, 2.5):+.3f}, {np.percentile(bdiff, 97.5):+.3f}]")

# (b) in sample
print("\n(b) in sample: shift F by s on cloudy days only")
base_mae = np.abs(err).mean()
base_hit = [bucket(a["edges"], a["f"]) == bucket(a["edges"], a["y"]) for a in allr]
for s in (0.0, -0.1, -0.2, -0.3, -0.39, -0.5, -0.6):
    f2 = [a["f"] + (s if c else 0.0) for a, c in zip(allr, cloudy)]
    mae = np.mean([abs(a["y"] - x) for a, x in zip(allr, f2)])
    hit = [bucket(a["edges"], x) == bucket(a["edges"], a["y"]) for a, x in zip(allr, f2)]
    ch = [h for h, c in zip(hit, cloudy) if c]
    cb = [h for h, c in zip(base_hit, cloudy) if c]
    print(f"    s {s:+.2f}: MAE {mae:.4f} (F {base_mae:.4f}); hits all {statistics.mean(hit):.1%} (F {statistics.mean(base_hit):.1%});"
          f" cloudy days hits {statistics.mean(ch):.1%} (F {statistics.mean(cb):.1%}) {paired(ch, cb)}")

# (c) out of sample, leave one event day out
print("\n(c) leave one event day out")
for label in ("cloudy mean shift", "cloudy median shift", "line a + b cloud"):
    pred = np.zeros(len(allr))
    for d in days:
        tr = np.array([a["ed"] != d for a in allr])
        te = ~tr
        if label == "line a + b cloud":
            X = np.c_[[a["p"]["cloud"] for a in allr], np.ones(len(allr))]
            b = np.linalg.lstsq(X[tr], err[tr], rcond=None)[0]
            pred[te] = X[te] @ b
        else:
            ref = err[tr & cloudy]
            shift = ref.mean() if label == "cloudy mean shift" else float(np.median(ref))
            pred[te] = np.where(cloudy[te], shift, 0.0)
    f2 = [a["f"] + pred[i] for i, a in enumerate(allr)]
    mae = np.mean([abs(a["y"] - x) for a, x in zip(allr, f2)])
    hit = [bucket(a["edges"], x) == bucket(a["edges"], a["y"]) for a, x in zip(allr, f2)]
    dm = np.array([abs(a["y"] - x) - abs(a["y"] - a["f"]) for a, x in zip(allr, f2)])
    # MAE difference interval by day blocks
    bs = []
    for _ in range(1000):
        idx = np.array([i for dd in rng.choice(days, size=len(days), replace=True) for i in by_day[dd]])
        bs.append(dm[idx].mean())
    print(f"    {label:20s}: MAE {mae:.4f} vs F {base_mae:.4f} (diff {dm.mean():+.4f} [{np.percentile(bs, 2.5):+.4f}, {np.percentile(bs, 97.5):+.4f}]);"
          f" hits {statistics.mean(hit):.1%} vs F {statistics.mean(base_hit):.1%} {paired(hit, base_hit)}; mean shift applied on cloudy days {pred[cloudy].mean():+.3f}")

# (d) the round 49 regression's cloud coefficient
X = np.array([[a["p"][k] for k in KEYS] for a in allr])
mu, sd = X.mean(0), X.std(0)
A = np.c_[(X - mu) / sd, np.ones(len(X))]
P = 5.0 * np.eye(A.shape[1])
P[-1, -1] = 0
b = np.linalg.solve(A.T @ A + P, A.T @ err)
print("\n(d) round 49's nine-feature fit on all rows, per +1 sd (C): " + ", ".join(f"{k} {v:+.3f}" for k, v in zip(KEYS, b[:-1])) + f"; intercept {b[-1]:+.3f}")
print(f"    corr(cloud, cloud_anom) {np.corrcoef(X[:, 0], X[:, 3])[0, 1]:+.2f}, corr(cloud, sw) {np.corrcoef(X[:, 0], X[:, 1])[0, 1]:+.2f}")
