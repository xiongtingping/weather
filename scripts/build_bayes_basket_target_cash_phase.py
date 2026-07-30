#!/usr/bin/env python3
"""Build one explicit cash-phase ingress packet without network or DB I/O."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.cash_phase_producer import build_cash_phase_ingress
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--policy-json", type=Path)
    source.add_argument(
        "--candidate-bundle",
        type=Path,
        help="Resolve the unique station cash policy from a bounded no-live candidate bundle",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--station-timezone", required=True)
    parser.add_argument("--observed-at-utc", required=True)
    parser.add_argument("--current-phase", choices=("HEAVY", "STANDARD", "TAIL"), required=True)
    parser.add_argument("--checkpoint-id", required=True)
    args = parser.parse_args()
    try:
        if args.policy_json is not None:
            policy = load_bounded_json(args.policy_json)
        else:
            bundle = load_bounded_json(args.candidate_bundle)
            policies = [
                item
                for item in bundle.get("cash_phase_policies", [])
                if isinstance(item, dict)
                and str(item.get("station_id") or "").strip().upper()
                == args.station_id.strip().upper()
            ]
            if len(policies) != 1:
                raise RuntimeError("cash_phase_policy_resolution_not_unique")
            policy = policies[0]
        result = build_cash_phase_ingress(
            policy=policy,
            station_id=args.station_id,
            event_date_local=args.event_date,
            station_timezone=args.station_timezone,
            observed_at_utc=args.observed_at_utc,
            current_phase=args.current_phase,
            checkpoint_id=args.checkpoint_id,
        )
        if args.output is not None:
            write_bounded_json(args.output.expanduser().resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "schema": "bayes_basket_target_cash_phase_producer_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "places_orders": False,
            "calls_order_gateway": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
