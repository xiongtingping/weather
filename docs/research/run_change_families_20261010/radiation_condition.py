#!/usr/bin/env python3
"""PROTOCOL.md addendum 2: forecast sunshine anomaly as a second condition."""
from __future__ import annotations
import json, math, sqlite3, statistics, warnings
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[2]
src = (HERE / "study.py").read_text()
ns = {"__file__": str(HERE / "study.py"), "__name__": "study_defs"}
exec(compile(src[: src.index("inst = defaultdict(list)")], "study.py", "exec"), ns)
exec(compile(src[src.index("def block("): src.index("def bll(")], "study.py", "exec"), ns)
by_sd, changes, at, floored, argmax_low, logit, utc, block = (ns[k] for k in ("by_sd", "changes", "at", "floored", "argmax_low", "logit", "utc", "block"))
TAU = 6.0

ssot = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
rad = defaultdict(lambda: defaultdict(list))          # station -> local_date -> [(captured, value)]
for st, day, val, cap in ssot.execute(
        "SELECT station_id, local_date, shortwave_radiation_sum, captured_at_utc FROM calibration_station_forecast_physics_version"
        " WHERE shortwave_radiation_sum IS NOT NULL AND local_date >= '2026-08-01'"):
    rad[st][day].append((utc(cap), float(val)))
ssot.close()
for st in rad:
    for d in rad[st]:
        rad[st][d].sort()


def rad_z(st, day, when):
    vers = [v for c, v in rad[st].get(day, []) if c <= when]
    if not vers:
        return None
    others = [vs[-1][1] for d, vs in rad[st].items() if d != day and vs]
    if len(others) < 15:
        return None
    sd = statistics.pstdev(others)
    return (vers[-1] - statistics.fmean(others)) / sd if sd > 0 else None


def fresh(st, day, system, when):
    ch = [c for c in changes(st, day, system) if c[0] <= when]
    if not ch:
        return 0.0
    a, d = ch[-1]
    return d * math.exp(-max(0.0, (when - a).total_seconds() / 3600.0) / TAU)


rows = []
for (st, day) in sorted(by_sd):
    for arrived, delta in changes(st, day, "ECMWF_AIFS_ENS_GLOBAL"):
        t = arrived + timedelta(hours=1)
        r = at((st, day), t, not_before=arrived)
        if r is None or r["y"] not in r["names"]:
            continue
        q = floored(r["q"]); M = argmax_low(q); yk = r["names"].index(r["y"])
        U, D = float(sum(q[M + 1:])), float(sum(q[:M]))
        if U <= 0 or D <= 0 or yk == M:
            continue
        z = rad_z(st, day, t)
        if z is None:
            continue
        rung = float(r["rung"] or 1.0)
        mom = fresh(st, day, "ECMWF_AIFS_ENS_GLOBAL", t) + fresh(st, day, "ECMWF_AIFS_SINGLE_POINT", t) + fresh(st, day, "NOAA_GEFS_05", t)
        rows.append({"st": st, "d": day, "Lq": U / (U + D), "y": 1.0 if yk > M else 0.0, "z": z, "mom": mom,
                     "x": [fresh(st, day, s, t) / rung for s in ("ECMWF_AIFS_ENS_GLOBAL", "ECMWF_AIFS_SINGLE_POINT", "NOAA_GEFS_05")]})


def score(sel, sign):
    sub = [r for r in rows if sel(r)]
    if len(sub) < 60:
        return {"n": len(sub)}
    v = [sign(r) * (1 if r["y"] > 0 else -1) for r in sub]
    lo, hi, _ = block([r["d"] for r in sub], v)
    days = sorted({r["d"] for r in sub}); half = days[len(days) // 2]
    return {"n": len(sub), "score": float(np.mean(v)), "ci": [lo, hi],
            "halves": [float(np.mean([a for a, r in zip(v, sub) if r["d"] < half])), float(np.mean([a for a, r in zip(v, sub) if r["d"] >= half]))]}


def increment(base, cand):
    days = sorted({r["d"] for r in rows}); dv = np.array([r["d"] for r in rows]); y = np.array([r["y"] for r in rows])
    preds = []
    for fe in (base, cand):
        X = np.array([fe(r) for r in rows]); p = np.full(len(rows), np.nan)
        for i, day in enumerate(days):
            if i < 8:
                continue
            tr, te = dv < day, dv == day
            s = StandardScaler().fit(X[tr]); m = LogisticRegression(C=1.0, max_iter=5000).fit(s.transform(X[tr]), y[tr])
            p[te] = m.predict_proba(s.transform(X[te]))[:, 1]
        preds.append(p)
    ok = ~np.isnan(preds[0])
    bl = lambda p: -(y[ok] * np.log(np.clip(p[ok], 1e-6, 1)) + (1 - y[ok]) * np.log(np.clip(1 - p[ok], 1e-6, 1)))
    diff = bl(preds[1]) - bl(preds[0])
    lo, hi, draws = block(list(dv[ok]), list(diff))
    td = sorted(set(dv[ok])); half = td[len(td) // 2]
    return {"n": int(ok.sum()), "delta": float(diff.mean()), "ci": [lo, hi],
            "halves": [float(diff[dv[ok] < half].mean()), float(diff[dv[ok] >= half].mean())], "p_one": float((draws >= 0).mean())}


B0 = lambda r: [logit(r["Lq"])]
B2 = lambda r: [logit(r["Lq"])] + r["x"]
res = {
    "rows": len(rows), "days": len({r["d"] for r in rows}),
    "z_alone_score_|z|>=1": score(lambda r: abs(r["z"]) >= 1, lambda r: math.copysign(1, r["z"])),
    "z_alone_increment": increment(B0, lambda r: B0(r) + [r["z"]]),
    "z_over_momentum_increment": increment(B2, lambda r: B2(r) + [r["z"]]),
    "momentum_alone_score_|M|>=0.3": score(lambda r: abs(r["mom"]) >= 0.3, lambda r: math.copysign(1, r["mom"])),
    "M&z_agree": score(lambda r: abs(r["mom"]) >= 0.3 and abs(r["z"]) >= 0.5 and r["mom"] * r["z"] > 0, lambda r: math.copysign(1, r["mom"])),
    "M&z_oppose": score(lambda r: abs(r["mom"]) >= 0.3 and abs(r["z"]) >= 0.5 and r["mom"] * r["z"] < 0, lambda r: math.copysign(1, r["mom"])),
    "M_quiet_z": score(lambda r: abs(r["mom"]) < 0.3 and abs(r["z"]) >= 1, lambda r: math.copysign(1, r["z"])),
}
(HERE / "results_radiation.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
print(json.dumps(res, ensure_ascii=False, indent=1))
