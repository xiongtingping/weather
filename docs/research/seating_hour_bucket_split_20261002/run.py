#!/usr/bin/env python3
"""Seating counterfactual: all observed systems at their own lead certificates (B) vs the
freshest-bucket-only seat production serves (A).  Reuses station_model_weights score.py."""
import os, sys, io, contextlib, json
from collections import defaultdict
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001")
sys.path.insert(0, ROOT)
os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-08-10", SMW_SPLIT="2026-09-14", SMW_TAUS="5", SMW_SKIP_HF="1", SMW_TAG="_seat")
src = open(os.path.join(SMW, "score.py")).read()
src = src[:src.index("# ---------------------------------------------------------------- per station-day records")]
g = {"__name__": "seat", "__file__": os.path.join(SMW, "score.py")}
with contextlib.redirect_stdout(io.StringIO()): exec(compile(src, "score_head", "exec"), g)
decs, norm = g["decs"], g["norm"]
# downstream (same as score.py) -- pulled in from its source after the records section
tail = open(os.path.join(SMW, "score.py")).read()
ds_src = tail[tail.index("# ---------------------------------------------------------------- full production downstream"):tail.index("results = defaultdict(dict)")]
g["by_station"] = {d["station"]: None for d in decs}
exec(compile(ds_src, "score_downstream", "exec"), g)
downstream = g["downstream"]; store = g["store"]
LEAD_ORDER = {"h00_12": 0, "h12_24": 1, "h24_48": 2, "h48_plus": 3}
rows = []
for d in decs:
    if not ("2026-08-10" <= d["day"] <= "2026-09-30"): continue
    fresh = [s for s in d["pool"] if d["slead"][s] == d["lead"]]
    split = [s for s in d["pool"] if d["slead"][s] != d["lead"]]
    if not split or len(fresh) < 2: continue
    wA = norm({s: d["w0"][s] for s in fresh}); wB = dict(d["w0"])
    rows.append({"sd": d["sd"], "day": d["day"], "bin": d["bin"], "n_fresh": len(fresh), "n_split": len(split), "lead": d["lead"],
                 "A": downstream(d, wA), "B": downstream(d, wB)})
print("split decisions", len(rows), "of", sum(1 for d in decs if "2026-08-10" <= d["day"] <= "2026-09-30"))
def gate(rs):
    acc = defaultdict(lambda: {"A": [], "B": []})
    for r in rs: acc[r["sd"]]["A"].append(r["A"]); acc[r["sd"]]["B"].append(r["B"])
    k = sorted(acc); A = [tuple(float(np.mean([t[i] for t in acc[s]["A"]])) for i in range(3)) for s in k]; B = [tuple(float(np.mean([t[i] for t in acc[s]["B"]])) for i in range(3)) for s in k]
    return store._proper_score_gate(A, B, k)
out = {}
for name, pred in (("all", lambda r: True), ("bin>=48", lambda r: r["bin"] >= 48), ("bin36-48", lambda r: r["bin"] == 36), ("bin24-36", lambda r: r["bin"] == 24), ("bin12-24", lambda r: r["bin"] == 12), ("bin0-12", lambda r: r["bin"] == 0),
                   ("fresh2", lambda r: r["n_fresh"] == 2), ("fresh3-4", lambda r: 3 <= r["n_fresh"] <= 4), ("first_half", lambda r: r["day"] < "2026-09-14"), ("second_half", lambda r: r["day"] >= "2026-09-14")):
    rs = [r for r in rows if pred(r)]
    if len(rs) < 20: continue
    x = gate(rs); out[name] = {k: x[k] for k in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower", "station_days", "cells")}
    print(f"{name:12s} n={len(rs):5d} sd={x['station_days']:4d} dLog(B-A)={x['d_logscore']:+.4f} [{x['ci_logscore'][0]:+.4f},{x['ci_logscore'][1]:+.4f}] dBrier={x['d_brier']:+.4f} [{x['ci_brier'][0]:+.4f},{x['ci_brier'][1]:+.4f}] dCRPS={x['d_crps']:+.4f} lower={x['lower']}")
from collections import Counter
out["split_census"] = {str(k): v for k, v in sorted(Counter((r["lead"], r["n_fresh"], r["n_split"]) for r in rows).items())}
print("census (lead, fresh, split):", out["split_census"])
json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
