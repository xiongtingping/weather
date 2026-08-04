#!/usr/bin/env python3
"""Preserve existing truth identities and emit a PR02.C append contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.historical_awc_truth_rebase import (  # noqa: E402
    rebase_historical_awc_truth_contract,
    rebase_truth_gap_build_receipt,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--proposed-contract", type=Path, required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--source-build-receipt", type=Path)
    parser.add_argument("--truth-gap-receipt-output", type=Path)
    args = parser.parse_args()

    proposed = load_bounded_json(
        args.proposed_contract.expanduser().resolve(strict=True),
        limit=MAX_JSON_INPUT_BYTES,
    )
    database = args.database.expanduser().resolve(strict=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='truth' ORDER BY scope_key"
        ).fetchall()
    existing = [json.loads(str(row[0])) for row in rows]
    contract, receipt = rebase_historical_awc_truth_contract(
        proposed,
        existing_truth_facts=existing,
    )
    receipt_identity = {
        key: value for key, value in receipt.items() if key != "content_sha256"
    }
    receipt_identity["database_path"] = str(database)
    from bayes_basket_target.contracts import content_sha256

    receipt = {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }
    write_bounded_json(args.contract_output, contract)
    write_bounded_json(args.receipt_output, receipt)
    truth_gap_receipt = None
    if (args.source_build_receipt is None) != (args.truth_gap_receipt_output is None):
        raise SystemExit(
            "--source-build-receipt and --truth-gap-receipt-output must be supplied together"
        )
    if args.source_build_receipt is not None:
        source_build_receipt = load_bounded_json(
            args.source_build_receipt.expanduser().resolve(strict=True),
            limit=MAX_JSON_INPUT_BYTES,
        )
        truth_gap_receipt = rebase_truth_gap_build_receipt(
            source_build_receipt,
            source_contract_content_sha256=receipt[
                "source_contract_content_sha256"
            ],
            rebased_contract_content_sha256=contract["content_sha256"],
            identity_rebase_receipt_content_sha256=receipt["content_sha256"],
            preserved_existing_truth_fact_count=receipt[
                "preserved_existing_truth_fact_count"
            ],
            append_candidate_truth_fact_count=receipt[
                "append_candidate_truth_fact_count"
            ],
        )
        write_bounded_json(args.truth_gap_receipt_output, truth_gap_receipt)
    print(
        json.dumps(
            {
                "contract_content_sha256": contract["content_sha256"],
                "receipt_content_sha256": receipt["content_sha256"],
                "existing_truth_fact_count": receipt["existing_truth_fact_count"],
                "preserved_existing_truth_fact_count": receipt[
                    "preserved_existing_truth_fact_count"
                ],
                "append_candidate_truth_fact_count": receipt[
                    "append_candidate_truth_fact_count"
                ],
                "truth_gap_receipt_content_sha256": (
                    truth_gap_receipt["content_sha256"]
                    if truth_gap_receipt is not None
                    else None
                ),
                "writes_target_database": False,
                "calls_order_gateway": False,
                "orders_submitted": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
