#!/usr/bin/env python3
"""PROTOCOL.md: split the model's LogScore deficit to the market into centre, width and shape.

Reading 1 (served history): production's L4 population.  Reading 2 (today's
P0 arithmetic): the band study's pre-band P0 vectors joined to the WS book in
force at each update.  Ladder geometry, floor, width kernel and market
reference are production's; the centre move is charter 190's tail transport
(the patch the bridge study replayed), unbounded.  Read-only; writes
results.json.

    python3 decompose.py [cache.pkl]
"""
from __future__ import annotations

import gzip
import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import cross_consensus as CC  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target import valuation_store  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target import center_width_calibration as CW  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

BINS = ((0, 12), (12, 24), (24, 36), (36, 48), (48, 60), (60, math.inf))
LABELS = ["0-12h", "12-24h", "24-36h", "36-48h", "48-60h", "60h+"]
# "24-60h" is strict (24 <= h < 60); until the 2026-09-26 review it also took
# the 60h+ bin, so section 2 disagreed with sections 1 and 3 in the fourth decimal.
GROUPS = {"all": (0, 1, 2, 3, 4, 5), "0-24h": (0, 1), "24-60h": (2, 3, 4)}
DRAWS = 2000
SEED = 20260926
MAX_BOOK_AGE = timedelta(minutes=30)
BIAS_WINDOW_DAYS = 20
BIAS_MIN_STATION_DAYS = 5
BAND_PASS1 = ROOT / "docs" / "research" / "band_layer_recheck_20260925" / "pass1"


def _instant(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def shift_tail(probability, *, delta_c, keys, centers, open_edges):
    """Charter 190's tail transport as a pure shift (the bridge patch's shift_probability)."""

    lower, upper = CW._open_edges(open_edges, centers)
    if lower is None and upper is None:
        return CC.shift_probability(probability, delta_c=delta_c, ordered_keys=keys, key_centers_c=centers)
    edges_c = CW._bucket_edges_c(centers, lower, upper)
    rung_c = CW.bucket_rung_width_c(centers)
    adjacent = (float(probability.get(keys[1], 0.0)), float(probability.get(keys[-2], 0.0)))
    shifted = {key: 0.0 for key in keys}
    for key, mass in probability.items():
        weight = float(mass)
        if weight <= 0.0:
            continue
        index = keys.index(str(key))
        side = ("lower" if lower is not None and index == 0
                else "upper" if upper is not None and index == len(keys) - 1 else None)
        if side is not None:
            landing, _scale = CW._open_edge_tail_landing(
                weight, side=side, bound_c=lower if side == "lower" else upper,
                adjacent_weight=adjacent[0 if side == "lower" else 1], rung_c=rung_c,
                edges_c=edges_c, mean_c=0.0, lam=1.0, shift=float(delta_c))
            for position, share in enumerate(landing):
                shifted[keys[position]] += share
            continue
        position = centers[index] + float(delta_c)
        if position <= centers[0]:
            shifted[keys[0]] += weight
            continue
        if position >= centers[-1]:
            shifted[keys[-1]] += weight
            continue
        for low in range(len(centers) - 1):
            a, b = centers[low], centers[low + 1]
            if a <= position <= b:
                span = b - a
                share = (position - a) / span if span > 0 else 0.0
                shifted[keys[low]] += weight * (1.0 - share)
                shifted[keys[low + 1]] += weight * share
                break
    return shifted


def moments(vec, centers):
    mean = float(np.dot(vec, centers))
    return mean, math.sqrt(max(float(np.dot(vec, (centers - mean) ** 2)), 0.0))


def as_vec(mapping, keys):
    v = np.array([float(mapping.get(k, 0.0)) for k in keys])
    return v / v.sum()


def recentre(p, target, keys, centers, open_edges):
    total = 0.0
    q = p
    for _ in range(3):
        mean, _sd = moments(q, centers)
        if abs(target - mean) < 0.01:
            break
        total += target - mean
        q = as_vec(shift_tail(dict(zip(keys, p)), delta_c=total, keys=keys, centers=list(centers),
                              open_edges=open_edges), keys)
    return q


def rescale(q, target_sd, keys, centers, open_edges):
    _mean, sd = moments(q, centers)
    if sd <= 1e-9:
        return q
    lam = min(CW.MAX_WIDTH_LAMBDA, max(CW.MIN_WIDTH_LAMBDA, target_sd / sd))
    if abs(lam - 1.0) < 1e-9:
        return q
    out, _receipt = CW.apply_center_width_calibration(
        dict(zip(keys, q)), delta_c=0.0, width_lambda=lam, ordered_keys=keys,
        key_centers_c=list(centers), open_edge_bounds_c=open_edges)
    return as_vec(out, keys)


def score(vec, keys, hit, floor=True):
    if floor:
        floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, vec)})
        vec = np.array([floored[k] for k in keys])
    y = np.zeros(len(vec))
    y[hit] = 1.0
    return -math.log(max(float(vec[hit]), 1e-12)), float(np.sum((vec - y) ** 2))


def analyse_decision(d):
    keys, centers, open_edges = d["keys"], np.asarray(d["centers"]), d["open_edges"]
    p, m, hit = d["p"], d["m"], d["hit"]
    mean_m, sd_m = moments(m, centers)
    pc = recentre(p, mean_m, keys, centers, open_edges)
    pcw = rescale(pc, sd_m, keys, centers, open_edges)
    pw = rescale(p, sd_m, keys, centers, open_edges)
    pwc = recentre(pw, mean_m, keys, centers, open_edges)
    s = {name: score(v, keys, hit) for name, v in (("p", p), ("pc", pc), ("pcw", pcw), ("pw", pw), ("pwc", pwc))}
    s["m"] = score(m, keys, hit, floor=False)
    out = {}
    for j, metric in enumerate(("ls", "br")):
        final = 0.5 * (s["pcw"][j] + s["pwc"][j])
        out[f"{metric}_gap"] = s["p"][j] - s["m"][j]
        out[f"{metric}_centre"] = 0.5 * ((s["p"][j] - s["pc"][j]) + (s["pw"][j] - s["pwc"][j]))
        out[f"{metric}_width"] = 0.5 * ((s["p"][j] - s["pw"][j]) + (s["pc"][j] - s["pcw"][j]))
        out[f"{metric}_shape"] = final - s["m"][j]
        out[f"{metric}_p"] = s["p"][j]
        out[f"{metric}_m"] = s["m"][j]
    mean_p, sd_p = moments(p, centers)
    out.update({"mean_p": mean_p, "mean_m": mean_m, "sd_p": sd_p, "sd_m": sd_m,
                "mode_distance": abs(int(np.argmax(p)) - hit)})
    return out


def ladder_geometry(ladder):
    return {"keys": list(ladder["ordered"]), "centers": [float(c) for c in ladder["centers_c"]],
            "open_edges": ladder["open_edge_bounds_c"]}


def load_labels(conn):
    rows = conn.execute(
        "SELECT station_id, event_date, settled_bucket, settled_tmax_c, known_at_utc, truth_revision_no"
        " FROM basket_station_day_settlement_label").fetchall()
    best = {}
    for station, day, bucket, tmax, known, revision in rows:
        key = (str(station), str(day))
        if key not in best or int(revision or 0) > best[key][3]:
            best[key] = (str(bucket), float(tmax), str(known), int(revision or 0))
    return best


def served_history(conn, labels):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ladders, out, skipped = {}, [], defaultdict(int)
    for stage in ("P0", "P0-M", "P1"):
        rows, _meta = valuation_store.population(conn, stage_scope=stage, anchored=False, window_days=70)
        for row in rows:
            key = (row["station"], row["date"])
            if key not in ladders:
                ladder = oos._station_day_ladder(conn, station_id=row["station"], event_date=row["date"], as_of_utc=now)
                ladders[key] = None if ladder is None else ladder_geometry(ladder)
            geo = ladders[key]
            if geo is None or set(geo["keys"]) != set(row["m"]):
                skipped["ladder_mismatch"] += 1
                continue
            if key not in labels:
                skipped["no_truth"] += 1
                continue
            hours = hours_to_settlement_if_known(row["date"], as_of_utc=row["at"], settlement_timezone=row.get("settlement_timezone"))
            index = next((i for i, (lo, hi) in enumerate(BINS) if hours is not None and lo <= hours < hi), None)
            if index is None:
                skipped["no_bin"] += 1
                continue
            keys = geo["keys"]
            p = np.array([float(row["p"].get(k, 0.0) or 0.0) for k in keys])
            if p.sum() <= 0:
                skipped["p_empty"] += 1
                continue
            out.append({"reading": "served", "stage": stage, "station": row["station"], "date": row["date"],
                        "at": row["at"], "bin": index, **geo, "p": p / p.sum(), "m": as_vec(row["m"], keys),
                        "hit": keys.index(row["label"]), "truth_c": labels[key][1]})
    return out, dict(skipped)


def todays_p0(conn, labels, zones):
    out, skipped = [], defaultdict(int)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for path in sorted(BAND_PASS1.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            station_days = [json.loads(line) for line in handle]
        for sd in station_days:
            key = (sd["station"], sd["event_date"])
            ladder = oos._station_day_ladder(conn, station_id=sd["station"], event_date=sd["event_date"], as_of_utc=now)
            if ladder is None or list(ladder["ordered"]) != sd["keys"]:
                skipped["ladder_mismatch"] += len(sd["updates"])
                continue
            geo = ladder_geometry(ladder)
            for update in sd["updates"]:
                row = conn.execute(
                    "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
                    " INDEXED BY basket_official_ws_snapshot_asof_idx"
                    " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=?"
                    " ORDER BY observed_at DESC LIMIT 1",
                    (sd["station"], sd["event_date"], update["at"])).fetchone()
                if row is None or _instant(update["at"]) - _instant(row[0]) > MAX_BOOK_AGE:
                    skipped["no_fresh_book"] += 1
                    continue
                market = market_reference_v1(json.loads(row[1]))
                if market["state"] != "complete" or set(market["vector"]) != set(geo["keys"]):
                    skipped["book_incomplete_or_mismatch"] += 1
                    continue
                hours = hours_to_settlement_if_known(sd["event_date"], as_of_utc=update["at"], settlement_timezone=zones.get(sd["station"]))
                index = next((i for i, (lo, hi) in enumerate(BINS) if hours is not None and lo <= hours < hi), None)
                if index is None:
                    skipped["no_bin"] += 1
                    continue
                p = np.array(update["vec"], dtype=float)
                out.append({"reading": "today_p0", "stage": "P0", "station": sd["station"], "date": sd["event_date"],
                            "at": update["at"], "bin": index, **geo, "p": p / p.sum(),
                            "m": as_vec(market["vector"], geo["keys"]), "hit": sd["hit"], "truth_c": float(sd["actual"])})
    return out, dict(skipped)


def station_bias_arm(items, labels):
    """Walk-forward station residual bias (truth - model mean), applied as a pure shift."""

    by_station = defaultdict(list)
    for d in items:
        # Per station AND stage: a P1 mean has already read the day's reports.
        by_station[(d["station"], d["stage"])].append(d)
    for (station, _stage), rows in by_station.items():
        # per station-day residual, known when the label became knowable
        per_sd = defaultdict(list)
        for d in rows:
            per_sd[d["date"]].append(d["truth_c"] - d["mean_p"])
        residual = {day: float(np.mean(v)) for day, v in per_sd.items()}
        for d in rows:
            at = _instant(d["at"])
            floor_day = (datetime.fromisoformat(d["date"]) - timedelta(days=BIAS_WINDOW_DAYS)).date().isoformat()
            usable = [residual[day] for day in residual
                      if floor_day <= day < d["date"] and (station, day) in labels
                      and _instant(labels[(station, day)][2]) <= at]
            if len(usable) < BIAS_MIN_STATION_DAYS:
                d["bias_shift"] = 0.0
                d["ls_bias"], d["br_bias"] = d["ls_p"], d["br_p"]
                continue
            shift = float(np.mean(usable))
            d["bias_shift"] = shift
            q = as_vec(shift_tail(dict(zip(d["keys"], d["p"])), delta_c=shift, keys=d["keys"],
                                  centers=list(d["centers"]), open_edges=d["open_edges"]), d["keys"])
            d["ls_bias"], d["br_bias"] = score(q, d["keys"], d["hit"])


def weighted_ci(items, value_of, rng):
    count = defaultdict(int)
    for d in items:
        count[(d["station"], d["date"])] += 1
    days = sorted({d["date"] for d in items})
    di = {day: i for i, day in enumerate(days)}
    num = np.zeros(len(days))
    den = np.zeros(len(days))
    for d in items:
        w = 1.0 / count[(d["station"], d["date"])]
        num[di[d["date"]]] += w * value_of(d)
        den[di[d["date"]]] += w
    mean = float(num.sum() / den.sum())
    draws = []
    for _ in range(DRAWS):
        pick = rng.integers(0, len(days), len(days))
        draws.append(num[pick].sum() / den[pick].sum())
    return [mean, float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]


def summarise(items, rng):
    if not items:
        return None
    count = defaultdict(int)
    for d in items:
        count[(d["station"], d["date"])] += 1
    out = {"decisions": len(items), "station_days": len(count), "days": len({d["date"] for d in items}),
           "stages": {s: sum(1 for d in items if d["stage"] == s) for s in ("P0", "P0-M", "P1")}}
    for metric in ("ls", "br"):
        for part in ("gap", "centre", "width", "shape"):
            out[f"{metric}_{part}"] = weighted_ci(items, lambda d, k=f"{metric}_{part}": d[k], rng)
        out[f"{metric}_bias_vs_p"] = weighted_ci(items, lambda d, m=metric: d[f"{m}_bias"] - d[f"{m}_p"], rng)
        out[f"{metric}_bias_vs_market"] = weighted_ci(items, lambda d, m=metric: d[f"{m}_bias"] - d[f"{m}_m"], rng)
    out["mean_p_minus_truth"] = weighted_ci(items, lambda d: d["mean_p"] - d["truth_c"], rng)
    out["mean_m_minus_truth"] = weighted_ci(items, lambda d: d["mean_m"] - d["truth_c"], rng)
    out["rmse_p"] = math.sqrt(weighted_ci(items, lambda d: (d["mean_p"] - d["truth_c"]) ** 2, rng)[0])
    out["rmse_m"] = math.sqrt(weighted_ci(items, lambda d: (d["mean_m"] - d["truth_c"]) ** 2, rng)[0])
    out["abs_mean_gap"] = weighted_ci(items, lambda d: abs(d["mean_p"] - d["mean_m"]), rng)[0]
    out["sd_p"] = weighted_ci(items, lambda d: d["sd_p"], rng)[0]
    out["sd_m"] = weighted_ci(items, lambda d: d["sd_m"], rng)[0]
    # Station-fixed share of the model-minus-market mean gap.
    w = np.array([1.0 / count[(d["station"], d["date"])] for d in items])
    gap = np.array([d["mean_p"] - d["mean_m"] for d in items])
    stations = sorted({d["station"] for d in items})
    si = np.array([stations.index(d["station"]) for d in items])
    station_mean = np.bincount(si, weights=w * gap, minlength=len(stations)) / np.bincount(si, weights=w, minlength=len(stations))
    grand = float((w * gap).sum() / w.sum())
    total_var = float((w * (gap - grand) ** 2).sum() / w.sum())
    between = float((w * (station_mean[si] - grand) ** 2).sum() / w.sum())
    out["mean_gap_between_station_share"] = between / total_var if total_var > 0 else None
    shares = {}
    total_gap = float((w * np.array([d["ls_gap"] for d in items])).sum())
    for label, test in (("0", lambda k: k == 0), ("1", lambda k: k == 1), ("2", lambda k: k == 2), ("3+", lambda k: k >= 3)):
        sel = np.array([test(d["mode_distance"]) for d in items])
        shares[label] = {"decision_share": float(w[sel].sum() / w.sum()),
                         "gap_share": float((w[sel] * np.array([d["ls_gap"] for d in items])[sel]).sum() / total_gap) if total_gap else None}
    out["gap_by_truth_distance_from_model_mode"] = shares
    return out


def main():
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if cache is not None and cache.exists():
        served, served_skipped, today, today_skipped = pickle.loads(cache.read_bytes())
    else:
        conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
        conn.execute("PRAGMA query_only=ON")
        labels = load_labels(conn)
        zones = dict(conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall())
        served, served_skipped = served_history(conn, labels)
        print("served", len(served), served_skipped, flush=True)
        today, today_skipped = todays_p0(conn, labels, zones)
        print("today", len(today), today_skipped, flush=True)
        conn.close()
        for index, items in enumerate((served, today)):
            kept = []
            for d in items:
                try:
                    d.update(analyse_decision(d))
                except (ValueError, KeyError) as exc:
                    (served_skipped if index == 0 else today_skipped)[f"analyse:{type(exc).__name__}"] = (
                        (served_skipped if index == 0 else today_skipped).get(f"analyse:{type(exc).__name__}", 0) + 1)
                    continue
                kept.append(d)
            items[:] = kept
            station_bias_arm(items, labels)
        if cache is not None:
            cache.write_bytes(pickle.dumps((served, served_skipped, today, today_skipped)))
    rng = np.random.default_rng(SEED)
    result = {"protocol": "PROTOCOL.md", "served_skipped": served_skipped, "today_skipped": today_skipped}
    for name, items in (("served", served), ("today_p0", today)):
        result[name] = {}
        for label, index in zip(LABELS, range(len(BINS))):
            result[name][label] = summarise([d for d in items if d["bin"] == index], rng)
        for group, bins in GROUPS.items():
            result[name][group] = summarise([d for d in items if d["bin"] in bins], rng)
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for name in ("served", "today_p0"):
        print("=====", name)
        for label, s in result[name].items():
            if not s:
                continue
            print(f"{label}: n={s['decisions']} sd={s['station_days']} days={s['days']} | LS gap {s['ls_gap'][0]:+.3f}"
                  f" = centre {s['ls_centre'][0]:+.3f} [{s['ls_centre'][1]:+.3f},{s['ls_centre'][2]:+.3f}]"
                  f" + width {s['ls_width'][0]:+.3f} [{s['ls_width'][1]:+.3f},{s['ls_width'][2]:+.3f}]"
                  f" + shape {s['ls_shape'][0]:+.3f} [{s['ls_shape'][1]:+.3f},{s['ls_shape'][2]:+.3f}]"
                  f" | bias arm vs p LS {s['ls_bias_vs_p'][0]:+.4f} [{s['ls_bias_vs_p'][1]:+.4f},{s['ls_bias_vs_p'][2]:+.4f}]"
                  f" Br {s['br_bias_vs_p'][0]:+.4f} [{s['br_bias_vs_p'][1]:+.4f},{s['br_bias_vs_p'][2]:+.4f}]"
                  f" | mean-truth p {s['mean_p_minus_truth'][0]:+.2f} m {s['mean_m_minus_truth'][0]:+.2f} rmse p {s['rmse_p']:.2f} m {s['rmse_m']:.2f}"
                  f" |gap| {s['abs_mean_gap']:.2f} sd p {s['sd_p']:.2f} m {s['sd_m']:.2f} between-station {s['mean_gap_between_station_share']:.2f}")


if __name__ == "__main__":
    main()
