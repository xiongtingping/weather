#!/usr/bin/env python3
"""Would splitting the layer-1 scope by another dimension earn its place?

The scope key -- system, station, season, lead bucket, run cycle, settlement
definition -- decides which station-days share a fitted parameter set.  Adding
a dimension to it is not free: every cell it creates is a cell with fewer days
in it, and the median cell already holds only 38.  A dimension pays only if
the structure it separates is worth more than the sample it costs.

So this fits the same data both ways on the same chronological split -- one
parameter set per existing cell, against one per (cell x new dimension) -- and
scores both on the days neither saw.  Splitting wins only if it wins out of
sample.

The resampling unit is the day.  Forty-five stations share one atmosphere, so
their errors on a date are anything but independent; resampling station-days
would shrink the interval by roughly the square root of the station count and
manufacture significance.

Two lanes, because the two candidate dimensions have different evidence:

``research`` pairs the multi-year archived point forecasts with the same-day
climatology record -- 38k station-days over all twelve months.  This is the
only lane that can speak to a monthly split at all: production truth begins
2026-06-28 and is entirely one season.

``production`` reads the real member archives through the same loader the OOS
builder uses.  It is the pipeline's own data, and it is thin.

Read-only.  Places no orders and writes nothing but its report.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    _load_cases,
    _season_bin,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
# Below this a cell cannot support even a mean and a spread; a split that
# starves its cells is refused rather than scored, because the comparison
# would then be about the floor and not about the dimension.
MIN_CELL_TRAIN_DAYS = 8
LN2 = math.log(2.0)


def _gaussian_nll(actual: float, centre: float, sigma: float) -> float:
    scale = max(float(sigma), 0.25)
    return (
        0.5 * math.log(2.0 * math.pi * scale * scale)
        + (float(actual) - float(centre)) ** 2 / (2.0 * scale * scale)
    )


def _fit(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """Bias and residual sigma -- layer 2's two scalars, reduced to a point."""

    errors = [actual - forecast for forecast, actual in pairs]
    bias = statistics.mean(errors)
    if len(errors) < 2:
        return bias, 1.0
    sigma = math.sqrt(
        sum((error - bias) ** 2 for error in errors) / len(errors)
    )
    return bias, max(sigma, 0.25)


def _bootstrap_by_day(
    by_day: dict[str, list[float]], *, draws: int, seed: int
) -> dict[str, float]:
    days = sorted(by_day)
    observed = statistics.mean(v for day in days for v in by_day[day])
    if len(days) < 2:
        return {
            "mean": observed,
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "day_count": len(days),
        }
    rng = random.Random(seed)
    means: list[float] = []
    for _draw in range(draws):
        pooled: list[float] = []
        for _index in range(len(days)):
            pooled.extend(by_day[days[rng.randrange(len(days))]])
        if pooled:
            means.append(statistics.mean(pooled))
    means.sort()
    return {
        "mean": observed,
        "ci_low": means[int(0.025 * (len(means) - 1))],
        "ci_high": means[int(0.975 * (len(means) - 1))],
        "day_count": len(days),
    }


def _research_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.station_id, r.local_date, r.forecast_tmax_c,"
        " c.daily_high_c"
        " FROM research_station_forecast_tmax_history r"
        " JOIN calibration_station_same_day_climatology c"
        "  ON c.station_id=r.station_id AND c.local_date=r.local_date"
    ).fetchall()
    return [
        {
            "cell": str(station),
            "day": str(day),
            "forecast": float(forecast),
            "actual": float(actual),
        }
        for station, day, forecast, actual in rows
        if forecast is not None and actual is not None
    ]


def _production_records(
    conn: sqlite3.Connection, *, stations: list[str], as_of_utc: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for station in stations:
        try:
            cases = _load_cases(conn, station_id=station, as_of_utc=as_of_utc)
        except Exception:  # noqa: BLE001 -- a station without cases is skipped
            continue
        for (system, season, lead, cycle), rows in cases.items():
            for case in rows:
                values = [float(value) for value in case["values"]]
                if not values:
                    continue
                out.append({
                    "cell": f"{station}|{system}|{season}|{lead}|{cycle}",
                    "station_id": station,
                    "day": str(case["event_date"]),
                    "forecast": sum(values) / len(values),
                    "actual": float(case["actual_tmax_c"]),
                })
    return out


def _month_label(record: dict[str, Any], _labels: Any) -> str:
    return record["day"][5:7]


def _season_label(record: dict[str, Any], _labels: Any) -> str:
    return _season_bin(record["day"])


def _weather_label(record: dict[str, Any], labels: Any) -> str | None:
    return labels.get((record.get("station_id"), record["day"]))


def _weather_labels(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Tercile of the station's own forecast radiation.

    Station-local because absolute radiation is mostly latitude and season;
    what a regime label has to separate is the day being brighter or duller
    *for this station*.  Knowable before the day, since it is a forecast --
    a scope dimension that could only be labelled afterwards could not be a
    scope dimension.
    """

    by_station: dict[str, list[tuple[str, float]]] = collections.defaultdict(
        list
    )
    for station, day, radiation in conn.execute(
        "SELECT station_id, local_date, shortwave_radiation_sum"
        " FROM calibration_station_forecast_physics"
        " WHERE shortwave_radiation_sum IS NOT NULL"
    ):
        by_station[str(station)].append((str(day), float(radiation)))
    labels: dict[tuple[str, str], str] = {}
    for station, series in by_station.items():
        if len(series) < 9:
            continue
        ordered = sorted(series, key=lambda item: item[1])
        third = len(ordered) // 3
        for index, (day, _value) in enumerate(ordered):
            if index < third:
                labels[(station, day)] = "DULL"
            elif index < 2 * third:
                labels[(station, day)] = "MID"
            else:
                labels[(station, day)] = "BRIGHT"
    return labels


LABELLERS = {
    "month": _month_label,
    "season": _season_label,
    "weather": _weather_label,
}


def evaluate(
    records: list[dict[str, Any]],
    *,
    dimension: str,
    holdout_fraction: float,
    draws: int,
    seed: int,
) -> dict[str, Any]:
    labeller = LABELLERS[dimension]
    days = sorted({record["day"] for record in records})
    cut = days[int(len(days) * (1.0 - holdout_fraction))]
    train = [record for record in records if record["day"] < cut]
    holdout = [record for record in records if record["day"] >= cut]

    pooled: dict[str, list[tuple[float, float]]] = collections.defaultdict(
        list
    )
    split: dict[tuple[str, str], list[tuple[float, float]]] = (
        collections.defaultdict(list)
    )
    for record in train:
        label = record["label"]
        if label is None:
            continue
        pair = (record["forecast"], record["actual"])
        pooled[record["cell"]].append(pair)
        split[(record["cell"], label)].append(pair)

    pooled_fit = {
        cell: _fit(pairs)
        for cell, pairs in pooled.items()
        if len(pairs) >= MIN_CELL_TRAIN_DAYS
    }
    split_fit = {
        key: _fit(pairs)
        for key, pairs in split.items()
        if len(pairs) >= MIN_CELL_TRAIN_DAYS
    }

    by_day: dict[str, list[float]] = collections.defaultdict(list)
    scored = starved = 0
    for record in holdout:
        label = record["label"]
        if label is None:
            continue
        base = pooled_fit.get(record["cell"])
        if base is None:
            continue
        fine = split_fit.get((record["cell"], label))
        if fine is None:
            starved += 1
            continue
        scored += 1
        pooled_nll = _gaussian_nll(
            record["actual"], record["forecast"] + base[0], base[1]
        )
        split_nll = _gaussian_nll(
            record["actual"], record["forecast"] + fine[0], fine[1]
        )
        by_day[record["day"]].append((pooled_nll - split_nll) / LN2)

    gain = (
        _bootstrap_by_day(dict(by_day), draws=draws, seed=seed)
        if by_day
        else {"mean": 0.0, "ci_low": float("nan"),
              "ci_high": float("nan"), "day_count": 0}
    )
    return {
        "dimension": dimension,
        "label_count": len({
            record["label"] for record in records
            if record["label"] is not None
        }),
        "train_day_count": len({record["day"] for record in train}),
        "holdout_day_count": len({record["day"] for record in holdout}),
        "pooled_cell_count": len(pooled_fit),
        "split_cell_count": len(split_fit),
        # Over the cells that cleared the floor and were actually fitted, not
        # over every cell the grouping produced: a median dragged down by
        # cells nothing was fitted from describes the grouping, not the fit.
        "median_pooled_cell_days": (
            statistics.median(len(pooled[cell]) for cell in pooled_fit)
            if pooled_fit else 0
        ),
        "median_split_cell_days": (
            statistics.median(len(split[key]) for key in split_fit)
            if split_fit else 0
        ),
        "scored_station_days": scored,
        # A split cell too thin to fit is the cost of the dimension made
        # visible: those station-days keep the pooled fit in any real
        # deployment, so a dimension that starves most of the network has
        # bought nothing even where it wins.
        "starved_station_days": starved,
        "split_minus_pooled_bits": gain,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--lane", choices=("research", "production"), default="research"
    )
    parser.add_argument(
        "--dimension",
        action="append",
        choices=sorted(LABELLERS),
        help="repeatable; default month and season",
    )
    parser.add_argument("--holdout-fraction", type=float, default=0.3)
    parser.add_argument("--bootstrap-draws", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--as-of-utc", default="2026-08-18T00:00:00Z")
    parser.add_argument("--station", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    dimensions = args.dimension or ["month", "season"]

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60.0)
    try:
        if args.lane == "research":
            records = _research_records(conn)
        else:
            stations = args.station or [
                str(row[0]) for row in conn.execute(
                    "SELECT DISTINCT station_id"
                    " FROM calibration_native_ensemble_system_artifact"
                )
            ]
            records = _production_records(
                conn, stations=stations, as_of_utc=args.as_of_utc
            )
        labels = _weather_labels(conn) if "weather" in dimensions else {}
    finally:
        conn.close()

    results = {}
    for dimension in dimensions:
        labeller = LABELLERS[dimension]
        for record in records:
            record["label"] = labeller(record, labels)
        results[dimension] = evaluate(
            records,
            dimension=dimension,
            holdout_fraction=args.holdout_fraction,
            draws=args.bootstrap_draws,
            seed=args.seed,
        )

    report = {
        "schema": "layer1_scope_dimension_audit_v1",
        "lane": args.lane,
        "record_count": len(records),
        "resampling_unit": "event_date",
        "min_cell_train_days": MIN_CELL_TRAIN_DAYS,
        "dimensions": results,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
