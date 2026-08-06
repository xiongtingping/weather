#!/usr/bin/env python3
"""Build PR14 strategy/utility/basket terminal artifacts read-only."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.pr14_strategy_basis import build_pr14_strategy_artifacts
from bayes_basket_target.resource_bounds import write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--policy-basis-output", type=Path, required=True)
    parser.add_argument("--utility-basis-output", type=Path, required=True)
    parser.add_argument("--terminal-basis-output", type=Path, required=True)
    parser.add_argument("--terminal-receipt-output", type=Path, required=True)
    args = parser.parse_args()
    policy, utility, terminals, receipt = build_pr14_strategy_artifacts(
        resolve_target_database(args.db), as_of_utc=args.as_of_utc
    )
    write_bounded_json(args.policy_basis_output, policy)
    write_bounded_json(args.utility_basis_output, utility)
    write_bounded_json(args.terminal_basis_output, terminals)
    write_bounded_json(args.terminal_receipt_output, receipt)
    print(json.dumps({
        "schema": receipt["schema"], "state": receipt["state"],
        "terminal_scope_count": receipt["terminal_scope_count"],
        "strategy_policy_expected_key_count": receipt["strategy_policy_expected_key_count"],
        "strategy_utility_expected_key_count": receipt["strategy_utility_expected_key_count"],
        "will_order_scope_count": receipt["will_order_scope_count"],
        "orders_submitted": receipt["orders_submitted"],
        "content_sha256": receipt["content_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
