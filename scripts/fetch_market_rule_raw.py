#!/usr/bin/env python3
"""Capture one new Polymarket Gamma target-rule response as immutable raw bytes.

This collector neither parses nor evaluates outcomes/prices.  It is intended
to be called when a target is first discovered, before any later finality or
label work.  A separate parser reads only rule semantics from the archive.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_rule_raw_archive import fetch_and_archive_market_rule_raw_snapshot


SCHEMA_VERSION = "fetch_market_rule_raw_v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-identity", required=True)
    parser.add_argument("--public-slug", required=True)
    parser.add_argument("--resolution-local-date", required=True)
    parser.add_argument("--source-url", required=True, help="Exact Gamma event payload URL captured for this target.")
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "data" / "shadow" / "market_rule_raw")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    args = parser.parse_args()
    try:
        result = fetch_and_archive_market_rule_raw_snapshot(
            archive_dir=args.archive_dir,
            target_identity=args.target_identity,
            public_slug=args.public_slug,
            resolution_local_date=args.resolution_local_date,
            source_url=args.source_url,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": [f"market_rule_capture_failed:{type(exc).__name__}"],
            "source_url": args.source_url,
            "writes_archive_db": False,
            "places_orders": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
