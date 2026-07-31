#!/usr/bin/env python3
"""Capture one official ECMWF PF50 historical day for active stations."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.ecmwf_ifs_archive import (  # noqa: E402
    capture_ecmwf_station_days,
    ingest_ecmwf_station_days,
)
from bayes_basket_target.market_registry import (  # noqa: E402
    load_active_registry_rows,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)


def _station_scopes(conn: sqlite3.Connection) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in load_active_registry_rows(conn):
        station = str(row.get("station_id") or "").strip().upper()
        grouped[station].append(row)
    scopes = []
    for station, rows in sorted(grouped.items()):
        reference_date = max(
            str(row.get("event_date") or "") for row in rows
        )
        rows = [
            row
            for row in rows
            if str(row.get("event_date") or "") == reference_date
        ]
        timezones = {
            str(row.get("settlement_timezone") or "").strip()
            for row in rows
        }
        if len(timezones) != 1 or not next(iter(timezones)):
            raise RuntimeError(
                "ecmwf_archive_registry_timezone_invalid"
            )
        coordinates = station_coordinates(station)
        scopes.append({
            "station_id": station,
            "settlement_timezone": next(iter(timezones)),
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
            "registry_rows": rows,
        })
    if len(scopes) != 45:
        raise RuntimeError("ecmwf_archive_45station_registry_required")
    return scopes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        scopes = _station_scopes(conn)
    if args.input is not None:
        archive = load_bounded_json(args.input.expanduser().resolve())
        if (
            not isinstance(archive, dict)
            or archive.get("event_date") != args.event_date
            or int(archive.get("station_count") or 0) != 45
        ):
            raise RuntimeError("ecmwf_archive_input_scope_mismatch")
        output = args.input.expanduser().resolve()
        output_wrote = False
    else:
        if args.output is None:
            raise RuntimeError("ecmwf_archive_output_required")
        queried_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        archive = capture_ecmwf_station_days(
            event_date=args.event_date,
            station_scopes=scopes,
            archive_query_at_utc=queried_at,
        )
        output = args.output.expanduser().resolve()
        output_wrote = write_bounded_json_if_changed(output, archive)
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
                ingress = ingest_ecmwf_station_days(conn, archive)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    print(json.dumps({
        "event_date": args.event_date,
        "station_count": archive["station_count"],
        "member_count": archive["member_count"],
        "member_availability_contract": archive[
            "member_availability_contract"
        ],
        "union_forecast_steps": archive["union_forecast_steps"],
        "downloaded_field_receipt_count": archive[
            "downloaded_field_receipt_count"
        ],
        "total_response_bytes": archive["total_response_bytes"],
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
