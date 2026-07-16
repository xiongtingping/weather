#!/usr/bin/env python3
"""Download bounded same-distribution global-model context for ZUUU research.

Only research files under ``--output-dir`` are written.  The script never
touches archive.db, launchd, strategy parameters, signal files, or orders.
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


SCHEMA = "open_meteo_global_model_sichuan_synoptic_download_v1"
API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
USER_AGENT = "ZUUU-v3.1-multimodel-research/1.0"

MODEL_CONFIG: dict[str, dict[str, Any]] = {
    "jma_gsm": {"origin": "JMA GSM", "archive_start": "2016-01-01"},
    "icon_global": {"origin": "DWD ICON Global", "archive_start": "2022-11-24"},
    "gem_global": {"origin": "ECCC GEM Global", "archive_start": "2022-11-23"},
    "ecmwf_ifs025": {"origin": "ECMWF IFS 0.25 degree", "archive_start": "2024-02-03"},
    "cma_grapes_global": {"origin": "CMA GRAPES Global", "archive_start": "2023-12-31"},
}

POINTS: dict[str, tuple[float, float]] = {
    "basin_center": (30.58, 103.95),
    "west_rim": (30.0, 100.0),
    "east_basin": (30.0, 107.5),
    "north_basin": (32.5, 105.0),
    "south_basin": (27.5, 105.0),
}

CORE_VARIABLES = (
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
)
OPTIONAL_VARIABLES = ("vertical_velocity_700hPa",)
HOURLY_VARIABLES = (*CORE_VARIABLES, *OPTIONAL_VARIABLES)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def _wind_uv(speed: float | None, direction_deg: float | None) -> tuple[float | None, float | None]:
    if speed is None or direction_deg is None:
        return None, None
    radians = math.radians(direction_deg)
    return -speed * math.sin(radians), -speed * math.cos(radians)


def _omega_pa_per_s(vertical_velocity_mps: float | None, temperature_700_c: float | None) -> float | None:
    if vertical_velocity_mps is None or temperature_700_c is None:
        return None
    density = 70000.0 / (287.05 * (temperature_700_c + 273.15))
    return -density * 9.80665 * vertical_velocity_mps


def _request_year(
    session: requests.Session,
    *,
    model: str,
    year: int,
    archive_start: date,
    timeout: float,
) -> tuple[bytes, list[dict[str, Any]], str]:
    start = max(date(year, 1, 1), archive_start)
    end = date(year, 12, 31)
    if start > end:
        return b"[]", [], ""
    params = {
        "latitude": ",".join(str(value[0]) for value in POINTS.values()),
        "longitude": ",".join(str(value[1]) for value in POINTS.values()),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(HOURLY_VARIABLES),
        "models": model,
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
                raise RuntimeError(f"expected {len(POINTS)} point payloads")
            return response.content, [dict(item) for item in payload], response.url
        except Exception as exc:  # requests exposes several transport exceptions
            last_error = exc
            if attempt < 3:
                time.sleep(float(attempt))
    raise RuntimeError(f"{model} year {year} download failed: {last_error}")


def _point_rows(payload: Mapping[str, Any]) -> tuple[dict[str, dict[str, float | None]], dict[str, int]]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise RuntimeError("point payload missing hourly object")
    times = hourly.get("time")
    if not isinstance(times, Sequence) or isinstance(times, (str, bytes, bytearray)):
        raise RuntimeError("point payload missing hourly.time")
    for key in HOURLY_VARIABLES:
        values = hourly.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)) or len(values) != len(times):
            raise RuntimeError(f"point payload invalid {key} width")
    rows: dict[str, dict[str, float | None]] = {}
    coverage = {key: 0 for key in HOURLY_VARIABLES}
    for index, timestamp in enumerate(times):
        valid = datetime.fromisoformat(str(timestamp)).replace(tzinfo=timezone.utc)
        if valid.hour not in {0, 6, 12, 18}:
            continue
        values = {key: _number(hourly[key][index]) for key in HOURLY_VARIABLES}
        for key, value in values.items():
            coverage[key] += int(value is not None)
        if any(values[key] is None for key in CORE_VARIABLES):
            continue
        rows[valid.isoformat().replace("+00:00", "Z")] = values
    return rows, coverage


def _normalize(payloads: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    per_point: dict[str, dict[str, dict[str, float | None]]] = {}
    variable_coverage = {key: 0 for key in HOURLY_VARIABLES}
    for point, payload in zip(POINTS, payloads):
        rows, coverage = _point_rows(payload)
        per_point[point] = rows
        for key, count in coverage.items():
            variable_coverage[key] += count
    common = set.intersection(*(set(value) for value in per_point.values())) if per_point else set()
    union = set.union(*(set(value) for value in per_point.values())) if per_point else set()
    output: list[dict[str, Any]] = []
    for timestamp in sorted(common):
        record: dict[str, Any] = {"valid_time_utc": timestamp}
        for point, rows in per_point.items():
            values = rows[timestamp]
            u850, v850 = _wind_uv(values["wind_speed_850hPa"], values["wind_direction_850hPa"])
            record[f"{point}_air_850_c"] = round(float(values["temperature_850hPa"]), 6)
            record[f"{point}_hgt_500_m"] = round(float(values["geopotential_height_500hPa"]), 6)
            record[f"{point}_uwnd_850_mps"] = round(float(u850), 6)
            record[f"{point}_vwnd_850_mps"] = round(float(v850), 6)
            if point == "basin_center":
                u700, v700 = _wind_uv(values["wind_speed_700hPa"], values["wind_direction_700hPa"])
                omega = _omega_pa_per_s(values.get("vertical_velocity_700hPa"), values["temperature_700hPa"])
                record.update(
                    {
                        "basin_center_rhum_850_pct": round(float(values["relative_humidity_850hPa"]), 6),
                        "basin_center_rhum_700_pct": round(float(values["relative_humidity_700hPa"]), 6),
                        "basin_center_hgt_850_m": round(float(values["geopotential_height_850hPa"]), 6),
                        "basin_center_uwnd_700_mps": round(float(u700), 6),
                        "basin_center_vwnd_700_mps": round(float(v700), 6),
                        "basin_center_omega_700_pa_per_s": round(float(omega), 8) if omega is not None else None,
                    }
                )
        output.append(record)
    return output, {
        "point_count": len(per_point),
        "common_timestamp_count": len(common),
        "union_timestamp_count": len(union),
        "dropped_non_common_timestamp_count": len(union - common),
        "first_valid_time_utc": output[0]["valid_time_utc"] if output else "",
        "last_valid_time_utc": output[-1]["valid_time_utc"] if output else "",
        "variable_non_null_count_across_points": variable_coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=sorted(MODEL_CONFIG), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-download-bytes", type=int, default=MAX_DOWNLOAD_BYTES)
    args = parser.parse_args()
    if args.start_year < 2021 or args.end_year > 2025 or args.start_year > args.end_year:
        raise SystemExit("bounded research download must remain inside 2021-2025")

    config = MODEL_CONFIG[args.model]
    archive_start = date.fromisoformat(str(config["archive_start"]))
    years = list(range(args.start_year, args.end_year + 1))
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    started_at = _utc_now()
    total_bytes = 0
    all_rows: dict[str, dict[str, Any]] = {}
    downloads: list[dict[str, Any]] = []
    for year in years:
        content, payloads, url = _request_year(
            session,
            model=args.model,
            year=year,
            archive_start=archive_start,
            timeout=args.timeout,
        )
        if not payloads:
            continue
        total_bytes += len(content)
        if total_bytes > int(args.max_download_bytes):
            raise RuntimeError(f"download cap exceeded: {total_bytes} > {args.max_download_bytes}")
        rows, quality = _normalize(payloads)
        for row in rows:
            all_rows[str(row["valid_time_utc"])] = row
        raw_path = args.output_dir / "raw_json" / f"{args.model}_sichuan_{year}.json.gz"
        compressed = gzip.compress(content, compresslevel=6)
        _atomic_bytes(raw_path, compressed)
        downloads.append(
            {
                "year": year,
                "url": url,
                "raw_path": str(raw_path),
                "response_bytes": len(content),
                "compressed_bytes": len(compressed),
                "sha256": _sha256(content),
                "quality": quality,
            }
        )
        print(json.dumps({"model": args.model, "year": year, "rows": len(rows), "bytes": len(content)}, sort_keys=True), flush=True)

    rows = [all_rows[key] for key in sorted(all_rows)]
    if not rows:
        raise SystemExit(f"no normalized rows for {args.model}")
    normalized_path = args.output_dir / "synoptic_features_6hourly.csv.gz"
    fields = ["valid_time_utc", *sorted({key for row in rows for key in row if key != "valid_time_utc"})]
    temporary = normalized_path.with_name(f".{normalized_path.name}.tmp.{os.getpid()}")
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(temporary, "wt", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, normalized_path)
    finally:
        temporary.unlink(missing_ok=True)

    manifest = {
        "schema": SCHEMA,
        "download_started_at": started_at,
        "download_completed_at": _utc_now(),
        "data_origin": config["origin"],
        "transport": "Open-Meteo Historical Forecast API",
        "transport_contract": "same_model_same_core_variables_as_open_meteo_live_forecast_api",
        "model": args.model,
        "archive_start": archive_start.isoformat(),
        "years_requested": years,
        "points": {key: {"latitude": value[0], "longitude": value[1]} for key, value in POINTS.items()},
        "core_variables": list(CORE_VARIABLES),
        "optional_variables": list(OPTIONAL_VARIABLES),
        "normalized_temporal_grain": "6-hourly",
        "normalized_timestamp_count": len(rows),
        "normalized_column_count": len(fields),
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
    print(json.dumps({"ok": True, "model": args.model, "rows": len(rows), "bytes": total_bytes, "output_dir": str(args.output_dir)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
