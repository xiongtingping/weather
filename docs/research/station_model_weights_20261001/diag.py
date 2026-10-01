#!/usr/bin/env python3
"""Post-verdict diagnostics (not gates): is the hindsight station-weight gain
real-but-unestimable, or in-sample optimism / non-stationarity?
Cross-fitted oracles (fit on one fold, score the other), per-cell stability,
and which hyperparameters the forward arms picked."""
import os, sys, math, io, contextlib, json, gzip
from collections import defaultdict, Counter
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "score.py")).read()
src = src[:src.index("# ---------------------------------------------------------------- verdicts")]
g = {"__name__": "diag", "__file__": os.path.join(HERE, "score.py")}
buf = io.StringIO()
with contextlib.redirect_stdout(buf): exec(compile(src, "score_head", "exec"), g)
decs, scored, FAM, FI, fit_fixed, downstream, norm, rec = g["decs"], g["scored"], g["FAM"], g["FI"], g["fit_fixed"], g["downstream"], g["norm"], g["rec"]
SPLIT = g["SPLIT"]; tab = g["tab"]; sd = g["sd"]; store = g["store"]
from datetime import date
def fold_half(d): return 0 if d["day"] < SPLIT else 1
def fold_week(d): return (date.fromisoformat(d["day"]) - date(2026, 8, 10)).days // 7 % 2
out = {}
for fname, fold in (("half", fold_half), ("alt_week", fold_week)):
    for gran in ("S", "SL"):
        key = (lambda d: d["station"]) if gran == "S" else (lambda d: (d["station"], d["lead"]))
        fits = {}
        for k in {key(d) for d in scored}:
            for f in (0, 1):
                train = [d for d in scored if key(d) == k and fold(d) != f]
                fits[(k, f)] = fit_fixed(train) if len(train) >= 5 else None
        res = defaultdict(list)
        for d in scored:
            w = fits[(key(d), fold(d))]
            ww = d["w0"] if w is None else norm({s: float(w[FI[s]]) for s in d["pool"]})
            res[(d["station"], d["day"])].append(downstream(d, ww))
        t = {k: tuple(float(np.mean([x[i] for x in v])) for i in range(3)) for k, v in res.items()}
        store.GATE_CONFIDENCE = 0.95
        gate = store._proper_score_gate([tab["A0"][k] for k in sd], [t[k] for k in sd], sd)
        name = f"CROSS-{gran}-{fname}"
        out[name] = {"d_logscore": gate["d_logscore"], "ci95": gate["ci_logscore"], "d_brier": gate["d_brier"], "d_crps": gate["d_crps"]}
        print(f"{name:22s} dLog={gate['d_logscore']:+.4f} ci95={['%+.4f' % x for x in gate['ci_logscore']]} dBrier={gate['d_brier']:+.4f} dCRPS={gate['d_crps']:+.4f}", flush=True)
# per station x system stability: mean r_jt in first vs second half
cells = []
for st, days in rec.items():
    a = defaultdict(list); b = defaultdict(list)
    for day, R in days.items():
        if day < "2026-08-10": continue
        for s, v in R["r"].items(): (a if day < SPLIT else b)[s].append(v)
    for s in FAM:
        if len(a[s]) >= 8 and len(b[s]) >= 8: cells.append((st, s, float(np.mean(a[s])), float(np.mean(b[s])), len(a[s]), len(b[s])))
x = np.array([c[2] for c in cells]); y = np.array([c[3] for c in cells])
out["stability"] = {"cells": len(cells), "corr_first_vs_second": float(np.corrcoef(x, y)[0, 1]), "same_sign_share": float(np.mean(np.sign(x) == np.sign(y))),
                    "first_sd": float(x.std()), "second_sd": float(y.std())}
# expected sampling spread of a cell mean under no true difference: per-day r sd / sqrt(n)
sds = []
for st, days in rec.items():
    for s in FAM:
        v = [R["r"][s] for day, R in days.items() if s in R["r"] and day >= "2026-08-10"]
        if len(v) >= 16: sds.append(np.std(v) / math.sqrt(len(v) / 2))
out["stability"]["noise_sd_of_half_mean"] = float(np.median(sds))
print("stability", out["stability"], flush=True)
# system-level (fleet) vs station-level part of the cell means
fleet = {s: float(np.mean([c[2] + c[3] for c in cells if c[1] == s]) / 2) for s in FAM}
resid_a = np.array([c[2] - fleet[c[1]] for c in cells]); resid_b = np.array([c[3] - fleet[c[1]] for c in cells])
out["stability"]["fleet_mean_r_by_system"] = fleet
out["stability"]["corr_station_specific_part"] = float(np.corrcoef(resid_a, resid_b)[0, 1])
print("fleet mean r by system", {k[:10]: round(v, 3) for k, v in fleet.items()}, "station-specific corr", round(out["stability"]["corr_station_specific_part"], 3), flush=True)
out["chosen"] = {arm: {str(k): n for k, n in Counter(map(tuple, v.values())).items()} for arm, v in g["chosen"].items()}
print("chosen", {k: {str(kk): vv for kk, vv in v.items()} for k, v in out["chosen"].items()}, flush=True)
json.dump(out, open(os.path.join(HERE, "diag.json"), "w"), indent=1, default=str)
