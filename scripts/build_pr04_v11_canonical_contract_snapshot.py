#!/usr/bin/env python3
"""Build the read-only V11 canonical contract snapshot consumed by PR04."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_contract_snapshot import (  # noqa: E402
    MAX_CANONICAL_CONTRACT_SNAPSHOT_BYTES,
    MAX_CANONICAL_CONTRACT_SNAPSHOT_NODES,
    build_canonical_contract_snapshot,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    write_bounded_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()
    contract, receipt = build_canonical_contract_snapshot(
        args.database,
        as_of_utc=args.as_of_utc,
    )
    write_bounded_json(
        args.contract_output,
        contract,
        limit=MAX_CANONICAL_CONTRACT_SNAPSHOT_BYTES,
        max_nodes=MAX_CANONICAL_CONTRACT_SNAPSHOT_NODES,
    )
    write_bounded_json(args.receipt_output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
