#!/usr/bin/env python3
"""Read-only: how many stable-pool OOS verdicts flip if the gate reads
Brier and LogScore (charter 211) instead of Brier and CRPS.

Stored verdict = the gate JSON on every stable-pool evaluation since the
gate history epoch; re-judged from the very same stored window means.
"""
import json, sqlite3, sys, time
from collections import Counter, defaultdict
EPS = 1e-12
TOL_LOSS, TOL_GAIN = 0.02, 0.05
EPOCH = "2026-09-14T04:00:00Z"
t0 = time.time()
conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5)
plan = conn.execute("EXPLAIN QUERY PLAN SELECT evaluation_id FROM calibration_cross_native_ensemble_oos_evaluation WHERE evaluated_at_utc>=?", (EPOCH,)).fetchall()
print("plan:", plan, flush=True)
sql = ("SELECT station_id, season_bin, lead_bucket, run_cycle, available_system_set_hash, evaluated_at_utc,"
       " json_extract(oos_metrics_json,'$.gate.pass'), json_extract(oos_metrics_json,'$.gate.path'),"
       " json_extract(oos_metrics_json,'$.gate.window_learned_metrics'), json_extract(oos_metrics_json,'$.gate.window_climate_metrics'),"
       " json_extract(oos_metrics_json,'$.gate.breakage_floor.own_relative_crps_loss'),"
       " json_extract(oos_metrics_json,'$.nll'), json_extract(oos_metrics_json,'$.equal_weight_diagnostic.climatology_metrics.nll'),"
       " json_extract(oos_metrics_json,'$.brier'), json_extract(oos_metrics_json,'$.equal_weight_diagnostic.climatology_metrics.brier')"
       " FROM calibration_cross_native_ensemble_oos_evaluation"
       " WHERE evaluated_at_utc>=? AND json_extract(oos_metrics_json,'$.gate.membership_mode')='stable_pool'")
rows = conn.execute(sql, (EPOCH,)).fetchall()
conn.close()
print(f"rows {len(rows)} in {time.time()-t0:.0f}s", flush=True)

def window_pass(wl, wc, use):
    strict = wl["brier"] <= wc["brier"] + EPS and wl[use] <= wc[use] + EPS
    if strict: return True, "STRICT"
    if wc["brier"] <= EPS or wc[use] <= EPS or wc.get("nll", 0.0) <= EPS: return False, "REJECTED"
    loss = (wl["brier"] - wc["brier"]) / wc["brier"]
    gain = (wc[use] - wl[use]) / wc[use]
    nll_gain = (wc["nll"] - wl["nll"]) / wc["nll"]
    if use == "crps":
        ok = loss <= TOL_LOSS and gain >= TOL_GAIN and nll_gain >= TOL_GAIN
    else:
        ok = loss <= TOL_LOSS and nll_gain >= TOL_GAIN
    return ok, ("TOLERANCE" if ok else "REJECTED")

paths = Counter(); stored_pass = 0; recomputed_old_match = 0
own_nll_loss = []; crps_breakage = 0
per_row = []
for st, sb, lb, rc, sh, at, gpass, gpath, wl, wc, own_crps_loss, own_nll, clim_nll, own_brier, clim_brier in rows:
    wl = json.loads(wl); wc = json.loads(wc)
    old_ok, old_path = window_pass(wl, wc, "crps")
    new_ok, new_path = window_pass(wl, wc, "nll")
    breakage_old = own_crps_loss is None or own_crps_loss > 1.0
    nll_rel = None if (own_nll is None or clim_nll is None or clim_nll <= EPS) else (own_nll - clim_nll) / clim_nll
    brier_rel = None if (own_brier is None or clim_brier is None or clim_brier <= EPS) else (own_brier - clim_brier) / clim_brier
    if nll_rel is not None: own_nll_loss.append(nll_rel)
    crps_breakage += breakage_old
    old_final = old_ok and not breakage_old
    stored_pass += bool(gpass)
    recomputed_old_match += (old_final == bool(gpass))
    paths[(str(gpath), old_path, new_path)] += 1
    per_row.append((st, sb, lb, rc, sh, at, bool(gpass), old_final, new_ok, breakage_old, nll_rel, brier_rel))

print(f"stored pass {stored_pass}/{len(rows)}; recomputed old verdict matches stored: {recomputed_old_match}/{len(rows)}")
print("crps breakage hits (old):", crps_breakage)
own_nll_loss.sort()
import statistics
q = lambda x: own_nll_loss[min(len(own_nll_loss)-1, int(x*len(own_nll_loss)))]
print(f"own relative NLL loss quantiles: p50 {q(0.5):+.3f} p90 {q(0.9):+.3f} p99 {q(0.99):+.3f} max {own_nll_loss[-1]:+.3f}; share>1.0 {sum(v>1.0 for v in own_nll_loss)/len(own_nll_loss):.4f}")
# candidate NLL breakage thresholds
for T in (0.5, 1.0, 2.0):
    hits = sum(1 for r in per_row if r[10] is not None and r[10] > T)
    print(f"  NLL breakage threshold {T}: hits {hits} ({hits/len(rows):.4%})")
# flips, all evaluations (window verdict only, breakage on NLL threshold 1.0)
def final_new(r, T=1.0):
    b = r[10] is None or r[10] > T
    return r[8] and not b
flip_all = Counter()
for r in per_row:
    flip_all[(r[7], final_new(r))] += 1
print("all evaluations (old_final, new_final):", dict(flip_all))
# newest per key
newest = {}
for r in per_row:
    k = r[:5]
    if k not in newest or r[5] > newest[k][5]: newest[k] = r
flip_new = Counter(); by_station = defaultdict(Counter)
for r in newest.values():
    key = (r[7], final_new(r)); flip_new[key] += 1
    if r[7] != final_new(r): by_station[r[0]][key] += 1
print(f"newest per key: {len(newest)} keys; (old_final, new_final):", dict(flip_new))
print("flips by station:", {s: dict(c) for s, c in sorted(by_station.items())})
print("path transitions (stored path, old window path, new window path) top:", paths.most_common(12))
print(f"{time.time()-t0:.0f}s")
