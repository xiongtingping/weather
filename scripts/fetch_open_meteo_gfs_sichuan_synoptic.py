#!/usr/bin/env python3
"""Download bounded NOAA GFS pressure-level context for Sichuan research.

The transport is Open-Meteo's Historical Forecast API, whose GFS archive and
live Forecast API share the same variables and response contract.  This keeps
the v3 training and live feature distributions aligned.  The script writes
only to the requested research directory and never touches production DBs,
signals, parameters, launchd, or orders.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests


SCHEMA = "open_meteo_noaa_gfs_sichuan_synoptic_download_v1"
API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
MODEL = "gfs_seamless"
ARCHIVE_START = date(2021, 3, 23)
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
USER_AGENT = "ZUUU-v3-synoptic-research/1.0"

POINTS: dict[str, tuple[float, float]] = {
    "basin_center": (30.58, 103.95),
    "west_rim": (30.0, 100.0),
    "east_basin": (30.0, 107.5),
    "north_basin": (32.5, 105.0),
    "south_basin": (27.5, 105.0),
}

HOURLY_VARIABLES = (
    "temperature_850hPa",
    "temperature_700hPa",
    "relative_humidity_850hPa",
    "relative_humidity_700hPa",
    "geopotential_height_500hPa",
    "geopotential_height_850hPa",
    "wind_speed_850hPa",
    "wind_direction_850hPa",
    "wind_speed_700hPa",
    "wind_direction_700hPa",
    "vertical_velocity_700hPa",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def _request_year(session: requests.Session, year: int, timeout: float) -> tuple[bytes, list[dict[str, Any]], str]:
    start = max(date(year, 1, 1), ARCHIVE_START)
    end = date(year, 12, 31)
    params = {
        "latitude": ",".join(str(value[0]) for value in POINTS.values()),
        "longitude": ",".join(str(value[1]) for value in POINTS.values()),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(HOURLY_VARIABLES),
        "models": MODEL,
        "wind_speed_unit": "ms",
        "timezone": "GMT",
    }
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = session.get(API_URL, params=params, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or len(payload) != len(POINTS):
                raise RuntimeError(f"expected {len(POINTS)} point payloads, got {type(payload).__name__}:{len(payload) if isinstance(payload, list) else 'na'}")
            return response.content, [dict(item) for item in payload], response.url
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(float(attempt))
    raise RuntimeError(f"GFS year {year} download failed: {last_error}")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _wind_uv(speed: float | None, direction_deg: float | None) -> tuple[float | None, float | None]:
    if speed is None or direction_deg is None:
        return None, None
    radians = math.radians(float(direction_deg))
    return -float(speed) * math.sin(radians), -float(speed) * math.cos(radians)


def _omega_pa_per_s(vertical_velocity_mps: float | None, temperature_700_c: float | None) -> float | None:
    if vertical_velocity_mps is None or temperature_700_c is None:
        return None
    density = 70000.0 / (287.05 * (float(temperature_700_c) + 273.15))
    return -density * 9.80665 * float(vertical_velocity_mps)


def _point_rows(point_payload: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    hourly = point_payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise RuntimeError("GFS point payload missing hourly object")
    times = hourly.get("time")
    if not isinstance(times, Sequence) or isinstance(times, (str, bytes, bytearray)):
        raise RuntimeError("GFS point payload missing hourly.time")
    for key in HOURLY_VARIABLES:
        values = hourly.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)) or len(values) != len(times):
            raise RuntimeError(f"GFS point payload invalid {key} width")
    rows: dict[str, dict[str, float]] = {}
    for index, timestamp in enumerate(times):
        dt = datetime.fromisoformat(str(timestamp)).replace(tzinfo=timezone.utc)
        if dt.hour not in {0, 6, 12, 18}:
            continue
        values: dict[str, float] = {}
        missing = False
        for key in HOURLY_VARIABLES:
            value = _number(hourly[key][index])
            if value is None:
                missing = True
                break
            values[key] = value
        if not missing:
            rows[dt.isoformat().replace("+00:00", "Z")] = values
    return rows


def normalize_payloads(payloads: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(payloads) != len(POINTS):
        raise RuntimeError("point payload count mismatch")
    per_point = {
        point: _point_rows(payload)
        for point, payload in zip(POINTS, payloads)
    }
    common_times = set.intersection(*(set(rows) for rows in per_point.values())) if per_point else set()
    all_times = set.union(*(set(rows) for rows in per_point.values())) if per_point else set()
    output: list[dict[str, Any]] = []
    for timestamp in sorted(common_times):
        record: dict[str, Any] = {"valid_time_utc": timestamp}
        for point, values_by_time in per_point.items():
            values = values_by_time[timestamp]
            t850 = values["temperature_850hPa"]
            hgt500 = values["geopotential_height_500hPa"]
            speed850 = values["wind_speed_850hPa"]
            direction850 = values["wind_direction_850hPa"]
            u850, v850 = _wind_uv(speed850, direction850)
            record[f"{point}_air_850_c"] = round(t850, 6)
            record[f"{point}_hgt_500_m"] = round(hgt500, 6)
            record[f"{point}_uwnd_850_mps"] = round(float(u850), 6)
            record[f"{point}_vwnd_850_mps"] = round(float(v850), 6)
            if point == "basin_center":
                speed700 = values["wind_speed_700hPa"]
                direction700 = values["wind_direction_700hPa"]
                u700, v700 = _wind_uv(speed700, direction700)
                record.update(
                    {
                        "basin_center_rhum_850_pct": round(values["relative_humidity_850hPa"], 6),
                        "basin_center_rhum_700_pct": round(values["relative_humidity_700hPa"], 6),
                        "basin_center_hgt_850_m": round(values["geopotential_height_850hPa"], 6),
                        "basin_center_uwnd_700_mps": round(float(u700), 6),
                        "basin_center_vwnd_700_mps": round(float(v700), 6),
                        "basin_center_omega_700_pa_per_s": round(
                            float(_omega_pa_per_s(values["vertical_velocity_700hPa"], values["temperature_700hPa"])),
                            8,
                        ),
                    }
                )
        output.append(record)
    quality = {
        "point_count": len(per_point),
        "common_timestamp_count": len(common_times),
        "union_timestamp_count": len(all_times),
        "dropped_non_common_timestamp_count": len(all_times - common_times),
        "first_valid_time_utc": output[0]["valid_time_utc"] if output else "",
        "last_valid_time_utc": output[-1]["valid_time_utc"] if output else "",
    }
    return output, quality


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-download-bytes", type=int, default=MAX_DOWNLOAD_BYTES)
    args = parser.parse_args()
    years = list(range(args.start_year, args.end_year + 1))
    if years != [2021, 2022, 2023, 2024, 2025]:
        raise SystemExit("bounded v3 download is locked to 2021-2025")

    started_at = _utc_now()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    all_rows: dict[str, dict[str, Any]] = {}
    downloads: list[dict[str, Any]] = []
    total_bytes = 0
    for year in years:
        content, payloads, response_url = _request_year(session, year, args.timeout)
        total_bytes += len(content)
        if total_bytes > int(args.max_download_bytes):
            raise RuntimeError(f"download cap exceeded: {total_bytes} > {args.max_download_bytes}")
        rows, quality = normalize_payloads(payloads)
        for row in rows:
            all_rows[str(row["valid_time_utc"])] = row
        raw_path = args.output_dir / "raw_json" / f"gfs_sichuan_{year}.json.gz"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_bytes = gzip.compress(content, compresslevel=6)
        _atomic_bytes(raw_path, raw_bytes)
        downloads.append(
            {
                "year": year,
                "url": response_url,
                "raw_path": str(raw_path),
                "response_bytes": len(content),
                "compressed_bytes": len(raw_bytes),
                "sha256": _sha256(content),
                "quality": quality,
            }
        )
        print(json.dumps({"year": year, "rows": len(rows), "bytes": len(content)}, sort_keys=True), flush=True)

    normalized_path = args.output_dir / "synoptic_features_6hourly.csv.gz"
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [all_rows[key] for key in sorted(all_rows)]
    fieldnames = ["valid_time_utc", *sorted({key for row in rows for key in row if key != "valid_time_utc"})]
    tmp = normalized_path.with_name(f".{normalized_path.name}.tmp.{os.getpid()}")
    try:
        with gzip.open(tmp, "wt", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, normalized_path)
    finally:
        tmp.unlink(missing_ok=True)

    manifest = {
        "schema": SCHEMA,
        "download_started_at": started_at,
        "download_completed_at": _utc_now(),
        "data_origin": "NOAA NCEP GFS",
        "transport": "Open-Meteo Historical Forecast API",
        "transport_contract": "same_model_same_variables_as_open_meteo_live_forecast_api",
        "model": MODEL,
        "years": years,
        "archive_start": ARCHIVE_START.isoformat(),
        "points": {key: {"latitude": value[0], "longitude": value[1]} for key, value in POINTS.items()},
        "hourly_variables": list(HOURLY_VARIABLES),
        "normalized_temporal_grain": "6-hourly",
        "normalized_timestamp_count": len(rows),
        "normalized_column_count": len(fieldnames),
        "normalized_path": str(normalized_path),
        "downloaded_total_bytes": total_bytes,
        "max_download_bytes": int(args.max_download_bytes),
        "downloads": downloads,
        "guards": {
            "historical_backfill": False,
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "production_integration": False,
        },
    }
    _atomic_json(args.output_dir / "download_manifest.json", manifest)
    print(json.dumps({"ok": True, "rows": len(rows), "bytes": total_bytes, "output_dir": str(args.output_dir)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
