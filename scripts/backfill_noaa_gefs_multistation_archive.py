#!/usr/bin/env python3
"""Capture one historical GEFS day once and ingest all active stations."""

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
from bayes_basket_target.market_registry import (  # noqa: E402
    load_active_registry_rows,
)
from bayes_basket_target.noaa_gefs_archive import (  # noqa: E402
    capture_gefs_station_days,
    ingest_gefs_station_days,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)


def _station_scopes(
    conn: sqlite3.Connection,
    *,
    selected_stations: set[str],
) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in load_active_registry_rows(conn):
        station = str(row.get("station_id") or "").strip().upper()
        if not selected_stations or station in selected_stations:
            grouped[station].append(row)
    if selected_stations - set(grouped):
        raise RuntimeError("gefs_multistation_registry_station_missing")
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
                "gefs_multistation_registry_timezone_invalid"
            )
        coordinates = station_coordinates(station)
        scopes.append({
            "station_id": station,
            "settlement_timezone": next(iter(timezones)),
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
            "registry_rows": rows,
        })
    if not scopes:
        raise RuntimeError("gefs_multistation_registry_empty")
    return scopes


def _input_station_scopes(
    archive: dict[str, object],
    *,
    selected_stations: set[str],
) -> list[dict[str, object]]:
    """Resolve a bounded historical input without requiring today's market.

    A closed-day capture is often intentionally partial: only stations whose
    settlement day has ended are downloaded.  Requiring that subset to equal
    the latest active market snapshot makes a valid historical archive
    impossible to ingest.  The archive itself owns the station scope here;
    the GEFS ingress still performs the full content/hash/member validation.
    """
    captures = archive.get("captures")
    if not isinstance(captures, list) or not captures:
        raise RuntimeError("gefs_multistation_input_captures_invalid")
    station_ids = {
        str(row.get("station_id") or "").strip().upper()
        for row in captures
        if isinstance(row, dict)
    }
    if not station_ids or "" in station_ids:
        raise RuntimeError("gefs_multistation_input_station_scope_invalid")
    declared = {
        str(value or "").strip().upper()
        for value in (archive.get("station_ids") or [])
        if str(value or "").strip()
    }
    if declared != station_ids:
        raise RuntimeError("gefs_multistation_input_station_scope_mismatch")
    if selected_stations and selected_stations != station_ids:
        raise RuntimeError("gefs_multistation_input_station_selection_mismatch")
    return [{"station_id": station} for station in sorted(station_ids)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--event-date", required=True)
    # P0 follows the freshest run, so history is backfilled per lead: the same
    # event date has a d2, a d1 and a d0 capture, each from a different run.
    parser.add_argument("--lead-days", type=int, default=2, choices=[0, 1, 2])
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    database = args.db.expanduser().resolve(strict=True)
    selected = {
        str(station).strip().upper()
        for station in args.station_id
        if str(station).strip()
    }
    if args.input is not None:
        archive = load_bounded_json(args.input.expanduser().resolve())
        if not isinstance(archive, dict) or archive.get("event_date") != args.event_date:
            raise RuntimeError("gefs_multistation_input_scope_mismatch")
        scopes = _input_station_scopes(
            archive,
            selected_stations=selected,
        )
        output = args.input.expanduser().resolve()
        output_wrote = False
    else:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            scopes = _station_scopes(
                conn,
                selected_stations=selected,
            )
        if args.output is None:
            raise RuntimeError("gefs_multistation_output_required")
        queried_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        archive = capture_gefs_station_days(
            lead_days=args.lead_days,
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
                ingress = ingest_gefs_station_days(conn, archive)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    print(json.dumps({
        "event_date": args.event_date,
        "station_count": archive["station_count"],
        "station_ids": archive["station_ids"],
        "union_forecast_hours": archive["union_forecast_hours"],
        "downloaded_field_receipt_count": archive[
            "downloaded_field_receipt_count"
        ],
        "single_download_per_global_field": archive[
            "single_download_per_global_field"
        ],
        "total_response_bytes": archive["total_response_bytes"],
        "output": str(output),
        "output_wrote": output_wrote,
        "ingress": (
            None
            if ingress is None
            else {
                "state": ingress["state"],
                "station_count": ingress["station_count"],
                "member_run_wrote_count": ingress[
                    "member_run_wrote_count"
                ],
                "member_bucket_wrote_count": ingress[
                    "member_bucket_wrote_count"
                ],
            }
        ),
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
