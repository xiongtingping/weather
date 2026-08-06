#!/usr/bin/env python3
"""Build a canonical ET AWC truth extension from retained official raw."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.historical_awc_replay import (  # noqa: E402
    build_historical_awc_replay_evidence,
)
from bayes_basket_target.historical_awc_truth_contract import (  # noqa: E402
    build_historical_awc_truth_contract,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation-packet", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--replay-output", type=Path, required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()
    packet = load_bounded_json(
        args.observation_packet.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES
    )
    evidences = [
        build_historical_awc_replay_evidence(
            packets=[packet],
            raw_root=args.raw_root.resolve(strict=True),
            station_id=station,
            settlement_timezone="America/New_York",
            start_date=args.start_date,
            end_date=args.end_date,
            as_of_utc=args.as_of_utc,
        )
        for station in coordinate_registry_manifest()["station_ids"]
    ]
    replay_identity = {
        "schema": "bayes_basket_target_pr10_awc_replay_extension_v1",
        "as_of_utc": args.as_of_utc,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "settlement_timezone": "America/New_York",
        "source_lane": "AWC",
        "station_count": len(evidences),
        "truth_ready_station_day_count": sum(
            len(item["truth_ready_dates"]) for item in evidences
        ),
        "evidences": evidences,
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    replay = {
        **replay_identity,
        "content_sha256": content_sha256(replay_identity),
    }
    contract, receipt = build_historical_awc_truth_contract(
        evidences, as_of_utc=args.as_of_utc
    )
    write_bounded_json(args.replay_output.resolve(), replay)
    write_bounded_json(args.contract_output.resolve(), contract)
    write_bounded_json(args.receipt_output.resolve(), receipt)
    print({
        "truth_ready_station_day_count": replay[
            "truth_ready_station_day_count"
        ],
        "truth_fact_count": len(contract["truth_facts"]),
        "contract_content_sha256": contract["content_sha256"],
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
