#!/usr/bin/env python3
"""Build and optionally append one exact V13 lead/model-family OOS scope."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.model_oos_builder import build_model_oos_contract
from bayes_basket_target.resource_bounds import (
    assert_target_db_capacity,
    load_bounded_json,
    write_bounded_json,
)


CONFIRMATION = "--confirm-production-no-live-model-oos-materialization"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(CONFIRMATION, action="store_true")
    args = parser.parse_args()
    if args.commit and not getattr(
        args, "confirm_production_no_live_model_oos_materialization"
    ):
        parser.error(f"--commit requires {CONFIRMATION}")

    raw = load_bounded_json(args.input)
    if not isinstance(raw, dict) or set(raw) != {
        "as_of_utc", "event_inputs", "cohort_spec"
    }:
        parser.error("input must contain exactly as_of_utc,event_inputs,cohort_spec")
    db = resolve_target_database(args.db)
    if args.commit:
        assert_target_db_capacity(db)
        conn = sqlite3.connect(db)
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    try:
        contract = build_model_oos_contract(
            conn,
            as_of_utc=str(raw["as_of_utc"]),
            event_inputs=raw["event_inputs"],
            cohort_spec=raw["cohort_spec"],
        )
        receipt = ingest_canonical_contract(
            conn,
            contract,
            commit=args.commit,
            ingested_at_utc=str(raw["as_of_utc"]),
        )
    finally:
        conn.close()
    write_bounded_json(args.contract_output, contract)
    write_bounded_json(args.receipt_output, receipt)
    print(json.dumps({
        "state": receipt["state"],
        "validated_counts": receipt["validated_counts"],
        "inserted_count": receipt["inserted_count"],
        "replayed_count": receipt["replayed_count"],
        "contract_content_sha256": contract["content_sha256"],
        "receipt_sha256": receipt["receipt_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
