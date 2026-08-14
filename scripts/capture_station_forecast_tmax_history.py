#!/usr/bin/env python3
"""Capture years of past daily-max forecasts, for the neighbour-tilt study.

The tilt the served distribution is missing -- the truth leans one bucket
warm and falls away faster on the cold side -- is estimated today on 47
calendar days of settled truth.  That is enough to see it fleet-wide and not
enough to resolve how it varies with latitude or season: the high-latitude
group reads a 1.4-1.5 warm factor against 1.0-1.2 elsewhere, but grouping
loses on CRPS because each group's estimate gets noisier faster than its
shape gets truer.

More days is the only fix, and the settlement lane cannot supply them (AWC
retention is a month).  This lane pairs two sources that can: the archived
past *forecast* of the daily maximum, and the ERA5 daily maximum already
captured for the same-day climatology.  Neither is the settlement truth and
neither may become it -- both are reference covariates, written to their own
tables, never to ``calibration_station_daily_high_truth``.

What transfers from such a pair is structure, not level: a gridded forecast
against a gridded analysis has its own error scale, so the factors fitted
here are a shape prior over latitude and season, to be validated against the
production pipeline's own 47 days before anything is served.  The fitting
script does that check and refuses the prior if it does not transfer.
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

BASE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
USER_AGENT = "weather-tilt-research/1.0"
SOURCE_ID = "OPEN_METEO_HISTORICAL_FORECAST"
TABLE = "research_station_forecast_tmax_history"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
REQUEST_SPACING_SECONDS = 1.2

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    station_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    forecast_tmax_c REAL NOT NULL,
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
    f"CREATE INDEX IF NOT EXISTS {TABLE}_station_idx "
    f"ON {TABLE}(station_id, local_date)"
)


def _fetch_json(url: str, *, timeout_seconds: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="tilt-research-") as root:
        temporary = Path(root)
        if shutil.disk_usage(temporary).free < MAX_RESPONSE_BYTES + 256 * 1024 * 1024:
            raise OSError("tilt_research_min_free_space_guard")
        body = temporary / "body"
        completed = subprocess.run(
            [
                "/usr/bin/curl",
                "--silent", "--show-error", "--location",
                "--max-redirs", "2",
                "--proto", "=https", "--proto-redir", "=https",
                "--tlsv1.2",
                "--connect-timeout", str(timeout_seconds),
                "--max-time", str(timeout_seconds * 3),
                "--max-filesize", str(MAX_RESPONSE_BYTES),
                "--user-agent", USER_AGENT,
                "--header", "Accept: application/json",
                "--output", str(body),
                "--write-out", "%{http_code}",
                url,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds * 3 + 10,
        )
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", "replace").strip()[:200]
            raise OSError(f"curl_exit_{completed.returncode}:{message}")
        status = int(completed.stdout.decode("ascii").strip() or "0")
        payload = json.loads(body.read_text(encoding="utf-8"))
        if status != 200:
            raise OSError(
                f"tilt_research_http_{status}:"
                f"{str(payload.get('reason') or '')[:120]}"
            )
        return payload


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
    payload = _fetch_json(
        BASE_URL
        + "?"
        + urlencode({
            "latitude": scope["latitude"],
            "longitude": scope["longitude"],
            "start_date": start_date,
            "end_date": end_date,
            "daily": "temperature_2m_max",
            "timezone": settlement_timezone,
        }),
        timeout_seconds=timeout_seconds,
    )
    daily = payload.get("daily") or {}
    days = daily.get("time") or []
    values = daily.get("temperature_2m_max") or []
    if len(days) != len(values):
        raise ValueError("tilt_research_series_length_mismatch")
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    written = 0
    for local_date, value in zip(days, values):
        if value is None:
            continue
        identity = {
            "schema": "research_station_forecast_tmax_v1",
            "station_id": station_id,
            "local_date": str(local_date),
            "source_id": SOURCE_ID,
            "forecast_tmax_c": float(value),
            "latitude": float(scope["latitude"]),
            "longitude": float(scope["longitude"]),
            "settlement_timezone": settlement_timezone,
        }
        conn.execute(
            f"INSERT OR IGNORE INTO {TABLE}"
            "(station_id,local_date,source_id,forecast_tmax_c,latitude,"
            "longitude,settlement_timezone,captured_at_utc,reference_only,"
            "content_sha256) VALUES(?,?,?,?,?,?,?,?,1,?)",
            (
                station_id,
                str(local_date),
                SOURCE_ID,
                float(value),
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
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--station", action="append", default=None)
    parser.add_argument(
        "--skip-complete",
        action="store_true",
        help="Skip a station that already covers the window; makes the "
             "pull resumable across days when the API quota runs out.",
    )
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
    end_date = args.end_date or (date.today().replace(day=1)).isoformat()
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
        if args.skip_complete:
            covered = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} "
                "WHERE station_id=? AND local_date BETWEEN ? AND ?",
                (station_id, args.start_date, end_date),
            ).fetchone()[0]
            span = (
                date.fromisoformat(end_date)
                - date.fromisoformat(args.start_date)
            ).days
            if covered >= span * 0.95:
                receipts.append({
                    "station_id": station_id,
                    "state": "ALREADY_COVERED",
                    "rows": covered,
                })
                continue
        try:
            receipt = capture_station(
                conn,
                station_id=station_id,
                start_date=args.start_date,
                end_date=end_date,
                settlement_timezone=settlement_timezone,
                timeout_seconds=args.timeout_seconds,
            )
            conn.commit()
            receipts.append({**receipt, "state": "CAPTURED"})
        except Exception as exc:  # noqa: BLE001 -- per-station isolation
            conn.rollback()
            reason = f"{type(exc).__name__}:{str(exc)[:160]}"
            receipts.append({
                "station_id": station_id,
                "state": "FAILED",
                "reason": reason,
            })
            if "http_429" in reason:
                # The daily quota is a hard wall, not a transient error.
                # Stop cleanly so the next run resumes with --skip-complete
                # instead of burning the next day's quota on retries.
                receipts.append({"state": "STOPPED_ON_QUOTA"})
                break
        if index + 1 < len(stations):
            time.sleep(REQUEST_SPACING_SECONDS)
    total, stations_done = conn.execute(
        f"SELECT COUNT(*),COUNT(DISTINCT station_id) FROM {TABLE}"
    ).fetchone()
    conn.close()
    print(json.dumps({
        "schema": "research_forecast_tmax_capture_v1",
        "window": {"start_date": args.start_date, "end_date": end_date},
        "station_count": len(stations),
        "captured": sum(1 for r in receipts if r.get("state") == "CAPTURED"),
        "already_covered": sum(
            1 for r in receipts if r.get("state") == "ALREADY_COVERED"
        ),
        "failed": [r for r in receipts if r.get("state") == "FAILED"][:5],
        "stopped_on_quota": any(
            r.get("state") == "STOPPED_ON_QUOTA" for r in receipts
        ),
        "table_rows": total,
        "table_stations": stations_done,
        "source_id": SOURCE_ID,
        "reference_only": True,
        "writes_truth_table": False,
        "calls_order_gateway": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
