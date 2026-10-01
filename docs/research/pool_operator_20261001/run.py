#!/usr/bin/env python3
"""Pool operators vs production linear pool, on codex's captured production packets.  See PROTOCOL.md."""
from __future__ import annotations
import json, gzip, glob, math, os, sys, time
from collections import defaultdict
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[3]; sys.path.insert(0, str(ROOT))
from bayes_basket_target.native_ensemble_p0 import _shift_probability_c, _bucket_for_value
from bayes_basket_target.center_width_calibration import served_bucket_axis, apply_center_width_calibration
import bayes_basket_target.center_width_calibration as cw_module
from bayes_basket_target.cross_consensus import bridged_cross_probability, distribution_sigma_c, shift_probability
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
CAP = ROOT / "tmp/production_fusion_trace_20261001_codex"; OUT = Path(__file__).resolve().parent
LAMS = [round(0.55 + 0.025 * i, 3) for i in range(29)]
EPS = float(os.environ.get('POOL_EPS', '1e-3'))  # pre-registered 1e-3; other values are diagnostics only
t0 = time.monotonic()

def norm(p):
    s = sum(p.values()); return {k: v / s for k, v in p.items()}

rows = [json.loads(l) for f in sorted(glob.glob(str(CAP / "????.jsonl.gz"))) for l in gzip.open(f, "rt")]
packets = []
for r in rows:
    l = r["lineage"]
    if (l.get("band_reliability_json") or {}).get("applied_factors"): continue
    keys = sorted(r["ordinals"], key=r["ordinals"].get); intervals = l["bucket_intervals_c_json"]
    axis, edges = served_bucket_axis(intervals, keys)
    w = {k: float(v) for k, v in l["system_weights_json"].items() if float(v) > 1e-12}
    pp = {s: {k: float(l["native_system_probabilities_json"][s][k]) for k in keys} for s in w}
    centers = l["native_calibrated_system_centers_c_json"]; cw = l["center_width_json"]; cool = l.get("synoptic_cooling_json") or {}
    y = r["truth"]["official_tmax_c"]; hit = _bucket_for_value(y, intervals)
    cw_module.bucket_rung_width_c = lambda axis, _r=float(cw["bucket_rung_c"]): _r
    def physics(p, cool=cool, keys=keys, axis=axis, intervals=intervals):
        z = cool.get("shift_sigma"); shift = float(cool.get("shift_c") or 0) if z is None else float(z) * distribution_sigma_c([p[k] for k in keys], axis)
        return dict(p) if abs(shift) <= 1e-9 else _shift_probability_c(p, ordered=keys, bucket_centers_c=[x + shift for x in axis], intervals=intervals)
    def width(p, lam, keys=keys, axis=axis, cw=cw):
        return apply_center_width_calibration(p, delta_c=0.0, width_lambda=lam, station_width_scale=1., sigma_floor_rungs=0., ordered_keys=keys, key_centers_c=axis, open_edge_bounds_c=cw.get("open_edge_bounds_c"))[0]
    # operators -> pre-width vector (after physics)
    linear = norm({k: sum(w[s] * pp[s][k] for s in w) for k in keys})
    bridged = bridged_cross_probability(system_probabilities=pp, system_weights=w, system_centers_c=centers, ordered_keys=keys, key_centers_c=axis)[0]
    cc = sum(w[s] * float(centers[s]) for s in w)
    consensus = {k: 0.0 for k in keys}
    for s in w:
        sh = shift_probability(pp[s], delta_c=cc - float(centers[s]), ordered_keys=keys, key_centers_c=axis)
        for k in keys: consensus[k] += w[s] * sh[k]
    consensus = norm(consensus)
    geo = norm({k: math.exp(sum(w[s] * math.log(max(pp[s][k], EPS)) for s in w)) for k in keys})
    # Appendix: consensus bridge v2 -- scale to A - B about the consensus centre.
    sig2 = {s: distribution_sigma_c([pp[s][k] for k in keys], axis) ** 2 for s in w}
    Avar = sum(w[s] * sig2[s] for s in w); Bvar = sum(w[s] * (float(centers[s]) - cc) ** 2 for s in w)
    target = max(Avar - Bvar, 0.25 * Avar)
    lam_e1 = math.sqrt(target / Avar) if Avar > 0 else 1.0
    lam_e2 = math.sqrt(target / (Avar + Bvar)) if Avar + Bvar > 0 else 1.0
    def clamp(x): return max(0.5, min(1.5, x))
    pre = {"A": physics(bridged), "B": physics(consensus), "C": physics(geo), "D": physics(linear),
           "E1": physics(width(consensus, clamp(lam_e1))), "E2": physics(width(linear, clamp(lam_e2)))}
    lam_b = {"E1": lam_e1, "E2": lam_e2}
    scores = {}
    for op, p in pre.items():
        for lam in LAMS:
            scores[(op, lam)] = _served_scores(width(p, lam), ordered_keys=keys, settled_bucket=hit, excluded_buckets=(), key_centers_c=axis)
    prod = _served_scores(r["probabilities"], ordered_keys=keys, settled_bucket=hit, excluded_buckets=(), key_centers_c=axis)
    # sanity: production path replay at 0.75 equals stored
    rep = scores[("A", 0.75)]
    scores[("E3", 1.0)] = scores[("E1", 1.0)]
    packets.append({"sd": (r["station_id"], r["event_date"]), "day": r["event_date"], "n": len(w), "h": r["hours_to_settlement"], "scores": scores, "prod": prod, "replay_gap": abs(rep[0] - prod[0]), "lam_b": lam_b, "A": Avar, "B": Bvar})
print(f"packets={len(packets)} max replay gap(LogScore, stored vs A@0.75)={max(p['replay_gap'] for p in packets):.2e} t={time.monotonic()-t0:.0f}s", flush=True)
days = sorted({p["day"] for p in packets})
# rolling-origin lambda per operator
fit = {}
for op in ["A", "B", "C", "D", "E1", "E2"]:
    for i in range(3, len(days)):
        prefix = [p for p in packets if p["day"] < days[i]]
        fit[(op, days[i])] = min(LAMS, key=lambda lam: sum(p["scores"][(op, lam)][0] for p in prefix) / len(prefix))
scored = [p for p in packets if p["day"] >= days[3]]
def arm_scores(p, arm):
    if arm == "A0": return p["prod"]
    if arm == "E3": return p["scores"][("E1", 1.0)]
    op = {"A1": "A", "B": "B", "C": "C", "D": "D", "E1": "E1", "E2": "E2"}[arm]; return p["scores"][(op, fit[(op, p["day"])])]
# station-day averaging: average triples within station-day per arm, then gate with cluster = station-day (one entry per sd)
def sd_avg(arm):
    acc = defaultdict(list)
    for p in scored: acc[p["sd"]].append(arm_scores(p, arm))
    return {sd: tuple(float(np.mean([t[i] for t in v])) for i in range(3)) for sd, v in acc.items()}
sds = sorted({p["sd"] for p in scored})
res = {"packets": len(packets), "scored_packets": len(scored), "scored_station_days": len(sds), "days_scored": days[3:], "lambda_by_day": {f"{k[0]}|{k[1]}": v for k, v in fit.items()}, "pairs": {}, "subgroups": {}}
tabs = {arm: sd_avg(arm) for arm in ["A0", "A1", "B", "C", "D", "E1", "E2", "E3"]}
for a, b in [("A1", "A0"), ("B", "A0"), ("B", "A1"), ("C", "A0"), ("C", "A1"), ("D", "A0"), ("D", "A1"), ("E1", "A0"), ("E1", "A1"), ("E1", "C"), ("E2", "A0"), ("E2", "A1"), ("E3", "A0")]:
    g = _proper_score_gate([tabs[b][s] for s in sds], [tabs[a][s] for s in sds], sds)
    res["pairs"][f"{a}_vs_{b}"] = g
    print(f"{a} vs {b}: dLog={g['d_logscore']:+.4f} {g['ci_logscore']} dBrier={g['d_brier']:+.4f} dCRPS={g['d_crps']:+.4f} lower={g['lower']}", flush=True)
for name, pred in {"n<=3": lambda p: p["n"] <= 3, "n4_5": lambda p: 4 <= p["n"] <= 5, "n6": lambda p: p["n"] == 6, "<=36h": lambda p: p["h"] <= 36, ">36h": lambda p: p["h"] > 36}.items():
    sub = [p for p in scored if pred(p)]
    if not sub: continue
    acc = defaultdict(lambda: defaultdict(list))
    for p in sub:
        for arm in ["A0", "A1", "B", "C", "D", "E1", "E2", "E3"]: acc[arm][p["sd"]].append(arm_scores(p, arm))
    ssd = sorted(acc["A0"]); mean = {arm: tuple(float(np.mean([np.mean([t[i] for t in acc[arm][s]]) for s in ssd])) for i in range(3)) for arm in acc}
    res["subgroups"][name] = {"station_days": len(ssd), "mean_scores": mean, "delta_vs_A0": {arm: [mean[arm][i] - mean["A0"][i] for i in range(3)] for arm in mean}}
    print(name, len(ssd), {arm: round(mean[arm][0] - mean["A0"][0], 4) for arm in mean}, flush=True)
res["lam_b_summary"] = {k: {"median": float(np.median([p["lam_b"][k] for p in packets])), "p10": float(np.percentile([p["lam_b"][k] for p in packets], 10)), "p90": float(np.percentile([p["lam_b"][k] for p in packets], 90))} for k in ("E1", "E2")}
res["A_minus_B_check"] = {str(n): {"A": float(np.mean([p["A"] for p in packets if p["n"] == n])), "B": float(np.mean([p["B"] for p in packets if p["n"] == n])), "packets": sum(1 for p in packets if p["n"] == n)} for n in sorted({p["n"] for p in packets})}
print("lam_b", res["lam_b_summary"]); print("A,B by n", res["A_minus_B_check"])
res["mean_scores_all"] = {arm: [float(np.mean([tabs[arm][s][i] for s in sds])) for i in range(3)] for arm in tabs}
print("means", {k: [round(x, 4) for x in v] for k, v in res["mean_scores_all"].items()})
if "POOL_EPS" not in os.environ:
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=str))
print("DONE", round(time.monotonic() - t0))
