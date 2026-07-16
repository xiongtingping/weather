from __future__ import annotations

import hashlib

from strong_observation_oos_evaluation_runtime import (
    REQUIRED_INVARIANT_EVIDENCE,
    build_strong_observation_oos_evaluation,
)
from strong_observation_training_dataset_runtime import build_strong_observation_training_dataset
from tests.test_strong_observation_training_dataset_runtime import _sample_and_feature


def _dataset() -> dict:
    rows = [
        _sample_and_feature(event_id=f"canobs_oos_{year}_123456", event_date=f"{year}-07-03")
        for year in (2024, 2025, 2026)
    ]
    return build_strong_observation_training_dataset(
        training_samples=[sample for sample, _feature, _trajectory in rows],
        training_features=[feature for _sample_row, feature, _trajectory in rows],
    )


def _invariants() -> dict:
    return {
        key: {"passed": True, "evidence_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for key in REQUIRED_INVARIANT_EVIDENCE
    }


def _fold_predictions(dataset: dict, *, candidate_probability: float = 0.9) -> list[dict]:
    output: list[dict] = []
    for fold in dataset["training_manifest"]["fold_manifest"]["folds"]:
        candidate = []
        baseline = []
        for sample_id in fold["test_training_sample_ids"]:
            candidate.append(
                {
                    "training_sample_id": sample_id,
                    "physical_high_distribution": {"31": 1.0 - candidate_probability, "32": candidate_probability},
                    "market_bucket_distribution": {"31C": 1.0 - candidate_probability, "32C": candidate_probability},
                }
            )
            baseline.append(
                {
                    "training_sample_id": sample_id,
                    "physical_high_distribution": {"31": 0.5, "32": 0.5},
                    "market_bucket_distribution": {"31C": 0.5, "32C": 0.5},
                }
            )
        output.append(
            {
                "test_year": fold["test_year"],
                "candidate_predictions": candidate,
                "baseline_predictions": baseline,
            }
        )
    return output


def test_oos_evaluation_recomputes_metrics_on_exact_expanding_folds() -> None:
    dataset = _dataset()
    evaluation = build_strong_observation_oos_evaluation(
        training_dataset=dataset,
        candidate_artifact_sha256="candidate-strong-artifact-1",
        baseline_artifact_sha256="baseline-strong-artifact-1",
        fold_predictions=_fold_predictions(dataset),
        invariant_evidence=_invariants(),
        minimum_oos_event_days=2,
    )

    assert evaluation["ok"] is True
    assert evaluation["state"] == "promotion_evidence_ready"
    assert evaluation["candidate_metrics"]["settlement_bucket_nll"] < evaluation["baseline_metrics"]["settlement_bucket_nll"]
    assert evaluation["candidate_metrics"]["physical_high_crps"] < evaluation["baseline_metrics"]["physical_high_crps"]
    assert evaluation["boundary"]["strict_asof_oos_runtime_gate"] is False
    assert evaluation["boundary"]["activates_artifact"] is False


def test_oos_evaluation_rejects_fold_identity_drift_and_candidate_regression() -> None:
    dataset = _dataset()
    drift = _fold_predictions(dataset)
    drift[0]["candidate_predictions"] = []
    bad_set = build_strong_observation_oos_evaluation(
        training_dataset=dataset,
        candidate_artifact_sha256="candidate-strong-artifact-1",
        baseline_artifact_sha256="baseline-strong-artifact-1",
        fold_predictions=drift,
        invariant_evidence=_invariants(),
        minimum_oos_event_days=2,
    )
    regression = build_strong_observation_oos_evaluation(
        training_dataset=dataset,
        candidate_artifact_sha256="candidate-strong-artifact-1",
        baseline_artifact_sha256="baseline-strong-artifact-1",
        fold_predictions=_fold_predictions(dataset, candidate_probability=0.4),
        invariant_evidence=_invariants(),
        minimum_oos_event_days=2,
    )

    assert bad_set["ok"] is False
    assert "oos_prediction_sample_set_mismatch" in bad_set["block_reasons"]
    assert regression["ok"] is True
    assert regression["state"] == "insufficient_for_promotion"
    assert regression["promotion_eligible"] is False


def test_market_oos_explicitly_rejects_physical_x_training_scope() -> None:
    dataset = _dataset()
    physical_dataset = dict(dataset)
    physical_dataset["training_manifest"] = dict(dataset["training_manifest"])
    physical_dataset["training_manifest"]["training_scope"] = "physical_x"
    evaluation = build_strong_observation_oos_evaluation(
        training_dataset=physical_dataset,
        candidate_artifact_sha256="candidate-strong-artifact-1",
        baseline_artifact_sha256="baseline-strong-artifact-1",
        fold_predictions=[],
        invariant_evidence=_invariants(),
    )

    assert evaluation["ok"] is False
    assert "oos_training_scope_not_settlement" in evaluation["block_reasons"]
