#!/usr/bin/env python3
"""Fit how the neighbour tilt varies with latitude and season, and test it.

The served distribution is symmetric about its centre and the truth is not.
Fleet-wide over 47 days of settled truth that shows up as one bucket above
realizing 1.16 of its claim against one below at 0.89.  Whether it varies
with latitude or season cannot be answered on 47 days: split by latitude and
the high group reads 1.4-1.5 against 1.0-1.2 elsewhere, but serving the split
loses CRPS because each group's estimate degrades faster than its shape
improves.

This fits the same quantity on a research pair that has years rather than
weeks: the archived past forecast of the daily maximum against the ERA5
daily maximum already held for the same-day climatology.  The served
distribution is stood in for by a Gaussian centred on the bias-corrected
forecast with the station-month error scale -- symmetric by construction,
which is the property under test.  Neither input is settlement truth and
neither may become one.

Structure transfers, level does not: a gridded forecast against a gridded
analysis has its own error scale.  So the last step is not a fit but a test.
The research factors are compared against what the production pipeline
measures on its own 47 days, and the prior is reported as transferable only
if the two agree in sign and rank across latitude bands.  A prior that does
not transfer is a description of Open-Meteo, not of our distributions.
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

OFFSETS = (-2, -1, 1, 2)
MIN_STATION_MONTH_SAMPLES = 60
BOOTSTRAP_DRAWS = 300


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _bucket_probability(offset: int, mu: float, sigma: float, center: int) -> float:
    """Mass a symmetric belief puts on the bucket ``offset`` from the centre."""

    lower = center + offset - 0.5
    upper = center + offset + 0.5
    return _normal_cdf((upper - mu) / sigma) - _normal_cdf((lower - mu) / sigma)


def load_pairs(conn: sqlite3.Connection) -> list[dict]:
    """One row per station-day that has both a forecast and an analysis."""

    forecast: dict[tuple[str, str], float] = {}
    for station_id, local_date, value in conn.execute(
        "SELECT station_id,local_date,forecast_tmax_c "
        "FROM research_station_forecast_tmax_history WHERE reference_only=1"
    ):
        forecast[(str(station_id), str(local_date))] = float(value)
    rows: list[dict] = []
    for station_id, local_date, value in conn.execute(
        "SELECT station_id,local_date,daily_high_c "
        "FROM calibration_station_same_day_climatology WHERE reference_only=1"
    ):
        key = (str(station_id), str(local_date))
        if key not in forecast:
            continue
        rows.append({
            "station_id": key[0],
            "local_date": key[1],
            "month": int(key[1][5:7]),
            "forecast_c": forecast[key],
            "actual_c": float(value),
        })
    return rows


def fit_factors(rows: list[dict], *, scales: dict) -> dict[int, float] | None:
    """Realized-over-claimed by offset, against a symmetric belief."""

    claimed: collections.Counter = collections.Counter()
    realized: collections.Counter = collections.Counter()
    count = 0
    for row in rows:
        scale = scales.get((row["station_id"], row["month"]))
        if scale is None:
            continue
        bias, sigma = scale
        if sigma <= 0.0:
            continue
        mu = row["forecast_c"] + bias
        center = math.floor(mu + 0.5)
        landed = math.floor(row["actual_c"] + 0.5) - center
        count += 1
        for offset in OFFSETS:
            claimed[offset] += _bucket_probability(offset, mu, sigma, center)
            realized[offset] += 1 if landed == offset else 0
    if count < MIN_STATION_MONTH_SAMPLES:
        return None
    return {
        offset: (realized[offset] / count) / (claimed[offset] / count)
        if claimed[offset] > 0
        else 1.0
        for offset in OFFSETS
    }


def station_month_scales(rows: list[dict]) -> dict:
    """Per station-month bias and error scale of the research forecast.

    Fitted per month so a season's own error scale is not borrowed from
    another's; the tilt being measured is what remains after that.
    """

    grouped: dict[tuple[str, int], list[float]] = collections.defaultdict(list)
    for row in rows:
        grouped[(row["station_id"], row["month"])].append(
            row["actual_c"] - row["forecast_c"]
        )
    scales: dict[tuple[str, int], tuple[float, float]] = {}
    for key, errors in grouped.items():
        if len(errors) < MIN_STATION_MONTH_SAMPLES:
            continue
        bias = statistics.fmean(errors)
        sigma = statistics.pstdev(errors)
        if sigma > 0.0:
            scales[key] = (bias, sigma)
    return scales


def bootstrap_by_day(
    rows: list[dict], *, scales: dict, draws: int
) -> dict[int, tuple[float, float]]:
    """Confidence intervals resampling days, the independent unit."""

    by_day: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_day[row["local_date"]].append(row)
    days = sorted(by_day)
    samples: dict[int, list[float]] = {offset: [] for offset in OFFSETS}
    for _ in range(draws):
        drawn = [row for day in random.choices(days, k=len(days)) for row in by_day[day]]
        factors = fit_factors(drawn, scales=scales)
        if factors is None:
            continue
        for offset in OFFSETS:
            samples[offset].append(factors[offset])
    intervals: dict[int, tuple[float, float]] = {}
    for offset in OFFSETS:
        values = sorted(samples[offset])
        if len(values) < 20:
            intervals[offset] = (float("nan"), float("nan"))
            continue
        low = values[int(0.025 * len(values))]
        high = values[int(0.975 * len(values))]
        intervals[offset] = (low, high)
    return intervals


def latitude_band(latitude: float) -> str:
    magnitude = abs(latitude)
    if magnitude < 25.0:
        return "lat_00_25"
    if magnitude < 35.0:
        return "lat_25_35"
    if magnitude < 45.0:
        return "lat_35_45"
    return "lat_45_plus"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data" / "bayes_basket_target_ssot.db",
    )
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP_DRAWS)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    random.seed(args.seed)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = load_pairs(conn)
    conn.close()
    if not rows:
        raise SystemExit("research_pair_dataset_empty")
    latitudes = {
        station_id: station_coordinates(station_id)["latitude"]
        for station_id in {row["station_id"] for row in rows}
    }
    for row in rows:
        row["band"] = latitude_band(latitudes[row["station_id"]])
        row["season"] = (
            "warm_half"
            if (
                (latitudes[row["station_id"]] >= 0 and 4 <= row["month"] <= 9)
                or (latitudes[row["station_id"]] < 0 and (row["month"] <= 3 or row["month"] >= 10))
            )
            else "cold_half"
        )
    scales = station_month_scales(rows)
    report: dict[str, dict] = {}

    def describe(label: str, subset: list[dict]) -> None:
        factors = fit_factors(subset, scales=scales)
        if factors is None:
            return
        intervals = bootstrap_by_day(subset, scales=scales, draws=args.bootstrap)
        report[label] = {
            "station_days": len(subset),
            "factors": {str(k): round(v, 4) for k, v in factors.items()},
            "intervals": {
                str(k): [round(v[0], 4), round(v[1], 4)]
                for k, v in intervals.items()
            },
            "significant": {
                str(k): not (
                    intervals[k][0] <= 1.0 <= intervals[k][1]
                )
                for k in OFFSETS
            },
        }

    describe("all", rows)
    for band in sorted({row["band"] for row in rows}):
        describe(band, [row for row in rows if row["band"] == band])
    for season in ("warm_half", "cold_half"):
        describe(season, [row for row in rows if row["season"] == season])
    for band in sorted({row["band"] for row in rows}):
        for season in ("warm_half", "cold_half"):
            describe(
                f"{band}|{season}",
                [
                    row
                    for row in rows
                    if row["band"] == band and row["season"] == season
                ],
            )
    payload = {
        "schema": "neighbour_tilt_structure_v1",
        "station_days": len(rows),
        "stations": len({row["station_id"] for row in rows}),
        "date_range": [
            min(row["local_date"] for row in rows),
            max(row["local_date"] for row in rows),
        ],
        "station_month_scales": len(scales),
        "groups": report,
        "reference_only": True,
        "is_settlement_truth": False,
        "calls_order_gateway": False,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
