#!/usr/bin/env python3
"""OP=LIN|GEO|VIN : per-decision scores on a lambda grid (thin slice and the rest)."""
import os, sys, io, contextlib, json, dataclasses, math, gzip
from collections import defaultdict
from pathlib import Path
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001"); sys.path.insert(0, ROOT)
os.environ.update(SMW_PASS="PHYS", SMW_FROM="2026-08-15", SMW_SPLIT="2026-07-01", SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="1", SMW_TAG="_op")
src = open(os.path.join(SMW, "score.py")).read().replace('(HERE / PASS_DIR)', '(Path(os.environ["PHYS_DIR"]))')
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
import bayes_basket_target.cross_consensus as CC
from bayes_basket_target.cross_consensus import bridged_cross_probability
from bayes_basket_target.served_shape import apply_served_table_layers
import replay_p0_stable_pool as replay
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
layers = {}
def served(d, w, *, k_pos=1.0, k_neg=1.0, thin_lam=1.1, bridge=(1.0, 1.5)):
    w = {s: v for s, v in w.items() if v > 1e-12}
    grid = replay._grid([d["p"][s] for s in w]); kc = [float(int(x)) for x in grid]
    probs = {s: {x: float(d["p"][s].get(x, 0.0)) for x in grid} for s in w}
    old = (CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C)
    CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C = bridge
    try:
        base = bridged_cross_probability(system_probabilities=probs, system_weights=w, system_centers_c={s: d["c"][s] for s in w}, ordered_keys=grid, key_centers_c=kc)[0]
    finally:
        CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C = old
    z = d["cooling"]; z = None if z is None else (z * (k_pos if z > 0 else k_neg))
    pre = oos._holdout_shape_stack(base, grid=grid, cooling_shift_sigma=z, station_latitude=d["lat"], neighbour_center_c=sum(w[s] * d["c"][s] for s in w), center_width_layer=None)
    if d["station"] not in layers: layers[d["station"]] = oos.p0_gate_tables(conn, station_id=d["station"])["center_width_layer"]
    thin = len({d["family"][s] for s in w}) <= 3 and d["bin"] >= 36
    lam = thin_lam if thin else 0.75
    v, _ = apply_served_table_layers(pre, ordered_keys=grid, key_centers_c=kc, center_width=dataclasses.replace(layers[d["station"]], width_lambda=lam))
    crps, nll, brier, _b, _a = replay._score(v, d["actual"]); return (round(nll, 6), round(brier, 6), round(crps, 6))
ARMS = {"k1": {}, "k0": {"k_pos": 0, "k_neg": 0}, "k0.5": {"k_pos": .5, "k_neg": .5}, "k1.5": {"k_pos": 1.5, "k_neg": 1.5}, "k2": {"k_pos": 2, "k_neg": 2},
        "warm0.5": {"k_pos": .5}, "warm1.5": {"k_pos": 1.5}, "cold0.5": {"k_neg": .5}, "cold1.5": {"k_neg": 1.5},
        "thin0.75": {"thin_lam": 0.75}, "bridge_old": {"bridge": (1.5, 2.0)}}
out = []
for d in decs:
    if not ("2026-08-20" <= d["day"] <= "2026-09-30") or d["day"] not in chosen["EBP"]: continue
    w = weights_for("EBP", chosen["EBP"][d["day"]], d)
    rec = {a: served(d, w, **kw) for a, kw in ARMS.items()}
    rec["seat"] = served(d, d["w0"])
    out.append({"station": d["station"], "day": d["day"], "signal": d["cooling"], "s": rec})
conn.close()
import gzip as _gz
with _gz.open(os.path.join(HERE, "rows.jsonl.gz"), "wt") as z:
    for r in out: z.write(json.dumps(r) + "\n")
print("rows", len(out), "with signal", sum(r["signal"] is not None for r in out))
