"""Build leakage-safe offline strong-LR features from the runtime feature owner.

Offline training must not recreate a second interpretation of a METAR/SPECI.
This module binds a previously frozen event-level training sample to the exact
``strong_observation_lr_input_v3`` accepted by runtime, then calls the same
deterministic feature extractor as the runtime evaluator.  It emits feature
evidence only: final X/S/B labels remain in the separately frozen training
sample and are never copied into this feature object.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from strong_observation_lr_artifact_contract import (
    REQUIRED_FEATURE_IDS as RUNTIME_REQUIRED_FEATURE_IDS,
    build_strong_observation_lr_input,
)
from strong_observation_lr_evaluator import extract_strong_observation_feature_values
from trajectory_label_sample_runtime import INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION


SCHEMA_VERSION = "strong_observation_training_feature_v1"
FEATURE_OWNER_ID = "strong_observation_runtime_feature_owner_v1"
FEATURE_EXTRACTOR_VERSION = "strong_observation_lr_evaluator_v2"
TRAINING_SCOPE_SETTLEMENT = "settlement"
TRAINING_SCOPE_PHYSICAL_X = "physical_x"
TRAINING_SCOPES = (TRAINING_SCOPE_SETTLEMENT, TRAINING_SCOPE_PHYSICAL_X)
REQUIRED_FEATURE_IDS = RUNTIME_REQUIRED_FEATURE_IDS


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _blocked(*reasons: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "read_only": True,
        "shadow_only": True,
        "places_orders": False,
    }


def _training_scope(value: Any) -> str:
    return _text(value)


def _sample_boundary_issues(sample: Mapping[str, Any], *, training_scope: str) -> list[str]:
    issues: list[str] = []
    if training_scope not in TRAINING_SCOPES:
        issues.append("strong_training_feature_training_scope_invalid")
    if _text(sample.get("schema")) != INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION:
        issues.append("strong_training_feature_sample_schema_invalid")
    if not _text(sample.get("training_sample_id")):
        issues.append("strong_training_feature_sample_id_missing")
    if training_scope == TRAINING_SCOPE_SETTLEMENT:
        if sample.get("quality_grade") != "A" or sample.get("settlement_evaluation_eligible") is not True:
            issues.append("strong_training_feature_sample_not_a_class_settlement_eligible")
    elif training_scope == TRAINING_SCOPE_PHYSICAL_X:
        # Daily physical learning is intentionally independent of market
        # finality, Wunderground S, and the X->S bridge.  It still requires a
        # canonical as-of event and a final physical X label downstream.
        if sample.get("quality_grade") not in {"A", "B"} or sample.get("physical_auxiliary_eligible") is not True:
            issues.append("strong_training_feature_sample_not_physical_x_eligible")
    if not _text(sample.get("target_identity")) or not _text(sample.get("event_date")):
        issues.append("strong_training_feature_sample_identity_missing")
    if not _text(sample.get("canonical_observation_event_id")):
        issues.append("strong_training_feature_sample_canonical_event_missing")
    if not _text(sample.get("cutoff_as_of")):
        issues.append("strong_training_feature_sample_cutoff_missing")
    sequence = sample.get("canonical_sequence_event_ids")
    if not isinstance(sequence, list) or not sequence:
        issues.append("strong_training_feature_sample_sequence_missing")
    elif _text(sequence[-1]) != _text(sample.get("canonical_observation_event_id")):
        issues.append("strong_training_feature_sample_sequence_current_event_mismatch")
    return issues


def _event_binding_issues(sample: Mapping[str, Any], event: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if _text(event.get("canonical_observation_event_id")) != _text(sample.get("canonical_observation_event_id")):
        issues.append("strong_training_feature_event_id_mismatch")
    if _text(event.get("target_identity")) != _text(sample.get("target_identity")):
        issues.append("strong_training_feature_event_target_mismatch")
    if _text(event.get("station_id")).upper() != _text(sample.get("station_id")).upper():
        issues.append("strong_training_feature_event_station_mismatch")
    if _text(event.get("first_actionable_seen_at")) != _text(sample.get("cutoff_as_of")):
        issues.append("strong_training_feature_event_cutoff_mismatch")
    return issues


def _trajectory_binding_issues(sample: Mapping[str, Any], trajectory: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    expected = [_text(value) for value in list(sample.get("canonical_sequence_event_ids") or [])[:-1] if _text(value)]
    actual = [_text(value) for value in list(trajectory.get("canonical_sequence_event_ids") or []) if _text(value)]
    if actual != expected:
        issues.append("strong_training_feature_pre_event_trajectory_sequence_mismatch")
    if trajectory.get("temporal_role") != "pre_event":
        issues.append("strong_training_feature_trajectory_not_pre_event")
    if _text(trajectory.get("target_identity")) != _text(sample.get("target_identity")):
        issues.append("strong_training_feature_trajectory_target_mismatch")
    if _text(trajectory.get("station_id")).upper() != _text(sample.get("station_id")).upper():
        issues.append("strong_training_feature_trajectory_station_mismatch")
    return issues


def build_strong_observation_training_feature(
    *,
    training_sample: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    pre_event_trajectory_feature_state: Mapping[str, Any],
    source_reliability_pre_state: Mapping[str, Any],
    training_scope: str = TRAINING_SCOPE_SETTLEMENT,
) -> dict[str, Any]:
    """Create one offline feature object with exactly runtime feature semantics."""

    sample = _mapping(training_sample)
    event = _mapping(canonical_observation_event)
    trajectory = _mapping(pre_event_trajectory_feature_state)
    scope = _training_scope(training_scope)
    issues = _sample_boundary_issues(sample, training_scope=scope)
    issues.extend(_event_binding_issues(sample, event))
    issues.extend(_trajectory_binding_issues(sample, trajectory))
    if issues:
        return _blocked(*issues)

    runtime_input = build_strong_observation_lr_input(
        canonical_observation_event=event,
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id=_text(sample.get("forecast_evidence_snapshot_id")),
        prior_context_id=_text(sample.get("prior_context_id")),
        source_reliability_pre_state=source_reliability_pre_state,
    )
    if runtime_input.get("ok") is not True:
        return _blocked(*list(runtime_input.get("block_reasons") or ["strong_training_feature_runtime_input_blocked"]))
    feature_values = extract_strong_observation_feature_values(runtime_input)
    if tuple(feature_values) != REQUIRED_FEATURE_IDS:
        return _blocked("strong_training_feature_runtime_feature_set_drift")

    lineage = {
        "training_scope": scope,
        "training_sample_id": _text(sample.get("training_sample_id")),
        "training_sample_contract_sha256": _text(sample.get("sample_contract_sha256")),
        "target_identity": _text(sample.get("target_identity")),
        "event_date": _text(sample.get("event_date")),
        "cutoff_as_of": _text(sample.get("cutoff_as_of")),
        "canonical_observation_event_id": _text(sample.get("canonical_observation_event_id")),
        "canonical_sequence_sha256": _text(sample.get("canonical_sequence_sha256")),
        "pre_event_trajectory_sequence_sha256": _text(trajectory.get("canonical_sequence_sha256")),
        "forecast_evidence_snapshot_id": _text(sample.get("forecast_evidence_snapshot_id")),
        "prior_context_id": _text(sample.get("prior_context_id")),
        "source_reliability_pre_state_sha256": _sha256(dict(source_reliability_pre_state)),
        "runtime_input_sha256": _sha256(runtime_input),
        "feature_owner_id": FEATURE_OWNER_ID,
        "feature_extractor_version": FEATURE_EXTRACTOR_VERSION,
    }
    feature_id = f"strongfeat_{_sha256(lineage)[:24]}"
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "strong_observation_training_feature_id": feature_id,
        "training_sample_id": lineage["training_sample_id"],
        "training_scope": scope,
        "target_identity": lineage["target_identity"],
        "event_date": lineage["event_date"],
        "cutoff_as_of": lineage["cutoff_as_of"],
        "canonical_observation_event_id": lineage["canonical_observation_event_id"],
        "feature_owner_id": FEATURE_OWNER_ID,
        "feature_extractor_version": FEATURE_EXTRACTOR_VERSION,
        "feature_values": feature_values,
        "lineage": lineage,
        "feature_sha256": _sha256({"lineage": lineage, "feature_values": feature_values}),
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "training_scope": scope,
            "physical_x_scope_does_not_require_settlement_or_bridge": scope == TRAINING_SCOPE_PHYSICAL_X,
            "uses_runtime_feature_owner": True,
            "current_observation_used_once": True,
            "trajectory_is_pre_event_only": True,
            "final_label_not_in_feature_object": True,
            "market_or_strategy_not_consumed": True,
            "strict_asof_oos_runtime_gate": False,
            "places_orders": False,
        },
    }


__all__ = [
    "FEATURE_EXTRACTOR_VERSION",
    "FEATURE_OWNER_ID",
    "REQUIRED_FEATURE_IDS",
    "SCHEMA_VERSION",
    "TRAINING_SCOPE_PHYSICAL_X",
    "TRAINING_SCOPE_SETTLEMENT",
    "TRAINING_SCOPES",
    "build_strong_observation_training_feature",
]
