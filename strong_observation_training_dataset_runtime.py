"""Freeze A-class strong-observation training rows and expanding OOS folds.

This module deliberately does not fit a model.  It turns only already frozen
event-level samples and runtime-owned feature objects into an immutable
offline dataset manifest.  A future trainer must consume this manifest and
write separate fit/OOS evidence; it may not silently scan archive.db or
rebuild a competing trajectory feature pipeline.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Mapping, Sequence

from strong_observation_training_feature_runtime import (
    FEATURE_EXTRACTOR_VERSION,
    FEATURE_OWNER_ID,
    REQUIRED_FEATURE_IDS,
    SCHEMA_VERSION as TRAINING_FEATURE_SCHEMA,
    TRAINING_SCOPE_PHYSICAL_X,
    TRAINING_SCOPE_SETTLEMENT,
    TRAINING_SCOPES,
)
from trajectory_label_sample_runtime import (
    INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION,
    assign_temperature_incremental_event_day_weights,
    build_temperature_incremental_expanding_year_folds,
)


SCHEMA_VERSION = "strong_observation_training_dataset_v1"
MANIFEST_SCHEMA_VERSION = "strong_observation_training_manifest_v1"
TRAINING_DATA_POLICY = "strict_asof_canonical_event_v2"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _blocked(*reasons: str, diagnostics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "diagnostics": dict(diagnostics or {}),
        "read_only": True,
        "shadow_only": True,
        "places_orders": False,
    }


def _sample_issues(sample: Mapping[str, Any], *, training_scope: str) -> list[str]:
    issues: list[str] = []
    if training_scope not in TRAINING_SCOPES:
        issues.append("strong_training_dataset_training_scope_invalid")
    if _text(sample.get("schema")) != INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION:
        issues.append("strong_training_dataset_sample_schema_invalid")
    if training_scope == TRAINING_SCOPE_SETTLEMENT:
        if sample.get("quality_grade") != "A" or sample.get("settlement_evaluation_eligible") is not True:
            issues.append("strong_training_dataset_sample_not_a_class_settlement_eligible")
    elif training_scope == TRAINING_SCOPE_PHYSICAL_X:
        if sample.get("quality_grade") not in {"A", "B"} or sample.get("physical_auxiliary_eligible") is not True:
            issues.append("strong_training_dataset_sample_not_physical_x_eligible")
    for key in (
        "training_sample_id",
        "sample_contract_sha256",
        "target_identity",
        "event_date",
        "cutoff_as_of",
        "canonical_observation_event_id",
        "canonical_sequence_sha256",
        "forecast_evidence_snapshot_id",
        "prior_context_id",
    ):
        if not _text(sample.get(key)):
            issues.append(f"strong_training_dataset_sample_{key}_missing")
    labels = _mapping(sample.get("labels"))
    required_labels = ("final_physical_high_c",)
    if training_scope == TRAINING_SCOPE_SETTLEMENT:
        required_labels += ("settlement_scalar_c", "market_settlement_bucket", "resolution_contract_id")
    for key in required_labels:
        if labels.get(key) in (None, ""):
            issues.append(f"strong_training_dataset_label_{key}_missing")
    if training_scope == TRAINING_SCOPE_PHYSICAL_X and labels.get("strict_asof_training_eligible") is not True:
        issues.append("strong_training_dataset_final_physical_x_not_strict_asof_eligible")
    return issues


def _feature_issues(feature: Mapping[str, Any], *, training_scope: str) -> list[str]:
    issues: list[str] = []
    if _text(feature.get("schema")) != TRAINING_FEATURE_SCHEMA:
        issues.append("strong_training_dataset_feature_schema_invalid")
    if feature.get("ok") is not True or _text(feature.get("state")) != "available":
        issues.append("strong_training_dataset_feature_not_available")
    if _text(feature.get("feature_owner_id")) != FEATURE_OWNER_ID:
        issues.append("strong_training_dataset_feature_owner_invalid")
    if _text(feature.get("feature_extractor_version")) != FEATURE_EXTRACTOR_VERSION:
        issues.append("strong_training_dataset_feature_extractor_version_invalid")
    if _text(feature.get("training_scope")) != training_scope:
        issues.append("strong_training_dataset_feature_training_scope_mismatch")
    feature_values = _mapping(feature.get("feature_values"))
    if tuple(feature_values) != REQUIRED_FEATURE_IDS:
        issues.append("strong_training_dataset_feature_set_drift")
    for key in ("strong_observation_training_feature_id", "training_sample_id", "feature_sha256"):
        if not _text(feature.get(key)):
            issues.append(f"strong_training_dataset_feature_{key}_missing")
    return issues


def build_strong_observation_training_dataset(
    *,
    training_samples: Sequence[Mapping[str, Any]],
    training_features: Sequence[Mapping[str, Any]],
    training_scope: str = TRAINING_SCOPE_SETTLEMENT,
) -> dict[str, Any]:
    """Return an immutable strict-as-of dataset and event-day expanding folds.

    ``settlement`` accepts only A-class settlement-evaluable samples.
    ``physical_x`` accepts A/B physical-X-eligible samples and deliberately
    does not require S, a market bucket, or a resolution contract.  In both
    scopes a feature row binds exactly one frozen canonical event/cutoff.
    """

    samples = [dict(row) for row in training_samples if isinstance(row, Mapping)]
    features = [dict(row) for row in training_features if isinstance(row, Mapping)]
    scope = _text(training_scope)
    sample_by_id: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for sample in samples:
        issues.extend(_sample_issues(sample, training_scope=scope))
        sample_id = _text(sample.get("training_sample_id"))
        if sample_id in sample_by_id:
            issues.append("strong_training_dataset_duplicate_training_sample_id")
        elif sample_id:
            sample_by_id[sample_id] = sample
    feature_by_sample: dict[str, dict[str, Any]] = {}
    for feature in features:
        issues.extend(_feature_issues(feature, training_scope=scope))
        sample_id = _text(feature.get("training_sample_id"))
        if sample_id in feature_by_sample:
            issues.append("strong_training_dataset_duplicate_feature_for_training_sample")
        elif sample_id:
            feature_by_sample[sample_id] = feature
    if not samples:
        issues.append("strong_training_dataset_samples_missing")
    if not features:
        issues.append("strong_training_dataset_features_missing")
    if issues:
        return _blocked(*issues, diagnostics={"sample_count": len(samples), "feature_count": len(features)})

    missing_features = sorted(set(sample_by_id) - set(feature_by_sample))
    unexpected_features = sorted(set(feature_by_sample) - set(sample_by_id))
    if missing_features or unexpected_features:
        return _blocked(
            *( ["strong_training_dataset_feature_missing_for_sample"] if missing_features else [] ),
            *( ["strong_training_dataset_feature_without_sample"] if unexpected_features else [] ),
            diagnostics={"missing_feature_sample_ids": missing_features, "unexpected_feature_sample_ids": unexpected_features},
        )

    rows: list[dict[str, Any]] = []
    for sample_id in sorted(sample_by_id):
        sample = sample_by_id[sample_id]
        feature = feature_by_sample[sample_id]
        lineage = _mapping(feature.get("lineage"))
        bindings = {
            "training_scope": scope,
            "canonical_observation_event_id": _text(sample.get("canonical_observation_event_id")),
            "target_identity": _text(sample.get("target_identity")),
            "event_date": _text(sample.get("event_date")),
            "cutoff_as_of": _text(sample.get("cutoff_as_of")),
            "canonical_sequence_sha256": _text(sample.get("canonical_sequence_sha256")),
            "forecast_evidence_snapshot_id": _text(sample.get("forecast_evidence_snapshot_id")),
            "prior_context_id": _text(sample.get("prior_context_id")),
            "training_sample_contract_sha256": _text(sample.get("sample_contract_sha256")),
        }
        mismatched = [key for key, expected in bindings.items() if _text(lineage.get(key)) != expected]
        if mismatched:
            return _blocked(
                "strong_training_dataset_feature_sample_lineage_mismatch",
                diagnostics={"training_sample_id": sample_id, "mismatched_fields": mismatched},
            )
        rows.append(
            {
                "training_sample_id": sample_id,
                "training_scope": scope,
                "strong_observation_training_feature_id": _text(feature.get("strong_observation_training_feature_id")),
                "feature_sha256": _text(feature.get("feature_sha256")),
                "sample_contract_sha256": bindings["training_sample_contract_sha256"],
                "target_identity": bindings["target_identity"],
                "event_date": bindings["event_date"],
                "cutoff_as_of": bindings["cutoff_as_of"],
                "canonical_observation_event_id": bindings["canonical_observation_event_id"],
                "canonical_sequence_sha256": bindings["canonical_sequence_sha256"],
                "forecast_evidence_snapshot_id": bindings["forecast_evidence_snapshot_id"],
                "prior_context_id": bindings["prior_context_id"],
                "feature_values": dict(_mapping(feature.get("feature_values"))),
                # Labels deliberately enter only as a supervised target at
                # this final assembly point, never the feature object.
                "labels": dict(_mapping(sample.get("labels"))),
            }
        )

    weighted_samples = {
        _text(row.get("training_sample_id")): row
        for row in assign_temperature_incremental_event_day_weights(list(sample_by_id.values()))
    }
    for row in rows:
        row["day_weight"] = weighted_samples[row["training_sample_id"]].get("day_weight")
    folds = build_temperature_incremental_expanding_year_folds(
        list(weighted_samples.values()),
        eligibility_field=("physical_auxiliary_eligible" if scope == TRAINING_SCOPE_PHYSICAL_X else "settlement_evaluation_eligible"),
    )
    day_counts = Counter(_text(row["event_date"]) for row in rows)
    core_manifest = {
        "schema": MANIFEST_SCHEMA_VERSION,
        "training_scope": scope,
        "training_data_policy": TRAINING_DATA_POLICY,
        "feature_owner_id": FEATURE_OWNER_ID,
        "feature_extractor_version": FEATURE_EXTRACTOR_VERSION,
        "sample_schema": INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION,
        "training_feature_schema": TRAINING_FEATURE_SCHEMA,
        "required_feature_ids": list(REQUIRED_FEATURE_IDS),
        "row_count": len(rows),
        "event_day_count": len(day_counts),
        "event_day_weights": "one_over_eligible_events_per_target_day",
        "rows": [
            {
                key: row[key]
                for key in (
                    "training_sample_id",
                    "training_scope",
                    "strong_observation_training_feature_id",
                    "feature_sha256",
                    "sample_contract_sha256",
                    "target_identity",
                    "event_date",
                    "cutoff_as_of",
                    "canonical_observation_event_id",
                    "canonical_sequence_sha256",
                    "forecast_evidence_snapshot_id",
                    "prior_context_id",
                    "day_weight",
                )
            }
            for row in rows
        ],
        "fold_manifest": folds,
        "random_event_split_allowed": False,
        "same_event_day_cross_fold": False,
    }
    manifest = {**core_manifest, "training_manifest_sha256": _sha256(core_manifest)}
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "ready_for_oos" if folds["folds"] else "insufficient_oos_years",
        "rows": rows,
        "training_manifest": manifest,
        "training_manifest_sha256": manifest["training_manifest_sha256"],
        "oos_fold_count": len(folds["folds"]),
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "training_scope": scope,
            "requires_a_class_settlement_samples": scope == TRAINING_SCOPE_SETTLEMENT,
            "physical_x_scope_does_not_require_settlement_or_bridge": scope == TRAINING_SCOPE_PHYSICAL_X,
            "labels_are_supervised_targets_only": True,
            "uses_runtime_feature_owner": True,
            "strict_asof_oos_runtime_gate": False,
            "fits_or_activates_model": False,
            "places_orders": False,
        },
    }


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "TRAINING_DATA_POLICY",
    "build_strong_observation_training_dataset",
]
