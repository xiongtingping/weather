#!/usr/bin/env python3
import os, sys, io, contextlib, json, dataclasses, math
from collections import defaultdict
from datetime import date
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001"); sys.path.insert(0, ROOT)
os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-02-15", SMW_SPLIT="2026-07-01", SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="1", SMW_TAG="_tw")
src = open(os.path.join(SMW, "score.py")).read()
cut = src.index("def proxy(d, w):"); head, rest = src[:cut], src[cut:]
patch = '''
def _baseline(d):
    fams = defaultdict(list)
    for s in d["pool"]: fams[d["family"][s]].append(s)
    return {s: 1.0 / len(fams) / len(m) for m in fams.values() for s in m}
for d in decs:
    b = _baseline(d)
    skill = {s: max(2.0 * d["w0"][s] - b[s], 0.0) for s in d["pool"]}; tot = sum(skill.values())
    skill = {s: v / tot for s, v in skill.items()} if tot > 0 else dict(b)
    w = {s: 0.75 * skill[s] + 0.25 * b[s] for s in d["pool"]}; t = sum(w.values()); d["w0"] = {s: v / t for s, v in w.items()}
'''
rest = rest.replace('combos = {"H": [(e, h) for e in ETAS for h in HALVES], "HL": [(e, h) for e in ETAS for h in HALVES],', 'combos = {')
rest = rest.replace('"HF": [(e, h) for e in ETAS for h in HALVES], "EB": [(t,) for t in TAUS], "EBP": [(t,) for t in TAUS]}', '"EBP": [(t,) for t in TAUS]}')
rest = rest.replace('if SKIP_HF: del combos["HF"]', '')
rest = rest[:rest.index("# ---------------------------------------------------------------- oracle upper bounds")]
g = {"__name__": "tw", "__file__": os.path.join(SMW, "score.py")}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(head + patch + rest, "score_tw", "exec"), g)
decs, chosen, weights_for = g["decs"], g["chosen"], g["weights_for"]
import sqlite3
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target.cross_consensus import bridged_cross_probability
from bayes_basket_target.served_shape import apply_served_table_layers
import replay_p0_stable_pool as replay
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
layers = {}
GRID = (0.75, 0.8, 0.825, 0.85, 0.9, 0.95, 1.0, 1.1, 1.2)
def scores(d, w):
    w = {s: v for s, v in w.items() if v > 1e-12}
    grid = replay._grid([d["p"][s] for s in w]); kc = [float(int(k)) for k in grid]
    probs = {s: {k: float(d["p"][s].get(k, 0.0)) for k in grid} for s in w}
    blended = bridged_cross_probability(system_probabilities=probs, system_weights=w, system_centers_c={s: d["c"][s] for s in w}, ordered_keys=grid, key_centers_c=kc)[0]
    pre = oos._holdout_shape_stack(blended, grid=grid, cooling_shift_sigma=d["cooling"], station_latitude=d["lat"], neighbour_center_c=sum(w[s] * d["c"][s] for s in w), center_width_layer=None)
    if d["station"] not in layers: layers[d["station"]] = oos.p0_gate_tables(conn, station_id=d["station"])["center_width_layer"]
    out = {}
    for lam in GRID:
        v, _ = apply_served_table_layers(pre, ordered_keys=grid, key_centers_c=kc, center_width=dataclasses.replace(layers[d["station"]], width_lambda=lam))
        crps, nll, brier, _b, _a = replay._score(v, d["actual"]); out[lam] = (nll, brier, crps)
    return out
rows = []
for d in decs:
    if d["day"] < "2026-02-15" or d["day"] > "2026-09-30" or d["day"] not in chosen["EBP"]: continue
    nf = len({d["family"][s] for s in d["pool"]})
    if nf > 4: continue
    rows.append({"station": d["station"], "day": d["day"], "known": d["known"].isoformat(), "bin": d["bin"], "nf": nf,
                 "s": {str(k): v for k, v in scores(d, weights_for("EBP", chosen["EBP"][d["day"]], d)).items()}})
conn.close()
json.dump(rows, open(os.path.join(HERE, "rows.json"), "w"))
print("rows", len(rows))
