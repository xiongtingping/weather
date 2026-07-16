"""Auditable production baseline for event-level strong-observation LR.

This is a conservative, pre-calibration physical model rather than a trained
artifact.  It scores the *innovation* in each canonical METAR/SPECI against a
candidate final physical maximum X using pre-event trajectory, current rate,
time remaining, cloud/rain and dewpoint evidence.  Its coefficients are
immutable and deliberately low-information; strict-as-of/OOS governs later
replacement, never whether the approved baseline can run live.
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


SCHEMA_VERSION = "strong_observation_lr_operational_physics_baseline_v1"
SCORING_KIND = "operational_physical_innovation_lr_v1"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def build_operational_physics_strong_lr_baseline() -> dict[str, Any]:
    core: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA_VERSION,
        "approval_state": "approved",
        "approval_kind": "operational_precalibration_baseline",
        "feature_builder_version": SCHEMA_VERSION,
        "training_data_policy": "no_training_governed_physical_baseline",
        "training_scope": "physical_x",
        "scoring_kind": SCORING_KIND,
        "production_runtime_eligible": True,
        "shadow_only": False,
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
        "post_state_transition": {state: {state: 1.0} for state in LATENT_STATES},
        "feature_evaluator": {
            "schema": FEATURE_EVALUATOR_SCHEMA_VERSION,
            "required_feature_ids": list(REQUIRED_FEATURE_IDS),
            "feature_definitions": build_strong_observation_feature_definitions(),
        },
        "operational_physics_parameters": {
            "reference_peak_local_hour": 15.0,
            "minimum_hours_to_peak": 0.5,
            "rate_sigma_c_per_hour": 1.25,
            "event_information_scale": 0.35,
            "no_history_information_scale": 0.25,
            "maximum_relative_log_penalty": 2.5,
            "base_clear_heating_capacity_c_per_hour": 1.1,
            "cloud_capacity_multiplier": {
                "clear_or_few": 1.0,
                "scattered": 0.8,
                "broken": 0.5,
                "overcast_or_obscured": 0.25,
                "__missing__": 0.65,
                "__other__": 0.65
            },
            "weather_capacity_multiplier": {
                "none": 1.0,
                "visibility_reduction": 0.65,
                "precipitation": 0.3,
                "thunderstorm": 0.2,
                "__missing__": 0.8,
                "__other__": 0.7
            },
            "dewpoint_capacity_multiplier": {
                "dry": 1.05,
                "moderate": 1.0,
                "humid": 0.8,
                "saturated": 0.55,
                "__missing__": 0.8
            },
            "remaining_capacity_soft_margin_c": 0.75,
            "remaining_capacity_excess_penalty": 0.22,
            "cooling_depth_penalty_per_c": 0.16,
            "cooling_duration_penalty_per_hour": 0.12,
            "post_peak_remaining_penalty_per_c": 0.28,
        },
        "baseline_contract": {
            "not_trained_or_oos_promoted": True,
            "strict_asof_oos_is_replacement_promotion_only": True,
            "low_information_conservative": True,
            "uses_current_event_once": True,
            "uses_pre_event_trajectory_only": True,
            "termination_risk_is_output_marginal_not_input": True,
            "replacement_policy": "only_registry_promoted_candidate_may_replace_whole_artifact",
        },
    }
    artifact_sha = _sha(core)
    artifact = {
        **core,
        "artifact_content_sha256": artifact_sha,
        "artifact_sha256": artifact_sha,
        "model_artifact_sha256": artifact_sha,
    }
    validation = validate_strong_observation_lr_runtime_artifact(artifact)
    if validation.get("ok") is not True:
        raise ValueError(
            "operational_physics_strong_lr_baseline_invalid:"
            + ",".join(validation.get("issues") or [])
        )
    return artifact


__all__ = ["SCHEMA_VERSION", "SCORING_KIND", "build_operational_physics_strong_lr_baseline"]
