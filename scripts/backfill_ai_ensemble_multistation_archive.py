#!/usr/bin/env python3
"""Capture one historical AIFS ENS or AIGEFS run for named stations, and ingest it.

The two AI ensembles' history lane, in the interface the native-history
driver (``scripts/run_native_history_backfill.py``) already drives for GEFS
and ECMWF IFS: one run per (event date, lead, cycle), written as a packet with
``--output`` and ingested from one with ``--input --commit``.  The capture and
ingest functions are the ones the production forward lane calls
(``capture_official_native_ensemble_forward.py``); only the run is older.

Both systems keep their published history on official storage -- AIFS ENS on
ECMWF's open-data replica (set ``ECMWF_ARCHIVE_TRANSPORT_BASE_URL`` to the GCS
replica, as the IFS lane does), AIGEFS on NOAA's NODD bucket -- so a station
that joined the fleet late (LLBG, LTFM and UUWW on 2026-10-04) can be walked
back over the days its truth already covers.  Rows are training-only, exactly
as the forward lane writes them; no raw bodies are kept.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import ecmwf_aifs_ens_archive, noaa_aigefs_archive  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
    write_bounded_json_if_changed,
)
# The station scope is the GEFS lane's: active registry rows, the station's
# own settlement zone, and a refusal for a named station the decoder cannot
# place.  The archive schema differs; the scope does not.
from scripts.backfill_noaa_gefs_multistation_archive import (  # noqa: E402
    _input_station_scopes,
    _station_scopes,
)

SYSTEMS = {
    "aifs": (
        ecmwf_aifs_ens_archive.capture_aifs_station_days,
        ecmwf_aifs_ens_archive.ingest_aifs_station_days,
    ),
    "aigefs": (
        noaa_aigefs_archive.capture_aigefs_station_days,
        noaa_aigefs_archive.ingest_aigefs_station_days,
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True, choices=sorted(SYSTEMS))
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--lead-days", type=int, default=2, choices=[0, 1, 2])
    parser.add_argument(
        "--run-cycle-hour", type=int, default=None, choices=[0, 6, 12, 18]
    )
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    capture, ingest = SYSTEMS[args.system]
    database = args.db.expanduser().resolve(strict=True)
    selected = {
        str(station).strip().upper()
        for station in args.station_id
        if str(station).strip()
    }
    if args.input is not None:
        archive = load_bounded_json(args.input.expanduser().resolve())
        if not isinstance(archive, dict) or archive.get("event_date") != args.event_date:
            raise RuntimeError("ai_ensemble_multistation_input_scope_mismatch")
        _input_station_scopes(archive, selected_stations=selected)
        output = args.input.expanduser().resolve()
        output_wrote = False
    else:
        if args.output is None:
            raise RuntimeError("ai_ensemble_multistation_output_required")
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            scopes, _without = _station_scopes(conn, selected_stations=selected)
        queried_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        archive = capture(
            event_date=args.event_date,
            station_scopes=scopes,
            archive_query_at_utc=queried_at,
            lead_days=args.lead_days,
            run_cycle_hour=args.run_cycle_hour,
        )
        output = args.output.expanduser().resolve()
        output_wrote = write_bounded_json_if_changed(output, archive)
    ingress = None
    if args.commit:
        lock_wait = float(os.environ.get("NATIVE_HISTORY_LOCK_WAIT_SECONDS", "300"))
        with sqlite3.connect(database, timeout=lock_wait) as conn:
            conn.execute(f"PRAGMA busy_timeout={int(lock_wait * 1000)}")
            assert_connection_target_capacity(conn, recover_oversized_wal=True)
            conn.execute("BEGIN IMMEDIATE")
            try:
                ingress = ingest(conn, archive)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    print(json.dumps({
        "system": args.system,
        "event_date": args.event_date,
        "run_init_utc": archive.get("run_init_utc"),
        "station_count": archive["station_count"],
        "station_ids": archive["station_ids"],
        "downloaded_field_receipt_count": archive["downloaded_field_receipt_count"],
        "total_response_bytes": archive["total_response_bytes"],
        "output": str(output),
        "output_wrote": output_wrote,
        "ingress": None if ingress is None else {
            "state": ingress["state"],
            "station_count": ingress["station_count"],
            "member_run_wrote_count": ingress["member_run_wrote_count"],
            "member_bucket_wrote_count": ingress["member_bucket_wrote_count"],
        },
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
