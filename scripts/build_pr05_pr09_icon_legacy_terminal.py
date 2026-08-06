#!/usr/bin/env python3
"""Seal legacy ICON deterministic OOS claims as canonical terminals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.icon_deterministic_legacy_terminal import (
    build_icon_legacy_terminal_bundle,
)
from bayes_basket_target.resource_bounds import write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--coverage-scopes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_icon_legacy_terminal_bundle(
        as_of_utc=args.as_of_utc,
        report_path=args.report,
        binding_path=args.binding,
        coverage_scope_path=args.coverage_scopes,
    )
    write_bounded_json(args.output, result)
    print(json.dumps({key: result[key] for key in (
        "state", "release_state", "legacy_pass_count", "legacy_rejected_count",
        "terminal_count", "content_sha256",
    )}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
