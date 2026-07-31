#!/usr/bin/env python3
"""Backfill historical same-day native members from Previous Runs."""

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
from bayes_basket_target.native_ensemble_history import (  # noqa: E402
    MAX_HISTORY_DAYS,
    ingest_historical_archive,
)
from bayes_basket_target.native_ensemble_previous_runs import (  # noqa: E402
    capture_previous_runs_history,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--history-days", type=int, default=28)
    parser.add_argument("--ensemble-system-id", action="append")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    station = args.station_id.strip().upper()
    if args.history_days < 1 or args.history_days > MAX_HISTORY_DAYS:
        raise RuntimeError("native_previous_runs_history_days_invalid")
    database = args.db.expanduser().resolve(strict=True)
    with sqlite3.connect(
        f"file:{database}?mode=ro",
        uri=True,
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
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
    if len(dates) < args.history_days or not registry_rows:
        raise RuntimeError(
            "native_previous_runs_truth_or_registry_insufficient"
        )
    coordinates = station_coordinates(station)
    captured_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    archive = capture_previous_runs_history(
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
        "available_system_count_by_date": {
            row["event_date"]: len(row["systems"])
            for row in archive["captures"]
        },
        "request_receipts": archive["request_receipts"],
        "output": str(output),
        "output_wrote": wrote,
        "ingress": ingress,
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": False,
        "offline_chronological_oos_candidate": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
