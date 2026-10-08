#!/usr/bin/env python3
"""Capture the models' own radiation and wind forecasts per station-day.

The temperature lane extracts one variable from the model archives, and the
day's radiation is the physics those models get right while mis-translating
it to the surface.  Measured 2026-08-14 over 5,230 station-days: the
*forecast* daily shortwave radiation correlates +0.189 with the standardised
temperature residual, against +0.191 for the reanalysis actual -- the
forecast loses essentially nothing, because the error is in the coupling to
screen temperature, not in the radiation itself.  Shifting the served
distribution by 0.15 sigma per standardised unit gains 2.07% CRPS and 12.7%
NLL, twice what the forecaster's TAF fields reach and over every station-day
rather than the 17 stations that publish a TX.

The same request carries ``hourly=precipitation`` since 2026-09-23, kept in
its own append-only version table (see ``PRECIP_HOURLY_TABLE`` below).

Two modes, one table.  ``historical`` reads the archived past forecasts that
the OOS holdout needs to fit and score against; ``forward`` reads the live
forecast for the days being traded.  Both are the same quantity -- what the
models said before the day happened -- so they share a table and a
normalisation.

Provenance is the same narrow class as the same-day climatology: these are
aggregator-served model output at the station's coordinates, so they are
**reference covariates only** -- never a truth, never a settlement value,
never a P0 probability authority, and never a substitute for an official
observation.  They condition a shift; they do not assert a temperature.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)

HISTORICAL_BASE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORWARD_BASE_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_FIELDS = (
    "shortwave_radiation_sum,windspeed_10m_max,"
    "cloudcover_mean,precipitation_sum"
)
USER_AGENT = "weather-forecast-physics/1.0"
SOURCE_ID = "OPEN_METEO_MODEL_FORECAST"
TABLE = "calibration_station_forecast_physics"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REQUEST_SPACING_SECONDS = 0.8
# The resident kills a forward run at 300 s (weather_basket_yes_daemon
# ``_refresh_forecast_physics``).  Measured 2026-10-08 03:31Z: a loaded host
# took ~15 s a station, the kill came after 19 of 48 and took the receipt with
# it, and because the round always started at CYYZ the 29 stations from LFPB
# on kept their 10-07 09:31Z forecast.  So a forward round starts with the
# station whose last forward capture is oldest and stops starting fetches at
# this deadline; the ones it did not reach go first next round.  Fetching all
# 48 takes ~110 s; the worst case after the deadline is one fetch at curl's
# 90 s cap plus the 60 s wait for the write lock, so 150 + 90 + 60 = 300.
FORWARD_DEADLINE_SECONDS = 150.0

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    station_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    shortwave_radiation_sum REAL,
    windspeed_10m_max REAL,
    cloudcover_mean REAL,
    precipitation_sum REAL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    settlement_timezone TEXT NOT NULL,
    capture_mode TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    reference_only INTEGER NOT NULL CHECK (reference_only = 1),
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (station_id, local_date, source_id)
)
"""
INDEX = (
    f"CREATE INDEX IF NOT EXISTS {TABLE}_station_idx "
    f"ON {TABLE}(station_id, local_date)"
)

# The table above keeps one row per station-day and every forward capture
# replaces it, so a day's row is rewritten until the evening of the day
# itself.  Measured 2026-09-21: 1,616 of the 1,745 past forward rows were last
# written between 18:00 and 24:00 local on their own event day.  A holdout
# that reads it scores a same-day radiation against a P0 that was served a
# forecast one to two days ahead -- and the forecast that was served is gone.
#
# The version table keeps every distinct content with the moment it was first
# captured, append-only, so a sample can resolve "the latest version captured
# at or before my cutoff".  A version holds from its captured_at until the
# next version of the same key.  ``capture_mode='historical'`` versions come
# from the stitched short-lead historical-forecast API and are captured after
# the day; they are never an as-of forecast.  The table above is unchanged and
# still what serving reads.
VERSION_TABLE = f"{TABLE}_version"
COLUMNS = (
    "station_id,local_date,source_id,shortwave_radiation_sum,"
    "windspeed_10m_max,cloudcover_mean,precipitation_sum,"
    "latitude,longitude,settlement_timezone,capture_mode,"
    "captured_at_utc,reference_only,content_sha256"
)
VERSION_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (
    station_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    shortwave_radiation_sum REAL,
    windspeed_10m_max REAL,
    cloudcover_mean REAL,
    precipitation_sum REAL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    settlement_timezone TEXT NOT NULL,
    capture_mode TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    reference_only INTEGER NOT NULL CHECK (reference_only = 1),
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (station_id, local_date, source_id, captured_at_utc)
)
"""
VERSION_TRIGGERS = tuple(
    f"CREATE TRIGGER IF NOT EXISTS {VERSION_TABLE}_no_{verb.lower()} "
    f"BEFORE {verb} ON {VERSION_TABLE} BEGIN "
    f"SELECT RAISE(ABORT, '{VERSION_TABLE}_append_only'); END"
    for verb in ("UPDATE", "DELETE")
)
# Copy the current row into the versions unless the latest version of its
# key already carries the same content (a capture that changed nothing is not
# a new version; A -> B -> A is three).
VERSION_COPY = f"""
INSERT OR IGNORE INTO {VERSION_TABLE} ({COLUMNS})
SELECT {COLUMNS} FROM {TABLE} m
WHERE {{where}} AND NOT EXISTS (
    SELECT 1 FROM {VERSION_TABLE} v
    WHERE v.station_id=m.station_id AND v.local_date=m.local_date
      AND v.source_id=m.source_id AND v.content_sha256=m.content_sha256
      AND v.captured_at_utc=(
          SELECT MAX(w.captured_at_utc) FROM {VERSION_TABLE} w
          WHERE w.station_id=m.station_id AND w.local_date=m.local_date
            AND w.source_id=m.source_id
      )
)
"""


# Hourly precipitation, same request (user, 2026-09-23: 「采集降水」, 「有信息
# 就上，哪怕改进一点」).  The daily sum above says whether a day is wet; the
# hour says whether the rain falls before the afternoon maximum or after it,
# and only the former can move a daily high (measured 2026-09-23 on the
# reports: rain before 15:00 local −0.23°C on both the model's and the
# market's centre, rain only after the maximum −0.06°C).  One append-only
# version table, keyed by capture instant, so a decision can resolve the
# version it could have read; ``capture_mode='historical'`` rows come from
# the stitched short-lead archive and are never an as-of value.  The
# aggregates are derived in the capturer from the hourly series and carry
# the series that made them, so a reader never re-derives them differently.
HOURLY_FIELDS = "precipitation"
PRECIP_HOURLY_TABLE = "calibration_station_forecast_precip_hourly_version"
# Open-Meteo's hourly precipitation is the total of the preceding hour, so the
# value stamped 15:00 covers 14:00–15:00: hours 0..15 are "before the peak".
PRECIP_PEAK_HOUR = 15
PRECIP_WET_HOUR_MM = 0.1
PRECIP_HOURLY_COLUMNS = (
    "station_id,local_date,source_id,captured_at_utc,capture_mode,"
    "hourly_precip_mm_json,precip_sum_mm,precip_before_peak_mm,"
    "precip_after_peak_mm,first_wet_hour,wet_hours,peak_hour,"
    "latitude,longitude,settlement_timezone,reference_only,content_sha256"
)
PRECIP_HOURLY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {PRECIP_HOURLY_TABLE} (
    station_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    capture_mode TEXT NOT NULL CHECK (capture_mode IN ('forward','historical')),
    hourly_precip_mm_json TEXT NOT NULL,
    precip_sum_mm REAL NOT NULL CHECK (precip_sum_mm >= 0),
    precip_before_peak_mm REAL NOT NULL CHECK (precip_before_peak_mm >= 0),
    precip_after_peak_mm REAL NOT NULL CHECK (precip_after_peak_mm >= 0),
    first_wet_hour INTEGER,
    wet_hours INTEGER NOT NULL CHECK (wet_hours >= 0),
    peak_hour INTEGER NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    settlement_timezone TEXT NOT NULL,
    reference_only INTEGER NOT NULL CHECK (reference_only = 1),
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (station_id, local_date, source_id, captured_at_utc)
)
"""
PRECIP_HOURLY_INDEX = (
    f"CREATE INDEX IF NOT EXISTS {PRECIP_HOURLY_TABLE}_day_idx "
    f"ON {PRECIP_HOURLY_TABLE}(station_id, local_date, captured_at_utc)"
)
PRECIP_HOURLY_TRIGGERS = tuple(
    f"CREATE TRIGGER IF NOT EXISTS {PRECIP_HOURLY_TABLE}_no_{verb.lower()} "
    f"BEFORE {verb} ON {PRECIP_HOURLY_TABLE} BEGIN "
    f"SELECT RAISE(ABORT, '{PRECIP_HOURLY_TABLE}_append_only'); END"
    for verb in ("UPDATE", "DELETE")
)


def hourly_precipitation_days(payload: dict) -> dict[str, dict[str, float]]:
    """The hourly series grouped by local date: ``{date: {"HH": mm}}``.

    A missing value voids the whole day rather than reading as zero; a local
    hour that occurs twice (the autumn clock change) adds up.
    """

    hourly = payload.get("hourly") or {}
    stamps = hourly.get("time") or []
    values = hourly.get(HOURLY_FIELDS) or []
    days: dict[str, dict[str, float]] = {}
    voided: set[str] = set()
    for stamp, value in zip(stamps, values):
        text = str(stamp)
        if len(text) < 13:
            continue
        day, hour = text[:10], text[11:13]
        if value is None:
            voided.add(day)
            continue
        days.setdefault(day, {})
        days[day][hour] = days[day].get(hour, 0.0) + float(value)
    return {day: hours for day, hours in days.items() if day not in voided and len(hours) >= 23}


def hourly_precipitation_aggregates(hours: dict[str, float]) -> dict:
    """What the served term will read, derived once, here."""

    ordered = sorted((int(hour), float(mm)) for hour, mm in hours.items())
    before = sum(mm for hour, mm in ordered if hour <= PRECIP_PEAK_HOUR)
    after = sum(mm for hour, mm in ordered if hour > PRECIP_PEAK_HOUR)
    wet = [hour for hour, mm in ordered if mm >= PRECIP_WET_HOUR_MM]
    return {
        "precip_sum_mm": round(before + after, 3),
        "precip_before_peak_mm": round(before, 3),
        "precip_after_peak_mm": round(after, 3),
        "first_wet_hour": wet[0] if wet else None,
        "wet_hours": len(wet),
        "peak_hour": PRECIP_PEAK_HOUR,
    }


def record_hourly_precipitation(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    payload: dict,
    scope: dict,
    settlement_timezone: str,
    mode: str,
    captured_at: str,
) -> dict:
    """Append one version per station-day whose hourly series changed."""

    days = hourly_precipitation_days(payload)
    versions = 0
    for local_date, hours in sorted(days.items()):
        series = {f"{int(hour):02d}": round(mm, 3) for hour, mm in sorted(hours.items())}
        identity = {
            "schema": "station_forecast_precip_hourly_v1",
            "station_id": station_id,
            "local_date": local_date,
            "source_id": SOURCE_ID,
            "hourly_precip_mm": series,
            "latitude": float(scope["latitude"]),
            "longitude": float(scope["longitude"]),
            "settlement_timezone": settlement_timezone,
        }
        digest = content_sha256(identity)
        latest = conn.execute(
            f"SELECT content_sha256 FROM {PRECIP_HOURLY_TABLE}"
            " WHERE station_id=? AND local_date=? AND source_id=?"
            " ORDER BY captured_at_utc DESC LIMIT 1",
            (station_id, local_date, SOURCE_ID),
        ).fetchone()
        if latest is not None and latest[0] == digest:
            continue
        aggregates = hourly_precipitation_aggregates(series)
        conn.execute(
            f"INSERT OR IGNORE INTO {PRECIP_HOURLY_TABLE} ({PRECIP_HOURLY_COLUMNS})"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
            (
                station_id, local_date, SOURCE_ID, captured_at, mode,
                json.dumps(series, sort_keys=True),
                aggregates["precip_sum_mm"], aggregates["precip_before_peak_mm"],
                aggregates["precip_after_peak_mm"], aggregates["first_wet_hour"],
                aggregates["wet_hours"], aggregates["peak_hour"],
                float(scope["latitude"]), float(scope["longitude"]),
                settlement_timezone, digest,
            ),
        )
        versions += 1
    return {"hourly_days": len(days), "hourly_versions": versions}


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Both tables, and every current row recorded as a version.

    The seed is idempotent: a row whose content is already the latest
    version is skipped.  Returns the number of versions it added.
    """

    conn.execute(SCHEMA)
    conn.execute(INDEX)
    conn.execute(VERSION_SCHEMA)
    for trigger in VERSION_TRIGGERS:
        conn.execute(trigger)
    conn.execute(PRECIP_HOURLY_SCHEMA)
    conn.execute(PRECIP_HOURLY_INDEX)
    for trigger in PRECIP_HOURLY_TRIGGERS:
        conn.execute(trigger)
    added = conn.execute(VERSION_COPY.format(where="1=1")).rowcount
    conn.commit()
    return int(added or 0)


def stalest_first(conn: sqlite3.Connection, stations: list[str]) -> list[str]:
    """Never-captured stations, then the oldest last forward capture first.

    Ties keep the given order (the sort is stable), so an all-fresh fleet
    runs in canonical order.
    """

    last = dict(conn.execute(
        f"SELECT station_id, MAX(captured_at_utc) FROM {TABLE}"
        " WHERE capture_mode='forward' GROUP BY station_id"
    ).fetchall())
    return sorted(stations, key=lambda station: str(last.get(station) or ""))


def write_fetched(
    conn: sqlite3.Connection,
    fetched: list[tuple[str, str, dict]],
    *,
    mode: str,
) -> list[dict]:
    """All fetched stations in one IMMEDIATE transaction, a savepoint each.

    A station whose rows fail is rolled back alone; a failure to take the
    write lock at all fails every station of the round, each saying so.
    """

    receipts: list[dict] = []
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        reason = f"{type(exc).__name__}:{str(exc)[:160]}"
        return [{"station_id": station_id, "state": "FAILED", "reason": reason}
                for station_id, _tz, _fetched in fetched]
    for station_id, settlement_timezone, payload in fetched:
        conn.execute("SAVEPOINT station_capture")
        try:
            receipt = write_station(
                conn,
                station_id=station_id,
                fetched=payload,
                settlement_timezone=settlement_timezone,
                mode=mode,
            )
            conn.execute("RELEASE station_capture")
            receipts.append({**receipt, "state": "CAPTURED"})
        except Exception as exc:  # noqa: BLE001 -- per-station isolation
            conn.execute("ROLLBACK TO station_capture")
            conn.execute("RELEASE station_capture")
            receipts.append({
                "station_id": station_id,
                "state": "FAILED",
                "reason": f"{type(exc).__name__}:{str(exc)[:160]}",
            })
    conn.commit()
    return receipts


def _fetch_json(url: str, *, timeout_seconds: int) -> dict:
    """One bounded HTTPS read through the platform trust store."""

    with tempfile.TemporaryDirectory(prefix="fcst-physics-") as root:
        temporary = Path(root)
        if shutil.disk_usage(temporary).free < MAX_RESPONSE_BYTES + 256 * 1024 * 1024:
            raise OSError("forecast_physics_min_free_space_guard")
        body_path = temporary / "body"
        completed = subprocess.run(
            [
                "/usr/bin/curl",
                "--silent", "--show-error", "--location",
                "--max-redirs", "2",
                "--proto", "=https", "--proto-redir", "=https",
                "--tlsv1.2",
                "--connect-timeout", str(timeout_seconds),
                "--max-time", str(timeout_seconds * 2),
                "--max-filesize", str(MAX_RESPONSE_BYTES),
                "--user-agent", USER_AGENT,
                "--header", "Accept: application/json",
                "--output", str(body_path),
                "--write-out", "%{http_code}",
                url,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds * 2 + 5,
        )
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", "replace").strip()[:200]
            raise OSError(f"curl_exit_{completed.returncode}:{message}")
        status = int(completed.stdout.decode("ascii").strip() or "0")
        if status != 200:
            raise OSError(f"forecast_physics_http_status_{status}")
        return json.loads(body_path.read_text(encoding="utf-8"))


def capture_station(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    start_date: str,
    end_date: str,
    settlement_timezone: str,
    mode: str,
    timeout_seconds: int,
    hourly_only: bool = False,
) -> dict:
    """One station's capture.  ``hourly_only`` leaves the daily table alone:
    a historical backfill of the hourly lane must not overwrite the daily
    rows the forward captures wrote for the same dates."""

    fetched = fetch_station(
        station_id=station_id,
        start_date=start_date,
        end_date=end_date,
        settlement_timezone=settlement_timezone,
        mode=mode,
        timeout_seconds=timeout_seconds,
    )
    return write_station(
        conn,
        station_id=station_id,
        fetched=fetched,
        settlement_timezone=settlement_timezone,
        mode=mode,
        hourly_only=hourly_only,
    )


def fetch_station(
    *,
    station_id: str,
    start_date: str,
    end_date: str,
    settlement_timezone: str,
    mode: str,
    timeout_seconds: int,
) -> dict:
    """The network half of a capture; touches no database."""

    scope = station_coordinates(station_id)
    query = {
        "latitude": scope["latitude"],
        "longitude": scope["longitude"],
        "daily": DAILY_FIELDS,
        "hourly": HOURLY_FIELDS,
        "timezone": settlement_timezone,
    }
    if mode == "historical":
        query["start_date"] = start_date
        query["end_date"] = end_date
        base = HISTORICAL_BASE_URL
    else:
        query["forecast_days"] = 7
        base = FORWARD_BASE_URL
    payload = _fetch_json(
        f"{base}?{urlencode(query)}", timeout_seconds=timeout_seconds
    )
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"scope": scope, "payload": payload, "captured_at": captured_at}


def write_station(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    fetched: dict,
    settlement_timezone: str,
    mode: str,
    hourly_only: bool = False,
) -> dict:
    """The database half: the station's rows, versions and hourly versions."""

    scope = fetched["scope"]
    payload = fetched["payload"]
    captured_at = fetched["captured_at"]
    daily = payload.get("daily") or {}
    days = daily.get("time") or []
    written = 0
    versions = 0
    for index, local_date in enumerate(days if not hourly_only else []):
        values = {
            field: (
                float(daily[field][index])
                if daily.get(field) and daily[field][index] is not None
                else None
            )
            for field in (
                "shortwave_radiation_sum",
                "windspeed_10m_max",
                "cloudcover_mean",
                "precipitation_sum",
            )
        }
        if values["shortwave_radiation_sum"] is None:
            continue
        identity = {
            "schema": "station_forecast_physics_v1",
            "station_id": station_id,
            "local_date": str(local_date),
            "source_id": SOURCE_ID,
            **values,
            "latitude": float(scope["latitude"]),
            "longitude": float(scope["longitude"]),
            "settlement_timezone": settlement_timezone,
        }
        conn.execute(
            f"INSERT OR REPLACE INTO {TABLE}"
            "(station_id,local_date,source_id,shortwave_radiation_sum,"
            "windspeed_10m_max,cloudcover_mean,precipitation_sum,"
            "latitude,longitude,settlement_timezone,capture_mode,"
            "captured_at_utc,reference_only,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
            (
                station_id,
                str(local_date),
                SOURCE_ID,
                values["shortwave_radiation_sum"],
                values["windspeed_10m_max"],
                values["cloudcover_mean"],
                values["precipitation_sum"],
                float(scope["latitude"]),
                float(scope["longitude"]),
                settlement_timezone,
                mode,
                captured_at,
                content_sha256(identity),
            ),
        )
        versions += conn.execute(
            VERSION_COPY.format(
                where="m.station_id=? AND m.local_date=? AND m.source_id=?"
            ),
            (station_id, str(local_date), SOURCE_ID),
        ).rowcount
        written += 1
    hourly = record_hourly_precipitation(
        conn,
        station_id=station_id,
        payload=payload,
        scope=scope,
        settlement_timezone=settlement_timezone,
        mode=mode,
        captured_at=captured_at,
    )
    return {
        "station_id": station_id,
        "rows": written,
        "days": len(days),
        "versions": versions,
        **hourly,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data" / "bayes_basket_target_ssot.db",
    )
    parser.add_argument(
        "--mode", choices=("historical", "forward"), default="forward"
    )
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument(
        "--deadline-seconds", type=float, default=None,
        help="stop starting stations after this many seconds "
             f"(forward default {FORWARD_DEADLINE_SECONDS:g}; 0 = none)",
    )
    parser.add_argument("--station", action="append", default=None)
    parser.add_argument(
        "--hourly-only", action="store_true",
        help="write only the hourly precipitation versions (historical backfill)",
    )
    args = parser.parse_args(argv)

    from bayes_basket_target.production_worker import CANONICAL_TARGET_STATIONS

    # Runs beside the production cycle now that the forecast lane no longer
    # takes the writer turn: each station is its own short transaction, so a
    # commit only ever waits out one of production's -- bounded here rather
    # than failing the capture at SQLite's default.
    conn = sqlite3.connect(args.db, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    seeded_versions = ensure_schema(conn)
    timezones = {
        str(station_id): str(timezone_name)
        for station_id, timezone_name in conn.execute(
            "SELECT station_id,settlement_timezone "
            "FROM calibration_station_market_timezone_binding"
        )
    }
    today = date.today()
    start_date = args.start_date or (today - timedelta(days=120)).isoformat()
    end_date = args.end_date or (today - timedelta(days=1)).isoformat()
    stations = args.station or list(CANONICAL_TARGET_STATIONS)
    if args.mode == "forward":
        stations = stalest_first(conn, stations)
    deadline_seconds = (
        args.deadline_seconds if args.deadline_seconds is not None
        else FORWARD_DEADLINE_SECONDS if args.mode == "forward" else 0.0
    )
    started = time.monotonic()
    receipts = []
    deferred: list[str] = []
    # A forward round fetches every station it reaches first and writes them
    # in one short IMMEDIATE transaction: one wait for the write lock instead
    # of one per station.  2026-10-08: a station's writes take ~0.05 s, yet
    # the per-station commits cost ~12 s each behind the production writer.
    # A historical backfill runs for hours by hand, so it keeps committing
    # each station as it goes.
    fetched: list[tuple[str, str, dict]] = []
    for index, station_id in enumerate(stations):
        if deadline_seconds > 0 and time.monotonic() - started >= deadline_seconds:
            deferred = list(stations[index:])
            break
        settlement_timezone = timezones.get(station_id)
        if not settlement_timezone:
            receipts.append({
                "station_id": station_id,
                "state": "SKIPPED_NO_TIMEZONE_BINDING",
            })
            continue
        try:
            if args.mode == "forward":
                fetched.append((station_id, settlement_timezone, fetch_station(
                    station_id=station_id,
                    start_date=start_date,
                    end_date=end_date,
                    settlement_timezone=settlement_timezone,
                    mode=args.mode,
                    timeout_seconds=args.timeout_seconds,
                )))
            else:
                receipt = capture_station(
                    conn,
                    station_id=station_id,
                    start_date=start_date,
                    end_date=end_date,
                    settlement_timezone=settlement_timezone,
                    mode=args.mode,
                    timeout_seconds=args.timeout_seconds,
                    hourly_only=bool(args.hourly_only),
                )
                conn.commit()
                receipts.append({**receipt, "state": "CAPTURED"})
        except Exception as exc:  # noqa: BLE001 -- per-station isolation
            conn.rollback()
            receipts.append({
                "station_id": station_id,
                "state": "FAILED",
                "reason": f"{type(exc).__name__}:{str(exc)[:160]}",
            })
        if index + 1 < len(stations):
            time.sleep(REQUEST_SPACING_SECONDS)
    if fetched:
        receipts.extend(write_fetched(conn, fetched, mode=args.mode))
    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    version_total = conn.execute(
        f"SELECT COUNT(*) FROM {VERSION_TABLE}"
    ).fetchone()[0]
    hourly_total = conn.execute(
        f"SELECT COUNT(*) FROM {PRECIP_HOURLY_TABLE}"
    ).fetchone()[0]
    conn.close()
    print(json.dumps({
        "schema": "station_forecast_physics_capture_v1",
        "mode": args.mode,
        "hourly_only": bool(args.hourly_only),
        "window": {"start_date": start_date, "end_date": end_date},
        "station_count": len(stations),
        "captured": sum(1 for r in receipts if r.get("state") == "CAPTURED"),
        "failed": [r for r in receipts if r.get("state") != "CAPTURED"],
        "deferred_past_deadline": deferred,
        "table_rows": total,
        "version_rows": version_total,
        "versions_added": seeded_versions + sum(
            int(r.get("versions") or 0) for r in receipts
        ),
        "precip_hourly_rows": hourly_total,
        "precip_hourly_versions_added": sum(
            int(r.get("hourly_versions") or 0) for r in receipts
        ),
        "source_id": SOURCE_ID,
        "reference_only": True,
        "writes_truth_table": False,
        "calls_order_gateway": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
