#!/usr/bin/env python3
"""Append historical AWC TAF AMD/COR station-day quarantine facts.

This consumes only retained, formal AWC calibration archives.  It never
fetches, rewrites TAF facts, or promotes an artifact; AMD/COR simply prevents
the entire station day from entering a strict P0-M exact join.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.native_ensemble_training_facts import (  # noqa: E402
    ingest_historical_taf_correction_quarantine,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
    write_bounded_json,
)


MAX_ARCHIVES = expected_station_count()
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def _path(value: Path, *, must_exist: bool = True) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise RuntimeError("historical_taf_correction_quarantine_symlink_forbidden")
    return path.resolve(strict=must_exist)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--archive", type=Path, action="append", required=True)
    parser.add_argument(
        "--event-date",
        action="append",
        default=[],
        help=(
            "Optional local-date filter for a bounded repair; correction "
            "raw bytes remain bound to the supplied official archive."
        ),
    )
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)

    if len(args.archive) > MAX_ARCHIVES:
        raise RuntimeError("historical_taf_correction_quarantine_archive_cap_exceeded")
    archives = [load_bounded_json(_path(path)) for path in args.archive]
    if not args.commit:
        result = {
            "schema": "historical_taf_correction_quarantine_run_v1",
            "state": "VALIDATED_NOT_COMMITTED",
            "archive_count": len(archives),
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
    else:
        database = _path(args.db)
        with sqlite3.connect(database, timeout=60.0) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            assert_connection_target_capacity(conn, recover_oversized_wal=True)
            conn.execute("BEGIN IMMEDIATE")
            try:
                receipts = [
                    ingest_historical_taf_correction_quarantine(
                        conn,
                        archive,
                        event_dates=set(args.event_date) or None,
                    )
                    for archive in archives
                ]
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        result = {
            "schema": "historical_taf_correction_quarantine_run_v1",
            "state": "COMMITTED",
            "archive_count": len(archives),
            "correction_count": sum(int(item["correction_count"]) for item in receipts),
            "wrote_count": sum(int(item["wrote_count"]) for item in receipts),
            "replay_count": sum(int(item["replay_count"]) for item in receipts),
            "receipts": receipts,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
    if args.receipt is not None:
        write_bounded_json(_path(args.receipt, must_exist=False), result, limit=MAX_OUTPUT_BYTES)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
