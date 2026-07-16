#!/usr/bin/env python3
"""Download bounded NOAA/NCEI GHCNh data for the exact ZUUU airport station.

Research-only.  The downloader enforces an aggregate byte ceiling, verifies
the NCEI station-list identity, writes content hashes, and never opens or
modifies archive.db, facts, signals, production parameters, or launchd.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATION_ID = "CHI0000ZUUU"
EXPECTED_ICAO = "ZUUU"
EXPECTED_NAME = "SHUANGLIU"
STATION_LIST_URL = (
    "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/"
    "hourly/doc/ghcnh-station-list.csv"
)
YEAR_URL = (
    "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/"
    "hourly/access/by-year/{year}/psv/GHCNh_{station}_{year}.psv"
)
USER_AGENT = "weather-research/1.0 (bounded ZUUU GHCNh study)"
SCHEMA = "ncei_zuuu_ghcnh_research_download_manifest_v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _request(url: str, *, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})


def _head_length(url: str, timeout: int) -> int | None:
    with urllib.request.urlopen(_request(url, method="HEAD"), timeout=timeout) as response:
        raw = response.headers.get("Content-Length")
        return int(raw) if raw and raw.isdigit() else None


def _fetch_station_identity(timeout: int) -> dict[str, Any]:
    with urllib.request.urlopen(_request(STATION_LIST_URL), timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    matches = [row for row in rows if row and str(row[0]).strip() == STATION_ID]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {STATION_ID} station-list row, found {len(matches)}")
    row = matches[0]
    if len(row) < 10:
        raise RuntimeError("NCEI station-list row has unexpected width")
    identity = {
        "ghcnh_station_id": row[0].strip(),
        "latitude": float(row[1]),
        "longitude": float(row[2]),
        "elevation_m": float(row[3]),
        "name": row[5].strip(),
        "wmo_id": row[8].strip(),
        "icao": row[9].strip(),
        "country": row[10].strip() if len(row) > 10 else "",
        "source_url": STATION_LIST_URL,
    }
    if identity["icao"] != EXPECTED_ICAO or EXPECTED_NAME not in identity["name"].upper():
        raise RuntimeError(f"station identity mismatch: {identity}")
    return identity


def _download(
    url: str,
    path: Path,
    *,
    timeout: int,
    max_file_bytes: int,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    digest = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as response, tmp.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_file_bytes:
                    raise RuntimeError(f"file byte ceiling exceeded for {url}")
                handle.write(chunk)
                digest.update(chunk)
            content_length = response.headers.get("Content-Length")
            last_modified = response.headers.get("Last-Modified", "")
            content_type = response.headers.get("Content-Type", "")
        if content_length and content_length.isdigit() and written != int(content_length):
            raise RuntimeError(f"content-length mismatch for {url}: {written}!={content_length}")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return {
        "url": url,
        "path": str(path),
        "bytes": written,
        "sha256": digest.hexdigest(),
        "last_modified": last_modified,
        "content_type": content_type,
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _parse_years(value: str) -> list[int]:
    text = str(value or "").strip()
    if ":" in text:
        start_text, end_text = text.split(":", 1)
        start, end = int(start_text), int(end_text)
        return list(range(start, end + 1))
    return sorted({int(item) for item in text.split(",") if item.strip()})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", default="2021:2025")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-total-bytes", type=int, default=250 * 1024 * 1024)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    years = _parse_years(args.years)
    if not years or min(years) < 1900 or max(years) > datetime.now(timezone.utc).year:
        raise SystemExit("invalid year range")

    started_at = _utc_now()
    identity = _fetch_station_identity(args.timeout)
    urls = {year: YEAR_URL.format(year=year, station=STATION_ID) for year in years}
    expected_lengths = {year: _head_length(url, args.timeout) for year, url in urls.items()}
    known_total = sum(length or 0 for length in expected_lengths.values())
    if any(length is None for length in expected_lengths.values()):
        raise RuntimeError("NCEI preflight missing Content-Length; refusing unbounded download")
    if known_total > args.max_total_bytes:
        raise RuntimeError(f"preflight total {known_total} exceeds cap {args.max_total_bytes}")

    files = []
    cumulative = 0
    for year in years:
        remaining = args.max_total_bytes - cumulative
        path = args.output_dir / "raw" / f"GHCNh_{STATION_ID}_{year}.psv"
        result = _download(urls[year], path, timeout=args.timeout, max_file_bytes=remaining)
        result["year"] = year
        expected = expected_lengths[year]
        result["preflight_content_length"] = expected
        if result["bytes"] != expected:
            raise RuntimeError(f"year {year} byte count changed after preflight")
        cumulative += int(result["bytes"])
        files.append(result)

    manifest = {
        "schema": SCHEMA,
        "download_started_at": started_at,
        "download_completed_at": _utc_now(),
        "provider": "NOAA/NCEI GHCNh",
        "station_identity": identity,
        "years": years,
        "max_total_bytes": args.max_total_bytes,
        "preflight_total_bytes": known_total,
        "downloaded_total_bytes": cumulative,
        "files": files,
        "guards": {
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "production_backfill": False,
        },
    }
    _atomic_json(args.output_dir / "download_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
