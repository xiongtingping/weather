#!/usr/bin/env python3
"""Build PR05--PR09's V13 exact-scope/terminal bundle without publication."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.pr05_pr09_v13_scope_builder import (  # noqa: E402
    build_pr05_pr09_v13_scope_bundle,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-matrix", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    matrix = load_bounded_json(args.coverage_matrix.expanduser().resolve(strict=True))
    bundle = build_pr05_pr09_v13_scope_bundle(matrix, as_of_utc=args.as_of_utc)
    write_bounded_json_if_changed(
        args.output.expanduser().resolve(), bundle, limit=64 * 1024 * 1024,
        max_nodes=1_000_000,
    )
    print(json.dumps({
        key: bundle[key]
        for key in (
            "state", "coverage_scope_count", "v13_expected_scope_count",
            "terminal_scope_count", "content_sha256",
        )
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
