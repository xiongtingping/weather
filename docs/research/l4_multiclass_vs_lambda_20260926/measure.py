#!/usr/bin/env python3
"""PROTOCOL.md: L4 walk-forward under both objectives, against the market and against lambda.

Population: production's L4 population, loaded by the 09-26 forward study's
``load_population`` (production ``valuation_store.population``, 70 days, not
anchored).  Fits: production ``posterior_store._design`` with ``_fit_logistic``
(the served objective) and ``_fit_multiclass`` (charter 211's); rows and the
runtime's row choice mirror the lane (28-day window, stage row then five lead
bands shrunk by 8 pseudo-days, 100 station-days / 8 days to leave
accumulating).  Beliefs: production ``posterior_vector_v1``.  Lambda:
production ``effective_edge_lambda_resolved`` on the tables in force at each
decision.  Read-only; writes results.json.

    python3 measure.py [population_cache.pkl]
"""
from __future__ import annotations

import importlib.util
import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import posterior_store as PS  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    beta_lead_band, economic_hurdle_usdc, effective_edge_lambda_resolved)
from bayes_basket_target.basket_kernel.posterior_v1 import posterior_vector_v1  # noqa: E402
from bayes_basket_target.effective_edge_store import load_effective_edge_tables  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "forward_edge", ROOT / "docs" / "research" / "posterior_forward_and_edge_20260926" / "forward_edge.py")
FE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FE)

STAGES = ("P0", "P0-M", "P1")
BANDS = PS.LEAD_BAND_LABELS
OBJECTIVES = {"binary": PS._fit_logistic, "multiclass": PS._fit_multiclass}
DRAWS, SEED = 2000, 20260926
EPS = 1e-9


def _instant(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _ps_rows(rows):
    count = defaultdict(int)
    for r in rows:
        count[(r["station"], r["date"])] += 1
    return [{"label": r["keys"][r["hit"]], "date": r["date"], "weight": 1.0 / count[(r["station"], r["date"])],
             "station": r["station"],
             "m": {k: float(v) for k, v in zip(r["keys"], r["m"])},
             "p": {k: float(v) for k, v in zip(r["keys"], r["p"])}} for r in rows]


def _fit(rows, objective, parent=None, n_days=None):
    """Point parameters, or None when the row is accumulating (production hurdles)."""
    station_days = len({(r["station"], r["date"]) for r in rows})
    days = {r["date"] for r in rows}
    if station_days < PS.MIN_STATION_DAYS or len(days) < PS.MIN_DAYS:
        return None
    design = PS._design(_ps_rows(rows))
    theta = OBJECTIVES[objective](design, row_weight=1.0, prior_mean=(0.0, 0.0, 0.0))
    if parent is not None:
        n = float(design["n_days"])
        theta = (n * theta + PS.BAND_SHRINK_DAYS * np.asarray(parent)) / (n + PS.BAND_SHRINK_DAYS)
    return [float(x) for x in theta]


def walk_forward(rows):
    tested = []
    for stage in STAGES:
        in_stage = [r for r in rows if r["stage"] == stage]
        for r in in_stage:
            r["lead_band"] = beta_lead_band(r["hours"])
        by_day = defaultdict(list)
        for r in in_stage:
            by_day[r["date"]].append(r)
        for day in sorted(by_day):
            first = min(_instant(r["at"]) for r in by_day[day])
            floor = (date.fromisoformat(day) - timedelta(days=PS.WINDOW_DAYS)).isoformat()
            train = [r for r in in_stage if floor <= r["date"] < day and _instant(r["label_known_at"]) <= first]
            fitted = {}
            for objective in OBJECTIVES:
                whole = _fit(train, objective)
                bands = {}
                if whole is not None:
                    for band in BANDS:
                        bands[band] = _fit([r for r in train if r["lead_band"] == band], objective, parent=whole)
                fitted[objective] = (whole, bands)
            for r in by_day[day]:
                out = {**r, "b": {"market": r["m"]}, "row": {}}
                for objective, (whole, bands) in fitted.items():
                    theta = bands.get(r["lead_band"]) or whole
                    out["row"][objective] = ("band" if bands.get(r["lead_band"]) else "stage" if whole else "market")
                    if theta is None:
                        out["b"][objective] = r["m"]
                        continue
                    vec = posterior_vector_v1(dict(zip(r["keys"], r["p"])), dict(zip(r["keys"], r["m"])),
                                              {"alpha": theta[0], "gamma": theta[1], "w": theta[2]})
                    out["b"][objective] = np.array([vec[k] for k in r["keys"]])
                    out.setdefault("theta", {})[objective] = theta
                tested.append(out)
        print(stage, "tested", sum(1 for t in tested if t["stage"] == stage), flush=True)
    return tested


def bucket_scores(b, hit):
    y = np.zeros_like(b)
    y[hit] = 1.0
    return -math.log(max(float(b[hit]), 1e-12)), float(np.sum((b - y) ** 2))


def compare(items, base, test, rng, score_of):
    count = defaultdict(int)
    for r in items:
        count[(r["station"], r["date"])] += 1
    days = sorted({r["date"] for r in items})
    di = {d: i for i, d in enumerate(days)}
    num = np.zeros((len(days), 2))
    den = np.zeros(len(days))
    for r in items:
        w = 1.0 / count[(r["station"], r["date"])]
        num[di[r["date"]]] += w * (np.array(score_of(r, test)) - np.array(score_of(r, base)))
        den[di[r["date"]]] += w
    out = {"decisions": len(items), "station_days": len(count), "days": len(days)}
    for j, name in enumerate(("logscore", "brier")):
        mean = float(num[:, j].sum() / den.sum())
        draws = []
        for _ in range(DRAWS):
            pick = rng.integers(0, len(days), len(days))
            draws.append(num[pick, j].sum() / den[pick].sum())
        out[name] = [mean, float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]
    out["both_lower"] = out["logscore"][2] < 0 and out["brier"][2] < 0
    out["either_higher"] = out["logscore"][1] > 0 or out["brier"][1] > 0
    return out


def candidate_windows(conn, tested):
    """Q3: candidate windows joined to the tested decisions they were enumerated at."""
    by_decision = {(t["station"], t["date"], t["stage"], t["at"]): t for t in tested}
    # The population keys a decision by the production decision's time; the
    # enumeration stamps its own, so the join goes through the evidence row.
    rows = conn.execute(
        "SELECT s.station_id, s.event_date, s.stage, s.decision_at_utc, e.production_decision_at_utc,"
        " s.buckets_json, s.win_probability, s.all_in_price, s.hit"
        " FROM basket_candidate_window_scored s JOIN basket_candidate_window_evidence e"
        " ON e.evidence_id = s.candidate_id"
        " WHERE s.hit IS NOT NULL AND s.win_probability > 0 AND s.all_in_price > 0"
    ).fetchall()
    tables_memo = {}
    out, unmatched = [], 0
    for station, day, stage, at, production_at, buckets, claim, price, hit in rows:
        t = by_decision.get((station, day, stage, production_at))
        if t is None:
            unmatched += 1
            continue
        hour = str(at)[:13]
        if hour not in tables_memo:
            tables_memo[hour] = load_effective_edge_tables(conn, as_of_utc=hour + ":59:59Z")
        lam, _src = effective_edge_lambda_resolved(stage, claim=float(claim), price=float(price), tables=tables_memo[hour])
        window = [str(b) for b in json.loads(buckets)]
        idx = [t["keys"].index(b) for b in window if b in t["keys"]]
        if len(idx) != len(window):
            unmatched += 1
            continue
        belief = {"lambda": min(1.0 - EPS, float(claim) * float(lam)),
                  "market": float(np.sum(np.asarray(t["m"])[idx])),
                  "l4_multiclass": float(np.sum(np.asarray(t["b"]["multiclass"])[idx]))}
        out.append({"station": station, "date": day, "stage": stage, "price": float(price), "hit": int(hit),
                    "belief": belief})
    return out, unmatched


def window_scores(r, arm):
    b = min(1.0 - EPS, max(EPS, r["belief"][arm]))
    h = r["hit"]
    return -math.log(b if h else 1.0 - b), (b - h) ** 2


def admitted_pnl(items, arm, rng):
    by_sd = defaultdict(lambda: [0.0, 0])
    for r in items:
        if r["belief"][arm] - r["price"] >= economic_hurdle_usdc(r["price"]):
            e = by_sd[(r["station"], r["date"])]
            e[0] += r["hit"] - r["price"]
            e[1] += 1
    if not by_sd:
        return {"station_days": 0, "windows": 0}
    days = sorted({d for _s, d in by_sd})
    di = {d: i for i, d in enumerate(days)}
    num, den = np.zeros(len(days)), np.zeros(len(days))
    for (_s, d), (pnl, n) in by_sd.items():
        num[di[d]] += pnl / n
        den[di[d]] += 1.0
    draws = []
    for _ in range(DRAWS):
        pick = rng.integers(0, len(days), len(days))
        draws.append(num[pick].sum() / den[pick].sum())
    return {"station_days": len(by_sd), "windows": int(sum(v[1] for v in by_sd.values())), "days": len(days),
            "pnl_per_window_unit": float(num.sum() / den.sum()),
            "ci": [float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]}


def main():
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    if cache is not None and cache.exists():
        rows = pickle.loads(cache.read_bytes())
    else:
        rows = FE.load_population(conn)
        if cache is not None:
            cache.write_bytes(pickle.dumps(rows))
    print("population", len(rows), flush=True)
    tested = walk_forward(rows)
    rng = np.random.default_rng(SEED)
    result = {"protocol": "PROTOCOL.md", "population": len(rows), "tested": len(tested), "Q1": {}, "Q2": {}, "Q3": {}}
    bs = lambda r, arm: bucket_scores(np.asarray(r["b"][arm]), r["hit"])  # noqa: E731
    for stage in STAGES:
        items = [t for t in tested if t["stage"] == stage]
        fitted = [t for t in items if t["row"]["multiclass"] != "market"]
        result["Q1"][stage] = compare(fitted, "binary", "multiclass", rng, bs) if fitted else None
        result["Q2"][stage] = {}
        for band in BANDS:
            sub = [t for t in items if t["lead_band"] == band]
            if sub:
                result["Q2"][stage][band] = {
                    "rows_read": dict(sorted(defaultdict(int, {k: sum(1 for t in sub if t["row"]["multiclass"] == k)
                                                             for k in ("band", "stage", "market")}).items())),
                    "multiclass_vs_market": compare(sub, "market", "multiclass", rng, bs),
                    "binary_vs_market": compare(sub, "market", "binary", rng, bs),
                    "theta_multiclass_median": (np.median([t["theta"]["multiclass"] for t in sub if "theta" in t
                                                           and "multiclass" in t["theta"]], axis=0).tolist()
                                                if any("theta" in t for t in sub) else None),
                }
    windows, unmatched = candidate_windows(conn, tested)
    conn.close()
    result["Q3"]["unmatched_windows"] = unmatched
    for stage in STAGES:
        sub = [w for w in windows if w["stage"] == stage]
        if not sub:
            continue
        result["Q3"][stage] = {
            "l4_vs_lambda": compare(sub, "lambda", "l4_multiclass", rng, window_scores),
            "l4_vs_market": compare(sub, "market", "l4_multiclass", rng, window_scores),
            "lambda_vs_market": compare(sub, "market", "lambda", rng, window_scores),
            "admitted": {arm: admitted_pnl(sub, arm, rng) for arm in ("lambda", "l4_multiclass", "market")},
        }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    fmt = lambda c: (f"LS {c['logscore'][0]:+.4f} [{c['logscore'][1]:+.4f},{c['logscore'][2]:+.4f}] "  # noqa: E731
                     f"Br {c['brier'][0]:+.4f} [{c['brier'][1]:+.4f},{c['brier'][2]:+.4f}] sd={c['station_days']} d={c['days']}")
    for stage in STAGES:
        c = result["Q1"][stage]
        print(f"Q1 {stage} multiclass - binary: {fmt(c) if c else 'no fitted decisions'}")
    for stage in STAGES:
        for band, q in result["Q2"][stage].items():
            print(f"Q2 {stage} {band:7} rows {q['rows_read']} | multi-mkt {fmt(q['multiclass_vs_market'])} both<0={q['multiclass_vs_market']['both_lower']}"
                  f" | theta {None if q['theta_multiclass_median'] is None else [round(x, 3) for x in q['theta_multiclass_median']]}")
    print("Q3 unmatched", unmatched)
    for stage in STAGES:
        q = result["Q3"].get(stage)
        if not q:
            continue
        print(f"Q3 {stage} l4-lambda {fmt(q['l4_vs_lambda'])}")
        print(f"Q3 {stage} l4-market {fmt(q['l4_vs_market'])}")
        print(f"Q3 {stage} lambda-market {fmt(q['lambda_vs_market'])}")
        for arm, a in q["admitted"].items():
            print(f"   admitted {arm:14} {a}")


if __name__ == "__main__":
    main()
