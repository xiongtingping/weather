#!/usr/bin/env python3
"""PROTOCOL.md: do regional models carry the market's local centre information on P0's disagreement days?

Decisions: today's-arithmetic P0 against the book in force
(``model_market_gap_decomposition_20260926``), 24-60h, event days from
2026-08-23.  Features: production's ``feature_at`` on the lane's rows,
debiased by production's ``resolve_correction`` on the replay curve fitted
before each event day.  Correction: charter 201's k and cap, walk-forward, the
width layer's own shift.  Read-only; writes results.json.

    python3 regional_centre.py <decompose_cache.pkl>
"""
from __future__ import annotations

import importlib.util
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
from bayes_basket_target import deterministic_forecast_family as DFF  # noqa: E402
from bayes_basket_target import regional_point_debias as RPD  # noqa: E402
from bayes_basket_target.center_width_calibration import apply_center_width_calibration  # noqa: E402
from bayes_basket_target.deterministic_feature_correction import CAP_C, MIN_K_EVENT_DAYS  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.regional_point_lane import runs_for  # noqa: E402

COASTAL = ("NZWN", "RJTT", "KSEA", "FACT", "RCSS", "KLAX", "KSFO")
FIRST_DAY = "2026-08-23"
BINS_24_60 = (2, 3, 4)
DISAGREE_C = 0.5
DRAWS = 2000
SEED = 20260926


def _instant(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def load_decisions(cache_path):
    spec = importlib.util.spec_from_file_location(
        "decompose", ROOT / "docs" / "research" / "model_market_gap_decomposition_20260926" / "decompose.py")
    dc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dc)
    _served, _ss, today, _ts = pickle.loads(Path(cache_path).read_bytes())
    return [d for d in today if d["bin"] in BINS_24_60 and d["date"] >= FIRST_DAY], dc


def attach_features(conn, decisions):
    zones = dict(conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall())
    by_sd = defaultdict(list)
    for d in decisions:
        by_sd[(d["station"], d["date"])].append(d)
    curves = {}
    counts = defaultdict(int)
    for (station, day), items in sorted(by_sd.items()):
        latest = max(d["at"] for d in items)
        rows = runs_for(conn, station_id=station, event_date=day, as_of_utc=latest)
        if day not in curves:
            curves[day] = RPD.replay_curve(day)
        for d in items:
            d["regional"] = {}
            if not rows:
                continue
            features = DFF.feature_at(conn, station_id=station, event_date=day, as_of_utc=d["at"],
                                      settlement_timezone=zones[station], rows=rows)
            for system, feature in features.items():
                correction = RPD.resolve_correction(
                    curves[day], system_id=system, station_id=station, run_init_utc=feature["run_init_utc"],
                    lead_hours_to_local_day_start=feature["lead_hours_to_local_day_start"],
                    forecast_level_c=feature["day_max_c"])
                if correction is None:
                    counts[f"{system}:no_debias"] += 1
                    continue
                d["regional"][system] = float(feature["day_max_c"]) + float(correction["bias_c"])
                counts[f"{system}:read"] += 1
    return dict(counts)


def score(vec, keys, hit):
    floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, vec)})
    values = np.array([floored[k] for k in keys])
    y = np.zeros(len(values))
    y[hit] = 1.0
    return -math.log(max(float(values[hit]), 1e-12)), float(np.sum((values - y) ** 2))


def walk_forward_correction(decisions, labels_known):
    """Charter 201's k per station x source, from earlier cells whose truth was knowable."""

    cells = defaultdict(list)          # (station, system) -> decisions carrying it
    for d in decisions:
        for system in d.get("regional", {}):
            cells[(d["station"], system)].append(d)
    for (station, system), items in cells.items():
        per_sd = defaultdict(int)
        for d in items:
            per_sd[d["date"]] += 1
        for d in items:
            at = _instant(d["at"])
            prior = [x for x in items if x["date"] < d["date"]
                     and (station, x["date"]) in labels_known and labels_known[(station, x["date"])] <= at]
            k = 0.0
            if len({x["date"] for x in prior}) >= MIN_K_EVENT_DAYS:
                num = sum((x["truth_c"] - x["mean_p"]) * (x["regional"][system] - x["mean_p"]) / per_sd[x["date"]] for x in prior)
                den = sum((x["regional"][system] - x["mean_p"]) ** 2 / per_sd[x["date"]] for x in prior)
                k = min(1.0, max(0.0, num / den)) if den > 0 else 0.0
            delta = max(-CAP_C, min(CAP_C, k * (d["regional"][system] - d["mean_p"])))
            if abs(delta) > 1e-12:
                shifted, _ = apply_center_width_calibration(
                    dict(zip(d["keys"], d["p"])), delta_c=delta, width_lambda=1.0, ordered_keys=d["keys"],
                    key_centers_c=list(d["centers"]), open_edge_bounds_c=d["open_edges"])
                vec = np.array([float(shifted[k_]) for k_ in d["keys"]])
                ls, br = score(vec / vec.sum(), d["keys"], d["hit"])
            else:
                ls, br = d["ls_p"], d["br_p"]
            d.setdefault("corrected", {})[system] = {"k": k, "delta": delta, "ls": ls, "br": br}


def _weights(items):
    count = defaultdict(int)
    for d in items:
        count[(d["station"], d["date"])] += 1
    return np.array([1.0 / count[(d["station"], d["date"])] for d in items])


def _day_ci(items, values, weights, rng):
    days = sorted({d["date"] for d in items})
    di = np.array([days.index(d["date"]) for d in items])
    num = np.bincount(di, weights=weights * values, minlength=len(days))
    den = np.bincount(di, weights=weights, minlength=len(days))
    mean = float(num.sum() / den.sum())
    draws = [num[p].sum() / den[p].sum() for p in (rng.integers(0, len(days), len(days)) for _ in range(DRAWS))]
    return [mean, float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]


def summarise(items, system, rng):
    if len({d["date"] for d in items}) < 3:
        return {"decisions": len(items), "note": "fewer than 3 event days"}
    w = _weights(items)
    R = np.array([d["regional"][system] for d in items])
    c = np.array([d["mean_p"] for d in items])
    m = np.array([d["mean_m"] for d in items])
    T = np.array([d["truth_c"] for d in items])
    dd, e, g = R - c, T - c, m - c
    wm = lambda x: float(np.sum(w * x) / np.sum(w))  # noqa: E731
    out = {
        "decisions": len(items), "station_days": len({(d["station"], d["date"]) for d in items}),
        "days": len({d["date"] for d in items}),
        "rmse_regional": math.sqrt(wm((R - T) ** 2)), "rmse_p0": math.sqrt(wm(e ** 2)), "rmse_market": math.sqrt(wm((m - T) ** 2)),
        "corr_d_e": float(np.corrcoef(dd, e)[0, 1]) if np.std(dd) > 0 else None,
        "corr_d_g": float(np.corrcoef(dd, g)[0, 1]) if np.std(dd) > 0 else None,
        "k_in_sample": float(np.sum(w * dd * e) / np.sum(w * dd ** 2)) if np.sum(w * dd ** 2) > 0 else None,
    }
    disagree = [i for i, x in enumerate(g) if abs(x) >= DISAGREE_C]
    if len({items[i]["date"] for i in disagree}) >= 3:
        sub = [items[i] for i in disagree]
        ws = _weights(sub)
        same = np.array([1.0 if np.sign(dd[i]) == np.sign(g[i]) and dd[i] != 0 else 0.0 for i in disagree])
        closer = np.array([1.0 if abs(R[i] - T[i]) < abs(c[i] - T[i]) else 0.0 for i in disagree])
        out["disagreement_decisions"] = len(disagree)
        out["sign_agrees_with_market"] = _day_ci(sub, same, ws, rng)
        out["regional_closer_than_p0"] = _day_ci(sub, closer, ws, rng)
    corrected = [d["corrected"][system] for d in items]
    k_active = np.array([1.0 if x["k"] > 0 else 0.0 for x in corrected])
    out["share_with_k_positive"] = wm(k_active)
    out["mean_k_when_active"] = float(np.mean([x["k"] for x in corrected if x["k"] > 0])) if k_active.any() else None
    d_ls = np.array([x["ls"] - d["ls_p"] for x, d in zip(corrected, items)])
    d_br = np.array([x["br"] - d["br_p"] for x, d in zip(corrected, items)])
    out["corrected_vs_p0_logscore"] = _day_ci(items, d_ls, w, rng)
    out["corrected_vs_p0_brier"] = _day_ci(items, d_br, w, rng)
    out["both_lower"] = out["corrected_vs_p0_logscore"][2] < 0 and out["corrected_vs_p0_brier"][2] < 0
    gap_before = np.array([d["ls_p"] - d["ls_m"] for d in items])
    gap_after = np.array([x["ls"] - d["ls_m"] for x, d in zip(corrected, items)])
    centre = np.array([d["ls_centre"] for d in items])
    out["gap_to_market_before"] = wm(gap_before)
    out["gap_to_market_after"] = wm(gap_after)
    out["centre_component"] = wm(centre)
    out["share_of_centre_recovered"] = (wm(gap_before) - wm(gap_after)) / wm(centre) if wm(centre) else None
    return out


def main():
    decisions, _dc = load_decisions(sys.argv[1])
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    counts = attach_features(conn, decisions)
    labels_known = {(str(s), str(d)): _instant(k) for s, d, k in conn.execute(
        "SELECT station_id, event_date, MIN(known_at_utc) FROM basket_station_day_settlement_label GROUP BY station_id, event_date")}
    conn.close()
    walk_forward_correction(decisions, labels_known)
    rng = np.random.default_rng(SEED)
    systems = sorted({s for d in decisions for s in d.get("regional", {})})
    result = {"protocol": "PROTOCOL.md", "decisions": len(decisions), "feature_counts": counts,
              "calibrated_systems": sorted(RPD.CALIBRATED_SYSTEMS), "by_system": {}}
    for system in systems:
        carrying = [d for d in decisions if system in d.get("regional", {})]
        entry = {group: summarise([d for d in carrying if (d["station"] in COASTAL) == (group == "coastal")], system, rng)
                 for group in ("coastal", "other")}
        entry["all"] = summarise(carrying, system, rng)
        entry["coastal_by_station"] = {st: summarise([d for d in carrying if d["station"] == st], system, rng)
                                       for st in COASTAL if any(d["station"] == st for d in carrying)}
        result["by_system"][system] = entry
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("decisions", len(decisions), counts)
    for system, entry in result["by_system"].items():
        for group in ("coastal", "other", "all"):
            s = entry[group]
            if "note" in s:
                print(f"{system} {group}: {s}")
                continue
            sign = s.get("sign_agrees_with_market")
            closer = s.get("regional_closer_than_p0")
            print(f"{system} {group}: n={s['decisions']} sd={s['station_days']} days={s['days']} | RMSE reg {s['rmse_regional']:.2f} p0 {s['rmse_p0']:.2f} mkt {s['rmse_market']:.2f}"
                  f" | corr(d,e) {s['corr_d_e']:+.2f} corr(d,g) {s['corr_d_g']:+.2f} k* {s['k_in_sample']:+.2f}"
                  + (f" | disagree n={s['disagreement_decisions']} sign=mkt {sign[0]:.0%} [{sign[1]:.0%},{sign[2]:.0%}] closer {closer[0]:.0%}" if sign else "")
                  + f" | k>0 {s['share_with_k_positive']:.0%} | dLS {s['corrected_vs_p0_logscore'][0]:+.4f} [{s['corrected_vs_p0_logscore'][1]:+.4f},{s['corrected_vs_p0_logscore'][2]:+.4f}]"
                  f" dBr {s['corrected_vs_p0_brier'][0]:+.4f} [{s['corrected_vs_p0_brier'][1]:+.4f},{s['corrected_vs_p0_brier'][2]:+.4f}] both={s['both_lower']}"
                  f" | gap to mkt {s['gap_to_market_before']:+.3f} -> {s['gap_to_market_after']:+.3f} (centre {s['centre_component']:+.3f})")


if __name__ == "__main__":
    main()
