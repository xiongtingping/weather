"""Round 49 (熊先生 10-12 「预报带的云雨、辐射等物理信息呢」).

``physics_r49.json`` (``fetch_physics_r49.py``): ECMWF IFS / GFS forecast
daytime cloud %, daytime shortwave W/m2 and day precipitation mm per station
x local day, at ~72 h (day3, strictly before the listing) and ~48 h (day2).
Features per pick (consensus = mean of the two models): cloud, shortwave and
log(1 + precip) for the event day, their anomaly against the station's own
seven preceding days of the same lead, and the change from the day before.
(1) Groups -- sunny / mixed / cloudy, dry / wet: the cluster's money centre -
F next to truth - F, and where the first pick falls.  (2) Direction on the
one-off picks against the base rate.  (3) The money centre - F on the
physics features (day-block bootstrap), leave one event day out; also added
to round 45's S3 leans.  (4) Physics on the truth: truth - F over every
first-rows station-day at listing + 5 min.
"""
import json
import math
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("cluster_money_target_r45.py").read_text()
ns = {"__name__": "r45", "__file__": str(here.with_name("cluster_money_target_r45.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split('print(f"{len(rows)} of {len(events)}')[0], "r45_head", "exec"), ns)
rows, bucket, prod_f, FR_ALL, XN = ns["rows"], ns["bucket"], ns["prod_f"], ns["FR_ALL"], ns["XN"]
edges_of = ns["ns"]["edges_of"]
PH = json.load(open(B / "physics_r49.json"))


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


def day_value(st, d, lead, key):
    vals = []
    for m in ("ecmwf_ifs025", "gfs_seamless"):
        v = PH.get(st, {}).get(f"{m}|{lead}", {}).get(d.isoformat(), {}).get(key)
        if v is not None:
            vals.append(math.log1p(v) if key == "precip" else v)
    return statistics.mean(vals) if vals else None


def phys(st, D, lead):
    out = {}
    for key in ("cloud", "sw", "precip"):
        v = day_value(st, D, lead, key)
        prev = [day_value(st, D - timedelta(days=k), lead, key) for k in range(1, 8)]
        prev = [p for p in prev if p is not None]
        if v is None or len(prev) < 4:
            return None
        out[key] = v
        out[f"{key}_anom"] = v - statistics.mean(prev)
        y1 = day_value(st, D - timedelta(days=1), lead, key)
        out[f"{key}_chg"] = 0.0 if y1 is None else v - y1
    return out


KEYS = ["cloud", "sw", "precip", "cloud_anom", "sw_anom", "precip_anom", "cloud_chg", "sw_chg", "precip_chg"]
for lead in ("previous_day3", "previous_day2"):
    print(f"\n======== lead {lead}")
    sub = []
    for r in rows:
        p = phys(r["ev"]["st"], date.fromisoformat(r["ev"]["ed"]), lead)
        if p and "1 h" in r["win"]:
            r["ph"] = p
            sub.append(r)
    days = sorted({r["ev"]["ed"] for r in sub})
    mid = days[len(days) // 2]
    print(f"  {len(sub)} picks with physics")
    # (1) groups
    for label, cond in (("sunny (cloud < 30%)", lambda p: p["cloud"] < 30), ("mixed", lambda p: 30 <= p["cloud"] <= 70),
                        ("cloudy (> 70%)", lambda p: p["cloud"] > 70), ("dry (< 0.5 mm)", lambda p: p["precip"] < math.log1p(0.5)),
                        ("wet (>= 2 mm)", lambda p: p["precip"] >= math.log1p(2.0)),
                        ("sunnier than the week (sw anom > +40)", lambda p: p["sw_anom"] > 40),
                        ("duller than the week (sw anom < -40)", lambda p: p["sw_anom"] < -40)):
        g = [r for r in sub if cond(r["ph"])]
        if not g:
            continue
        cen = [r["win"]["1 h"]["centre"] - r["f"] for r in g]
        tr = [r for r in g if r["ev"]["truth"] is not None]
        tru = [ns["ns"]["ns"]["ALL_TRUTHS"].get((r["ev"]["st"], r["ev"]["ed"])) for r in g]
        tru = [t - r["f"] for t, r in zip(tru, g) if t is not None]
        up = sum(r["ev"]["choice"] > r["ev"]["fb"] for r in g)
        dn = sum(r["ev"]["choice"] < r["ev"]["fb"] for r in g)
        print(f"    {label:40s} n {len(g):3d}: centre - F {statistics.mean(cen):+.2f} C, truth - F {statistics.mean(tru) if tru else float('nan'):+.2f} C;"
              f" pick above F {up}, = F {len(g) - up - dn}, below {dn}")
    # (2) direction
    one = [r for r in sub if abs(r["ev"]["choice"] - r["ev"]["fb"]) == 1]
    for name, fn in (("shortwave anomaly", lambda r: r["ph"]["sw_anom"]), ("- cloud anomaly", lambda r: -r["ph"]["cloud_anom"]),
                     ("- precip anomaly", lambda r: -r["ph"]["precip_anom"]), ("shortwave change", lambda r: r["ph"]["sw_chg"]),
                     ("- cloud change", lambda r: -r["ph"]["cloud_chg"])):
        told = [(fn(r), r) for r in one if fn(r) != 0]
        right = sum((v > 0) == (r["ev"]["choice"] > r["ev"]["fb"]) for v, r in told)
        ups = sum(r["ev"]["choice"] > r["ev"]["fb"] for _v, r in told)
        print(f"    direction by {name:18s} on {len(told):3d} one-off picks: {right / len(told):.1%} (base {max(ups, len(told) - ups) / len(told):.1%})")
    # (3) money centre regression, physics alone and with S3
    y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r in sub])
    s3 = [XN.index(k) for k in ("skew", "station corr", "recal", "F unit")]
    for label, X in (("physics", np.array([[r["ph"][k] for k in KEYS] for r in sub])),
                     ("S3 leans (no L)", np.array([r["x"][s3] for r in sub])),
                     ("S3 leans + physics", np.c_[np.array([r["x"][s3] for r in sub]), np.array([[r["ph"][k] for k in KEYS] for r in sub])])):
        mu, sd = X.mean(0), X.std(0)
        sd = np.where(sd > 1e-9, sd, 1.0)
        A = np.c_[(X - mu) / sd, np.ones(len(X))]
        P = 5.0 * np.eye(A.shape[1])
        P[-1, -1] = 0
        b_all = np.linalg.solve(A.T @ A + P, A.T @ y)
        oos = np.zeros(len(sub))
        for d in days:
            trm = np.array([r["ev"]["ed"] != d for r in sub])
            b = np.linalg.solve(A[trm].T @ A[trm] + P, A[trm].T @ y[trm])
            oos[~trm] = A[~trm] @ b
        got = [bucket(r["ev"]["edges"], r["f"] + oos[i]) == r["ev"]["choice"] for i, r in enumerate(sub)]
        base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
        h1 = [g for g, r in zip(got, sub) if r["ev"]["ed"] < mid]
        h2 = [g for g, r in zip(got, sub) if r["ev"]["ed"] >= mid]
        extra = ""
        if label == "physics":
            rng = np.random.default_rng(9)
            bs = []
            for _ in range(300):
                pick = rng.choice(days, size=len(days), replace=True)
                idx = [i for dd in pick for i, r in enumerate(sub) if r["ev"]["ed"] == dd]
                bs.append(np.linalg.solve(A[idx].T @ A[idx] + P, A[idx].T @ y[idx])[:-1])
            bs = np.array(bs)
            extra = "\n      per +1 sd (C): " + ", ".join(
                f"{k} {b_all[i]:+.2f} [{np.percentile(bs[:, i], 2.5):+.2f}, {np.percentile(bs[:, i], 97.5):+.2f}]{'*' if np.percentile(bs[:, i], 2.5) > 0 or np.percentile(bs[:, i], 97.5) < 0 else ''}"
                for i, k in enumerate(KEYS))
        print(f"    centre model [{label}]: centre MAE {np.abs(y - oos).mean():.3f} vs F {np.abs(y).mean():.3f}; = first pick {statistics.mean(got):.1%}"
              f" vs F {statistics.mean(base):.1%} {paired(got, base)} (halves {statistics.mean(h1):.1%}/{statistics.mean(h2):.1%})" + extra)

# (4) physics on the truth (strict lead only)
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
    allr.append({"f": e["f_c"], "p": p, "y": float(r["truth_c"]), "edges": edges_of(r), "ed": r["ed"]})
X = np.array([[a["p"][k] for k in KEYS] for a in allr])
mu, sd = X.mean(0), X.std(0)
A = np.c_[(X - mu) / sd, np.ones(len(X))]
Y = np.array([a["y"] - a["f"] for a in allr])
P = 5.0 * np.eye(A.shape[1])
P[-1, -1] = 0
pa = np.zeros(len(allr))
for dd in sorted({a["ed"] for a in allr}):
    trm = np.array([a["ed"] != dd for a in allr])
    b = np.linalg.solve(A[trm].T @ A[trm] + P, A[trm].T @ Y[trm])
    pa[~trm] = A[~trm] @ b
hit = [bucket(a["edges"], a["f"] + pa[i]) == bucket(a["edges"], a["y"]) for i, a in enumerate(allr)]
fh = [bucket(a["edges"], a["f"]) == bucket(a["edges"], a["y"]) for a in allr]
print(f"\n(4) physics (day3) on the truth, {len(allr)} station-days: leave one day out MAE {np.abs(Y - pa).mean():.3f} vs F {np.abs(Y).mean():.3f};"
      f" hits {statistics.mean(hit):.1%} vs F {statistics.mean(fh):.1%} {paired(hit, fh)}")

# (5) is F cold on sunny days across the whole sample, or only on the cluster's picks?
print("\n(5) truth - F by forecast cloud (day3), every first-rows station-day vs the cluster's picks")
for label, lo_, hi_ in (("sunny < 30%", -1, 30), ("mixed 30-70%", 30, 70.0001), ("cloudy > 70%", 70.0001, 101)):
    g = [a for a in allr if lo_ <= a["p"]["cloud"] < hi_]
    e = np.array([a["y"] - a["f"] for a in g])
    print(f"    {label:13s} all station-days n {len(g):3d}: truth - F {e.mean():+.2f} C (95% +-{1.96 * e.std() / len(e) ** 0.5:.2f}),"
          f" F hits {statistics.mean(bucket(a['edges'], a['f']) == bucket(a['edges'], a['y']) for a in g):.1%},"
          f" F+0.4 hits {statistics.mean(bucket(a['edges'], a['f'] + 0.4) == bucket(a['edges'], a['y']) for a in g):.1%}")
