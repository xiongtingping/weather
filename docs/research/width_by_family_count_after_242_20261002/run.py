#!/usr/bin/env python3
"""Diagnostic (not a gate): does the served P0's width match its centre error by
seated family count, before (old seat weights, kappa 0.5, served as-is) and after
(kappa 0.25 seat prior + charter 242 station weights)?  Same downstream as score.py."""
import os, sys, io, contextlib, json, math
from collections import defaultdict
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001"); sys.path.insert(0, ROOT)
os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-04-01", SMW_SPLIT="2026-07-01", SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="1", SMW_TAG="_wd")
src = open(os.path.join(SMW, "score.py")).read()
cut = src.index("def proxy(d, w):"); head, rest = src[:cut], src[cut:]
patch = '''
def _baseline(d):
    fams = defaultdict(list)
    for s in d["pool"]: fams[d["family"][s]].append(s)
    return {s: 1.0 / len(fams) / len(m) for m in fams.values() for s in m}
for d in decs:
    d["w_old"] = dict(d["w0"])
    b = _baseline(d)
    skill = {s: max(2.0 * d["w0"][s] - b[s], 0.0) for s in d["pool"]}; tot = sum(skill.values())
    skill = {s: v / tot for s, v in skill.items()} if tot > 0 else dict(b)
    w = {s: 0.75 * skill[s] + 0.25 * b[s] for s in d["pool"]}; t = sum(w.values()); d["w0"] = {s: v / t for s, v in w.items()}
'''
rest = rest.replace('combos = {"H": [(e, h) for e in ETAS for h in HALVES], "HL": [(e, h) for e in ETAS for h in HALVES],', 'combos = {')
rest = rest.replace('"HF": [(e, h) for e in ETAS for h in HALVES], "EB": [(t,) for t in TAUS], "EBP": [(t,) for t in TAUS]}', '"EBP": [(t,) for t in TAUS]}')
rest = rest.replace('if SKIP_HF: del combos["HF"]', '')
rest = rest[:rest.index("# ---------------------------------------------------------------- oracle upper bounds")]
g = {"__name__": "wd", "__file__": os.path.join(SMW, "score.py")}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(head + patch + rest, "score_wd", "exec"), g)
decs, chosen, weights_for = g["decs"], g["chosen"], g["weights_for"]
import sqlite3
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target.cross_consensus import bridged_cross_probability
from bayes_basket_target.served_shape import apply_served_table_layers
import replay_p0_stable_pool as replay
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
layers = {}
def served(d, w):
    w = {s: v for s, v in w.items() if v > 1e-12}
    grid = replay._grid([d["p"][s] for s in w]); kc = [float(int(k)) for k in grid]
    probs = {s: {k: float(d["p"][s].get(k, 0.0)) for k in grid} for s in w}
    blended = bridged_cross_probability(system_probabilities=probs, system_weights=w, system_centers_c={s: d["c"][s] for s in w}, ordered_keys=grid, key_centers_c=kc)[0]
    pre = oos._holdout_shape_stack(blended, grid=grid, cooling_shift_sigma=d["cooling"], station_latitude=d["lat"], neighbour_center_c=sum(w[s] * d["c"][s] for s in w), center_width_layer=None)
    if d["station"] not in layers: layers[d["station"]] = oos.p0_gate_tables(conn, station_id=d["station"])["center_width_layer"]
    out, _ = apply_served_table_layers(pre, ordered_keys=grid, key_centers_c=kc, center_width=layers[d["station"]])
    x = np.array(kc); p = np.array([out[k] for k in grid]); mu = float(p @ x); var = float(p @ (x - mu) ** 2)
    return mu, var, replay._score(out, d["actual"])[1]
rows = []
for d in decs:
    if not ("2026-04-01" <= d["day"] <= "2026-09-30"): continue
    n = len({d["family"][s] for s in d["pool"]})
    wn = weights_for("EBP", chosen["EBP"][d["day"]], d)
    for arm, w in (("before", d["w_old"]), ("after", wn)):
        mu, var, nll = served(d, w); rows.append((arm, n, d["station"], d["day"], (d["actual"] - mu) ** 2, var, nll))
conn.close()
agg = defaultdict(lambda: defaultdict(list))
for arm, n, st, day, se, var, nll in rows: agg[(arm, n)][(st, day)].append((se, var, nll))
out = {}
for arm in ("before", "after"):
    for n in sorted({k[1] for k in agg}):
        sd = agg.get((arm, n))
        if not sd: continue
        se = np.mean([np.mean([t[0] for t in v]) for v in sd.values()]); var = np.mean([np.mean([t[1] for t in v]) for v in sd.values()])
        z2 = np.mean([np.mean([t[0] / t[1] for t in v if t[1] > 1e-9]) for v in sd.values()]); nll = np.mean([np.mean([t[2] for t in v]) for v in sd.values()])
        out[f"{arm}|{n}"] = {"station_days": len(sd), "mse": float(se), "var": float(var), "mse_over_var": float(se / var), "mean_z2": float(z2), "nll": float(nll)}
        print(f"{arm:6s} families={n} station-days={len(sd):5d}  centre MSE={se:.3f}  served var={var:.3f}  MSE/var={se/var:.2f}  mean z^2={z2:.2f}  NLL={nll:.4f}")
json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
