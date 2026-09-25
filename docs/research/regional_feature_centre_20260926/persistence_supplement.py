#!/usr/bin/env python3
"""Supplementary (not in PROTOCOL.md): does P0's error persist over the last few known days?

The market beats P0 on disagreement days and the regional models do not
explain it.  A trader also sees the last few days' outcomes against the
forecasts; a regime such as a marine layer lasts several days.  So: shift each
24-60h P0 decision by k x (mean of P0's error over the last N station-days
whose truth was knowable at the decision), N in {1, 2, 3}, k fitted
walk-forward by charter 201's rule (clip(sum(e r) / sum(r^2), 0, 1), ten event
days).  Same decisions as regional_centre.py, all event days from 08-15.
Read-only; writes results_persistence.json.

    python3 persistence_supplement.py <decompose_cache.pkl>
"""
from __future__ import annotations

import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.center_width_calibration import apply_center_width_calibration  # noqa: E402
from bayes_basket_target.deterministic_feature_correction import CAP_C, MIN_K_EVENT_DAYS  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

COASTAL = ("NZWN", "RJTT", "KSEA", "FACT", "RCSS", "KLAX", "KSFO")
DRAWS = 2000
SEED = 20260926


def _instant(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def score(vec, keys, hit):
    floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, vec)})
    values = np.array([floored[k] for k in keys])
    y = np.zeros(len(values))
    y[hit] = 1.0
    return -math.log(max(float(values[hit]), 1e-12)), float(np.sum((values - y) ** 2))


def day_ci(items, values, rng):
    count = defaultdict(int)
    for d in items:
        count[(d["station"], d["date"])] += 1
    w = np.array([1.0 / count[(d["station"], d["date"])] for d in items])
    days = sorted({d["date"] for d in items})
    di = np.array([days.index(d["date"]) for d in items])
    num = np.bincount(di, weights=w * values, minlength=len(days))
    den = np.bincount(di, weights=w, minlength=len(days))
    draws = [num[p].sum() / den[p].sum() for p in (rng.integers(0, len(days), len(days)) for _ in range(DRAWS))]
    return [float(num.sum() / den.sum()), float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]


def main():
    _served, _ss, today, _ts = pickle.loads(Path(sys.argv[1]).read_bytes())
    items = [d for d in today if d["bin"] in (2, 3, 4)]
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    known = {(str(s), str(d)): _instant(k) for s, d, k in conn.execute(
        "SELECT station_id, event_date, MIN(known_at_utc) FROM basket_station_day_settlement_label GROUP BY station_id, event_date")}
    conn.close()
    # Each station-day's P0 error: truth minus the mean of its 24-60h P0 means.
    per_sd = defaultdict(list)
    truth = {}
    for d in items:
        per_sd[(d["station"], d["date"])].append(d["mean_p"])
        truth[(d["station"], d["date"])] = d["truth_c"]
    error = {key: truth[key] - float(np.mean(v)) for key, v in per_sd.items()}
    by_station = defaultdict(list)
    for (station, day), e in error.items():
        by_station[station].append((day, e))
    for station in by_station:
        by_station[station].sort()
    rng = np.random.default_rng(SEED)
    result = {"note": "supplementary, not pre-registered", "decisions": len(items)}
    for n in (1, 2, 3):
        # the recent-error feature per decision
        for d in items:
            at = _instant(d["at"])
            recent = [e for day, e in by_station[d["station"]]
                      if day < d["date"] and (d["station"], day) in known and known[(d["station"], day)] <= at]
            d[f"r{n}"] = float(np.mean(recent[-n:])) if len(recent) >= n else None
        # walk-forward k per station
        by_st = defaultdict(list)
        for d in items:
            by_st[d["station"]].append(d)
        for station, rows in by_st.items():
            for d in rows:
                at = _instant(d["at"])
                prior = [x for x in rows if x["date"] < d["date"] and x[f"r{n}"] is not None
                         and (station, x["date"]) in known and known[(station, x["date"])] <= at]
                k = 0.0
                if len({x["date"] for x in prior}) >= MIN_K_EVENT_DAYS:
                    num = sum((x["truth_c"] - x["mean_p"]) * x[f"r{n}"] for x in prior)
                    den = sum(x[f"r{n}"] ** 2 for x in prior)
                    k = min(1.0, max(0.0, num / den)) if den > 0 else 0.0
                delta = 0.0 if d[f"r{n}"] is None else max(-CAP_C, min(CAP_C, k * d[f"r{n}"]))
                if abs(delta) > 1e-12:
                    shifted, _ = apply_center_width_calibration(
                        dict(zip(d["keys"], d["p"])), delta_c=delta, width_lambda=1.0, ordered_keys=d["keys"],
                        key_centers_c=list(d["centers"]), open_edge_bounds_c=d["open_edges"])
                    vec = np.array([float(shifted[key]) for key in d["keys"]])
                    ls, br = score(vec / vec.sum(), d["keys"], d["hit"])
                else:
                    ls, br = d["ls_p"], d["br_p"]
                d[f"k{n}"], d[f"ls{n}"], d[f"br{n}"] = k, ls, br
        out = {}
        for group, sel in (("all", items), ("coastal", [d for d in items if d["station"] in COASTAL]),
                           ("other", [d for d in items if d["station"] not in COASTAL])):
            with_r = [d for d in sel if d[f"r{n}"] is not None]
            e = np.array([d["truth_c"] - d["mean_p"] for d in with_r])
            r = np.array([d[f"r{n}"] for d in with_r])
            g = np.array([d["mean_m"] - d["mean_p"] for d in with_r])
            out[group] = {
                "decisions": len(sel),
                "corr_recent_error_with_error": float(np.corrcoef(r, e)[0, 1]) if len(r) > 2 else None,
                "corr_recent_error_with_market_gap": float(np.corrcoef(r, g)[0, 1]) if len(r) > 2 else None,
                "share_k_positive": float(np.mean([d[f"k{n}"] > 0 for d in sel])),
                "d_logscore": day_ci(sel, np.array([d[f"ls{n}"] - d["ls_p"] for d in sel]), rng),
                "d_brier": day_ci(sel, np.array([d[f"br{n}"] - d["br_p"] for d in sel]), rng),
            }
            out[group]["both_lower"] = out[group]["d_logscore"][2] < 0 and out[group]["d_brier"][2] < 0
        result[f"last_{n}_days"] = out
        for group, s in out.items():
            print(f"N={n} {group}: n={s['decisions']} corr(r,e) {s['corr_recent_error_with_error']:+.3f} corr(r,g) {s['corr_recent_error_with_market_gap']:+.3f}"
                  f" k>0 {s['share_k_positive']:.0%} dLS {s['d_logscore'][0]:+.4f} [{s['d_logscore'][1]:+.4f},{s['d_logscore'][2]:+.4f}]"
                  f" dBr {s['d_brier'][0]:+.4f} [{s['d_brier'][1]:+.4f},{s['d_brier'][2]:+.4f}] both={s['both_lower']}", flush=True)
    (HERE / "results_persistence.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
