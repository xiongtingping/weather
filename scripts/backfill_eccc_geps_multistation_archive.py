#!/usr/bin/env python3
"""Capture one official ECCC GEPS historical day for active stations."""

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
from bayes_basket_target.eccc_geps_archive import (  # noqa: E402
    RUN_CYCLE_HOUR_BY_LEAD,
    SUPPORTED_RUN_CYCLE_HOURS,
    capture_geps_station_days,
    ingest_geps_station_days,
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


def _snapshot_registry_rows(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    snapshot_sha256: str,
) -> list[dict[str, object]]:
    """Read one atomic registry snapshot, mirroring the canonical row shape."""

    rows = conn.execute(
        "SELECT slug,event_date,station_id,settlement_timezone,settlement_unit,"
        "outcome,yes_token,registry_json,source_lane "
        "FROM basket_target_market_registry "
        "WHERE event_date=? AND snapshot_sha256=? AND source_eligible=1 "
        "ORDER BY station_id,event_date,slug,outcome",
        (event_date, snapshot_sha256),
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        registry_json = str(row[7] or "{}")
        raw = json.loads(registry_json)
        if not isinstance(raw, dict):
            raise RuntimeError("eccc_geps_archive_registry_json_invalid")
        item = {
            "slug": str(row[0]),
            "event_date": str(row[1]),
            "station_id": str(row[2] or ""),
            "settlement_timezone": str(row[3] or ""),
            "settlement_unit": str(row[4] or ""),
            "outcome": str(row[5] or ""),
            "yes_token": str(row[6] or ""),
            "registry_json": registry_json,
            "source_lane": str(row[8] or "").strip().lower(),
            "market_id": str(raw.get("market_id") or ""),
            "lower": raw.get("lower"),
            "upper": raw.get("upper"),
        }
        if (
            not item["station_id"]
            or not item["outcome"]
            or item["source_lane"] not in {"amsc", "awc"}
        ):
            raise RuntimeError("eccc_geps_archive_registry_identity_missing")
        result.append(item)
    return result


def _station_scopes(
    conn: sqlite3.Connection,
    *,
    event_date: str,
) -> list[dict[str, object]]:
    """Freeze scopes to one historical market date, never today's registry.

    The scope must come from the snapshot that actually covered the 45-station
    market on that date.  `load_active_registry_rows` resolves the *latest*
    snapshot for the date, which for a settled day is the one taken as its
    markets were closing: 2026-07-31's newest snapshot holds 3 stations, while
    earlier snapshots that day hold all 45.  Selecting by recency therefore made
    every historical backfill fail the 45-station requirement, which is exactly
    the data an out-of-sample holdout needs.

    Choosing the widest snapshot keeps the scope internally consistent -- all
    rows still come from one atomic registry snapshot -- while restoring the
    coverage the date genuinely had.
    """

    snapshot = conn.execute(
        "SELECT snapshot_sha256,COUNT(DISTINCT station_id) AS covered "
        "FROM basket_target_market_registry "
        "WHERE event_date=? AND source_eligible=1 "
        "GROUP BY snapshot_sha256 "
        "ORDER BY covered DESC,snapshot_sha256 "
        "LIMIT 1",
        (event_date,),
    ).fetchone()
    if snapshot is None:
        raise RuntimeError("eccc_geps_archive_registry_snapshot_missing")
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in _snapshot_registry_rows(
        conn, event_date=event_date, snapshot_sha256=str(snapshot[0])
    ):
        station = str(row.get("station_id") or "").strip().upper()
        grouped[station].append(row)
    scopes = []
    for station, rows in sorted(grouped.items()):
        if any(str(row.get("event_date") or "") != event_date for row in rows):
            raise RuntimeError("eccc_geps_archive_registry_event_date_mixed")
        timezones = {
            str(row.get("settlement_timezone") or "").strip()
            for row in rows
        }
        if len(timezones) != 1 or not next(iter(timezones)):
            raise RuntimeError(
                "eccc_geps_archive_registry_timezone_invalid"
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
        raise RuntimeError("eccc_geps_archive_45station_registry_required")
    return scopes


def _current_station_scopes(
    conn: sqlite3.Connection,
) -> list[dict[str, object]]:
    """Reuse the latest complete 45-station contract for offline backfill.

    This is a download-scope fallback only.  It never represents historical
    market arrival and callers must keep the resulting archive train-only.
    """

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
                "eccc_geps_archive_current_registry_timezone_invalid"
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
        raise RuntimeError(
            "eccc_geps_archive_current_45station_registry_required"
        )
    return scopes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--event-date", required=True)
    # P0 follows the freshest run, so history is backfilled per lead: the same
    # event date has a d2, a d1 and a d0 capture, each from a different run.
    parser.add_argument("--lead-days", type=int, default=2, choices=[0, 1, 2])
    # ECCC is admitted at two cycles, so a backfill has to say which one it is
    # taking.  Left unset it takes the cycle production reads at that lead, so
    # backfilled history and forward capture stay the same evidence; naming a
    # cycle explicitly is for filling the other one.
    parser.add_argument(
        "--run-cycle-hour",
        type=int,
        default=None,
        choices=list(SUPPORTED_RUN_CYCLE_HOURS),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--allow-current-registry-scope",
        action="store_true",
        help=(
            "When the historical date has no complete 45-station registry, "
            "reuse the latest complete 45-station contract strictly as an "
            "offline train-only download scope."
        ),
    )
    args = parser.parse_args()
    run_cycle_hour = (
        RUN_CYCLE_HOUR_BY_LEAD[args.lead_days]
        if args.run_cycle_hour is None
        else args.run_cycle_hour
    )
    database = args.db.expanduser().resolve(strict=True)
    scope_authority = "HISTORICAL_EVENT_DATE_REGISTRY"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        try:
            scopes = _station_scopes(conn, event_date=args.event_date)
        except RuntimeError as exc:
            # A date whose snapshot is missing entirely is the same situation
            # as one whose snapshot is incomplete, only more so: the historical
            # registry cannot supply a download scope.  The flag's whole
            # purpose is to fall back to the current 45-station contract for
            # exactly that, train-only and labelled as such -- and the market
            # registry only reaches back 25 days, so without this the OOS
            # window can never be filled for this family.
            if (
                str(exc) not in {
                    "eccc_geps_archive_45station_registry_required",
                    "eccc_geps_archive_registry_snapshot_missing",
                }
                or not args.allow_current_registry_scope
            ):
                raise
            scopes = _current_station_scopes(conn)
            scope_authority = (
                "CURRENT_45_STATION_REGISTRY_OFFLINE_TRAIN_ONLY"
            )
    if args.input is not None:
        archive = load_bounded_json(args.input.expanduser().resolve())
        if (
            not isinstance(archive, dict)
            or archive.get("event_date") != args.event_date
            or int(archive.get("station_count") or 0) != 45
        ):
            raise RuntimeError("eccc_geps_archive_input_scope_mismatch")
        output = args.input.expanduser().resolve()
        output_wrote = False
    else:
        if args.output is None:
            raise RuntimeError("eccc_geps_archive_output_required")
        queried_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        archive = capture_geps_station_days(
            event_date=args.event_date,
            lead_days=args.lead_days,
            run_cycle_hour=run_cycle_hour,
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
                ingress = ingest_geps_station_days(conn, archive)
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
        "run_cycle": archive["run_cycle"],
        "union_forecast_hours": archive["union_forecast_hours"],
        "downloaded_file_receipt_count": archive[
            "downloaded_file_receipt_count"
        ],
        "total_response_bytes": archive["total_response_bytes"],
        "output": str(output),
        "output_wrote": output_wrote,
        "ingress": ingress,
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": False,
        "station_scope_authority": scope_authority,
        "historical_arrival_claimed": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
