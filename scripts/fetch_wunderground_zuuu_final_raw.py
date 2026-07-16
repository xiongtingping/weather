#!/usr/bin/env python3
"""Explicit raw-only capture of the accepted Wunderground ZUUU daily page."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_authority_contract_runtime import load_resolution_authority_contract
from wunderground_final_raw_archive import fetch_and_archive_wunderground_daily_raw_capture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "data" / "wunderground_daily_history_raw")
    parser.add_argument("--contract", type=Path, default=ROOT / "config" / "resolution_authorities" / "zuuu_amsc_wunderground_daily_high_v4.json")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    args = parser.parse_args()
    contract, audit = load_resolution_authority_contract(args.contract)
    if audit.get("ok") is not True:
        print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    result = fetch_and_archive_wunderground_daily_raw_capture(
        archive_dir=args.archive_dir,
        event_date=args.event_date,
        authority_contract=contract,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
