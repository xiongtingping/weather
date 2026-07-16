from __future__ import annotations

from strong_observation_lr_fit_runtime import fit_strong_observation_lr_shadow_candidate
from strong_observation_lr_artifact_contract import validate_strong_observation_lr_runtime_artifact
from strong_observation_training_dataset_runtime import build_strong_observation_training_dataset
from strong_observation_training_feature_runtime import TRAINING_SCOPE_PHYSICAL_X, build_strong_observation_training_feature
from tests.test_strong_observation_training_dataset_runtime import _event, _sample_and_feature


def _dataset() -> dict:
    rows = [_sample_and_feature(event_id=f"canobs_{year}_123456", event_date=f"{year}-07-03") for year in (2024, 2025, 2026)]
    return build_strong_observation_training_dataset(
        training_samples=[sample for sample, _feature, _trajectory in rows],
        training_features=[feature for _sample, feature, _trajectory in rows],
    )


def _physical_x_dataset() -> dict:
    rows = []
    for year in (2024, 2025, 2026):
        sample, _feature, trajectory = _sample_and_feature(event_id=f"canobs_physical_{year}_123456", event_date=f"{year}-07-03")
        sample["quality_grade"] = "B"
        sample["settlement_evaluation_eligible"] = False
        sample["physical_auxiliary_eligible"] = True
        sample["labels"] = {
            "final_physical_high_c": sample["labels"]["final_physical_high_c"],
            "strict_asof_training_eligible": True,
        }
        event = _event(event_id=sample["canonical_observation_event_id"], event_date=sample["event_date"])
        feature = build_strong_observation_training_feature(
            training_sample=sample,
            canonical_observation_event=event,
            pre_event_trajectory_feature_state=trajectory,
            source_reliability_pre_state={"AMSC": {"state": "unknown"}},
            training_scope=TRAINING_SCOPE_PHYSICAL_X,
        )
        rows.append((sample, feature))
    return build_strong_observation_training_dataset(
        training_samples=[sample for sample, _feature in rows],
        training_features=[feature for _sample, feature in rows],
        training_scope=TRAINING_SCOPE_PHYSICAL_X,
    )


def test_fit_emits_runtime_valid_shadow_candidate_from_frozen_dataset_and_latent_replay() -> None:
    dataset = _dataset()
    labels = [
        {"training_sample_id": row["training_sample_id"], "training_scope": "settlement", "z_pre": "warming", "z_post": "slowing_warming", "latent_replay_sha256": f"replay-{index}"}
        for index, row in enumerate(dataset["rows"])
    ]
    result = fit_strong_observation_lr_shadow_candidate(
        training_dataset=dataset,
        latent_transition_labels=labels,
        temperature_grid_c=[31.5, 32.0, 32.5],
    )

    assert result["ok"] is True
    assert result["state"] == "shadow_candidate_ready"
    assert result["candidate_artifact"]["approval_state"] == "shadow_only"
    assert result["candidate_artifact"]["post_state_transition"]["warming"]
    assert validate_strong_observation_lr_runtime_artifact(result["candidate_artifact"])["ok"] is True
    assert result["boundary"]["activates_artifact"] is False


def test_fit_rejects_missing_or_invalid_latent_replay_labels() -> None:
    dataset = _dataset()
    missing = fit_strong_observation_lr_shadow_candidate(
        training_dataset=dataset,
        latent_transition_labels=[],
        temperature_grid_c=[32.0, 32.5],
    )
    invalid = fit_strong_observation_lr_shadow_candidate(
        training_dataset=dataset,
        latent_transition_labels=[
            {"training_sample_id": row["training_sample_id"], "training_scope": "settlement", "z_pre": "not_a_state", "z_post": "warming", "latent_replay_sha256": "hash"}
            for row in dataset["rows"]
        ],
        temperature_grid_c=[32.0, 32.5],
    )

    assert missing["ok"] is False
    assert "strong_lr_fit_latent_transition_label_set_mismatch" in missing["block_reasons"]
    assert invalid["ok"] is False
    assert "strong_lr_fit_latent_transition_label_invalid" in invalid["block_reasons"]


def test_fitted_candidate_content_hash_rejects_mutation() -> None:
    dataset = _dataset()
    labels = [
        {"training_sample_id": row["training_sample_id"], "training_scope": "settlement", "z_pre": "warming", "z_post": "warming", "latent_replay_sha256": f"replay-{index}"}
        for index, row in enumerate(dataset["rows"])
    ]
    candidate = fit_strong_observation_lr_shadow_candidate(
        training_dataset=dataset, latent_transition_labels=labels, temperature_grid_c=[31.5, 32.0, 32.5]
    )["candidate_artifact"]
    candidate["default_raw_log_score"] = 99.0
    validation = validate_strong_observation_lr_runtime_artifact(candidate)
    assert validation["ok"] is False
    assert "strong_lr_artifact_content_sha256_mismatch" in validation["issues"]


def test_fit_physical_x_scope_does_not_require_settlement_or_bridge() -> None:
    dataset = _physical_x_dataset()
    labels = [
        {"training_sample_id": row["training_sample_id"], "training_scope": TRAINING_SCOPE_PHYSICAL_X, "z_pre": "warming", "z_post": "warming", "latent_replay_sha256": f"physical-replay-{index}"}
        for index, row in enumerate(dataset["rows"])
    ]
    result = fit_strong_observation_lr_shadow_candidate(
        training_dataset=dataset,
        latent_transition_labels=labels,
        temperature_grid_c=[31.5, 32.0, 32.5],
    )

    assert result["ok"] is True
    assert result["candidate_artifact"]["training_scope"] == TRAINING_SCOPE_PHYSICAL_X
    assert result["boundary"]["physical_x_scope_does_not_require_settlement_or_bridge"] is True
