#!/usr/bin/env python3
"""Where the settled truth lands against each distribution's mean and mode: K1, the basket, the market.

User: 「分别计算最后的真值落位的偏向，是偏均值还是众数？分别计算k1、篮子和市场」

Two samples, one decision per station-day:
* P0 opening -- the first update inside strict 24 <= h < 60 on the tested days
  (08-27..09-23): the basket is today's-P0 replay, K1 its walk-forward shift
  (``centres.py``'s K1, refit per event day), the market the book in force.
* P1 first decision of the day, as served (the gap study's cache, 09-01..09-23):
  K1 is the same event day's P0-trained fold applied to the P1 vector, with the
  P1's own exact zeros (hard exclusions) kept at zero.

The three-state reading is production's ``mean_mode_skew_reading_v1`` (0.35
sigma); truth is the label table's standing revision.  Read-only; writes
results_truth_lean.json.

    python3 truth_lean.py cache.pkl
"""
from __future__ import annotations

import json
import pickle
import sys
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import centres as C  # noqa: E402
import walkforward as W  # noqa: E402
from bayes_basket_target.basket_kernel.support import mean_mode_skew_reading_v1  # noqa: E402

P1_FROM = "2026-09-01"


def features(row):
    positions, masses, mean_p, mean_m = C.centre_features(row)
    return {**{k: v - mean_p for k, v in positions.items() if k != "mean_p"}, **masses}


def shift(row, delta, keep_zeros):
    keys, base = row["keys"], np.asarray(row["p"])
    q = W.D.as_vec(W.D.shift_tail(dict(zip(keys, base)), delta_c=delta, keys=keys,
                                  centers=list(row["centers"]), open_edges=row["open_edges"]), keys)
    if keep_zeros:
        q = np.where(base == 0.0, 0.0, q)
        q = q / q.sum()
    return q


def reading(vec, row):
    """Mode, continuous mean, three-state, and where the truth sits."""

    keys, centers, hit = row["keys"], np.asarray(row["centers"]), row["hit"]
    ords = {k: i for i, k in enumerate(keys)}
    r = mean_mode_skew_reading_v1({k: float(v) for k, v in zip(keys, vec)}, ords)
    mode = int(r["mode_ordinal"])
    mean_c = float(np.dot(vec, centers))
    mean_bucket = int(round(r["mean_ordinal"]))
    lean = float(np.sign(r["gap_buckets"])) if r["gap_buckets"] else 0.0
    side = "mode" if hit == mode else ("mean_side" if np.sign(hit - mode) == lean else "other_side")
    return {"state": r["state"], "skewed": r["sign"] != 0, "gap": r["gap_buckets"],
            "mode_eq_mean_bucket": mode == mean_bucket, "side": side,
            "mean_hit": hit == mean_bucket, "mode_hit": hit == mode,
            "closer_to_mean": abs(row["truth_c"] - mean_c) < abs(row["truth_c"] - centers[mode]),
            "tie": abs(abs(row["truth_c"] - mean_c) - abs(row["truth_c"] - centers[mode])) < 1e-9,
            "abs_err_mean": abs(row["truth_c"] - mean_c), "abs_err_mode": abs(row["truth_c"] - centers[mode])}


def summarise(readings):
    n = len(readings)
    skew = [r for r in readings if r["skewed"]]
    split = [r for r in readings if not r["mode_eq_mean_bucket"]]
    lean = [r for r in readings if r["gap"]]
    def sides(rs):
        c = Counter(r["side"] for r in rs)
        return {k: [c[k], round(c[k] / len(rs), 3) if rs else None] for k in ("mean_side", "mode", "other_side")}
    return {
        "station_days": n,
        "skewed_share": round(len(skew) / n, 3),
        "mean_bucket_eq_mode_bucket_share": round(1 - len(split) / n, 3),
        "hit_mean_bucket": round(np.mean([r["mean_hit"] for r in readings]), 3),
        "hit_mode_bucket": round(np.mean([r["mode_hit"] for r in readings]), 3),
        "mae_mean_c": round(float(np.mean([r["abs_err_mean"] for r in readings])), 3),
        "mae_mode_c": round(float(np.mean([r["abs_err_mode"] for r in readings])), 3),
        "closer_to_mean_share_excl_ties": round(np.mean([r["closer_to_mean"] for r in readings if not r["tie"]]), 3),
        "truth_side_all_with_any_gap": sides(lean),
        "truth_side_skewed_days": sides(skew),
        "split_days": {"n": len(split), "hit_mean_bucket": round(np.mean([r["mean_hit"] for r in split]), 3) if split else None,
                       "hit_mode_bucket": round(np.mean([r["mode_hit"] for r in split]), 3) if split else None,
                       "truth_side": sides(split)},
    }


def main():
    rows, _ = W.build_rows(sys.argv[1])
    for r in rows:
        pos, mas, mp, mm = C.centre_features(r)
        r["mean_m"] = mm
        r["f"] = {"p": {**{k: v - mp for k, v in pos.items() if k != "mean_p"}, **mas}}
    days = sorted({r["date"] for r in rows})
    folds = {}
    for day in days:
        test = [r for r in rows if r["date"] == day]
        cutoff = min(W.instant(r["at"]) for r in test)
        train = [r for r in rows if r["date"] < day and r["known_at"] <= cutoff]
        if len({(r["station"], r["date"]) for r in train}) < W.MIN_TRAIN_STATION_DAYS:
            continue
        folds[day] = C.fit(train, C.CENTRES, "p", False)

    # P0 opening
    opening = {}
    for r in sorted((r for r in rows if r["date"] in folds), key=lambda r: r["at"]):
        opening.setdefault((r["station"], r["date"]), r)
    out = {"P0_opening": {"basket": [], "k1": [], "market": []}}
    for r in opening.values():
        delta = C.predict(folds[r["date"]], r, "p")
        out["P0_opening"]["basket"].append(reading(np.asarray(r["p"]), r))
        out["P0_opening"]["k1"].append(reading(shift(r, delta, False), r))
        out["P0_opening"]["market"].append(reading(np.asarray(r["m"]), r))

    # P1 first decision, as served
    served, _s, _t, _ts = pickle.loads(Path(sys.argv[1]).read_bytes())
    labels = {(r["station"], r["date"]): r for r in rows}  # formal truth already mapped per station-day
    import sqlite3  # noqa: PLC0415
    conn = sqlite3.connect(f"file:{W.ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    formal = W.D.load_labels(conn)
    conn.close()
    first_p1 = {}
    for d in sorted((d for d in served if d["stage"] == "P1" and d["date"] >= P1_FROM and d["date"] in folds),
                    key=lambda d: d["at"]):
        first_p1.setdefault((d["station"], d["date"]), d)
    out["P1_first"] = {"basket": [], "k1": [], "market": []}
    for d in first_p1.values():
        label = formal.get((d["station"], d["date"]))
        if label is None or label[0] not in d["keys"]:
            continue
        row = {**d, "hit": d["keys"].index(label[0]), "truth_c": float(label[1])}
        if np.asarray(row["p"])[row["hit"]] == 0.0:
            continue  # a truth the P1 had excluded is a truth-lane revision, not a lean
        f = features(row)
        delta = C.predict(folds[d["date"]], {"f": {"p": f}}, "p")
        out["P1_first"]["basket"].append(reading(np.asarray(row["p"]), row))
        out["P1_first"]["k1"].append(reading(shift(row, delta, True), row))
        out["P1_first"]["market"].append(reading(np.asarray(row["m"]), row))

    result = {sample: {name: summarise(rs) for name, rs in arms.items()} for sample, arms in out.items()}
    (HERE / "results_truth_lean.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
