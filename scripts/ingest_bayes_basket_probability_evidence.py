#!/usr/bin/env python3
"""Validate/append offline P0-history or P1-trajectory authority packets."""

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
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.probability_evidence_store import (
    insert_probability_evidence_activation,
    insert_probability_evidence_artifact,
    insert_probability_evidence_promotion,
    probability_evidence_schema_status,
)
from bayes_basket_target.resource_bounds import load_bounded_json


SCHEMA = "bayes_basket_probability_evidence_ingress_v1"
CONFIRMATION = "INGEST_PROBABILITY_EVIDENCE_NO_LIVE"
MAX_PACKET_ROWS = 256


def ingest(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any],
    *,
    commit: bool,
    finalize_transaction: bool = True,
    expected_station_id: str | None = None,
    require_no_activations: bool = False,
) -> dict[str, Any]:
    if not commit and not finalize_transaction:
        raise ContractError(
            "probability_evidence_ingress_transaction_mode_invalid"
        )
    if packet.get("schema") != SCHEMA:
        raise ContractError("probability_evidence_packet_schema_invalid")
    identity = dict(packet)
    supplied_sha = str(identity.pop("packet_sha256", "")).lower()
    if (
        len(supplied_sha) != 64
        or content_sha256(identity) != supplied_sha
    ):
        raise ContractError("probability_evidence_packet_sha256_mismatch")
    artifacts = packet.get("artifacts") or []
    promotions = packet.get("promotions") or []
    activations = packet.get("activations") or []
    if (
        not isinstance(artifacts, list)
        or not isinstance(promotions, list)
        or not isinstance(activations, list)
        or len(artifacts) + len(promotions) + len(activations)
        > MAX_PACKET_ROWS
        or any(
            not isinstance(row, dict)
            for row in artifacts + promotions + activations
        )
    ):
        raise ContractError("probability_evidence_packet_rows_invalid")
    if require_no_activations and activations:
        raise ContractError(
            "probability_evidence_candidate_activation_forbidden"
        )
    if expected_station_id is not None:
        expected_station = str(expected_station_id).strip().upper()
        if not expected_station:
            raise ContractError(
                "probability_evidence_expected_station_invalid"
            )
        artifact_ids: set[str] = set()
        for row in artifacts:
            artifact_id = str(row.get("artifact_id") or "").strip()
            station_id = str(row.get("station_id") or "").strip().upper()
            if not artifact_id or artifact_id in artifact_ids:
                raise ContractError(
                    "probability_evidence_scoped_artifact_identity_invalid"
                )
            if station_id != expected_station:
                raise ContractError(
                    "probability_evidence_packet_station_scope_mismatch"
                )
            artifact_ids.add(artifact_id)
        if not artifact_ids:
            raise ContractError(
                "probability_evidence_scoped_packet_artifact_required"
            )
        for row in promotions + activations:
            if str(row.get("artifact_id") or "").strip() not in artifact_ids:
                raise ContractError(
                    "probability_evidence_scoped_reference_outside_packet"
                )
            row_station = row.get("station_id")
            if (
                row_station is not None
                and str(row_station).strip().upper() != expected_station
            ):
                raise ContractError(
                    "probability_evidence_packet_station_scope_mismatch"
                )
    if probability_evidence_schema_status(conn)["ok"] is not True:
        raise ContractError("probability_evidence_schema_incomplete")
    savepoint = "probability_evidence_packet_ingress"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        artifact_receipts = [
            insert_probability_evidence_artifact(conn, row)
            for row in artifacts
        ]
        promotion_receipts = [
            insert_probability_evidence_promotion(conn, row)
            for row in promotions
        ]
        activation_receipts = [
            insert_probability_evidence_activation(conn, row)
            for row in activations
        ]
        if commit:
            conn.execute(f"RELEASE {savepoint}")
            if finalize_transaction:
                conn.commit()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    return {
        "schema": SCHEMA,
        "state": (
            "COMMITTED"
            if commit and finalize_transaction
            else (
                "STAGED_IN_CALLER_TRANSACTION"
                if commit
                else "VALIDATED_NO_WRITE"
            )
        ),
        "artifact_receipts": artifact_receipts,
        "promotion_receipts": promotion_receipts,
        "activation_receipts": activation_receipts,
        "row_count": len(artifacts) + len(promotions) + len(activations),
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--expected-station-id")
    parser.add_argument("--require-no-activations", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply and args.confirm != CONFIRMATION:
            raise ContractError(
                "probability_evidence_ingress_confirmation_invalid"
            )
        packet = load_bounded_json(args.packet)
        if not isinstance(packet, Mapping):
            raise ContractError(
                "probability_evidence_packet_mapping_required"
            )
        with sqlite3.connect(
            resolve_target_database(args.db), timeout=60.0
        ) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            result = ingest(
                conn,
                packet,
                commit=bool(args.apply),
                expected_station_id=args.expected_station_id,
                require_no_activations=args.require_no_activations,
            )
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return (
        0
        if result["state"] in {"COMMITTED", "VALIDATED_NO_WRITE"}
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
