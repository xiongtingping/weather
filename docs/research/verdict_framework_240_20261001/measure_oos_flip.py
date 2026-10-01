"""Read-only: how many stored cross-native OOS verdicts flip under charter 240's ranked verdict.

Re-judged with the production function (native_ensemble_oos._cross_oos_gate) on the very means each
evaluation stored: stable-pool rows on their stored window means plus the NLL breakage floor; other rows
on their own holdout means against the stored climatology baseline.  Newest evaluation per key gives the
admitted / withdrawn counts that serving would see."""
import json, sqlite3, sys, time, collections, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[3]; sys.path.insert(0, str(ROOT))
from bayes_basket_target import native_ensemble_oos as oos
EPOCH = "2026-09-14T04:00:00Z"
t0 = time.time()
c = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5); c.execute("pragma query_only=ON")
sql = ("SELECT station_id, season_bin, lead_bucket, run_cycle, available_system_set_hash, evaluated_at_utc, oos_state,"
       " json_extract(oos_metrics_json,'$.gate.membership_mode'), json_extract(oos_metrics_json,'$.gate.window_learned_metrics'),"
       " json_extract(oos_metrics_json,'$.gate.window_climate_metrics'), json_extract(oos_metrics_json,'$.gate.breakage_floor.hit'),"
       " oos_metrics_json, climatology_baseline_metrics_json"
       " FROM calibration_cross_native_ensemble_oos_evaluation WHERE evaluated_at_utc>=?")
flips = collections.Counter(); paths = collections.Counter(); newest = {}; n = 0
for st, sb, lb, rc, sh, at, state, mode, wl, wc, broke, own_json, clim_json in c.execute(sql, (EPOCH,)):
    n += 1
    old = state == "PRODUCTION_NO_LIVE_OOS_PASS"
    try:
        if mode == "stable_pool" and wl and wc:
            g = oos._cross_oos_gate(json.loads(wl), json.loads(wc)); new = bool(g["pass"]) and not bool(broke)
            kind = "stable_pool"
        else:
            own = json.loads(own_json); clim = json.loads(clim_json)
            g = oos._cross_oos_gate({k: own[k] for k in ("brier", "nll", "crps") if k in own}, {k: clim[k] for k in ("brier", "nll", "crps") if k in clim})
            new = bool(g["pass"]); kind = "contest"
    except (KeyError, TypeError, ValueError):
        flips["unreadable"] += 1; continue
    flips[(kind, old, new)] += 1
    if old != new: paths[(kind, g["path"])] += 1
    key = (st, sb, lb, rc, sh)
    if key not in newest or at > newest[key][0]: newest[key] = (at, kind, old, new)
c.close()
keys = collections.Counter((k, o, nw) for _a, k, o, nw in newest.values())
out = {"rows": n, "seconds": round(time.time() - t0), "flips": {str(k): v for k, v in flips.items()}, "flip_paths": {str(k): v for k, v in paths.items()},
       "newest_per_key": {str(k): v for k, v in keys.items()}, "key_count": len(newest)}
(pathlib.Path(__file__).parent / "oos_flip.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))

# ---- fidelity: the charter 211 rule (as it stood in 901f1d61) on the same means must reproduce the stored verdict
def old_gate(m, cl, EPS=1e-12):
    if m["brier"] <= cl["brier"] + EPS and m["nll"] <= cl["nll"] + EPS: return True
    if cl["brier"] <= EPS or cl["nll"] <= EPS: return False
    return (m["brier"] - cl["brier"]) / cl["brier"] <= 0.02 and (cl["nll"] - m["nll"]) / cl["nll"] >= 0.05
c = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5); c.execute("pragma query_only=ON")
fid = collections.Counter(); why = collections.Counter()
for st, sb, lb, rc, sh, at, state, mode, wl, wc, broke, own_json, clim_json in c.execute(sql, (EPOCH,)):
    stored = state == "PRODUCTION_NO_LIVE_OOS_PASS"
    try:
        if mode == "stable_pool" and wl and wc:
            m, cl = json.loads(wl), json.loads(wc); re_old = old_gate(m, cl) and not bool(broke); kind = "stable_pool"
        else:
            m, cl = json.loads(own_json), json.loads(clim_json); re_old = old_gate(m, cl); kind = "contest"
        re_new = bool(oos._cross_oos_gate(m, cl)["pass"]) and not (kind == "stable_pool" and bool(broke))
    except (KeyError, TypeError, ValueError):
        continue
    fid[(kind, "old_rule_reproduces_stored", stored == re_old)] += 1
    if stored == re_old and re_old != re_new: why[(kind, "genuine_flip", re_old, re_new)] += 1
c.close()
out["fidelity"] = {str(k): v for k, v in fid.items()}; out["genuine_flips"] = {str(k): v for k, v in why.items()}
(pathlib.Path(__file__).parent / "oos_flip.json").write_text(json.dumps(out, indent=1))
print(json.dumps({"fidelity": out["fidelity"], "genuine_flips": out["genuine_flips"]}, indent=1))
