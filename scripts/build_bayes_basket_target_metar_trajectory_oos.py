#!/usr/bin/env python3
"""Build a bounded, content-addressed METAR trajectory OOS ingress packet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    content_sha256,
)
from bayes_basket_target.metar_trajectory import (  # noqa: E402
    MetarTrajectoryLikelihoodArtifact,
)
from bayes_basket_target.metar_trajectory_training import (  # noqa: E402
    train_metar_trajectory_artifact,
)
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    metar_trajectory_scope_key,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from scripts.ingest_bayes_basket_probability_evidence import (  # noqa: E402
    SCHEMA as INGRESS_SCHEMA,
)


SCHEMA = "bayes_basket_metar_trajectory_oos_build_receipt_v1"
INPUT_LIMIT_BYTES = 8 * 1024 * 1024
OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024


def build_ingress_packet(
    *,
    result: Mapping[str, Any],
    generation: int,
    decided_at_utc: str,
) -> dict[str, Any]:
    artifact = MetarTrajectoryLikelihoodArtifact.from_mapping(
        result.get("artifact") or {}
    )
    evidence = result.get("training_evidence")
    if not isinstance(evidence, Mapping):
        raise ContractError(
            "metar_trajectory_training_evidence_missing"
        )
    gate_passed = evidence.get("oos_gate_passed") is True
    if artifact.activated is not gate_passed:
        raise ContractError(
            "metar_trajectory_training_gate_artifact_mismatch"
        )
    if isinstance(generation, bool) or generation < 1:
        raise ContractError(
            "metar_trajectory_activation_generation_invalid"
        )
    scope_key = metar_trajectory_scope_key(
        source_lane=artifact.source_lane,
        season_bin=artifact.season_bin,
        local_time_bucket=artifact.local_time_bucket,
        maturity_bin=artifact.maturity_bin,
        settlement_definition=artifact.settlement_definition,
    )
    lifecycle_scope = (
        "FORMAL_PROMOTED"
        if gate_passed
        else "PRODUCTION_NO_LIVE_CANDIDATE"
    )
    artifact_row = {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "station_id": artifact.station_id,
        "settlement_timezone": artifact.settlement_timezone,
        "scope_key": scope_key,
        "trained_through_utc": artifact.trained_through_utc,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "oos_gate_passed": gate_passed,
        "authority_scope": lifecycle_scope,
        "artifact": artifact.to_dict(),
        "content_sha256": artifact.content_sha256,
    }
    gate_receipt = {
        "schema": "bayes_basket_metar_trajectory_oos_gate_v1",
        "artifact_id": artifact.artifact_id,
        "training_evidence_sha256": evidence.get("content_sha256"),
        "baseline_oos_metrics": evidence.get("baseline_oos_metrics"),
        "candidate_oos_metrics": evidence.get("candidate_oos_metrics"),
        "stable_oos_gain": evidence.get("stable_oos_gain") is True,
        "effective_sample_n": evidence.get("effective_sample_n"),
        "minimum_effective_sample_n": evidence.get(
            "minimum_effective_sample_n"
        ),
        "oos_gate_passed": gate_passed,
    }
    gate_receipt_sha = content_sha256(gate_receipt)
    promotion_identity = {
        "promotion_id": (
            "metar-trajectory-promotion-"
            + content_sha256({
                "artifact_id": artifact.artifact_id,
                "gate_receipt_sha256": gate_receipt_sha,
            })[:32]
        ),
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": artifact.artifact_id,
        "decision_seq": 1,
        "decision": "PROMOTED" if gate_passed else "REJECTED",
        "gate_version": (
            "metar_trajectory_expanding_window_probability_oos_v1"
        ),
        "metrics_sha256": str(evidence.get("content_sha256") or ""),
        "decided_at_utc": decided_at_utc,
        "receipt_sha256": gate_receipt_sha,
    }
    promotion_row = {
        **promotion_identity,
        "content_sha256": content_sha256(promotion_identity),
    }
    activations: list[dict[str, Any]] = []
    if gate_passed:
        activation_identity = {
            "activation_id": artifact.activation_id,
            "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
            "artifact_id": artifact.artifact_id,
            "station_id": artifact.station_id,
            "scope_key": scope_key,
            "generation": generation,
            "valid_from_utc": artifact.valid_from_utc,
            "valid_until_utc": artifact.valid_until_utc,
            "activated_at_utc": decided_at_utc,
            "authority_scope": lifecycle_scope,
        }
        activations.append({
            **activation_identity,
            "content_sha256": content_sha256(activation_identity),
        })
    identity = {
        "schema": INGRESS_SCHEMA,
        "artifacts": [artifact_row],
        "promotions": [promotion_row],
        "activations": activations,
    }
    return {
        **identity,
        "packet_sha256": content_sha256(identity),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--packet-output", type=Path, required=True)
    # Persistence is opt-in so the offline evaluate-and-inspect flow keeps
    # writing nothing.  With ``--commit`` the packet's rows are ingested
    # through the evidence store's own validated inserts: the artifact row
    # always (deduplicated on its content-addressed id), an activation row
    # only when the gate promoted it -- a rejected candidate stays visible as
    # evidence without ever becoming resolvable authority.
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    input_path = args.input.expanduser().resolve()
    if (
        not input_path.is_file()
        or input_path.is_symlink()
        or input_path.stat().st_size <= 0
        or input_path.stat().st_size > INPUT_LIMIT_BYTES
    ):
        raise ContractError(
            "metar_trajectory_training_input_resource_limit_exceeded"
        )
    packet_input = load_bounded_json(
        input_path, limit=INPUT_LIMIT_BYTES
    )
    if (
        not isinstance(packet_input, Mapping)
        or packet_input.get("schema")
        != "bayes_basket_metar_trajectory_training_packet_v1"
    ):
        raise ContractError(
            "metar_trajectory_training_packet_schema_invalid"
        )
    identity = dict(packet_input)
    supplied_sha = str(identity.pop("content_sha256", "")).lower()
    if supplied_sha != content_sha256(identity):
        raise ContractError(
            "metar_trajectory_training_packet_sha256_mismatch"
        )
    boundary = packet_input.get("boundary")
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("execution_mode") != "production_no_live"
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("live_submit_authorized") is not False
        or int(boundary.get("orders_submitted") or 0) != 0
    ):
        raise ContractError(
            "metar_trajectory_training_boundary_invalid"
        )
    try:
        result = train_metar_trajectory_artifact(
            folds=packet_input.get("folds") or (),
            smoothing_candidates=(
                packet_input.get("smoothing_candidates") or ()
            ),
            minimum_training_folds=int(
                packet_input.get("minimum_training_folds") or 0
            ),
            minimum_effective_sample_n=float(
                packet_input.get("minimum_effective_sample_n") or 0
            ),
            artifact_parameters=(
                packet_input.get("artifact_parameters") or {}
            ),
        )
    except ContractError as exc:
        reason = str(exc)
        if reason not in {
            "metar_trajectory_training_complete_hourly_curve_folds_missing",
            "metar_trajectory_training_fold_count_invalid",
        }:
            raise
        source_manifest = packet_input.get("source_manifest")
        source_counts = (
            {
                "eligible_fold_count": int(
                    source_manifest.get("eligible_fold_count") or 0
                ),
                "v3_eligible_fold_count": int(
                    source_manifest.get("v3_eligible_fold_count") or 0
                ),
                "v3_incomplete_future_curve_fold_count": int(
                    source_manifest.get(
                        "v3_incomplete_future_curve_fold_count"
                    )
                    or 0
                ),
            }
            if isinstance(source_manifest, Mapping)
            else {}
        )
        blocked_identity = {
            "schema": "bayes_basket_metar_trajectory_oos_blocked_v1",
            "source_packet_sha256": supplied_sha,
            "state": "BLOCKED_EVIDENCE",
            "reason": reason,
            "source_counts": source_counts,
            "oos_gate_passed": False,
            "activations": [],
            "boundary": {
                "execution_mode": "production_no_live",
                "writes_production_database": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            },
        }
        blocked = {
            **blocked_identity,
            "content_sha256": content_sha256(blocked_identity),
        }
        write_bounded_json(
            args.packet_output.expanduser().resolve(),
            blocked,
            limit=OUTPUT_LIMIT_BYTES,
        )
        write_bounded_json(
            args.output.expanduser().resolve(),
            blocked,
            limit=OUTPUT_LIMIT_BYTES,
        )
        print(json.dumps({
            "state": "BLOCKED_EVIDENCE",
            "reason": reason,
            "oos_gate_passed": False,
            "orders_submitted": 0,
        }, sort_keys=True))
        return 2
    ingress_packet = build_ingress_packet(
        result=result,
        generation=int(packet_input.get("generation") or 0),
        decided_at_utc=str(
            packet_input.get("decided_at_utc") or ""
        ),
    )
    receipt = {
        "schema": SCHEMA,
        "source_packet_sha256": supplied_sha,
        "training_evidence": result["training_evidence"],
        "artifact": result["artifact"],
        "ingress_packet_sha256": ingress_packet["packet_sha256"],
        "oos_gate_passed": result["training_evidence"][
            "oos_gate_passed"
        ],
        "boundary": {
            "execution_mode": "production_no_live",
            "writes_production_database": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
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
    ingress_receipt = None
    if args.commit:
        if args.db is None:
            raise SystemExit("--commit requires --db")
        import sqlite3

        from bayes_basket_target.probability_evidence_store import (
            apply_probability_evidence_schema,
            insert_probability_evidence_activation,
            insert_probability_evidence_artifact,
        )

        conn = sqlite3.connect(
            str(args.db.expanduser().resolve(strict=True)), timeout=120.0
        )
        conn.execute("PRAGMA busy_timeout=120000")
        try:
            apply_probability_evidence_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            wrote_artifacts = 0
            wrote_activations = 0
            for row in ingress_packet.get("artifacts") or []:
                exists = conn.execute(
                    "SELECT 1 FROM basket_probability_evidence_artifact "
                    "WHERE artifact_id=?",
                    (str(row.get("artifact_id") or ""),),
                ).fetchone()
                if exists:
                    continue
                insert_probability_evidence_artifact(conn, dict(row))
                wrote_artifacts += 1
            for row in ingress_packet.get("activations") or []:
                insert_probability_evidence_activation(conn, dict(row))
                wrote_activations += 1
            conn.commit()
            ingress_receipt = {
                "wrote_artifacts": wrote_artifacts,
                "wrote_activations": wrote_activations,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    print(json.dumps({
        "artifact_id": result["artifact"]["artifact_id"],
        "oos_gate_passed": receipt["oos_gate_passed"],
        "packet_sha256": ingress_packet["packet_sha256"],
        "ingress": ingress_receipt,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
