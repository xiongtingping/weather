#!/usr/bin/env python3
"""Appendix 2: weights and seating arms on the captured production packets.  See PROTOCOL.md."""
from __future__ import annotations
import json, gzip, glob, math, sys, time
from collections import defaultdict
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[3]; sys.path.insert(0, str(ROOT))
from bayes_basket_target.native_ensemble_p0 import _shift_probability_c, _bucket_for_value
from bayes_basket_target.center_width_calibration import served_bucket_axis, apply_center_width_calibration
import bayes_basket_target.center_width_calibration as cw_module
from bayes_basket_target.cross_consensus import bridged_cross_probability, distribution_sigma_c
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
CAP = ROOT / "tmp/production_fusion_trace_20261001_codex"; OUT = Path(__file__).resolve().parent
LAMS = [round(0.55 + 0.025 * i, 3) for i in range(29)]; FAM = ["DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS", "ECMWF_AIFS_ENS_GLOBAL", "ECMWF_IFS_ENS_OPEN_PF50", "NOAA_AIGEFS_025", "NOAA_GEFS_05"]
t0 = time.monotonic()
def norm(p): s = sum(p.values()); return {k: v / s for k, v in p.items()}
rows = [json.loads(l) for f in sorted(glob.glob(str(CAP / "????.jsonl.gz"))) for l in gzip.open(f, "rt")]
P = []
for r in rows:
    l = r["lineage"]
    if (l.get("band_reliability_json") or {}).get("applied_factors"): continue
    keys = sorted(r["ordinals"], key=r["ordinals"].get); intervals = l["bucket_intervals_c_json"]; axis, _ = served_bucket_axis(intervals, keys)
    w = {k: float(v) for k, v in l["system_weights_json"].items() if float(v) > 1e-12}
    pp = {s: {k: float(l["native_system_probabilities_json"][s][k]) for k in keys} for s in w}
    y = r["truth"]["official_tmax_c"]; hit = _bucket_for_value(y, intervals)
    seat = (l.get("fusion_blend_json") or {}).get("seat_weights"); seat = json.loads(seat) if isinstance(seat, str) else seat
    seat = {s: float(seat[s]) for s in w} if seat and set(seat) >= set(w) else dict(w)
    P.append(dict(sd=(r["station_id"], r["event_date"]), day=r["event_date"], n=len(w), h=r["hours_to_settlement"], keys=keys, intervals=intervals, axis=axis, w=w, seat=norm(seat), pp=pp, hit=hit,
                  centers=l["native_calibrated_system_centers_c_json"], cw=l["center_width_json"], cool=l.get("synoptic_cooling_json") or {}, prod=r["probabilities"],
                  phit={s: max(pp[s][hit], 1e-3) for s in w}, sig2={s: distribution_sigma_c([pp[s][k] for k in keys], axis) ** 2 for s in w}))
days = sorted({p["day"] for p in P}); print("packets", len(P), "days", len(days), flush=True)
def finish(p, weights, lam):
    keys, axis = p["keys"], p["axis"]; cw_module.bucket_rung_width_c = lambda a, _r=float(p["cw"]["bucket_rung_c"]): _r
    pp = {s: p["pp"][s] for s in weights}
    q = bridged_cross_probability(system_probabilities=pp, system_weights=weights, system_centers_c={s: p["centers"][s] for s in weights}, ordered_keys=keys, key_centers_c=axis)[0] if len(weights) > 1 else norm(dict(pp[next(iter(weights))]))
    z = p["cool"].get("shift_sigma"); shift = float(p["cool"].get("shift_c") or 0) if z is None else float(z) * distribution_sigma_c([q[k] for k in keys], axis)
    if abs(shift) > 1e-9: q = _shift_probability_c(q, ordered=keys, bucket_centers_c=[x + shift for x in axis], intervals=p["intervals"])
    q = apply_center_width_calibration(q, delta_c=0.0, width_lambda=lam, station_width_scale=1., sigma_floor_rungs=0., ordered_keys=keys, key_centers_c=axis, open_edge_bounds_c=p["cw"].get("open_edge_bounds_c"))[0]
    return _served_scores(q, ordered_keys=keys, settled_bucket=p["hit"], excluded_buckets=(), key_centers_c=axis)
# fleet log-score-optimal weights on a prefix
def em_weights(prefix):
    theta = np.zeros(len(FAM)); idx = {f: i for i, f in enumerate(FAM)}
    def F(th):
        w = np.exp(th - th.max()); tot = 0.0
        for p in prefix:
            ws = np.array([w[idx[s]] for s in p["w"]]); ps = np.array([p["phit"][s] for s in p["w"]]); tot += math.log((ws * ps).sum() / ws.sum())
        return tot / len(prefix)
    lr = 0.5
    for _ in range(150):
        g = np.zeros(len(FAM)); base = F(theta)
        for i in range(len(FAM)):
            e = np.zeros(len(FAM)); e[i] = 1e-4; g[i] = (F(theta + e) - base) / 1e-4
        theta = theta + lr * g; theta -= theta.mean()
    w = np.exp(theta); w /= w.sum(); return {f: float(w[idx[f]]) for f in FAM}
def single_skill(prefix):
    acc = defaultdict(list)
    for p in prefix:
        for s in p["w"]: acc[s].append(-math.log(p["phit"][s]))
    return {s: float(np.mean(v)) for s, v in acc.items()}
fits = {}
for i in range(3, len(days)):
    prefix = [p for p in P if p["day"] < days[i]]; fits[days[i]] = {"em": em_weights(prefix), "skill": single_skill(prefix)}
    print(days[i], "EM", {k[:9]: round(v, 3) for k, v in fits[days[i]]["em"].items()}, "skill", {k[:9]: round(v, 3) for k, v in fits[days[i]]["skill"].items()}, flush=True)
def arm_weights(p, arm):
    w = p["w"]
    if arm in ("A1",): return w
    if arm == "S": return p["seat"]
    if arm == "EQ": return {s: 1 / len(w) for s in w}
    if arm == "PW": return norm({s: 1 / p["sig2"][s] for s in w})
    f = fits[p["day"]]
    if arm == "EM": return norm({s: f["em"][s] for s in w})
    if arm == "EM50": e = norm({s: f["em"][s] for s in w}); return norm({s: 0.5 * e[s] + 0.5 * w[s] for s in w})
    if arm == "DROP1":
        if len(w) < 3: return w
        worst = max(w, key=lambda s: f["skill"].get(s, 0)); return norm({s: w[s] for s in w if s != worst})
    if arm == "BEST1": best = min(w, key=lambda s: f["skill"].get(s, 9)); return {best: 1.0}
ARMS = ["A1", "S", "EQ", "PW", "EM", "EM50", "DROP1", "BEST1"]
scored = [p for p in P if p["day"] >= days[3]]
cache = {}
def sc(p, arm, lam):
    k = (id(p), arm, lam)
    if k not in cache: cache[k] = finish(p, arm_weights(p, arm), lam)
    return cache[k]
lamfit = {}
for arm in ARMS:
    for i in range(3, len(days)):
        prefix = [p for p in P if p["day"] < days[i]]
        lamfit[(arm, days[i])] = min(LAMS, key=lambda lam: sum(sc(p, arm, lam)[0] for p in prefix) / len(prefix))
    print(arm, "lambda by day", [lamfit[(arm, d)] for d in days[3:]], f"t={time.monotonic()-t0:.0f}s", flush=True)
def tab(arm):
    acc = defaultdict(list)
    for p in scored:
        acc[p["sd"]].append(_served_scores(p["prod"], ordered_keys=p["keys"], settled_bucket=p["hit"], excluded_buckets=(), key_centers_c=p["axis"]) if arm == "A0" else sc(p, arm, lamfit[(arm, p["day"])]))
    return {sd: tuple(float(np.mean([t[i] for t in v])) for i in range(3)) for sd, v in acc.items()}
tabs = {arm: tab(arm) for arm in ["A0"] + ARMS}; sds = sorted(tabs["A0"])
res = {"scored_station_days": len(sds), "days_scored": days[3:], "fits": {d: fits[d] for d in days[3:]}, "lambda": {f"{a}|{d}": v for (a, d), v in lamfit.items()}, "pairs": {}, "means": {a: [float(np.mean([tabs[a][s][i] for s in sds])) for i in range(3)] for a in tabs}, "subgroups": {}}
for a in ARMS[1:]:
    for b in ("A0", "A1"):
        g = _proper_score_gate([tabs[b][s] for s in sds], [tabs[a][s] for s in sds], sds); res["pairs"][f"{a}_vs_{b}"] = g
        print(f"{a} vs {b}: dLog={g['d_logscore']:+.4f} [{g['ci_logscore'][0]:+.4f},{g['ci_logscore'][1]:+.4f}] dBrier={g['d_brier']:+.4f} dCRPS={g['d_crps']:+.4f} lower={g['lower']}", flush=True)
for name, pred in {"n<=3": lambda p: p["n"] <= 3, "n4_5": lambda p: 4 <= p["n"] <= 5, "n6": lambda p: p["n"] == 6}.items():
    sub = [p for p in scored if pred(p)]; acc = defaultdict(lambda: defaultdict(list))
    for p in sub:
        for a in ["A0"] + ARMS: acc[a][p["sd"]].append(_served_scores(p["prod"], ordered_keys=p["keys"], settled_bucket=p["hit"], excluded_buckets=(), key_centers_c=p["axis"]) if a == "A0" else sc(p, a, lamfit[(a, p["day"])]))
    ssd = sorted(acc["A0"]); m = {a: float(np.mean([np.mean([t[0] for t in acc[a][s]]) for s in ssd])) for a in acc}
    res["subgroups"][name] = {"station_days": len(ssd), "dLog_vs_A0": {a: m[a] - m["A0"] for a in m}}; print(name, len(ssd), {a: round(m[a] - m["A0"], 4) for a in m}, flush=True)
(OUT / "results_w.json").write_text(json.dumps(res, indent=1, default=str)); print("DONE", round(time.monotonic() - t0))
