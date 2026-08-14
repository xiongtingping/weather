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
) -> dict:
    scope = station_coordinates(station_id)
    query = {
        "latitude": scope["latitude"],
        "longitude": scope["longitude"],
        "daily": DAILY_FIELDS,
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
    daily = payload.get("daily") or {}
    days = daily.get("time") or []
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    written = 0
    for index, local_date in enumerate(days):
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
        written += 1
    return {"station_id": station_id, "rows": written, "days": len(days)}


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
    parser.add_argument("--station", action="append", default=None)
    args = parser.parse_args(argv)

    from bayes_basket_target.production_worker import CANONICAL_TARGET_STATIONS

    conn = sqlite3.connect(args.db, timeout=30.0)
    conn.execute(SCHEMA)
    conn.execute(INDEX)
    conn.commit()
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
    receipts = []
    for index, station_id in enumerate(stations):
        settlement_timezone = timezones.get(station_id)
        if not settlement_timezone:
            receipts.append({
                "station_id": station_id,
                "state": "SKIPPED_NO_TIMEZONE_BINDING",
            })
            continue
        try:
            receipt = capture_station(
                conn,
                station_id=station_id,
                start_date=start_date,
                end_date=end_date,
                settlement_timezone=settlement_timezone,
                mode=args.mode,
                timeout_seconds=args.timeout_seconds,
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
    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    conn.close()
    print(json.dumps({
        "schema": "station_forecast_physics_capture_v1",
        "mode": args.mode,
        "window": {"start_date": start_date, "end_date": end_date},
        "station_count": len(stations),
        "captured": sum(1 for r in receipts if r.get("state") == "CAPTURED"),
        "failed": [r for r in receipts if r.get("state") != "CAPTURED"],
        "table_rows": total,
        "source_id": SOURCE_ID,
        "reference_only": True,
        "writes_truth_table": False,
        "calls_order_gateway": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
