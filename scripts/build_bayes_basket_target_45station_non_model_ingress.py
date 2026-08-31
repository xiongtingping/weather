#!/usr/bin/env python3
"""Build the canonical 45-station non-model no-live ingress packet."""

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
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)
from scripts.ingest_bayes_basket_target_artifacts import (  # noqa: E402
    validate_packet_sha256,
)
from bayes_basket_target.fleet import expected_station_count


SCHEMA = "bayes_basket_target_artifact_ingress_v1"
MODEL_FIELDS = (
    "calibration_artifacts",
    "ensemble_selection_evidence",
    "ensemble_artifacts",
    "calibration_activations",
    "calibration_promotions",
)
NON_MODEL_FIELDS = (
    "global_priors",
    "policy_artifacts",
    "policy_activations",
    "cash_phase_policies",
    "cash_phase_authority_manifests",
)


def _rows(packet: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    raw = packet.get(field)
    if not isinstance(raw, list) or any(
        not isinstance(row, Mapping) for row in raw
    ):
        raise ContractError(f"non_model_ingress_{field}_rows_invalid")
    return [dict(row) for row in raw]


def build_non_model_packet(
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Strip incompatible model/OOS rows without weakening their contracts."""

    if packet.get("schema") != SCHEMA:
        raise ContractError("non_model_ingress_source_schema_invalid")
    validate_packet_sha256(packet, required=True)
    if (
        packet.get("execution_mode") != "production_no_live"
        or packet.get("policy_authority_scope")
        != "PRODUCTION_NO_LIVE_CANDIDATE"
        or packet.get("promoted") is not False
    ):
        raise ContractError("non_model_ingress_authority_invalid")
    for key in (
        "live_order_authorized",
        "live_submit_authorized",
        "places_orders",
        "calls_order_gateway",
    ):
        if packet.get(key) is not False:
            raise ContractError(f"non_model_ingress_{key}_must_be_false")
    if int(packet.get("orders_submitted") or 0) != 0:
        raise ContractError("non_model_ingress_orders_submitted_must_be_zero")

    expected_stations = tuple(coordinate_registry_manifest()["station_ids"])
    supplied_stations = tuple(
        sorted(
            str(value or "").strip().upper()
            for value in packet.get("station_ids") or []
        )
    )
    if supplied_stations != expected_stations:
        raise ContractError("non_model_ingress_station_scope_invalid")

    selected = {
        field: _rows(packet, field)
        for field in NON_MODEL_FIELDS
    }
    expected_counts = {
        "global_priors": 1,
        "policy_artifacts": expected_station_count() * 3,
        "policy_activations": expected_station_count() * 3,
        "cash_phase_policies": expected_station_count(),
        "cash_phase_authority_manifests": expected_station_count(),
    }
    for field, expected in expected_counts.items():
        if len(selected[field]) != expected:
            raise ContractError(
                f"non_model_ingress_{field}_count_invalid"
            )
    for field in (
        "policy_artifacts",
        "policy_activations",
        "cash_phase_policies",
        "cash_phase_authority_manifests",
    ):
        stations = {
            str(row.get("station_id") or "").strip().upper()
            for row in selected[field]
        }
        if tuple(sorted(stations)) != expected_stations:
            raise ContractError(
                f"non_model_ingress_{field}_station_scope_invalid"
            )

    artifact_ids = {
        str(row.get("artifact_id") or "").strip()
        for row in selected["policy_artifacts"]
    }
    prior_ids = {
        str(row.get("global_prior_id") or "").strip()
        for row in selected["global_priors"]
    }
    for activation in selected["policy_activations"]:
        fallback_families = {
            str(value or "").strip()
            for value in activation.get("fallback_families") or []
        }
        if (
            str(activation.get("global_prior_id") or "").strip()
            not in prior_ids
            or any(
                (
                    str(activation.get(field) or "").strip()
                    not in artifact_ids
                    and family not in fallback_families
                )
                for field, family in (
                    ("target_artifact_id", "target"),
                    ("reserve_artifact_id", "reserve"),
                    ("cadence_artifact_id", "cadence"),
                )
            )
        ):
            raise ContractError(
                "non_model_ingress_policy_binding_invalid"
            )

    identity: dict[str, Any] = {
        "schema": SCHEMA,
        "producer_schema": (
            "bayes_basket_target_45station_non_model_ingress_v1"
        ),
        "source_packet_sha256": str(packet["packet_sha256"]),
        "station_ids": list(expected_stations),
        **selected,
        "policy_promotions": [],
        "execution_mode": "production_no_live",
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "promoted": False,
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    for field in MODEL_FIELDS:
        identity[field] = []
    return {
        **identity,
        "packet_sha256": content_sha256(identity),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_non_model_packet(load_bounded_json(args.packet))
    output = args.output.expanduser().resolve()
    write_bounded_json(output, result)
    print(
        json.dumps(
            {
                "schema": result["producer_schema"],
                "state": "BUILT",
                "station_count": len(result["station_ids"]),
                "policy_artifact_count": len(result["policy_artifacts"]),
                "policy_activation_count": len(
                    result["policy_activations"]
                ),
                "cash_phase_policy_count": len(
                    result["cash_phase_policies"]
                ),
                "cash_phase_manifest_count": len(
                    result["cash_phase_authority_manifests"]
                ),
                "model_row_count": sum(
                    len(result[field]) for field in MODEL_FIELDS
                ),
                "packet_sha256": result["packet_sha256"],
                "output": str(output),
                "live_submit_authorized": False,
                "calls_order_gateway": False,
                "orders_submitted": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
