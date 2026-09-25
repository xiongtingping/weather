#!/usr/bin/env python3
"""P0 opening, ladder (basket) level: the model's ladder vs the market's ladder.

熊先生 2026-09-26 追问：「那如果策略是 p0 开仓，梯子策略（即篮子策略呢），不比较
单桶，比较篮子」.  Population: production's L4 population, stage P0, each
station-day's first P0 decision (the opening), plus every P0 decision with the
station-day counted once as a second reading.  The market's raw best ask per
bucket comes from the same WS snapshot through production's
``market_reference_v1`` (ask = mid + half spread on the raw scale; fee_rate is
0 in config/trading/basket_yes_v1.json).  One share per bucket, held to
settlement.  Read-only; writes basket_results.json.

    python3 basket_ladders.py [population_cache.pkl [asks_cache.pkl]]
"""
from __future__ import annotations

import json
import pickle
import sqlite3
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure import FIRST, LAST, ROOT, load, order  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402

DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
SPLIT = "2026-09-19"
DRAWS = 1000
SEED = 20260926


def load_asks():
    """(station, event_date, decided_at) -> {bucket: raw best ask} for P0 facts."""

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
                " WHERE f.stage = 'P0' AND f.event_date > ? AND f.event_date <= ?",
                (floor.isoformat(), as_of.isoformat())):
            ref = market_reference_v1(json.loads(book or "[]"))
            if ref.get("state") != "complete":
                continue
            total = ref["mid_sum"]
            asks[(str(station), str(day), str(at))] = {
                b: (ref["vector"][b] + ref["half_spread"].get(b, 0.0)) * total for b in ref["vector"]}
        conn.close()
        as_of = floor
    return asks


def window(v, k):
    """The k adjacent buckets holding the most of v's mass."""

    k = min(k, len(v))
    sums = [v[i:i + k].sum() for i in range(len(v) - k + 1)]
    start = int(np.argmax(sums))
    return start, start + k


def prepare(rows, asks):
    out = []
    for r in rows:
        if r["stage"] != "P0":
            continue
        book = asks.get((r["station"], r["date"], r["at"]))
        if book is None:
            continue
        keys = sorted(r["m"], key=order)
        m = np.array([float(r["m"][k]) for k in keys])
        m = m / m.sum()
        p = np.array([float(r["p"].get(k, 0.0) or 0.0) for k in keys])
        p = p / p.sum()
        ask = np.array([float(book.get(k, 1.0)) for k in keys])
        out.append({"station": r["station"], "date": r["date"], "at": r["at"], "w": r["weight"],
                    "keys": keys, "y": keys.index(r["label"]), "m": m, "p": p, "ask": ask})
    return out


def ladder(d, side, k):
    i, j = window(d["p"] if side == "model" else d["m"], k)
    return {"span": (i, j), "hit": float(i <= d["y"] < j), "claim": float(d["p"][i:j].sum()),
            "mid": float(d["m"][i:j].sum()), "cost": float(d["ask"][i:j].sum())}


def day_ci(items, num, den=None):
    days = sorted({d["date"] for d in items})
    index = {day: i for i, day in enumerate(days)}
    di = np.array([index[d["date"]] for d in items])
    w = np.array([d["w"] for d in items])
    a = np.bincount(di, weights=w * np.array([num(d) for d in items], float), minlength=len(days))
    b = np.bincount(di, weights=w * (np.ones(len(items)) if den is None else np.array([den(d) for d in items], float)),
                    minlength=len(days))
    rng = np.random.default_rng(SEED)
    boots = []
    for _ in range(DRAWS):
        pick = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
        boots.append(float(pick @ a / (pick @ b)))
    return [float(a.sum() / b.sum()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]


def wmean(items, f):
    w = np.array([d["w"] for d in items])
    return float((w * np.array([f(d) for d in items], float)).sum() / w.sum())


def ladders_table(items):
    out = {"station_days": len({(d["station"], d["date"]) for d in items}),
           "event_days": len({d["date"] for d in items}), "k": {}}
    for k in (1, 2, 3, 4, 5):
        for d in items:
            d["_m"], d["_k"] = ladder(d, "model", k), ladder(d, "market", k)
        row = {"same_ladder": wmean(items, lambda d: d["_m"]["span"] == d["_k"]["span"])}
        for side, key in (("model", "_m"), ("market", "_k")):
            row[side] = {
                "cover": wmean(items, lambda d: d[key]["hit"]),
                "model_claim": wmean(items, lambda d: d[key]["claim"]),
                "market_mid": wmean(items, lambda d: d[key]["mid"]),
                "ask_cost": wmean(items, lambda d: d[key]["cost"]),
                "cover_minus_mid": day_ci(items, lambda d: d[key]["hit"] - d[key]["mid"]),
                "roi_at_ask": day_ci(items, lambda d: d[key]["hit"] - d[key]["cost"], lambda d: d[key]["cost"]),
            }
        row["cover_market_minus_model"] = day_ci(items, lambda d: d["_k"]["hit"] - d["_m"]["hit"])
        row["pnl_per_set_model_minus_market"] = day_ci(
            items, lambda d: (d["_m"]["hit"] - d["_m"]["cost"]) - (d["_k"]["hit"] - d["_k"]["cost"]))
        out["k"][str(k)] = row
    for d in items:
        d.pop("_m", None)
        d.pop("_k", None)
    return out


def split_by_same(items):
    out = {}
    for k in (2, 3):
        for label, keep in (("same", True), ("different", False)):
            part = [dict(d, _m=ladder(d, "model", k), _k=ladder(d, "market", k)) for d in items]
            part = [d for d in part if (d["_m"]["span"] == d["_k"]["span"]) == keep]
            out[f"k{k}:{label}"] = {
                "station_days": len(part),
                "cover_model": wmean(part, lambda d: d["_m"]["hit"]),
                "cover_market": wmean(part, lambda d: d["_k"]["hit"]),
                "model_claim_on_model_ladder": wmean(part, lambda d: d["_m"]["claim"]),
                "market_mid_on_model_ladder": wmean(part, lambda d: d["_m"]["mid"]),
                "market_mid_on_market_ladder": wmean(part, lambda d: d["_k"]["mid"]),
                "ask_cost_model": wmean(part, lambda d: d["_m"]["cost"]),
                "ask_cost_market": wmean(part, lambda d: d["_k"]["cost"]),
                "roi_at_ask_model": day_ci(part, lambda d: d["_m"]["hit"] - d["_m"]["cost"], lambda d: d["_m"]["cost"]),
                "roi_at_ask_market": day_ci(part, lambda d: d["_k"]["hit"] - d["_k"]["cost"], lambda d: d["_k"]["cost"]),
            }
            if k == 3 and not keep:
                for shift in (-1, 1):
                    sub = [d for d in part if d["_m"]["span"][0] - d["_k"]["span"][0] == shift]
                    out[f"k3:shift{shift:+d}"] = {
                        "station_days": len(sub),
                        "both": wmean(sub, lambda d: d["_m"]["hit"] and d["_k"]["hit"]),
                        "only_model": wmean(sub, lambda d: d["_m"]["hit"] and not d["_k"]["hit"]),
                        "only_market": wmean(sub, lambda d: d["_k"]["hit"] and not d["_m"]["hit"]),
                        "neither": wmean(sub, lambda d: not d["_m"]["hit"] and not d["_k"]["hit"]),
                    }
    return out


def ev_ladders(items):
    """Every bucket where the model's probability is at least the ask plus h."""

    out = {}
    for h in (0.0, 0.03, 0.05, 0.10):
        part = []
        for d in items:
            chosen = np.flatnonzero(d["p"] - d["ask"] >= h)
            if len(chosen):
                part.append(dict(d, _size=len(chosen), _hit=float(d["y"] in chosen), _claim=float(d["p"][chosen].sum()),
                                 _mid=float(d["m"][chosen].sum()), _cost=float(d["ask"][chosen].sum())))
        out[f"h={h:.2f}"] = {
            "station_days": len(part), "size": wmean(part, lambda d: d["_size"]),
            "model_claim": wmean(part, lambda d: d["_claim"]), "market_mid": wmean(part, lambda d: d["_mid"]),
            "cover": wmean(part, lambda d: d["_hit"]), "ask_cost": wmean(part, lambda d: d["_cost"]),
            "roi_at_ask": day_ci(part, lambda d: d["_hit"] - d["_cost"], lambda d: d["_cost"]),
        }
    return out


def openings(items):
    best = {}
    for d in items:
        key = (d["station"], d["date"])
        if key not in best or d["at"] < best[key]["at"]:
            best[key] = d
    return [dict(d, w=1.0) for d in best.values()]


def main():
    pop_cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    ask_cache = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if pop_cache is not None and pop_cache.exists():
        rows = pickle.loads(pop_cache.read_bytes())
        rows = rows[0] if isinstance(rows, tuple) else rows
    else:
        rows = load(DB)
    asks = pickle.loads(ask_cache.read_bytes()) if ask_cache is not None and ask_cache.exists() else load_asks()
    items = prepare(rows, asks)
    opening = openings(items)
    count = Counter((d["station"], d["date"]) for d in items)
    result = {
        "question": "熊先生 2026-09-26：P0 开仓的梯子（篮子）策略，模型梯子 vs 市场梯子",
        "p0_decisions_with_book": len(items),
        "opening": ladders_table(opening),
        "opening_before_0919": ladders_table([d for d in opening if d["date"] < SPLIT]),
        "opening_since_0919": ladders_table([d for d in opening if d["date"] >= SPLIT]),
        "all_p0_decisions_station_day_once": ladders_table(
            [dict(d, w=1.0 / count[(d["station"], d["date"])]) for d in items]),
        "opening_same_vs_different_ladder": split_by_same(opening),
        "opening_model_p_above_ask_ladder": ev_ladders(opening),
    }
    (HERE / "basket_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for k, row in result["opening"]["k"].items():
        print("k", k, "cover", round(100 * row["model"]["cover"], 1), round(100 * row["market"]["cover"], 1),
              "roi", [round(100 * x, 1) for x in row["model"]["roi_at_ask"]], [round(100 * x, 1) for x in row["market"]["roi_at_ask"]])
    for h, row in result["opening_model_p_above_ask_ladder"].items():
        print(h, row["station_days"], round(row["size"], 2), round(100 * row["model_claim"], 1), round(100 * row["market_mid"], 1),
              round(100 * row["cover"], 1), [round(100 * x, 1) for x in row["roi_at_ask"]])


if __name__ == "__main__":
    main()
