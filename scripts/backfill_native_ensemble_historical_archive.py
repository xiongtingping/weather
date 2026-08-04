#!/usr/bin/env python3
"""Backfill one station's training-only Open-Meteo ensemble archive."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.market_registry import (  # noqa: E402
    load_active_registry_rows,
)
from bayes_basket_target.native_ensemble_history import (  # noqa: E402
    MAX_HISTORY_DAYS,
    capture_historical_archive,
    ingest_historical_archive,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--station-id", required=True)
    parser.add_argument(
        "--history-days",
        type=int,
        default=20,
    )
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--ensemble-system-id",
        action="append",
        help=(
            "Limit the Open-Meteo request to one or more registered systems. "
            "Repeat for multiple systems; omitted means the full catalog."
        ),
    )
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    station = args.station_id.strip().upper()
    if args.history_days < 1 or args.history_days > MAX_HISTORY_DAYS:
        raise RuntimeError("native_ensemble_history_days_invalid")
    if bool(args.start_date) != bool(args.end_date):
        raise RuntimeError("native_ensemble_history_date_range_incomplete")
    requested_range = None
    if args.start_date:
        try:
            start_date = date.fromisoformat(str(args.start_date).strip())
            end_date = date.fromisoformat(str(args.end_date).strip())
        except ValueError as exc:
            raise RuntimeError("native_ensemble_history_date_invalid") from exc
        if end_date < start_date or (end_date - start_date).days >= MAX_HISTORY_DAYS:
            raise RuntimeError("native_ensemble_history_date_range_invalid")
        requested_range = (start_date.isoformat(), end_date.isoformat())
    database = args.db.expanduser().resolve(strict=True)
    with sqlite3.connect(
        f"file:{database}?mode=ro",
        uri=True,
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        if requested_range is None:
            dates = [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT local_date "
                    "FROM calibration_station_daily_high_truth "
                    "WHERE station_id=? AND qc_status='ACCEPTED' "
                    "AND authority_scope IN "
                    "('FORMAL_NATIVE_ELIGIBLE',"
                    "'NO_LIVE_REPLAY_CANDIDATE') "
                    "ORDER BY local_date DESC LIMIT ?",
                    (station, args.history_days),
                )
            ]
        else:
            dates = [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT local_date "
                    "FROM calibration_station_daily_high_truth "
                    "WHERE station_id=? AND qc_status='ACCEPTED' "
                    "AND authority_scope IN "
                    "('FORMAL_NATIVE_ELIGIBLE',"
                    "'NO_LIVE_REPLAY_CANDIDATE') "
                    "AND local_date BETWEEN ? AND ? "
                    "ORDER BY local_date",
                    (station, requested_range[0], requested_range[1]),
                )
            ]
        registry_rows = load_active_registry_rows(
            conn,
            station_id=station,
        )
        reference_event_date = max(
            (
                str(row.get("event_date") or "")
                for row in registry_rows
                if str(row.get("event_date") or "")
            ),
            default="",
        )
        registry_rows = [
            row
            for row in registry_rows
            if str(row.get("event_date") or "")
            == reference_event_date
        ]
    required_date_count = (
        args.history_days
        if requested_range is None
        else (date.fromisoformat(requested_range[1]) - date.fromisoformat(requested_range[0])).days + 1
    )
    if len(dates) < required_date_count or not registry_rows:
        raise RuntimeError(
            "native_ensemble_history_truth_or_registry_insufficient"
        )
    coordinates = station_coordinates(station)
    captured_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    archive = capture_historical_archive(
        station_id=station,
        event_dates=dates,
        latitude=float(coordinates["latitude"]),
        longitude=float(coordinates["longitude"]),
        settlement_timezone=str(
            registry_rows[0]["settlement_timezone"]
        ),
        registry_rows=registry_rows,
        archive_query_at_utc=captured_at,
        ensemble_system_ids=args.ensemble_system_id,
    )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, archive)
    ingress = None
    if args.commit:
        with sqlite3.connect(database, timeout=60.0) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            assert_connection_target_capacity(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                ingress = ingest_historical_archive(conn, archive)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    print(json.dumps({
        "station_id": station,
        "event_date_count": len(dates),
        "successful_event_date_count": int(
            archive["successful_event_date_count"]
        ),
        "unavailable_event_date_count": int(
            archive["unavailable_event_date_count"]
        ),
        "available_system_count_by_date": {
            row["event_date"]: len(row["systems"])
            for row in archive["captures"]
        },
        "output": str(output),
        "output_wrote": wrote,
        "ingress": ingress,
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": False,
        "selected_system_ids": archive.get("selected_system_ids", []),
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
