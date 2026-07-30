#!/usr/bin/env python3
"""CLI adapter for package-owned target registry scope export."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB
from bayes_basket_target.market_registry import SCHEMA
from bayes_basket_target.registry_export import export_scope

__all__ = ["export_scope", "main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = export_scope(
            db_path=args.db,
            event_date=args.event_date,
            output=args.output.expanduser().resolve(),
        )
        print(json.dumps({
            "schema": result["schema"],
            "state": "EXPORTED",
            "event_date": args.event_date,
            "row_count": result["row_count"],
            "snapshot_sha256": result["snapshot_sha256"],
            "output": str(args.output.expanduser().resolve()),
            "calls_order_gateway": False,
            "places_orders": False,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "calls_order_gateway": False,
            "places_orders": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
