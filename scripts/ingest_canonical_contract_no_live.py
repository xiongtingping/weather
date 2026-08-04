#!/usr/bin/env python3
"""Validate or append one canonical contract under no-live authority."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_contract import CanonicalContract  # noqa: E402
from bayes_basket_target.canonical_ingress import ingest_canonical_contract  # noqa: E402
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


def _assert_no_live_authority(contract: CanonicalContract) -> None:
    if any(
        model.formal_live_authority or model.p1_eligible
        for model in contract.model_facts
    ):
        raise ContractError("canonical_no_live_model_authority_forbidden")
    if any(
        activation.authority_scope != "PRODUCTION_NO_LIVE_ONLY"
        for activation in contract.activation_facts
    ):
        raise ContractError("canonical_no_live_activation_authority_forbidden")
    if any(
        truth.source_lane != "AWC"
        or truth.source_role != "AWC_HISTORICAL_REPLAY_INGRESS"
        or truth.authority_scope != "PRODUCTION_NO_LIVE_ONLY"
        for truth in contract.truth_facts
    ):
        raise ContractError("canonical_no_live_truth_authority_forbidden")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--apply-production-no-live", action="store_true")
    args = parser.parse_args()
    raw = load_bounded_json(
        args.contract.expanduser().resolve(strict=True),
        limit=MAX_JSON_INPUT_BYTES,
    )
    contract = CanonicalContract.from_mapping(raw)
    _assert_no_live_authority(contract)
    database = args.database.expanduser().resolve(strict=True)
    with sqlite3.connect(database, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        before = conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact"
        ).fetchone()[0]
        ingress = ingest_canonical_contract(
            conn,
            contract,
            commit=bool(args.apply_production_no_live),
        )
        after = conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact"
        ).fetchone()[0]
    identity = {
        "schema": "bayes_basket_target_canonical_no_live_ingress_receipt_v1",
        "state": ingress["state"],
        "contract_content_sha256": contract.content_sha256,
        "database_path": str(database),
        "before_count": int(before),
        "after_count": int(after),
        "validated_counts": ingress["validated_counts"],
        "inserted_count": ingress["inserted_count"],
        "replayed_count": ingress["replayed_count"],
        "ingress_receipt_sha256": ingress["receipt_sha256"],
        "production_no_live": True,
        "writes_target_database": bool(args.apply_production_no_live and after != before),
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    receipt = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(args.receipt_output.expanduser().resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
