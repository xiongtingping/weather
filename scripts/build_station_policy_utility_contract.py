#!/usr/bin/env python3
"""Materialize the versioned station-policy utility contract (read-only).

This writes only a content-addressed audit artifact.  It never reads or
mutates the SSOT database, creates utility scores, changes promotion state, or
contacts the order gateway.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.station_policy_forward_outcome_binding import (  # noqa: E402
    assert_station_policy_utility_contract,
    build_station_policy_utility_contract,
    build_station_policy_utility_contract_pointer,
)


DEFAULT_OUTPUT = ROOT / "output" / "audits" / "station_policy_utility_contract_v2.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    contract = build_station_policy_utility_contract()
    assert_station_policy_utility_contract(contract)
    pointer = build_station_policy_utility_contract_pointer(contract)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            contract,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "schema": contract["schema"],
                "artifact_id": contract["artifact_id"],
                "contract_version": contract["contract_version"],
                "content_sha256": contract["content_sha256"],
                "pointer_sha256": pointer["pointer_sha256"],
                "direct_fill_binding": "UNBOUND",
                "metric_state": pointer["metric_state"],
                "score_emitted": False,
                "promotion_permitted": False,
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
