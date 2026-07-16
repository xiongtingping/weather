"""Compute immutable offline OOS evidence for a strong-observation candidate.

Predictions are evaluated only against labels in an A-class frozen dataset.
The module never trains, reads live state, changes artifacts, or participates
in the runtime decision/order path.  It recalculates NLL, bucket Brier and
physical-high CRPS from per-event out-of-sample predictions, rather than
accepting a caller-supplied aggregate metric.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any, Mapping, Sequence

from strong_observation_training_dataset_runtime import MANIFEST_SCHEMA_VERSION
from strong_observation_training_feature_runtime import TRAINING_SCOPE_SETTLEMENT


SCHEMA_VERSION = "strong_observation_oos_evaluation_v1"
EPSILON = 1e-12
REQUIRED_INVARIANT_EVIDENCE = (
    "no_information_constant_raw_score",
    "duplicate_confirmation_posterior_unchanged",
    "hard_floor_mass_zero",
    "ancestor_duplication_rejected",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


def _distribution(value: Any, *, numeric_keys: bool) -> tuple[dict[Any, float], str]:
    raw = _mapping(value)
    parsed: dict[Any, float] = {}
    for raw_key, raw_probability in raw.items():
        key: Any
        if numeric_keys:
            key = _finite(raw_key)
            if key is None:
                return {}, "physical_distribution_key_invalid"
        else:
            key = _text(raw_key)
            if not key:
                return {}, "bucket_distribution_key_invalid"
        probability = _finite(raw_probability)
        if probability is None or probability < 0.0 or key in parsed:
            return {}, "distribution_probability_invalid"
        parsed[key] = probability
    if not parsed or abs(sum(parsed.values()) - 1.0) > 1e-8:
        return {}, "distribution_not_normalized"
    return dict(sorted(parsed.items(), key=lambda item: item[0])), ""


def _crps(distribution: Mapping[float, float], observed: float) -> float:
    first = sum(probability * abs(value - observed) for value, probability in distribution.items())
    pairwise = sum(
        left_probability * right_probability * abs(left - right)
        for left, left_probability in distribution.items()
        for right, right_probability in distribution.items()
    )
    return first - 0.5 * pairwise


def _bucket_brier(distribution: Mapping[str, float], observed_bucket: str) -> float:
    labels = sorted(set(distribution) | {observed_bucket})
    return sum((distribution.get(label, 0.0) - (1.0 if label == observed_bucket else 0.0)) ** 2 for label in labels)


def _prediction_metrics(prediction: Mapping[str, Any], *, expected_sample: Mapping[str, Any]) -> tuple[dict[str, float], str]:
    labels = _mapping(expected_sample.get("labels"))
    observed_bucket = _text(labels.get("market_settlement_bucket"))
    observed_physical = _finite(labels.get("final_physical_high_c"))
    if not observed_bucket or observed_physical is None:
        return {}, "oos_expected_sample_label_invalid"
    physical, physical_error = _distribution(prediction.get("physical_high_distribution"), numeric_keys=True)
    buckets, bucket_error = _distribution(prediction.get("market_bucket_distribution"), numeric_keys=False)
    if physical_error or bucket_error:
        return {}, physical_error or bucket_error
    target_probability = buckets.get(observed_bucket)
    if target_probability is None or target_probability <= 0.0:
        return {}, "oos_observed_bucket_probability_missing_or_zero"
    return {
        "settlement_bucket_nll": -math.log(target_probability),
        "settlement_bucket_brier": _bucket_brier(buckets, observed_bucket),
        "physical_high_crps": _crps(physical, observed_physical),
    }, ""


def _invariant_issues(value: Any) -> list[str]:
    evidence = _mapping(value)
    issues: list[str] = []
    if set(evidence) != set(REQUIRED_INVARIANT_EVIDENCE):
        issues.append("oos_invariant_evidence_set_invalid")
    for name in REQUIRED_INVARIANT_EVIDENCE:
        item = _mapping(evidence.get(name))
        if item.get("passed") is not True or not _text(item.get("evidence_sha256")):
            issues.append(f"oos_invariant_evidence_invalid:{name}")
    return issues


def _aggregate(weighted_metrics: Sequence[tuple[float, Mapping[str, float]]]) -> dict[str, float]:
    total_weight = sum(weight for weight, _metrics in weighted_metrics)
    return {
        metric: sum(weight * metrics[metric] for weight, metrics in weighted_metrics) / total_weight
        for metric in ("settlement_bucket_nll", "settlement_bucket_brier", "physical_high_crps")
    }


def build_strong_observation_oos_evaluation(
    *,
    training_dataset: Mapping[str, Any],
    candidate_artifact_sha256: str,
    baseline_artifact_sha256: str,
    fold_predictions: Sequence[Mapping[str, Any]],
    invariant_evidence: Mapping[str, Any],
    minimum_oos_event_days: int = 30,
) -> dict[str, Any]:
    """Recompute candidate-vs-baseline OOS metrics over exact manifest folds."""

    dataset = _mapping(training_dataset)
    manifest = _mapping(dataset.get("training_manifest"))
    if dataset.get("ok") is not True or _text(dataset.get("state")) != "ready_for_oos":
        return _blocked("oos_training_dataset_not_ready")
    if _text(manifest.get("schema")) != MANIFEST_SCHEMA_VERSION or not _text(dataset.get("training_manifest_sha256")):
        return _blocked("oos_training_manifest_invalid")
    # This evaluator is deliberately market-settlement OOS: it scores Brier
    # and bucket NLL as well as physical CRPS.  Physical-X daily learning has
    # its own shadow path and must never be misrepresented as this evidence.
    if _text(manifest.get("training_scope")) != TRAINING_SCOPE_SETTLEMENT:
        return _blocked("oos_training_scope_not_settlement")
    candidate_sha = _text(candidate_artifact_sha256)
    baseline_sha = _text(baseline_artifact_sha256)
    if not candidate_sha or not baseline_sha or candidate_sha == baseline_sha:
        return _blocked("oos_candidate_or_baseline_artifact_identity_invalid")
    if int(minimum_oos_event_days) < 1:
        return _blocked("oos_minimum_event_days_invalid")
    invariant_issues = _invariant_issues(invariant_evidence)
    if invariant_issues:
        return _blocked(*invariant_issues)

    rows_by_id = {_text(row.get("training_sample_id")): _mapping(row) for row in dataset.get("rows") or []}
    expected_folds = list(_mapping(manifest.get("fold_manifest")).get("folds") or [])
    supplied_by_year: dict[int, Mapping[str, Any]] = {}
    for raw_fold in fold_predictions:
        fold = _mapping(raw_fold)
        try:
            year = int(fold.get("test_year"))
        except (TypeError, ValueError):
            return _blocked("oos_fold_test_year_invalid")
        if year in supplied_by_year:
            return _blocked("oos_fold_duplicate_test_year")
        supplied_by_year[year] = fold
    expected_years = {int(fold["test_year"]) for fold in expected_folds}
    if set(supplied_by_year) != expected_years:
        return _blocked(
            "oos_fold_set_mismatch",
            diagnostics={"expected_test_years": sorted(expected_years), "supplied_test_years": sorted(supplied_by_year)},
        )

    candidate_weighted: list[tuple[float, Mapping[str, float]]] = []
    baseline_weighted: list[tuple[float, Mapping[str, float]]] = []
    per_fold: list[dict[str, Any]] = []
    oos_days: set[str] = set()
    for expected in expected_folds:
        year = int(expected["test_year"])
        supplied = supplied_by_year[year]
        expected_ids = sorted(_text(value) for value in expected.get("test_training_sample_ids") or [])
        candidate_rows = {_text(row.get("training_sample_id")): _mapping(row) for row in supplied.get("candidate_predictions") or []}
        baseline_rows = {_text(row.get("training_sample_id")): _mapping(row) for row in supplied.get("baseline_predictions") or []}
        if sorted(candidate_rows) != expected_ids or sorted(baseline_rows) != expected_ids:
            return _blocked("oos_prediction_sample_set_mismatch", diagnostics={"test_year": year, "expected": expected_ids})
        candidate_fold: list[tuple[float, Mapping[str, float]]] = []
        baseline_fold: list[tuple[float, Mapping[str, float]]] = []
        for sample_id in expected_ids:
            sample = rows_by_id.get(sample_id)
            if not sample:
                return _blocked("oos_prediction_references_unknown_training_sample")
            candidate_metrics, candidate_error = _prediction_metrics(candidate_rows[sample_id], expected_sample=sample)
            baseline_metrics, baseline_error = _prediction_metrics(baseline_rows[sample_id], expected_sample=sample)
            if candidate_error or baseline_error:
                return _blocked(
                    "oos_prediction_distribution_invalid",
                    diagnostics={"training_sample_id": sample_id, "candidate_error": candidate_error, "baseline_error": baseline_error},
                )
            weight = _finite(sample.get("day_weight"))
            if weight is None or weight <= 0.0:
                return _blocked("oos_training_sample_day_weight_invalid", diagnostics={"training_sample_id": sample_id})
            candidate_fold.append((weight, candidate_metrics))
            baseline_fold.append((weight, baseline_metrics))
            candidate_weighted.append((weight, candidate_metrics))
            baseline_weighted.append((weight, baseline_metrics))
            oos_days.add(_text(sample.get("event_date")))
        per_fold.append(
            {
                "test_year": year,
                "test_training_sample_ids": expected_ids,
                "candidate_metrics": _aggregate(candidate_fold),
                "baseline_metrics": _aggregate(baseline_fold),
            }
        )
    candidate_metrics = _aggregate(candidate_weighted)
    baseline_metrics = _aggregate(baseline_weighted)
    deltas = {key: candidate_metrics[key] - baseline_metrics[key] for key in candidate_metrics}
    non_inferior = all(value <= 1e-12 for value in deltas.values())
    strictly_better = any(value < -1e-12 for value in deltas.values())
    sufficient_days = len(oos_days) >= int(minimum_oos_event_days)
    promotion_eligible = bool(non_inferior and strictly_better and sufficient_days)
    core = {
        "schema": SCHEMA_VERSION,
        "training_manifest_sha256": _text(dataset.get("training_manifest_sha256")),
        "candidate_artifact_sha256": candidate_sha,
        "baseline_artifact_sha256": baseline_sha,
        "event_day_split": "expanding_year_walk_forward",
        "minimum_oos_event_days": int(minimum_oos_event_days),
        "oos_event_day_count": len(oos_days),
        "per_fold": per_fold,
        "candidate_metrics": candidate_metrics,
        "baseline_metrics": baseline_metrics,
        "candidate_minus_baseline": deltas,
        "invariant_evidence": dict(invariant_evidence),
        "non_inferior_all_primary_metrics": non_inferior,
        "strictly_better_at_least_one_primary_metric": strictly_better,
        "promotion_eligible": promotion_eligible,
    }
    return {
        **core,
        "oos_evaluation_sha256": _sha256(core),
        "ok": True,
        "state": "promotion_evidence_ready" if promotion_eligible else "insufficient_for_promotion",
        "read_only": True,
        "offline_only": True,
        "boundary": {
            "metrics_recomputed_from_oos_predictions": True,
            "labels_are_evaluation_only": True,
            "strict_asof_oos_runtime_gate": False,
            "activates_artifact": False,
            "places_orders": False,
        },
    }


__all__ = [
    "REQUIRED_INVARIANT_EVIDENCE",
    "SCHEMA_VERSION",
    "build_strong_observation_oos_evaluation",
]
