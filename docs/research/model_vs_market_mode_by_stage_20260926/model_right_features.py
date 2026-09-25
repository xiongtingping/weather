#!/usr/bin/env python3
"""When the model's top bucket is right and the market's is not: conditions.

熊先生 2026-09-26 追问：「模型对的时候有什么条件和特征？」  Same population as
measure.py (production's L4 population).  Disagreement = the model's top bucket
differs from the market's (market top unique).  Each station-day x stage counts
once, spread over its disagreement decisions.  Read-only; writes
model_right_results.json.

    python3 model_right_features.py [cache.pkl]
"""
from __future__ import annotations

import json
import pickle
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure import ROOT, load, order  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

DRAWS = 1000
SEED = 20260926
DISCOVERY_END = "2026-09-16"  # the 09-18 cell searches read event days up to here


def build(rows):
    decisions, disagreements = [], []
    for r in rows:
        keys = sorted(r["m"], key=order)
        m = np.array([float(r["m"][k]) for k in keys])
        m = m / m.sum()
        p = np.array([float(r["p"].get(k, 0.0) or 0.0) for k in keys])
        p = p / p.sum()
        y = keys.index(r["label"])
        ranked = np.argsort(-m, kind="stable")
        m1, m2 = int(ranked[0]), int(ranked[1])
        if m[m1] - m[m2] < 1e-12:
            continue  # the market has no single top bucket
        pi = int(np.argmax(p))
        hours = hours_to_settlement_if_known(r["date"], as_of_utc=r["at"],
                                             settlement_timezone=r.get("settlement_timezone"))
        d = {"stage": r["stage"], "station": r["station"], "date": r["date"], "at": r["at"], "hours": hours,
             "unit": "F" if "°F" in keys[0] else "C", "y": y, "pi": pi, "m1": m1, "m2": m2,
             "pm1": float(m[m1]), "pm2": float(m[m2]), "m_on_p": float(m[pi]), "p_self": float(p[pi]),
             "p_on_m1": float(p[m1]), "adjacent": abs(m1 - m2) == 1,
             "model_on": "m1" if pi == m1 else "m2" if pi == m2 else "other"}
        decisions.append(d)
        if pi != m1:
            disagreements.append(dict(d, gap=abs(pi - m1), model_warmer=pi > m1,
                                      right="model" if y == pi else "market" if y == m1 else "neither"))
    return decisions, disagreements


def weight_once(items):
    count = Counter((d["stage"], d["station"], d["date"]) for d in items)
    for d in items:
        d["w"] = 1.0 / count[(d["stage"], d["station"], d["date"])]
    return items


def mean_ci(items, f, seed):
    days = sorted({d["date"] for d in items})
    index = {day: i for i, day in enumerate(days)}
    di = np.array([index[d["date"]] for d in items])
    w = np.array([d.get("w", 1.0) for d in items])
    v = np.array([float(f(d)) for d in items])
    s = np.bincount(di, weights=w * v, minlength=len(days))
    t = np.bincount(di, weights=w, minlength=len(days))
    rng = np.random.default_rng(seed)
    boots = [float(pick @ s / (pick @ t)) for pick in
             (np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
              for _ in range(DRAWS))]
    return [float(s.sum() / t.sum()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]


def cell(items, seed):
    if not items:
        return None
    w = np.array([d.get("w", 1.0) for d in items])

    def wm(f):
        return float((w * np.array([float(f(d)) for d in items])).sum() / w.sum())

    return {"station_day_stages": float(w.sum()), "event_days": len({d["date"] for d in items}),
            "model_right": wm(lambda d: d["right"] == "model"),
            "market_right": wm(lambda d: d["right"] == "market"),
            "market_price_on_model_bucket": wm(lambda d: d["m_on_p"]),
            "model_claim_on_own_bucket": wm(lambda d: d["p_self"]),
            "model_right_minus_price": mean_ci(items, lambda d: (d["right"] == "model") - d["m_on_p"], seed)}


def binned(items, key, bins, seed):
    return {name: cell([d for d in items if lo <= key(d) < hi], seed + i)
            for i, (name, lo, hi) in enumerate(bins)}


def fit_logit(x, y, w, l2=1e-3):
    b = np.zeros(x.shape[1])
    for _ in range(50):
        q = 1.0 / (1.0 + np.exp(-(x @ b)))
        g = x.T @ (w * (q - y)) + l2 * b
        h = (x * (w * q * (1.0 - q))[:, None]).T @ x + l2 * np.eye(len(b))
        b -= np.linalg.solve(h, g)
    return b


def logit(x):
    x = np.clip(np.asarray(x, float), 1e-4, 1.0 - 1e-4)
    return np.log(x / (1.0 - x))


def claim_adds_information(dis):
    """Leave-one-day-out log loss for 'model right' when the model picks the
    market's #2: the market's price alone vs price plus the model's claim."""

    out = {}
    for stage in ("ALL", "P0", "P0-M", "P1"):
        items = [d for d in dis if d["model_on"] == "m2" and (stage == "ALL" or d["stage"] == stage)]
        y = np.array([d["right"] == "model" for d in items], float)
        w = np.array([d["w"] for d in items])
        x0 = np.column_stack([np.ones(len(items)), logit([d["m_on_p"] for d in items])])
        x1 = np.column_stack([x0, logit([d["p_self"] for d in items])])
        days = np.array([d["date"] for d in items])
        loss = {"price_only": 0.0, "price_plus_model_claim": 0.0}
        for day in sorted(set(days)):
            train, test = days != day, days == day
            for name, x in (("price_only", x0), ("price_plus_model_claim", x1)):
                q = np.clip(1.0 / (1.0 + np.exp(-(x[test] @ fit_logit(x[train], y[train], w[train])))), 1e-6, 1 - 1e-6)
                loss[name] += float(-(w[test] * (y[test] * np.log(q) + (1 - y[test]) * np.log(1 - q))).sum())
        out[stage] = {"station_day_stages": float(w.sum()), **{k: v / w.sum() for k, v in loss.items()}}
    return out


def near_tie(decisions):
    """Market split between two adjacent buckets (#1 - #2 < 0.10): does the
    model's pick move which of the two wins?"""

    out = {}
    for stage in ("ALL", "P0", "P0-M", "P1"):
        for pick in ("m2", "m1"):
            items = weight_once([dict(d) for d in decisions
                                 if (stage == "ALL" or d["stage"] == stage) and d["adjacent"]
                                 and d["pm1"] - d["pm2"] < 0.10 and d["model_on"] == pick])
            if not items:
                continue
            w = np.array([d["w"] for d in items])
            out[f"{stage}:model_picks_{pick}"] = {
                "station_day_stages": float(w.sum()),
                "market_top_wins": float((w * np.array([d["y"] == d["m1"] for d in items])).sum() / w.sum()),
                "market_second_wins": float((w * np.array([d["y"] == d["m2"] for d in items])).sum() / w.sum()),
            }
    return out


def market_second_by_model_pick(decisions):
    """The market's #2 bucket at the same price: does it hit more often when
    the model picks it than when the model picks the market's #1?"""

    out = {}
    for i, (name, lo, hi) in enumerate((("[.2,.3)", .2, .3), ("[.3,.4)", .3, .4), ("[.4,.5]", .4, .51))):
        for pick in ("m2", "m1", "other"):
            items = weight_once([dict(d) for d in decisions if lo <= d["pm2"] < hi and d["model_on"] == pick])
            if len({(d["stage"], d["station"], d["date"]) for d in items}) < 15:
                continue
            w = np.array([d["w"] for d in items])
            out[f"{name}:model_picks_{pick}"] = {
                "station_day_stages": float(w.sum()),
                "second_hit": float((w * np.array([d["y"] == d["m2"] for d in items])).sum() / w.sum()),
                "second_price": float((w * np.array([d["pm2"] for d in items])).sum() / w.sum()),
                "second_hit_minus_price": mean_ci(items, lambda d: (d["y"] == d["m2"]) - d["pm2"], SEED + 80 + i),
            }
    return out


def station_stability(dis):
    dates = sorted({d["date"] for d in dis})
    split = dates[len(dates) // 2]
    acc = defaultdict(lambda: {"a": [0.0, 0.0], "b": [0.0, 0.0]})
    for d in dis:
        half = acc[d["station"]]["a" if d["date"] < split else "b"]
        half[0] += d["w"]
        half[1] += d["w"] * (d["right"] == "model")
    rates = [(v["a"][1] / v["a"][0], v["b"][1] / v["b"][0]) for v in acc.values()
             if v["a"][0] >= 8 and v["b"][0] >= 8]
    first, second = np.array(rates).T
    top = np.argsort(-first)[:10]
    return {"split_date": split, "stations": len(rates),
            "corr_first_vs_second_half": float(np.corrcoef(first, second)[0, 1]),
            "top10_first_half": float(first[top].mean()), "same_stations_second_half": float(second[top].mean())}


def forward_checks(decisions, dis):
    def first_per(items, key):
        best = {}
        for d in items:
            k = key(d)
            if k not in best or d["at"] < best[k]["at"]:
                best[k] = d
        return [dict(d, w=1.0) for d in best.values()]

    def read(items, hit, price, seed):
        if not items:
            return None
        return {"n": len(items), "event_days": len({d["date"] for d in items}),
                "hit": float(np.mean([hit(d) for d in items])), "price": float(np.mean([price(d) for d in items])),
                "hit_minus_price": mean_ci(items, lambda d: hit(d) - price(d), seed)}

    # The 09-18 cell: first decision of each stage, disagreeing, adjacent, market gives the model's bucket >= 0.40.
    firsts = {(d["stage"], d["station"], d["date"]): d for d in first_per(decisions, lambda d: (d["stage"], d["station"], d["date"]))}
    by_key = {(d["stage"], d["station"], d["date"], d["at"]): d for d in dis}
    tie_cell = [dict(by_key[k], w=1.0) for k in ((d["stage"], d["station"], d["date"], d["at"]) for d in firsts.values())
                if k in by_key and by_key[k]["gap"] == 1 and by_key[k]["m_on_p"] >= 0.40]
    # The 09-18 mid-band rule: the model's top bucket priced 0.40-0.60, once per station-day (at mid, not ask).
    band = [d for d in decisions if 0.40 <= d["m_on_p"] < 0.60]
    out = {}
    for label, keep in (("discovery_to_0916", lambda d: d["date"] <= DISCOVERY_END),
                        ("forward_from_0917", lambda d: d["date"] > DISCOVERY_END)):
        out[f"near_tie_cell:{label}"] = read([d for d in tie_cell if keep(d)], lambda d: d["right"] == "model",
                                             lambda d: d["m_on_p"], SEED + 70)
        out[f"model_top_priced_40_60:{label}"] = read(
            first_per([d for d in band if keep(d)], lambda d: (d["station"], d["date"])),
            lambda d: d["y"] == d["pi"], lambda d: d["m_on_p"], SEED + 71)
    return out


def main():
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if cache is not None and cache.exists():
        rows = pickle.loads(cache.read_bytes())
        rows = rows[0] if isinstance(rows, tuple) else rows
    else:
        rows = load(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro")
    decisions, dis = build(rows)
    weight_once(dis)
    prob = [("[0,.1)", 0, .1), ("[.1,.2)", .1, .2), ("[.2,.3)", .2, .3), ("[.3,.4)", .3, .4), ("[.4,1]", .4, 1.01)]
    result = {
        "question": "熊先生 2026-09-26：模型对的时候有什么条件和特征",
        "disagreement_station_day_stages": len({(d["stage"], d["station"], d["date"]) for d in dis}),
        "by_stage": {s: cell([d for d in dis if d["stage"] == s], SEED + i) for i, s in enumerate(("P0", "P0-M", "P1"))},
        "by_market_price_on_model_bucket": binned(dis, lambda d: d["m_on_p"], prob, SEED + 10),
        "by_model_claim_on_own_bucket": binned(dis, lambda d: d["p_self"], [
            ("<.3", 0, .3), (".3-.4", .3, .4), (".4-.5", .4, .5), (".5-.7", .5, .7), (">=.7", .7, 1.01)], SEED + 20),
        "by_gap_buckets": binned(dis, lambda d: d["gap"], [("1", 1, 2), ("2", 2, 3), ("3+", 3, 99)], SEED + 30),
        "by_direction": binned(dis, lambda d: int(d["model_warmer"]), [("model_colder", 0, 1), ("model_warmer", 1, 2)], SEED + 40),
        "by_hours_to_settlement": binned(dis, lambda d: -1 if d["hours"] is None else d["hours"], [
            ("6-12h", 6, 12), ("12-18h", 12, 18), ("18-24h", 18, 24), ("24-36h", 24, 36), ("36-48h", 36, 48), ("48h+", 48, 999)], SEED + 50),
        "by_unit": binned(dis, lambda d: int(d["unit"] == "F"), [("C", 0, 1), ("F", 1, 2)], SEED + 60),
        "by_model_pick_is_market_second": binned(dis, lambda d: int(d["model_on"] == "m2"), [("no", 0, 1), ("yes", 1, 2)], SEED + 65),
        "near_tie_who_wins": near_tie(decisions),
        "market_second_bucket_by_model_pick": market_second_by_model_pick(decisions),
        "model_claim_adds_information_lodo_logloss": claim_adds_information(dis),
        "station_split_half": station_stability(dis),
        "forward_checks_of_0918_cells": forward_checks(decisions, dis),
    }
    (HERE / "model_right_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for key in ("by_market_price_on_model_bucket", "by_model_claim_on_own_bucket", "by_gap_buckets"):
        print(key, {k: (round(100 * v["model_right"], 1), round(100 * v["market_price_on_model_bucket"], 1))
                    for k, v in result[key].items() if v})
    print("near tie", {k: (round(100 * v["market_top_wins"], 1), round(100 * v["market_second_wins"], 1))
                       for k, v in result["near_tie_who_wins"].items() if k.startswith("ALL")})
    print("lodo", result["model_claim_adds_information_lodo_logloss"]["ALL"])
    print("station", result["station_split_half"])
    print("forward", {k: (v["n"], round(100 * v["hit"], 1), round(100 * v["price"], 1),
                          [round(100 * x, 1) for x in v["hit_minus_price"]]) for k, v in result["forward_checks_of_0918_cells"].items() if v})


if __name__ == "__main__":
    main()
