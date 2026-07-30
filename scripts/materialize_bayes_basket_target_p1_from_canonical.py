#!/usr/bin/env python3
"""Materialize one target P1 from an existing canonical METAR/SPECI row."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_ingress import materialize_p1_from_canonical_observation
from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.resource_bounds import load_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        packet = load_bounded_json(args.packet)
        packet_observation_id = str(
            packet.get("observation_id") or ""
        ).strip()
        if packet_observation_id != args.observation_id:
            raise ValueError("p1_packet_observation_id_mismatch")
        predecessor = ProbabilityEnvelope.from_mapping(packet["predecessor"])
        conn = sqlite3.connect(resolve_target_database(args.db), timeout=60.0)
        conn.row_factory = sqlite3.Row
        try:
            result = materialize_p1_from_canonical_observation(
                conn,
                predecessor=predecessor,
                prediction_root=packet.get("prediction_root"),
                observation_id=args.observation_id,
                trajectory_artifact=packet.get("trajectory_artifact"),
                trajectory_proof=packet.get("trajectory_proof"),
                trajectory_observations=(
                    packet.get("trajectory_observations") or []
                ),
                bucket_centers_c=packet.get("bucket_centers_c"),
                bucket_upper_bounds_c=packet.get(
                    "bucket_upper_bounds_c"
                ),
                reported_high_advanced=packet.get(
                    "reported_high_advanced"
                ),
                decision_at_utc=packet.get("decision_at_utc"),
                fresh_until_utc=packet.get("fresh_until_utc"),
                created_at_utc=packet.get("created_at_utc"),
                commit=bool(args.apply),
            )
        finally:
            conn.close()
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_canonical_p1_ingress_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "places_orders": False,
            "calls_order_gateway": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {"VALIDATED_NO_WRITE", "COMMITTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
