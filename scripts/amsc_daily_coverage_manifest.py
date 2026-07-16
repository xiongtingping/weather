#!/usr/bin/env python3
"""Build a read-only AMSC raw-METAR coverage manifest for one local day."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_raw_history_archive import build_amsc_daily_coverage_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True, help="station-local YYYY-MM-DD")
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument(
        "--archive-dir",
        default=str(ROOT / "data" / "amsc_raw_history"),
        help="raw archive root (default: %(default)s)",
    )
    parser.add_argument(
        "--late-arrival-tolerance-seconds",
        type=int,
        default=5 * 60,
        help="maximum valid-to-first-seen lag for A-class replay eligibility",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = build_amsc_daily_coverage_manifest(
        args.archive_dir,
        event_date=args.event_date,
        station_id=args.station_id,
        late_arrival_tolerance_seconds=args.late_arrival_tolerance_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not result["issues"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
