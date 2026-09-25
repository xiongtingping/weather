#!/usr/bin/env python3
"""Does P0's radiation term carry its information on the warm side, the cold side, or both?

User 2026-09-25: 「为什么只有降温，没有升温」.  The served term is symmetric --
0.07 x Kt z x sigma, warm when the day is forecast clearer than the station's
own last 180 days, cold when cloudier -- but Kt has a ceiling (a sky cannot be
clearer than clear) and a long overcast tail, so its warm shifts are small and
its cold shifts large.  This asks which side the residual actually follows.

Residual r0 = (truth - c0) / sigma on the P0 proxy *before* the radiation
shift (measure.p0_roots), regressed on z+ = max(z, 0) and z- = min(z, 0).
Fits are reported in-sample with a day-block bootstrap, and every arm is also
scored walk-forward (d - 2) on the settled bucket, as in measure.py.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

import numpy as np

import measure as m

BOOT = 400


def residual_rows(days: list[dict], source: str) -> list[dict]:
    m.P0_SOURCE = source
    roots = m.p0_roots(days)
    rows = []
    for d in days:
        root = roots.get((d["station"], d["date"]))
        z = m._num(d.get("kt_z"))
        if root is None or z is None:
            continue
        rows.append({
            "date": d["date"], "station": d["station"], "unit": d["unit"],
            "truth_rep": d["truth_rep"], "lat": d.get("lat"),
            "c0": root["c0"], "sigma": root["sigma"], "z": z,
            "r0": (d["truth_c"] - root["c0"]) / root["sigma"],
        })
    return rows


def fit(rows: list[dict]) -> np.ndarray:
    z = np.array([r["z"] for r in rows])
    A = np.column_stack([np.ones(len(rows)), np.clip(z, 0, None), np.clip(z, None, 0)])
    beta, *_ = np.linalg.lstsq(A, np.array([r["r0"] for r in rows]), rcond=None)
    return beta


def fit_with_ci(rows: list[dict]) -> dict:
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(row)
    days = sorted(by_day)
    rng = random.Random(11)
    draws = []
    for _ in range(BOOT):
        sample = [row for _ in days for row in by_day[days[rng.randrange(len(days))]]]
        draws.append(fit(sample))
    draws = np.array(draws)
    beta = fit(rows)
    out = {"n": len(rows), "days": len(days)}
    for name, col in (("warm_side_per_z", 1), ("cold_side_per_z", 2)):
        lo, hi = np.percentile(draws[:, col], [2.5, 97.5])
        out[name] = [round(float(beta[col]), 4), round(float(lo), 4), round(float(hi), 4)]
    return out


ARMS = {
    # name: (fitted columns, fixed term) -- the intercept is fitted in every arm.
    "none": ((), None),
    "served_symmetric_0.07": ((), lambda z: 0.07 * z),
    "fitted_symmetric": (("z",), None),
    "fitted_asymmetric": (("zp", "zn"), None),
    "warm_side_only": (("zp",), None),
    # The pair charter 222 fits (fit_two_sided.py, charter 210's objective),
    # fixed, not refitted -- but uncentred, and with the fleet intercept
    # every arm here gets.  Serving centres it; centered_pair.py scores that.
    "pair_0.12_0.04_uncentred": ((), lambda z: 0.12 * max(z, 0.0) + 0.04 * min(z, 0.0)),
}


def walk_forward(rows: list[dict]) -> dict:
    def columns(row: dict, names) -> list[float]:
        z = row["z"]
        values = {"z": z, "zp": max(z, 0.0), "zn": min(z, 0.0)}
        return [1.0] + [values[n] for n in names]

    by_day = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(row)
    scores = {}
    for arm, (names, fixed) in ARMS.items():
        X = np.array([columns(r, names) for r in rows])
        y = np.array([r["r0"] - (fixed(r["z"]) if fixed else 0.0) for r in rows])
        wf = m.WalkForward([r["date"] for r in rows], X, y)
        arm_scores = {}
        for day in sorted(by_day):
            fitted = wf.fit_before(day)
            if fitted is None:
                continue
            beta = fitted[0]
            for row in by_day[day]:
                shift = float(np.dot(beta, columns(row, names))) + (fixed(row["z"]) if fixed else 0.0)
                arm_scores[(row["station"], day)] = m.score_gaussian(
                    row["c0"] + shift * row["sigma"], row["sigma"], row["truth_rep"], row["unit"]
                )
        scores[arm] = arm_scores
    out = {}
    for arm in ARMS:
        if arm == "served_symmetric_0.07":
            continue
        keys = sorted(set(scores[arm]) & set(scores["served_symmetric_0.07"]))
        out[f"{arm} vs served_symmetric_0.07 logscore"] = m.summarise(
            [(k[1], scores[arm][k][0] - scores["served_symmetric_0.07"][k][0]) for k in keys]
        )
        out[f"{arm} vs served_symmetric_0.07 brier"] = m.summarise(
            [(k[1], scores["served_symmetric_0.07"][k][1] - scores[arm][k][1]) for k in keys]
        )
    return out


FIXED = {
    "none": lambda z: 0.0,
    "served_symmetric_0.07": lambda z: 0.07 * z,
    "pair_0.12_0.04_uncentred": lambda z: 0.12 * max(z, 0.0) + 0.04 * min(z, 0.0),
}


def fixed_no_intercept(rows: list[dict]) -> dict:
    """The fixed arms exactly as serving applies them: nothing refitted.

    ``walk_forward`` fits a fleet intercept in every arm, which absorbs any
    net drift an arm adds.  Serving has no such term -- its bias layer is the
    station's own walk-forward bias, already in the proxy's ``c0`` -- and the
    uncentred pair does add one: z > 0 on 59% of station-days, so the warm
    side's larger coefficient moves the fleet mean warm where the symmetric
    0.07 nets to about zero.  So the arms are also scored bare; this is what
    showed the pair must be centred (``centered_pair.py``).
    """

    scores = {arm: {} for arm in FIXED}
    for row in rows:
        for arm, shift in FIXED.items():
            scores[arm][(row["station"], row["date"])] = m.score_gaussian(
                row["c0"] + shift(row["z"]) * row["sigma"], row["sigma"], row["truth_rep"], row["unit"]
            )
    out = {
        f"{arm} mean shift": {
            "sigma": round(float(np.mean([FIXED[arm](r["z"]) for r in rows])), 4),
            "c": round(float(np.mean([FIXED[arm](r["z"]) * r["sigma"] for r in rows])), 4),
        }
        for arm in FIXED
    }
    base = scores["served_symmetric_0.07"]
    for arm in ("none", "pair_0.12_0.04_uncentred"):
        keys = sorted(set(scores[arm]) & set(base))
        out[f"{arm} vs served_symmetric_0.07 logscore"] = m.summarise(
            [(k[1], scores[arm][k][0] - base[k][0]) for k in keys]
        )
        out[f"{arm} vs served_symmetric_0.07 brier"] = m.summarise(
            [(k[1], base[k][1] - scores[arm][k][1]) for k in keys]
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    days = m.load_jsonl(Path(args.cache_dir) / "derived" / "days.jsonl")
    result = {}
    for source in ("global", "best_match"):
        rows = residual_rows(days, source)
        dates = sorted({r["date"] for r in rows})
        middle = dates[len(dates) // 2]
        z = np.array([r["z"] for r in rows])
        shift = 0.07 * z
        result[source] = {
            "kt_z": {
                "median": round(float(np.median(z)), 3),
                "skew": round(float(((z - z.mean()) ** 3).mean() / z.std() ** 3), 3),
                "share_warm": round(float((z > 0).mean()), 3),
                "served_shift_sigma_warm_mean": round(float(shift[shift > 0].mean()), 4),
                "served_shift_sigma_cold_mean": round(float(shift[shift < 0].mean()), 4),
            },
            "fit_all": fit_with_ci(rows),
            "fit_first_half": fit_with_ci([r for r in rows if r["date"] < middle]),
            "fit_second_half": fit_with_ci([r for r in rows if r["date"] >= middle]),
            "fit_production_window_0816_0924": fit_with_ci(
                [r for r in rows if "2026-08-16" <= r["date"] <= "2026-09-24"]
            ),
            "fit_southern_or_tropics": fit_with_ci(
                [r for r in rows if r["lat"] is not None and float(r["lat"]) < 23.5]
            ),
            "fit_northern_extratropics": fit_with_ci(
                [r for r in rows if r["lat"] is not None and float(r["lat"]) >= 23.5]
            ),
            "walk_forward": walk_forward(rows),
            "fixed_no_intercept": fixed_no_intercept(rows),
            "fixed_no_intercept_production_window_0816_0924": fixed_no_intercept(
                [r for r in rows if "2026-08-16" <= r["date"] <= "2026-09-24"]
            ),
        }
    Path(args.out).write_text(json.dumps(result, indent=1, sort_keys=True))
    print(json.dumps(result, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
