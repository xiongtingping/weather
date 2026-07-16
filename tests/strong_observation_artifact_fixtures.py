"""Fixtures for the v3 strong-observation artifact contract only.

The values are intentionally synthetic test fixtures.  They are not a model
artifact and cannot be referenced by a resident prepared cycle outside tests.
"""

from __future__ import annotations

from typing import Any, Mapping
import hashlib
import json

from strong_observation_lr_artifact_contract import (
    FEATURE_EVALUATOR_SCHEMA_VERSION,
    REQUIRED_FEATURE_IDS,
    SCHEMA_VERSION,
)
from strong_observation_lr_fit_runtime import _feature_definitions
from strong_observation_lr_runtime_registry import ACTIVATION_ATTESTATION_SCHEMA_VERSION


def strong_activation_attestation(
    *, target_identity: str, artifact_sha256: str, activation_basis: str, activation_ref: str
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": ACTIVATION_ATTESTATION_SCHEMA_VERSION,
        "target_identity": target_identity,
        "artifact_sha256": artifact_sha256,
        "activation_basis": activation_basis,
        "activation_ref": activation_ref,
        "attested_at": "2026-07-03T04:20:00Z",
        "attested_by": "pytest",
    }
    if activation_basis == "promoted_candidate":
        gate = {
            "schema": "strong_observation_artifact_promotion_gate_v1",
            "ok": True,
            "state": "eligible_for_registry_activation",
            "candidate_artifact_sha256": artifact_sha256,
            "training_manifest_sha256": "test-training-manifest",
            "oos_evaluation_sha256": "test-oos-evaluation",
            "feature_ablation_sha256": "test-feature-ablation",
            "block_reasons": [],
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
        gate["promotion_gate_sha256"] = hashlib.sha256(
            json.dumps(gate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        value["promotion_gate"] = gate
    else:
        value["baseline_approval"] = {
            "approval_kind": "operational_precalibration_baseline",
            "approval_state": "approved",
            "not_trained_or_oos_promoted": True,
            "risk_and_scope_statement": "pytest isolated runtime baseline only",
        }
    return value


def _surface(default: float = 0.0, **cells: float) -> dict[str, float]:
    return {"default": default, **cells}


def _adjustment(default: float = 0.0, **cells: float) -> dict[str, Any]:
    return {"raw_log_score_adjustment_by_temperature_and_pre_state": _surface(default, **cells)}


def _categorical(*labels: str) -> dict[str, Any]:
    values = {label: _adjustment() for label in labels}
    values["__missing__"] = _adjustment()
    values["__other__"] = _adjustment()
    return {"kind": "categorical", "value_score_adjustments": values}


def _numeric(bins: list[dict[str, Any]]) -> dict[str, Any]:
    values = {str(item["label"]): _adjustment() for item in bins}
    values["__missing__"] = _adjustment()
    return {"kind": "numeric_binned", "bins": bins, "value_score_adjustments": values}


def approved_strong_artifact(
    *,
    model_artifact_sha256: str = "strong-artifact-test",
    raw_log_score_by_temperature_and_pre_state: Mapping[str, float] | None = None,
    raw_log_score_by_pre_state: Mapping[str, float] | None = None,
    default_raw_log_score: float | None = None,
    post_state_transition: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "approval_state": "approved",
        "training_manifest_sha256": "test-training-manifest",
        "oos_evaluation_sha256": "test-oos-evaluation",
        "feature_builder_version": "test-feature-builder-v2",
        "training_data_policy": "strict_asof_canonical_event_v2",
        "input_contract": {
            "trajectory_owner_id": "canonical_temperature_trajectory_owner_v1",
            "d0_source_reliability_is_pre_state_only": True,
            "canonical_observation_is_only_fact_source": True,
        },
        "runtime_contract": {
            "output": "raw_log_score_and_post_state_transition_only",
            "composer_owns_normalizer": True,
            "composer_owns_posterior_delta": True,
            "composer_owns_hard_condition": True,
            "same_event_cannot_update_its_pre_state": True,
            "creates_independent_posterior": False,
            "creates_strategy_signal": False,
        },
        "post_state_transition": dict(post_state_transition or {"warming": {"warming": 1.0}}),
        "feature_evaluator": {
            "schema": FEATURE_EVALUATOR_SCHEMA_VERSION,
            "training_feature_manifest_sha256": "test-feature-manifest",
            "oos_feature_ablation_sha256": "test-feature-ablation",
            "required_feature_ids": list(REQUIRED_FEATURE_IDS),
            "feature_definitions": _feature_definitions(),
        },
    }
    if raw_log_score_by_temperature_and_pre_state is not None:
        artifact["raw_log_score_by_temperature_and_pre_state"] = dict(raw_log_score_by_temperature_and_pre_state)
    if raw_log_score_by_pre_state is not None:
        artifact["raw_log_score_by_pre_state"] = dict(raw_log_score_by_pre_state)
    if default_raw_log_score is not None:
        artifact["default_raw_log_score"] = default_raw_log_score
    if not any(key in artifact for key in ("raw_log_score_by_temperature_and_pre_state", "raw_log_score_by_pre_state", "default_raw_log_score")):
        artifact["default_raw_log_score"] = 0.0
    artifact["artifact_sha256"] = model_artifact_sha256
    artifact["model_artifact_sha256"] = model_artifact_sha256
    return artifact
