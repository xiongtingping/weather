#!/usr/bin/env python3
"""Multi-year GEFS/ECMWF archive capture into the band-replay evidence DB.

Research-only.  Reuses the production capture functions (one global
download per day serving every station) but ingests member rows into the
standalone replay evidence database, never the production SSOT.  Raw GRIB
messages go to a scratch store that is wiped after each day: with one
run-cycle per event date there is no cross-day reuse, and a multi-year
walk would otherwise accumulate the full transfer volume on disk.

Resumable: each (family, event_date, lead, cycle) writes a ledger row and
completed days are skipped on the next invocation.  Per-day failures are
recorded and do not stop the walk -- the public archives have real holes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.market_registry import (  # noqa: E402
    load_active_registry_rows,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)
from scripts.multiyear_band_replay_common import (  # noqa: E402
    DEFAULT_REPLAY_DB,
    FAMILY_ARCHIVE_START,
    FAMILY_SYSTEM_IDS,
    REPLAY_FAMILIES,
    open_production_db_readonly,
    open_replay_db,
    utc_now_iso,
)

TRANSPORT_PROVIDER_IDS = {
    "gefs": "NOAA_GEFS_PDS_S3",
    "ecmwf": "ECMWF_OPEN_DATA_S3",
}
RAW_SCRATCH_ENV = {
    "gefs": "GEFS_RAW_OBJECT_DIR",
    "ecmwf": "ECMWF_PF50_RAW_OBJECT_DIR",
}


def _station_scopes(production: sqlite3.Connection) -> list[dict[str, Any]]:
    from collections import defaultdict

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in load_active_registry_rows(production):
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
            raise RuntimeError("replay_capture_registry_timezone_invalid")
        coordinates = station_coordinates(station)
        scopes.append({
            "station_id": station,
            "settlement_timezone": next(iter(timezones)),
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
            "registry_rows": rows,
        })
    if not scopes:
        raise RuntimeError("replay_capture_registry_empty")
    return scopes


def _capture_module(family: str):
    if family == "gefs":
        from bayes_basket_target import noaa_gefs_archive as module
    else:
        from bayes_basket_target import ecmwf_ifs_archive as module
    return module


def _ingest_day(
    replay: sqlite3.Connection,
    *,
    family: str,
    archive: Mapping[str, Any],
    extraction_version: str,
) -> int:
    rows = 0
    captures = archive.get("captures")
    if not isinstance(captures, list) or not captures:
        raise RuntimeError("replay_capture_day_captures_missing")
    for capture in captures:
        members = capture.get("members")
        if not isinstance(members, list) or not members:
            raise RuntimeError("replay_capture_day_members_missing")
        for member in members:
            replay.execute(
                "INSERT INTO replay_member_run(ensemble_system_id,"
                " station_id, target_local_date, archive_lead_bucket,"
                " run_cycle, native_run_init_utc, member_id, member_role,"
                " raw_tmax_c, extraction_version, transport_provider_id,"
                " member_set_sha256, archive_query_at_utc)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT DO NOTHING",
                (
                    str(capture["ensemble_system_id"]),
                    str(capture["station_id"]),
                    str(capture["event_date"]),
                    str(capture["archive_lead_bucket"]),
                    str(capture["run_cycle"]),
                    str(capture["run_init_utc"]),
                    str(member["member_id"]),
                    str(member["member_role"]),
                    float(member["raw_tmax_c"]),
                    extraction_version,
                    TRANSPORT_PROVIDER_IDS[family],
                    str(capture["member_set_sha256"]),
                    str(capture["archive_query_at_utc"]),
                ),
            )
            rows += 1
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=REPLAY_FAMILIES, required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--lead-days", type=int, default=1, choices=[0, 1, 2])
    parser.add_argument("--run-cycle-hour", type=int)
    parser.add_argument("--max-days", type=int, default=100000)
    parser.add_argument("--replay-db", type=Path, default=DEFAULT_REPLAY_DB)
    parser.add_argument(
        "--production-db", type=Path, default=DEFAULT_TARGET_DB
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-attempt days whose ledger row is FAILED.",
    )
    args = parser.parse_args()

    family = args.family
    start = max(
        date.fromisoformat(args.start_date),
        date.fromisoformat(FAMILY_ARCHIVE_START[family]),
    )
    end = date.fromisoformat(args.end_date)
    if end < start:
        raise RuntimeError("replay_capture_date_range_empty")

    module = _capture_module(family)
    capture_days = (
        module.capture_gefs_station_days
        if family == "gefs"
        else module.capture_ecmwf_station_days
    )
    extraction_version = module.extraction_version_for_lead(
        args.lead_days, args.run_cycle_hour
    )
    cycle_hour = module.resolve_run_cycle_hour(
        args.lead_days, args.run_cycle_hour
    )

    replay = open_replay_db(args.replay_db)
    with open_production_db_readonly(args.production_db) as production:
        scopes = _station_scopes(production)

    done = {
        str(row[0])
        for row in replay.execute(
            "SELECT event_date FROM replay_capture_ledger"
            " WHERE family=? AND lead_days=? AND run_cycle_hour=?"
            + ("" if args.retry_failed else " AND state IN ('DONE','FAILED')")
            + (" AND state='DONE'" if args.retry_failed else ""),
            (family, args.lead_days, cycle_hour),
        )
    }

    scratch = Path(tempfile.mkdtemp(prefix=f"replay-{family}-raw-"))
    os.environ[RAW_SCRATCH_ENV[family]] = str(scratch)
    processed = []
    try:
        day = start
        while day <= end and len(processed) < args.max_days:
            event_date = day.isoformat()
            day += timedelta(days=1)
            if event_date in done:
                continue
            queried_at = (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            try:
                archive = capture_days(
                    event_date=event_date,
                    station_scopes=scopes,
                    archive_query_at_utc=queried_at,
                    lead_days=args.lead_days,
                    run_cycle_hour=args.run_cycle_hour,
                )
                row_count = _ingest_day(
                    replay,
                    family=family,
                    archive=archive,
                    extraction_version=extraction_version,
                )
                replay.execute(
                    "INSERT OR REPLACE INTO replay_capture_ledger(family,"
                    " event_date, lead_days, run_cycle_hour, state,"
                    " station_count, member_row_count, error,"
                    " completed_at_utc) VALUES(?,?,?,?,?,?,?,?,?)",
                    (family, event_date, args.lead_days, cycle_hour,
                     "DONE", len(archive.get("captures") or []),
                     row_count, "", utc_now_iso()),
                )
                replay.commit()
                processed.append({"event_date": event_date, "state": "DONE",
                                  "member_rows": row_count})
            except Exception as exc:  # noqa: BLE001 -- archive holes are data
                replay.rollback()
                replay.execute(
                    "INSERT OR REPLACE INTO replay_capture_ledger(family,"
                    " event_date, lead_days, run_cycle_hour, state,"
                    " station_count, member_row_count, error,"
                    " completed_at_utc) VALUES(?,?,?,?,?,?,?,?,?)",
                    (family, event_date, args.lead_days, cycle_hour,
                     "FAILED", 0, 0,
                     f"{type(exc).__name__}:{exc}"[:500], utc_now_iso()),
                )
                replay.commit()
                processed.append({"event_date": event_date, "state": "FAILED",
                                  "error": f"{type(exc).__name__}:{exc}"[:200]})
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
                scratch.mkdir(parents=True, exist_ok=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    summary = {
        "family": family,
        "lead_days": args.lead_days,
        "run_cycle_hour": cycle_hour,
        "extraction_version": extraction_version,
        "requested_range": [start.isoformat(), end.isoformat()],
        "processed_days": len(processed),
        "done_days": sum(1 for p in processed if p["state"] == "DONE"),
        "failed_days": sum(1 for p in processed if p["state"] == "FAILED"),
        "tail": processed[-5:],
        "guards": {
            "writes_archive_db": False,
            "writes_production_ssot": False,
            "places_orders": False,
        },
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
