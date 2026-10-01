"""Measure the P0 climatology blend at its own layer, with production's functions.

For every station-day in the window, take the P0 envelope production actually
served (first and latest), re-run the same compile chain from the retained
members and the artifacts named in its lineage, and score three arms against
the settled truth: r = served (the blend as shipped), r = 1 (members only),
r = 0 (climate only).  Downstream layers (bridge, radiation shift, neighbour
tilt, centre/width, band) are re-applied with the receipts the lineage carries,
so the only difference between arms is the blend share.
"""
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.native_ensemble_p0 import (  # noqa: E402
    _bucket_centers, _bucket_for_value, _distribution_sigma_c,
    _interval_probability, _ordered_buckets, _resolved_bias_c,
    _shift_probability_c,
)
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    apply_center_width_calibration,
)

DB = "/Users/xiong/weather/data/bayes_basket_target_ssot.db"
START, END = sys.argv[1] if len(sys.argv) > 1 else "2026-08-15", sys.argv[2] if len(sys.argv) > 2 else "2026-09-05"
OUT = sys.argv[3] if len(sys.argv) > 3 else "/private/tmp/claude-501/-Users-xiong-weather/b62c5338-a9d3-4aeb-97b4-2fb839c056e3/scratchpad/climate_blend_rows.json"

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
from scripts.ssot_cold_offload import attach_cold_history  # noqa: E402

attach_cold_history(c, ("basket_probability_envelope",))  # charter 238: whole history

truth = {}
for st, d, v, rev, at in c.execute(
    "SELECT station_id, local_date, official_tmax_c, revision_no, canonical_committed_at_utc"
    " FROM calibration_station_daily_high_truth WHERE local_date BETWEEN ? AND ?", (START, END)):
    key = (st, d)
    cur = truth.get(key)
    if cur is None or (rev, at) > (cur[1], cur[2]):
        truth[key] = (float(v), rev, at)

rows = c.execute(
    "SELECT envelope_id, station_id, event_date, decision_at_utc FROM basket_probability_envelope"
    " WHERE stage='P0' AND event_date BETWEEN ? AND ?", (START, END)).fetchall()
first, latest = {}, {}
for eid, st, d, at in rows:
    k = (st, d)
    if k not in first or at < first[k][1]:
        first[k] = (eid, at)
    if k not in latest or at > latest[k][1]:
        latest[k] = (eid, at)

art_cache = {}
def artifact(sha):
    if sha not in art_cache:
        r = c.execute("SELECT bias_parameters_json, spread_parameters_json, reliability_parameters_json"
                      " FROM calibration_native_ensemble_system_artifact WHERE content_sha256=?", (sha,)).fetchone()
        art_cache[sha] = tuple(json.loads(x) for x in r) if r else None
    return art_cache[sha]

mem_cache = {}
def members(system, station, day, run_init):
    k = (system, station, day, run_init)
    if k not in mem_cache:
        best = {}
        for mid, v, at in c.execute(
            "SELECT member_id, raw_tmax_c, archive_query_at_utc FROM calibration_native_ensemble_historical_member_run"
            " WHERE ensemble_system_id=? AND station_id=? AND target_local_date=? AND native_run_init_utc=?",
            (system, station, day, run_init)):
            if mid not in best or str(at) > best[mid][1]:
                best[mid] = (float(v), str(at))
        mem_cache[k] = [x[0] for x in best.values()]
    return mem_cache[k]

def system_prob(vals, bias, spread, rel, r, intervals):
    mean = sum(vals) / len(vals)
    b = _resolved_bias_c(mean, bias)
    sf = float(spread["spread_factor"])
    corrected = [mean + b + sf * (v - mean) for v in vals]
    bw = None
    if len(vals) == 1:
        bw = max(0.5, float(spread.get("train_residual_sigma_c") or spread.get("train_rmse_c") or 0.0))
    mp = _interval_probability(corrected, intervals, bandwidth_c=bw)
    cp = _interval_probability([float(x) for x in rel["climate_samples_c"]], intervals)
    p = {k: r * mp[k] + (1.0 - r) * cp[k] for k in intervals}
    t = sum(p.values())
    return {k: v / t for k, v in p.items()}, mean + b, mp, cp

def mul(p, factors):
    q = {k: p[k] * float(factors.get(k, 1.0)) for k in p}
    t = sum(q.values())
    return {k: v / t for k, v in q.items()}

def downstream(sysprob, centers, weights, lin, intervals, ordered, shift_sigma):
    kc = _bucket_centers(intervals, ordered)
    if lin.get("consensus_bridge_json"):
        p, _ = bridged_cross_probability(system_probabilities=sysprob, system_weights=weights,
                                         system_centers_c=centers, ordered_keys=ordered, key_centers_c=kc)
    else:
        p = {k: sum(weights[s] * sysprob[s][k] for s in weights) for k in ordered}
        t = sum(p.values()); p = {k: v / t for k, v in p.items()}
    pre_shift_sigma = _distribution_sigma_c(p, kc)
    if shift_sigma is not None:
        shift = float(shift_sigma) * pre_shift_sigma
        if abs(shift) > 1e-9:
            p = _shift_probability_c(p, ordered=ordered, bucket_centers_c=[x + shift for x in kc], intervals=intervals)
    nb = lin.get("neighbour_direction_json")
    if nb and nb.get("state") == "APPLIED":
        p = mul(p, nb.get("applied_factors") or {})
    tr = lin.get("tail_reliability_json")
    if tr and tr.get("state") == "APPLIED":
        if "applied_factors" in tr:
            p = mul(p, tr["applied_factors"])
        elif tr.get("adjusted_buckets") and tr.get("center_bucket") in ordered:
            ci = ordered.index(tr["center_bucket"])
            f = {}
            for b in tr["adjusted_buckets"]:
                if b not in ordered:
                    continue
                f[b] = float(tr.get("warm_tail_factor", 1.0)) if ordered.index(b) > ci else float(tr.get("cold_tail_factor", 1.0))
            p = mul(p, f)
        else:
            raise RuntimeError("tail_unreproducible")
    cw = lin.get("center_width_json")
    if cw and cw.get("state") == "APPLIED":
        lam = cw["width_tier_lambda"] if cw.get("width_tier_lambda") is not None else cw["pooled_width_lambda"]
        p, _ = apply_center_width_calibration(
            # A receipt from before 2026-09-17 may carry the fleet regime
            # shift, which the transform composed onto the fitted one; the
            # parameter is gone, so the sum is passed as the shift.
            p, delta_c=float(cw["fitted_delta_c"]) + float(cw.get("regime_delta_c", 0.0)),
            width_lambda=float(lam),
            station_width_scale=float(cw.get("station_width_scale", 1.0)),
            sigma_floor_rungs=float(cw.get("sigma_floor_rungs", 0.0)),
            ordered_keys=ordered, key_centers_c=kc)
    bd = lin.get("band_reliability_json")
    if bd and bd.get("state") == "APPLIED":
        p = mul(p, bd.get("applied_factors") or {})
    return p, pre_shift_sigma

def score(p, tb, ordinals):
    nll = -math.log(max(p[tb], 1e-12))
    brier = sum((p[b] - (1.0 if b == tb else 0.0)) ** 2 for b in p)
    order = sorted(p, key=lambda b: ordinals[b])
    F = 0.0; crps = 0.0; ot = ordinals[tb]
    for b in order:
        F += p[b]
        crps += (F - (1.0 if ordinals[b] >= ot else 0.0)) ** 2
    return nll, brier, crps

def loadj(s):
    if s is None: return None
    if isinstance(s, str):
        try: return json.loads(s)
        except ValueError: return None
    return s

out = []
skipped = defaultdict(int)
targets = [("first", k, v) for k, v in first.items()] + [("latest", k, v) for k, v in latest.items()]
for which, (st, day), (eid, at) in targets:
    if (st, day) not in truth:
        skipped["no_truth"] += 1; continue
    r = c.execute("SELECT lineage_json, probabilities_json, bucket_ordinals_json FROM basket_probability_envelope WHERE envelope_id=?", (eid,)).fetchone()
    lin = json.loads(r[0]); served = json.loads(r[1]); ordinals = json.loads(r[2])
    for key in ("synoptic_cooling_json", "neighbour_direction_json", "tail_reliability_json", "center_width_json",
                "band_reliability_json", "consensus_bridge_json", "system_artifact_shas_json", "system_weights_json",
                "run_init_utc_by_system_json", "bucket_intervals_c_json", "provisional_parameter_sources_json"):
        lin[key] = loadj(lin.get(key))
    provisional = lin.get("provisional_parameter_sources_json") or {}
    intervals = lin["bucket_intervals_c_json"]
    if not intervals or not lin.get("system_artifact_shas_json") or not lin.get("run_init_utc_by_system_json"):
        skipped["lineage_missing"] += 1; continue
    ordered = _ordered_buckets(intervals)
    weights = {k: float(v) for k, v in lin["system_weights_json"].items()}
    tv = truth[(st, day)][0]
    tb = _bucket_for_value(tv, intervals)
    arms = {}
    ok = True
    rel_mean = 0.0
    memonly_by_sys = {}
    for r_name, r_val in (("served", None), ("members", 1.0), ("climate", 0.0)):
        sysprob, centers = {}, {}
        for s in weights:
            vals = members(s, st, day, lin["run_init_utc_by_system_json"].get(s))
            if not vals:
                ok = False; break
            if s in provisional:
                src = provisional[s]
                bias = {"bias_c": float(src["bias_c"])}
                spread = {"spread_factor": float(src["spread_factor"])}
                rel = {"reliability_weight": 1.0, "climate_samples_c": list(vals)}
                rr = 1.0
            else:
                art = artifact(lin["system_artifact_shas_json"].get(s))
                if not art:
                    ok = False; break
                bias, spread, rel = art
                rr = float(rel["reliability_weight"]) if r_val is None else r_val
            sysprob[s], centers[s], mp, cp = system_prob(vals, bias, spread, rel, rr, intervals)
            if r_val is None:
                rel_mean += weights[s] * float(rel["reliability_weight"])
                memonly_by_sys[s] = mp
        if not ok: break
        arms[r_name] = (sysprob, centers)
    if not ok:
        skipped["members_or_artifact_missing"] += 1; continue
    sc = lin.get("synoptic_cooling_json") or {}
    shift_sigma = sc.get("shift_sigma") if sc.get("state") == "RESOLVED" else None
    try:
        if shift_sigma is None and sc.get("shift_c"):
            p0, sig0 = downstream(*arms["served"], weights, lin, intervals, ordered, None)
            shift_sigma = float(sc["shift_c"]) / sig0 if sig0 > 0 else None
        vec = {}
        for r_name in arms:
            vec[r_name], _ = downstream(*arms[r_name], weights, lin, intervals, ordered, shift_sigma)
    except RuntimeError as exc:
        skipped[str(exc)] += 1; continue
    except Exception as exc:  # noqa: BLE001
        skipped["error:" + type(exc).__name__] += 1; continue
    maxdiff = max(abs(vec["served"][b] - served[b]) for b in ordered)
    open_buckets = [ordered[0], ordered[-1]]
    argmax_served = max(served, key=served.get)
    memonly_mix = {b: sum(weights[s] * memonly_by_sys[s][b] for s in weights) for b in ordered}
    rec = {
        "which": which, "station": st, "day": day, "decision_at": at, "truth_c": tv, "truth_bucket": tb,
        "truth_ordinal": ordinals[tb], "rel_mean": rel_mean, "maxdiff": maxdiff,
        "argmax_served": argmax_served, "argmax_is_open": argmax_served in open_buckets,
        "truth_is_open": tb in open_buckets,
        "served_open_mass": served[ordered[0]] + served[ordered[-1]],
        "members_open_mass": vec["members"][ordered[0]] + vec["members"][ordered[-1]],
        "memonly_mass_at_argmax": memonly_mix[argmax_served],
        "served_p_truth": served[tb], "members_p_truth": vec["members"][tb], "climate_p_truth": vec["climate"][tb],
        "served_top": served[ordered[-1]], "members_top": vec["members"][ordered[-1]],
        "served_bottom": served[ordered[0]], "members_bottom": vec["members"][ordered[0]],
        "truth_top": tb == ordered[-1], "truth_bottom": tb == ordered[0],
        "n_systems": len(weights), "n_provisional": len([s for s in weights if s in provisional]),
    }
    for r_name in ("served", "members", "climate"):
        vsrc = served if r_name == "served" else vec[r_name]
        nll, brier, crps = score(vsrc, tb, ordinals)
        rec[f"{r_name}_nll"], rec[f"{r_name}_brier"], rec[f"{r_name}_crps"] = nll, brier, crps
        rec[f"{r_name}_vec"] = {b: round(vsrc[b], 5) for b in ordered}
    out.append(rec)
    if len(out) % 200 == 0:
        print(f"... {len(out)} rows", file=sys.stderr)

json.dump({"rows": out, "skipped": dict(skipped), "window": [START, END]}, open(OUT, "w"))
print("rows", len(out), "skipped", dict(skipped))
