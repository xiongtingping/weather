"""Round 42 (熊先生 10-12 「继续找，从模型校准方面呢，比如logloss」).

Does a calibrated predictive distribution -- trained on the truth by
likelihood (LogLoss) -- pick the cluster's bucket more often than F?  The
cluster's first YES hits the truth more often than F does (45.8% vs 40.2%),
so a predictor trained to hit the truth may move toward it.

Training rows: every roster station x settled day 08-14..10-10, the native
members as knowable at that day's listing instant (D-2 05:00Z), the truth.
Strictly forward: a cluster pick on event day D is predicted by models fitted
on rows whose day ended by D-4.  Models (each gives a full bucket
distribution on the pick's own ladder; the single pick is its mode):
  F            production F's bucket (lambda 0.5)
  N-F          normal around F + a, sigma = exp(s0 + s1 log spread) -- LogLoss
  N-W          normal, centre = a + weighted mean of the bias-corrected members
               (weights free, by LogLoss), heteroscedastic sigma
  L1-W         the same centre fitted by absolute error (a median-type centre)
  E-W          N-W's centre with the empirical residual shape (kernel over the
               training residuals) -- a mode that can sit off the mean when
               errors are skewed
Scored on the cluster's pre-formation first YES (all, and within 15 minutes
of listing) and on the truth over every evaluation station-day.
"""
import collections
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import erf

here = Path(__file__)
src = here.with_name("cluster_pick_model_r40.py").read_text()
ns = {"__name__": "r40", "__file__": str(here.with_name("cluster_pick_model_r40.py"))}
exec(compile(src.split("all_events = len(events)")[0], "r40_head", "exec"), ns)
events, bucket = ns["events"], ns["bucket"]
r39 = ns["ns"]
import bayes_basket_target.listing_forecast_f as mod  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS  # noqa: E402

conn, tz, ALL_TRUTHS, ROSTER = r39["conn"], r39["tz"], r39["ALL_TRUTHS"], r39["ROSTER"]
SYSTEMS = sorted(F_SYSTEM_IDS)

# 1) history rows ------------------------------------------------------------
start, end = date(2026, 8, 10), date(2026, 10, 12)
H = {}
for st, _lat, _lon, zone in ROSTER:
    d = start
    while d <= end:
        pt = mod._history_point(conn, station_id=st, event_date=d, zone=zone)
        if pt and len(pt) >= 3:
            H[(st, d)] = pt
        d += timedelta(days=1)
truth = {(st, date.fromisoformat(d)): v for (st, d), v in ALL_TRUTHS.items()}
print(f"history rows with >=3 members: {len(H)}; with truth: {sum(1 for k in H if k in truth)}")


def features(st, d, members, cut):
    """Bias-corrected members (bias over the station's days in [d-60, cut]) and F."""
    out = {}
    for s, x in members.items():
        errs = [truth[(st, dd)] - H[(st, dd)][s] for dd in (d - timedelta(days=k) for k in range(4, 61))
                if dd <= cut and (st, dd) in H and (st, dd) in truth and s in H[(st, dd)]]
        out[s] = x + (statistics.mean(errs) if len(errs) >= 3 else 0.0)
    return out


_FLEET = {}


def fleet_c(d):
    if d in _FLEET:
        return _FLEET[d]
    errs = [truth[(st, dd)] - statistics.median(H[(st, dd)].values())
            for (st, dd) in H if d - timedelta(days=30) <= dd <= d - timedelta(days=4) and (st, dd) in truth]
    _FLEET[d] = statistics.mean(errs) if len(errs) >= 200 else 0.8
    return _FLEET[d]


def row(st, d, members, cut):
    corr = features(st, d, members, cut)
    raw_med = statistics.median(members.values())
    c = fleet_c(d)
    f = raw_med + c + 0.5 * (statistics.median(corr.values()) - raw_med)
    vals = np.array([corr[s] for s in SYSTEMS if s in corr])
    mask = np.array([s in corr for s in SYSTEMS], dtype=float)
    z = np.array([corr.get(s, 0.0) for s in SYSTEMS])
    spread = float(np.std(vals)) if len(vals) > 1 else 1.0
    return {"f": f, "z": z, "mask": mask, "spread": spread, "c": c}


def cdf(x):
    return 0.5 * (1 + erf(np.asarray(x) / math.sqrt(2)))


def centre(p, r, kind):
    if kind == "F":
        return r["f"] + p[0]
    w = np.exp(p[:len(SYSTEMS)]) * r["mask"]
    return p[len(SYSTEMS)] + float(w @ r["z"]) / max(1e-9, float(w.sum())) + 0.0


def sigma(p, r):
    return math.exp(p[-2] + p[-1] * math.log(r["spread"] + 0.1))


def arrays(rows, ys):
    return {"f": np.array([r["f"] for r in rows]), "z": np.array([r["z"] for r in rows]),
            "mask": np.array([r["mask"] for r in rows]), "ls": np.log(np.array([r["spread"] for r in rows]) + 0.1),
            "y": np.array(ys)}


def centres(p, A, kind):
    if kind == "F":
        return A["f"] + p[0]
    w = np.exp(p[:len(SYSTEMS)])[None, :] * A["mask"]
    return p[len(SYSTEMS)] + (w * A["z"]).sum(1) / np.maximum(1e-9, w.sum(1))


def nll(p, A, kind):
    mu = centres(p, A, kind)
    s = np.exp(p[-2] + p[-1] * A["ls"])
    return float(np.mean(0.5 * ((A["y"] - mu) / s) ** 2 + np.log(s))) + 1e-3 * float(p @ p)


def l1(p, A):
    return float(np.mean(np.abs(A["y"] - centres(p, A, "W")))) + 1e-3 * float(p[:len(SYSTEMS)] @ p[:len(SYSTEMS)])


fits = {}


def fitted(D):
    """Models fitted on rows whose day ended by D-4."""
    if D in fits:
        return fits[D]
    cut = D - timedelta(days=4)
    rows, ys = [], []
    for (st, d), members in H.items():
        if d <= cut and (st, d) in truth and d >= start + timedelta(days=4):
            rows.append(row(st, d, members, d - timedelta(days=4)))
            ys.append(truth[(st, d)])
    A = arrays(rows, ys)
    pF = minimize(nll, np.array([0.0, 0.0, 0.0]), args=(A, "F"), method="L-BFGS-B").x
    p0 = np.r_[np.zeros(len(SYSTEMS)), 0.0, 0.0, 0.0]
    pW = minimize(nll, p0, args=(A, "W"), method="L-BFGS-B").x
    pL = minimize(l1, p0, args=(A,), method="Powell", options={"maxiter": 4000}).x
    resid = np.array([(y - centre(pW, r, "W")) / sigma(pW, r) for r, y in zip(rows, ys)])
    fits[D] = {"F": pF, "W": pW, "L1": pL, "resid": resid, "n": len(rows)}
    return fits[D]


def bucket_probs(edges, mu, s, resid=None):
    if resid is None:
        return [float(cdf((hi - mu) / s) - cdf((lo - mu) / s)) for lo, hi in edges]
    h = 1.06 * resid.std() * len(resid) ** -0.2
    pts = mu + s * resid
    return [float(np.mean(cdf((hi - pts) / (s * h)) - cdf((lo - pts) / (s * h)))) for lo, hi in edges]


def picks_for(st, d, members, edges):
    D = d
    m = fitted(D)
    r = row(st, d, members, d - timedelta(days=4))
    out = {"F": bucket(edges, r["f"])}
    pF, pW, pL = m["F"], m["W"], m["L1"]
    out["N-F"] = int(np.argmax(bucket_probs(edges, centre(pF, r, "F"), sigma(pF, r))))
    out["N-W"] = int(np.argmax(bucket_probs(edges, centre(pW, r, "W"), sigma(pW, r))))
    out["L1-W"] = bucket(edges, centre(pL, r, "W"))
    out["E-W"] = int(np.argmax(bucket_probs(edges, centre(pW, r, "W"), sigma(pW, r), m["resid"])))
    return out


# 2) the cluster's picks -------------------------------------------------------
MODELS = ["F", "N-F", "N-W", "L1-W", "E-W"]
agree = {k: collections.Counter() for k in MODELS}
for ev in events:
    d = date.fromisoformat(ev["ed"])
    # members as of the trade (production F's own read), not the listing instant
    e = ns["prod_f"](ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]
    members = {s: v["tmax_c"] for s, v in e["systems"].items()}
    pk = picks_for(ev["st"], d, members, ev["edges"])
    early = ev["ts"] - ev["listed"] < 900
    for k in MODELS:
        agree[k]["n"] += 1
        agree[k]["same"] += pk[k] == ev["choice"]
        agree[k]["truth"] += ev["truth"] is not None and pk[k] == ev["truth"]
        if early:
            agree[k]["n_e"] += 1
            agree[k]["same_e"] += pk[k] == ev["choice"]
        agree[k]["w"] += pk[k] == ev["choice"] and pk["F"] != ev["choice"]
        agree[k]["l"] += pk["F"] == ev["choice"] and pk[k] != ev["choice"]
print(f"\ncluster pre-formation first YES ({len(events)}; early {agree['F']['n_e']}):")
for k in MODELS:
    a = agree[k]
    n = max(1, a["n"])
    d_ = (a["w"] - a["l"]) / n
    h = 1.96 * (a["w"] + a["l"]) ** 0.5 / n
    print(f"   {k:5s} = cluster {a['same'] / n:.1%} (vs F {d_:+.3f} [{d_ - h:+.3f}, {d_ + h:+.3f}])"
          f"   early {a['same_e'] / max(1, a['n_e']):.1%}   hits the truth on these days {a['truth'] / n:.1%}")
print("\nlatest fit:", {k: (np.round(np.exp(v[:len(SYSTEMS)]), 2).tolist() if k in ('W', 'L1') else np.round(v, 2).tolist())
                        for k, v in fits[max(fits)].items() if k in ("F", "W", "L1")}, "systems", [s.split("_")[1] for s in SYSTEMS])
