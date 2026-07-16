#!/usr/bin/env python3
"""Preview or explicitly materialize AMSC coverage_v2 from immutable raw objects."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_raw_history_archive import (
    build_amsc_daily_coverage_manifest,
    persist_amsc_daily_coverage_snapshot,
)


CONFIRMATION_PHRASE = "MATERIALIZE_AMSC_DAILY_COVERAGE_V2"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True, help="ZUUU local YYYY-MM-DD")
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=ROOT / "data" / "amsc_raw_history",
        help="immutable raw archive root (default: %(default)s)",
    )
    parser.add_argument("--late-arrival-tolerance-seconds", type=int, default=5 * 60)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.apply:
        coverage = build_amsc_daily_coverage_manifest(
            args.archive_dir,
            event_date=args.event_date,
            station_id=args.station_id,
            late_arrival_tolerance_seconds=args.late_arrival_tolerance_seconds,
        )
        print(json.dumps({
            "ok": not coverage.get("issues") and int(coverage.get("event_raw_object_count") or 0) > 0,
            "state": "preview_only",
            "apply_confirmation_required": CONFIRMATION_PHRASE,
            "coverage": coverage,
            "boundary": {
                "writes_coverage_v2": False,
                "reads_legacy_coverage": False,
                "writes_archive_db": False,
                "writes_posterior": False,
                "places_orders": False,
            },
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not coverage.get("issues") and int(coverage.get("event_raw_object_count") or 0) > 0 else 1
    if args.confirm != CONFIRMATION_PHRASE:
        print(json.dumps({
            "ok": False,
            "state": "blocked",
            "block_reasons": ["coverage_v2_materialization_requires_explicit_confirmation"],
            "confirmation_phrase_required": CONFIRMATION_PHRASE,
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    try:
        result = persist_amsc_daily_coverage_snapshot(
            args.archive_dir,
            event_date=args.event_date,
            station_id=args.station_id,
            late_arrival_tolerance_seconds=args.late_arrival_tolerance_seconds,
        )
    except ValueError as exc:
        print(json.dumps({
            "ok": False,
            "state": "rejected",
            "block_reasons": [str(exc)],
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
