#!/usr/bin/env python3
"""Seal retained regional evidence as unpublished canonical terminals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.regional_retained_terminal import (
    build_regional_retained_terminal_bundle,
)
from bayes_basket_target.resource_bounds import write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--coverage-scopes", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_regional_retained_terminal_bundle(
        as_of_utc=args.as_of_utc,
        coverage_scope_path=args.coverage_scopes,
        evidence_paths=args.evidence,
    )
    write_bounded_json(args.output, result)
    print(json.dumps({key: result[key] for key in (
        "state", "release_state", "terminal_count", "content_sha256",
    )}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
