"""Validate per-feature OOS ablation evidence for a strong-LR candidate."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from strong_observation_oos_evaluation_runtime import SCHEMA_VERSION as OOS_SCHEMA_VERSION
from strong_observation_training_feature_runtime import REQUIRED_FEATURE_IDS


SCHEMA_VERSION = "strong_observation_feature_ablation_v1"
PRIMARY_METRICS = ("settlement_bucket_nll", "settlement_bucket_brier", "physical_high_crps")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


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
        "offline_only": True,
        "places_orders": False,
    }


def _oos_identity_issues(evaluation: Mapping[str, Any], *, require_promotion: bool) -> list[str]:
    issues: list[str] = []
    if _text(evaluation.get("schema")) != OOS_SCHEMA_VERSION or evaluation.get("ok") is not True:
        issues.append("feature_ablation_oos_evaluation_invalid")
    if require_promotion and evaluation.get("promotion_eligible") is not True:
        issues.append("feature_ablation_full_oos_not_promotion_eligible")
    for key in ("training_manifest_sha256", "candidate_artifact_sha256", "oos_evaluation_sha256"):
        if not _text(evaluation.get(key)):
            issues.append(f"feature_ablation_oos_{key}_missing")
    metrics = _mapping(evaluation.get("candidate_metrics"))
    if any(_finite(metrics.get(name)) is None for name in PRIMARY_METRICS):
        issues.append("feature_ablation_oos_candidate_metrics_invalid")
    return issues


def build_strong_observation_feature_ablation_evidence(
    *,
    full_oos_evaluation: Mapping[str, Any],
    ablation_oos_evaluations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Prove each required runtime feature contributes OOS information.

    Each ablation must use the same frozen dataset/folds as the full candidate
    but a distinct candidate artifact identity.  The full model must be no
    worse on every primary metric and strictly better on at least one.  A
    feature that cannot meet this test should be removed from the candidate
    design rather than carried into production as decorative complexity.
    """

    full = _mapping(full_oos_evaluation)
    issues = _oos_identity_issues(full, require_promotion=True)
    supplied = _mapping(ablation_oos_evaluations)
    if set(supplied) != set(REQUIRED_FEATURE_IDS):
        issues.append("feature_ablation_required_feature_set_invalid")
    if issues:
        return _blocked(*issues)
    full_metrics = _mapping(full.get("candidate_metrics"))
    result_rows: list[dict[str, Any]] = []
    for feature_id in REQUIRED_FEATURE_IDS:
        ablation = _mapping(supplied.get(feature_id))
        issues = _oos_identity_issues(ablation, require_promotion=False)
        if _text(ablation.get("training_manifest_sha256")) != _text(full.get("training_manifest_sha256")):
            issues.append("feature_ablation_training_manifest_mismatch")
        if _text(ablation.get("candidate_artifact_sha256")) == _text(full.get("candidate_artifact_sha256")):
            issues.append("feature_ablation_candidate_artifact_not_distinct")
        ablation_metrics = _mapping(ablation.get("candidate_metrics"))
        deltas: dict[str, float] = {}
        for name in PRIMARY_METRICS:
            full_value = _finite(full_metrics.get(name))
            ablated_value = _finite(ablation_metrics.get(name))
            if full_value is None or ablated_value is None:
                issues.append("feature_ablation_primary_metric_invalid")
                continue
            deltas[name] = full_value - ablated_value
        non_inferior = bool(deltas) and all(value <= 1e-12 for value in deltas.values())
        strictly_better = bool(deltas) and any(value < -1e-12 for value in deltas.values())
        if not non_inferior or not strictly_better:
            issues.append(f"feature_ablation_no_oos_contribution:{feature_id}")
        if issues:
            return _blocked(*issues, diagnostics={"feature_id": feature_id, "metric_delta_full_minus_ablation": deltas})
        result_rows.append(
            {
                "feature_id": feature_id,
                "ablation_candidate_artifact_sha256": _text(ablation.get("candidate_artifact_sha256")),
                "ablation_oos_evaluation_sha256": _text(ablation.get("oos_evaluation_sha256")),
                "metric_delta_full_minus_ablation": deltas,
            }
        )
    core = {
        "schema": SCHEMA_VERSION,
        "training_manifest_sha256": _text(full.get("training_manifest_sha256")),
        "full_candidate_artifact_sha256": _text(full.get("candidate_artifact_sha256")),
        "full_oos_evaluation_sha256": _text(full.get("oos_evaluation_sha256")),
        "required_feature_ids": list(REQUIRED_FEATURE_IDS),
        "ablations": result_rows,
        "all_required_features_have_oos_contribution": True,
    }
    return {
        **core,
        "feature_ablation_sha256": _sha256(core),
        "ok": True,
        "state": "available",
        "read_only": True,
        "offline_only": True,
        "boundary": {
            "uses_exact_full_oos_folds": True,
            "labels_are_evaluation_only": True,
            "activates_artifact": False,
            "strict_asof_oos_runtime_gate": False,
            "places_orders": False,
        },
    }


__all__ = [
    "PRIMARY_METRICS",
    "SCHEMA_VERSION",
    "build_strong_observation_feature_ablation_evidence",
]
