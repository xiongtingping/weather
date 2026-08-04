#!/usr/bin/env python3
"""Build a bounded registry-v1 from activated TAF artifacts, offline only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.taf_artifact_registry import (  # noqa: E402
    MAX_ARTIFACTS,
    build_taf_artifact_registry,
    validate_taf_artifact_registry,
)


MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact",
        action="append",
        type=Path,
        required=True,
        help="Activated strict-OOS TAF v2 artifact; repeat at most 45 times.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path)
    args = parser.parse_args()
    if len(args.artifact) > MAX_ARTIFACTS:
        raise ValueError("taf_artifact_registry_count_invalid")
    sources = [path.expanduser().resolve(strict=True) for path in args.artifact]
    target = args.output.expanduser().resolve()
    if target in sources:
        raise ValueError("taf_artifact_registry_output_input_collision")
    artifact_rows = [load_bounded_json(path, limit=MAX_INPUT_BYTES) for path in sources]
    registry = build_taf_artifact_registry(artifact_rows)
    validate_taf_artifact_registry(registry)
    wrote = write_bounded_json_if_changed(
        target,
        registry,
        limit=MAX_OUTPUT_BYTES,
    )
    receipt = {
        "schema": "bayes_basket_target_taf_artifact_registry_build_receipt_v1",
        "state": "updated" if wrote else "unchanged_zero_write",
        "output_path": str(target),
        "registry_content_sha256": registry["content_sha256"],
        "artifact_count": len(registry["artifacts"]),
        "artifact_ids": [
            str(item["artifact_id"]) for item in registry["artifacts"]
        ],
        "source_paths": [str(path) for path in sources],
        "boundary": {
            "offline_only": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
    }
    if args.receipt_output is not None:
        receipt_target = args.receipt_output.expanduser().resolve()
        if receipt_target == target or receipt_target in sources:
            raise ValueError("taf_artifact_registry_receipt_path_collision")
        write_bounded_json_if_changed(
            receipt_target,
            receipt,
            limit=MAX_OUTPUT_BYTES,
        )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
