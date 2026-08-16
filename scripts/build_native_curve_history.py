#!/usr/bin/env python3
"""Rebuild the d1 family temperature curves the trajectory fit trains on.

``native_curve_history`` held 2026-07-16..2026-08-01 and nothing since, because
it had a consumer -- ``_historical_fold_binding`` in the trajectory packet
builder -- and no producer at all.  Every settled day after 2026-08-01 whose
station had no committed P0 root therefore lost its fold: the binding needs two
independent families' curves for the day and found no file to read them from.
Measured 2026-08-17: 505 of 1023 folds carried no conditional path state, 463
of those because the day had no root, and the days concentrated in exactly the
window this file covers.

Nothing here is new evidence.  It is the same capture the forward lane runs,
pointed at a past event date: the d1 run genuinely published the morning
before the day, and both families keep their GRIB long enough to be read again
-- NOAA GEFS on the AWS public dataset, ECMWF on open-data, both with byte
range indices, so a rebuild costs range requests rather than whole fields.
The curve is training-only evidence; it never becomes a P0 and never enters
``basket_probability_envelope``.  ``available_at_utc`` on the sealed curve is
the query time, and the consumer overrides it with the run's real publication
morning -- a retained transport timestamp is transport, not availability.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.ecmwf_ifs_archive import (  # noqa: E402
    capture_ecmwf_station_days,
)
from bayes_basket_target.noaa_gefs_archive import (  # noqa: E402
    capture_gefs_station_days,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)

DEFAULT_TARGET_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT_DIR = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "native_curve_history"
)
SCHEMA = "native_family_curve_history_day_v1"
LEAD_DAYS = 1
LEAD_BUCKET = "d1"
# The consumer intersects the families' valid times and needs three common
# points; a day that yields one family is not usable either way.
REQUIRED_FAMILY_COUNT = 2
MAX_DAYS_PER_RUN = 40


def _station_scopes(
    conn: sqlite3.Connection,
    *,
    event_date: str,
) -> list[dict[str, Any]]:
    """Registry scopes for one past day, from the persisted registry.

    The forward lane reads the live registry snapshot, which only carries days
    that are still ahead.  ``basket_target_market_registry`` keeps the same
    rows per event date, so a past day resolves from what was actually listed
    then rather than from what is listed now.
    """

    # The registry is append-only per refresh, so one station-day holds many
    # snapshots -- KLAX on 2026-08-12 has 242 rows across 22 snapshots for 11
    # real outcomes.  Reading them all at once makes the outcome set look
    # duplicated and the whole day unusable.  Take each station's own newest
    # snapshot: different stations are listed by different refreshes, so a
    # single global latest snapshot would drop most of the day.
    rows = conn.execute(
        "SELECT r.station_id,r.event_date,r.settlement_timezone,"
        "r.settlement_unit,r.outcome,r.registry_json,r.market_active,"
        "r.source_eligible FROM basket_target_market_registry r "
        "JOIN (SELECT station_id,snapshot_sha256,"
        "      MAX(refreshed_at) AS refreshed_at "
        "        FROM basket_target_market_registry "
        "       WHERE event_date=? AND market_active=1 AND source_eligible=1 "
        "       GROUP BY station_id) latest "
        "  ON latest.station_id=r.station_id "
        " AND latest.snapshot_sha256=r.snapshot_sha256 "
        "WHERE r.event_date=? AND r.market_active=1 AND r.source_eligible=1",
        (event_date, event_date),
    ).fetchall()
    by_station: dict[str, list[dict[str, Any]]] = {}
    for (
        station_id,
        row_date,
        timezone_name,
        unit,
        outcome,
        registry_json,
        active,
        eligible,
    ) in rows:
        station = str(station_id or "").strip().upper()
        name = str(timezone_name or "").strip()
        label = str(outcome or "").strip()
        # ``bucket_intervals_from_registry_rows`` reads the bucket edges out of
        # ``registry_json`` and keys the scope on station/date/unit, so a row
        # missing any of them cannot form an interval and is not a scope.
        if not station or not name or not label or not registry_json:
            continue
        by_station.setdefault(station, []).append({
            "station_id": station,
            "event_date": str(row_date or ""),
            "settlement_timezone": name,
            "settlement_unit": str(unit or "").strip().upper(),
            "outcome": label,
            "registry_json": str(registry_json),
            "market_active": int(active or 0),
            "source_eligible": int(eligible or 0),
        })
    scopes: list[dict[str, Any]] = []
    for station in sorted(by_station):
        matching = by_station[station]
        timezones = {row["settlement_timezone"] for row in matching}
        outcomes = [row["outcome"] for row in matching]
        if len(timezones) != 1 or len(outcomes) != len(set(outcomes)):
            # A station the registry describes incoherently fails closed for
            # that day rather than taking the whole date down: the other
            # stations' curves are still worth writing.
            continue
        coordinates = station_coordinates(station)
        scopes.append({
            "station_id": station,
            "settlement_timezone": next(iter(timezones)),
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
            "registry_rows": matching,
        })
    return scopes


def _curves_by_station(capture: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in capture.get("captures") or ():
        if not isinstance(entry, Mapping):
            continue
        curve = entry.get("family_temperature_curve")
        station = str(entry.get("station_id") or "").strip().upper()
        if station and isinstance(curve, Mapping):
            result[station] = dict(curve)
    return result


def build_curve_history_day(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    archive_query_at_utc: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    scopes = _station_scopes(conn, event_date=event_date)
    if not scopes:
        raise ContractError(
            f"native_curve_history_registry_scope_empty:{event_date}"
        )
    curves_by_system: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for system_id, capture in (
        ("NOAA_GEFS_05", capture_gefs_station_days),
        ("ECMWF_IFS_ENS_OPEN_PF50", capture_ecmwf_station_days),
    ):
        try:
            result = capture(
                event_date=event_date,
                station_scopes=scopes,
                archive_query_at_utc=archive_query_at_utc,
                timeout_seconds=timeout_seconds,
                lead_days=LEAD_DAYS,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            failures[system_id] = f"{type(exc).__name__}:{exc}"[:200]
            continue
        curves = _curves_by_station(result)
        if curves:
            curves_by_system[system_id] = curves
    if len(curves_by_system) < REQUIRED_FAMILY_COUNT:
        raise ContractError(
            "native_curve_history_insufficient_families:"
            + event_date
            + ":"
            + json.dumps(failures, sort_keys=True)
        )
    body = {
        "schema": SCHEMA,
        "event_date": event_date,
        "archive_lead_bucket": LEAD_BUCKET,
        "curves_by_system": curves_by_system,
    }
    return {**body, "content_sha256": content_sha256(body)}


def _dates(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first:
        raise ContractError("native_curve_history_date_range_invalid")
    span = (last - first).days + 1
    if span > MAX_DAYS_PER_RUN:
        raise ContractError("native_curve_history_date_range_too_wide")
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range(span)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--target-db", default=str(DEFAULT_TARGET_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Leave a day alone if its file is already on disk.",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    query_at = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    written: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}
    conn = sqlite3.connect(
        f"file:{Path(args.target_db).resolve()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    try:
        for event_date in _dates(args.start_date, args.end_date):
            path = output_dir / f"{event_date}_{LEAD_BUCKET}.json"
            if args.skip_existing and path.is_file():
                skipped.append(event_date)
                continue
            try:
                day = build_curve_history_day(
                    conn,
                    event_date=event_date,
                    archive_query_at_utc=query_at,
                    timeout_seconds=float(args.timeout_seconds),
                )
            except Exception as exc:  # noqa: BLE001 - reported per day
                failed[event_date] = f"{type(exc).__name__}:{exc}"[:240]
                continue
            write_bounded_json_if_changed(path, day)
            written.append(event_date)
    finally:
        conn.close()
    print(json.dumps({
        "schema": "native_curve_history_build_receipt_v1",
        "archive_query_at_utc": query_at,
        "written": written,
        "skipped": skipped,
        "failed": failed,
    }, indent=2, sort_keys=True))
    return 0 if written or skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
