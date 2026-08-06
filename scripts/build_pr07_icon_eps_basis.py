#!/usr/bin/env python3
"""Build unpublished PR07 ICON EPS facts against an exact PR12 contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.pr07_icon_eps_basis import (  # noqa: E402
    build_pr07_icon_eps_basis,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


def _load(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(
        path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES
    )
    if not isinstance(value, Mapping):
        raise RuntimeError("pr07_icon_eps_basis_input_mapping_required")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bound-archive", type=Path, required=True)
    parser.add_argument("--awc-contract", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = build_pr07_icon_eps_basis(
        bound_archive=_load(args.bound_archive),
        awc_contract=_load(args.awc_contract),
        as_of_utc=args.as_of_utc,
    )
    output = args.output.expanduser().resolve()
    write_bounded_json_if_changed(output, bundle, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps({
        key: bundle[key]
        for key in (
            "state", "release_state", "expected_scope_count", "fact_count",
            "terminal_count", "blocker_counts", "content_sha256",
            "writes_database", "publishes_pr04_pointer", "starts_services",
            "releases_live_pause", "orders_submitted", "consumable",
        )
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
