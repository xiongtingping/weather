#!/usr/bin/env python3
"""Measure what ICON's 6-hourly sampling loses against its native hourly grid.

ICON publishes ``t_2m`` hourly out to +48h, but the collector reads it every
six hours.  Unlike ECMWF/GEFS/ECCC -- which all sample 3-hourly and whose
residual differences proved to be per-station constants that calibration
absorbs -- a 6-hour grid can miss the afternoon peak entirely, and the amount
missed scales with that day's diurnal range rather than staying constant.  A
varying miss is not something a per-station bias correction can remove.

This settles it without waiting for truth to accrue: hourly, 3-hourly and
6-hourly maxima all come from the *same* download of the *same* run, so the
difference between them is exactly the sampling loss and nothing else.  What
matters is not the mean loss (calibration eats that) but its spread within a
station -- that is the part no bias term can reach.
"""

from __future__ import annotations

import argparse
import bz2
import json
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.dwd_icon_eps_archive import (  # noqa: E402
    BASE_URL,
    EXPECTED_MEMBER_COUNT,
    _eccodes,
    _fetch,
    _grid_index,
    _message_values,
    _split_grib_messages,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)
from official_nwp_https_transport import official_nwp_proxy_url  # noqa: E402

MAX_FORECAST_HOUR = 48
MAX_DOWNLOAD_WORKERS = 3
CANDIDATE_STEPS = (1, 3, 6)


def _station_scopes(db: Path) -> list[dict[str, object]]:
    """Active stations with their settlement timezone, from the registry."""

    import sqlite3

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT DISTINCT station_id,settlement_timezone "
            "FROM basket_target_market_registry "
            "WHERE source_eligible=1 AND station_id IS NOT NULL "
            "  AND settlement_timezone IS NOT NULL "
            "ORDER BY station_id"
        ).fetchall()
    scopes = []
    for station, zone in rows:
        coordinates = station_coordinates(str(station))
        scopes.append({
            "station_id": str(station),
            "settlement_timezone": str(zone),
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
        })
    return scopes


def _covered_local_day(
    *,
    run_init: datetime,
    settlement_timezone: str,
) -> str | None:
    """First local date fully inside the downloaded forecast window."""

    zone = ZoneInfo(settlement_timezone)
    hours = range(MAX_FORECAST_HOUR + 1)
    by_date: dict[str, set[int]] = defaultdict(set)
    for hour in hours:
        stamp = (run_init + timedelta(hours=hour)).astimezone(zone)
        by_date[stamp.date().isoformat()].add(stamp.hour)
    complete = sorted(
        local_date
        for local_date, present in by_date.items()
        if len(present) == 24
    )
    return complete[0] if complete else None


def _download_hour(
    client: httpx.Client,
    library: object,
    *,
    run_day: str,
    cycle: str,
    forecast_hour: int,
    grid_index: dict[str, int],
) -> tuple[int, dict[str, dict[int, float]]]:
    """Station values for every member at one forecast hour."""

    url = (
        f"{BASE_URL}/{cycle}/t_2m/"
        f"icon-eps_global_icosahedral_single-level_"
        f"{run_day}{cycle}_{forecast_hour:03d}_t_2m.grib2.bz2"
    )
    raw = bz2.decompress(_fetch(client, url))
    per_station: dict[str, dict[int, float]] = {
        station: {} for station in grid_index
    }
    for offset, length in _split_grib_messages(raw):
        member, values = _message_values(
            library, raw[offset : offset + length]
        )
        for station, point in grid_index.items():
            per_station[station][member] = values[point] - 273.15
    return forecast_hour, per_station


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--run-day", required=True, help="YYYYMMDD")
    parser.add_argument("--cycle", default="00", choices=["00", "06", "12", "18"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "output" / "experiments" / "icon_grid_cache",
    )
    args = parser.parse_args()

    database = args.db.expanduser().resolve(strict=True)
    scopes = _station_scopes(database)
    run_init = datetime.combine(
        date(
            int(args.run_day[:4]), int(args.run_day[4:6]), int(args.run_day[6:])
        ),
        time(hour=int(args.cycle)),
        tzinfo=timezone.utc,
    )

    targets: dict[str, str] = {}
    for scope in scopes:
        local_date = _covered_local_day(
            run_init=run_init,
            settlement_timezone=str(scope["settlement_timezone"]),
        )
        if local_date is not None:
            targets[str(scope["station_id"])] = local_date
    if not targets:
        raise SystemExit("icon_sampling_experiment_no_covered_local_day")

    library = _eccodes()
    proxy = official_nwp_proxy_url()
    station_ids = sorted(targets)
    with httpx.Client(
        timeout=90.0, follow_redirects=True, proxy=proxy or None
    ) as client:
        grid_index = _grid_index(
            client,
            library,
            run_day=args.run_day,
            cycle=args.cycle,
            station_ids=station_ids,
            cache_dir=args.cache_dir.expanduser().resolve(),
        )
        hours = list(range(MAX_FORECAST_HOUR + 1))
        # hour -> station -> member -> celsius
        grid: dict[int, dict[str, dict[int, float]]] = {}
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
            for forecast_hour, per_station in pool.map(
                lambda hour: _download_hour(
                    client,
                    library,
                    run_day=args.run_day,
                    cycle=args.cycle,
                    forecast_hour=hour,
                    grid_index=grid_index,
                ),
                hours,
            ):
                grid[forecast_hour] = per_station
                print(
                    f"  fetched f{forecast_hour:03d}", file=sys.stderr, flush=True
                )

    # Per station, per member: daily max under each sampling step.
    per_station_losses: dict[str, dict[int, list[float]]] = {}
    for station, local_date in sorted(targets.items()):
        zone = ZoneInfo(
            next(
                str(scope["settlement_timezone"])
                for scope in scopes
                if scope["station_id"] == station
            )
        )
        in_day = [
            hour
            for hour in grid
            if (run_init + timedelta(hours=hour)).astimezone(zone)
            .date()
            .isoformat()
            == local_date
        ]
        maxima: dict[int, dict[int, float]] = {}
        for step in CANDIDATE_STEPS:
            sampled = [hour for hour in in_day if hour % step == 0]
            if not sampled:
                continue
            per_member: dict[int, float] = {}
            for member in range(EXPECTED_MEMBER_COUNT):
                values = [
                    grid[hour][station][member]
                    for hour in sampled
                    if member in grid[hour][station]
                ]
                if values:
                    per_member[member] = max(values)
            maxima[step] = per_member
        if 1 not in maxima:
            continue
        losses: dict[int, list[float]] = {}
        for step in CANDIDATE_STEPS:
            if step == 1 or step not in maxima:
                continue
            losses[step] = [
                maxima[1][member] - maxima[step][member]
                for member in sorted(maxima[1])
                if member in maxima[step]
            ]
        per_station_losses[station] = losses

    report_stations: dict[str, dict[str, object]] = {}
    for station, losses in sorted(per_station_losses.items()):
        entry: dict[str, object] = {"local_date": targets[station]}
        for step, values in sorted(losses.items()):
            if not values:
                continue
            entry[f"step_{step}h"] = {
                "mean_loss_c": round(statistics.fmean(values), 4),
                "sd_within_station_c": round(
                    statistics.pstdev(values), 4
                ),
                "max_loss_c": round(max(values), 4),
                "members": len(values),
            }
        report_stations[station] = entry

    summary: dict[str, object] = {}
    for step in CANDIDATE_STEPS:
        if step == 1:
            continue
        key = f"step_{step}h"
        means = [
            float(entry[key]["mean_loss_c"])  # type: ignore[index]
            for entry in report_stations.values()
            if key in entry
        ]
        sds = [
            float(entry[key]["sd_within_station_c"])  # type: ignore[index]
            for entry in report_stations.values()
            if key in entry
        ]
        if not means:
            continue
        summary[key] = {
            "stations": len(means),
            "mean_loss_c": round(statistics.fmean(means), 4),
            "sd_of_station_means_c": round(
                statistics.pstdev(means) if len(means) > 1 else 0.0, 4
            ),
            "mean_within_station_sd_c": round(statistics.fmean(sds), 4),
            "max_station_mean_loss_c": round(max(means), 4),
        }

    report = {
        "schema": "icon_eps_sampling_step_experiment_v1",
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "forecast_hours": f"0..{MAX_FORECAST_HOUR}",
        "candidate_steps_hours": list(CANDIDATE_STEPS),
        "summary": summary,
        "stations": report_stations,
        "writes_ssot": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(
        {"output": str(output), "summary": summary},
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
