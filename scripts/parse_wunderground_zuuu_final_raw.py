#!/usr/bin/env python3
"""Read-only parse of one immutable Wunderground raw manifest into an S candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wunderground_daily_settlement_parser import parse_wunderground_daily_high
from wunderground_final_raw_archive import load_wunderground_daily_raw_capture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "data" / "wunderground_daily_history_raw")
    parser.add_argument("--manifest-ref", required=True)
    args = parser.parse_args()
    capture = load_wunderground_daily_raw_capture(archive_dir=args.archive_dir, manifest_ref=args.manifest_ref)
    result = parse_wunderground_daily_high(capture["raw_payload"], event_date=capture["event_date"])
    print(json.dumps({"capture": {key: value for key, value in capture.items() if key != "raw_payload"}, "parse": result}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
