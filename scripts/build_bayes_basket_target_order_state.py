#!/usr/bin/env python3
"""Build the target no-live order-state ingress packet without network I/O."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.order_state_producer import build_no_live_order_state_ingress
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--activation",
        type=Path,
        default=ROOT / "config" / "trading" / "bayes_basket_target_production_no_live_activation_v1.json",
    )
    parser.add_argument("--observed-at-utc", default="")
    parser.add_argument("--station-id", default="")
    parser.add_argument("--event-date", default="")
    args = parser.parse_args()
    observed = args.observed_at_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        db_path = resolve_target_database(args.db)
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            result = build_no_live_order_state_ingress(
                conn,
                activation_path=args.activation.expanduser().resolve(),
                observed_at_utc=observed,
                station_id=str(args.station_id or "").strip(),
                event_date=str(args.event_date or "").strip(),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "schema": "bayes_basket_target_order_state_producer_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "places_orders": False,
            "calls_order_gateway": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
