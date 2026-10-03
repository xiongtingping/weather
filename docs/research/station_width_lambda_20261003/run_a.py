#!/usr/bin/env python3
"""PROTOCOL.md data A: every representative P0 decision of the long replay,
scored downstream at each width lambda of the grid.

    python3 run_a.py prep                 # decisions + served weights -> cache_a.pkl
    python3 run_a.py score CYYZ,EDDM ...  # rows_a/<station>.json

Same machine as ``thin_pool_width_long_20261002/run.py`` (score.py loaded from
source, served weights = kappa 0.25 seat prior + charter 242 EBP*), with no
filter on the seated family count and the protocol's lambda grid.
"""
import contextlib
import dataclasses
import io
import json
import os
import pickle
import sqlite3
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
GRID = (0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.1, 1.2, 1.35, 1.5)
CACHE = os.path.join(HERE, "cache_a.pkl")
OUT = os.path.join(HERE, "rows_a")


def prep():
    os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-02-15", SMW_SPLIT="2026-07-01",
                      SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="1", SMW_TAG="_sw")
    src = open(os.path.join(SMW, "score.py")).read()
    cut = src.index("def proxy(d, w):")
    head, rest = src[:cut], src[cut:]
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
    old_a = 'combos = {"H": [(e, h) for e in ETAS for h in HALVES], "HL": [(e, h) for e in ETAS for h in HALVES],'
    old_b = '"HF": [(e, h) for e in ETAS for h in HALVES], "EB": [(t,) for t in TAUS], "EBP": [(t,) for t in TAUS]}'
    assert rest.count(old_a) == 1 and rest.count(old_b) == 1
    rest = rest.replace(old_a, "combos = {").replace(old_b, '"EBP": [(t,) for t in TAUS]}')
    rest = rest.replace('if SKIP_HF: del combos["HF"]', "")
    rest = rest[:rest.index("# ---------------------------------------------------------------- oracle upper bounds")]
    g = {"__name__": "sw", "__file__": os.path.join(SMW, "score.py")}
    t0 = time.monotonic()
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(head + patch + rest, "score_sw", "exec"), g)
    decs, chosen, weights_for = g["decs"], g["chosen"], g["weights_for"]
    out = []
    for d in decs:
        if d["day"] < "2026-02-15" or d["day"] > "2026-09-30" or d["day"] not in chosen["EBP"]:
            continue
        w = {s: v for s, v in weights_for("EBP", chosen["EBP"][d["day"]], d).items() if v > 1e-12}
        out.append({
            "station": d["station"], "day": d["day"], "known": d["known"].isoformat(), "bin": d["bin"],
            "nf": len({d["family"][s] for s in d["pool"]}), "seated": len(w), "actual": d["actual"],
            "lat": d["lat"], "cooling": d["cooling"], "w": w,
            "p": {s: d["p"][s] for s in w}, "c": {s: d["c"][s] for s in w},
        })
    with open(CACHE, "wb") as handle:
        pickle.dump(out, handle)
    print("decisions", len(out), "station-days", len({(r["station"], r["day"]) for r in out}),
          "stations", len({r["station"] for r in out}), "%.0fs" % (time.monotonic() - t0))


def score(stations):
    from bayes_basket_target import native_ensemble_oos as oos
    from bayes_basket_target.cross_consensus import bridged_cross_probability
    from bayes_basket_target.served_shape import apply_served_table_layers
    import replay_p0_stable_pool as replay

    with open(CACHE, "rb") as handle:
        decs = pickle.load(handle)
    os.makedirs(OUT, exist_ok=True)
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
    by_station = defaultdict(list)
    for d in decs:
        by_station[d["station"]].append(d)
    for station in stations:
        t0 = time.monotonic()
        layer = oos.p0_gate_tables(conn, station_id=station)["center_width_layer"]
        assert layer is not None and abs(layer.width_lambda - 0.75) < 1e-12 and abs(layer.delta_c) < 1e-12
        rows = []
        for d in by_station[station]:
            w = d["w"]
            grid = replay._grid([d["p"][s] for s in w])
            kc = [float(int(k)) for k in grid]
            probs = {s: {k: float(d["p"][s].get(k, 0.0)) for k in grid} for s in w}
            if len(w) > 1:
                blended = bridged_cross_probability(
                    system_probabilities=probs, system_weights=w, system_centers_c={s: d["c"][s] for s in w},
                    ordered_keys=grid, key_centers_c=kc)[0]
            else:
                only = probs[next(iter(w))]
                total = sum(only.values())
                blended = {k: v / total for k, v in only.items()}
            pre = oos._holdout_shape_stack(
                blended, grid=grid, cooling_shift_sigma=d["cooling"], station_latitude=d["lat"],
                neighbour_center_c=sum(w[s] * d["c"][s] for s in w), center_width_layer=None)
            s = {}
            for lam in GRID:
                v, _ = apply_served_table_layers(pre, ordered_keys=grid, key_centers_c=kc,
                                                 center_width=dataclasses.replace(layer, width_lambda=lam))
                crps, nll, brier, _b, _a = replay._score(v, d["actual"])
                s[str(lam)] = [round(nll, 6), round(brier, 6), round(crps, 6)]
            rows.append({k: d[k] for k in ("station", "day", "known", "bin", "nf", "seated")} | {"s": s})
        with open(os.path.join(OUT, f"{station}.json"), "w") as handle:
            json.dump(rows, handle)
        print(station, len(rows), "%.0fs" % (time.monotonic() - t0), flush=True)
    conn.close()


if __name__ == "__main__":
    if sys.argv[1] == "prep":
        prep()
    elif sys.argv[1] == "score":
        score(sys.argv[2].split(","))
    elif sys.argv[1] == "stations":
        with open(CACHE, "rb") as handle:
            print(",".join(sorted({d["station"] for d in pickle.load(handle)})))
