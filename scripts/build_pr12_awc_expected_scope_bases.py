#!/usr/bin/env python3
"""Build PR12 truth expected-scope bases from the frozen AWC domain contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.pr12_awc_observation import (  # noqa: E402
    build_pr12_expected_scope_bases,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--builder-receipt", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--observation-basis-output", type=Path, required=True)
    parser.add_argument("--day-basis-output", type=Path, required=True)
    parser.add_argument("--terminal-receipt-output", type=Path, required=True)
    args = parser.parse_args()

    observation, day, terminal = build_pr12_expected_scope_bases(
        load_bounded_json(args.contract.expanduser().resolve(strict=True)),
        load_bounded_json(args.builder_receipt.expanduser().resolve(strict=True)),
        release_as_of_utc=args.as_of_utc,
    )
    write_bounded_json(args.observation_basis_output.expanduser().resolve(), observation)
    write_bounded_json(args.day_basis_output.expanduser().resolve(), day)
    write_bounded_json(args.terminal_receipt_output.expanduser().resolve(), terminal)
    print(json.dumps(terminal, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
