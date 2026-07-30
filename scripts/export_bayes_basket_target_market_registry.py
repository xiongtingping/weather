#!/usr/bin/env python3
"""Export the resident market registry as an isolated target ingress object.

The source database is opened read-only.  This command does not initialize
the target DB, touch launchd, or contact a market gateway.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.market_registry import export_shared_live_registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "bayes_basket_target_market_registry_snapshot.json",
    )
    parser.add_argument("--station-id", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--as-of-utc", default="", help="ET boundary clock used to exclude not-started event days")
    parser.add_argument(
        "--include-not-started-for-p0",
        action="store_true",
        help="Retain a not-started event date only for forward P0 bucket bridging; never target-registry ingress.",
    )
    parser.add_argument(
        "--current-catalog-only",
        action="store_true",
        help="Export only each station's open scope plus earliest not-started scope; omit already closed history.",
    )
    args = parser.parse_args()
    try:
        result = export_shared_live_registry(
            source_db=args.source_db,
            output_path=args.output,
            station_id=str(args.station_id or "").strip(),
            event_date=str(args.event_date or "").strip(),
            as_of_utc=str(args.as_of_utc or "").strip() or None,
            include_not_started_for_p0=bool(args.include_not_started_for_p0),
            current_catalog_only=bool(args.current_catalog_only),
        )
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_market_registry_snapshot_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "places_orders": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") == "EXPORTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
