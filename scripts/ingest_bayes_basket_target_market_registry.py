#!/usr/bin/env python3
"""Validate or append a target-local market-registry snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.market_registry import ingest_target_market_registry  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--as-of-utc", default="", help="ET boundary clock used to reject future event days")
    parser.add_argument(
        "--receipt",
        type=Path,
        default=ROOT / "output/audits/bayes_basket_target_market_registry_ingress.json",
    )
    args = parser.parse_args()
    try:
        db = resolve_target_database(args.db)
        with sqlite3.connect(db, timeout=60.0) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            result = ingest_target_market_registry(
                conn,
                snapshot_path=args.snapshot,
                commit=bool(args.commit),
                as_of_utc=str(args.as_of_utc or "").strip() or None,
            )
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_market_registry_snapshot_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "writes_shared_live_database": False,
            "places_orders": False,
        }
    receipt = args.receipt.expanduser().resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    write_bounded_json(receipt, result)
    print(json.dumps({**result, "receipt": str(receipt)}, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {"VALIDATED_NO_WRITE", "COMMITTED", "ALREADY_COMMITTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
