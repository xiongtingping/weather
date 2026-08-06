#!/usr/bin/env python3
"""Build the one-time PF50/GEFS/GEPS ET gap ledger without side effects."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.pr06_pr09_native_ensemble_et_gap import (  # noqa: E402
    build_native_ensemble_et_gap_bundle,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES, load_bounded_json, write_bounded_json_if_changed,
)


def _load(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise RuntimeError("native_ensemble_et_cli_mapping_required")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", action="append", type=Path, required=True)
    parser.add_argument("--truth-supersession-contract", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = build_native_ensemble_et_gap_bundle(
        packets=[_load(path) for path in args.packet],
        truth_supersession_contract=_load(args.truth_supersession_contract),
        as_of_utc=args.as_of_utc,
    )
    output = args.output.expanduser().resolve()
    write_bounded_json_if_changed(output, bundle, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps({
        key: bundle[key]
        for key in (
            "state", "release_state", "source_packet_count",
            "expected_scope_count", "fact_count", "terminal_count",
            "blocker_counts", "diagnostic_counts", "content_sha256",
            "writes_database", "publishes_pr04_pointer", "starts_services",
            "releases_live_pause", "orders_submitted", "consumable",
        )
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
