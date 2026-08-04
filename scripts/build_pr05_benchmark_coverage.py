#!/usr/bin/env python3
"""Build the read-only PR05 benchmark coverage matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.benchmark_model_coverage import (
    GLOBAL_DETERMINISTIC_PRODUCTS,
    REGIONAL_BENCHMARK_PRODUCTS,
    build_global_deterministic_coverage_rows,
    build_regional_benchmark_coverage_rows,
    load_pr05_inputs,
)
from bayes_basket_target.model_system_coverage import build_coverage_matrix


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/bayes_basket_target_ssot.db")
    parser.add_argument(
        "--d1-build-receipt",
        type=Path,
        default=ROOT / "output/audits/d1_canonical_residual_45station_20260804_post_pr02c_v2/build_receipt.json",
    )
    parser.add_argument(
        "--regional-audit",
        type=Path,
        default=ROOT / "output/audits/regional_model_candidate_closure_20260803_post_proxy_refresh.json",
    )
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    facts, d1_receipt, regional = load_pr05_inputs(
        db=args.db,
        d1_build_receipt=args.d1_build_receipt,
        regional_audit=args.regional_audit,
    )
    rows = (
        *build_global_deterministic_coverage_rows(
            facts, d1_build_receipt=d1_receipt, as_of_utc=args.as_of_utc
        ),
        *build_regional_benchmark_coverage_rows(regional, as_of_utc=args.as_of_utc),
    )
    products = (*GLOBAL_DETERMINISTIC_PRODUCTS, *REGIONAL_BENCHMARK_PRODUCTS)
    matrix = build_coverage_matrix(rows, expected_product_ids=products, as_of_utc=args.as_of_utc)
    rendered = json.dumps(matrix, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
