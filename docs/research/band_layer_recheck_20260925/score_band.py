#!/usr/bin/env python3
"""PROTOCOL.md: fit the band table walk-forward with production's own report, apply, score.

For every test day D the table is fitted by ``band_lane_report`` on the
station-days before D -- each station-day's FIRST update, as the lane reads
it -- with the served parameters (distance cells, 1,000 two-level bootstrap
draws, 30 stations, the 95% end nearest one), then applied by production's
``apply_band_reliability`` to every update of day D.  Three arms: identity,
the production rule, and the point estimates.  Station-day weights, three-day
block bootstrap.  Writes results.json.
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import band_reliability_store as BRS  # noqa: E402
from bayes_basket_target.cross_consensus import apply_band_reliability  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

ARMS = ("identity", "production", "point")
SUBPERIOD = ("2026-09-01", "2026-09-23")
BLOCK, BOOT = 3, 2000


def load():
    days = []
    for path in sorted((HERE / "pass1").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            days.extend(json.loads(line) for line in handle)
    return days


def score(vec, keys, hit):
    floored, _ = floor_probabilities({key: float(value) for key, value in zip(keys, vec)})
    values = [floored[key] for key in keys]
    brier = sum((value - (1.0 if index == hit else 0.0)) ** 2 for index, value in enumerate(values))
    return brier, -math.log(max(values[hit], 1e-12))


def banded(vec, keys, factors):
    probability = {key: float(value) for key, value in zip(keys, vec)}
    if all(value == 1.0 for _band, value in factors):
        return vec
    served, _receipt = apply_band_reliability(
        probability, factors=factors, ordinals={key: index for index, key in enumerate(keys)})
    return [served[key] for key in keys]


def fit_tables(prior_by_day, prior_stations):
    report = BRS.band_lane_report(
        prior_by_day, bands=BRS.BAND_DISTANCE_CELLS, bootstrap_samples=1000,
        axis=BRS.BAND_AXIS_CENTRE_DISTANCE, interval_alpha=BRS.DEFAULT_INTERVAL_ALPHA,
        stations_by_day=prior_stations, min_cell_stations=BRS.MIN_CELL_STATIONS)
    production, point = [], []
    for band in BRS.BAND_DISTANCE_CELLS:
        cell = report.get(band)
        production.append((band, float(cell["conservative_factor"]) if cell and cell["qualified"] else 1.0))
        point.append((band, min(BRS.FACTOR_CEILING, max(BRS.FACTOR_FLOOR, float(cell["ratio"]))) if cell else 1.0))
    return tuple(production), tuple(point), report


def compare(rows, base, test):
    count = defaultdict(int)
    for r in rows:
        count[(r["station"], r["event_date"])] += 1
    w = np.array([1.0 / count[(r["station"], r["event_date"])] for r in rows])
    d = np.array([[t - b for t, b in zip(r[test], r[base])] for r in rows])
    days = sorted({r["event_date"] for r in rows})
    by_day = defaultdict(list)
    for i, r in enumerate(rows):
        by_day[r["event_date"]].append(i)
    rng = np.random.default_rng(20260925)
    n_blocks = math.ceil(len(days) / BLOCK)
    boot = np.array([
        (w[idx, None] * d[idx]).sum(axis=0) / w[idx].sum()
        for idx in (np.concatenate([by_day[days[(s + j) % len(days)]]
                                    for s in rng.integers(0, len(days), n_blocks) for j in range(BLOCK)])
                    for _ in range(BOOT))])
    delta = (w[:, None] * d).sum(axis=0) / w.sum()
    lo, hi = np.percentile(boot, 2.5, axis=0), np.percentile(boot, 97.5, axis=0)
    return {"updates": len(rows), "station_days": len(count), "days": len(days),
            "d_brier": float(delta[0]), "ci_brier": [float(lo[0]), float(hi[0])],
            "d_logscore": float(delta[1]), "ci_logscore": [float(lo[1]), float(hi[1])],
            "both_lower": bool(hi[0] < 0 and hi[1] < 0)}


def main():
    station_days = load()
    by_date = defaultdict(list)
    for sd in station_days:
        by_date[sd["event_date"]].append(sd)
    dates = sorted(by_date)
    fit_records, fit_stations = {}, {}
    for day in dates:
        rows, names = [], []
        for sd in by_date[day]:
            first = min(sd["updates"], key=lambda u: u["at"])
            rows.append([(float(p), 1 if index == sd["hit"] else 0) for index, p in enumerate(first["vec"])])
            names.append(sd["station"])
        fit_records[day], fit_stations[day] = rows, names
    scored, tables = [], {}
    for position, day in enumerate(dates):
        prior = dates[:position]
        if len(prior) < BRS.MIN_QUALIFYING_DAYS:
            continue
        production, point, report = fit_tables({d: fit_records[d] for d in prior},
                                               {d: fit_stations[d] for d in prior})
        tables[day] = {"production": [f for _b, f in production], "point": [round(f, 4) for _b, f in point],
                       "qualified": [bool(report.get(b, {}).get("qualified")) for b in BRS.BAND_DISTANCE_CELLS]}
        for sd in by_date[day]:
            for update in sd["updates"]:
                vec = update["vec"]
                scored.append({
                    "station": sd["station"], "event_date": day,
                    "identity": score(vec, sd["keys"], sd["hit"]),
                    "production": score(banded(vec, sd["keys"], production), sd["keys"], sd["hit"]),
                    "point": score(banded(vec, sd["keys"], point), sd["keys"], sd["hit"]),
                })
        print(day, "prod", tables[day]["production"], "point", tables[day]["point"], flush=True)
    sub = [r for r in scored if SUBPERIOD[0] <= r["event_date"] <= SUBPERIOD[1]]
    result = {
        "protocol": "PROTOCOL.md", "station_days": len(station_days), "test_days": sorted(tables),
        "tables": tables,
        "production_nonidentity_days": sum(1 for t in tables.values() if any(f != 1.0 for f in t["production"])),
        "all": {"production_vs_identity": compare(scored, "identity", "production"),
                "point_vs_identity": compare(scored, "identity", "point")},
        "sep": {"production_vs_identity": compare(sub, "identity", "production"),
                "point_vs_identity": compare(sub, "identity", "point")},
    }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))

    def line(label, c):
        return (f"{label}: {c['station_days']} 站日 {c['days']} 天 | Brier {c['d_brier']:+.5f} [{c['ci_brier'][0]:+.5f},{c['ci_brier'][1]:+.5f}]"
                f" | LogScore {c['d_logscore']:+.5f} [{c['ci_logscore'][0]:+.5f},{c['ci_logscore'][1]:+.5f}] | both lower={c['both_lower']}")

    print(f"test days {len(tables)} ({min(tables)}..{max(tables)}); production table non-identity on {result['production_nonidentity_days']} days")
    for period in ("all", "sep"):
        for key in ("production_vs_identity", "point_vs_identity"):
            print(line(f"{period} {key}", result[period][key]))


if __name__ == "__main__":
    main()
