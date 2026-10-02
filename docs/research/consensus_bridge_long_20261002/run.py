#!/usr/bin/env python3
"""OP=LIN|GEO|VIN : per-decision scores on a lambda grid (thin slice and the rest)."""
import os, sys, io, contextlib, json, dataclasses, math, gzip
from collections import defaultdict
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001"); sys.path.insert(0, ROOT)
OP = "LIN"; ARM = os.environ["ARM"]
os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-02-15", SMW_SPLIT="2026-07-01", SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="1", SMW_TAG="_op")
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
g = {"__name__": "op", "__file__": os.path.join(SMW, "score.py")}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(head + patch + rest, "score_op", "exec"), g)
decs, chosen, weights_for = g["decs"], g["chosen"], g["weights_for"]
import sqlite3
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target.cross_consensus import bridged_cross_probability
from bayes_basket_target.served_shape import apply_served_table_layers
import replay_p0_stable_pool as replay
import bayes_basket_target.cross_consensus as CC
if ARM == "OFF": CC.CONSENSUS_BRIDGE_LAMBDA_MAX = 0.0
elif ARM == "STRONG": CC.CONSENSUS_BRIDGE_LAMBDA_MAX = 0.6
elif ARM == "EARLY": CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C = 1.0, 1.5
else: raise SystemExit("ARM must be OFF|STRONG|EARLY")
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
layers = {}
LAMS = tuple(round(0.6 + 0.05 * i, 2) for i in range(15))
U = np.linspace(0.005, 0.995, 199)
def combine(d, w, grid):
    kc = [float(int(k)) for k in grid]
    probs = {s: {k: float(d["p"][s].get(k, 0.0)) for k in grid} for s in w}
    if OP == "LIN":
        return bridged_cross_probability(system_probabilities=probs, system_weights=w, system_centers_c={s: d["c"][s] for s in w}, ordered_keys=grid, key_centers_c=kc)[0]
    if OP == "GEO":
        logp = {k: sum(w[s] * math.log(max(probs[s][k], 1e-3)) for s in w) for k in grid}; m = max(logp.values())
        v = {k: math.exp(x - m) for k, x in logp.items()}; t = sum(v.values()); return {k: x / t for k, x in v.items()}
    # VIN: quantile averaging on the integer-degree grid
    edges = np.array([kc[0] - 0.5] + [x + 0.5 for x in kc])
    qbar = np.zeros_like(U)
    for s in w:
        p = np.array([probs[s][k] for k in grid]); p = p / p.sum(); cdf = np.concatenate([[0.0], np.cumsum(p)])
        cdf = np.maximum.accumulate(cdf + np.arange(len(cdf)) * 1e-12)
        qbar += w[s] * np.interp(U, cdf, edges)
    hist = np.histogram(qbar, bins=edges)[0].astype(float)
    # mass per bucket from the averaged quantile function, smoothed by the 199-point resolution
    hist = hist / hist.sum(); return {k: float(v) for k, v in zip(grid, hist)}
out = []
for d in decs:
    if not ("2026-02-15" <= d["day"] <= "2026-09-30") or d["day"] not in chosen["EBP"]: continue
    w = {s: v for s, v in weights_for("EBP", chosen["EBP"][d["day"]], d).items() if v > 1e-12}
    grid = replay._grid([d["p"][s] for s in w]); kc = [float(int(k)) for k in grid]
    base = combine(d, w, grid)
    pre = oos._holdout_shape_stack(base, grid=grid, cooling_shift_sigma=d["cooling"], station_latitude=d["lat"], neighbour_center_c=sum(w[s] * d["c"][s] for s in w), center_width_layer=None)
    if d["station"] not in layers: layers[d["station"]] = oos.p0_gate_tables(conn, station_id=d["station"])["center_width_layer"]
    sc = {}
    for lam in LAMS + (0.75, 1.1):
        v, _ = apply_served_table_layers(pre, ordered_keys=grid, key_centers_c=kc, center_width=dataclasses.replace(layers[d["station"]], width_lambda=lam))
        crps, nll, brier, _b, _a = replay._score(v, d["actual"]); sc[str(lam)] = (round(nll, 6), round(brier, 6), round(crps, 6))
    thin = len({d["family"][s] for s in w}) <= 3 and d["bin"] >= 36
    out.append({"station": d["station"], "day": d["day"], "known": d["known"].isoformat(), "thin": thin, "s": sc})
conn.close()
with gzip.open(os.path.join(HERE, f"rows_{ARM}.jsonl.gz"), "wt") as z:
    for r in out: z.write(json.dumps(r) + "\n")
print(ARM, "rows", len(out), "bridge consts", CC.CONSENSUS_BRIDGE_LAMBDA_MAX, CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C)
