#!/usr/bin/env python3
"""Build the AIFS historical terminal receipt without SSOT or runtime writes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.pr06_aifs_single_historical_terminal import (  # noqa: E402
    build_pr06_aifs_single_historical_terminal_bundle,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES, load_bounded_json, write_bounded_json_if_changed,
)


def _load(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise RuntimeError("pr06_aifs_terminal_cli_mapping_required")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-capture", required=True, type=Path)
    parser.add_argument("--official-decode", required=True, type=Path)
    parser.add_argument("--awc-contract", required=True, type=Path)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    capture_path = args.official_capture.expanduser().resolve(strict=True)
    bundle = build_pr06_aifs_single_historical_terminal_bundle(
        official_capture=_load(capture_path),
        official_decode=_load(args.official_decode),
        awc_contract=_load(args.awc_contract),
        as_of_utc=args.as_of_utc,
        official_capture_file_sha256=_sha256_file(capture_path),
    )
    write_bounded_json_if_changed(args.output.expanduser().resolve(), bundle, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps({
        key: bundle[key]
        for key in (
            "state", "release_state", "expected_scope_count", "fact_count",
            "terminal_count", "blocker_counts", "diagnostic_counts", "content_sha256",
            "writes_database", "publishes_pr04_pointer", "starts_services",
            "releases_live_pause", "orders_submitted", "consumable",
        )
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
