#!/usr/bin/env python3
"""Capture official date-addressed AWC TAF for bounded station-days."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from scripts.capture_bayes_basket_target_awc_taf_calibration import (  # noqa: E402
    MAX_OUTPUT_BYTES,
    capture,
)


def _station_timezones(
    conn: sqlite3.Connection,
    requested: set[str],
) -> dict[str, str]:
    rows = conn.execute(
        "SELECT station_id,MIN(settlement_timezone),"
        "COUNT(DISTINCT settlement_timezone) "
        "FROM basket_target_market_registry "
        "WHERE station_id IS NOT NULL AND station_id<>'' "
        "AND settlement_timezone<>'' "
        "GROUP BY station_id ORDER BY station_id"
    ).fetchall()
    result = {
        str(row[0]).strip().upper(): str(row[1])
        for row in rows
        if int(row[2]) == 1
        and (not requested or str(row[0]).strip().upper() in requested)
    }
    expected_count = len(requested) if requested else 45
    if (
        len(result) != expected_count
        or len(result) > 45
        or requested - set(result)
    ):
        raise RuntimeError("historical_taf_station_scope_invalid")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--timeout-sec", type=int, default=20)
    parser.add_argument("--request-interval-sec", type=float, default=0.25)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_workers <= 4:
        raise RuntimeError("historical_taf_worker_count_invalid")
    requested = {
        str(value or "").strip().upper()
        for value in args.station_id
        if str(value or "").strip()
    }
    database = args.db.expanduser().resolve(strict=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        scopes = _station_timezones(conn, requested)
    destination = args.output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    def run(station: str, timezone_name: str) -> dict[str, object]:
        output = destination / (
            f"AWC_TAF_{station}_{args.start_date.replace('-', '')}_"
            f"{args.end_date.replace('-', '')}.json"
        )
        existing = (
            load_bounded_json(output)
            if args.resume and output.is_file()
            else None
        )
        artifact = capture(
            station_id=station,
            timezone_name=timezone_name,
            start_date=args.start_date,
            end_date=args.end_date,
            timeout_sec=args.timeout_sec,
            request_interval_sec=args.request_interval_sec,
            existing=existing,
        )
        write_bounded_json(
            output,
            artifact,
            limit=MAX_OUTPUT_BYTES,
        )
        return {
            "station_id": station,
            "output": str(output),
            "sample_count": int(artifact["sample_count"]),
            "excluded_sample_count": int(
                artifact["excluded_sample_count"]
            ),
            "blocker_count": len(artifact["blockers"]),
        }

    results = []
    failures = []
    with ThreadPoolExecutor(
        max_workers=args.max_workers,
        thread_name_prefix="awc-taf-history",
    ) as executor:
        pending = {
            executor.submit(run, station, timezone_name): station
            for station, timezone_name in scopes.items()
        }
        for future in as_completed(pending):
            station = pending[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append({
                    "station_id": station,
                    "reason": f"{type(exc).__name__}:{exc}",
                })
    results.sort(key=lambda row: str(row["station_id"]))
    failures.sort(key=lambda row: str(row["station_id"]))
    print(json.dumps({
        "station_count": len(scopes),
        "completed_station_count": len(results),
        "failed_station_count": len(failures),
        "sample_count": sum(
            int(row["sample_count"]) for row in results
        ),
        "excluded_sample_count": sum(
            int(row["excluded_sample_count"]) for row in results
        ),
        "blocker_count": sum(
            int(row["blocker_count"]) for row in results
        ),
        "failures": failures,
        "outputs": [row["output"] for row in results],
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
