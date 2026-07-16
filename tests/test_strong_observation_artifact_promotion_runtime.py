from __future__ import annotations

from strong_observation_artifact_promotion_runtime import build_strong_observation_artifact_promotion_gate
from strong_observation_feature_ablation_runtime import build_strong_observation_feature_ablation_evidence
from strong_observation_oos_evaluation_runtime import build_strong_observation_oos_evaluation
from tests.strong_observation_artifact_fixtures import approved_strong_artifact
from tests.test_strong_observation_oos_evaluation_runtime import _dataset, _fold_predictions, _invariants
from strong_observation_training_feature_runtime import REQUIRED_FEATURE_IDS


def _full_oos(dataset: dict) -> dict:
    return build_strong_observation_oos_evaluation(
        training_dataset=dataset,
        candidate_artifact_sha256="candidate-strong-artifact-1",
        baseline_artifact_sha256="baseline-strong-artifact-1",
        fold_predictions=_fold_predictions(dataset, candidate_probability=0.9),
        invariant_evidence=_invariants(),
        minimum_oos_event_days=2,
    )


def _ablation_evaluations(dataset: dict) -> dict:
    return {
        feature_id: build_strong_observation_oos_evaluation(
            training_dataset=dataset,
            candidate_artifact_sha256=f"ablation-{index}-strong-artifact-1",
            baseline_artifact_sha256="baseline-strong-artifact-1",
            fold_predictions=_fold_predictions(dataset, candidate_probability=0.8),
            invariant_evidence=_invariants(),
            minimum_oos_event_days=2,
        )
        for index, feature_id in enumerate(REQUIRED_FEATURE_IDS)
    }
def _ablation_evidence(dataset: dict, full: dict) -> dict:
    return build_strong_observation_feature_ablation_evidence(
        full_oos_evaluation=full,
        ablation_oos_evaluations=_ablation_evaluations(dataset),
    )


def _artifact(dataset: dict, full: dict, ablation: dict) -> dict:
    artifact = approved_strong_artifact(model_artifact_sha256="candidate-strong-artifact-1")
    artifact["training_manifest_sha256"] = dataset["training_manifest_sha256"]
    artifact["oos_evaluation_sha256"] = full["oos_evaluation_sha256"]
    artifact["feature_evaluator"]["training_feature_manifest_sha256"] = dataset["training_manifest_sha256"]
    artifact["feature_evaluator"]["oos_feature_ablation_sha256"] = ablation["feature_ablation_sha256"]
    return artifact


def test_feature_ablation_and_promotion_gate_bind_exact_offline_evidence_without_touching_runtime() -> None:
    dataset = _dataset()
    full = _full_oos(dataset)
    ablation = _ablation_evidence(dataset, full)
    gate = build_strong_observation_artifact_promotion_gate(
        candidate_artifact=_artifact(dataset, full, ablation),
        training_dataset=dataset,
        oos_evaluation=full,
        feature_ablation_evidence=ablation,
    )

    assert ablation["ok"] is True
    assert ablation["all_required_features_have_oos_contribution"] is True
    assert gate["ok"] is True
    assert gate["state"] == "eligible_for_registry_activation"
    assert len(gate["promotion_gate_sha256"]) == 64
    assert gate["boundary"]["strict_asof_oos_are_promotion_only"] is True
    assert gate["boundary"]["blocks_existing_live_runtime"] is False
    assert gate["boundary"]["writes_artifact_registry"] is False


def test_promotion_gate_rejects_evidence_hash_drift_and_ablation_rejects_unhelpful_feature() -> None:
    dataset = _dataset()
    full = _full_oos(dataset)
    ablation = _ablation_evidence(dataset, full)
    artifact = _artifact(dataset, full, ablation)
    artifact["oos_evaluation_sha256"] = "drifted"
    gate = build_strong_observation_artifact_promotion_gate(
        candidate_artifact=artifact,
        training_dataset=dataset,
        oos_evaluation=full,
        feature_ablation_evidence=ablation,
    )
    no_contribution = build_strong_observation_feature_ablation_evidence(
        full_oos_evaluation=full,
        ablation_oos_evaluations={
            feature_id: full if index == 0 else evaluation
            for index, (feature_id, evaluation) in enumerate(_ablation_evaluations(dataset).items())
        },
    )

    assert gate["ok"] is False
    assert "promotion_artifact_oos_evaluation_binding_mismatch" in gate["block_reasons"]
    assert no_contribution["ok"] is False
    assert any(reason.startswith("feature_ablation_candidate_artifact_not_distinct") for reason in no_contribution["block_reasons"])
