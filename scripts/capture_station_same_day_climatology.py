#!/usr/bin/env python3
"""Capture a multi-year daily-high record per station, for same-day climatology.

The conditional tail layer's width term asks where a forecast sits inside the
station's own same-calendar-day record (charter 43).  Until now the only
record available was this season's settled truth -- 46 days, one season -- so
"same calendar day" degraded to "same seven-day window of this summer".  This
fills the actual thing: ten years of daily maxima at each station's own
coordinates, which makes the same-calendar-day window a real climatology.

Provenance is explicit and deliberately narrow.  These rows are ERA5
reanalysis served through the Open-Meteo archive; ERA5 is an official ECMWF
product, but this transport is an aggregator republisher and the values are a
gridded reanalysis at the station's coordinates, not the station's own
observations.  They are therefore **reference covariates only**: written to a
separate table, never to ``calibration_station_daily_high_truth``, never a
settlement truth, never a P0 probability authority, and never a substitute
for an official observation on a day the truth lane owns.  What they are used
for is one scalar -- how far today's forecast sits below the record high for
this calendar week at this station -- which is a slowly varying property of
the site, exactly what a reanalysis is good at.
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
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)

ARCHIVE_BASE_URL = "https://archive-api.open-meteo.com/v1/archive"
USER_AGENT = "weather-same-day-climatology/1.0"
SOURCE_ID = "OPEN_METEO_ERA5_ARCHIVE"
TABLE = "calibration_station_same_day_climatology"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_YEARS = 10
REQUEST_SPACING_SECONDS = 1.0

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    station_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    daily_high_c REAL NOT NULL,
    source_id TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    settlement_timezone TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    reference_only INTEGER NOT NULL CHECK (reference_only = 1),
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (station_id, local_date, source_id)
)
"""
INDEX = (
    f"CREATE INDEX IF NOT EXISTS {TABLE}_calendar_idx "
    f"ON {TABLE}(station_id, substr(local_date, 6))"
)


def _fetch_json(url: str, *, timeout_seconds: int) -> dict:
    """One bounded HTTPS read through the platform trust store.

    Python's urllib uses certifi, which does not contain the local proxy's
    inspection CA; the platform curl does.  Same reasoning and same shape as
    the AWC gap transport.
    """

    with tempfile.TemporaryDirectory(prefix="climatology-") as temporary_root:
        temporary = Path(temporary_root)
        if shutil.disk_usage(temporary).free < MAX_RESPONSE_BYTES + 256 * 1024 * 1024:
            raise OSError("climatology_transport_min_free_space_guard")
        body_path = temporary / "body"
        command = [
            "/usr/bin/curl",
            "--silent",
            "--show-error",
            "--location",
            "--max-redirs", "2",
            "--proto", "=https",
            "--proto-redir", "=https",
            "--tlsv1.2",
            "--connect-timeout", str(timeout_seconds),
            "--max-time", str(timeout_seconds * 2),
            "--max-filesize", str(MAX_RESPONSE_BYTES),
            "--user-agent", USER_AGENT,
            "--header", "Accept: application/json",
            "--output", str(body_path),
            "--write-out", "%{http_code}",
            url,
        ]
        completed = subprocess.run(
            command,
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
            raise OSError(f"climatology_http_status_{status}")
        return json.loads(body_path.read_text(encoding="utf-8"))


def capture_station(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    start_date: str,
    end_date: str,
    settlement_timezone: str,
    timeout_seconds: int,
) -> dict:
    scope = station_coordinates(station_id)
    query = urlencode({
        "latitude": scope["latitude"],
        "longitude": scope["longitude"],
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max",
        "timezone": settlement_timezone,
    })
    payload = _fetch_json(
        f"{ARCHIVE_BASE_URL}?{query}", timeout_seconds=timeout_seconds
    )
    daily = payload.get("daily") or {}
    days = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    if len(days) != len(highs):
        raise ValueError("climatology_series_length_mismatch")
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    written = 0
    for local_date, high in zip(days, highs):
        if high is None:
            continue
        identity = {
            "schema": "station_same_day_climatology_v1",
            "station_id": station_id,
            "local_date": str(local_date),
            "daily_high_c": float(high),
            "source_id": SOURCE_ID,
            "latitude": float(scope["latitude"]),
            "longitude": float(scope["longitude"]),
            "settlement_timezone": settlement_timezone,
        }
        conn.execute(
            f"INSERT OR IGNORE INTO {TABLE}"
            "(station_id,local_date,daily_high_c,source_id,latitude,longitude,"
            "settlement_timezone,captured_at_utc,reference_only,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,1,?)",
            (
                station_id,
                str(local_date),
                float(high),
                SOURCE_ID,
                float(scope["latitude"]),
                float(scope["longitude"]),
                settlement_timezone,
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
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--station", action="append", default=None)
    args = parser.parse_args(argv)

    from bayes_basket_target.production_worker import CANONICAL_TARGET_STATIONS

    conn = sqlite3.connect(args.db)
    conn.execute(SCHEMA)
    conn.execute(INDEX)
    conn.commit()
    timezones: dict[str, str] = {}
    for station_id, timezone_name in conn.execute(
        "SELECT station_id,settlement_timezone "
        "FROM calibration_station_market_timezone_binding"
    ):
        timezones[str(station_id)] = str(timezone_name)

    today = date.today()
    end_date = today.replace(year=today.year - 1).isoformat()
    start_date = today.replace(year=today.year - args.years).isoformat()
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
        "schema": "station_same_day_climatology_capture_v1",
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
