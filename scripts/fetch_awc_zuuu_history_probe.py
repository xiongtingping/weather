#!/usr/bin/env python3
"""Fetch one bounded ZUUU METAR history window from the official AWC API.

Research-only: the response and a sanitized manifest are written below an
explicit output directory.  The script never opens archive.db, writes facts or
signals, changes parameters, or starts a background process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ENDPOINT = "https://aviationweather.gov/api/data/metar"
SCHEMA = "awc_zuuu_history_probe_manifest_v1"
USER_AGENT = "weather-research-probe/1.0 (bounded ZUUU data-quality study)"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _report_time(row: Mapping[str, Any]) -> str:
    for key in ("reportTime", "obsTime", "observation_time", "receiptTime"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _station(row: Mapping[str, Any]) -> str:
    return str(row.get("icaoId") or row.get("station_id") or row.get("station") or "").strip().upper()


def fetch(*, date: str, hours: float, timeout: int) -> tuple[int, str, list[dict[str, Any]], dict[str, str]]:
    params = {
        "ids": "ZUUU",
        "format": "json",
        "taf": "false",
        "hours": f"{hours:g}",
        "date": date,
    }
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read().decode("utf-8", "replace")
            headers = {key: value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", "replace")
        headers = {key: value for key, value in exc.headers.items()}
        if status != 204:
            raise
    if status == 204 or not raw.strip():
        rows: list[dict[str, Any]] = []
    else:
        value = json.loads(raw)
        if isinstance(value, list):
            rows = [dict(row) for row in value if isinstance(row, Mapping)]
        elif isinstance(value, Mapping) and isinstance(value.get("data"), list):
            rows = [dict(row) for row in value["data"] if isinstance(row, Mapping)]
        else:
            raise ValueError("unexpected AWC JSON response shape")
    return status, url, rows, headers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="AWC query date in ISO 8601 or yyyymmdd_hhmm form")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.hours <= 360:
        raise SystemExit("probe hours must be in (0, 360], matching AWC's documented 15-day retention")

    started_at = _utc_now()
    status, url, rows, headers = fetch(date=args.date, hours=args.hours, timeout=args.timeout)
    completed_at = _utc_now()
    station_rows = [row for row in rows if _station(row) == "ZUUU"]
    report_times = sorted({_report_time(row) for row in station_rows if _report_time(row)})
    raw_reports = [str(row.get("rawOb") or row.get("raw_text") or row.get("raw") or "").strip() for row in station_rows]
    raw_reports = [value for value in raw_reports if value]
    response_path = args.output_dir / "awc_response.json"
    manifest_path = args.output_dir / "manifest.json"
    _atomic_json(response_path, station_rows)
    manifest = {
        "schema": SCHEMA,
        "provider": "NOAA Aviation Weather Center",
        "endpoint": ENDPOINT,
        "request": {"ids": "ZUUU", "format": "json", "taf": False, "hours": args.hours, "date": args.date},
        "request_started_at": started_at,
        "response_completed_at": completed_at,
        "response_status": status,
        "response_date_header": headers.get("Date", ""),
        "response_url": url,
        "returned_row_count": len(rows),
        "zuuu_row_count": len(station_rows),
        "distinct_report_time_count": len(report_times),
        "first_report_time": report_times[0] if report_times else None,
        "last_report_time": report_times[-1] if report_times else None,
        "distinct_raw_report_count": len(set(raw_reports)),
        "response_sha256": hashlib.sha256(response_path.read_bytes()).hexdigest(),
        "guards": {
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "historical_backfill": False,
        },
    }
    _atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0 if status in {200, 204} else 1


if __name__ == "__main__":
    raise SystemExit(main())
