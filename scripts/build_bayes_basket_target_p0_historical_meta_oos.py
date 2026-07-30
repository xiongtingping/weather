#!/usr/bin/env python3
"""Build and optionally materialize bounded P0 historical-meta OOS evidence."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.p0_historical_meta import (
    P0HistoricalMetaCombinationArtifact,
)
from bayes_basket_target.p0_historical_meta_training import (
    train_p0_historical_meta_artifact,
)
from bayes_basket_target.probability_evidence_store import (
    p0_historical_meta_scope_key,
    same_day_climatology_scope_key,
)
from bayes_basket_target.same_day_climatology import (
    SameDayClimatologyArtifact,
)
from bayes_basket_target.resource_bounds import write_bounded_json
from scripts.ingest_bayes_basket_probability_evidence import (
    SCHEMA as INGRESS_SCHEMA,
    ingest as ingest_probability_evidence,
)


INPUT_LIMIT_BYTES = 4 * 1024 * 1024
OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024


def _read_packet(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size <= 0 or size > INPUT_LIMIT_BYTES:
        raise ContractError("historical_meta_input_resource_limit_exceeded")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ContractError("historical_meta_input_not_object")
    return raw


def build_ingress_packet(
    *,
    result: Mapping[str, Any],
    generation: int,
    activated_at_utc: str,
    climatology_artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical artifact/promotion/activation ingress packet."""
    artifact = P0HistoricalMetaCombinationArtifact.from_mapping(
        result.get("artifact") or {}
    )
    training = result.get("training_evidence")
    if not isinstance(training, Mapping):
        raise ContractError("historical_meta_training_evidence_missing")
    oos_passed = (
        artifact.stable_oos_gain
        and artifact.effective_sample_n
        >= artifact.minimum_effective_sample_n
    )
    authority_scope = (
        "FORMAL_PROMOTED"
        if oos_passed
        else "PRODUCTION_NO_LIVE_CANDIDATE"
    )
    scope_key = p0_historical_meta_scope_key(
        season_bin=artifact.season_bin,
        lead_bucket=artifact.lead_bucket,
        run_cycle=artifact.run_cycle,
        settlement_definition=artifact.settlement_definition,
        settlement_timezone=artifact.settlement_timezone,
    )
    artifact_row = {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": "P0_HISTORICAL_META",
        "station_id": artifact.station_id,
        "settlement_timezone": artifact.settlement_timezone,
        "scope_key": scope_key,
        "trained_through_utc": artifact.trained_through_utc,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "oos_gate_passed": oos_passed,
        "authority_scope": authority_scope,
        "artifact": artifact.to_dict(),
        "content_sha256": artifact.content_sha256,
    }
    gate_receipt = {
        "schema": "bayes_basket_p0_historical_meta_oos_gate_v1",
        "artifact_id": artifact.artifact_id,
        "training_evidence_sha256": training.get("content_sha256"),
        "oos_gate_passed": oos_passed,
        "stable_oos_gain": artifact.stable_oos_gain,
        "effective_sample_n": artifact.effective_sample_n,
        "minimum_effective_sample_n": (
            artifact.minimum_effective_sample_n
        ),
    }
    gate_receipt_sha = content_sha256(gate_receipt)
    promotion_identity = {
        "promotion_id": (
            "p0-historical-promotion-"
            + content_sha256({
                "artifact_id": artifact.artifact_id,
                "gate_receipt_sha256": gate_receipt_sha,
            })[:32]
        ),
        "artifact_kind": "P0_HISTORICAL_META",
        "artifact_id": artifact.artifact_id,
        "decision_seq": 1,
        "decision": "PROMOTED" if oos_passed else "REJECTED",
        "gate_version": "p0_historical_meta_walk_forward_oos_v1",
        "metrics_sha256": str(training.get("content_sha256") or ""),
        "decided_at_utc": activated_at_utc,
        "receipt_sha256": gate_receipt_sha,
    }
    promotion_row = {
        **promotion_identity,
        "content_sha256": content_sha256(promotion_identity),
    }
    activation_row: dict[str, Any] | None = None
    if oos_passed:
        if isinstance(generation, bool) or generation < 1:
            raise ContractError(
                "historical_meta_activation_generation_invalid"
            )
        activation_identity = {
            "activation_id": artifact.activation_id,
            "artifact_kind": "P0_HISTORICAL_META",
            "artifact_id": artifact.artifact_id,
            "station_id": artifact.station_id,
            "scope_key": scope_key,
            "generation": generation,
            "valid_from_utc": artifact.valid_from_utc,
            "valid_until_utc": artifact.valid_until_utc,
            "activated_at_utc": activated_at_utc,
            "authority_scope": authority_scope,
        }
        activation_row = {
            **activation_identity,
            "content_sha256": content_sha256(activation_identity),
        }
    artifact_rows = [artifact_row]
    promotion_rows = [promotion_row]
    activation_rows = (
        [activation_row] if activation_row is not None else []
    )
    if climatology_artifact is not None:
        climate = SameDayClimatologyArtifact.from_mapping(
            climatology_artifact
        )
        if (
            climate.station_id != artifact.station_id
            or climate.settlement_timezone
            != artifact.settlement_timezone
            or climate.settlement_definition
            != artifact.settlement_definition
        ):
            raise ContractError(
                "historical_meta_climatology_scope_mismatch"
            )
        climate_scope = same_day_climatology_scope_key(
            f"{climate.target_year:04d}-{climate.calendar_day}",
            climate.settlement_definition,
        )
        climate_row = {
            "artifact_id": climate.artifact_id,
            "artifact_kind": "SAME_DAY_CLIMATOLOGY",
            "station_id": climate.station_id,
            "settlement_timezone": climate.settlement_timezone,
            "scope_key": climate_scope,
            "trained_through_utc": climate.built_at_utc,
            "valid_from_utc": climate.valid_from_utc,
            "valid_until_utc": climate.valid_until_utc,
            "oos_gate_passed": oos_passed,
            "authority_scope": authority_scope,
            "artifact": climate.to_dict(),
            "content_sha256": climate.content_sha256,
        }
        climate_gate_receipt = {
            "schema": "bayes_basket_same_day_climatology_pair_gate_v1",
            "artifact_id": climate.artifact_id,
            "meta_artifact_id": artifact.artifact_id,
            "meta_training_evidence_sha256": training.get(
                "content_sha256"
            ),
            "oos_gate_passed": oos_passed,
        }
        climate_gate_sha = content_sha256(climate_gate_receipt)
        climate_promotion_identity = {
            "promotion_id": (
                "same-day-climatology-promotion-"
                + content_sha256({
                    "artifact_id": climate.artifact_id,
                    "gate_receipt_sha256": climate_gate_sha,
                })[:32]
            ),
            "artifact_kind": "SAME_DAY_CLIMATOLOGY",
            "artifact_id": climate.artifact_id,
            "decision_seq": 1,
            "decision": "PROMOTED" if oos_passed else "REJECTED",
            "gate_version": (
                "p0_historical_meta_pair_walk_forward_oos_v1"
            ),
            "metrics_sha256": str(
                training.get("content_sha256") or ""
            ),
            "decided_at_utc": activated_at_utc,
            "receipt_sha256": climate_gate_sha,
        }
        artifact_rows.append(climate_row)
        promotion_rows.append({
            **climate_promotion_identity,
            "content_sha256": content_sha256(
                climate_promotion_identity
            ),
        })
        if oos_passed:
            climate_activation_identity = {
                "activation_id": climate.activation_id,
                "artifact_kind": "SAME_DAY_CLIMATOLOGY",
                "artifact_id": climate.artifact_id,
                "station_id": climate.station_id,
                "scope_key": climate_scope,
                "generation": generation,
                "valid_from_utc": climate.valid_from_utc,
                "valid_until_utc": climate.valid_until_utc,
                "activated_at_utc": activated_at_utc,
                "authority_scope": authority_scope,
            }
            activation_rows.append({
                **climate_activation_identity,
                "content_sha256": content_sha256(
                    climate_activation_identity
                ),
            })
    identity = {
        "schema": INGRESS_SCHEMA,
        "artifacts": artifact_rows,
        "promotions": promotion_rows,
        "activations": activation_rows,
    }
    return {
        **identity,
        "packet_sha256": content_sha256(identity),
    }


def materialize_training_result(
    conn: sqlite3.Connection,
    *,
    result: Mapping[str, Any],
    generation: int,
    activated_at_utc: str,
) -> dict[str, Any]:
    """Materialize only through the canonical packet ingress."""

    packet = build_ingress_packet(
        result=result,
        generation=generation,
        activated_at_utc=activated_at_utc,
    )
    ingress = ingest_probability_evidence(
        conn,
        packet,
        commit=True,
    )
    artifact = P0HistoricalMetaCombinationArtifact.from_mapping(
        result.get("artifact") or {}
    )
    oos_passed = (
        artifact.stable_oos_gain
        and artifact.effective_sample_n
        >= artifact.minimum_effective_sample_n
    )
    authority_scope = (
        "FORMAL_PROMOTED"
        if oos_passed
        else "PRODUCTION_NO_LIVE_CANDIDATE"
    )
    return {
        "schema": "bayes_basket_p0_historical_meta_materialization_receipt_v1",
        "artifact_id": artifact.artifact_id,
        "artifact_content_sha256": artifact.content_sha256,
        "oos_gate_passed": oos_passed,
        "authority_scope": authority_scope,
        "packet_sha256": packet["packet_sha256"],
        "ingress": ingress,
        "writes_order_state": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--packet-output", type=Path, required=True)
    args = parser.parse_args()
    packet = _read_packet(args.input.expanduser().resolve())
    packet_identity = dict(packet)
    supplied_sha = str(
        packet_identity.pop("content_sha256", "")
    ).lower()
    if (
        packet.get("schema")
        != "bayes_basket_p0_historical_meta_training_packet_v1"
        or supplied_sha != content_sha256(packet_identity)
    ):
        raise ContractError("historical_meta_training_packet_invalid")
    boundary = packet.get("boundary")
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("execution_mode") != "production_no_live"
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("live_submit_authorized") is not False
        or int(boundary.get("orders_submitted") or 0) != 0
    ):
        raise ContractError(
            "historical_meta_training_boundary_invalid"
        )
    result = train_p0_historical_meta_artifact(
        folds=packet.get("folds") or (),
        coefficient_candidates=packet.get("coefficient_candidates") or (),
        minimum_meta_training_folds=int(
            packet.get("minimum_meta_training_folds", 1)
        ),
        artifact_parameters=packet.get("artifact_parameters") or {},
    )
    ingress_packet = build_ingress_packet(
        result=result,
        generation=int(packet.get("generation", 1)),
        activated_at_utc=str(packet.get("activated_at_utc") or ""),
        climatology_artifact=(
            packet.get("same_day_climatology_artifact")
        ),
    )
    receipt: dict[str, Any] = {
        "schema": "bayes_basket_p0_historical_meta_build_receipt_v1",
        **result,
        "source_packet_sha256": supplied_sha,
        "ingress_packet_sha256": ingress_packet["packet_sha256"],
        "writes_production_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    write_bounded_json(
        args.packet_output.expanduser().resolve(),
        ingress_packet,
        limit=OUTPUT_LIMIT_BYTES,
    )
    write_bounded_json(
        args.output.expanduser().resolve(),
        receipt,
        limit=OUTPUT_LIMIT_BYTES,
    )
    print(json.dumps({
        "artifact_id": result["artifact"]["artifact_id"],
        "oos_gate_passed": (
            result["training_evidence"]["stable_oos_gain"]
            and result["training_evidence"]["effective_sample_n"]
            >= result["artifact"]["minimum_effective_sample_n"]
        ),
        "packet_sha256": ingress_packet["packet_sha256"],
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
