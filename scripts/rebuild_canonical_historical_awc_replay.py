#!/usr/bin/env python3
"""Re-envelope retained official AWC raw as canonical historical replay evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.historical_awc_replay import (  # noqa: E402
    build_historical_awc_replay_evidence,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation-artifact", action="append", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--settlement-timezone", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packets = [
        load_bounded_json(path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES)
        for path in args.observation_artifact
    ]
    result = build_historical_awc_replay_evidence(
        packets=packets,
        raw_root=args.raw_root,
        station_id=args.station_id,
        settlement_timezone=args.settlement_timezone,
        start_date=args.start_date,
        end_date=args.end_date,
        as_of_utc=args.as_of_utc,
    )
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "schema": result["schema"],
        "station_id": result["station_id"],
        "observation_count": len(result["observations"]),
        "observed_date_count": len(result["observed_dates"]),
        "truth_ready_date_count": len(result["truth_ready_dates"]),
        "truth_blocked_date_count": len(result["truth_blocked_dates"]),
        "evidence_sha256": result["evidence_sha256"],
        "writes_production_db": False,
        "calls_order_gateway": False,
        "places_orders": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
