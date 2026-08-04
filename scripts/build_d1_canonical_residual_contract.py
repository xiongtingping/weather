#!/usr/bin/env python3
"""Build the d1 canonical residual contract from the active SSOT only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.d1_canonical_builder import (  # noqa: E402
    build_d1_canonical_residual_contract,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()
    contract, receipt = build_d1_canonical_residual_contract(
        db=args.database.expanduser().resolve(strict=True),
        stations=coordinate_registry_manifest()["station_ids"],
        as_of_utc=args.as_of_utc,
    )
    write_bounded_json(args.contract_output.expanduser().resolve(), contract)
    write_bounded_json(args.receipt_output.expanduser().resolve(), receipt)
    print(json.dumps({
        "contract_content_sha256": contract["content_sha256"],
        "receipt_content_sha256": receipt["content_sha256"],
        "model_fact_count": receipt["model_fact_count"],
        "truth_fact_count": receipt["truth_fact_count"],
        "residual_fact_count": receipt["residual_fact_count"],
        "ready_station_count": receipt["ready_station_count"],
        "blocked_station_count": receipt["blocked_station_count"],
        "writes_target_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
