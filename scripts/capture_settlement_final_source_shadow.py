#!/usr/bin/env python3
"""Run one shadow-only Wunderground final-S capture cycle after finality proof."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_authority_contract_runtime import load_resolution_authority_contract
from settlement_final_source_shadow_capture_runtime import run_settlement_final_source_shadow_capture_cycle
from wunderground_final_raw_archive import fetch_and_archive_wunderground_daily_raw_capture


CONFIRMATION_PHRASE = "CAPTURE_FORWARD_SETTLEMENT_SOURCE"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-db", type=Path, default=ROOT / "data" / "shadow" / "resolution_final_labels.db")
    parser.add_argument("--wunderground-raw-archive-dir", type=Path, default=ROOT / "data" / "shadow" / "wunderground_daily_history_raw")
    parser.add_argument("--authority-contract", type=Path, default=ROOT / "config" / "resolution_authorities" / "zuuu_amsc_wunderground_daily_high_v4.json")
    parser.add_argument("--max-targets", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if not args.apply or args.confirm != CONFIRMATION_PHRASE:
        print(json.dumps({
            "ok": False,
            "state": "blocked",
            "block_reasons": ["settlement_final_source_capture_requires_explicit_apply_confirmation"],
            "confirmation_phrase_required": CONFIRMATION_PHRASE,
            "boundary": {"shadow_only": True, "writes_archive_db": False, "places_orders": False},
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    authority, audit = load_resolution_authority_contract(args.authority_contract)
    if audit.get("ok") is not True:
        print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
        return 1

    def fetcher(**kwargs):
        return fetch_and_archive_wunderground_daily_raw_capture(**kwargs, timeout_seconds=args.timeout_seconds)

    result = run_settlement_final_source_shadow_capture_cycle(
        shadow_db_path=args.shadow_db,
        wunderground_raw_archive_dir=args.wunderground_raw_archive_dir,
        authority_contract=authority,
        captured_at=_now_utc(),
        max_targets=max(1, int(args.max_targets)),
        fetcher=fetcher,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
