#!/usr/bin/env python3
"""Searching for "model right, market wrong" with features known before the
outcome, validated forward in time.

熊先生 2026-09-26：「模型在哪个阶段对真值的概率比市场要准？」续「我要找到模型对，
市场错的时候」.  Population: production's L4 population (measure.py), every
decision, each station-day x stage counted once.  Target d = log p_model(truth)
- log p_market(truth) (the model's arithmetic as served: its vector on the
market's contract, renormalised, charter 224 floor); d > 0 means the model put
more probability on what happened.  Trade target: the bucket where the model
most exceeds the market (argmax p - m), bought at the decision snapshot's ask
(market_reference_v1 mid + half spread on the raw scale, fee 0).

1. Univariate: every feature cut at its training quintiles (first 60% of event
   days), read on the training days and on the later 40%.
2. Walk-forward gradient boosting (sklearn HistGradientBoostingRegressor,
   depth 3): refit every 3 event days on days ending two days before the test
   block (labels of the last two days are not knowable yet), predict the block.
3. The one positive-looking cell (P0-M, the model's favourite is the market's
   #2 bucket) against its control and split in halves.

Read-only; writes model_right_market_wrong_results.json.

    python3 model_right_market_wrong.py [population_cache.pkl [asks_cache.pkl]]
"""
from __future__ import annotations

import json
import math
import pickle
import re
import sqlite3
import sys
import warnings
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure import FIRST, LAST, ROOT, load, order  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
DRAWS = 1000
SEED = 20260926
FEATURES = ("hours", "unit_F", "n_buckets", "m_max", "m_ent", "m_margin", "p_max", "p_ent", "mean_gap", "tv",
            "agree", "m_on_ptop", "p_on_mtop", "edge_max", "ratio_max", "st_trail", "st_trail_n", "version",
            "m_mean_pos", "is_p0", "is_p0m", "is_p1")
SCAN = ("hours", "m_max", "m_ent", "m_margin", "p_max", "p_ent", "mean_gap", "tv", "m_on_ptop", "p_on_mtop",
        "edge_max", "ratio_max", "st_trail", "version", "n_buckets", "m_mean_pos")


def load_asks():
    asks = {}
    as_of = LAST
    while as_of > FIRST:
        span = min(7, (as_of - FIRST).days)
        floor = as_of - timedelta(days=span)
        conn = sqlite3.connect(DB, uri=True, timeout=60)
        conn.execute("PRAGMA query_only=ON")
        for station, day, at, book in conn.execute(
                "SELECT f.station_id, f.event_date, f.decision_at_utc, s.buckets_json"
                " FROM basket_policy_training_fact f"
                " JOIN basket_official_ws_snapshot s ON s.snapshot_id = f.ws_snapshot_id"
                " WHERE f.event_date > ? AND f.event_date <= ?", (floor.isoformat(), as_of.isoformat())):
            ref = market_reference_v1(json.loads(book or "[]"))
            if ref.get("state") == "complete":
                asks[(str(station), str(day), str(at))] = {
                    b: (ref["vector"][b] + ref["half_spread"].get(b, 0.0)) * ref["mid_sum"] for b in ref["vector"]}
        conn.close()
        as_of = floor
    return asks


def entropy(v):
    v = v[v > 0]
    return float(-(v * np.log(v)).sum())


def build(rows, asks):
    recs = []
    for r in rows:
        keys = sorted(r["m"], key=order)
        n = len(keys)
        idx = np.arange(n)
        m = np.array([float(r["m"][k]) for k in keys])
        m = m / m.sum()
        p = np.array([float(r["p"].get(k, 0.0) or 0.0) for k in keys])
        p = p / p.sum()
        floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, p)})
        pf = np.array([floored[k] for k in keys])
        y = keys.index(r["label"])
        book = asks.get((r["station"], r["date"], r["at"]))
        ranked = np.sort(m)[::-1]
        fav = int(np.argmax(pf - m))
        second = int(np.argsort(-m, kind="stable")[1])
        hours = hours_to_settlement_if_known(r["date"], as_of_utc=r["at"],
                                             settlement_timezone=r.get("settlement_timezone"))
        version = re.search(r"_v(\d+)$", str(r.get("posterior_version") or ""))
        recs.append({
            "stage": r["stage"], "station": r["station"], "date": r["date"], "at": r["at"],
            "known": r["label_known_at"], "w": r["weight"],
            "d": math.log(pf[y]) - math.log(max(m[y], 1e-12)),
            "model_top_right": int(np.argmax(p) == y), "market_top_right": int(np.argmax(m) == y),
            "hours": np.nan if hours is None else hours, "unit_F": int("°F" in keys[0]), "n_buckets": n,
            "m_max": float(m.max()), "m_ent": entropy(m), "m_margin": float(ranked[0] - ranked[1]),
            "p_max": float(p.max()), "p_ent": entropy(p), "mean_gap": float((p * idx).sum() - (m * idx).sum()),
            "tv": float(0.5 * np.abs(p - m).sum()), "agree": int(np.argmax(p) == np.argmax(m)),
            "m_on_ptop": float(m[int(np.argmax(p))]), "p_on_mtop": float(p[int(np.argmax(m))]),
            "edge_max": float((pf - m).max()), "ratio_max": float(np.log(pf[fav] / max(m[fav], 1e-6))),
            "version": int(version.group(1)) if version else -1,
            "m_mean_pos": float((m * idx).sum() / max(n - 1, 1)),
            "is_p0": int(r["stage"] == "P0"), "is_p0m": int(r["stage"] == "P0-M"), "is_p1": int(r["stage"] == "P1"),
            "fav_is_second": int(fav == second), "fav_hit": int(y == fav), "fav_mid": float(m[fav]),
            "fav_claim": float(pf[fav]),
            "fav_ask": float(book.get(keys[fav], 1.0)) if book else np.nan,
            "second_hit": int(y == second), "second_ask": float(book.get(keys[second], 1.0)) if book else np.nan,
        })
    # The station's own recent record, strictly as of the decision: only
    # station-days whose label was knowable before it.
    by_station = defaultdict(list)
    for d in recs:
        by_station[d["station"]].append(d)
    for items in by_station.values():
        days = defaultdict(lambda: [0.0, 0.0, ""])
        for d in items:
            acc = days[d["date"]]
            acc[0] += d["w"] * d["d"]
            acc[1] += d["w"]
            acc[2] = d["known"]
        history = sorted((known, total / weight) for total, weight, known in days.values())
        for d in items:
            prior = [value for known, value in history if known <= d["at"]]
            d["st_trail"] = float(np.mean(prior[-15:])) if len(prior) >= 5 else np.nan
            d["st_trail_n"] = len(prior)
    for d in recs:
        d["trade"] = d["fav_hit"] - d["fav_ask"] if not np.isnan(d["fav_ask"]) else np.nan
    return recs


def summary(items, seed):
    if not items:
        return None
    w = np.array([d["w"] for d in items])
    W = w.sum()

    def wm(f):
        return float((w * np.array([float(f(d)) for d in items])).sum() / W)

    days = sorted({d["date"] for d in items})
    index = {day: i for i, day in enumerate(days)}
    di = np.array([index[d["date"]] for d in items])
    rng = np.random.default_rng(seed)

    def ci(num, den):
        a = np.bincount(di, weights=w * num, minlength=len(days))
        b = np.bincount(di, weights=w * den, minlength=len(days))
        boots = []
        for _ in range(DRAWS):
            pick = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
            boots.append(float(pick @ a / (pick @ b)))
        return [float(a.sum() / b.sum()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]

    traded = np.array([not np.isnan(d["trade"]) for d in items])
    out = {
        "station_day_stages": float(W), "event_days": len(days),
        "d": ci(np.array([d["d"] for d in items]), np.ones(len(items))),
        "model_gives_truth_more": wm(lambda d: d["d"] > 0),
        "model_top_right_market_top_wrong": wm(lambda d: d["model_top_right"] and not d["market_top_right"]),
        "market_top_right_model_top_wrong": wm(lambda d: d["market_top_right"] and not d["model_top_right"]),
    }
    if traded.any():
        num = np.where(traded, np.nan_to_num(np.array([d["trade"] for d in items])), 0.0)
        den = np.where(traded, np.nan_to_num(np.array([d["fav_ask"] for d in items])), 0.0)
        out["model_favourite_roi_at_ask"] = ci(num, den)
    return out


def univariate(recs, cut):
    train = [d for d in recs if d["date"] < cut]
    test = [d for d in recs if d["date"] >= cut]
    both_positive = []
    cells = 0
    for stage in ("ALL", "P0", "P0-M", "P1"):
        tr = [d for d in train if stage == "ALL" or d["stage"] == stage]
        te = [d for d in test if stage == "ALL" or d["stage"] == stage]
        for f in SCAN:
            values = np.array([d[f] for d in tr], float)
            ok = ~np.isnan(values)
            if ok.sum() < 50:
                continue
            edges = [-np.inf, *np.unique(np.quantile(values[ok], [0.2, 0.4, 0.6, 0.8])), np.inf]
            for lo, hi in zip(edges[:-1], edges[1:]):
                a = [d for d in tr if not np.isnan(d[f]) and lo <= d[f] < hi]
                b = [d for d in te if not np.isnan(d[f]) and lo <= d[f] < hi]
                wa, wb = sum(d["w"] for d in a), sum(d["w"] for d in b)
                if wa < 20 or wb < 20:
                    continue
                cells += 1
                da = sum(d["w"] * d["d"] for d in a) / wa
                db = sum(d["w"] * d["d"] for d in b) / wb
                if da > 0 and db > 0:
                    both_positive.append({"stage": stage, "feature": f, "range": [lo, hi], "train_d": da, "test_d": db})
    return {"split_event_date": cut, "cells_read": cells, "cells_model_ahead_in_both": both_positive}


def walk_forward(recs, target, start=20, step=3):
    dates = sorted({d["date"] for d in recs})
    di = np.array([dates.index(d["date"]) for d in recs])
    X = np.array([[d[f] for f in FEATURES] for d in recs], float)
    y = np.array([d[target] for d in recs], float)
    w = np.array([d["w"] for d in recs])
    pred = np.full(len(recs), np.nan)
    for k in range(start, len(dates), step):
        train = (di < k - 2) & ~np.isnan(y)
        test = (di >= k) & (di < k + step)
        if train.sum() < 500 or not test.any():
            continue
        model = HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=200,
                                              min_samples_leaf=200, l2_regularization=1.0)
        model.fit(X[train], y[train], sample_weight=w[train])
        pred[test] = model.predict(X[test])
    out = {"test_event_dates": [dates[start], dates[-1]]}
    for s, stage in enumerate(("P0", "P0-M", "P1")):
        chosen = ~np.isnan(pred) & np.array([d["stage"] == stage for d in recs])
        if not chosen.any():
            continue
        q90 = np.quantile(pred[chosen], 0.9)
        out[stage] = {
            "all": summary([recs[i] for i in np.flatnonzero(chosen)], SEED + 10 * s),
            "top_10pct_predicted": summary([recs[i] for i in np.flatnonzero(chosen & (pred >= q90))], SEED + 10 * s + 1),
            "predicted_positive": summary([recs[i] for i in np.flatnonzero(chosen & (pred > 0))], SEED + 10 * s + 2),
        }
    return out


def second_bucket_cell(recs):
    out = {}
    for stage in ("P0", "P0-M"):
        items = [d for d in recs if d["stage"] == stage and not np.isnan(d["second_ask"])]
        count = Counter((d["station"], d["date"]) for d in items)
        for d in items:
            d["_w"] = 1.0 / count[(d["station"], d["date"])]

        def roi(sub):
            w = np.array([d["_w"] for d in sub])
            return {"station_days": float(w.sum()), "event_days": len({d["date"] for d in sub}),
                    "hit": float(np.average([d["second_hit"] for d in sub], weights=w)),
                    "ask": float(np.average([d["second_ask"] for d in sub], weights=w)),
                    "roi_at_ask": float(np.average([d["second_hit"] for d in sub], weights=w)
                                        / np.average([d["second_ask"] for d in sub], weights=w) - 1.0)}
        favoured = [d for d in items if d["fav_is_second"]]
        days = sorted({d["date"] for d in favoured})
        half = days[len(days) // 2]
        out[stage] = {"market_second_all": roi(items), "market_second_model_favours": roi(favoured),
                      "market_second_model_favours_other": roi([d for d in items if not d["fav_is_second"]]),
                      "model_favours_first_half": roi([d for d in favoured if d["date"] < half]),
                      "model_favours_second_half": roi([d for d in favoured if d["date"] >= half])}
    return out


def main():
    warnings.filterwarnings("ignore")
    pop_cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    ask_cache = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if pop_cache is not None and pop_cache.exists():
        rows = pickle.loads(pop_cache.read_bytes())
        rows = rows[0] if isinstance(rows, tuple) else rows
    else:
        rows = load(DB)
    asks = pickle.loads(ask_cache.read_bytes()) if ask_cache is not None and ask_cache.exists() else load_asks()
    recs = build(rows, asks)
    dates = sorted({d["date"] for d in recs})
    result = {
        "question": "熊先生 2026-09-26：找到模型对、市场错的时候",
        "decisions": len(recs),
        "by_stage": {s: summary([d for d in recs if d["stage"] == s], SEED + i)
                     for i, s in enumerate(("P0", "P0-M", "P1"))},
        "univariate_time_split": univariate(recs, dates[int(len(dates) * 0.6)]),
        "walk_forward_target_d": walk_forward(recs, "d"),
        "walk_forward_target_trade": walk_forward(recs, "trade"),
        "p0m_second_bucket_cell": second_bucket_cell(recs),
    }
    (HERE / "model_right_market_wrong_results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1, default=float))
    u = result["univariate_time_split"]
    print("univariate cells read", u["cells_read"], "model ahead in both halves", len(u["cells_model_ahead_in_both"]))
    for key in ("walk_forward_target_d", "walk_forward_target_trade"):
        for stage in ("P0", "P0-M", "P1"):
            row = result[key].get(stage)
            if row:
                top = row["top_10pct_predicted"]
                print(key, stage, "top10 d", [round(x, 3) for x in top["d"]],
                      "roi", [round(100 * x, 1) for x in top.get("model_favourite_roi_at_ask", [np.nan] * 3)])
    print(json.dumps(result["p0m_second_bucket_cell"]["P0-M"], ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
