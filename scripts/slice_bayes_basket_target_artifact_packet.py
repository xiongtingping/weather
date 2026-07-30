#!/usr/bin/env python3
"""Create one exact-station artifact packet from a reviewed no-live packet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from scripts.ingest_bayes_basket_target_artifacts import validate_packet_sha256  # noqa: E402


SCHEMA = "bayes_basket_target_artifact_ingress_v1"
STATION_FIELDS = (
    "calibration_artifacts",
    "ensemble_selection_evidence",
    "ensemble_artifacts",
    "calibration_activations",
    "policy_artifacts",
    "policy_activations",
    "cash_phase_policies",
    "cash_phase_authority_manifests",
)


def _rows(packet: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    raw = packet.get(field) or []
    if not isinstance(raw, list) or any(not isinstance(row, Mapping) for row in raw):
        raise ContractError(f"artifact_packet_{field}_rows_invalid")
    return [dict(row) for row in raw]


def slice_packet(
    packet: Mapping[str, Any],
    *,
    station_id: str,
) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    if not station:
        raise ContractError("artifact_slice_station_id_required")
    if packet.get("schema") != SCHEMA:
        raise ContractError("artifact_slice_schema_invalid")
    validate_packet_sha256(packet, required=True)
    if packet.get("execution_mode") != "production_no_live":
        raise ContractError("artifact_slice_execution_mode_invalid")
    for flag in (
        "live_order_authorized",
        "live_submit_authorized",
        "places_orders",
        "calls_order_gateway",
    ):
        if packet.get(flag) is not False:
            raise ContractError(f"artifact_slice_{flag}_must_be_false")

    identity = {
        key: value
        for key, value in packet.items()
        if key != "packet_sha256"
    }
    identity["station_ids"] = [station]
    for field in STATION_FIELDS:
        identity[field] = [
            row
            for row in _rows(packet, field)
            if str(row.get("station_id") or "").strip().upper() == station
        ]

    policy_activations = identity["policy_activations"]
    referenced_global_priors = {
        str(row.get("global_prior_id") or "").strip()
        for row in policy_activations
        if str(row.get("global_prior_id") or "").strip()
    }
    identity["global_priors"] = [
        row
        for row in _rows(packet, "global_priors")
        if str(row.get("global_prior_id") or "").strip()
        in referenced_global_priors
    ]
    if referenced_global_priors != {
        str(row.get("global_prior_id") or "").strip()
        for row in identity["global_priors"]
    }:
        raise ContractError("artifact_slice_global_prior_binding_incomplete")

    selected_artifact_ids = {
        str(row.get("artifact_id") or "").strip()
        for row in identity["policy_artifacts"]
    }
    identity["policy_promotions"] = [
        row
        for row in _rows(packet, "policy_promotions")
        if str(row.get("artifact_id") or "").strip() in selected_artifact_ids
    ]
    selected_calibration_ids = {
        str(row.get("calibration_artifact_id") or "").strip()
        for row in identity["calibration_artifacts"]
    } | {
        str(row.get("ensemble_artifact_id") or "").strip()
        for row in identity["ensemble_artifacts"]
    }
    identity["calibration_promotions"] = [
        row
        for row in _rows(packet, "calibration_promotions")
        if str(row.get("artifact_id") or "").strip() in selected_calibration_ids
    ]

    required_counts = {
        "policy_artifacts": 3,
        "policy_activations": 3,
        "cash_phase_policies": 1,
        "cash_phase_authority_manifests": 1,
    }
    for field, expected in required_counts.items():
        if len(identity[field]) != expected:
            raise ContractError(
                f"artifact_slice_{field}_count_invalid:"
                f"{len(identity[field])}:{expected}"
            )
    if not identity["global_priors"]:
        raise ContractError("artifact_slice_global_prior_missing")
    identity["vertical_slice"] = {
        "mode": "single_station_exact_scope",
        "station_id": station,
        "batch_expansion_authorized": False,
    }
    identity["live_order_authorized"] = False
    identity["live_submit_authorized"] = False
    identity["places_orders"] = False
    identity["calls_order_gateway"] = False
    identity["orders_submitted"] = 0
    return {**identity, "packet_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = slice_packet(
        load_bounded_json(args.packet),
        station_id=args.station_id,
    )
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(
        json.dumps(
            {
                "schema": result["schema"],
                "station_ids": result["station_ids"],
                "packet_sha256": result["packet_sha256"],
                "policy_artifact_count": len(result["policy_artifacts"]),
                "policy_activation_count": len(result["policy_activations"]),
                "cash_phase_policy_count": len(result["cash_phase_policies"]),
                "cash_phase_manifest_count": len(
                    result["cash_phase_authority_manifests"]
                ),
                "output": str(args.output.expanduser().resolve()),
                "live_submit_authorized": False,
                "orders_submitted": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
