"""Shoulder information through P0's construction, step by step (熊先生 10-16「从原始模型到校准后逐步排查，看哪一步把信息丢了」).

For each served P0 behind the morning hourly P1 rows (joined.jsonl.gz), rebuild every
system's vector with production's own functions (native_ensemble_p0._interval_probability /
_calibrated_system_probability) from the raw members of the exact run the P0 seated
(calibration_native_ensemble_historical_member_run, archive_query_at <= the P0's decision),
and the exact system artifact (system_artifact_shas_json):

  A raw_hist     members counted into buckets, no kernel
  B raw_kernel   members dressed with the 0.5 C kernel
  C bias         members shifted by the artifact's bias, 0.5 C kernel
  D bias_spread  bias + spread factor + the artifact's kernel rule (reliability 1)
  E system       D mixed with climatology by the reliability weight (= what P0 stores)
  pools of B / D / E with the P0's own system weights, and the served P0.

Reference = the served P0's own mode m (no market anywhere).  Metrics on rows whose truth
is in m-1 or m+1: which-shoulder AUC and split log loss; on all rows: mode-vs-shoulder AUC.
"""
from __future__ import annotations

import gzip
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs/research/obs_minus_forecast_station_direction_20261015"))
import analyze as A  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402

SSOT = ROOT / "data/bayes_basket_target_ssot.db"


def auc(s, y):
    s = np.asarray(s, float); y = np.asarray(y, float)
    o = np.argsort(s, kind="mergesort"); r = np.empty(len(s)); r[o] = np.arange(1, len(s) + 1)
    # average ranks on ties
    _, inv, cnt = np.unique(s, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=r); r = (sums / cnt)[inv]
    n1 = y.sum(); n0 = len(y) - n1
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float("nan")


def main():
    c = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=5)
    seen = {}
    for line in gzip.open(ROOT / "docs/research/p0m_morning_side_20261016/joined.jsonl.gz", "rt"):
        r = json.loads(line)
        if not r.get("p0") or not (7 <= r["local_hour"] < 13):
            continue
        key = (r["station"], r["day"], r["p0"]["decided_at"])
        seen.setdefault(key, r)
    print("unique morning P0 decisions", len(seen))
    art_cache = {}
    stages = ["A_raw_hist", "B_raw_kernel", "C_bias", "D_bias_spread", "E_system"]
    out = []
    stats = defaultdict(int)
    for (st, d, dec), r in seen.items():
        row = c.execute("select lineage_json, probabilities_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0'"
                        " and decision_at_utc=? and state='COMMITTED' limit 1", (st, d, dec)).fetchone()
        if row is None:
            stats["no_p0_row"] += 1; continue
        lin = json.loads(row[0]); served = json.loads(row[1])
        iv = json.loads(lin.get("bucket_intervals_c_json") or "null")
        if not iv:
            stats["no_intervals"] += 1; continue
        order, cc, w = A.centres(iv)
        stored = json.loads(lin.get("native_system_probabilities_json") or "{}")
        runs = json.loads(lin.get("run_init_utc_by_system_json") or "{}")
        arts = json.loads(lin.get("system_artifact_shas_json") or "{}")
        weights = json.loads(lin.get("system_weights_json") or "{}")
        T = r["truth"]
        ti = next(i for i, b in enumerate(order) if (iv[b][0] is None or iv[b][0] <= T) and (iv[b][1] is None or T < iv[b][1]))
        p_served = np.array([float(served[b]) for b in order]); p_served /= p_served.sum()
        m = int(np.argmax(p_served))
        rec = {"st": st, "day": d, "lh": r["local_hour"], "ti": ti, "m": m, "w": w, "c_m": cc[m], "served": p_served.tolist(),
               "systems": {}, "weights": weights}
        for s in stored:
            if s not in runs or s not in arts:
                stats["sys_missing_lineage"] += 1; continue
            vals = [float(v) for (v,) in c.execute(
                "select raw_tmax_c from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=?"
                " and native_run_init_utc=? and archive_query_at_utc<=? group by member_id", (s, st, d, runs[s], dec))]
            if len(vals) < 2:
                stats["sys_no_members:" + s] += 1; continue
            if arts[s] not in art_cache:
                a = c.execute("select bias_parameters_json, spread_parameters_json, reliability_parameters_json from calibration_native_ensemble_system_artifact where content_sha256=?",
                              (arts[s],)).fetchone()
                art_cache[arts[s]] = None if a is None else tuple(json.loads(x) for x in a)
            art = art_cache[arts[s]]
            if art is None:
                stats["sys_no_artifact"] += 1; continue
            bias_p, spread_p, rel_p = art
            ivs = {b: iv[b] for b in order}
            try:
                mean = sum(vals) / len(vals)
                bias = NP._resolved_bias_c(mean, bias_p)
                hist = np.zeros(len(order))
                for v in vals:
                    k = next(i for i, b in enumerate(order) if (iv[b][0] is None or iv[b][0] <= v) and (iv[b][1] is None or v < iv[b][1]))
                    hist[k] += 1
                vecs = {
                    "A_raw_hist": hist / hist.sum(),
                    "B_raw_kernel": NP._interval_probability(vals, ivs),
                    "C_bias": NP._interval_probability([v + bias for v in vals], ivs),
                    "D_bias_spread": NP._calibrated_system_probability(values=vals, intervals=ivs, bias_parameters=bias_p, spread_parameters=spread_p,
                                                                       reliability_parameters={**rel_p, "reliability_weight": 1.0}),
                    "E_system": NP._calibrated_system_probability(values=vals, intervals=ivs, bias_parameters=bias_p, spread_parameters=spread_p,
                                                                  reliability_parameters=rel_p),
                }
            except Exception as exc:  # noqa: BLE001
                stats["sys_rebuild_error:" + type(exc).__name__] += 1; continue
            vecs = {k: (np.array([float(v[b]) for b in order]) if isinstance(v, dict) else np.asarray(v)) for k, v in vecs.items()}
            st_vec = np.array([float(stored[s][b]) for b in order])
            tv = 0.5 * np.abs(vecs["E_system"] - st_vec).sum()
            stats["rebuild_tv_le_1e-6" if tv <= 1e-6 else ("rebuild_tv_le_1e-3" if tv <= 1e-3 else "rebuild_tv_gt_1e-3")] += 1
            rec["systems"][s] = {"vecs": {k: v.tolist() for k, v in vecs.items()}, "stored": st_vec.tolist(), "tv": tv,
                                 "raw_mean_off": (mean - cc[m]) / w, "bias_mean_off": (mean + bias - cc[m]) / w,
                                 "bias_c": bias, "spread": float(spread_p.get("spread_factor", 1.0)),
                                 "reliability": float(rel_p.get("reliability_weight", 1.0)), "n_members": len(vals)}
        if rec["systems"]:
            out.append(rec)
    print("rebuild", dict(stats))
    json.dump(out, gzip.open(HERE / "pipeline_rows.json.gz", "wt"))

    # ---------------- metrics ----------------
    def split(v, m):
        a, b = v[m + 1], v[m - 1]
        return (a + 1e-12) / (a + b + 2e-12)

    def metrics(get, label, rows):
        sh = [x for x in rows if abs(x["ti"] - x["m"]) == 1 and 0 < x["m"] < len(x["served"]) - 1]
        sv = [get(x) for x in sh]
        ok = [i for i, v in enumerate(sv) if v is not None]
        y = np.array([float(sh[i]["ti"] == sh[i]["m"] + 1) for i in ok])
        sp = np.array([split(sv[i], sh[i]["m"]) for i in ok])
        spc = np.clip(sp, 1e-3, 1 - 1e-3)
        ll = float(np.mean(-(y * np.log(spc) + (1 - y) * np.log(1 - spc))))
        allr = [x for x in rows if abs(x["ti"] - x["m"]) <= 1 and get(x) is not None]
        ym = np.array([float(x["ti"] == x["m"]) for x in allr])
        pm = np.array([get(x)[x["m"]] for x in allr])
        shm = np.array([(get(x)[x["m"] - 1] if x["m"] > 0 else 0) + (get(x)[x["m"] + 1] if x["m"] + 1 < len(x["served"]) else 0) for x in allr])
        print(f"  {label:38s} n_sh {len(ok):4d}  which-shoulder AUC {auc(sp, y):.3f}  split logloss {ll:.4f}  |split-0.5| {np.mean(np.abs(sp-0.5)):.3f}"
              f"  | mode-vs-shoulder AUC {auc(pm, ym):.3f}  p(mode) {pm.mean():.3f} realised {ym.mean():.3f}  p(shoulders) {shm.mean():.3f}")
        return {"which_shoulder_auc": auc(sp, y), "split_logloss": ll, "split_extremity": float(np.mean(np.abs(sp - 0.5))), "n": len(ok),
                "mode_vs_shoulder_auc": auc(pm, ym), "p_mode": float(pm.mean()), "p_shoulders": float(shm.mean()), "mode_freq": float(ym.mean())}

    res = {"per_system": {}, "pool": {}}
    systems = sorted({s for x in out for s in x["systems"]})
    for s in systems:
        print(s)
        rows = [x for x in out if s in x["systems"]]
        res["per_system"][s] = {}
        for k in stages:
            res["per_system"][s][k] = metrics(lambda x, k=k, s=s: np.array(x["systems"][s]["vecs"][k]), k, rows)
        for k in ("raw_mean_off", "bias_mean_off"):
            sh = [x for x in rows if abs(x["ti"] - x["m"]) == 1]
            a = auc([x["systems"][s][k] for x in sh], [float(x["ti"] == x["m"] + 1) for x in sh])
            print(f"  {k:38s} which-shoulder AUC {a:.3f}  (continuous member mean inside the mode bucket)")
            res["per_system"][s][k + "_auc"] = a
        print(f"  mean bias {np.mean([x['systems'][s]['bias_c'] for x in rows]):+.2f}C  spread {np.mean([x['systems'][s]['spread'] for x in rows]):.2f}"
              f"  reliability {np.mean([x['systems'][s]['reliability'] for x in rows]):.2f}  members {np.mean([x['systems'][s]['n_members'] for x in rows]):.0f}")
    print("POOL (P0's own weights over the systems rebuilt; renormalised)")

    def pool(x, k):
        ws = {s: float(x["weights"].get(s, 0.0)) for s in x["systems"]}
        tot = sum(ws.values())
        if tot <= 0:
            return None
        return sum(ws[s] * np.array(x["systems"][s]["vecs"][k]) for s in ws) / tot

    def eq_pool(x, k):
        return np.mean([np.array(x["systems"][s]["vecs"][k]) for s in x["systems"]], 0)

    for k in stages:
        res["pool"]["weighted_" + k] = metrics(lambda x, k=k: pool(x, k), "weighted pool of " + k, out)
    for k in ("B_raw_kernel", "E_system"):
        res["pool"]["equal_" + k] = metrics(lambda x, k=k: eq_pool(x, k), "equal pool of " + k, out)
    res["pool"]["served_P0"] = metrics(lambda x: np.array(x["served"]), "served P0 (after bridge/radiation/width/tail/ceiling)", out)
    sh = [x for x in out if abs(x["ti"] - x["m"]) == 1]
    for k in ("raw_mean_off", "bias_mean_off"):
        def pooled_off(x, k=k):
            ws = {s: float(x["weights"].get(s, 0.0)) for s in x["systems"]}
            tot = sum(ws.values()) or 1.0
            return sum(ws[s] * x["systems"][s][k] for s in ws) / tot
        a = auc([pooled_off(x) for x in sh], [float(x["ti"] == x["m"] + 1) for x in sh])
        print(f"  weighted {k:29s} which-shoulder AUC {a:.3f}")
        res["pool"]["weighted_" + k + "_auc"] = a
    res["rebuild_stats"] = dict(stats)
    json.dump(res, open(HERE / "readings_pipeline.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
