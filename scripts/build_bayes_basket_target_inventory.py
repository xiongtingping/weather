#!/usr/bin/env python3
"""Build a read-only target confirmed-inventory ingress packet."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.inventory_producer import build_confirmed_inventory_ingress
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "reported_trajectory_held_yes_tokens.json",
    )
    parser.add_argument("--remaining-budget-usdc", type=float, required=True)
    parser.add_argument("--station-id", default="")
    parser.add_argument("--event-date", default="")
    args = parser.parse_args()
    try:
        db_path = resolve_target_database(args.db)
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            result = build_confirmed_inventory_ingress(
                conn,
                manifest_path=args.manifest.expanduser().resolve(),
                remaining_budget_usdc=args.remaining_budget_usdc,
                station_id=str(args.station_id or "").strip(),
                event_date=str(args.event_date or "").strip(),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "schema": "bayes_basket_confirmed_inventory_producer_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "places_orders": False,
            "calls_order_gateway": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
