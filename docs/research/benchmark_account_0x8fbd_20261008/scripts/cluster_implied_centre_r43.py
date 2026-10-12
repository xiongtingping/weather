"""Round 43 (熊先生 10-12 「用簇选桶倒推，再看f的校准条件性问题」).

(1) Invert the cluster's pick: buying bucket k says its centre sat in
[lo_k, hi_k].  Treat each pre-formation first YES as an interval-censored
observation of the cluster's offset from F, delta = centre - F, and regress it
on conditions knowable at the trade (normal errors, fitted by likelihood):
  members' deviations from the corrected median (AIFS, HRES, GFS, AIGFS),
  members' spread and skew (mean - median), the station correction and fleet
  shift F used, F's own recent error at the station (last settled days), the
  change from the latest observed maximum to F, F-unit station, |latitude|,
  minutes from listing to the trade, F's position in its bucket.
  Leave-one-event-day-out: does bucket(F + predicted delta) match more often?
  Coefficients with day-block bootstrap intervals.
(2) The same conditions on the truth's offset from F (truth - F), on every
  roster station-day as of its listing (2,200 rows): where is F conditionally
  biased against the truth, and does the cluster lean the same way?
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
src = here.with_name("calibrated_pick_r42.py").read_text()
ns = {"__name__": "r42", "__file__": str(here.with_name("calibrated_pick_r42.py"))}
exec(compile(src.split("# 2) the cluster's picks")[0], "r42_head", "exec"), ns)
events, bucket, H, truth, row, SYSTEMS = (ns[k] for k in ("events", "bucket", "H", "truth", "row", "SYSTEMS"))
r40 = ns["ns"]
prod_f, ROSTER = r40["prod_f"], r40["ns"]["ROSTER"]
LAT = {s: abs(lat) for s, lat, _lon, _z in ROSTER}
DEV = ["ECMWF_AIFS_SINGLE_POINT", "ECMWF_IFS_HRES_NATIVE_POINT", "NOAA_GFS_025_NATIVE_POINT", "NOAA_AIGFS_025_NATIVE_POINT"]
NAMES = ["dev AIFS", "dev HRES", "dev GFS", "dev AIGFS", "spread", "skew", "station corr", "fleet c",
         "recent F error", "obs->F change", "F station", "|lat|/30", "log minutes", "position"]


def recent_f_error(st, d, k=5):
    errs = []
    for back in range(4, 4 + 12):
        dd = d - timedelta(days=back)
        if (st, dd) in H and (st, dd) in truth:
            r = row(st, dd, H[(st, dd)], dd - timedelta(days=4))
            errs.append(truth[(st, dd)] - r["f"])
        if len(errs) >= k:
            break
    return statistics.mean(errs) if errs else 0.0


def latest_obs(st, d):
    for back in (3, 4, 5):
        dd = d - timedelta(days=back)
        if (st, dd) in truth:
            return truth[(st, dd)]
    return None


def cond(st, d, systems, f, station_corr, fleet_c, minutes, position, unit_f):
    corrected = {s: v["tmax_c"] + v.get("bias_c", 0.0) for s, v in systems.items()}
    vals = list(corrected.values())
    med = statistics.median(vals)
    obs = latest_obs(st, d)
    x = [corrected[s] - med if s in corrected else 0.0 for s in DEV]
    x += [float(np.std(vals)), statistics.mean(vals) - med, station_corr, fleet_c,
          recent_f_error(st, d), 0.0 if obs is None else f - obs, 1.0 if unit_f else 0.0,
          LAT.get(st, 30.0) / 30.0, math.log1p(max(0.0, minutes)), position]
    return np.array(x)


# (1) the cluster ------------------------------------------------------------------
data = []
for ev in events:
    e = prod_f(ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]
    d = date.fromisoformat(ev["ed"])
    lo, hi = ev["edges"][ev["fb"]]
    pos = (ev["f"] - lo) / (hi - lo) - 0.5
    x = cond(ev["st"], d, e["systems"], ev["f"], e["station_corr_c"], e["fleet_c"], (ev["ts"] - ev["listed"]) / 60,
             pos, ev["unit"] == "F")
    clo, chi = ev["edges"][ev["choice"]]
    # open tails: the lower tail bucket has no lower bound, the upper none above
    clo = -1e6 if ev["choice"] == 0 else clo
    chi = 1e6 if ev["choice"] == ev["n"] - 1 else chi
    data.append({"ev": ev, "x": x, "lo": clo - ev["f"], "hi": chi - ev["f"], "day": ev["ed"]})
X = np.array([d_["x"] for d_ in data])
MU, SD = X.mean(0), X.std(0) + 1e-9


def Phi(z):
    return 0.5 * (1 + erf(z / math.sqrt(2)))


def nll(p, rows, l2=2.0):
    b, a, ls = p[:-2], p[-2], p[-1]
    s = math.exp(ls)
    Z = (np.array([r["x"] for r in rows]) - MU) / SD
    m = a + Z @ b
    lo = np.array([r["lo"] for r in rows])
    hi = np.array([r["hi"] for r in rows])
    pr = np.clip(Phi((hi - m) / s) - Phi((lo - m) / s), 1e-9, 1.0)
    return -float(np.mean(np.log(pr))) + l2 * float(b @ b) / len(rows)


def fit(rows):
    p0 = np.r_[np.zeros(X.shape[1]), 0.0, math.log(0.8)]
    return minimize(nll, p0, args=(rows,), method="L-BFGS-B").x


days = sorted({d_["day"] for d_ in data})
mid = days[len(days) // 2]
pick = {}
for day in days:
    p = fit([d_ for d_ in data if d_["day"] != day])
    for d_ in data:
        if d_["day"] == day:
            m = p[-2] + ((d_["x"] - MU) / SD) @ p[:-2]
            pick[id(d_)] = bucket(d_["ev"]["edges"], d_["ev"]["f"] + m)
for label, sub in (("all", data), ("1st half", [d_ for d_ in data if d_["day"] < mid]),
                   ("2nd half", [d_ for d_ in data if d_["day"] >= mid]),
                   ("early <15 min", [d_ for d_ in data if d_["ev"]["ts"] - d_["ev"]["listed"] < 900])):
    n = max(1, len(sub))
    got = sum(pick[id(d_)] == d_["ev"]["choice"] for d_ in sub)
    base = sum(d_["ev"]["fb"] == d_["ev"]["choice"] for d_ in sub)
    w = sum(1 for d_ in sub if pick[id(d_)] == d_["ev"]["choice"] and d_["ev"]["fb"] != d_["ev"]["choice"])
    l = sum(1 for d_ in sub if d_["ev"]["fb"] == d_["ev"]["choice"] and pick[id(d_)] != d_["ev"]["choice"])
    print(f"(1) bucket(F + predicted cluster offset) {label:14s} n={len(sub):3d}  {got / n:.1%}  (F {base / n:.1%}; +{w}/-{l})")

full = fit(data)
rng = np.random.default_rng(7)
boots = []
for _ in range(200):
    pick_days = rng.choice(days, size=len(days), replace=True)
    rows_b = [d_ for day in pick_days for d_ in data if d_["day"] == day]
    boots.append(fit(rows_b))
boots = np.array(boots)
print(f"    cluster offset model: mean offset {full[-2]:+.2f} C, residual sd {math.exp(full[-1]):.2f} C; per +1 sd of each condition (C):")
for i, name in enumerate(NAMES):
    lo_, hi_ = np.percentile(boots[:, i], [2.5, 97.5])
    flag = " *" if lo_ > 0 or hi_ < 0 else ""
    print(f"      {name:16s} {full[i]:+.3f}  [{lo_:+.3f}, {hi_:+.3f}]{flag}")

# (2) the truth over every station-day -------------------------------------------------------
T = []
for (st, d), members in H.items():
    if (st, d) not in truth or d < date(2026, 8, 20):
        continue
    r = row(st, d, members, d - timedelta(days=4))
    systems = {s: {"tmax_c": members[s], "bias_c": float(r["z"][SYSTEMS.index(s)] - members[s])} for s in members}
    raw_med = statistics.median(members.values())
    corr = statistics.median(v["tmax_c"] + v["bias_c"] for v in systems.values()) - raw_med
    x = cond(st, d, systems, r["f"], corr, r["c"], 5.0, 0.0, False)
    T.append((x, truth[(st, d)] - r["f"], d))
XT = np.array([t[0] for t in T])
yT = np.array([t[1] for t in T])
keep = [i for i, n in enumerate(NAMES) if n not in ("F station", "log minutes", "position")]
ZT = (XT[:, keep] - MU[keep]) / SD[keep]
A = np.c_[ZT, np.ones(len(ZT))]
beta = np.linalg.lstsq(A, yT, rcond=None)[0]
tdays = sorted({t[2] for t in T})
tb = []
for _ in range(200):
    sel = set(rng.choice(tdays, size=len(tdays), replace=True).tolist())
    idx = [i for i, t in enumerate(T) if t[2] in sel]
    tb.append(np.linalg.lstsq(A[idx], yT[idx], rcond=None)[0])
tb = np.array(tb)
print(f"\n(2) truth - F on {len(T)} station-days: mean {yT.mean():+.2f} C; per +1 sd of each condition (C), next to the cluster's lean:")
for j, i in enumerate(keep):
    lo_, hi_ = np.percentile(tb[:, j], [2.5, 97.5])
    flag = " *" if lo_ > 0 or hi_ < 0 else ""
    same = "same way" if np.sign(beta[j]) == np.sign(full[i]) else "opposite"
    print(f"      {NAMES[i]:16s} truth {beta[j]:+.3f} [{lo_:+.3f}, {hi_:+.3f}]{flag:2s}   cluster {full[i]:+.3f}   ({same})")
