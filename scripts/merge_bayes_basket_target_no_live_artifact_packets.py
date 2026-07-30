#!/usr/bin/env python3
"""Merge reviewed cash/policy and calibration candidates into one ingress packet."""

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


SCHEMA = "bayes_basket_target_artifact_ingress_v1"
CALIBRATION_FIELDS = (
    "calibration_artifacts",
    "ensemble_selection_evidence",
    "ensemble_artifacts",
    "calibration_activations",
)
EMPTY_CALIBRATION_FIELDS = ("calibration_promotions",)
FORBIDDEN_CALIBRATION_POLICY_FIELDS = (
    "global_priors",
    "policy_artifacts",
    "policy_activations",
    "cash_phase_policies",
    "cash_phase_authority_manifests",
    "policy_promotions",
)
PRIMARY_KEYS = {
    "calibration_artifacts": "calibration_artifact_id",
    "ensemble_selection_evidence": "selection_evidence_id",
    "ensemble_artifacts": "ensemble_artifact_id",
    "calibration_activations": "activation_id",
}


def _rows(packet: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    raw = packet.get(field) or []
    if not isinstance(raw, list) or any(not isinstance(row, Mapping) for row in raw):
        raise ContractError(f"artifact_packet_{field}_rows_invalid")
    return [dict(row) for row in raw]


def _verify_packet_sha(packet: Mapping[str, Any]) -> None:
    supplied = str(packet.get("packet_sha256") or "").strip().lower()
    identity = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError("cash_policy_packet_sha256_mismatch")


def merge_packets(
    cash_policy_packet: Mapping[str, Any],
    calibration_packet: Mapping[str, Any],
) -> dict[str, Any]:
    if cash_policy_packet.get("schema") != SCHEMA:
        raise ContractError("cash_policy_packet_schema_invalid")
    if calibration_packet.get("schema") != SCHEMA:
        raise ContractError("calibration_packet_schema_invalid")
    _verify_packet_sha(cash_policy_packet)
    if cash_policy_packet.get("execution_mode") != "production_no_live":
        raise ContractError("cash_policy_packet_execution_mode_invalid")
    for flag in ("live_order_authorized", "live_submit_authorized", "places_orders", "calls_order_gateway"):
        if cash_policy_packet.get(flag) is not False:
            raise ContractError(f"cash_policy_packet_{flag}_must_be_false")

    authority = calibration_packet.get("authority_receipt")
    if not isinstance(authority, Mapping):
        raise ContractError("calibration_authority_receipt_missing")
    if authority.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY":
        raise ContractError("calibration_authority_scope_invalid")
    if authority.get("formal_promotion_created") is not False:
        raise ContractError("calibration_formal_promotion_forbidden")
    if authority.get("live_order_authorized") is not False:
        raise ContractError("calibration_live_order_authorized")
    if any(_rows(calibration_packet, field) for field in (*EMPTY_CALIBRATION_FIELDS, *FORBIDDEN_CALIBRATION_POLICY_FIELDS)):
        raise ContractError("calibration_packet_contains_forbidden_policy_or_promotion_rows")

    identity = {
        key: value
        for key, value in cash_policy_packet.items()
        if key != "packet_sha256"
    }
    for field in CALIBRATION_FIELDS:
        rows = _rows(calibration_packet, field)
        primary_key = PRIMARY_KEYS[field]
        identities = [str(row.get(primary_key) or "").strip() for row in rows]
        if any(not value for value in identities) or len(identities) != len(set(identities)):
            raise ContractError(f"calibration_packet_{field}_identity_invalid")
        identity[field] = rows
    identity["calibration_promotions"] = []
    identity["calibration_authority_receipt"] = dict(authority)
    identity["calibration_source_packet_sha256"] = content_sha256(calibration_packet)
    identity["promoted"] = False
    identity["live_order_authorized"] = False
    identity["live_submit_authorized"] = False
    identity["places_orders"] = False
    identity["calls_order_gateway"] = False
    identity["orders_submitted"] = 0
    return {**identity, "packet_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cash-policy-packet", type=Path, required=True)
    parser.add_argument("--calibration-packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = merge_packets(
        load_bounded_json(args.cash_policy_packet),
        load_bounded_json(args.calibration_packet),
    )
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "schema": result["schema"],
        "output": str(args.output.expanduser().resolve()),
        "packet_sha256": result["packet_sha256"],
        "station_count": len(result.get("station_ids") or []),
        "calibration_artifact_count": len(result["calibration_artifacts"]),
        "ensemble_artifact_count": len(result["ensemble_artifacts"]),
        "calibration_activation_count": len(result["calibration_activations"]),
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
