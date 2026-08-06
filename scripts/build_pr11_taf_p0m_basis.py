#!/usr/bin/env python3
"""Build PR11's unresolved 45-station TAF/P0-M artifacts read-only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.pr11_taf_p0m_basis import build_pr11_taf_p0m_artifacts
from bayes_basket_target.resource_bounds import write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--taf-gap-evidence", type=Path)
    parser.add_argument("--pr10-selection-terminal", type=Path)
    parser.add_argument("--pr10-p0-bundle", type=Path)
    parser.add_argument("--expected-basis-output", type=Path, required=True)
    parser.add_argument("--terminal-basis-output", type=Path, required=True)
    parser.add_argument("--terminal-receipt-output", type=Path, required=True)
    args = parser.parse_args()
    expected, terminals, receipt = build_pr11_taf_p0m_artifacts(
        resolve_target_database(args.db), as_of_utc=args.as_of_utc,
        taf_gap_evidence_path=args.taf_gap_evidence,
        pr10_selection_terminal_path=args.pr10_selection_terminal,
        pr10_p0_bundle_path=args.pr10_p0_bundle,
    )
    write_bounded_json(args.expected_basis_output, expected)
    write_bounded_json(args.terminal_basis_output, terminals)
    write_bounded_json(args.terminal_receipt_output, receipt)
    print(json.dumps({
        "schema": receipt["schema"],
        "state": receipt["state"],
        "release_state": receipt["release_state"],
        "blockers": receipt["blockers"],
        "terminal_scope_count": receipt["terminal_scope_count"],
        "canonical_p0_activation_root_count": receipt[
            "canonical_p0_activation_root_count"
        ],
        "typed_expected_key_count": receipt["typed_expected_key_count"],
        "unresolved_input_receipt_count": receipt[
            "unresolved_input_receipt_count"
        ],
        "legacy_exact_join_sample_count_total": receipt[
            "legacy_exact_join_sample_count_total"
        ],
        "legacy_exact_join_max_sample_count": receipt[
            "legacy_exact_join_max_sample_count"
        ],
        "legacy_exact_join_qualified_scope_count_at_24": receipt[
            "legacy_exact_join_qualified_scope_count_at_24"
        ],
        "terminal_basis_content_sha256": terminals["content_sha256"],
        "expected_basis_content_sha256": expected["content_sha256"],
        "content_sha256": receipt["content_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
