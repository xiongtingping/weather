"""Round 45e: Polymarket's own ladder as a forecast.

Each ladder is laid around some value when the market is created (two days
ahead); everyone sees it at listing.  L = the centre of the interior buckets
(between the lower tail's upper edge and the upper tail's lower edge).
(1) Every first-rows station-day with a truth (09-24..10-13): L against F at
listing + 5 min on the truth (MAE, bias, bucket hits), and how far L sits
from F.  (2) The 227 pre-formation first YES: the pick relative to L and to
F; single-bucket rules mixing them (bucket of w F + (1-w) L for w = 0..1, and
the ladder's centre bucket itself); does knowing L - F move the cluster
(regression of the money centre - F on L - F, day-block bootstrap)?
"""
import statistics
import sys
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("cluster_money_target_r45.py").read_text()
ns = {"__name__": "r45", "__file__": str(here.with_name("cluster_money_target_r45.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split('print(f"{len(rows)} of {len(events)}')[0], "r45_head", "exec"), ns)
rows, bucket, prod_f, FR_ALL, edges_of = ns["rows"], ns["bucket"], ns["prod_f"], ns["FR_ALL"], ns["ns"]["edges_of"]


def ladder_centre(edges):
    return (edges[1][0] + edges[-2][1]) / 2.0


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


# (1) every station-day
allrows = []
for r in FR_ALL:
    if r.get("truth_c") is None or len(r["labels"]) < 5:
        continue
    e = prod_f(int(r["listed"]) + 300)["station_days"].get(f"{r['st']}|{r['ed']}")
    if not e or e.get("state") != "READY":
        continue
    edges = edges_of(r)
    L = ladder_centre(edges)
    y = float(r["truth_c"])
    allrows.append({"L": L, "f": e["f_c"], "fb_": e["f_base_c"], "y": y, "edges": edges, "yb": bucket(edges, y),
                    "Lb": bucket(edges, L), "fb": bucket(edges, e["f_c"]), "ed": r["ed"]})
n = len(allrows)
print(f"(1) {n} station-days with a truth and F at listing + 5 min")
for name, k in (("ladder centre L", "L"), ("F", "f"), ("F_base", "fb_")):
    err = np.array([a["y"] - a[k] for a in allrows])
    hit = statistics.mean(bucket(a["edges"], a[k]) == a["yb"] for a in allrows)
    print(f"    {name:16s} MAE {np.abs(err).mean():.3f} C, bias (truth - x) {err.mean():+.2f}, sd {err.std():.2f}; bucket hits {hit:.1%}")
d = np.array([a["L"] - a["f"] for a in allrows])
print(f"    L - F: mean {d.mean():+.2f} C, sd {d.std():.2f}; L's bucket = F's {statistics.mean(a['Lb'] == a['fb'] for a in allrows):.1%}")
# Does L add to F on the truth?  truth - F on L - F, leave one event day out.
X = np.array([[a["L"] - a["f"], 1.0] for a in allrows])
Y = np.array([a["y"] - a["f"] for a in allrows])
days = sorted({a["ed"] for a in allrows})
pred = np.zeros(n)
for day in days:
    tr = [i for i, a in enumerate(allrows) if a["ed"] != day]
    b = np.linalg.lstsq(X[tr], Y[tr], rcond=None)[0]
    for i, a in enumerate(allrows):
        if a["ed"] == day:
            pred[i] = X[i] @ b
b_all = np.linalg.lstsq(X, Y, rcond=None)[0]
mix_hit = [bucket(a["edges"], a["f"] + pred[i]) == a["yb"] for i, a in enumerate(allrows)]
f_hit = [a["fb"] == a["yb"] for a in allrows]
print(f"    truth - F = {b_all[0]:+.3f} (L - F) {b_all[1]:+.3f}; leave one day out: MAE {np.abs(Y - pred).mean():.3f} vs F {np.abs(Y).mean():.3f},"
      f" hits {statistics.mean(mix_hit):.1%} vs F {statistics.mean(f_hit):.1%} {paired(mix_hit, f_hit)}")

# (2) the cluster's picks
print(f"\n(2) the {len(rows)} pre-formation first YES")
off_L = [r["ev"]["choice"] - bucket(r["ev"]["edges"], ladder_centre(r["ev"]["edges"])) for r in rows]
print(f"    pick - L's bucket: = {sum(o == 0 for o in off_L)}, +1 {sum(o == 1 for o in off_L)}, -1 {sum(o == -1 for o in off_L)}, else {sum(abs(o) > 1 for o in off_L)}")
base = [r["ev"]["fb"] == r["ev"]["choice"] for r in rows]
early = [r["ev"]["ts"] - r["ev"]["listed"] < 900 for r in rows]
for w in (0.0, 0.25, 0.5, 0.75, 0.9, 1.0):
    same = [bucket(r["ev"]["edges"], w * r["f"] + (1 - w) * ladder_centre(r["ev"]["edges"])) == r["ev"]["choice"] for r in rows]
    e_same = [s for s, e in zip(same, early) if e]
    e_base = [s for s, e in zip(base, early) if e]
    print(f"    bucket({w:.2f} F + {1 - w:.2f} L): = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)};"
          f" early {statistics.mean(e_same):.1%} vs {statistics.mean(e_base):.1%}")
sub = [r for r in rows if "1 h" in r["win"]]
x = np.array([ladder_centre(r["ev"]["edges"]) - r["f"] for r in sub])
y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r in sub])
A = np.c_[x, np.ones(len(x))]
beta = np.linalg.lstsq(A, y, rcond=None)[0]
pdays = sorted({r["ev"]["ed"] for r in sub})
rng = np.random.default_rng(5)
bs = []
for _ in range(400):
    pick = rng.choice(pdays, size=len(pdays), replace=True)
    idx = [i for dd in pick for i, r in enumerate(sub) if r["ev"]["ed"] == dd]
    bs.append(np.linalg.lstsq(A[idx], y[idx], rcond=None)[0][0])
lo_, hi_ = np.percentile(bs, [2.5, 97.5])
print(f"    money centre - F = {beta[0]:+.3f} [{lo_:+.3f}, {hi_:+.3f}] x (L - F) {beta[1]:+.2f}  (1 = the cluster sits on L, 0 = ignores it)")
