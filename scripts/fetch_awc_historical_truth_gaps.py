#!/usr/bin/env python3
"""Freeze or execute the bounded PR02.C official AWC truth-gap inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.awc_historical_gap_fetch import (  # noqa: E402
    DEFAULT_PROXY_URL,
    build_gap_inventory_from_receipt,
    execute_gap_fetch,
    verify_gap_inventory,
    write_gap_fetch_bundle,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


def _inventory(args: argparse.Namespace) -> int:
    inventory = build_gap_inventory_from_receipt(args.truth_gap_receipt)
    write_bounded_json(args.output, inventory)
    print(
        json.dumps(
            {
                "schema": inventory["schema"],
                "content_sha256": inventory["content_sha256"],
                "station_count": inventory["station_count"],
                "target_count": inventory["target_count"],
                "output": str(args.output.expanduser().resolve()),
                "writes_target_database": False,
                "calls_order_gateway": False,
                "orders_submitted": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _fetch(args: argparse.Namespace) -> int:
    inventory = load_bounded_json(args.inventory, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(inventory, dict):
        raise SystemExit("inventory_mapping_required")
    inventory = verify_gap_inventory(inventory)
    if inventory["content_sha256"] != args.expect_inventory_sha256.lower():
        raise SystemExit("inventory_content_sha256_mismatch")
    if int(inventory["target_count"]) != args.expect_target_count:
        raise SystemExit("inventory_target_count_mismatch")
    prior_receipt = None
    if args.prior_terminal_receipt is not None:
        prior_receipt = load_bounded_json(
            args.prior_terminal_receipt, limit=MAX_JSON_INPUT_BYTES
        )
        if not isinstance(prior_receipt, dict):
            raise SystemExit("prior_terminal_receipt_mapping_required")
        if (
            prior_receipt.get("content_sha256")
            != args.expect_prior_receipt_sha256.lower()
        ):
            raise SystemExit("prior_terminal_receipt_content_sha256_mismatch")
    elif args.expect_prior_receipt_sha256:
        raise SystemExit("prior_terminal_receipt_required")
    receipt, observations = execute_gap_fetch(
        inventory,
        raw_root=args.raw_root,
        requested_at_utc=args.requested_at_utc,
        proxy_url=DEFAULT_PROXY_URL,
        timeout_seconds=args.timeout_seconds,
        prior_receipt=prior_receipt,
    )
    write_gap_fetch_bundle(
        receipt=receipt,
        observation_artifact=observations,
        receipt_output=args.receipt_output,
        observation_output=args.observation_output,
    )
    print(
        json.dumps(
            {
                "schema": receipt["schema"],
                "content_sha256": receipt["content_sha256"],
                "inventory_content_sha256": receipt["inventory_content_sha256"],
                "terminal_count": receipt["terminal_count"],
                "terminal_state_counts": receipt["terminal_state_counts"],
                "terminal_subtype_counts": receipt["terminal_subtype_counts"],
                "query_count": receipt["query_count"],
                "response_bytes_total": receipt["response_bytes_total"],
                "observation_count": receipt["observation_count"],
                "newly_retained_raw_count": receipt["newly_retained_raw_count"],
                "receipt_output": str(args.receipt_output.expanduser().resolve()),
                "observation_output": str(args.observation_output.expanduser().resolve()),
                "proxy_url": DEFAULT_PROXY_URL,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "orders_submitted": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser(
        "inventory", help="Freeze exact targets from one verified truth-gap receipt."
    )
    inventory_parser.add_argument("--truth-gap-receipt", type=Path, required=True)
    inventory_parser.add_argument("--output", type=Path, required=True)
    inventory_parser.set_defaults(handler=_inventory)

    fetch_parser = subparsers.add_parser(
        "fetch", help="Resolve every frozen target through the fixed 1082 proxy."
    )
    fetch_parser.add_argument("--inventory", type=Path, required=True)
    fetch_parser.add_argument("--expect-inventory-sha256", required=True)
    fetch_parser.add_argument("--expect-target-count", type=int, required=True)
    fetch_parser.add_argument("--raw-root", type=Path, required=True)
    fetch_parser.add_argument("--requested-at-utc", required=True)
    fetch_parser.add_argument("--timeout-seconds", type=int, default=15)
    fetch_parser.add_argument("--prior-terminal-receipt", type=Path)
    fetch_parser.add_argument("--expect-prior-receipt-sha256", default="")
    fetch_parser.add_argument("--receipt-output", type=Path, required=True)
    fetch_parser.add_argument("--observation-output", type=Path, required=True)
    fetch_parser.set_defaults(handler=_fetch)

    args = parser.parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
