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

from bayes_basket_target.contracts import ContractError  # noqa: E402
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


def _station_scopes(
    conn: sqlite3.Connection,
    *,
    selected_stations: set[str],
) -> tuple[list[dict[str, object]], list[str]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    without_coordinates: list[str] = []
    for row in load_active_registry_rows(conn):
        station = str(row.get("station_id") or "").strip().upper()
        if not selected_stations or station in selected_stations:
            grouped[station].append(row)
    if selected_stations - set(grouped):
        raise RuntimeError("ecmwf_archive_registry_station_missing")
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
        try:
            coordinates = station_coordinates(station)
        except ContractError as exc:
            # The market roster is wider than the coordinate roster (LLBG,
            # LTFM, UUWW).  The forward capture skips them too; never decode
            # a station at a guessed location.
            if (
                str(exc) != "native_station_coordinates_missing"
                or station in selected_stations
            ):
                raise
            without_coordinates.append(station)
            continue
        scopes.append({
            "station_id": station,
            "settlement_timezone": next(iter(timezones)),
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
            "registry_rows": rows,
        })
    # Every active station, not a fixed 45: the registry grew to 48 and the
    # capture is per global field, so station count costs no extra download.
    if not scopes:
        raise RuntimeError("ecmwf_archive_registry_empty")
    return scopes, without_coordinates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--event-date", required=True)
    # P0 follows the freshest run, so history is backfilled per lead: the same
    # event date has a d2, a d1 and a d0 capture, each from a different run.
    parser.add_argument("--lead-days", type=int, default=2, choices=[0, 1, 2])
    # The cycle defaults to the lead's own mapping (d2 18Z, d1/d0 00Z).  The
    # hour bucket a row is stamped with is per station and per run, so the
    # tradable h12_24/h24_48 scopes of the Americas only come from the 12Z
    # runs, which no lead default names.
    parser.add_argument(
        "--run-cycle-hour", type=int, default=None, choices=[0, 6, 12, 18]
    )
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
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        scopes, without_coordinates = _station_scopes(
            conn, selected_stations=selected
        )
    if args.input is not None:
        archive = load_bounded_json(args.input.expanduser().resolve())
        if (
            not isinstance(archive, dict)
            or archive.get("event_date") != args.event_date
            or int(archive.get("station_count") or 0) != len(scopes)
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
            lead_days=args.lead_days,
            run_cycle_hour=args.run_cycle_hour,
            station_scopes=scopes,
            archive_query_at_utc=queried_at,
        )
        output = args.output.expanduser().resolve()
        output_wrote = write_bounded_json_if_changed(output, archive)
    ingress = None
    if args.commit:
        # The resident cycle can hold the SSOT write lock for minutes; a
        # historical ingest has nothing to lose by waiting for it.
        with sqlite3.connect(database, timeout=300.0) as conn:
            conn.execute("PRAGMA busy_timeout=300000")
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
        "stations_without_coordinates": without_coordinates,
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
