#!/usr/bin/env python3
"""Fetch the public inputs of the physical-line study into a local cache.

The production SSOT is not reachable from where this study was first run, so
every input is re-read from the public source production itself reads, or
from its long-history republisher:

* ``model``   Open-Meteo previous-runs API, hourly ``*_previous_day1``
              (temperature, shortwave radiation, cloud cover, precipitation,
              10 m wind): what the models said about each hour one day
              before it -- an as-of day-ahead forecast, unlike the stitched
              short-lead historical API.
* ``metar``   IEM ASOS/METAR archive, raw METAR/SPECI text per station
              (global), which the production parser reads directly.
* ``awc_taf`` AWC decoded TAF JSON, one snapshot per hour over the 30 days
              AWC keeps -- the exact object production's
              ``_forecast_features`` parses.
* ``iem_taf`` IEM TAF archive (US stations only, long history), converted
              later to the AWC decoded shape.

Resource bounds: every response is capped (``--max-filesize``), the cache
directory refuses to grow past ``--max-cache-mb``, and a fetch stops when
free disk falls under ``--min-free-mb``.  Existing cache files are never
re-fetched, so an interrupted run resumes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stations import station_table  # noqa: E402

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MODEL_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODEL_FIELDS = (
    "temperature_2m_previous_day1,shortwave_radiation_previous_day1,"
    "cloud_cover_previous_day1,precipitation_previous_day1,"
    "wind_speed_10m_previous_day1"
)
METAR_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
AWC_TAF_URL = "https://aviationweather.gov/api/data/taf"
IEM_TAF_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/taf.py"


def _guard(cache: Path, *, max_cache_mb: int, min_free_mb: int) -> None:
    used = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file())
    if used > max_cache_mb * 1024 * 1024:
        raise SystemExit(f"cache_ceiling_reached:{used}")
    if shutil.disk_usage(cache).free < min_free_mb * 1024 * 1024:
        raise SystemExit("min_free_disk_guard")


def _curl(url: str, target: Path, *, timeout: int = 180) -> None:
    """Fetch ``url`` into ``target`` atomically, with bounded retries."""

    partial = target.with_suffix(target.suffix + ".part")
    for attempt in range(1, 6):
        completed = subprocess.run(
            [
                "curl", "--silent", "--show-error", "--location",
                "--max-redirs", "2", "--proto", "=https",
                "--connect-timeout", "30", "--max-time", str(timeout),
                "--max-filesize", str(MAX_RESPONSE_BYTES),
                "--user-agent", "weather-physical-line-study/1.0",
                "--output", str(partial), "--write-out", "%{http_code}", url,
            ],
            capture_output=True,
        )
        status = completed.stdout.decode("ascii", "replace").strip()
        if completed.returncode == 0 and status == "200":
            partial.replace(target)
            return
        partial.unlink(missing_ok=True)
        if attempt == 5:
            raise SystemExit(
                f"fetch_failed:{status}:{completed.stderr[:200]!r}:{url}"
            )
        time.sleep(2 ** attempt)


def fetch_model(cache: Path, start: str, end: str, args) -> None:
    out = cache / "model"
    out.mkdir(parents=True, exist_ok=True)
    for station, row in station_table().items():
        target = out / f"{station}_{start}_{end}.json"
        if target.exists():
            continue
        _guard(cache, max_cache_mb=args.max_cache_mb, min_free_mb=args.min_free_mb)
        query = {
            "latitude": row["latitude"], "longitude": row["longitude"],
            "hourly": MODEL_FIELDS, "start_date": start, "end_date": end,
            "timezone": "UTC",
        }
        _curl(f"{MODEL_URL}?{urlencode(query)}", target)
        body = json.loads(target.read_text())
        if "hourly" not in body:
            target.unlink()
            raise SystemExit(f"model_payload_invalid:{station}:{str(body)[:200]}")
        print("model", station, target.stat().st_size, flush=True)
        time.sleep(1.0)


GLOBAL_MODELS = "ecmwf_ifs025,gfs_global,icon_global,gem_global"


def fetch_global(cache: Path, start: str, end: str, args) -> None:
    """Day-ahead 2 m temperature of four global models (the P0 proxy).

    Production's P0 is a calibrated mixture of global ensembles; Open-Meteo's
    ``best_match`` leans on regional high-resolution models where they exist,
    a different -- and sharper -- temperature line.  The radiation term
    itself keeps reading ``best_match`` radiation, as production's physics
    table does.
    """

    out = cache / "global"
    out.mkdir(parents=True, exist_ok=True)
    for station, row in station_table().items():
        target = out / f"{station}_{start}_{end}.json"
        if target.exists():
            continue
        _guard(cache, max_cache_mb=args.max_cache_mb, min_free_mb=args.min_free_mb)
        query = {
            "latitude": row["latitude"], "longitude": row["longitude"],
            "hourly": "temperature_2m_previous_day1", "models": GLOBAL_MODELS,
            "start_date": start, "end_date": end, "timezone": "UTC",
        }
        _curl(f"{MODEL_URL}?{urlencode(query)}", target)
        body = json.loads(target.read_text())
        if "hourly" not in body:
            target.unlink()
            raise SystemExit(f"global_payload_invalid:{station}:{str(body)[:200]}")
        print("global", station, target.stat().st_size, flush=True)
        time.sleep(1.5)


def _chunks(start: date, end: date, days: int):
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=days - 1))
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def fetch_metar(cache: Path, start: str, end: str, args) -> None:
    out = cache / "metar"
    out.mkdir(parents=True, exist_ok=True)
    for station in station_table():
        for left, right in _chunks(
            date.fromisoformat(start), date.fromisoformat(end), 120
        ):
            target = out / f"{station}_{left}_{right}.csv"
            if target.exists():
                continue
            _guard(cache, max_cache_mb=args.max_cache_mb, min_free_mb=args.min_free_mb)
            stop = right + timedelta(days=1)
            query = [
                ("station", station), ("data", "metar"), ("data", "tmpf"),
                ("year1", left.year), ("month1", left.month), ("day1", left.day),
                ("year2", stop.year), ("month2", stop.month), ("day2", stop.day),
                ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"),
                ("elev", "no"), ("missing", "M"), ("trace", "T"),
                ("direct", "no"), ("report_type", 3), ("report_type", 4),
            ]
            _curl(f"{METAR_URL}?{urlencode(query)}", target, timeout=600)
            print("metar", station, left, right, target.stat().st_size, flush=True)
            time.sleep(1.0)


def fetch_awc_taf(cache: Path, args) -> None:
    out = cache / "awc_taf"
    out.mkdir(parents=True, exist_ok=True)
    ids = ",".join(station_table())
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    first = now - timedelta(days=args.awc_days)
    cursor = first
    while cursor <= now:
        stamp = cursor.strftime("%Y-%m-%dT%H:%M:%SZ")
        target = out / f"{cursor:%Y%m%dT%H}.json"
        if not target.exists():
            _guard(cache, max_cache_mb=args.max_cache_mb, min_free_mb=args.min_free_mb)
            query = {"ids": ids, "format": "json", "date": stamp}
            _curl(f"{AWC_TAF_URL}?{urlencode(query)}", target)
            body = target.read_text().strip()
            if not body.startswith("["):
                target.unlink()
                print("awc_taf_skip", stamp, body[:120], flush=True)
            else:
                print("awc_taf", stamp, len(json.loads(body)), flush=True)
            time.sleep(0.7)
        cursor += timedelta(hours=1)


US_STATIONS = (
    "KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD",
    "KSEA", "KSFO",
)


def fetch_iem_taf(cache: Path, start: str, end: str, args) -> None:
    out = cache / "iem_taf"
    out.mkdir(parents=True, exist_ok=True)
    for station in US_STATIONS:
        for left, right in _chunks(
            date.fromisoformat(start), date.fromisoformat(end), 31
        ):
            target = out / f"{station}_{left}_{right}.csv"
            if target.exists():
                continue
            _guard(cache, max_cache_mb=args.max_cache_mb, min_free_mb=args.min_free_mb)
            query = {
                "station": station, "fmt": "csv",
                "sts": f"{left}T00:00Z",
                "ets": f"{right + timedelta(days=1)}T00:00Z",
            }
            _curl(f"{IEM_TAF_URL}?{urlencode(query)}", target, timeout=300)
            print("iem_taf", station, left, right, target.stat().st_size, flush=True)
            time.sleep(1.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("what", choices=("model", "global", "metar", "awc_taf", "iem_taf"))
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--start", default="2025-06-01")
    parser.add_argument("--end", default="2026-09-24")
    parser.add_argument("--awc-days", type=int, default=29)
    parser.add_argument("--max-cache-mb", type=int, default=1500)
    parser.add_argument("--min-free-mb", type=int, default=3000)
    args = parser.parse_args()
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    if args.what == "model":
        fetch_model(cache, args.start, args.end, args)
    elif args.what == "global":
        fetch_global(cache, args.start, args.end, args)
    elif args.what == "metar":
        fetch_metar(cache, args.start, args.end, args)
    elif args.what == "awc_taf":
        fetch_awc_taf(cache, args)
    else:
        fetch_iem_taf(cache, args.start, args.end, args)


if __name__ == "__main__":
    main()
