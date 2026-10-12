"""Round 45f: every lean found so far, in one model of the cluster's money centre.

Target: the cluster's money-weighted centre in the hour after its first YES
(round 45a: that is where 93% of its money is) minus F (recalibrated).  Feature
sets, each ridge-fitted and scored leave-one-event-day-out:
  S1 round 45c (members' deviations, spread, skew, station correction, the
     recalibration, |lat|, F unit, position);
  S2 S1 + the change from the newest settled observation to F, ladder centre
     L - F, log minutes since listing, P0 mean - F (0 when no P0) and a no-P0
     flag;
  S3 the four leans with intervals clear of zero in 45c/45e only (skew,
     station correction, recalibration, L - F) + F unit.
Scored as bucket(F + predicted offset) against the first pick and the main
bucket, all 227 picks and the 96 early (< 15 min) picks, by halves, with the
residual sd left -- how far the conditions known at the trade get, and what
is left over (90% single-bucket agreement needs ~0.13 bucket, round 40d).
"""
import math
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
rows, bucket, prod_f, XN = ns["rows"], ns["bucket"], ns["prod_f"], ns["XN"]


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


sub = [r for r in rows if "1 h" in r["win"]]
extra, names_extra = [], ["obs->F change", "L - F", "log minutes", "P0 mean - F", "no P0"]
for r in sub:
    ev = r["ev"]
    e = prod_f(ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]
    edges = ev["edges"]
    L = (edges[1][0] + edges[-2][1]) / 2.0
    if ev["p0"]:
        p = np.array([ev["p0"].get(j, 0.0) for j in range(ev["n"])])
        p0m = float(p @ np.array([(a + b) / 2 for a, b in edges]) / p.sum()) - r["f"]
        nop0 = 0.0
    else:
        p0m, nop0 = 0.0, 1.0
    extra.append([e["recal_features"]["obs_to_f_change_c"], L - r["f"], math.log1p(max(0.0, (ev["ts"] - ev["listed"]) / 60)), p0m, nop0])
X1 = np.array([r["x"] for r in sub])
X2 = np.c_[X1, np.array(extra)]
NAMES2 = XN + names_extra
pick3 = [NAMES2.index(k) for k in ("skew", "station corr", "recal", "L - F", "F unit")]
SETS = {"S1 (45c)": (X1, XN), "S2 (all)": (X2, NAMES2), "S3 (clear leans)": (X2[:, pick3], [NAMES2[i] for i in pick3])}
y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r in sub])
days = sorted({r["ev"]["ed"] for r in sub})
mid = days[len(days) // 2]
early = np.array([r["ev"]["ts"] - r["ev"]["listed"] < 900 for r in sub])
widths = np.array([r["ev"]["edges"][r["ev"]["fb"]][1] - r["ev"]["edges"][r["ev"]["fb"]][0] for r in sub])
print(f"{len(sub)} picks ({early.sum()} early); centre - F: mean {y.mean():+.2f} C, sd {y.std():.2f} C = {np.mean(np.abs(y) / widths):.2f} bucket MAE")


def ridge(A, t, lam):
    P = lam * np.eye(A.shape[1])
    P[-1, -1] = 0.0
    return np.linalg.solve(A.T @ A + P, A.T @ t)


for sname, (X, names) in SETS.items():
    for lam in (1.0, 5.0, 20.0):
        oos = np.zeros(len(sub))
        for d in days:
            tr = np.array([r["ev"]["ed"] != d for r in sub])
            mu, sd = X[tr].mean(0), X[tr].std(0)
            sd = np.where(sd > 1e-9, sd, 1.0)
            A = np.c_[(X - mu) / sd, np.ones(len(X))]
            b = ridge(A[tr], y[tr], lam)
            oos[~tr] = A[~tr] @ b
        res = y - oos
        line = f"  {sname:17s} ridge {lam:4.0f}: centre MAE {np.abs(res).mean():.3f} vs F {np.abs(y).mean():.3f} C, residual sd {res.std():.2f}"
        for target in ("first", "main"):
            tgt = [r["ev"]["choice"] if target == "first" else r["win"]["1 h"]["main"] for r in sub]
            got = [bucket(r["ev"]["edges"], r["f"] + oos[i]) == tgt[i] for i, r in enumerate(sub)]
            base = [r["ev"]["fb"] == tgt[i] for i, r in enumerate(sub)]
            ge = [g for g, e in zip(got, early) if e]
            be = [g for g, e in zip(base, early) if e]
            h1 = [g for g, r in zip(got, sub) if r["ev"]["ed"] < mid]
            h2 = [g for g, r in zip(got, sub) if r["ev"]["ed"] >= mid]
            line += (f"\n      = {target:5s} {statistics.mean(got):.1%} vs F {statistics.mean(base):.1%} {paired(got, base)}"
                     f" (halves {statistics.mean(h1):.1%}/{statistics.mean(h2):.1%}); early {statistics.mean(ge):.1%} vs F {statistics.mean(be):.1%} {paired(ge, be)}")
        print(line)
# Coefficients of S2 on all rows (ridge 5), for the record.
mu, sd = X2.mean(0), X2.std(0)
sd = np.where(sd > 1e-9, sd, 1.0)
A = np.c_[(X2 - mu) / sd, np.ones(len(X2))]
b = ridge(A, y, 5.0)
print("  S2 coefficients per +1 sd (C): " + ", ".join(f"{n} {v:+.2f}" for n, v in zip(NAMES2, b[:-1]) if abs(v) >= 0.03) + f"; intercept {b[-1]:+.2f}")

# Frozen S3 (ridge 20) on all picks: the forward check reads only picks on event days after these.
import json  # noqa: E402

X3 = X2[:, pick3]
mu3, sd3 = X3.mean(0), X3.std(0)
sd3 = np.where(sd3 > 1e-9, sd3, 1.0)
b3 = ridge(np.c_[(X3 - mu3) / sd3, np.ones(len(X3))], y, 20.0)
frozen = {
    "model": "cluster_money_centre_offset_from_F_s3_v1",
    "features": [NAMES2[i] for i in pick3],
    "feature_definitions": {
        "skew": "mean - median of the corrected members (tmax_c + bias_c) in F's entry",
        "station corr": "entry station_corr_c", "recal": "entry recal_c",
        "L - F": "centre of the ladder's interior buckets (C) - f_c", "F unit": "1 for F-unit markets",
    },
    "mean": [float(v) for v in mu3], "sd": [float(v) for v in sd3],
    "beta_per_sd": [float(v) for v in b3[:-1]], "intercept_c": float(b3[-1]),
    "fitted_on": {"picks": len(sub), "event_days": [days[0], days[-1]], "fills_to_unix": int(ns["NEWEST"])},
    "forward_protocol": "pre-formation first YES on event days after fitted_on.event_days[1]: bucket(f_c + offset) vs the first pick and the 1 h main bucket, paired against bucket(f_c); read when >= 80 picks",
}
json.dump(frozen, open(here.parent.parent / "cluster_centre_s3_frozen_r45f.json", "w"), indent=1)
print(f"  frozen S3 -> cluster_centre_s3_frozen_r45f.json: beta {np.round(b3[:-1], 3).tolist()}, intercept {b3[-1]:+.3f}")
