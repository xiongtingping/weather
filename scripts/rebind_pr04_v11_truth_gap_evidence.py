#!/usr/bin/env python3
"""Rebind an exact terminal AWC truth-gap receipt to a V11 contract snapshot."""

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
    rebind_truth_gap_evidence,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-truth-gap-receipt", type=Path, required=True)
    parser.add_argument("--canonical-contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contract = load_bounded_json(
        args.canonical_contract,
        limit=MAX_CANONICAL_CONTRACT_SNAPSHOT_BYTES,
        max_nodes=MAX_CANONICAL_CONTRACT_SNAPSHOT_NODES,
    )
    if not isinstance(contract, dict):
        parser.error("canonical contract must be a JSON object")
    result = rebind_truth_gap_evidence(
        args.source_truth_gap_receipt,
        contract,
    )
    write_bounded_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
