#!/usr/bin/env python3
"""Build a bounded, fail-closed TAF ↔ OOS P0-M exact-join dataset."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.taf_exact_join import (  # noqa: E402
    MAX_EXACT_JOIN_ROWS,
    build_taf_exact_join,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--taf-dataset", type=Path, required=True)
    parser.add_argument("--nwp-crossfit-dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=MAX_EXACT_JOIN_ROWS)
    args = parser.parse_args()
    taf = load_bounded_json(args.taf_dataset.expanduser().resolve())
    nwp = load_bounded_json(args.nwp_crossfit_dataset.expanduser().resolve())
    if not isinstance(taf, dict) or not isinstance(nwp, dict):
        raise SystemExit("input datasets must be JSON objects")
    result = build_taf_exact_join(taf, nwp, max_rows=args.max_rows)
    write_bounded_json(args.output.expanduser().resolve(), result)
    return 0 if not result["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
