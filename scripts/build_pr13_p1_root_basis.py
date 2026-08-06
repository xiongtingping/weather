#!/usr/bin/env python3
"""Build PR13's 180-scope P1 readiness artifacts without any SSOT write."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.pr13_p1_basis import build_pr13_p1_readiness_artifacts
from bayes_basket_target.resource_bounds import write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument(
        "--awc-observation-contract", type=Path,
        help="Validated unpublished PR12 parent contract; never written to SSOT.",
    )
    parser.add_argument("--expected-basis-output", type=Path, required=True)
    parser.add_argument("--typed-envelope-output", type=Path, required=True)
    parser.add_argument("--terminal-receipt-output", type=Path, required=True)
    args = parser.parse_args()
    awc_parent = (
        None
        if args.awc_observation_contract is None
        else json.loads(
            args.awc_observation_contract.expanduser().resolve(strict=True).read_text()
        )
    )
    expected, envelope, receipt = build_pr13_p1_readiness_artifacts(
        resolve_target_database(args.db), as_of_utc=args.as_of_utc,
        awc_observation_contract=awc_parent,
    )
    write_bounded_json(args.expected_basis_output, expected)
    write_bounded_json(args.typed_envelope_output, envelope)
    write_bounded_json(args.terminal_receipt_output, receipt)
    print(json.dumps({
        "schema": receipt["schema"],
        "state": receipt["state"],
        "release_state": receipt["release_state"],
        "blockers": receipt["blockers"],
        "blocker_counts": receipt["blocker_counts"],
        "terminal_scope_count": receipt["terminal_scope_count"],
        "canonical_awc_observation_fact_count": receipt[
            "canonical_awc_observation_fact_count"
        ],
        "canonical_p0_snapshot_fact_count": receipt[
            "canonical_p0_snapshot_fact_count"
        ],
        "expected_basis_content_sha256": expected["content_sha256"],
        "typed_envelope_content_sha256": envelope["content_sha256"],
        "content_sha256": receipt["content_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
