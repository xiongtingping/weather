#!/usr/bin/env python3
"""Replay one durable target station-day from SSOT without writes or orders."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.target_ssot_replay import (  # noqa: E402
    replay_durable_target_chain,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--as-of-utc")
    parser.add_argument("--through-rowid", type=int)
    parser.add_argument("--max-facts", type=int, default=512)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    database = args.db.expanduser().resolve()
    if not database.is_file() or database.is_symlink():
        raise SystemExit("durable replay database invalid")
    with sqlite3.connect(
        f"file:{database}?mode=ro",
        uri=True,
        timeout=30,
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        result = replay_durable_target_chain(
            conn,
            station_id=args.station_id,
            event_date=args.event_date,
            as_of_utc=args.as_of_utc,
            through_rowid=args.through_rowid,
            max_facts=args.max_facts,
        )
    if args.output is not None:
        write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "state": result["state"],
        "station_id": result["station_id"],
        "event_date": result["event_date"],
        "replayed_fact_count": result["replayed_fact_count"],
        "ws_economic_summary": result["ws_economic_summary"],
        "label_required": result["label_status"][
            "required_for_production_no_live_replay"
        ],
        "future_data_counts_as_oos": result["historical_oos"][
            "future_data_counts_as_oos"
        ],
        "historical_oos_blocks_production_no_live": result[
            "historical_oos"
        ]["blocks_production_no_live"],
        "content_sha256": result["content_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
