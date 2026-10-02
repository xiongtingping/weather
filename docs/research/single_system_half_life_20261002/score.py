#!/usr/bin/env python3
"""Score every arm's capture on the served chain with seat weights (see PROTOCOL.md)."""
import gzip, json, os, sys, sqlite3, dataclasses
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target import center_width_store as store
from bayes_basket_target.cross_consensus import bridged_cross_probability
from bayes_basket_target.served_shape import apply_served_table_layers
import replay_p0_stable_pool as replay
BINS = ((48, 1e9), (36, 48), (24, 36), (12, 24), (0, 12))
ARMS = ["CUR", "B3", "B8", "L8", "L30"]
HALF = set(open(os.path.join(HERE, "half_stations.txt")).read().strip().split(","))
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30); layers = {}
def utc(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
def served(r, st):
    w = {s: v for s, v in r["seat"].items() if v > 1e-12 and s in r["sys"]}
    t = sum(w.values()); w = {s: v / t for s, v in w.items()}
    grid = replay._grid([r["sys"][s]["p"] for s in w]); kc = [float(int(k)) for k in grid]
    probs = {s: {k: float(r["sys"][s]["p"].get(k, 0.0)) for k in grid} for s in w}
    base = bridged_cross_probability(system_probabilities=probs, system_weights=w, system_centers_c={s: r["sys"][s]["c"] for s in w}, ordered_keys=grid, key_centers_c=kc)[0] if len(w) > 1 else probs[next(iter(w))]
    pre = oos._holdout_shape_stack(base, grid=grid, cooling_shift_sigma=r["cooling"], station_latitude=r["lat"], neighbour_center_c=sum(w[s] * r["sys"][s]["c"] for s in w), center_width_layer=None)
    if st not in layers: layers[st] = oos.p0_gate_tables(conn, station_id=st)["center_width_layer"]
    end = datetime.fromisoformat(r["event_date"]).replace(tzinfo=ZoneInfo(r["tz"])) + timedelta(days=1)
    thin = len({r["family"][s] for s in w}) <= 3 and (end - utc(r["at"])).total_seconds() / 3600 > 36
    v, _ = apply_served_table_layers(pre, ordered_keys=grid, key_centers_c=kc, center_width=dataclasses.replace(layers[st], width_lambda=1.1 if thin else 0.75))
    crps, nll, brier, _b, _a = replay._score(v, r["actual"]); return (nll, brier, crps)
tabs = {}
for arm in ARMS:
    d = os.path.join(HERE, f"pass1_sc_{arm}")
    if not os.path.isdir(d) or not HALF <= {f[:4] for f in os.listdir(d)}: print(arm, "capture incomplete"); continue
    acc = defaultdict(list)
    for f in sorted(os.listdir(d)):
        st = f[:4]
        if st not in HALF: continue
        by_day = defaultdict(list)
        for l in gzip.open(os.path.join(d, f), "rt"):
            r = json.loads(l)
            if "2026-08-15" <= r["event_date"] <= "2026-09-30": by_day[r["event_date"]].append(r)
        for day, rs in by_day.items():
            end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(rs[0]["tz"])) + timedelta(days=1)
            for lo, hi in BINS:
                inbin = [r for r in rs if lo <= (end - utc(r["at"])).total_seconds() / 3600 < hi]
                if inbin: acc[(st, day)].append(served(max(inbin, key=lambda x: x["at"]), st))
    tabs[arm] = {k: tuple(float(np.mean([t[i] for t in v])) for i in range(3)) for k, v in acc.items()}
    print(arm, "station-days", len(tabs[arm]), flush=True)
conn.close()
out = {}
if "CUR" in tabs:
    for arm in ARMS[1:]:
        if arm not in tabs: continue
        k = sorted(set(tabs["CUR"]) & set(tabs[arm])); res = {}
        for name, p in (("all", lambda x: True), ("h1", lambda x: x[1] < "2026-09-08"), ("h2", lambda x: x[1] >= "2026-09-08")):
            kk = [x for x in k if p(x)]; store.GATE_CONFIDENCE = 1 - 0.05 / 4
            g = store._proper_score_gate([tabs["CUR"][x] for x in kk], [tabs[arm][x] for x in kk], kk); store.GATE_CONFIDENCE = 0.95
            res[name] = {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower")}
        out[arm] = res; x = res["all"]
        print(f"{arm:4s} vs CUR: dLog={x['d_logscore']:+.4f} [{x['ci_logscore'][0]:+.4f},{x['ci_logscore'][1]:+.4f}] dBrier={x['d_brier']:+.4f} [{x['ci_brier'][0]:+.4f},{x['ci_brier'][1]:+.4f}] dCRPS={x['d_crps']:+.4f} [{x['ci_crps'][0]:+.4f},{x['ci_crps'][1]:+.4f}] lower={x['lower']} halves={res['h1']['d_logscore']:+.4f}/{res['h2']['d_logscore']:+.4f}")
    json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
