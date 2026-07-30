#!/usr/bin/env python3
"""Export canonical AMSC/AWC observations into an isolated target audit.

Only this explicit exporter may read the resident ``archive.db``.  It never
opens the target SQLite database, changes the live line, or calls an order
gateway.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_observation_export import (  # noqa: E402
    export_canonical_observation_evidence,
)


DEFAULT_SOURCE = ROOT / "data" / "archive.db"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data" / "bayes_basket_target_market_registry_snapshot.json"
DEFAULT_LANE_MAP = ROOT / "config" / "trading" / "bayes_basket_target_station_lane_map_v1.json"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "bayes_basket_target_canonical_observation_evidence.json"


def _timezone_map(path: Path) -> dict[str, str]:
    payload: Any = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("station_timezone_registry_rows_missing")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        station = str(row.get("station_id") or "").strip()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if station and timezone_name:
            previous = result.setdefault(station, timezone_name)
            if previous != timezone_name:
                raise ValueError(f"station_timezone_conflict:{station}")
    if not result:
        raise ValueError("station_timezone_registry_empty")
    return result


def _lane_map(path: Path) -> dict[str, str]:
    payload: Any = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    stations = payload.get("stations") if isinstance(payload, dict) else None
    if not isinstance(stations, dict) or not stations:
        raise ValueError("station_lane_map_empty")
    return {str(key).strip().upper(): str(value).strip().lower() for key, value in stations.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--timezone-registry", type=Path, default=DEFAULT_TIMEZONE_REGISTRY)
    parser.add_argument("--station-lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--source-lane", action="append", default=[])
    parser.add_argument("--as-of-utc", default="")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        result = export_canonical_observation_evidence(
            source_db=args.source_db,
            output_path=args.output,
            station_ids=args.station_id,
            source_lanes=args.source_lane,
            station_timezones=_timezone_map(args.timezone_registry),
            station_lane_map=_lane_map(args.station_lane_map),
            as_of_utc=args.as_of_utc,
        )
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_canonical_observation_evidence_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "blockers": [f"{type(exc).__name__}:{exc}"],
            "boundary": {
                "reads_shared_live_database": False,
                "writes_shared_live_database": False,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
            },
        }
    print(json.dumps({
        "schema": result.get("schema"),
        "state": result.get("state"),
        "output": result.get("output"),
        "observation_count": result.get("observation_count", 0),
        "accepted_truth_count": result.get("accepted_truth_count", 0),
        "quarantined_truth_count": result.get("quarantined_truth_count", 0),
        "source_lane_counts": result.get("source_lane_counts", {}),
        "blocker_count": len(result.get("blockers") or []),
        "receipt_sha256": result.get("receipt_sha256"),
        "boundary": result.get("boundary"),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") == "EXPORTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
