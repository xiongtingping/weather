"""Offline-only release gate for a trained strong-observation LR artifact.

The runtime structural validator intentionally ignores strict-as-of/OOS so a
previously activated trading chain is not stopped by research metadata.  This
separate module is the only place that may declare a *new* artifact eligible
for activation: it binds the exact frozen dataset, recomputed OOS evaluation,
and all-feature ablation proof to the artifact's immutable hashes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from strong_observation_feature_ablation_runtime import SCHEMA_VERSION as ABLATION_SCHEMA_VERSION
from strong_observation_lr_artifact_contract import validate_strong_observation_lr_runtime_artifact
from strong_observation_oos_evaluation_runtime import SCHEMA_VERSION as OOS_SCHEMA_VERSION
from strong_observation_training_dataset_runtime import (
    MANIFEST_SCHEMA_VERSION,
    TRAINING_DATA_POLICY,
)
from strong_observation_training_feature_runtime import TRAINING_SCOPE_SETTLEMENT


SCHEMA_VERSION = "strong_observation_artifact_promotion_gate_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_strong_observation_artifact_promotion_gate(
    *,
    candidate_artifact: Mapping[str, Any],
    training_dataset: Mapping[str, Any],
    oos_evaluation: Mapping[str, Any],
    feature_ablation_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate immutable offline evidence before a new artifact is activated.

    It returns a declaration only; artifact registry writes, runtime reloads,
    launchd restarts and every kind of order are deliberately outside scope.
    """

    artifact = _mapping(candidate_artifact)
    dataset = _mapping(training_dataset)
    manifest = _mapping(dataset.get("training_manifest"))
    oos = _mapping(oos_evaluation)
    ablation = _mapping(feature_ablation_evidence)
    issues: list[str] = []
    runtime = validate_strong_observation_lr_runtime_artifact(artifact)
    if runtime.get("ok") is not True:
        issues.extend(f"promotion_runtime_artifact_contract:{item}" for item in runtime.get("issues") or ["invalid"])
    artifact_sha = _text(artifact.get("model_artifact_sha256"))
    manifest_sha = _text(dataset.get("training_manifest_sha256"))
    if dataset.get("ok") is not True or _text(dataset.get("state")) != "ready_for_oos":
        issues.append("promotion_training_dataset_not_oos_ready")
    if _text(manifest.get("schema")) != MANIFEST_SCHEMA_VERSION or _text(manifest.get("training_data_policy")) != TRAINING_DATA_POLICY:
        issues.append("promotion_training_manifest_schema_or_policy_invalid")
    if _text(manifest.get("training_scope")) != TRAINING_SCOPE_SETTLEMENT:
        # Physical-X samples are daily correction/shadow evidence.  They do
        # not establish the separate X->S->B settlement calibration required
        # before a candidate may influence real orders.
        issues.append("promotion_training_scope_not_settlement")
    if not manifest_sha:
        issues.append("promotion_training_manifest_sha256_missing")
    if _text(artifact.get("training_manifest_sha256")) != manifest_sha:
        issues.append("promotion_artifact_training_manifest_binding_mismatch")
    if _text(oos.get("schema")) != OOS_SCHEMA_VERSION or oos.get("ok") is not True or oos.get("promotion_eligible") is not True:
        issues.append("promotion_oos_evaluation_not_eligible")
    if _text(oos.get("training_manifest_sha256")) != manifest_sha:
        issues.append("promotion_oos_training_manifest_binding_mismatch")
    if _text(oos.get("candidate_artifact_sha256")) != artifact_sha:
        issues.append("promotion_oos_candidate_artifact_binding_mismatch")
    oos_sha = _text(oos.get("oos_evaluation_sha256"))
    if not oos_sha or _text(artifact.get("oos_evaluation_sha256")) != oos_sha:
        issues.append("promotion_artifact_oos_evaluation_binding_mismatch")
    if _text(ablation.get("schema")) != ABLATION_SCHEMA_VERSION or ablation.get("ok") is not True:
        issues.append("promotion_feature_ablation_invalid")
    if _text(ablation.get("training_manifest_sha256")) != manifest_sha:
        issues.append("promotion_ablation_training_manifest_binding_mismatch")
    if _text(ablation.get("full_candidate_artifact_sha256")) != artifact_sha:
        issues.append("promotion_ablation_candidate_artifact_binding_mismatch")
    if _text(ablation.get("full_oos_evaluation_sha256")) != oos_sha:
        issues.append("promotion_ablation_oos_binding_mismatch")
    ablation_sha = _text(ablation.get("feature_ablation_sha256"))
    evaluator = _mapping(artifact.get("feature_evaluator"))
    if not ablation_sha or _text(evaluator.get("training_feature_manifest_sha256")) != manifest_sha:
        issues.append("promotion_artifact_feature_training_manifest_binding_mismatch")
    if _text(evaluator.get("oos_feature_ablation_sha256")) != ablation_sha:
        issues.append("promotion_artifact_feature_ablation_binding_mismatch")
    if artifact.get("approval_state") != "approved":
        issues.append("promotion_artifact_approval_state_invalid")
    gate = {
        "schema": SCHEMA_VERSION,
        "ok": not issues,
        "state": "eligible_for_registry_activation" if not issues else "blocked",
        "block_reasons": sorted(set(issues)),
        "candidate_artifact_sha256": artifact_sha,
        "training_manifest_sha256": manifest_sha,
        "oos_evaluation_sha256": oos_sha,
        "feature_ablation_sha256": ablation_sha,
        "read_only": True,
        "offline_only": True,
        "boundary": {
            "strict_asof_oos_are_promotion_only": True,
            "writes_artifact_registry": False,
            "reloads_runtime_artifact": False,
            "blocks_existing_live_runtime": False,
            "places_orders": False,
        },
    }
    gate["promotion_gate_sha256"] = _sha(gate)
    return gate


__all__ = ["SCHEMA_VERSION", "build_strong_observation_artifact_promotion_gate"]
