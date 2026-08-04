#!/usr/bin/env python3
"""Validate or atomically apply the PR02 canonical fact-store v2 rebuild."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_ssot_v2_migration import (  # noqa: E402
    migrate_canonical_ssot_v2,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--truth-contract", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument(
        "--apply-production-no-live-canonical-v2",
        action="store_true",
        help="atomically retire the invalid v1 fact table; never grants live authority",
    )
    args = parser.parse_args()
    contract = load_bounded_json(
        args.truth_contract.expanduser().resolve(strict=True),
        limit=MAX_JSON_INPUT_BYTES,
    )
    database = args.database.expanduser().resolve(strict=True)
    with sqlite3.connect(database, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        receipt = migrate_canonical_ssot_v2(
            conn,
            contract,
            commit=bool(args.apply_production_no_live_canonical_v2),
        )
    write_bounded_json(args.receipt_output.expanduser().resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
