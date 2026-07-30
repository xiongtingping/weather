#!/usr/bin/env python3
"""Compile one target-SSOT basket cycle without writing or ordering."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import resolve_target_database  # noqa: E402
from bayes_basket_target.production_io import (  # noqa: E402
    compile_production_cycle_from_ssot,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--evaluation-at-utc", required=True)
    parser.add_argument("--season-bin", required=True)
    parser.add_argument("--maturity-bin", required=True)
    parser.add_argument("--liquidity-bin", default="liquid")
    parser.add_argument("--strategy-version", default="target-v1")
    parser.add_argument(
        "--policy-authority-scope",
        default="PRODUCTION_NO_LIVE_CANDIDATE",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    conn = None
    try:
        db = resolve_target_database(args.db)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        plan = compile_production_cycle_from_ssot(
            conn,
            station_id=args.station_id.strip().upper(),
            event_date=args.event_date,
            evaluation_at_utc=args.evaluation_at_utc,
            season_bin=args.season_bin,
            maturity_bin=args.maturity_bin,
            liquidity_bin=args.liquidity_bin,
            strategy_version=args.strategy_version,
            policy_authority_scope=args.policy_authority_scope,
        )
        result = {
            "schema": "bayes_basket_target_no_live_cycle_compile_v1",
            "state": "COMPILED_NO_WRITE",
            "plan": plan,
            "boundary": {
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
            },
        }
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_no_live_cycle_compile_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "boundary": {
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
            },
        }
    finally:
        if conn is not None:
            conn.close()
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "COMPILED_NO_WRITE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
