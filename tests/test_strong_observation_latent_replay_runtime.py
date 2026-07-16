from __future__ import annotations

import hashlib
import json

from strong_observation_latent_replay_runtime import ROW_SCHEMA_VERSION, build_strong_observation_latent_replay_labels
from strong_observation_training_feature_runtime import TRAINING_SCOPE_PHYSICAL_X
from tests.test_strong_observation_training_dataset_runtime import _sample_and_feature


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sample() -> dict:
    sample, _feature, _trajectory = _sample_and_feature(event_id="canobs_2026_123456", event_date="2026-07-03")
    return sample


def test_latent_replay_label_binds_exact_a_class_sample_and_final_label() -> None:
    sample = _sample()
    row = {"schema": ROW_SCHEMA_VERSION, "training_sample_id": sample["training_sample_id"], "target_identity": sample["target_identity"], "event_date": sample["event_date"], "canonical_observation_event_id": sample["canonical_observation_event_id"], "canonical_sequence_sha256": sample["canonical_sequence_sha256"], "final_label_sha256": _sha(sample["labels"]), "replay_ordering": "canonical_valid_time_then_first_actionable_seen_at", "ordered_replay_sha256": "replay-sha", "z_pre": "warming", "z_post": "slowing_warming"}
    result = build_strong_observation_latent_replay_labels(training_samples=[sample], ordered_replay_rows=[row])
    assert result["ok"] is True
    assert result["labels"] == [{"training_sample_id": sample["training_sample_id"], "training_scope": "settlement", "z_pre": "warming", "z_post": "slowing_warming", "latent_replay_sha256": "replay-sha"}]


def test_latent_replay_rejects_market_or_source_reliability_post_state() -> None:
    sample = _sample()
    row = {"schema": ROW_SCHEMA_VERSION, "training_sample_id": sample["training_sample_id"], "target_identity": sample["target_identity"], "event_date": sample["event_date"], "canonical_observation_event_id": sample["canonical_observation_event_id"], "canonical_sequence_sha256": sample["canonical_sequence_sha256"], "final_label_sha256": _sha(sample["labels"]), "replay_ordering": "canonical_valid_time_then_first_actionable_seen_at", "ordered_replay_sha256": "replay-sha", "z_pre": "warming", "z_post": "slowing_warming", "source_reliability_after": "verified"}
    result = build_strong_observation_latent_replay_labels(training_samples=[sample], ordered_replay_rows=[row])
    assert result["ok"] is False
    assert "strong_latent_replay_forbidden_nonphysical_surface" in result["block_reasons"]


def test_latent_replay_physical_x_scope_accepts_b_class_without_settlement_label() -> None:
    sample = _sample()
    sample["quality_grade"] = "B"
    sample["settlement_evaluation_eligible"] = False
    sample["physical_auxiliary_eligible"] = True
    sample["labels"] = {"final_physical_high_c": 32.0}
    row = {"schema": ROW_SCHEMA_VERSION, "training_sample_id": sample["training_sample_id"], "target_identity": sample["target_identity"], "event_date": sample["event_date"], "canonical_observation_event_id": sample["canonical_observation_event_id"], "canonical_sequence_sha256": sample["canonical_sequence_sha256"], "final_label_sha256": _sha(sample["labels"]), "replay_ordering": "canonical_valid_time_then_first_actionable_seen_at", "ordered_replay_sha256": "replay-sha", "z_pre": "warming", "z_post": "slowing_warming"}
    result = build_strong_observation_latent_replay_labels(
        training_samples=[sample],
        ordered_replay_rows=[row],
        training_scope=TRAINING_SCOPE_PHYSICAL_X,
    )

    assert result["ok"] is True
    assert result["labels"][0]["training_scope"] == TRAINING_SCOPE_PHYSICAL_X
    assert result["boundary"]["physical_x_scope_does_not_require_settlement_or_bridge"] is True
