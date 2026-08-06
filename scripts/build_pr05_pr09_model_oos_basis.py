#!/usr/bin/env python3
"""Build the PR05--PR09 no-live MODEL_OOS basis without SSOT writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.pr05_pr09_model_oos_basis import (
    build_pr05_pr09_model_oos_basis,
)
from bayes_basket_target.resource_bounds import write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--republisher-fixed-lead", type=Path, required=True)
    parser.add_argument("--republisher-cohorts", type=Path, required=True)
    parser.add_argument("--icon-eps-basis", type=Path, required=True)
    parser.add_argument("--native-ensemble-terminals", type=Path, required=True)
    parser.add_argument("--aifs-terminals", type=Path, required=True)
    parser.add_argument("--coverage-scopes", type=Path, required=True)
    parser.add_argument("--regional-retained-terminals", type=Path, required=True)
    parser.add_argument("--icon-legacy-terminals", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_pr05_pr09_model_oos_basis(
        as_of_utc=args.as_of_utc,
        republisher_fixed_lead_path=args.republisher_fixed_lead,
        republisher_cohort_path=args.republisher_cohorts,
        icon_eps_basis_path=args.icon_eps_basis,
        native_ensemble_terminal_path=args.native_ensemble_terminals,
        aifs_terminal_path=args.aifs_terminals,
        coverage_scope_path=args.coverage_scopes,
        regional_retained_terminal_path=args.regional_retained_terminals,
        icon_legacy_terminal_path=args.icon_legacy_terminals,
    )
    write_bounded_json(args.output, result)
    print(json.dumps({key: result[key] for key in (
        "state", "release_state", "scope_count",
        "canonical_model_oos_event_count", "historical_member_fact_count",
        "unscoped_terminal_count", "blocker_counts", "content_sha256",
    )}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
