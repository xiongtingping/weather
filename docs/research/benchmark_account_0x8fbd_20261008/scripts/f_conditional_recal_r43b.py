"""Round 43b: F's conditional calibration against the truth.  Correction
F' = F + sum_j beta_j z_j over the conditions round 43 found biased (station
correction, members' skew and spread, fleet shift, the change from the latest
observed maximum, |latitude|, members' deviations), fitted strictly forward
(rows whose day ended by D-4) by least squares and by absolute error; scored
on every later roster station-day (hits on the market ladder, MAE, bias) and
on the cluster's pre-formation first YES (agreement, hits).  A robust subset
(station correction + skew + observed change) alongside the full set.
"""
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

here = Path(__file__)
src = here.with_name("cluster_implied_centre_r43.py").read_text()
ns = {"__name__": "r43", "__file__": str(here.with_name("cluster_implied_centre_r43.py"))}
exec(compile(src.split("# (2) the truth over every station-day")[0].split("days = sorted({d_[\"day\"]")[0], "r43_head", "exec"), ns)
data, T_src = ns["data"], None
H, truth, row, cond, NAMES, SYSTEMS, bucket = (ns[k] for k in ("H", "truth", "row", "cond", "NAMES", "SYSTEMS", "bucket"))
_n = ns
while "conn" not in _n:
    _n = _n["ns"]
ns["conn"] = _n["conn"]
import json  # noqa: E402
ns["json"] = json
src42b = here.with_name("market_calibrated_pick_r42b.py").read_text()
exec(compile(src42b[src42b.index("def ladder_of"):src42b.index("targets = {}")], "ladder", "exec"), ns)
ladder_of = ns["ladder_of"]

T = []
for (st, d), members in H.items():
    if (st, d) not in truth or d < date(2026, 8, 20):
        continue
    r = row(st, d, members, d - timedelta(days=4))
    systems = {s: {"tmax_c": members[s], "bias_c": float(r["z"][SYSTEMS.index(s)] - members[s])} for s in members}
    raw_med = statistics.median(members.values())
    corr = statistics.median(v["tmax_c"] + v["bias_c"] for v in systems.values()) - raw_med
    x = cond(st, d, systems, r["f"], corr, r["c"], 5.0, 0.0, False)
    lad = ladder_of(st, d)
    T.append({"st": st, "d": d, "x": x, "f": r["f"], "y": truth[(st, d)], "edges": None if lad is None else lad[1]})
FULL = [i for i, n in enumerate(NAMES) if n not in ("F station", "log minutes", "position")]
ROBUST = [NAMES.index(n) for n in ("station corr", "skew", "obs->F change")]


def fit(rows, cols, kind):
    X = np.array([r["x"][cols] for r in rows])
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    y = np.array([r["y"] - r["f"] for r in rows])
    if kind == "ls":
        A = np.c_[Z, np.ones(len(Z))]
        b = np.linalg.solve(A.T @ A + 1.0 * np.eye(A.shape[1]), A.T @ y)
    else:
        obj = lambda b: float(np.mean(np.abs(y - (Z @ b[:-1] + b[-1])))) + 1e-3 * float(b[:-1] @ b[:-1])  # noqa: E731
        b = minimize(obj, np.zeros(Z.shape[1] + 1), method="Powell", options={"maxiter": 6000}).x
    return mu, sd, b


def corrected(f, x, model, cols):
    mu, sd, b = model
    return f + float(((x[cols] - mu) / sd) @ b[:-1] + b[-1])


EVAL_FROM = date(2026, 9, 20)
models = {}
for kind in ("ls", "l1"):
    for cols, cname in ((FULL, "full"), (ROBUST, "robust")):
        stat = {"hit": 0, "fhit": 0, "n": 0, "ae": 0.0, "fae": 0.0, "bias": 0.0, "fbias": 0.0, "w": 0, "l": 0, "dae": []}
        cache = {}
        for r in T:
            if r["d"] < EVAL_FROM or r["edges"] is None:
                continue
            D = r["d"]
            if D not in cache:
                cache[D] = fit([t for t in T if t["d"] <= D - timedelta(days=4)], cols, kind)
            fp = corrected(r["f"], r["x"], cache[D], cols)
            yb = bucket(r["edges"], r["y"])
            stat["n"] += 1
            a_, b_ = bucket(r["edges"], fp) == yb, bucket(r["edges"], r["f"]) == yb
            stat["hit"] += a_
            stat["fhit"] += b_
            stat["w"] += a_ and not b_
            stat["l"] += b_ and not a_
            stat["dae"].append(abs(r["y"] - fp) - abs(r["y"] - r["f"]))
            stat["ae"] += abs(r["y"] - fp)
            stat["fae"] += abs(r["y"] - r["f"])
            stat["bias"] += r["y"] - fp
            stat["fbias"] += r["y"] - r["f"]
        n = max(1, stat["n"])
        dh = (stat["w"] - stat["l"]) / n
        hh = 1.96 * (stat["w"] + stat["l"]) ** 0.5 / n
        dae = np.array(stat["dae"])
        hae = 1.96 * dae.std() / len(dae) ** 0.5
        paired = f"  [hits {dh:+.3f} ({dh - hh:+.3f}, {dh + hh:+.3f}); MAE {dae.mean():+.3f} ({dae.mean() - hae:+.3f}, {dae.mean() + hae:+.3f})]"
        # the cluster's picks
        cl = {"n": 0, "same": 0, "fsame": 0, "hit": 0, "fhit": 0}
        for d_ in data:
            ev = d_["ev"]
            D = date.fromisoformat(ev["ed"])
            if D not in cache:
                cache[D] = fit([t for t in T if t["d"] <= D - timedelta(days=4)], cols, kind)
            fp = corrected(ev["f"], d_["x"], cache[D], cols)
            b = bucket(ev["edges"], fp)
            cl["n"] += 1
            cl["same"] += b == ev["choice"]
            cl["fsame"] += ev["fb"] == ev["choice"]
            cl["hit"] += ev["truth"] is not None and b == ev["truth"]
            cl["fhit"] += ev["truth"] is not None and ev["fb"] == ev["truth"]
        print(f"{kind.upper()} {cname:6s}  every station-day from {EVAL_FROM} (n {stat['n']}): hits {stat['hit'] / n:.1%} vs F {stat['fhit'] / n:.1%};"
              f" MAE {stat['ae'] / n:.2f} vs {stat['fae'] / n:.2f} C; bias {stat['bias'] / n:+.2f} vs {stat['fbias'] / n:+.2f} C"
              f"   | cluster picks (n {cl['n']}): = cluster {cl['same'] / cl['n']:.1%} vs F {cl['fsame'] / cl['n']:.1%},"
              f" hits {cl['hit'] / cl['n']:.1%} vs F {cl['fhit'] / cl['n']:.1%}" + paired)
