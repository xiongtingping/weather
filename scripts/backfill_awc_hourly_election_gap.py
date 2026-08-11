#!/usr/bin/env python3
"""Backfill the AWC routine METARs discarded by the literal-`:00` contract.

Between 2026-08-01T02:00Z and the hourly-election fix, ``_preview_observation``
raised ``nonformal_source_report`` for every routine METAR that did not land
exactly on the hour, so those reports were never persisted at all.  Seventeen
stations publish on no other cadence and therefore have no daily-high truth for
that period; this script re-fetches the window and replays it through the one
canonical ingress so the rows arrive with identical parsing, lineage,
deduplication and correction-halt semantics as the live lane.

It must fetch one station per request.  The AWC raw endpoint caps a *response*
at 400 reports regardless of how many stations it covers, so the production
45-station batch silently collapses to roughly the last five hours no matter
what ``hours`` asks for.  Per-station requests get the full 400-report quota
each, which reaches back about eight days.

Writes observations only.  No orders, no CLOB, no capital surface.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reported_trajectory_awc_ingress import (  # noqa: E402
    ReportedTrajectoryAwcIngress,
)
from reported_trajectory_market_registry import load_config  # noqa: E402
from reported_trajectory_sources import (  # noqa: E402
    fetch_awc_temperature_reports,
    utc_now,
)

# The endpoint's own per-response ceiling.  Asking for more simply returns the
# same 400 newest reports, so a larger value would only misreport coverage.
MAX_LOOKBACK_HOURS = 240.0


def _active_awc_stations(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT station_id
                 FROM reported_trajectory_market_registry
                WHERE source_lane='awc' AND source_eligible=1
                  AND COALESCE(station_id,'')<>''
                ORDER BY station_id"""
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--hours", type=float, default=MAX_LOOKBACK_HOURS)
    parser.add_argument("--station", action="append", default=None)
    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=1.0,
        help="pause between per-station requests to stay polite to AWC",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--date-anchor",
        default=None,
        help=(
            "Read the window ending at this UTC instant instead of now; "
            "how a day deeper in AWC's 30-day retention is reached past "
            "the 400-report response cap."
        ),
    )
    args = parser.parse_args()

    hours = max(1.0, min(MAX_LOOKBACK_HOURS, float(args.hours)))
    config = load_config()
    conn = sqlite3.connect(str(args.db), timeout=60.0)
    # The live daemon holds this database; wait rather than fail a batch.
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        stations = (
            [str(value).strip().upper() for value in args.station]
            if args.station
            else _active_awc_stations(conn)
        )
        ingress = ReportedTrajectoryAwcIngress(conn, config=config)
        summary: list[dict[str, object]] = []
        total_new = 0
        for index, station in enumerate(stations):
            request_started_at = utc_now()
            poll = dict(
                fetch_awc_temperature_reports(
                    [station], hours=hours, timeout=20.0,
                    date_anchor=args.date_anchor,
                )
            )
            poll["source_lane"] = "awc"
            response_completed_at = utc_now()
            report_count = len(poll.get("reports") or [])
            if args.dry_run:
                summary.append({
                    "station_id": station,
                    "fetched_report_count": report_count,
                    "new_observation_count": 0,
                    "dry_run": True,
                })
                continue
            try:
                result = ingress.process_source_poll(
                    poll,
                    request_started_at=request_started_at,
                    response_completed_at=response_completed_at,
                )
            except Exception as exc:  # one station must not abort the sweep
                summary.append({
                    "station_id": station,
                    "fetched_report_count": report_count,
                    "error": f"{type(exc).__name__}:{exc}",
                })
                continue
            new_count = int(result.get("new_observation_count") or 0)
            total_new += new_count
            summary.append({
                "station_id": station,
                "fetched_report_count": report_count,
                "new_observation_count": new_count,
                "station_day_fact_updates": len(
                    result.get("station_day_fact_updates") or []
                ),
            })
            if index + 1 < len(stations):
                time.sleep(max(0.0, float(args.sleep_sec)))
        print(json.dumps(
            {
                "schema": "awc_hourly_election_backfill_v1",
                "db": str(args.db),
                "requested_hours": hours,
                "station_count": len(stations),
                "new_observation_count": total_new,
                "stations": summary,
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
