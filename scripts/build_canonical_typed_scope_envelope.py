#!/usr/bin/env python3
"""Materialize one read-only canonical typed-scope source projection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_typed_scope_builder import (
    build_canonical_typed_scope_envelope,
)
from bayes_basket_target.resource_bounds import write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--scope-type",
        choices=(
            "TRUTH_DAY",
            "TAF_P0M",
            "TRAJECTORY_P1",
            "STRATEGY_UTILITY",
            "MARKET_WS",
            "RUNTIME",
        ),
        required=True,
    )
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--envelope-output", type=Path)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()

    envelope, receipt = build_canonical_typed_scope_envelope(
        args.database,
        scope_type=args.scope_type,
        as_of_utc=args.as_of_utc,
    )
    if envelope is not None:
        if args.envelope_output is None:
            parser.error("supported source scope requires --envelope-output")
        write_bounded_json(args.envelope_output, envelope)
    elif args.envelope_output is not None:
        parser.error(
            "missing-producer scope must not receive --envelope-output"
        )
    write_bounded_json(args.receipt_output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
