#!/usr/bin/env python3
"""PROTOCOL.md: is the NWS point forecast's max the information the market holds on P0's disagreement days?

Decisions: today's-arithmetic P0 against the book in force
(``model_market_gap_decomposition_20260926``), 24-60h, the eleven US
stations.  Feature: the daytime Max for the event's local day in the latest
PFM issued at least ten minutes before the decision (``fetch_pfm.py``).
Correction: charter 201's k and cap, walk-forward, the width layer's own
shift; plus the fixed rules k = 0.5 and k = 1.  Read-only; writes results.json.

    python3 nws_pilot.py <decompose_cache.pkl>
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.center_width_calibration import apply_center_width_calibration  # noqa: E402
from bayes_basket_target.deterministic_feature_correction import CAP_C, MIN_K_EVENT_DAYS  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

VERDICT_STATIONS = ("KATL", "KAUS", "KBKF", "KDAL", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO")
PROXY_STATIONS = ("KHOU",)                  # PFM point is IAH, 35 km north: described, not judged
COASTAL = ("KSEA", "KLAX", "KSFO")
BINS_24_60 = (2, 3, 4)
DISAGREE_C = 0.5
AVAILABILITY_LAG = timedelta(minutes=10)
FIXED_K = (0.5, 1.0)
DRAWS = 2000
SEED = 20260926


def _instant(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def f_to_c(value):
    return (float(value) - 32.0) * 5.0 / 9.0


def load_blocks():
    by_station = defaultdict(list)
    with gzip.open(HERE / "pfm_station_blocks.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            by_station[row["station"]].append((_instant(row["issued_utc"]), row["max_f"], row["wmo_header"]))
    for rows in by_station.values():
        rows.sort(key=lambda r: r[0])
    return by_station


def attach_nws(decisions, blocks):
    """R for each decision: the latest product, issued AVAILABILITY_LAG before t, carrying the day's Max."""

    counts = defaultdict(int)
    for d in decisions:
        rows = blocks.get(d["station"], [])
        at = _instant(d["at"])
        idx = bisect.bisect_right([r[0] for r in rows], at - AVAILABILITY_LAG)
        d["nws"] = None
        for issued, maxima, header in reversed(rows[:idx]):
            if d["date"] in maxima:
                d["nws"] = f_to_c(maxima[d["date"]])
                d["nws_age_h"] = (at - issued).total_seconds() / 3600.0
                d["nws_header"] = header
                break
        counts["with_nws" if d["nws"] is not None else "without_nws"] += 1
    return dict(counts)


def attach_bias(decisions, labels_known):
    """Secondary R + b: the station's mean T - R over earlier knowable station-days (>= 10 event days)."""

    by_station = defaultdict(list)
    for d in decisions:
        by_station[d["station"]].append(d)
    for station, items in by_station.items():
        per_sd = defaultdict(int)
        for d in items:
            per_sd[d["date"]] += 1
        for d in items:
            at = _instant(d["at"])
            prior = [x for x in items if x["date"] < d["date"]
                     and (station, x["date"]) in labels_known and labels_known[(station, x["date"])] <= at]
            b = 0.0
            if len({x["date"] for x in prior}) >= MIN_K_EVENT_DAYS:
                w = np.array([1.0 / per_sd[x["date"]] for x in prior])
                b = float(np.sum(w * np.array([x["truth_c"] - x["nws"] for x in prior])) / np.sum(w))
            d["nws_debiased"] = d["nws"] + b
            d["nws_bias"] = b


def score(vec, keys, hit):
    floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, vec)})
    values = np.array([floored[k] for k in keys])
    y = np.zeros(len(values))
    y[hit] = 1.0
    return -math.log(max(float(values[hit]), 1e-12)), float(np.sum((values - y) ** 2))


def shifted_scores(d, delta):
    if abs(delta) <= 1e-12:
        return d["ls_p"], d["br_p"]
    shifted, _ = apply_center_width_calibration(
        dict(zip(d["keys"], d["p"])), delta_c=delta, width_lambda=1.0, ordered_keys=d["keys"],
        key_centers_c=list(d["centers"]), open_edge_bounds_c=d["open_edges"])
    vec = np.array([float(shifted[k_]) for k_ in d["keys"]])
    return score(vec / vec.sum(), d["keys"], d["hit"])


def corrections(decisions, labels_known, feature):
    """Charter 201's walk-forward k per station, and the fixed rules, for one feature column."""

    by_station = defaultdict(list)
    for d in decisions:
        by_station[d["station"]].append(d)
    for station, items in by_station.items():
        per_sd = defaultdict(int)
        for d in items:
            per_sd[d["date"]] += 1
        for d in items:
            at = _instant(d["at"])
            prior = [x for x in items if x["date"] < d["date"]
                     and (station, x["date"]) in labels_known and labels_known[(station, x["date"])] <= at]
            k = 0.0
            if len({x["date"] for x in prior}) >= MIN_K_EVENT_DAYS:
                num = sum((x["truth_c"] - x["mean_p"]) * (x[feature] - x["mean_p"]) / per_sd[x["date"]] for x in prior)
                den = sum((x[feature] - x["mean_p"]) ** 2 / per_sd[x["date"]] for x in prior)
                k = min(1.0, max(0.0, num / den)) if den > 0 else 0.0
            gap = d[feature] - d["mean_p"]
            out = {}
            delta = max(-CAP_C, min(CAP_C, k * gap))
            out["walk_forward"] = {"k": k, "delta": delta, **dict(zip(("ls", "br"), shifted_scores(d, delta)))}
            for fixed in FIXED_K:
                delta = max(-CAP_C, min(CAP_C, fixed * gap))
                out[f"k_{fixed}"] = {"k": fixed, "delta": delta, **dict(zip(("ls", "br"), shifted_scores(d, delta)))}
            d.setdefault("corrected", {})[feature] = out


def _weights(items):
    count = defaultdict(int)
    for d in items:
        count[(d["station"], d["date"])] += 1
    return np.array([1.0 / count[(d["station"], d["date"])] for d in items])


def _day_ci(items, values, weights, rng):
    days = sorted({d["date"] for d in items})
    index = {day: i for i, day in enumerate(days)}
    di = np.array([index[d["date"]] for d in items])
    num = np.bincount(di, weights=weights * values, minlength=len(days))
    den = np.bincount(di, weights=weights, minlength=len(days))
    mean = float(num.sum() / den.sum())
    draws = [num[p].sum() / den[p].sum() for p in (rng.integers(0, len(days), len(days)) for _ in range(DRAWS))]
    return [mean, float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]


def summarise(items, feature, rng):
    if len({d["date"] for d in items}) < 3:
        return {"decisions": len(items), "note": "fewer than 3 event days"}
    w = _weights(items)
    wm = lambda x: float(np.sum(w * x) / np.sum(w))  # noqa: E731
    R = np.array([d[feature] for d in items])
    c = np.array([d["mean_p"] for d in items])
    m = np.array([d["mean_m"] for d in items])
    T = np.array([d["truth_c"] for d in items])
    dd, e, g = R - c, T - c, m - c
    out = {
        "decisions": len(items), "station_days": len({(d["station"], d["date"]) for d in items}),
        "days": len({d["date"] for d in items}),
        "median_nws_age_h": float(np.median([d["nws_age_h"] for d in items])),
        "rmse_nws": math.sqrt(wm((R - T) ** 2)), "rmse_p0": math.sqrt(wm(e ** 2)), "rmse_market": math.sqrt(wm((m - T) ** 2)),
        "mean_error_nws": wm(T - R), "mean_error_p0": wm(e), "mean_error_market": wm(T - m),
        "corr_d_e": float(np.corrcoef(dd, e)[0, 1]) if np.std(dd) > 0 else None,
        "corr_d_g": float(np.corrcoef(dd, g)[0, 1]) if np.std(dd) > 0 else None,
        "k_in_sample": float(np.sum(w * dd * e) / np.sum(w * dd ** 2)) if np.sum(w * dd ** 2) > 0 else None,
        "rms_market_to_nws": math.sqrt(wm((m - R) ** 2)), "rms_market_to_p0": math.sqrt(wm(g ** 2)),
    }
    disagree = [i for i, x in enumerate(g) if abs(x) >= DISAGREE_C]
    if len({items[i]["date"] for i in disagree}) >= 3:
        sub = [items[i] for i in disagree]
        ws = _weights(sub)
        same = np.array([1.0 if np.sign(dd[i]) == np.sign(g[i]) and dd[i] != 0 else 0.0 for i in disagree])
        closer = np.array([1.0 if abs(R[i] - T[i]) < abs(c[i] - T[i]) else 0.0 for i in disagree])
        out["disagreement_decisions"] = len(disagree)
        out["sign_agrees_with_market"] = _day_ci(sub, same, ws, rng)
        out["nws_closer_than_p0"] = _day_ci(sub, closer, ws, rng)
    apart = [i for i in range(len(items)) if abs(m[i] - R[i]) >= DISAGREE_C]
    if len({items[i]["date"] for i in apart}) >= 3:
        sub = [items[i] for i in apart]
        market_closer = np.array([1.0 if abs(m[i] - T[i]) < abs(R[i] - T[i]) else 0.0 for i in apart])
        out["market_nws_apart_decisions"] = len(apart)
        out["market_closer_than_nws_when_apart"] = _day_ci(sub, market_closer, _weights(sub), rng)
    gap_before = np.array([d["ls_p"] - d["ls_m"] for d in items])
    centre = np.array([d["ls_centre"] for d in items])
    out["gap_to_market_before"] = wm(gap_before)
    out["centre_component"] = wm(centre)
    for rule in ("walk_forward",) + tuple(f"k_{k}" for k in FIXED_K):
        corrected = [d["corrected"][feature][rule] for d in items]
        d_ls = np.array([x["ls"] - d["ls_p"] for x, d in zip(corrected, items)])
        d_br = np.array([x["br"] - d["br_p"] for x, d in zip(corrected, items)])
        gap_after = np.array([x["ls"] - d["ls_m"] for x, d in zip(corrected, items)])
        entry = {
            "share_k_positive": wm(np.array([1.0 if x["k"] > 0 else 0.0 for x in corrected])),
            "d_logscore": _day_ci(items, d_ls, w, rng), "d_brier": _day_ci(items, d_br, w, rng),
            "gap_to_market_after": wm(gap_after),
        }
        entry["both_lower"] = entry["d_logscore"][2] < 0 and entry["d_brier"][2] < 0
        entry["both_higher"] = entry["d_logscore"][1] > 0 and entry["d_brier"][1] > 0
        entry["share_of_centre_recovered"] = (wm(gap_before) - wm(gap_after)) / wm(centre) if wm(centre) else None
        out[rule] = entry
    return out


def _line(name, s):
    if "note" in s:
        return f"{name}: {s}"
    sign, closer, apart = s.get("sign_agrees_with_market"), s.get("nws_closer_than_p0"), s.get("market_closer_than_nws_when_apart")
    text = (f"{name}: n={s['decisions']} sd={s['station_days']} days={s['days']} age~{s['median_nws_age_h']:.0f}h"
            f" | RMSE nws {s['rmse_nws']:.2f} p0 {s['rmse_p0']:.2f} mkt {s['rmse_market']:.2f}"
            f" | bias T-R {s['mean_error_nws']:+.2f} T-c {s['mean_error_p0']:+.2f} T-m {s['mean_error_market']:+.2f}"
            f" | corr(d,e) {s['corr_d_e']:+.2f} corr(d,g) {s['corr_d_g']:+.2f} k* {s['k_in_sample']:+.2f}"
            f" | m-R {s['rms_market_to_nws']:.2f} m-c {s['rms_market_to_p0']:.2f}")
    if sign:
        text += (f" | disagree n={s['disagreement_decisions']} sign=mkt {sign[0]:.0%} [{sign[1]:.0%},{sign[2]:.0%}]"
                 f" nws closer {closer[0]:.0%} [{closer[1]:.0%},{closer[2]:.0%}]")
    if apart:
        text += f" | apart n={s['market_nws_apart_decisions']} mkt closer {apart[0]:.0%} [{apart[1]:.0%},{apart[2]:.0%}]"
    text += f" | gap {s['gap_to_market_before']:+.3f} centre {s['centre_component']:+.3f}"
    for rule in ("walk_forward",) + tuple(f"k_{k}" for k in FIXED_K):
        r = s[rule]
        text += (f"\n    {rule}: k>0 {r['share_k_positive']:.0%} dLS {r['d_logscore'][0]:+.4f} [{r['d_logscore'][1]:+.4f},{r['d_logscore'][2]:+.4f}]"
                 f" dBr {r['d_brier'][0]:+.4f} [{r['d_brier'][1]:+.4f},{r['d_brier'][2]:+.4f}] both_lower={r['both_lower']}"
                 f" gap after {r['gap_to_market_after']:+.3f} recovered {r['share_of_centre_recovered']:+.0%}")
    return text


def main():
    _served, _ss, today, _ts = pickle.loads(Path(sys.argv[1]).read_bytes())
    stations = VERDICT_STATIONS + PROXY_STATIONS
    decisions = [d for d in today if d["bin"] in BINS_24_60 and d["station"] in stations]
    blocks = load_blocks()
    counts = attach_nws(decisions, blocks)
    decisions = [d for d in decisions if d["nws"] is not None]
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    labels_known = {(str(s), str(d)): _instant(k) for s, d, k in conn.execute(
        "SELECT station_id, event_date, MIN(known_at_utc) FROM basket_station_day_settlement_label GROUP BY station_id, event_date")}
    conn.close()
    attach_bias(decisions, labels_known)
    for feature in ("nws", "nws_debiased"):
        corrections(decisions, labels_known, feature)
    rng = np.random.default_rng(SEED)
    result = {"protocol": "PROTOCOL.md", "feature_counts": counts, "decisions": len(decisions),
              "event_days": [min(d["date"] for d in decisions), max(d["date"] for d in decisions)], "by_feature": {}}
    judged = [d for d in decisions if d["station"] in VERDICT_STATIONS]
    for feature in ("nws", "nws_debiased"):
        entry = {
            "all": summarise(judged, feature, rng),
            "coastal": summarise([d for d in judged if d["station"] in COASTAL], feature, rng),
            "other": summarise([d for d in judged if d["station"] not in COASTAL], feature, rng),
            "by_station": {st: summarise([d for d in decisions if d["station"] == st], feature, rng) for st in stations},
        }
        result["by_feature"][feature] = entry
        for group in ("all", "coastal", "other"):
            print(_line(f"{feature} {group}", entry[group]), flush=True)
        for st in stations:
            print(_line(f"{feature} {st}", entry["by_station"][st]), flush=True)
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
