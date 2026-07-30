#!/usr/bin/env python3
"""Capture one native GEM/GDPS run as bounded P0-compatible evidence."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.native_model_decode import (  # noqa: E402
    materialize_native_daily_curves_by_station_timezone,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402
from scripts.rebuild_bayes_basket_target_historical_gem import (  # noqa: E402
    PROVIDER_ID,
    _decode,
    _fetch,
    _grid_file,
    MIN_FREE_BYTES,
    _raw_cache,
    _stamp,
    _url,
    _window,
)


SCHEMA = "bayes_basket_target_native_model_arrival_decode_evidence_v1"
MODEL_ID = "GEM"
DEFAULT_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_RAW_ROOT = (
    ROOT / "data/runtime/bayes_basket_target/gem_exact_run/raw"
)
DEFAULT_OUTPUT = (
    ROOT / "output/audits/bayes_basket_target_gem_exact_run_decode.json"
)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("gem_exact_run_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _station_timezones(
    registry: Mapping[str, Any],
    *,
    stations: Sequence[str],
    event_date: str,
) -> dict[str, str]:
    values: dict[str, set[str]] = {station: set() for station in stations}
    for row in registry.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        if (
            station in values
            and str(row.get("event_date") or "") == event_date
            and str(row.get("settlement_timezone") or "").strip()
        ):
            values[station].add(
                str(row["settlement_timezone"]).strip()
            )
    result = {}
    for station, timezones in values.items():
        if len(timezones) != 1:
            raise ContractError(
                f"gem_exact_run_registry_timezone_invalid:{station}"
            )
        result[station] = next(iter(timezones))
    return result


def capture(
    *,
    stations: Sequence[str],
    event_date: str,
    run_initialization_utc: str,
    as_of_utc: str,
    registry_path: Path,
    raw_root: Path,
    output_path: Path,
    max_workers: int = 2,
) -> dict[str, Any]:
    normalized = sorted({
        str(station).strip().upper()
        for station in stations
        if str(station).strip()
    })
    if not normalized or len(normalized) > 15:
        raise ContractError("gem_exact_run_station_scope_invalid")
    for station in normalized:
        station_coordinates(station)
    target_date = date.fromisoformat(event_date)
    run = _parse_utc(run_initialization_utc)
    as_of = _parse_utc(as_of_utc)
    if run.hour not in {0, 12} or run > as_of:
        raise ContractError("gem_exact_run_initialization_invalid")
    registry = load_market_registry_snapshot(registry_path.resolve())
    timezones = _station_timezones(
        registry, stations=normalized, event_date=event_date
    )
    windows = {
        station: _window(target_date, timezones[station])
        for station in normalized
    }
    earliest = min(start for start, _end in windows.values())
    latest = max(end for _start, end in windows.values())
    cursor = earliest.replace(minute=0, second=0, microsecond=0)
    if cursor < earliest:
        cursor += timedelta(hours=1)
    valid_times = []
    while cursor < latest:
        valid_times.append(cursor)
        cursor += timedelta(hours=1)
    raw_root = raw_root.expanduser().resolve()
    raw_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(raw_root).free < MIN_FREE_BYTES:
        raise ContractError("gem_exact_run_min_free_space_guard")
    cached = _raw_cache(raw_root)
    received_at = _stamp(datetime.now(timezone.utc))
    blockers = []
    points = []
    with tempfile.TemporaryDirectory(prefix="gem-exact-run-") as temp:
        temporary = Path(temp)
        grid = _grid_file(normalized, temporary)

        def capture_hour(
            valid_time: datetime,
        ) -> tuple[datetime, dict[str, Any], dict[str, float]]:
            lead = int((valid_time - run).total_seconds() // 3600)
            if lead < 0 or lead > 240:
                raise ContractError("gem_exact_run_lead_out_of_range")
            worker = temporary / f"{lead:03d}"
            worker.mkdir(parents=True, exist_ok=True)
            fetched = _fetch(
                _url(run, lead),
                raw_root / f"pending-{lead:03d}.grib2",
                cached=cached,
            )
            return valid_time, fetched, _decode(
                fetched["path"],
                stations=normalized,
                grid_path=grid,
                directory=worker,
            )

        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="gem-exact-run",
        ) as executor:
            futures = {
                executor.submit(capture_hour, valid_time): valid_time
                for valid_time in valid_times
            }
            for future in as_completed(futures):
                valid_time = futures[future]
                try:
                    _, fetched, decoded = future.result()
                except (ContractError, OSError, ValueError) as exc:
                    blockers.append({
                        "valid_time_utc": _stamp(valid_time),
                        "reason": str(exc)[:256],
                    })
                    continue
                for station, temperature in decoded.items():
                    start, end = windows[station]
                    if start <= valid_time < end:
                        point_identity = {
                            "provider_id": PROVIDER_ID,
                            "station_id": station,
                            "event_date": event_date,
                            "model_run_initialization_utc": _stamp(run),
                            "valid_time_utc": _stamp(valid_time),
                            "raw_message_sha256": fetched["sha256"],
                            "published_at_utc": fetched[
                                "published_at_utc"
                            ],
                            "received_at_utc": received_at,
                        }
                        points.append({
                            **point_identity,
                            "model_id": MODEL_ID,
                            "temperature_2m_c": temperature,
                            "receipt_sha256": content_sha256(
                                point_identity
                            ),
                            "coordinate_lineage": station_coordinates(
                                station
                            ),
                        })
    curves = materialize_native_daily_curves_by_station_timezone(
        points, station_timezones=timezones
    )
    if (
        blockers
        or curves["all_complete"] is not True
        or curves["complete_curve_count"] != len(normalized)
    ):
        raise ContractError("gem_exact_run_curve_incomplete")
    identity = {
        "schema": SCHEMA,
        "as_of_utc": _stamp(as_of),
        "target_local_date": event_date,
        "target_timezone": (
            next(iter(set(timezones.values())))
            if len(set(timezones.values())) == 1
            else "REGISTRY_BOUND_PER_STATION"
        ),
        "station_timezones": dict(sorted(timezones.items())),
        "market_registry_snapshot_sha256": registry["snapshot_sha256"],
        "decoded_point_count": len(points),
        "decoded_points": sorted(
            points,
            key=lambda row: (
                row["station_id"], row["valid_time_utc"]
            ),
        ),
        "blocked_captures": [],
        "curves": curves,
        "checks": {
            "explicit_single_run": True,
            "native_official_archive": True,
            "complete_curve_count_positive": True,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
        "boundary": {
            "production_no_live_only": True,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    result = {**identity, "receipt_sha256": content_sha256(identity)}
    write_bounded_json(output_path.expanduser().resolve(), result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", action="append", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-workers", type=int, choices=(1, 2), default=2)
    args = parser.parse_args(argv)
    result = capture(
        stations=args.station,
        event_date=args.event_date,
        run_initialization_utc=args.run_initialization_utc,
        as_of_utc=args.as_of_utc,
        registry_path=args.registry,
        raw_root=args.raw_root,
        output_path=args.output,
        max_workers=args.max_workers,
    )
    print(json.dumps({
        "schema": result["schema"],
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "station_count": result["curves"]["complete_curve_count"],
        "decoded_point_count": result["decoded_point_count"],
        "output": str(args.output.expanduser().resolve()),
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
