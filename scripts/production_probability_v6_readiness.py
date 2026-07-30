#!/usr/bin/env python3
"""Read-only readiness audit for the production-successor v6 epoch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_state_bayes_v6.schema_contract import build_forward_plan
from physical_state_bayes_v6.artifact_contracts import (
    validate_observation_artifact,
    validate_transition_artifact,
)
from production_probability_v6_activation_contract import validate_v6_activation
from production_probability_v6_activation_contract import IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
from identifiable_physical_state_bayes.core import (
    validate_observation_artifact as validate_physical_observation_artifact,
    validate_state_model_artifact,
)
from identifiable_physical_state_bayes.reporting import validate_reporting_operator_artifact
from production_probability_v6_runtime_manifest import (
    audit_v6_runtime_manifest_dependency_closure,
    v6_runtime_manifest_sha256,
)
from production_probability_v6_live_route_contract import audit_v6_live_routes


SCHEMA_VERSION = "production_probability_v6_readiness_v1"


def audit(
    root: Path,
    db: Path,
    *,
    cutover_candidate_pre_rehearsal: bool = False,
) -> dict[str, object]:
    required = (
        "docs/adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md",
        "physical_state_bayes_v6/contracts.py",
        "physical_state_bayes_v6/core.py",
        "physical_state_bayes_v6/schema_contract.py",
        "production_probability_v6_activation_contract.py",
        "production_probability_v6_p0_runtime.py",
        "production_probability_v6_p0_input_runtime.py",
        "production_probability_v6_initial_writer.py",
        "production_probability_v6_candidate_compiler.py",
        "production_probability_v6_public_ssot_writer.py",
        "production_probability_v6_event_runtime.py",
        "production_probability_v6_canonical_event_drain_runtime.py",
        "production_probability_v6_amsc_archive_ingress.py",
        "scripts/apply_amsc_probability_fast_lane_v6_from_plan.py",
        "production_probability_v6_runtime_manifest.py",
        "production_probability_v6_weak_revision_runtime.py",
        "production_probability_v6_weak_revision_writer.py",
        "production_probability_v6_weak_revision_event_runtime.py",
        "scripts/apply_amsc_taf_forecast_revision_v6_from_plan.py",
        "production_probability_v6_live_route_contract.py",
        "scripts/prepare_next_day_production_probability_v6.py",
        "physical_state_bayes_v6/artifact_contracts.py",
        "physical_state_bayes_v6/forcing_adapter.py",
        "physical_state_bayes_v6/observation_adapter.py",
        "physical_state_bayes_v6/observation_model.py",
        "physical_state_bayes_v6/transition_model.py",
        "scripts/activate_physical_state_bayes_v6_schema.py",
    )
    missing = [path for path in required if not (root / path).is_file()]
    schema_plan = build_forward_plan(db)
    candidate_activation = root / "config/production_probability_runtime_activation.v6_candidate.json"
    active_activation_path = root / "config/production_probability_runtime_activation.json"
    transition_artifact = root / "config/physical_state_bayes_v6_transition_artifact.json"
    observation_artifact = root / "config/physical_state_bayes_v6_observation_artifact.json"
    blockers = [f"required_file_missing:{path}" for path in missing]
    runtime_closure = audit_v6_runtime_manifest_dependency_closure(root)
    if runtime_closure.get("ok") is not True:
        blockers.append("v6_runtime_dependency_closure_incomplete")
    live_routes = audit_v6_live_routes(root)
    if live_routes.get("ok") is not True:
        blockers.append("v6_live_routes_not_active")
    if not schema_plan.get("ok"):
        blockers.extend(str(value) for value in schema_plan.get("block_reasons") or [])
    if schema_plan.get("actions"):
        blockers.append("v6_additive_schema_not_applied")
    transition_value = None
    observation_value = None
    activation_value = None
    active_activation_value = None
    if not transition_artifact.is_file():
        blockers.append("v6_transition_artifact_missing")
    else:
        try:
            transition_value = validate_transition_artifact(
                json.loads(transition_artifact.read_text(encoding="utf-8"))
            )
            if transition_value.get("production_runtime_eligible") is not True:
                blockers.append("v6_transition_artifact_not_production_approved")
        except (OSError, ValueError, json.JSONDecodeError):
            blockers.append("v6_transition_artifact_invalid")
    if not observation_artifact.is_file():
        blockers.append("v6_observation_artifact_missing")
    else:
        try:
            observation_value = validate_observation_artifact(
                json.loads(observation_artifact.read_text(encoding="utf-8"))
            )
            if observation_value.get("production_runtime_eligible") is not True:
                blockers.append("v6_observation_artifact_not_production_approved")
        except (OSError, ValueError, json.JSONDecodeError):
            blockers.append("v6_observation_artifact_invalid")
    if not candidate_activation.is_file():
        blockers.append("v6_candidate_activation_missing")
    else:
        try:
            activation_value = validate_v6_activation(
                json.loads(candidate_activation.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, json.JSONDecodeError):
            blockers.append("v6_candidate_activation_invalid")
    identifiable_physical = bool(
        activation_value
        and activation_value.get("composer_schema") == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    )
    if identifiable_physical:
        physical_artifacts = (
            (root / "config/artifacts/identifiable_physical_state_model_v1.json", validate_state_model_artifact, "physical_state_model_artifact_sha256"),
            (root / "config/artifacts/identifiable_physical_observation_v1.json", validate_physical_observation_artifact, "physical_observation_artifact_sha256"),
            (root / "config/artifacts/amsc_frozen_reporting_operator_v1.json", validate_reporting_operator_artifact, "physical_reporting_operator_sha256"),
        )
        for artifact_path, validator, activation_field in physical_artifacts:
            try:
                artifact = validator(json.loads(artifact_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError):
                blockers.append(f"v6_{activation_field}_invalid")
                continue
            if activation_value.get(activation_field) != artifact.get("artifact_sha256"):
                blockers.append(f"v6_{activation_field}_activation_mismatch")
    else:
        if activation_value and transition_value and (
            activation_value.get("transition_artifact_sha256")
            != transition_value.get("artifact_sha256")
        ):
            blockers.append("v6_transition_artifact_activation_mismatch")
        if activation_value and observation_value and (
            activation_value.get("observation_artifact_sha256")
            != observation_value.get("artifact_sha256")
        ):
            blockers.append("v6_observation_artifact_activation_mismatch")
    if activation_value and activation_value.get("runtime_manifest_sha256") != v6_runtime_manifest_sha256(root):
        blockers.append("v6_runtime_manifest_activation_mismatch")
    try:
        active_activation_value = validate_v6_activation(
            json.loads(active_activation_path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, json.JSONDecodeError):
        blockers.append("v6_active_activation_not_installed")
    if not cutover_candidate_pre_rehearsal and activation_value and active_activation_value and (
        active_activation_value.get("activation_contract_sha256")
        != activation_value.get("activation_contract_sha256")
    ):
        blockers.append("v6_active_activation_candidate_mismatch")
    replay_receipt = root / "output/audits/physical_state_bayes_v6_canonical_replay.json"
    zero_order_receipt = root / "output/audits/physical_state_bayes_v6_zero_order.json"
    host_rehearsal_receipt = root / "output/audits/physical_state_bayes_v6_host_rehearsal.json"
    for receipt, missing_reason, invalid_reason in (() if cutover_candidate_pre_rehearsal else (
        (replay_receipt, "v6_canonical_replay_receipt_missing", "v6_canonical_replay_receipt_invalid"),
        (zero_order_receipt, "v6_zero_order_receipt_missing", "v6_zero_order_receipt_invalid"),
        (host_rehearsal_receipt, "v6_host_rehearsal_receipt_missing", "v6_host_rehearsal_receipt_invalid"),
    )):
        if not receipt.is_file():
            blockers.append(missing_reason)
            continue
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            blockers.append(invalid_reason)
            continue
        identity_mismatch = (
            value.get("ok") is not True
            or value.get("places_orders") is not False
            or not activation_value
            or value.get("activation_contract_sha256")
            != activation_value.get("activation_contract_sha256")
            or value.get("runtime_manifest_sha256")
            != activation_value.get("runtime_manifest_sha256")
        )
        if not identity_mismatch and identifiable_physical:
            identity_mismatch = any(
                value.get(field) != activation_value.get(field)
                for field in (
                    "physical_state_model_artifact_sha256",
                    "physical_observation_artifact_sha256",
                    "physical_reporting_operator_sha256",
                )
            )
        elif not identity_mismatch:
            identity_mismatch = (
                value.get("transition_artifact_sha256")
                != activation_value.get("transition_artifact_sha256")
                or value.get("observation_artifact_sha256")
                != activation_value.get("observation_artifact_sha256")
            )
        if identity_mismatch:
            blockers.append(invalid_reason)
    production_v6_active = bool(
        active_activation_value
        and activation_value
        and active_activation_value.get("activation_contract_sha256")
        == activation_value.get("activation_contract_sha256")
    )
    previous_v6_active = bool(active_activation_value and not production_v6_active)
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": (
            "ready_for_candidate_rehearsal"
            if not blockers and cutover_candidate_pre_rehearsal
            else
            "production_v6_live_ready"
            if not blockers and production_v6_active
            else "ready_for_controlled_cutover"
            if not blockers
            else "not_ready"
        ),
        "blockers": blockers,
        "cutover_candidate_pre_rehearsal": cutover_candidate_pre_rehearsal,
        "schema_plan": {
            "ok": schema_plan.get("ok"),
            "state": schema_plan.get("state"),
            "pending_action_count": len(schema_plan.get("actions") or []),
        },
        "runtime_dependency_closure": runtime_closure,
        "live_routes": live_routes,
        "production_v6_active": production_v6_active,
        "previous_v6_activation_remains_authoritative_until_replacement": previous_v6_active,
        "production_v5_remains_authoritative_until_cutover": not active_activation_value,
        "v5_fallback_after_v6_activation": False,
        "read_only": True,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--cutover-candidate-pre-rehearsal", action="store_true")
    args = parser.parse_args()
    result = audit(
        ROOT,
        Path(args.db).expanduser().resolve(),
        cutover_candidate_pre_rehearsal=args.cutover_candidate_pre_rehearsal,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
