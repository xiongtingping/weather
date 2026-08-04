#!/usr/bin/env python3
"""Materialize read-only Δv2 observations from fact-bound source rows."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.delta_v2_forward_observation_candidate import (  # noqa: E402
    assert_forward_observation_candidate,
    build_from_connection,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "delta_v2_target_owned_forward_observation_candidate.json"
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    database = args.db.expanduser().resolve()
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        artifact = build_from_connection(conn, as_of_utc=args.as_of_utc)
    finally:
        conn.close()
    assert_forward_observation_candidate(artifact)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    write_bounded_json(output, artifact, limit=MAX_OUTPUT_BYTES)
    print(json.dumps({
        "schema": artifact["schema"],
        "state": artifact["state"],
        "observation_count": artifact["counts"]["observation_count"],
        "candidate_price_count": artifact["counts"]["candidate_price_count"],
        "counterfactual_settlement_attribution_count": artifact["counts"]["counterfactual_settlement_attribution_count"],
        "source_blocker_count": len(artifact["source_blockers"]),
        "formal_promotion_allowed": artifact["formal_promotion_allowed"],
        "orders_submitted": artifact["orders_submitted"],
        "output": str(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
