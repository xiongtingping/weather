"""Fit a shadow-only strong-observation LR candidate from frozen evidence.

The fitter implements a deliberately simple, auditable conditional-frequency
model: for each runtime feature category it estimates a smoothed
``P(feature_category | final_temperature_cell, Z_pre)`` surface.  The runtime
composer centres these raw log scores; this module never creates a posterior.
It also estimates only the physical latent ``Z_pre -> Z_post`` transition.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from physical_latent_state_contract import LATENT_STATES
from strong_observation_feature_contract import (
    build_strong_observation_feature_definitions as _feature_definitions,
)
from strong_observation_lr_artifact_contract import (
    FEATURE_EVALUATOR_SCHEMA_VERSION,
    REQUIRED_FEATURE_IDS,
    SCHEMA_VERSION as ARTIFACT_SCHEMA,
    validate_strong_observation_lr_runtime_artifact,
)
from strong_observation_lr_evaluator import (
    _category_for_feature,
    derive_candidate_dependent_feature_values,
)
from strong_observation_training_dataset_runtime import (
    MANIFEST_SCHEMA_VERSION,
    SCHEMA_VERSION as DATASET_SCHEMA,
    TRAINING_DATA_POLICY,
)
from strong_observation_training_feature_runtime import TRAINING_SCOPE_PHYSICAL_X, TRAINING_SCOPE_SETTLEMENT, TRAINING_SCOPES
from temperature_trajectory_feature_runtime import OWNER_ID as TRAJECTORY_OWNER_ID


SCHEMA_VERSION = "strong_observation_lr_fit_v1"
FIT_POLICY = "strict_asof_conditional_feature_frequency_shadow_v1"
def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _nearest_grid(value: float, grid: Sequence[float], tolerance: float) -> float | None:
    candidate = min(grid, key=lambda item: abs(item - value))
    return candidate if abs(candidate - value) <= tolerance else None


def _blocked(*reasons: str, diagnostics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(item for item in reasons if item)), "diagnostics": dict(diagnostics or {}), "read_only": True, "shadow_only": True, "places_orders": False}


def fit_strong_observation_lr_shadow_candidate(
    *,
    training_dataset: Mapping[str, Any],
    latent_transition_labels: Sequence[Mapping[str, Any]],
    temperature_grid_c: Sequence[Any],
    laplace_alpha: float = 1.0,
    temperature_grid_tolerance_c: float = 0.26,
) -> dict[str, Any]:
    """Fit one unapproved candidate; no registry, posterior, or order writes."""

    dataset = _mapping(training_dataset)
    manifest = _mapping(dataset.get("training_manifest"))
    training_scope = _text(manifest.get("training_scope"))
    grid = sorted({_finite(item) for item in temperature_grid_c if _finite(item) is not None})
    alpha = _finite(laplace_alpha)
    tolerance = _finite(temperature_grid_tolerance_c)
    issues: list[str] = []
    if dataset.get("schema") != DATASET_SCHEMA or dataset.get("ok") is not True or dataset.get("state") != "ready_for_oos":
        issues.append("strong_lr_fit_training_dataset_not_oos_ready")
    if manifest.get("schema") != MANIFEST_SCHEMA_VERSION or _text(dataset.get("training_manifest_sha256")) == "":
        issues.append("strong_lr_fit_training_manifest_invalid")
    if training_scope not in TRAINING_SCOPES:
        issues.append("strong_lr_fit_training_scope_invalid")
    if not grid or any(item is None for item in grid) or len(grid) < 2:
        issues.append("strong_lr_fit_temperature_grid_invalid")
    if alpha is None or alpha <= 0.0:
        issues.append("strong_lr_fit_laplace_alpha_invalid")
    if tolerance is None or tolerance <= 0.0:
        issues.append("strong_lr_fit_temperature_grid_tolerance_invalid")
    rows = [_mapping(row) for row in dataset.get("rows") or []]
    labels_by_sample: dict[str, dict[str, Any]] = {}
    for item in latent_transition_labels:
        row = _mapping(item)
        sample_id = _text(row.get("training_sample_id"))
        pre, post = _text(row.get("z_pre")), _text(row.get("z_post"))
        if not sample_id or pre not in LATENT_STATES or post not in LATENT_STATES or not _text(row.get("latent_replay_sha256")):
            issues.append("strong_lr_fit_latent_transition_label_invalid")
            continue
        if _text(row.get("training_scope")) != training_scope:
            issues.append("strong_lr_fit_latent_transition_training_scope_mismatch")
            continue
        if sample_id in labels_by_sample:
            issues.append("strong_lr_fit_duplicate_latent_transition_label")
            continue
        labels_by_sample[sample_id] = row
    expected_ids = {_text(row.get("training_sample_id")) for row in rows}
    if set(labels_by_sample) != expected_ids:
        issues.append("strong_lr_fit_latent_transition_label_set_mismatch")
    if issues:
        return _blocked(*issues, diagnostics={"dataset_row_count": len(rows), "latent_label_count": len(labels_by_sample)})

    definitions = _feature_definitions()
    # category_count[feature][category][temperature|z_pre] and denominator
    # are fit only from frozen supervised labels.  Labels never enter the
    # emitted runtime feature definition.
    category_counts: dict[str, dict[str, dict[str, float]]] = {feature: defaultdict(lambda: defaultdict(float)) for feature in REQUIRED_FEATURE_IDS}
    denominators: dict[str, float] = defaultdict(float)
    transition_counts: dict[str, dict[str, float]] = {pre: defaultdict(float) for pre in LATENT_STATES}
    fitted_rows: list[dict[str, Any]] = []
    for row in rows:
        sample_id = _text(row.get("training_sample_id"))
        target = _finite(_mapping(row.get("labels")).get("final_physical_high_c"))
        weight = _finite(row.get("day_weight"))
        transition = labels_by_sample[sample_id]
        if target is None or weight is None or weight <= 0.0:
            return _blocked("strong_lr_fit_training_row_target_or_weight_invalid", diagnostics={"training_sample_id": sample_id})
        grid_temperature = _nearest_grid(target, grid, float(tolerance))
        if grid_temperature is None:
            return _blocked("strong_lr_fit_final_physical_label_off_temperature_grid", diagnostics={"training_sample_id": sample_id, "final_physical_high_c": target})
        pre, post = _text(transition["z_pre"]), _text(transition["z_post"])
        cell = f"{grid_temperature:g}|{pre}"
        denominators[cell] += weight
        transition_counts[pre][post] += weight
        feature_values = derive_candidate_dependent_feature_values(
            _mapping(row.get("feature_values")),
            candidate_temperature_c=grid_temperature,
        )
        categories: dict[str, str] = {}
        for feature_id in REQUIRED_FEATURE_IDS:
            category, reason = _category_for_feature(definitions[feature_id], feature_values.get(feature_id))
            if reason:
                return _blocked("strong_lr_fit_runtime_feature_category_unresolved", diagnostics={"training_sample_id": sample_id, "feature_id": feature_id, "reason": reason})
            categories[feature_id] = category
            category_counts[feature_id][category][cell] += weight
        fitted_rows.append({"training_sample_id": sample_id, "temperature_cell_c": grid_temperature, "z_pre": pre, "z_post": post, "day_weight": weight, "categories": categories, "latent_replay_sha256": _text(transition["latent_replay_sha256"])})

    for feature_id, definition in definitions.items():
        adjustments = _mapping(definition.get("value_score_adjustments"))
        category_count = max(1, len(adjustments))
        for category, adjustment in adjustments.items():
            surface = _mapping(_mapping(adjustment).get("raw_log_score_adjustment_by_temperature_and_pre_state"))
            # A finite, conservative default preserves evaluator totality for
            # unseen cells; exact fitted cells remain auditable below.
            surface["default"] = math.log(1.0 / category_count)
            for cell, denominator in denominators.items():
                probability = (category_counts[feature_id][category].get(cell, 0.0) + float(alpha)) / (denominator + float(alpha) * category_count)
                surface[cell] = math.log(probability)
            adjustment["raw_log_score_adjustment_by_temperature_and_pre_state"] = surface

    transition_matrix: dict[str, dict[str, float]] = {}
    for pre in LATENT_STATES:
        total = sum(transition_counts[pre].values())
        transition_matrix[pre] = {post: (transition_counts[pre].get(post, 0.0) + float(alpha)) / (total + float(alpha) * len(LATENT_STATES)) for post in LATENT_STATES}

    fit_lineage = {
        "fit_policy": FIT_POLICY,
        "training_scope": training_scope,
        "training_manifest_sha256": _text(dataset.get("training_manifest_sha256")),
        "temperature_grid_c": grid,
        "laplace_alpha": alpha,
        "temperature_grid_tolerance_c": tolerance,
        "fitted_rows": fitted_rows,
    }
    artifact: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "approval_state": "shadow_only",
        "feature_builder_version": SCHEMA_VERSION,
        "training_manifest_sha256": _text(dataset.get("training_manifest_sha256")),
        "training_data_policy": TRAINING_DATA_POLICY,
        "training_scope": training_scope,
        "input_contract": {"trajectory_owner_id": TRAJECTORY_OWNER_ID, "d0_source_reliability_is_pre_state_only": True, "canonical_observation_is_only_fact_source": True},
        "runtime_contract": {"output": "raw_log_score_and_post_state_transition_only", "composer_owns_normalizer": True, "composer_owns_posterior_delta": True, "composer_owns_hard_condition": True, "same_event_cannot_update_its_pre_state": True, "creates_independent_posterior": False, "creates_strategy_signal": False},
        "default_raw_log_score": 0.0,
        "post_state_transition": transition_matrix,
        "feature_evaluator": {"schema": FEATURE_EVALUATOR_SCHEMA_VERSION, "required_feature_ids": list(REQUIRED_FEATURE_IDS), "feature_definitions": definitions},
        "fit_lineage": fit_lineage,
        "shadow_only": True,
    }
    immutable = {key: value for key, value in artifact.items() if key not in {"artifact_sha256", "model_artifact_sha256"}}
    artifact_sha = _sha256(immutable)
    artifact["artifact_content_sha256"] = artifact_sha
    artifact["artifact_sha256"] = artifact_sha
    artifact["model_artifact_sha256"] = artifact_sha
    validation = validate_strong_observation_lr_runtime_artifact(artifact)
    if validation.get("ok") is not True:
        return _blocked("strong_lr_fit_emitted_artifact_runtime_contract_invalid", diagnostics={"issues": validation.get("issues") or []})
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "shadow_candidate_ready", "candidate_artifact": artifact, "candidate_artifact_sha256": artifact_sha, "fit_lineage_sha256": _sha256(fit_lineage), "training_row_count": len(fitted_rows), "read_only": True, "shadow_only": True, "boundary": {"training_scope": training_scope, "physical_x_scope_does_not_require_settlement_or_bridge": training_scope == TRAINING_SCOPE_PHYSICAL_X, "strict_dataset_only": True, "latent_transition_is_physical_state_only": True, "source_reliability_post_state_not_fitted": True, "activates_artifact": False, "writes_artifact_registry": False, "creates_posterior": False, "places_orders": False}}


__all__ = ["FIT_POLICY", "LATENT_STATES", "SCHEMA_VERSION", "fit_strong_observation_lr_shadow_candidate"]
