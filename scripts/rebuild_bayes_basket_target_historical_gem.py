#!/usr/bin/env python3
"""Rebuild bounded native GEM/GDPS 12Z history for the canonical stations.

Environment Canada's HPFX archive is date-addressable and exposes each
2-metre-temperature forecast hour as a small GRIB2 object.  This adapter
downloads one object at a time, remaps it to the registered station points,
retains only content-addressed source objects, and materializes registry-
timezone daily curves.  It never writes SQLite, probability state, or an
order gateway.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import assert_event_day_closed  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_PROCESS_RSS_BYTES,
    MAX_TARGET_RESEARCH_RAW_BYTES,
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402
from official_nwp_https_transport import official_nwp_proxy_url  # noqa: E402


SCHEMA = "bayes_basket_target_historical_native_rebuild_evidence_v1"
PROVIDER_ID = "gem_gdps_official"
MODEL_ID = "GEM"
ARCHIVE = "https://hpfx.collab.science.gc.ca"
_DIRECT_TRANSPORT = False
DEFAULT_STATION_MAP = (
    ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
)
DEFAULT_TIMEZONE_REGISTRY = (
    ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
)
DEFAULT_RAW_ROOT = (
    ROOT / "data/research/bayes_basket_target/model_arrivals"
    / PROVIDER_ID / "raw"
)
DEFAULT_OUTPUT = (
    ROOT / "output/audits/bayes_basket_target_historical_gem_rebuild.json"
)
MAX_DATES = 4
MAX_STATIONS = expected_station_count()
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024
_RAW_PERSIST_LOCK = threading.Lock()


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _stamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _binary(name: str, candidates: Sequence[str]) -> str:
    for candidate in candidates:
        resolved = shutil.which(candidate) or candidate
        if Path(resolved).is_file() and os.access(resolved, os.X_OK):
            return str(Path(resolved).resolve())
    raise ContractError(f"historical_gem_{name}_unavailable")


def _dates(args: argparse.Namespace) -> list[date]:
    result = {
        date.fromisoformat(str(value))
        for value in (args.event_date or [])
    }
    if args.start_date or args.end_date:
        if not args.start_date or not args.end_date:
            raise ContractError("historical_gem_date_range_incomplete")
        start = date.fromisoformat(args.start_date)
        end = date.fromisoformat(args.end_date)
        if end < start:
            raise ContractError("historical_gem_date_range_invalid")
        result.update(
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
        )
    values = sorted(result)
    if not values or len(values) > MAX_DATES:
        raise ContractError("historical_gem_date_count_invalid")
    return values


def _stations(path: Path, explicit: Sequence[str] | None) -> list[str]:
    payload = load_bounded_json(path.expanduser().resolve())
    raw = payload.get("stations") if isinstance(payload, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ContractError("historical_gem_station_map_invalid")
    allowed = {str(value).strip().upper() for value in raw}
    selected = (
        {str(value).strip().upper() for value in explicit}
        if explicit else allowed
    )
    if not selected or len(selected) > MAX_STATIONS or selected - allowed:
        raise ContractError("historical_gem_station_scope_invalid")
    for station in selected:
        station_coordinates(station)
    return sorted(selected)


def _timezones(path: Path, stations: Sequence[str]) -> dict[str, str]:
    payload = load_bounded_json(path.expanduser().resolve())
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("historical_gem_timezone_registry_invalid")
    values: dict[str, set[str]] = {station: set() for station in stations}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        zone = str(row.get("settlement_timezone") or "").strip()
        if station in values and zone:
            values[station].add(zone)
    result = {}
    for station, zones in values.items():
        if len(zones) != 1:
            raise ContractError(
                f"historical_gem_station_timezone_invalid:{station}"
            )
        result[station] = next(iter(zones))
    return result


def _run(event_date: date, run_cycle: str, run_day_offset: int) -> datetime:
    cycle = str(run_cycle).strip().upper()
    if cycle not in {"00Z", "12Z"} or run_day_offset not in {1, 2}:
        raise ContractError("historical_gem_run_scope_invalid")
    return datetime.combine(
        event_date - timedelta(days=run_day_offset),
        time(hour=int(cycle[:2])),
        tzinfo=timezone.utc,
    )


def _window(event_date: date, zone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(zone_name)
    start = datetime.combine(event_date, time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    end = datetime.combine(
        event_date + timedelta(days=1), time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    return start, end


def _url(run: datetime, lead_hour: int) -> str:
    day = run.strftime("%Y%m%d")
    hour = run.strftime("%H")
    filename = (
        f"{day}T{hour}Z_MSC_GDPS_AirTemp_AGL-2m_"
        f"LatLon0.15_PT{lead_hour:03d}H.grib2"
    )
    return (
        f"{ARCHIVE}/{day}/WXO-DD/model_gdps/15km/{hour}/"
        f"{lead_hour:03d}/{filename}"
    )


def _fetch(
    url: str,
    destination: Path,
    *,
    cached: Mapping[str, Mapping[str, Any]],
    aggregate_raw_root: Path,
) -> dict[str, Any]:
    prior = cached.get(url)
    if isinstance(prior, Mapping):
        prior_path = Path(str(prior.get("path") or "")).expanduser().resolve()
        prior_sha = str(prior.get("sha256") or "")
        if (
            prior_path.is_file()
            and len(prior_sha) == 64
            and _sha256(prior_path) == prior_sha
        ):
            return dict(prior)
    # Serialize download-and-persist so parallel workers cannot each pass the
    # aggregate preflight against the same stale retained-byte snapshot.
    with _RAW_PERSIST_LOCK:
        assert_target_raw_retention_budget(
            aggregate_raw_root,
            bytes_needed=MAX_RESPONSE_BYTES,
        )
        curl = _binary("curl", ("curl", "/usr/bin/curl"))
        proxy = "" if _DIRECT_TRANSPORT else official_nwp_proxy_url()
        with tempfile.NamedTemporaryFile(
            prefix="gem-headers-", suffix=".txt", delete=False
        ) as header_file:
            header_path = Path(header_file.name)
        temporary = destination.with_suffix(".part")
        temporary.unlink(missing_ok=True)
        args = [
            curl, "--silent", "--show-error", "--location",
            "--connect-timeout", "15", "--max-time", "120",
            "--retry", "3", "--retry-delay", "1",
            "--max-filesize", str(MAX_RESPONSE_BYTES),
            "--dump-header", str(header_path),
            "--output", str(temporary),
            "--write-out", "%{http_code}",
        ]
        args.extend(("--proxy", proxy) if proxy else ("--noproxy", "*"))
        args.append(url)
        try:
            completed = subprocess.run(
                args, check=False, capture_output=True, text=True, timeout=140
            )
            if completed.returncode != 0 or completed.stdout.strip() != "200":
                raise ContractError(
                    f"historical_gem_http_failed:{completed.stdout.strip()}"
                )
            size = temporary.stat().st_size
            if size <= 0 or size > MAX_RESPONSE_BYTES:
                raise ContractError("historical_gem_response_size_invalid")
            header_text = header_path.read_text(
                encoding="iso-8859-1", errors="replace"
            )
            modified_values = [
                line.split(":", 1)[1].strip()
                for line in header_text.splitlines()
                if line.lower().startswith("last-modified:")
            ]
            if not modified_values:
                raise ContractError("historical_gem_last_modified_missing")
            published = parsedate_to_datetime(modified_values[-1])
            digest = _sha256(temporary)
            destination = destination.parent / f"{digest}.grib2"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if (
                    destination.stat().st_size != size
                    or _sha256(destination) != digest
                ):
                    raise ContractError("historical_gem_raw_collision")
                temporary.unlink()
            else:
                # Recheck with the actual object size immediately before the
                # atomic persist; no over-budget body is ever retained.
                assert_target_raw_retention_budget(
                    aggregate_raw_root,
                    bytes_needed=size,
                )
                os.replace(temporary, destination)
            receipt = {
                "path": destination,
                "sha256": digest,
                "bytes": size,
                "published_at_utc": _stamp(published),
                "source_url": url,
            }
            receipt_path = destination.with_suffix(".receipt.json")
            write_bounded_json(receipt_path, {
                key: str(value) if isinstance(value, Path) else value
                for key, value in receipt.items()
            })
            return receipt
        finally:
            temporary.unlink(missing_ok=True)
            header_path.unlink(missing_ok=True)


def _grid_file(stations: Sequence[str], directory: Path) -> Path:
    output = directory / "stations-grid.txt"
    coordinates = [station_coordinates(station) for station in stations]
    output.write_text(
        "\n".join([
            "gridtype = unstructured",
            f"gridsize = {len(stations)}",
            "xname = lon",
            "yname = lat",
            "xunits = degrees_east",
            "yunits = degrees_north",
            "xvals = " + " ".join(
                str(float(row["longitude"]) % 360.0) for row in coordinates
            ),
            "yvals = " + " ".join(
                str(float(row["latitude"])) for row in coordinates
            ),
            "",
        ]),
        encoding="utf-8",
    )
    return output


def _decode(
    raw_path: Path,
    *,
    stations: Sequence[str],
    grid_path: Path,
    directory: Path,
) -> dict[str, float]:
    cdo = _binary("cdo", ("cdo", "/opt/homebrew/bin/cdo"))
    remapped = directory / "remapped.nc"
    completed = subprocess.run(
        [
            cdo, "--silent", "-f", "nc",
            f"remapnn,{grid_path}", str(raw_path), str(remapped),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise ContractError("historical_gem_remap_failed")
    table = subprocess.run(
        [cdo, "--silent", "outputtab,value", str(remapped)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    remapped.unlink(missing_ok=True)
    if table.returncode != 0:
        raise ContractError("historical_gem_decode_failed")
    values = []
    for line in table.stdout.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            values.append(float(text.split()[-1]))
        except (ValueError, IndexError) as exc:
            raise ContractError("historical_gem_decode_value_invalid") from exc
    if len(values) != len(stations):
        raise ContractError("historical_gem_decode_station_count_invalid")
    return {
        station: round(value - 273.15 if value > 150 else value, 6)
        for station, value in zip(stations, values)
    }


def _raw_cache(raw_root: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for receipt_path in raw_root.glob("*.receipt.json"):
        try:
            value = load_bounded_json(receipt_path)
        except (ContractError, OSError, ValueError, json.JSONDecodeError):
            continue
        url = str(value.get("source_url") or "")
        if url:
            result[url] = dict(value)
    return result


def rebuild(args: argparse.Namespace) -> dict[str, Any]:
    event_dates = _dates(args)
    stations = _stations(args.station_map, args.station_id)
    timezones = _timezones(args.timezone_registry, stations)
    for station in stations:
        for event_date in event_dates:
            assert_event_day_closed(
                event_date.isoformat(),
                as_of_utc=args.as_of_utc,
                target_timezone=timezones[station],
            )
    raw_root = args.raw_root.expanduser().resolve()
    raw_root.mkdir(parents=True, exist_ok=True)
    aggregate_raw_root = raw_root.parents[1]
    retained_bytes_before = assert_target_raw_retention_budget(
        aggregate_raw_root,
        bytes_needed=0,
    )
    if shutil.disk_usage(raw_root).free < MIN_FREE_BYTES:
        raise ContractError("historical_gem_min_free_space_guard")
    points: dict[tuple[str, str], list[dict[str, Any]]] = {}
    blockers: list[dict[str, Any]] = []
    cached = _raw_cache(raw_root)
    with tempfile.TemporaryDirectory(prefix="gem-native-rebuild-") as temp:
        temporary = Path(temp)
        grid = _grid_file(stations, temporary)
        for event_date in event_dates:
            run = _run(event_date, args.run_cycle, args.run_day_offset)
            windows = {
                station: _window(event_date, timezones[station])
                for station in stations
            }
            earliest = min(start for start, _end in windows.values())
            latest = max(end for _start, end in windows.values())
            first_hour = earliest.replace(minute=0, second=0, microsecond=0)
            if first_hour < earliest:
                first_hour += timedelta(hours=1)
            valid_times = []
            cursor = first_hour
            while cursor < latest:
                valid_times.append(cursor)
                cursor += timedelta(hours=1)
            def capture_valid_time(
                valid_time: datetime,
            ) -> tuple[datetime, dict[str, Any], dict[str, float]]:
                lead = int((valid_time - run).total_seconds() // 3600)
                if lead < 0 or lead > 240:
                    raise ContractError("historical_gem_lead_out_of_range")
                worker_directory = temporary / (
                    f"{event_date.isoformat()}-{lead:03d}"
                )
                worker_directory.mkdir(parents=True, exist_ok=True)
                fetched = _fetch(
                    _url(run, lead),
                    raw_root / f"pending-{lead:03d}.grib2",
                    cached=cached,
                    aggregate_raw_root=aggregate_raw_root,
                )
                decoded = _decode(
                    fetched["path"],
                    stations=stations,
                    grid_path=grid,
                    directory=worker_directory,
                )
                return valid_time, fetched, decoded

            with ThreadPoolExecutor(
                max_workers=args.max_workers,
                thread_name_prefix="gem-native",
            ) as executor:
                futures = {
                    executor.submit(capture_valid_time, valid_time): valid_time
                    for valid_time in valid_times
                }
                results = []
                for future in as_completed(futures):
                    valid_time = futures[future]
                    try:
                        results.append(future.result())
                    except (
                        ContractError,
                        OSError,
                        subprocess.SubprocessError,
                    ) as exc:
                        blockers.append({
                            "event_date": event_date.isoformat(),
                            "valid_time_utc": _stamp(valid_time),
                            "reason": "historical_gem_fetch_or_decode_failed",
                            "detail": str(exc)[:256],
                        })
            for valid_time, fetched, decoded in sorted(
                results, key=lambda item: item[0]
            ):
                for station, value in decoded.items():
                    start, end = windows[station]
                    if start <= valid_time < end:
                        points.setdefault(
                            (station, event_date.isoformat()), []
                        ).append({
                            "valid_time_utc": _stamp(valid_time),
                            "temperature_2m_c": value,
                            "raw_message_sha256": fetched["sha256"],
                            "raw_object_ref": str(fetched["path"]),
                            "source_url": fetched["source_url"],
                            "published_at_utc": fetched["published_at_utc"],
                        })
                assert_process_memory_budget(
                    limit=MAX_TARGET_PROCESS_RSS_BYTES
                )
    model_rows = []
    for station in stations:
        for event_date in event_dates:
            key = (station, event_date.isoformat())
            rows = sorted(
                points.get(key, []), key=lambda row: row["valid_time_utc"]
            )
            start, end = _window(event_date, timezones[station])
            expected = int((end - start).total_seconds() // 3600)
            if len(rows) != expected:
                blockers.append({
                    "station_id": station,
                    "event_date": event_date.isoformat(),
                    "reason": "historical_gem_curve_incomplete",
                    "point_count": len(rows),
                    "expected_point_count": expected,
                })
                continue
            maximum = max(rows, key=lambda row: row["temperature_2m_c"])
            lineage = content_sha256({
                "provider_id": PROVIDER_ID,
                "station_id": station,
                "event_date": event_date.isoformat(),
                "model_run_initialization_utc": _stamp(
                    _run(event_date, args.run_cycle, args.run_day_offset)
                ),
                "points": rows,
            })
            model_rows.append({
                "station_id": station,
                "event_date": event_date.isoformat(),
                "target_timezone": timezones[station],
                "provider_id": PROVIDER_ID,
                "model_id": MODEL_ID,
                "model_run_initialization_utc": _stamp(
                    _run(event_date, args.run_cycle, args.run_day_offset)
                ),
                "valid_time_utc": maximum["valid_time_utc"],
                "temperature_2m_c": maximum["temperature_2m_c"],
                "forecast_daily_max_c": maximum["temperature_2m_c"],
                "sample_count": expected,
                "daily_curve_complete": True,
                "published_at_utc": max(
                    row["published_at_utc"] for row in rows
                ),
                "received_at_utc": "",
                "raw_message_sha256": content_sha256(
                    [row["raw_message_sha256"] for row in rows]
                ),
                "receipt_sha256": lineage,
                "model_run_lineage_sha256": lineage,
                "temperature_points": rows,
                "source_class": "native_or_official_cloud_replica",
                "historical_replay_eligible": True,
                "prospective_strict_asof_eligible": False,
            })
    identity = {
        "schema": SCHEMA,
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "run_cycle": args.run_cycle,
        "run_day_offset": args.run_day_offset,
        "as_of_utc": args.as_of_utc,
        "station_ids": stations,
        "event_dates": [value.isoformat() for value in event_dates],
        "model_rows": model_rows,
        "blockers": blockers,
        "resource_budget": {
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_total_raw_bytes": MAX_TARGET_RESEARCH_RAW_BYTES,
            "retained_bytes_before_run": retained_bytes_before,
            "min_free_bytes": MIN_FREE_BYTES,
            "retained_bytes_after_run": assert_target_raw_retention_budget(
                aggregate_raw_root,
                bytes_needed=0,
            ),
            "max_process_rss_bytes": MAX_TARGET_PROCESS_RSS_BYTES,
        },
        "boundary": {
            "historical_replay_only": True,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(args.output.expanduser().resolve(), result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", action="append")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--station-id", action="append")
    parser.add_argument("--station-map", type=Path, default=DEFAULT_STATION_MAP)
    parser.add_argument(
        "--timezone-registry", type=Path, default=DEFAULT_TIMEZONE_REGISTRY
    )
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--run-cycle", choices=("00Z", "12Z"), default="12Z")
    parser.add_argument("--run-day-offset", type=int, choices=(1, 2), default=2)
    parser.add_argument("--max-workers", type=int, choices=(1, 2, 3, 4), default=4)
    parser.add_argument(
        "--transport",
        choices=("configured_proxy", "direct"),
        default="configured_proxy",
    )
    args = parser.parse_args(argv)
    global _DIRECT_TRANSPORT
    _DIRECT_TRANSPORT = args.transport == "direct"
    result = rebuild(args)
    print(json.dumps({
        "schema": result["schema"],
        "provider_id": result["provider_id"],
        "station_count": len(result["station_ids"]),
        "event_date_count": len(result["event_dates"]),
        "model_row_count": len(result["model_rows"]),
        "blocker_count": len(result["blockers"]),
        "output": str(args.output.expanduser().resolve()),
        "writes_production_database": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
