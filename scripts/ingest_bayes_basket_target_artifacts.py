#!/usr/bin/env python3
"""Validate or append an offline-produced target artifact packet."""

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

from bayes_basket_target.artifact_ingress import CONFIRMATION, ingest_artifact_bundle, SCHEMA
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json
from bayes_basket_target.sqlite_busy import begin_immediate_if_idle


def validate_packet_sha256(
    packet: Mapping[str, Any],
    *,
    required: bool,
) -> str | None:
    stored = packet.get("packet_sha256")
    if stored is None:
        if required:
            raise ValueError("artifact_ingress_packet_sha256_required")
        return None
    normalized = str(stored).strip().lower()
    if len(normalized) != 64 or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise ValueError("artifact_ingress_packet_sha256_invalid")
    identity = dict(packet)
    identity.pop("packet_sha256", None)
    if content_sha256(identity) != normalized:
        raise ValueError("artifact_ingress_packet_sha256_mismatch")
    return normalized


def packet_binding_counts(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any],
) -> dict[str, int]:
    bindings = (
        (
            "cash_phase_policies",
            "basket_cash_phase_policy_artifact",
            "policy_version",
        ),
        (
            "cash_phase_authority_manifests",
            "basket_cash_phase_authority_manifest",
            "manifest_sha256",
        ),
        (
            "calibration_artifacts",
            "calibration_model_artifact",
            "calibration_artifact_id",
        ),
        (
            "ensemble_selection_evidence",
            "calibration_ensemble_selection_evidence",
            "selection_evidence_id",
        ),
        (
            "ensemble_artifacts",
            "calibration_ensemble_artifact",
            "ensemble_artifact_id",
        ),
        (
            "calibration_activations",
            "calibration_activation",
            "activation_id",
        ),
    )
    result: dict[str, int] = {}
    for packet_key, table, primary_key in bindings:
        raw_rows = packet.get(packet_key) or []
        if not isinstance(raw_rows, list) or any(
            not isinstance(row, Mapping) for row in raw_rows
        ):
            raise ValueError(f"{packet_key}_rows_invalid")
        values = tuple(str(row.get(primary_key) or "") for row in raw_rows)
        if any(not value for value in values):
            raise ValueError(f"{packet_key}_{primary_key}_missing")
        if not values:
            result[packet_key] = 0
            continue
        placeholders = ",".join("?" for _ in values)
        result[packet_key] = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {primary_key} IN ({placeholders})",
                values,
            ).fetchone()[0]
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--require-packet-sha256", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        packet = load_bounded_json(args.packet)
        if not isinstance(packet, Mapping):
            raise ValueError("artifact_ingress_packet_mapping_required")
        packet_sha256 = validate_packet_sha256(
            packet,
            required=bool(args.apply or args.require_packet_sha256),
        )
        if args.apply and args.confirm != CONFIRMATION:
            raise ValueError("artifact_ingress_confirmation_invalid")
        with sqlite3.connect(resolve_target_database(args.db), timeout=60.0) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            # The bundle's SAVEPOINT would open a deferred transaction that
            # looks rows up before writing them; with the resident writer
            # committing in between, the first write fails at once on
            # SQLITE_BUSY_SNAPSHOT and busy_timeout is never consulted
            # (2026-10-04: the cash-phase onboarding packet, validate and
            # apply alike).  Take the lock first.
            begin_immediate_if_idle(conn)
            result = ingest_artifact_bundle(conn, packet, commit=bool(args.apply))
            bound_counts = packet_binding_counts(conn, packet)
        result = {
            **result,
            "packet": str(args.packet.expanduser().resolve()),
            "packet_sha256": packet_sha256,
            "policy_authority_scope": packet.get("policy_authority_scope"),
            "station_count": len(packet.get("station_ids") or []),
            "station_ids": list(packet.get("station_ids") or []),
            "packet_expected_counts": {
                key: len(packet.get(key) or [])
                for key in (
                    "cash_phase_policies",
                    "cash_phase_authority_manifests",
                    "calibration_artifacts",
                    "ensemble_selection_evidence",
                    "ensemble_artifacts",
                    "calibration_activations",
                )
            },
            "packet_binding_counts": bound_counts,
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "authorizes_order": False,
            "orders_submitted": 0,
        }
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "authorizes_order": False,
            "places_orders": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
    if args.receipt is not None:
        write_bounded_json(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {"VALIDATED_NO_WRITE", "COMMITTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
