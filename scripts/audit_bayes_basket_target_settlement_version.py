#!/usr/bin/env python3
"""Write a bounded read-only Settlement-Version Evidence Packet v1."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.settlement_version_scorecard import (  # noqa: E402
    MAX_FACTS,
    build_settlement_version_evidence_packet,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--window-start-date")
    parser.add_argument("--window-end-date")
    parser.add_argument(
        "--latest-labeled-event-date",
        action="store_true",
        help=(
            "use the latest event_date with a label visible at --as-of-utc; "
            "does not wait for a future settlement"
        ),
    )
    parser.add_argument("--max-facts", type=int, default=MAX_FACTS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    database = resolve_target_database(args.db)
    with sqlite3.connect(
        f"file:{database}?mode=ro",
        uri=True,
        timeout=30,
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SystemExit("target_database_quick_check_failed")
        if args.latest_labeled_event_date:
            if args.window_start_date or args.window_end_date:
                raise SystemExit(
                    "latest_labeled_event_date_conflicts_with_window"
                )
            stations = tuple(sorted({
                str(value).strip().upper()
                for value in args.station_id
                if str(value).strip()
            }))
            placeholders = ",".join("?" for _ in stations)
            latest = conn.execute(
                "SELECT MAX(f.event_date) "
                "FROM basket_policy_training_fact f "
                "JOIN basket_policy_training_label l "
                "ON l.fact_id=f.fact_id "
                f"WHERE f.station_id IN ({placeholders}) "
                "AND julianday(l.labeled_at_utc)<=julianday(?) "
                "AND julianday(l.settlement_closed_at_utc)<=julianday(?)",
                (*stations, args.as_of_utc, args.as_of_utc),
            ).fetchone()[0]
            if latest in (None, ""):
                raise SystemExit(
                    "latest_labeled_event_date_missing"
                )
            window_start_date = str(latest)
            window_end_date = str(latest)
        else:
            if not args.window_start_date or not args.window_end_date:
                raise SystemExit(
                    "explicit_window_or_latest_labeled_event_date_required"
                )
            window_start_date = args.window_start_date
            window_end_date = args.window_end_date
        result = build_settlement_version_evidence_packet(
            conn,
            as_of_utc=args.as_of_utc,
            station_ids=args.station_id,
            window_start_date=window_start_date,
            window_end_date=window_end_date,
            max_facts=args.max_facts,
        )
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "state": result["state"],
        "content_sha256": result["content_sha256"],
        "production_no_live_blocked_by_forward_oos": result[
            "production_no_live_blocked_by_forward_oos"
        ],
        "output": str(args.output.expanduser().resolve()),
    }, sort_keys=True))
    return 2 if result["state"] == "BLOCKED_FAIL_CLOSED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
