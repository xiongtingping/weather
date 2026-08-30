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

from bayes_basket_target.fleet import expected_station_count
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
        or len(result) > expected_station_count()
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
    parser.add_argument(
        "--decision-anchor-hour",
        type=int,
        action="append",
        default=[],
        help=(
            "Hours after the local settlement day start to anchor a replay at; "
            "repeat for several anchors (default 1).  TAF is issued on a "
            "roughly six-hourly cycle, so 1/7/13/19 covers a day's decisions."
        ),
    )
    args = parser.parse_args()
    if not 1 <= args.max_workers <= 4:
        raise RuntimeError("historical_taf_worker_count_invalid")
    anchors = sorted({int(value) for value in args.decision_anchor_hour}) or [1]
    if any(not 0 < value < 24 for value in anchors) or len(anchors) > 8:
        raise RuntimeError("historical_taf_decision_anchor_scope_invalid")
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
        anchor_hour: int,
    ) -> dict[str, object]:
        timezone_name, source_lane = scope
        # The default anchor keeps its historical filename, so existing
        # artifacts stay resumable and every consumer that globs AWC_TAF_*
        # keeps finding them.  Further anchors are separate replays.
        suffix = "" if anchor_hour == 1 else f"_h{anchor_hour:02d}"
        output = destination / (
            f"AWC_TAF_{station}_{args.start_date.replace('-', '')}_"
            f"{args.end_date.replace('-', '')}{suffix}.json"
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
            decision_anchor_offset_hours=anchor_hour,
        )
        write_bounded_json(
            output,
            artifact,
            limit=MAX_OUTPUT_BYTES,
        )
        return {
            "station_id": station,
            "source_lane": source_lane,
            "decision_anchor_offset_hours": anchor_hour,
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
            executor.submit(run, station, scope, anchor): (station, anchor)
            for station, scope in scopes.items()
            for anchor in anchors
        }
        for future in as_completed(pending):
            station, anchor = pending[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append({
                    "station_id": station,
                    "decision_anchor_offset_hours": anchor,
                    "reason": f"{type(exc).__name__}:{exc}",
                })
    results.sort(
        key=lambda row: (
            str(row["station_id"]),
            int(row["decision_anchor_offset_hours"]),
        )
    )
    failures.sort(
        key=lambda row: (
            str(row["station_id"]),
            int(row.get("decision_anchor_offset_hours") or 0),
        )
    )
    receipt = {
        "station_count": len(scopes),
        "decision_anchor_offset_hours": anchors,
        # One run is one station at one anchor, so these stop equalling the
        # station count as soon as more than one anchor is requested.
        "completed_run_count": len(results),
        "failed_run_count": len(failures),
        "completed_station_count": len({
            str(row["station_id"]) for row in results
        }),
        "failed_station_count": len({
            str(row["station_id"]) for row in failures
        }),
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
