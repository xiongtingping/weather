#!/usr/bin/env python3
"""Recommit an unpublished historical truth contract at the real build clock."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_contract import CanonicalContract  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import MAX_JSON_INPUT_BYTES, load_bounded_json, write_bounded_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--committed-at-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = CanonicalContract.from_mapping(
        load_bounded_json(args.input.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    ).to_mapping()
    truth_facts = []
    for raw in source["truth_facts"]:
        identity = {
            **{key: value for key, value in raw.items() if key != "content_sha256"},
            "canonical_committed_at_utc": args.committed_at_utc,
        }
        truth_facts.append({**identity, "content_sha256": content_sha256(identity)})
    identity = {
        **{key: value for key, value in source.items() if key not in {"content_sha256", "truth_facts", "as_of_utc", "builder_id"}},
        "as_of_utc": args.committed_at_utc,
        "builder_id": "truth.awc_historical_replay.pr10_real_clock.v1",
        "truth_facts": truth_facts,
    }
    contract = CanonicalContract.from_mapping(
        {**identity, "content_sha256": content_sha256(identity)}
    ).to_mapping()
    write_bounded_json(args.output.resolve(), contract)
    print({"truth_fact_count": len(truth_facts), "content_sha256": contract["content_sha256"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
