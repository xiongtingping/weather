#!/usr/bin/env python3
"""Compile PR10 native-current terminal evidence into a zero-write basis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.pr10_p0_basis import build_pr10_p0_terminal_basis
from bayes_basket_target.resource_bounds import write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--terminal-receipt", type=Path, action="append", required=True)
    parser.add_argument("--probe-result", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_pr10_p0_terminal_basis(
        resolve_target_database(args.db),
        as_of_utc=args.as_of_utc,
        terminal_receipt_paths=args.terminal_receipt,
        probe_result_paths=args.probe_result,
    )
    write_bounded_json(args.output, result)
    print(
        json.dumps(
            {
                "schema": result["schema"],
                "state": result["state"],
                "release_state": result["release_state"],
                "blockers": result["blockers"],
                "terminal_scope_count": result["terminal_scope_count"],
                "canonical_station_count": result["canonical_station_count"],
                "canonical_gen2_activation_count": result[
                    "canonical_gen2_activation_count"
                ],
                "activation_bound_station_count": result[
                    "activation_bound_station_count"
                ],
                "activation_missing_station_count": result[
                    "activation_missing_station_count"
                ],
                "content_sha256": result["content_sha256"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
