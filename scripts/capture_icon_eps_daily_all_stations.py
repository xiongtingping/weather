#!/usr/bin/env python3
"""Capture one ICON EPS 12Z run for all 45 stations, once per day.

DWD keeps exactly one run per cycle: the moment tomorrow's 12Z publishes, this
one is gone and its event date can never be reconstructed.  ECMWF, GEFS and
ECCC all expose rolling archives that can be backfilled; ICON's history exists
only if it is taken while the run is still live, so a missed day is missed
permanently.

This deliberately covers all 45 stations in one pass rather than following the
per-station capture rotation.  ICON objects are global fields -- one file holds
every member for one lead -- so a station-at-a-time rotation would re-download
the same ~543MiB for each of the 45 stations, and the retained-archive reuse
check keys on the exact station set, so it would not deduplicate them.

When the target date's registry is still incomplete, stations missing from it
borrow their scope from the newest complete 45-station snapshot.  The member
values are properties of the run and are unaffected; the bucket intervals such
rows carry belong to the borrowed date and must not be treated as settlement
authority.
"""

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
from bayes_basket_target.dwd_icon_eps_archive import (  # noqa: E402
    capture_icon_eps_station_days,
    ingest_icon_eps_station_days,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    write_bounded_json_if_changed,
)
from scripts.backfill_eccc_geps_multistation_archive import (  # noqa: E402
    _snapshot_registry_rows,
)

EXPECTED_STATION_COUNT = 45


def _widest_snapshot(
    conn: sqlite3.Connection,
    *,
    event_date: str,
) -> tuple[str, int] | None:
    row = conn.execute(
        "SELECT snapshot_sha256,COUNT(DISTINCT station_id) AS covered "
        "FROM basket_target_market_registry "
        "WHERE event_date=? AND source_eligible=1 "
        "GROUP BY snapshot_sha256 "
        "ORDER BY covered DESC,snapshot_sha256 "
        "LIMIT 1",
        (event_date,),
    ).fetchone()
    return (str(row[0]), int(row[1])) if row is not None else None


def _latest_complete_date(conn: sqlite3.Connection) -> str:
    """Newest event date whose registry actually covers all 45 stations."""

    row = conn.execute(
        "SELECT event_date FROM ("
        "  SELECT event_date,snapshot_sha256,"
        "         COUNT(DISTINCT station_id) covered"
        "  FROM basket_target_market_registry"
        "  WHERE source_eligible=1"
        "  GROUP BY event_date,snapshot_sha256"
        ") WHERE covered>=? ORDER BY event_date DESC LIMIT 1",
        (EXPECTED_STATION_COUNT,),
    ).fetchone()
    if row is None:
        raise RuntimeError("icon_rescue_no_complete_registry_snapshot")
    return str(row[0])


def _scopes(
    conn: sqlite3.Connection,
    *,
    event_date: str,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    from bayes_basket_target.station_registry import station_coordinates

    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    native = _widest_snapshot(conn, event_date=event_date)
    if native is not None:
        for row in _snapshot_registry_rows(
            conn, event_date=event_date, snapshot_sha256=native[0]
        ):
            grouped[str(row["station_id"]).strip().upper()].append(row)
    native_stations = set(grouped)

    borrowed_from = ""
    if len(grouped) < EXPECTED_STATION_COUNT:
        borrowed_from = _latest_complete_date(conn)
        borrowed = _widest_snapshot(conn, event_date=borrowed_from)
        if borrowed is None:
            raise RuntimeError("icon_rescue_no_complete_registry_snapshot")
        # Decide which stations to borrow *before* copying any rows.  Testing
        # membership while filling would admit only each station's first bucket
        # and silently drop the other ten, leaving an interval set with no tail.
        borrow_stations = {
            str(row["station_id"]).strip().upper()
            for row in _snapshot_registry_rows(
                conn,
                event_date=borrowed_from,
                snapshot_sha256=borrowed[0],
            )
        } - native_stations
        for row in _snapshot_registry_rows(
            conn, event_date=borrowed_from, snapshot_sha256=borrowed[0]
        ):
            station = str(row["station_id"]).strip().upper()
            if station in borrow_stations:
                grouped[station].append(row)

    scopes: list[dict[str, object]] = []
    for station, rows in sorted(grouped.items()):
        timezones = {
            str(row.get("settlement_timezone") or "").strip()
            for row in rows
        }
        if len(timezones) != 1 or not next(iter(timezones)):
            raise RuntimeError("icon_rescue_registry_timezone_invalid")
        coordinates = station_coordinates(station)
        scopes.append({
            "station_id": station,
            "settlement_timezone": next(iter(timezones)),
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
            "registry_rows": rows,
        })
    provenance = {
        "stations_total": len(scopes),
        "stations_from_target_date_registry": len(native_stations),
        "stations_borrowed": len(scopes) - len(native_stations),
        "borrowed_scope_event_date": borrowed_from,
        "station_scope_authority": (
            "HISTORICAL_EVENT_DATE_REGISTRY"
            if not borrowed_from
            else "CURRENT_45_STATION_REGISTRY_OFFLINE_TRAIN_ONLY"
        ),
        "borrowed_bucket_intervals_are_settlement_authority": False,
    }
    return scopes, provenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()

    database = args.db.expanduser().resolve(strict=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        scopes, provenance = _scopes(conn, event_date=args.event_date)

    queried_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    archive = capture_icon_eps_station_days(
        event_date=args.event_date,
        station_scopes=scopes,
        archive_query_at_utc=queried_at,
        timeout_seconds=args.timeout_seconds,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    wrote = write_bounded_json_if_changed(output, archive)

    ingress = None
    if args.commit:
        with sqlite3.connect(database, timeout=60.0) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            assert_connection_target_capacity(
                conn, recover_oversized_wal=True
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                ingress = ingest_icon_eps_station_days(conn, archive)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    print(json.dumps({
        "event_date": args.event_date,
        "run_init_utc": archive["run_init_utc"],
        "run_cycle": archive["run_cycle"],
        "station_count": archive["station_count"],
        "member_count": archive["member_count"],
        "union_forecast_hours": archive["union_forecast_hours"],
        "total_response_bytes": archive["total_response_bytes"],
        "output": str(output),
        "output_wrote": wrote,
        "ingress": ingress,
        **provenance,
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
