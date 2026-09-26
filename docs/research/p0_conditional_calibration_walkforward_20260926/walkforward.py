#!/usr/bin/env python3
"""PROTOCOL.md: conditional centre calibration of today's P0, strict walk-forward on closed days.

Rows are the gap study's cached today's-P0 updates (strict 24 <= h < 60) with
the WS book in force; the shift is that study's charter-190 tail transport and
the score its floored ladder score, so nothing here restates serving.  Truth is
the settlement label table's standing revision (the table ``knowable_labels``
reads); a station-day trains a fold only once that revision was committed
before the fold's first decision.  Read-only; writes results.json.

    python3 walkforward.py cache.pkl      # model_market_gap_decomposition_20260926's cache
"""
from __future__ import annotations

import gzip
import json
import math
import pickle
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs" / "research" / "model_market_gap_decomposition_20260926"))
import decompose as D  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

BAND = ROOT / "docs" / "research" / "band_layer_recheck_20260925" / "pass1"
BRIDGE = ROOT / "docs" / "research" / "consensus_bridge_recheck_20260925" / "pass1"
STRICT_BINS = (2, 3, 4)
MIN_TRAIN_STATION_DAYS = 300
RIDGE = 50.0
RIDGE_STATION = 20.0
TREND_HOURS = 12
ANOMALY_N, ANOMALY_MIN = 15, 5
BLOCK_DAYS = 3
DRAWS = 2000
SEED = 20260926
NWP = ("lead_h", "sd_p", "gap", "pool", "trend", "no_trend")
TRUTH = ("anomaly", "no_anomaly", "persist", "no_persist")
ARMS = {
    "A1": ((), False), "A1s": ((), True),
    "A2": (NWP, False), "A3": (NWP + TRUTH, False), "A3s": (NWP + TRUTH, True),
}


def instant(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def build_rows(cache_path):
    _served, _s, today, _t = pickle.loads(Path(cache_path).read_bytes())
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    labels = D.load_labels(conn)  # (station, day) -> (bucket, tmax_c, known_at, revision), standing revision
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall())
    conn.close()

    bridge = {}
    for path in BRIDGE.glob("*.jsonl.gz"):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                bridge[(r["station"], r["event_date"], r["at"])] = (float(r["gap"]), int(r["pool"]))
    band = {}
    for path in BAND.glob("*.jsonl.gz"):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                sd = json.loads(line)
                band[(sd["station"], sd["event_date"])] = sd
    known_truths = defaultdict(list)  # station -> [(event_date, known_at, tmax_c)]
    for (station, day), (_bucket, tmax, known, _rev) in labels.items():
        known_truths[station].append((day, instant(known), tmax))
    for series in known_truths.values():
        series.sort()

    rows, skipped = [], Counter()
    for d in today:
        if d["bin"] not in STRICT_BINS:
            continue
        key = (d["station"], d["date"])
        label = labels.get(key)
        if label is None or label[0] not in d["keys"]:
            skipped["no_formal_label_on_ladder"] += 1
            continue
        at = instant(d["at"])
        centers = np.asarray(d["centers"])
        mean_p = float(np.dot(d["p"], centers))
        sd_p = math.sqrt(max(float(np.dot(d["p"], (centers - mean_p) ** 2)), 0.0))
        gap, pool = bridge[(d["station"], d["date"], d["at"])]
        earlier = [u for u in band[key]["updates"] if instant(u["at"]) <= at - timedelta(hours=TREND_HOURS)]
        if earlier:
            vec = np.asarray(earlier[-1]["vec"], dtype=float)
            trend, no_trend = mean_p - float(np.dot(vec / vec.sum(), centers)), 0.0
        else:
            trend, no_trend = 0.0, 1.0
        past = [(day, tmax) for day, known, tmax in known_truths[d["station"]] if day < d["date"] and known <= at]
        recent = [tmax for _day, tmax in past[-ANOMALY_N:]]
        anomaly, no_anomaly = ((mean_p - float(np.mean(recent)), 0.0) if len(recent) >= ANOMALY_MIN else (0.0, 1.0))
        persist, no_persist = ((mean_p - past[-1][1], 0.0) if past else (0.0, 1.0))
        rows.append({
            "station": d["station"], "date": d["date"], "at": d["at"],
            "known_at": instant(label[2]), "keys": d["keys"], "centers": d["centers"],
            "open_edges": d["open_edges"], "p": d["p"], "m": d["m"],
            "hit": d["keys"].index(label[0]), "truth_c": float(label[1]), "mean_p": mean_p,
            "is_f": "°F" in d["keys"][0] or "°F" in d["keys"][-1],
            "x": {"lead_h": hours_to_settlement_if_known(d["date"], as_of_utc=d["at"],
                                                         settlement_timezone=zones.get(d["station"])),
                  "sd_p": sd_p, "gap": gap, "pool": float(pool), "trend": trend, "no_trend": no_trend,
                  "anomaly": anomaly, "no_anomaly": no_anomaly, "persist": persist, "no_persist": no_persist},
        })
    return rows, dict(skipped)


def fit(train, features, with_station, stations):
    """Station-day-weighted ridge on the residual, features standardised on the fold."""

    count = Counter((r["station"], r["date"]) for r in train)
    w = np.array([1.0 / count[(r["station"], r["date"])] for r in train])
    y = np.array([r["truth_c"] - r["mean_p"] for r in train])
    raw = np.array([[r["x"][f] for f in features] for r in train]).reshape(len(train), len(features))
    mu = (w[:, None] * raw).sum(0) / w.sum() if features else np.zeros(0)
    sd = np.sqrt((w[:, None] * (raw - mu) ** 2).sum(0) / w.sum()) if features else np.zeros(0)
    sd = np.where(sd > 1e-9, sd, 1.0)
    cols = [np.ones(len(train)), *((raw - mu) / sd).T]
    penalty = [0.0] + [RIDGE] * len(features)
    if with_station:
        for s in stations:
            cols.append(np.array([1.0 if r["station"] == s else 0.0 for r in train]))
            penalty.append(RIDGE_STATION)
    X = np.column_stack(cols)
    beta = np.linalg.solve(X.T @ (w[:, None] * X) + np.diag(penalty), X.T @ (w * y))
    return {"features": features, "mu": mu, "sd": sd, "beta": beta,
            "stations": list(stations) if with_station else []}


def predict(model, row):
    x = np.array([row["x"][f] for f in model["features"]])
    delta = model["beta"][0] + float(np.dot((x - model["mu"]) / model["sd"], model["beta"][1:1 + len(x)]))
    if model["stations"]:
        index = model["stations"].index(row["station"]) if row["station"] in model["stations"] else None
        if index is not None:
            delta += model["beta"][1 + len(x) + index]
    return float(delta)


def scored(row, delta):
    q = row["p"] if delta == 0.0 else D.as_vec(
        D.shift_tail(dict(zip(row["keys"], row["p"])), delta_c=delta, keys=row["keys"],
                     centers=list(row["centers"]), open_edges=row["open_edges"]), row["keys"])
    return D.score(q, row["keys"], row["hit"])


def per_day(items, value):
    count = Counter((r["station"], r["date"]) for r in items)
    days = sorted({r["date"] for r in items})
    index = {day: i for i, day in enumerate(days)}
    num, den = np.zeros(len(days)), np.zeros(len(days))
    for r in items:
        weight = 1.0 / count[(r["station"], r["date"])]
        num[index[r["date"]]] += weight * value(r)
        den[index[r["date"]]] += weight
    return days, num, den


def block_ci(days, num, den, rng):
    first = date.fromisoformat(days[0])
    block = np.array([(date.fromisoformat(day) - first).days // BLOCK_DAYS for day in days])
    labels = sorted(set(block))
    bn = np.array([num[block == b].sum() for b in labels])
    bd = np.array([den[block == b].sum() for b in labels])
    draws = []
    for _ in range(DRAWS):
        pick = rng.integers(0, len(labels), len(labels))
        draws.append(bn[pick].sum() / bd[pick].sum())
    return [float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]


def reading(items, arm, rng):
    out = {"station_days": len({(r["station"], r["date"]) for r in items}), "days": len({r["date"] for r in items})}
    for metric, j in (("logscore", 0), ("brier", 1)):
        days, num, den = per_day(items, lambda r: r["score"][arm][j] - r["score"]["A0"][j])
        out[f"d_{metric}"] = [float(num.sum() / den.sum()), *block_ci(days, num, den, rng)]
        days, num, den = per_day(items, lambda r: r["score"][arm][j] - r["market"][j])
        out[f"vs_market_{metric}"] = float(num.sum() / den.sum())
    return out


def main():
    rows, skipped = build_rows(sys.argv[1])
    stations = sorted({r["station"] for r in rows})
    days = sorted({r["date"] for r in rows})
    for r in rows:
        r["market"] = D.score(r["m"], r["keys"], r["hit"], floor=False)
        r["score"] = {"A0": scored(r, 0.0)}
        r["delta"] = {}
    tested_days, folds = [], {}
    for day in days:
        test = [r for r in rows if r["date"] == day]
        cutoff = min(instant(r["at"]) for r in test)
        train = [r for r in rows if r["date"] < day and r["known_at"] <= cutoff]
        n_train = len({(r["station"], r["date"]) for r in train})
        folds[day] = n_train
        if n_train < MIN_TRAIN_STATION_DAYS:
            continue
        tested_days.append(day)
        for arm, (features, with_station) in ARMS.items():
            model = fit(train, features, with_station, stations)
            for r in test:
                r["delta"][arm] = predict(model, r)
    tested = [r for r in rows if r["date"] in tested_days]
    for r in tested:
        for arm in ARMS:
            r["score"][arm] = scored(r, r["delta"][arm])

    # In-sample ceiling: one fit on every tested row, applied back to the same rows (not OOS).
    for arm, (features, with_station) in ARMS.items():
        model = fit(tested, features, with_station, stations)
        for r in tested:
            r["score"][f"{arm}_insample"] = scored(r, predict(model, r))
        if arm == "A3":
            names = ["intercept", *features]
            final_coefficients = {n: round(float(b), 4) for n, b in zip(names, model["beta"])}

    rng = np.random.default_rng(SEED)
    half = tested_days[len(tested_days) // 2]
    result = {
        "protocol": "PROTOCOL.md", "skipped": skipped, "rows": len(rows), "tested_rows": len(tested),
        "tested_days": [tested_days[0], tested_days[-1], len(tested_days)], "train_station_days_by_fold": folds,
        "a0": {"vs_market_logscore": reading(tested, "A0", rng)["vs_market_logscore"],
               "vs_market_brier": reading(tested, "A0", rng)["vs_market_brier"]},
        "a3_insample_standardised_coefficients": final_coefficients, "arms": {},
    }
    for arm in ARMS:
        deltas = np.array([r["delta"][arm] for r in tested])
        result["arms"][arm] = {
            "all": reading(tested, arm, rng),
            "F": reading([r for r in tested if r["is_f"]], arm, rng),
            "C": reading([r for r in tested if not r["is_f"]], arm, rng),
            "first_half": reading([r for r in tested if r["date"] < half], arm, rng),
            "second_half": reading([r for r in tested if r["date"] >= half], arm, rng),
            "insample_ceiling": reading(tested, f"{arm}_insample", rng),
            "abs_delta_c_quantiles": [round(float(q), 3) for q in np.percentile(np.abs(deltas), [50, 90, 99])],
        }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "train_station_days_by_fold"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
