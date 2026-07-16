from __future__ import annotations

from joint_forecast_weak_likelihood_oos_runtime import evaluate_joint_weak_walk_forward
from joint_forecast_weak_likelihood_promotion_runtime import build_joint_weak_promotion_packet
from joint_forecast_weak_likelihood_training_runtime import build_joint_weak_training_dataset, train_joint_weak_source_set_artifact
from tests.test_joint_forecast_weak_likelihood_training_runtime import CATALOG, _event


def test_promotion_packet_requires_identity_matched_oos_but_cannot_apply() -> None:
    events = [_event(index, ["gfs", "taf"]) for index in range(30)]
    events += [_event(index + 31, ["gfs", "taf"]) for index in range(30)]
    dataset = build_joint_weak_training_dataset(events=events, source_catalog=CATALOG)
    candidate = train_joint_weak_source_set_artifact(
        dataset=build_joint_weak_training_dataset(events=events[:30], source_catalog=CATALOG),
        minimum_event_days_per_surface=30,
        minimum_pair_samples=20,
        bias_ewma_alpha=0.03,
    )["artifact"]
    oos = evaluate_joint_weak_walk_forward(
        dataset=dataset,
        evaluation_start_date="2025-05-31",
        minimum_event_days_per_surface=30,
        minimum_pair_samples=20,
        candidate_bias_ewma_alpha=0.03,
    )

    packet = build_joint_weak_promotion_packet(candidate_artifact=candidate, oos_evaluation=oos, minimum_oos_samples=30)

    assert packet["state"] == "proposed"
    assert packet["oos_evaluation"]["bias_alpha_selection"]["selected_bias_ewma_alpha"] == 0.03
    assert packet["boundary"]["writes_runtime_artifact_pointer"] is False
    assert packet["boundary"]["strict_asof_oos_runtime_gate"] is False


def test_promotion_packet_rejects_artifact_mutation_or_identity_mismatch() -> None:
    events = [_event(index, ["gfs", "taf"]) for index in range(30)]
    dataset = build_joint_weak_training_dataset(events=events, source_catalog=CATALOG)
    candidate = train_joint_weak_source_set_artifact(dataset=dataset, minimum_event_days_per_surface=30, minimum_pair_samples=20)["artifact"]
    candidate["approval_state"] = "approved"

    packet = build_joint_weak_promotion_packet(candidate_artifact=candidate, oos_evaluation={"state": "evaluated_shadow_only", "training": {"artifact_sha256": "wrong"}, "sample_count": 100, "mae_c": 1.0, "mean_negative_log_probability": 1.0})

    assert packet["state"] == "blocked"
    assert "candidate_must_remain_shadow_only_until_approval" in packet["blockers"]
    assert "candidate_artifact_sha256_invalid" in packet["blockers"]
    assert "bias_alpha_oos_comparison_missing_or_incomplete" in packet["blockers"]
    assert "oos_raw_baseline_harm_or_tail_protection_failed" in packet["blockers"]
