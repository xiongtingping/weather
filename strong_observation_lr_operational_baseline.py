"""Governed zero-information strong-LR baseline for runtime continuity.

This artifact is intentionally neutral: every feature adjustment is zero and
latent-state transitions are identity.  It therefore contributes no invented
meteorological skill.  The composer still applies the non-negotiable observed
maximum hard condition, while a trained/OOS-approved candidate is accumulated
offline and may later replace this baseline through the registry.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from strong_observation_lr_artifact_contract import (
    FEATURE_EVALUATOR_SCHEMA_VERSION,
    REQUIRED_FEATURE_IDS,
    SCHEMA_VERSION as ARTIFACT_SCHEMA_VERSION,
    validate_strong_observation_lr_runtime_artifact,
)
from physical_latent_state_contract import LATENT_STATES
from strong_observation_feature_contract import build_strong_observation_feature_definitions
from temperature_trajectory_feature_runtime import OWNER_ID as TRAJECTORY_OWNER_ID


SCHEMA_VERSION = "strong_observation_lr_neutral_operational_baseline_v1"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def build_neutral_strong_lr_operational_baseline() -> dict[str, Any]:
    """Return a valid approved-runtime baseline without training claims."""

    core: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA_VERSION,
        "approval_state": "approved",
        "approval_kind": "operational_precalibration_baseline",
        "feature_builder_version": SCHEMA_VERSION,
        "training_data_policy": "no_training_neutral_operational_baseline",
        "training_scope": "physical_x",
        "input_contract": {
            "trajectory_owner_id": TRAJECTORY_OWNER_ID,
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
        "default_raw_log_score": 0.0,
        "post_state_transition": {
            state: {state: 1.0}
            for state in LATENT_STATES
        },
        "feature_evaluator": {
            "schema": FEATURE_EVALUATOR_SCHEMA_VERSION,
            "required_feature_ids": list(REQUIRED_FEATURE_IDS),
            "feature_definitions": build_strong_observation_feature_definitions(),
        },
        "baseline_contract": {
            "not_trained_or_oos_promoted": True,
            "all_raw_log_score_adjustments_are_zero": True,
            "latent_transition_is_identity": True,
            "hard_observed_max_condition_remains_composer_owned": True,
            "strict_asof_oos_is_replacement_promotion_only": True,
            "replacement_policy": "only_registry_promoted_candidate_may_replace",
        },
        "shadow_only": True,
    }
    artifact_sha = _sha(core)
    artifact = {
        **core,
        "artifact_content_sha256": artifact_sha,
        "artifact_sha256": artifact_sha,
        "model_artifact_sha256": artifact_sha,
    }
    contract = validate_strong_observation_lr_runtime_artifact(artifact)
    if contract.get("ok") is not True:
        raise ValueError("neutral_strong_lr_operational_baseline_invalid:" + ",".join(contract.get("issues") or []))
    return artifact


__all__ = ["SCHEMA_VERSION", "build_neutral_strong_lr_operational_baseline"]
