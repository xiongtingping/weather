#!/usr/bin/env python3
"""Charter 222 follow-up: the warm/cold pair, centred on the station's own history.

The pair k(z) = 0.12 z+ + 0.04 z- is not mean-neutral.  z is standardised on
the station's last 180 days, so it averages exactly zero there, but k bends at
zero: its average over that history is (0.12 - 0.04) x mean(z+), about +0.034
sigma, because Kt's ceiling puts z > 0 on 59% of days.  The symmetric 0.07
averages zero by construction.  Charter 160 says a mean belongs to the bias
layer, not the radiation layer.  So the pair is centred:

    shift = k(z) - mean over the same history of k(z_i)

with z_i each history day's clearness index standardised by the event day's
own history mean and sd -- exactly the numbers the production function already
computes.  The shift then pivots at about the station's median day rather than
its mean, and moves no station's mean.

What this measures, on the replica (``build_cases.py`` cache, P0 proxy of
``measure.p0_roots``):

* each station's radiation series rebuilt exactly as ``build_cases.py`` does,
  and z recomputed with the production arithmetic (checked against the cached
  ``kt_z``);
* scores with no intercept -- what serving applies -- and with a fleet
  intercept walk-forward (d - 2), as ``asymmetry.py`` does;
* charter 210's objective (residual de-meaned per station against z times the
  spread) with the regressors de-meaned per station too: the fixed-effects form
  the centred shift implies.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, time as wall_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (str(ROOT), str(ROOT / "scripts"), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

import build_cases as bc  # noqa: E402
import measure as m  # noqa: E402
from bayes_basket_target.cross_consensus import (  # noqa: E402
    clearness_index,
    extraterrestrial_radiation_mj,
    forecast_clearness_shift_sigma,
)
from stations import station_table  # noqa: E402

WARM, COLD = 0.12, 0.04
BOOT = 1000


def pair(z: float, warm: float = WARM, cold: float = COLD) -> float:
    return (cold if z < 0.0 else warm) * z


def station_series(cache: Path) -> dict[str, dict]:
    """Per station: latitude and radiation by local day, as build_cases.py."""

    out = {}
    for station, meta in station_table().items():
        model_path = next(iter(sorted((cache / "model").glob(f"{station}_*.json"))), None)
        if model_path is None:
            continue
        tz = ZoneInfo(meta["timezone"])
        model = bc.load_model(model_path)
        radiation_by_day = {}
        for local_day in sorted({t.astimezone(tz).date() for t in model}):
            start = datetime.combine(local_day, wall_time.min, tzinfo=tz).astimezone(timezone.utc)
            day = bc.model_day(model, start)
            if day is None:
                continue
            radiation_by_day[local_day.isoformat()] = sum(
                h["sw"] for h in day["hours"][1:]
            ) * 3600.0 / 1e6
        out[station] = {"lat": meta["latitude"], "radiation": radiation_by_day,
                        "days": sorted(radiation_by_day)}
    return out


def history_terms(series: dict, event_date: str) -> dict | None:
    """z and the history's mean of z+ and z-, with the production arithmetic
    (``forecast_clearness_shift_sigma``: population sd, days strictly before)."""

    days = series["days"]
    if event_date not in series["radiation"]:
        return None
    position = days.index(event_date)
    lat = series["lat"]
    values = []
    for d in days[max(0, position - bc.HISTORY_DAYS):position]:
        kt = clearness_index(series["radiation"][d], lat, d)
        if kt is not None:
            values.append(kt)
    if len(values) < 30:
        return None
    mean = sum(values) / len(values)
    sd = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    if sd <= 0.0:
        return None
    today = series["radiation"][event_date] / extraterrestrial_radiation_mj(lat, event_date)
    zs = [(v - mean) / sd for v in values]
    # What production serves for this day (charter 222's centred pair), to
    # check this study scores exactly that.
    production, _receipt = forecast_clearness_shift_sigma(
        radiation_sum=series["radiation"][event_date], event_date=event_date,
        latitude_deg=lat,
        station_history=[(d, series["radiation"][d])
                         for d in days[max(0, position - bc.HISTORY_DAYS):position]],
        shift_sigma_per_unit=WARM, cold_shift_sigma_per_unit=COLD,
    )
    return {
        "z": (today - mean) / sd,
        "mean_zp": sum(max(z, 0.0) for z in zs) / len(zs),
        "mean_zn": sum(min(z, 0.0) for z in zs) / len(zs),
        "production_shift": production,
    }


def rows_for(days: list[dict], series: dict[str, dict], source: str) -> tuple[list[dict], float, float]:
    m.P0_SOURCE = source
    roots = m.p0_roots(days)
    rows, worst, worst_production = [], 0.0, 0.0
    for d in days:
        root = roots.get((d["station"], d["date"]))
        if root is None or d["station"] not in series:
            continue
        terms = history_terms(series[d["station"]], d["date"])
        if terms is None:
            continue
        cached = m._num(d.get("kt_z"))
        if cached is not None:
            worst = max(worst, abs(cached - terms["z"]))
        centred = pair(terms["z"]) - (WARM * terms["mean_zp"] + COLD * terms["mean_zn"])
        worst_production = max(worst_production, abs(terms["production_shift"] - centred))
        rows.append({
            "station": d["station"], "date": d["date"], "unit": d["unit"],
            "truth_rep": d["truth_rep"], "truth_c": d["truth_c"],
            "c0": root["c0"], "sigma": root["sigma"], **terms,
        })
    return rows, worst, worst_production


def arms(warm: float, cold: float) -> dict:
    def centred(r):
        return pair(r["z"], warm, cold) - (warm * r["mean_zp"] + cold * r["mean_zn"])

    return {
        "none": lambda r: 0.0,
        "served_symmetric_0.07": lambda r: 0.07 * r["z"],
        "pair_uncentred": lambda r: pair(r["z"], warm, cold),
        "pair_centred": centred,
    }


def score(rows: list[dict], shifts: dict, *, intercept: bool) -> dict:
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(row)
    scores = {}
    for arm, shift in shifts.items():
        arm_scores = {}
        if intercept:
            X = np.ones((len(rows), 1))
            y = np.array([(r["truth_c"] - r["c0"]) / r["sigma"] - shift(r) for r in rows])
            wf = m.WalkForward([r["date"] for r in rows], X, y)
        for day in sorted(by_day):
            offset = 0.0
            if intercept:
                fitted = wf.fit_before(day)
                if fitted is None:
                    continue
                offset = float(fitted[0][0])
            for r in by_day[day]:
                arm_scores[(r["station"], day)] = m.score_gaussian(
                    r["c0"] + (shift(r) + offset) * r["sigma"], r["sigma"], r["truth_rep"], r["unit"]
                )
        scores[arm] = arm_scores
    base = scores["served_symmetric_0.07"]
    out = {
        f"{arm} mean shift sigma": round(float(np.mean([shifts[arm](r) for r in rows])), 4)
        for arm in shifts
    }
    for arm in shifts:
        if arm == "served_symmetric_0.07":
            continue
        keys = sorted(set(scores[arm]) & set(base))
        out[f"{arm} vs served_symmetric_0.07 logscore"] = m.summarise(
            [(k[1], scores[arm][k][0] - base[k][0]) for k in keys]
        )
        out[f"{arm} vs served_symmetric_0.07 brier"] = m.summarise(
            [(k[1], base[k][1] - scores[arm][k][1]) for k in keys]
        )
    if "pair_centred" in scores and "pair_uncentred" in scores:
        keys = sorted(set(scores["pair_centred"]) & set(scores["pair_uncentred"]))
        out["pair_centred vs pair_uncentred logscore"] = m.summarise(
            [(k[1], scores["pair_centred"][k][0] - scores["pair_uncentred"][k][0]) for k in keys]
        )
        out["pair_centred vs pair_uncentred brier"] = m.summarise(
            [(k[1], scores["pair_uncentred"][k][1] - scores["pair_centred"][k][1]) for k in keys]
        )
    return out


def centred_arm(warm: float, cold: float):
    return lambda r: pair(r["z"], warm, cold) - (warm * r["mean_zp"] + cold * r["mean_zn"])


def cross_half(rows: list[dict]) -> dict:
    """Out of sample by time: the fixed-effects pair fitted on one half of
    the days (each side floored at 0 -- a cloudier day is not a warmer one)
    and scored, centred and with no intercept, on the other half, beside the
    fixed 0.12 / 0.04 and a grid of fixed pairs on the same half."""

    dates = sorted({r["date"] for r in rows})
    middle = dates[len(dates) // 2]
    halves = ([r for r in rows if r["date"] < middle], [r for r in rows if r["date"] >= middle])
    out = {}
    for name, fit_rows, test_rows in (
        ("fit_first_score_second", halves[0], halves[1]),
        ("fit_second_score_first", halves[1], halves[0]),
    ):
        fitted = fit_fixed_effects(fit_rows, boot=0)
        warm, cold = max(fitted["warm"][0], 0.0), max(fitted["cold"][0], 0.0)
        shifts = {
            "served_symmetric_0.07": lambda r: 0.07 * r["z"],
            "pair_centred_0.12_0.04": centred_arm(0.12, 0.04),
            "pair_centred_fitted_on_other_half": centred_arm(warm, cold),
        }
        for grid_warm, grid_cold in ((0.16, 0.02), (0.20, 0.0), (0.24, 0.0)):
            shifts[f"pair_centred_{grid_warm:g}_{grid_cold:g}"] = centred_arm(grid_warm, grid_cold)
        out[name] = {
            "fitted_pair_floored": [round(warm, 4), round(cold, 4)],
            "scores": score(test_rows, shifts, intercept=False),
        }
    return out


def fit_fixed_effects(rows: list[dict], *, boot: int = BOOT) -> dict:
    """Charter 210's objective with the regressors de-meaned per station."""

    by_station = defaultdict(list)
    for r in rows:
        by_station[r["station"]].append(r)
    W, C, S, Y, D = [], [], [], [], []
    for items in by_station.values():
        if len(items) < 8:
            continue
        w = np.array([max(r["z"], 0.0) * r["sigma"] for r in items])
        c = np.array([min(r["z"], 0.0) * r["sigma"] for r in items])
        s = np.array([r["z"] * r["sigma"] for r in items])
        y = np.array([r["truth_c"] - r["c0"] for r in items])
        W.extend(w - w.mean())
        C.extend(c - c.mean())
        S.extend(s - s.mean())
        Y.extend(y - y.mean())
        D.extend(r["date"] for r in items)
    W, C, S, Y = map(np.array, (W, C, S, Y))

    def solve(ix):
        A = np.column_stack([W[ix], C[ix]])
        beta, *_ = np.linalg.lstsq(A, Y[ix], rcond=None)
        sym = float(S[ix] @ Y[ix] / (S[ix] @ S[ix]))
        return float(beta[0]), float(beta[1]), sym

    index = np.arange(len(Y))
    point = solve(index)
    by_day = defaultdict(list)
    for i, d in enumerate(D):
        by_day[d].append(i)
    days = sorted(by_day)
    out = {"station_days": int(len(Y)), "days": len(days)}
    if not boot:
        for k, name in enumerate(("warm", "cold", "symmetric")):
            out[name] = [round(point[k], 4)]
        return out
    rng = random.Random(1)
    draws = np.array([
        solve(np.array([i for d in (rng.choice(days) for _ in days) for i in by_day[d]]))
        for _ in range(boot)
    ])
    for k, name in enumerate(("warm", "cold", "symmetric")):
        lo, hi = np.percentile(draws[:, k], [2.5, 97.5])
        out[name] = [round(point[k], 4), round(float(lo), 4), round(float(hi), 4)]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    cache = Path(args.cache_dir)
    days = m.load_jsonl(cache / "derived" / "days.jsonl")
    series = station_series(cache)
    result = {}
    for source in ("global", "best_match"):
        rows, worst, worst_production = rows_for(days, series, source)
        window = [r for r in rows if "2026-08-16" <= r["date"] <= "2026-09-24"]
        fit = fit_fixed_effects(rows)
        block = {
            "rows": len(rows),
            "max_abs_z_difference_vs_cached": worst,
            "max_abs_centred_shift_difference_vs_production": worst_production,
            "mean_centre_sigma": round(float(np.mean(
                [WARM * r["mean_zp"] + COLD * r["mean_zn"] for r in rows])), 4),
            "fit_fixed_effects": fit,
            "fit_fixed_effects_production_window_0816_0924": fit_fixed_effects(window),
            "no_intercept": score(rows, arms(WARM, COLD), intercept=False),
            "no_intercept_production_window_0816_0924": score(window, arms(WARM, COLD), intercept=False),
            "fleet_intercept_walk_forward": score(rows, arms(WARM, COLD), intercept=True),
            "cross_half": cross_half(rows),
        }
        result[source] = block
        print(source, json.dumps({k: v for k, v in block.items() if not isinstance(v, dict)}), flush=True)
    Path(args.out).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
