#!/usr/bin/env python3
"""CLI adapter for the package-owned Bayes basket P0 producer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from bayes_basket_target.database import DEFAULT_TARGET_DB
from bayes_basket_target.p0_producer import (
    _curve_rows,
    _curve_rows_from_receipts,
    _select_latest_compatible_bundle,
    bucket_intervals_from_registry_rows,
    build_packets,
)

__all__ = [
    "_curve_rows",
    "_curve_rows_from_receipts",
    "_select_latest_compatible_bundle",
    "bucket_intervals_from_registry_rows",
    "build_packets",
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--native-decode-evidence",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--market-registry-snapshot", type=Path, required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument("--fresh-until-utc", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--inbox", type=Path)
    parser.add_argument("--no-packet-files", action="store_true")
    args = parser.parse_args(argv)
    result = build_packets(
        db_path=args.db,
        decode_evidence_path=list(args.native_decode_evidence),
        registry_snapshot_path=args.market_registry_snapshot,
        event_date=args.event_date,
        decision_at_utc=args.decision_at_utc,
        fresh_until_utc=args.fresh_until_utc,
        output_dir=args.output_dir,
        inbox_path=args.inbox,
        emit_packet_files=not args.no_packet_files,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not result["blockers"] and result["packet_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
