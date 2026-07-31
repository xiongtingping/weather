#!/usr/bin/env python3
"""Capture and optionally ingest one official NOAA GEFS historical station-day."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.market_registry import (  # noqa: E402
    load_active_registry_rows,
)
from bayes_basket_target.noaa_gefs_archive import (  # noqa: E402
    capture_gefs_station_day,
    ingest_gefs_station_day,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--input",
        type=Path,
        help="Reuse a completed capture instead of downloading it again.",
    )
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    station = args.station_id.strip().upper()
    database = args.db.expanduser().resolve(strict=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
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
        if str(row.get("event_date") or "") == reference_event_date
    ]
    if not registry_rows:
        raise RuntimeError("gefs_archive_registry_missing")
    timezones = {
        str(row.get("settlement_timezone") or "").strip()
        for row in registry_rows
    }
    if len(timezones) != 1 or not next(iter(timezones)):
        raise RuntimeError("gefs_archive_registry_timezone_invalid")

    if args.input is not None:
        capture = load_bounded_json(args.input.expanduser().resolve())
        if (
            not isinstance(capture, dict)
            or capture.get("station_id") != station
            or capture.get("event_date") != args.event_date
        ):
            raise RuntimeError("gefs_archive_input_scope_mismatch")
        output = args.input.expanduser().resolve()
        output_wrote = False
    else:
        if args.output is None:
            raise RuntimeError("gefs_archive_output_required")
        coordinates = station_coordinates(station)
        queried_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        capture = capture_gefs_station_day(
            station_id=station,
            event_date=args.event_date,
            latitude=float(coordinates["latitude"]),
            longitude=float(coordinates["longitude"]),
            settlement_timezone=next(iter(timezones)),
            registry_rows=registry_rows,
            archive_query_at_utc=queried_at,
        )
        output = args.output.expanduser().resolve()
        output_wrote = write_bounded_json_if_changed(output, capture)
    ingress = None
    if args.commit:
        with sqlite3.connect(database, timeout=60.0) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            assert_connection_target_capacity(
                conn,
                recover_oversized_wal=True,
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                ingress = ingest_gefs_station_day(conn, capture)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    print(json.dumps({
        "station_id": station,
        "event_date": args.event_date,
        "ensemble_system_id": capture["ensemble_system_id"],
        "member_count": capture["member_count"],
        "field_receipt_count": capture["field_receipt_count"],
        "run_cycle": capture["run_cycle"],
        "output": str(output),
        "output_wrote": output_wrote,
        "ingress": ingress,
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
