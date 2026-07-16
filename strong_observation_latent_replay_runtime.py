"""Freeze physical latent replay labels for strong-LR fitting.

This is a contract bridge, not a rule-based state classifier.  A caller must
provide results from an ordered, final-label-bound physical replay; this module
only validates exact lineage and turns them into trainer inputs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from physical_latent_state_contract import LATENT_STATES
from trajectory_label_sample_runtime import INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION
from strong_observation_training_feature_runtime import (
    TRAINING_SCOPE_PHYSICAL_X,
    TRAINING_SCOPE_SETTLEMENT,
    TRAINING_SCOPES,
)


SCHEMA_VERSION = "strong_observation_latent_replay_label_batch_v1"
ROW_SCHEMA_VERSION = "strong_observation_latent_replay_label_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _forbidden_surface_keys(value: Any) -> bool:
    forbidden = ("market", "price", "order", "position", "pnl", "edge", "lcb", "source_reliability_after")
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = _text(key).lower()
            # Lineage names such as ``ordered_replay_sha256`` are not orders;
            # only an actual forbidden surface key is rejected.
            if name in forbidden:
                return True
            if _forbidden_surface_keys(child):
                return True
    elif isinstance(value, list):
        return any(_forbidden_surface_keys(item) for item in value)
    return False


def _sample_scope_eligible(sample: Mapping[str, Any], training_scope: str) -> bool:
    if training_scope == TRAINING_SCOPE_SETTLEMENT:
        return sample.get("quality_grade") == "A" and sample.get("settlement_evaluation_eligible") is True
    if training_scope == TRAINING_SCOPE_PHYSICAL_X:
        return sample.get("quality_grade") in {"A", "B"} and sample.get("physical_auxiliary_eligible") is True
    return False


def build_strong_observation_latent_replay_labels(
    *,
    training_samples: Sequence[Mapping[str, Any]],
    ordered_replay_rows: Sequence[Mapping[str, Any]],
    training_scope: str = TRAINING_SCOPE_SETTLEMENT,
) -> dict[str, Any]:
    """Return physical replay labels for exact frozen rows in one training scope."""

    scope = _text(training_scope)
    samples = {_text(row.get("training_sample_id")): _mapping(row) for row in training_samples if isinstance(row, Mapping)}
    rows_by_id: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for raw in ordered_replay_rows:
        row = _mapping(raw)
        sample_id = _text(row.get("training_sample_id"))
        if sample_id in rows_by_id:
            issues.append("strong_latent_replay_duplicate_training_sample_id")
            continue
        rows_by_id[sample_id] = row
    if set(rows_by_id) != set(samples):
        issues.append("strong_latent_replay_training_sample_set_mismatch")
    labels: list[dict[str, Any]] = []
    if scope not in TRAINING_SCOPES:
        issues.append("strong_latent_replay_training_scope_invalid")
    for sample_id in sorted(samples):
        sample, row = samples[sample_id], rows_by_id.get(sample_id, {})
        if _text(sample.get("schema")) != INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION or not _sample_scope_eligible(sample, scope):
            issues.append(
                "strong_latent_replay_training_sample_not_physical_x_eligible"
                if scope == TRAINING_SCOPE_PHYSICAL_X
                else "strong_latent_replay_training_sample_not_a_class"
            )
            continue
        if _text(row.get("schema")) != ROW_SCHEMA_VERSION:
            issues.append("strong_latent_replay_row_schema_invalid")
        for key in ("target_identity", "event_date", "canonical_observation_event_id", "canonical_sequence_sha256"):
            if _text(row.get(key)) != _text(sample.get(key)):
                issues.append(f"strong_latent_replay_{key}_binding_mismatch")
        labels_surface = _mapping(sample.get("labels"))
        expected_label_sha = _sha256(labels_surface)
        if _text(row.get("final_label_sha256")) != expected_label_sha:
            issues.append("strong_latent_replay_final_label_binding_mismatch")
        if _text(row.get("replay_ordering")) != "canonical_valid_time_then_first_actionable_seen_at":
            issues.append("strong_latent_replay_ordering_invalid")
        pre, post = _text(row.get("z_pre")), _text(row.get("z_post"))
        if pre not in LATENT_STATES or post not in LATENT_STATES:
            issues.append("strong_latent_replay_physical_state_invalid")
        if not _text(row.get("ordered_replay_sha256")):
            issues.append("strong_latent_replay_sha256_missing")
        if _forbidden_surface_keys(row):
            issues.append("strong_latent_replay_forbidden_nonphysical_surface")
        labels.append({"training_sample_id": sample_id, "training_scope": scope, "z_pre": pre, "z_post": post, "latent_replay_sha256": _text(row.get("ordered_replay_sha256"))})
    if issues:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(issues)), "read_only": True, "shadow_only": True, "places_orders": False}
    core = {"schema": SCHEMA_VERSION, "training_scope": scope, "labels": labels, "training_sample_ids": sorted(samples)}
    return {**core, "latent_replay_label_batch_sha256": _sha256(core), "ok": True, "state": "available", "read_only": True, "shadow_only": True, "boundary": {"training_scope": scope, "physical_x_scope_does_not_require_settlement_or_bridge": scope == TRAINING_SCOPE_PHYSICAL_X, "physical_latent_state_only": True, "final_label_is_replay_target_not_feature": True, "writes_posterior": False, "places_orders": False}}


__all__ = ["ROW_SCHEMA_VERSION", "SCHEMA_VERSION", "build_strong_observation_latent_replay_labels"]
