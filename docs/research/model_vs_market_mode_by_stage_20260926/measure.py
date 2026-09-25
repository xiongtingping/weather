#!/usr/bin/env python3
"""Model's top bucket vs the market's top bucket, per stage, scored on truth.

熊先生 2026-09-26: 「比较 p0、p0m 和 p1 三个阶段：用模型的预报概率和市场的预报
概率比较，哪一个更准？」  Population: production's L4 population
(``valuation_store.population``) -- one row per decision with the served
weather vector p, the market's normalised mid vector m from the WS snapshot the
decision priced against, and the settled bucket.  Read in 7-day event-date
chunks so no read transaction is long.  Read-only; writes results.json.

    python3 measure.py [cache.pkl]
"""
from __future__ import annotations

import json
import math
import pickle
import re
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import valuation_store  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

STAGES = ("P0", "P0-M", "P1")
FIRST, LAST = date(2026, 8, 14), date(2026, 9, 24)
SPLIT = "2026-09-19"
DRAWS = 1000
SEED = 20260926
NUM = re.compile(r"-?\d+(?:\.\d+)?")


def load(conn_uri):
    rows = []
    for stage in STAGES:
        as_of = LAST
        while as_of > FIRST:
            span = min(7, (as_of - FIRST).days)
            conn = sqlite3.connect(conn_uri, uri=True, timeout=60)
            conn.execute("PRAGMA query_only=ON")
            chunk, _meta = valuation_store.population(
                conn, stage_scope=stage, as_of=as_of.isoformat(), window_days=span, anchored=False)
            conn.close()
            for r in chunk:
                r.pop("half_spread", None)
            rows.extend(chunk)
            as_of -= timedelta(days=span)
    return rows


def order(label):
    return float(NUM.search(label).group())


def prepare(rows):
    recs = []
    for r in rows:
        keys = sorted(r["m"], key=order)
        m = np.array([float(r["m"][k]) for k in keys])
        m = m / m.sum()
        p = np.array([float(r["p"].get(k, 0.0) or 0.0) for k in keys])
        p = p / p.sum()
        floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, p)})
        pf = np.array([floored[k] for k in keys])
        y = keys.index(r["label"])
        onehot = np.eye(len(keys))[y]
        m_top = sorted(np.flatnonzero(m >= m.max() - 1e-12))
        p_top = sorted(np.flatnonzero(p >= p.max() - 1e-12))
        d = {
            "stage": r["stage"], "station": r["station"], "date": r["date"], "at": r["at"],
            "w": r["weight"], "keys": keys, "y": y, "m_top": m_top, "p_top": p_top,
            "hours": hours_to_settlement_if_known(r["date"], as_of_utc=r["at"],
                                                  settlement_timezone=r.get("settlement_timezone")),
            # A tie on the top shares the credit.
            "hit_m": (y in m_top) / len(m_top), "hit_p": (y in p_top) / len(p_top),
            "p_self": float(p.max()), "m_self": float(m.max()),
            "m_on_p_top": float(m[p_top[0]]), "p_on_m_top": float(p[m_top[0]]),
            "ls_m": -math.log(max(m[y], 1e-12)), "ls_p": -math.log(max(pf[y], 1e-12)),
            "br_m": float(((m - onehot) ** 2).sum()), "br_p": float(((pf - onehot) ** 2).sum()),
        }
        if set(m_top) == set(p_top):
            d["relation"] = "agree"
        elif set(m_top) & set(p_top):
            d["relation"] = "tie_overlap"
        else:
            d["relation"] = "disagree"
            pi, mi = p_top[0], m_top[0]
            lo, hi = sorted((pi, mi))
            d["truth"] = ("model_top" if y in p_top else "market_top" if y in m_top else
                          "between" if lo < y < hi else
                          "beyond_market_top" if (y - mi) * (mi - pi) > 0 else "beyond_model_top")
            d["gap_buckets"] = abs(pi - mi)
        recs.append(d)
    return recs


def first_decisions(items):
    best = {}
    for d in items:
        key = (d["station"], d["date"])
        if key not in best or d["at"] < best[key]["at"]:
            best[key] = d
    return [dict(d, w=1.0) for d in best.values()]


def once_per_station_day(items):
    count = Counter((d["station"], d["date"]) for d in items)
    return [dict(d, w=1.0 / count[(d["station"], d["date"])]) for d in items]


def mean_ci(items, f, seed):
    days = sorted({d["date"] for d in items})
    index = {day: i for i, day in enumerate(days)}
    di = np.array([index[d["date"]] for d in items])
    w = np.array([d["w"] for d in items])
    v = np.array([float(f(d)) for d in items])
    s = np.bincount(di, weights=w * v, minlength=len(days))
    t = np.bincount(di, weights=w, minlength=len(days))
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(DRAWS):
        pick = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
        boots.append(float(pick @ s / (pick @ t)))
    return [float(s.sum() / t.sum()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]


def wmean(items, f):
    w = np.array([d["w"] for d in items])
    return float((w * np.array([float(f(d)) for d in items])).sum() / w.sum())


def summarise(items, seed):
    if not items:
        return None
    out = {
        "station_days": len({(d["station"], d["date"]) for d in items}),
        "event_days": len({d["date"] for d in items}), "decisions": len(items),
        "median_hours_to_settlement": float(np.median([d["hours"] for d in items if d["hours"] is not None])),
        "hit_model_top": wmean(items, lambda d: d["hit_p"]),
        "hit_market_top": wmean(items, lambda d: d["hit_m"]),
        "hit_market_minus_model": mean_ci(items, lambda d: d["hit_m"] - d["hit_p"], seed),
        "share_disagree": wmean(items, lambda d: d["relation"] == "disagree"),
        "share_tie_overlap": wmean(items, lambda d: d["relation"] == "tie_overlap"),
        "logscore_model": wmean(items, lambda d: d["ls_p"]),
        "logscore_market": wmean(items, lambda d: d["ls_m"]),
        "logscore_model_minus_market": mean_ci(items, lambda d: d["ls_p"] - d["ls_m"], seed + 1),
        "brier_model": wmean(items, lambda d: d["br_p"]),
        "brier_market": wmean(items, lambda d: d["br_m"]),
        "brier_model_minus_market": mean_ci(items, lambda d: d["br_p"] - d["br_m"], seed + 2),
    }
    agree = [d for d in items if d["relation"] == "agree"]
    out["hit_when_agree"] = wmean(agree, lambda d: d["hit_m"]) if agree else None
    wins = Counter()
    for day in sorted({d["date"] for d in items}):
        diff = wmean([d for d in items if d["date"] == day], lambda d: d["hit_m"] - d["hit_p"])
        wins["market" if diff > 1e-9 else "model" if diff < -1e-9 else "tie"] += 1
    out["event_days_won_on_hit"] = dict(wins)
    dis = [d for d in items if d["relation"] == "disagree"]
    if dis:
        total = sum(d["w"] for d in dis)
        where = Counter()
        gaps = Counter()
        for d in dis:
            where[d["truth"]] += d["w"]
            gaps[d["gap_buckets"]] += d["w"]
        out["disagree"] = {
            "station_days": len({(d["station"], d["date"]) for d in dis}),
            "truth_at": {k: v / total for k, v in where.items()},
            "market_minus_model_hit": mean_ci(
                dis, lambda d: (d["truth"] == "market_top") - (d["truth"] == "model_top"), seed + 3),
            "gap_buckets": {str(k): v / total for k, v in sorted(gaps.items())},
            "model_prob_on_own_top": wmean(dis, lambda d: d["p_self"]),
            "market_prob_on_model_top": wmean(dis, lambda d: d["m_on_p_top"]),
            "market_prob_on_own_top": wmean(dis, lambda d: d["m_self"]),
            "model_prob_on_market_top": wmean(dis, lambda d: d["p_on_m_top"]),
        }
    return out


def examples(recs, stage, model_top, market_top):
    seen = {}
    for d in sorted(recs, key=lambda d: d["at"]):
        if (d["stage"] == stage and d["relation"] == "disagree"
                and d["keys"][d["p_top"][0]] == model_top and d["keys"][d["m_top"][0]] == market_top):
            seen.setdefault((d["station"], d["date"]), d)
    return [{
        "station": d["station"], "event_date": d["date"], "decided_at": d["at"],
        "hours_to_settlement": None if d["hours"] is None else round(d["hours"], 1),
        "model_top": d["keys"][d["p_top"][0]], "model_prob": round(d["p_self"], 3),
        "market_top": d["keys"][d["m_top"][0]], "market_prob": round(d["m_self"], 3),
        "truth": d["keys"][d["y"]],
    } for d in sorted(seen.values(), key=lambda d: d["date"])]


def main():
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if cache is not None and cache.exists():
        rows = pickle.loads(cache.read_bytes())
        rows = rows[0] if isinstance(rows, tuple) else rows
    else:
        rows = load(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro")
        if cache is not None:
            cache.write_bytes(pickle.dumps(rows))
    recs = prepare(rows)
    result = {"question": "熊先生 2026-09-26：P0/P0-M/P1 各阶段，模型众数桶 vs 市场众数桶，谁更准",
              "population": {"decisions": len(recs), "event_dates": [min(d["date"] for d in recs),
                                                                      max(d["date"] for d in recs)]},
              "stages": {}}
    for i, stage in enumerate(STAGES):
        items = [d for d in recs if d["stage"] == stage]
        seed = SEED + 10 * i
        result["stages"][stage] = {
            "first_decision": summarise(first_decisions(items), seed),
            "first_decision_before_0919": summarise(first_decisions([d for d in items if d["date"] < SPLIT]), seed),
            "first_decision_since_0919": summarise(first_decisions([d for d in items if d["date"] >= SPLIT]), seed),
            "all_decisions_station_day_once": summarise(once_per_station_day(items), seed),
        }
        bins = {}
        for lo, hi in ((0, 6), (6, 12), (12, 18), (18, 24), (24, 36), (36, 48), (48, 60), (60, 999)):
            for label, keep in (("before_0919", lambda d: d["date"] < SPLIT), ("since_0919", lambda d: d["date"] >= SPLIT)):
                part = [d for d in items if d["hours"] is not None and lo <= d["hours"] < hi and keep(d)]
                if len({d["date"] for d in part}) >= 3:
                    bins[f"{lo}-{hi}h:{label}"] = summarise(once_per_station_day(part), seed + 5)
        result["stages"][stage]["by_hours_to_settlement"] = bins
        result["stages"][stage]["examples_model_26C_market_27C"] = examples(recs, stage, "26°C", "27°C")
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for stage in STAGES:
        r = result["stages"][stage]["first_decision"]
        dis = r["disagree"]
        print(stage, r["station_days"], "model", round(100 * r["hit_model_top"], 1), "market",
              round(100 * r["hit_market_top"], 1), "disagree", round(100 * r["share_disagree"], 1),
              {k: round(100 * v, 1) for k, v in dis["truth_at"].items()},
              "LS", [round(x, 3) for x in r["logscore_model_minus_market"]])


if __name__ == "__main__":
    main()
