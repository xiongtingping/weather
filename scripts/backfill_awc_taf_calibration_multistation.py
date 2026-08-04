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


def _station_scopes(
    conn: sqlite3.Connection,
    requested: set[str],
) -> dict[str, tuple[str, str]]:
    # A station can legitimately have both retired AMSC history and the
    # current global AWC registry row (the Chinese/Taiwan stations do).  Do
    # not let MIN(source_lane) select AMSC merely because it sorts first:
    # this backfill is explicitly an AWC calibration lane.  Keep the scope
    # fail-closed when timezones disagree or an AWC row is absent.
    rows = conn.execute(
        "SELECT station_id,settlement_timezone,UPPER(source_lane) "
        "FROM basket_target_market_registry "
        "WHERE station_id IS NOT NULL AND station_id<>'' "
        "AND settlement_timezone<>'' "
        "AND UPPER(source_lane) IN ('AMSC','AWC') "
        "ORDER BY station_id,UPPER(source_lane),settlement_timezone"
    ).fetchall()
    grouped: dict[str, list[tuple[str, str]]] = {}
    for station_id, timezone_name, source_lane in rows:
        station = str(station_id).strip().upper()
        if requested and station not in requested:
            continue
        grouped.setdefault(station, []).append(
            (str(timezone_name).strip(), str(source_lane).upper())
        )
    result: dict[str, tuple[str, str]] = {}
    for station, station_rows in grouped.items():
        timezones = {timezone_name for timezone_name, _lane in station_rows}
        awc_timezones = {
            timezone_name
            for timezone_name, lane in station_rows
            if lane == "AWC"
        }
        if len(timezones) == 1 and len(awc_timezones) == 1:
            result[station] = (next(iter(awc_timezones)), "AWC")
    expected_count = len(requested) if requested else 45
    if (
        len(result) != expected_count
        or len(result) > 45
        or requested - set(result)
    ):
        raise RuntimeError("historical_taf_station_scope_invalid")
    return result


# Keep the old helper name out of the execution path; callers that still
# import it must not receive a scope with an unbound lane.
def _station_timezones(
    conn: sqlite3.Connection,
    requested: set[str],
) -> dict[str, str]:
    return {
        station: timezone_name
        for station, (timezone_name, _source_lane) in _station_scopes(
            conn, requested
        ).items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--receipt",
        type=Path,
        help="Optional bounded run receipt, written for both success and fail-closed runs.",
    )
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
        scopes = _station_scopes(conn, requested)
    destination = args.output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    def run(
        station: str,
        scope: tuple[str, str],
    ) -> dict[str, object]:
        timezone_name, source_lane = scope
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
            source_lane=source_lane,
            existing=existing,
        )
        write_bounded_json(
            output,
            artifact,
            limit=MAX_OUTPUT_BYTES,
        )
        return {
            "station_id": station,
            "source_lane": source_lane,
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
            executor.submit(run, station, scope): station
            for station, scope in scopes.items()
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
    receipt = {
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
    }
    if args.receipt is not None:
        write_bounded_json(
            args.receipt.expanduser().resolve(),
            receipt,
            limit=MAX_OUTPUT_BYTES,
        )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
