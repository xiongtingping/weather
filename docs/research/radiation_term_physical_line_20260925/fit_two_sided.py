#!/usr/bin/env python3
"""Warm-side and cold-side radiation coefficients, charter 210's objective.

User 2026-09-25: 「把辐射系数拆成升温、降温两侧」.  Charter 173/210 fitted the
fleet coefficient as residual ~ k * (z * sigma): residual = settled truth minus
the pre-shift centre, de-meaned per station, z the clearness form, sigma the
served spread, day-block bootstrap, and a mean-squared-residual grid.  Here
the same objective with the two sides free:

    residual ~ k_warm * (z+ * sigma) + k_cold * (z- * sigma)

``--source replica`` reads this study's public replica (``build_cases.py``
output; the P0 proxy of ``measure.p0_roots``), which reproduces charter 210's
symmetric fit (0.069 against production's 0.068).  ``--source ssot`` reads
production exactly as ``model_layer_review_20260921/fit_radiation_coefficient_asof.py``
does (first P0 envelope of each station-day's 00Z and 12Z hours since
2026-08-16, its receipt's radiation and spread, z recomputed with the
production function) -- run it where the SSOT lives.

``within`` is the check charter 222 declares for production.  Charter 222
serves the pair centred on each station's history, which moves no station's
mean, so it is judged within stations: the residual and each arm's shift
both de-meaned per station, and the pair is kept while its mean squared
residual there is no higher than the symmetric 0.07's.  The fixed-effects
fit beside it is the same regression with the regressors de-meaned too.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
import statistics as st
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (str(ROOT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

GRID = (0.0, 0.02, 0.04, 0.07, 0.10, 0.13, 0.16, 0.19, 0.22)


def replica_rows(cache_dir: str) -> list[dict]:
    import measure as m

    m.P0_SOURCE = "global"
    days = m.load_jsonl(Path(cache_dir) / "derived" / "days.jsonl")
    roots = m.p0_roots(days)
    rows = []
    for d in days:
        root = roots.get((d["station"], d["date"]))
        z = m._num(d.get("kt_z"))
        if root is None or z is None:
            continue
        rows.append({
            "s": d["station"], "ev": d["date"], "z": z, "sig": root["sigma"],
            "res": d["truth_c"] - root["c0"],
        })
    return rows


def ssot_rows() -> list[dict]:
    import sqlite3

    from bayes_basket_target.cross_consensus import forecast_clearness_shift_sigma
    from bayes_basket_target.materialization import rehydrate_lineage_documents
    from bayes_basket_target.station_registry import station_coordinates

    c = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("pragma query_only=on")
    stations = [
        r[0] for r in c.execute(
            "select distinct station_id from calibration_station_market_timezone_binding"
        ) if len(r[0]) == 4
    ]
    history = collections.defaultdict(list)
    for s, d, v in c.execute(
        "select station_id,local_date,shortwave_radiation_sum from calibration_station_forecast_physics"
        " where shortwave_radiation_sum is not null"
    ):
        history[s].append((d, float(v)))
    for s in history:
        history[s].sort()
    truth = {}
    for s, d, t, rev in c.execute(
        "select station_id,local_date,official_tmax_c,revision_no from calibration_station_daily_high_truth"
        " where local_date>='2026-08-10' and official_tmax_c is not null"
    ):
        if (s, d) not in truth or (rev or 0) > truth[(s, d)][1]:
            truth[(s, d)] = (t, rev or 0)
    last = max(d for _, d in truth)
    span = (date.fromisoformat(last) - date(2026, 8, 16)).days + 1
    rows = []
    for s in stations:
        try:
            lat = float(station_coordinates(s)["latitude"])
        except Exception:  # noqa: BLE001
            continue
        seen = set()
        for i in range(span):
            d = (date(2026, 8, 16) + timedelta(days=i)).isoformat()
            for hh in ("T00:00", "T12:00"):
                r = c.execute(
                    "select event_date,lineage_json from basket_probability_envelope"
                    " indexed by basket_probability_envelope_station_stage_recency_idx"
                    " where station_id=? and stage='P0' and state='COMMITTED' and decision_at_utc>=?"
                    " order by decision_at_utc asc limit 1",
                    (s, d + hh),
                ).fetchone()
                if not r or r["event_date"] in seen:
                    continue
                lineage = rehydrate_lineage_documents(c, json.loads(r["lineage_json"]))
                sc = lineage.get("synoptic_cooling_json")
                sc = (json.loads(sc) if isinstance(sc, str) else sc) or {}
                if sc.get("state") != "RESOLVED" or not sc.get("shift_sigma") or sc.get("radiation_sum") is None:
                    continue
                tr = truth.get((s, r["event_date"]))
                if tr is None or lineage.get("model_center_c") is None:
                    continue
                z, _ = forecast_clearness_shift_sigma(
                    radiation_sum=float(sc["radiation_sum"]), event_date=r["event_date"],
                    latitude_deg=lat,
                    station_history=[(dd, v) for dd, v in history[s] if dd < r["event_date"]][-180:],
                    shift_sigma_per_unit=1.0,
                )
                if z is None:
                    continue
                seen.add(r["event_date"])
                rows.append({
                    "s": s, "ev": r["event_date"], "z": z,
                    "sig": sc["shift_c"] / sc["shift_sigma"],
                    "res": tr[0] - float(lineage["model_center_c"]),
                })
    return rows


def fit(rows: list[dict], *, since: str | None = None) -> dict:
    rows = [r for r in rows if since is None or r["ev"] >= since]
    by = collections.defaultdict(list)
    for r in rows:
        by[r["s"]].append(r)
    W, C, Y, D = [], [], [], []
    for station, items in by.items():
        if len(items) < 8:
            continue
        mean = st.mean(r["res"] for r in items)
        for r in items:
            W.append(max(r["z"], 0.0) * r["sig"])
            C.append(min(r["z"], 0.0) * r["sig"])
            Y.append(r["res"] - mean)
            D.append(r["ev"])

    def solve(ix):
        ww = sum(W[i] * W[i] for i in ix)
        cc = sum(C[i] * C[i] for i in ix)
        wy = sum(W[i] * Y[i] for i in ix)
        cy = sum(C[i] * Y[i] for i in ix)
        # z+ and z- never both non-zero, so the two sides decouple.
        return wy / ww, cy / cc

    def symmetric(ix):
        xx = sum((W[i] + C[i]) ** 2 for i in ix)
        return sum((W[i] + C[i]) * Y[i] for i in ix) / xx

    index = range(len(Y))
    k_warm, k_cold = solve(index)
    k_sym = symmetric(index)
    by_day = collections.defaultdict(list)
    for i, d in enumerate(D):
        by_day[d].append(i)
    days = sorted(by_day)
    random.seed(1)
    draws = []
    for _ in range(1000):
        sample = [i for d in random.choices(days, k=len(days)) for i in by_day[d]]
        draws.append(solve(sample) + (symmetric(sample),))
    warm = sorted(x[0] for x in draws)
    cold = sorted(x[1] for x in draws)
    sym = sorted(x[2] for x in draws)

    def mse(kw, kc):
        return st.mean((Y[i] - kw * W[i] - kc * C[i]) ** 2 for i in index)

    grid = {
        f"warm={kw:g},cold={kc:g}": round(mse(kw, kc), 5)
        for kw in GRID for kc in GRID
    }
    best = min(grid.items(), key=lambda item: item[1])
    return {
        "within": within(rows),
        "station_days": len(Y), "days": len(days),
        "warm": [round(k_warm, 4), round(warm[25], 4), round(warm[974], 4)],
        "cold": [round(k_cold, 4), round(cold[25], 4), round(cold[974], 4)],
        "symmetric": [round(k_sym, 4), round(sym[25], 4), round(sym[974], 4)],
        "mse_served_0.07": round(mse(0.07, 0.07), 5),
        "mse_none": round(mse(0.0, 0.0), 5),
        "mse_fitted_pair": round(mse(k_warm, k_cold), 5),
        "grid_best": best,
        "mse_grid": grid,
    }


def within(rows: list[dict]) -> dict:
    """Charter 222's production check: everything de-meaned per station."""

    by = collections.defaultdict(list)
    for r in rows:
        by[r["s"]].append(r)
    W, C, Y, D = [], [], [], []
    for items in by.values():
        if len(items) < 8:
            continue
        w = [max(r["z"], 0.0) * r["sig"] for r in items]
        c = [min(r["z"], 0.0) * r["sig"] for r in items]
        y = [r["res"] for r in items]
        mw, mc, my = st.mean(w), st.mean(c), st.mean(y)
        W.extend(v - mw for v in w)
        C.extend(v - mc for v in c)
        Y.extend(v - my for v in y)
        D.extend(r["ev"] for r in items)

    def mse(kw, kc, ix):
        return st.mean((Y[i] - kw * W[i] - kc * C[i]) ** 2 for i in ix)

    def solve(ix):
        ww = sum(W[i] * W[i] for i in ix)
        cc = sum(C[i] * C[i] for i in ix)
        wc = sum(W[i] * C[i] for i in ix)
        wy = sum(W[i] * Y[i] for i in ix)
        cy = sum(C[i] * Y[i] for i in ix)
        det = ww * cc - wc * wc
        return (cc * wy - wc * cy) / det, (ww * cy - wc * wy) / det

    index = range(len(Y))
    by_day = collections.defaultdict(list)
    for i, d in enumerate(D):
        by_day[d].append(i)
    days = sorted(by_day)
    random.seed(2)
    draws, paired = [], []
    for _ in range(1000):
        sample = [i for d in random.choices(days, k=len(days)) for i in by_day[d]]
        draws.append(solve(sample))
        paired.append(mse(0.12, 0.04, sample) - mse(0.07, 0.07, sample))
    warm = sorted(x[0] for x in draws)
    cold = sorted(x[1] for x in draws)
    paired.sort()
    k_warm, k_cold = solve(index)
    pair, symmetric = mse(0.12, 0.04, index), mse(0.07, 0.07, index)
    return {
        "station_days": len(Y), "days": len(days),
        "fixed_effects_warm": [round(k_warm, 4), round(warm[25], 4), round(warm[974], 4)],
        "fixed_effects_cold": [round(k_cold, 4), round(cold[25], 4), round(cold[974], 4)],
        "mse_none": round(mse(0.0, 0.0, index), 5),
        "mse_symmetric_0.07": round(symmetric, 5),
        "mse_pair_0.12_0.04": round(pair, 5),
        "pair_minus_symmetric": [round(pair - symmetric, 5), round(paired[25], 5), round(paired[974], 5)],
        "keep_pair": pair <= symmetric,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("replica", "ssot"), required=True)
    parser.add_argument("--cache-dir", help="replica cache (build_cases.py output)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    rows = replica_rows(args.cache_dir) if args.source == "replica" else ssot_rows()
    out = {"source": args.source, "all": fit(rows)}
    if args.source == "replica":
        out["since_2026_03_01"] = fit(rows, since="2026-03-01")
        out["production_window_since_2026_08_16"] = fit(rows, since="2026-08-16")
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    brief = {k: {kk: v[kk] for kk in ("within", "station_days", "days", "warm", "cold", "symmetric",
                                        "mse_served_0.07", "mse_none", "mse_fitted_pair", "grid_best")}
             for k, v in out.items() if isinstance(v, dict)}
    print(json.dumps(brief, indent=1))


if __name__ == "__main__":
    main()
