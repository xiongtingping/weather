#!/usr/bin/env python3
"""Download bounded NOAA PSL NCEP/DOE R2 synoptic features for Sichuan.

The script uses NOAA PSL's NetCDF Subset Service in grid-as-point CSV mode.
Only 2021-2025, five fixed regional points, selected pressure levels, and
research files are in scope.  It never touches production databases or signals.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


SCHEMA = "noaa_psl_ncep_r2_sichuan_synoptic_research_v1"
BASE_URL = "https://psl.noaa.gov/thredds/ncss/grid/Datasets/ncep.reanalysis2/pressure/{variable}.{year}.nc"
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024

POINTS = {
    "basin_center": (30.58, 103.95),
    "west_rim": (30.0, 100.0),
    "east_basin": (30.0, 107.5),
    "north_basin": (32.5, 105.0),
    "south_basin": (27.5, 105.0),
}

CENTER_SPECS = [
    ("air", 850),
    ("rhum", 850),
    ("rhum", 700),
    ("hgt", 500),
    ("hgt", 850),
    ("uwnd", 850),
    ("vwnd", 850),
    ("uwnd", 700),
    ("vwnd", 700),
    ("omega", 700),
]

REGIONAL_SPECS = [
    ("air", 850),
    ("hgt", 500),
    ("uwnd", 850),
    ("vwnd", 850),
]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expected_rows(year: int) -> int:
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return (366 if leap else 365) * 4


def _output_name(point: str, variable: str, level: int) -> str:
    suffix = "c" if variable == "air" else "pct" if variable == "rhum" else "m" if variable == "hgt" else "mps" if variable in {"uwnd", "vwnd"} else "pa_per_s"
    return f"{point}_{variable}_{level}_{suffix}"


def _convert(variable: str, value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < -1e30:
        raise ValueError(f"invalid {variable} value: {value}")
    return number - 273.15 if variable == "air" else number


def _request_csv(
    session: requests.Session,
    *,
    point: str,
    latitude: float,
    longitude: float,
    variable: str,
    level: int,
    year: int,
    timeout: float,
) -> tuple[bytes, list[dict[str, str]], str]:
    url = BASE_URL.format(variable=variable, year=year)
    params = {
        "var": variable,
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "time_start": f"{year}-01-01T00:00:00Z",
        "time_end": f"{year}-12-31T18:00:00Z",
        "vertCoord": str(level),
        "accept": "csv",
    }
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            if not response.content.startswith(b"time,station,"):
                raise RuntimeError(f"unexpected response for {point}/{variable}/{level}/{year}: {response.text[:200]}")
            text = response.content.decode("utf-8")
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            if len(rows) != _expected_rows(year):
                raise RuntimeError(f"row count mismatch {point}/{variable}/{level}/{year}: {len(rows)}")
            value_columns = [name for name in (reader.fieldnames or []) if name.startswith(f"{variable}[")]
            if len(value_columns) != 1:
                raise RuntimeError(f"value column mismatch: {reader.fieldnames}")
            return response.content, rows, value_columns[0]
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.5 * attempt)
    raise RuntimeError(str(last_error))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    years = list(range(args.start_year, args.end_year + 1))
    if years != [2021, 2022, 2023, 2024, 2025]:
        raise SystemExit("this bounded research downloader is locked to 2021-2025")

    raw_dir = args.output_dir / "raw_csv"
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "ZUUU-research/1.0 (bounded NOAA PSL NCSS client)"})
    wide: dict[str, dict[str, Any]] = {}
    downloads = []
    total_bytes = 0
    series = []
    for point in POINTS:
        specs = CENTER_SPECS if point == "basin_center" else REGIONAL_SPECS
        series.extend((point, variable, level) for variable, level in specs)

    for year in years:
        for point, variable, level in series:
            latitude, longitude = POINTS[point]
            content, rows, value_column = _request_csv(
                session,
                point=point,
                latitude=latitude,
                longitude=longitude,
                variable=variable,
                level=level,
                year=year,
                timeout=args.timeout,
            )
            total_bytes += len(content)
            if total_bytes > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"download cap exceeded: {total_bytes}")
            relative = Path("raw_csv") / point / f"{variable}_{level}_{year}.csv"
            path = args.output_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            output_name = _output_name(point, variable, level)
            actual_latitudes = {float(row['latitude[unit="degrees_north"]']) for row in rows}
            actual_longitudes = {float(row['longitude[unit="degrees_east"]']) for row in rows}
            if len(actual_latitudes) != 1 or len(actual_longitudes) != 1:
                raise RuntimeError(f"grid point changed within request: {point}/{variable}/{level}/{year}")
            for row in rows:
                timestamp = row["time"]
                record = wide.setdefault(timestamp, {"valid_time_utc": timestamp})
                record[output_name] = round(_convert(variable, row[value_column]), 6)
            downloads.append(
                {
                    "year": year,
                    "point": point,
                    "requested_latitude": latitude,
                    "requested_longitude": longitude,
                    "actual_grid_latitude": next(iter(actual_latitudes)),
                    "actual_grid_longitude": next(iter(actual_longitudes)),
                    "variable": variable,
                    "level_hpa": level,
                    "value_column": value_column,
                    "row_count": len(rows),
                    "bytes": len(content),
                    "sha256": _sha256(content),
                    "path": str(relative),
                    "source_url": BASE_URL.format(variable=variable, year=year),
                }
            )
            print(json.dumps({"downloaded": str(relative), "rows": len(rows), "bytes": len(content)}, ensure_ascii=False), flush=True)

    expected_columns = {_output_name(point, variable, level) for point, variable, level in series}
    expected_timestamps = sum(_expected_rows(year) for year in years)
    if len(wide) != expected_timestamps:
        raise RuntimeError(f"timestamp count mismatch: {len(wide)} != {expected_timestamps}")
    missing_cells = sum(len(expected_columns - set(row)) for row in wide.values())
    if missing_cells:
        raise RuntimeError(f"normalized join has {missing_cells} missing cells")
    normalized_path = args.output_dir / "synoptic_features_6hourly.csv.gz"
    fields = ["valid_time_utc", *sorted(expected_columns)]
    with gzip.open(normalized_path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(wide[timestamp] for timestamp in sorted(wide))

    manifest = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "NOAA PSL NCEP/DOE Reanalysis 2 NCSS",
        "source_product_url": "https://psl.noaa.gov/data/gridded/data.ncep.Reanalysis2.html",
        "years": years,
        "temporal_grain": "6-hourly",
        "spatial_grid": "2.5 degree nearest grid point",
        "points": {name: {"latitude": value[0], "longitude": value[1]} for name, value in POINTS.items()},
        "series_count": len(series),
        "normalized_timestamp_count": len(wide),
        "normalized_column_count": len(fields),
        "raw_download_bytes": total_bytes,
        "download_cap_bytes": MAX_DOWNLOAD_BYTES,
        "downloads": downloads,
        "normalized_path": normalized_path.name,
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
    (args.output_dir / "download_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "requests": len(downloads), "timestamps": len(wide), "raw_bytes": total_bytes, "output_dir": str(args.output_dir), "guards": manifest["guards"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
