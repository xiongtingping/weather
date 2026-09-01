#!/usr/bin/env python3
"""Ingest bounded official historical AWC TAF calibration artifacts."""

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
    ingest_historical_taf_calibration_with_quarantine,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--archive",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    if len(args.archive) > expected_station_count():
        raise RuntimeError("historical_taf_archive_count_exceeded")
    archives = [
        load_bounded_json(path.expanduser().resolve(strict=True))
        for path in args.archive
    ]
    if not args.commit:
        print(json.dumps({
            "state": "VALIDATED_NOT_COMMITTED",
            "archive_count": len(archives),
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    database = args.db.expanduser().resolve(strict=True)
    with sqlite3.connect(database, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        assert_connection_target_capacity(
            conn,
            recover_oversized_wal=True,
        )
        # One transaction per archive, not one across the batch.
        #
        # This runs as a subprocess of the resident, spawned from the
        # historical-TAF capture thread while the cycle thread is mid-flight
        # on the same store.  Held across forty archives the write lock is
        # held for minutes, and the cycle's market ingress -- which does not
        # go through the retrying writer-lock helper -- fail-closes on
        # ``database is locked``.  Measured 2026-09-01: 171 blocked decisions
        # carrying that error over three days, two consecutive cycles at
        # 05:44 and 05:58 producing no scope at all, with this process
        # holding the lock through a forty-file ZUUU backfill.
        #
        # An archive is a frozen-fact replay and re-ingest verifies rather
        # than duplicates, so the archive is the natural atomic unit; batching
        # them bought nothing but lock hold time.  A failure now leaves the
        # archives already committed in place, which the next pass re-ingests
        # idempotently, and still exits nonzero so the caller records it.
        receipts = []
        for artifact in archives:
            conn.execute("BEGIN IMMEDIATE")
            try:
                receipts.append(
                    ingest_historical_taf_calibration_with_quarantine(
                        conn, artifact
                    )
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    print(json.dumps({
        "state": "COMMITTED",
        "archive_count": len(archives),
        "capture_count": sum(
            int(row["capture_count"]) for row in receipts
        ),
        "wrote_count": sum(
            int(row["wrote_count"]) for row in receipts
        ),
        "replay_count": sum(
            int(row["replay_count"]) for row in receipts
        ),
        "skipped_after_anchor_count": sum(
            int(row["skipped_after_anchor_count"])
            for row in receipts
        ),
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
