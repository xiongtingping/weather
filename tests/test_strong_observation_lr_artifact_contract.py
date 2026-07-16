from __future__ import annotations

from strong_observation_lr_artifact_contract import (
    SCHEMA_VERSION,
    build_strong_observation_lr_input,
    validate_strong_observation_lr_artifact,
    validate_strong_observation_lr_runtime_artifact,
)
from temperature_trajectory_feature_runtime import build_temperature_trajectory_feature_state
from tests.strong_observation_artifact_fixtures import approved_strong_artifact


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"


def _event(index: int, temperature_c: float) -> dict:
    minute = index * 30
    hour = 6 + minute // 60
    minute %= 60
    return {
        "canonical_observation_event_id": f"canobs_strong_{index}_1234567890",
        "canonical_observation_key": f"canokey_strong_{index}",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "selected_source_id": "AMSC",
        "state": "accepted_primary",
        "valid_time": f"2026-07-03T{hour:02d}:{minute:02d}:00Z",
        "first_actionable_seen_at": f"2026-07-03T{hour:02d}:{minute + 1:02d}:00Z",
        "parsed_observation": {"temperature_c": temperature_c, "cloud_text": "FEW020"},
    }


def _artifact() -> dict:
    return approved_strong_artifact(
        model_artifact_sha256="strong-lr-artifact-immutable-1",
        raw_log_score_by_pre_state={"warming": 0.2, "cooling": -0.2},
        post_state_transition={
            "warming": {"warming": 0.8, "temporary_platform": 0.2},
            "cooling": {"cooling": 1.0},
        },
    )


def test_artifact_contract_allows_only_raw_log_score_and_post_state_proposal() -> None:
    contract = validate_strong_observation_lr_artifact(_artifact())

    assert contract["ok"] is True
    assert contract["state"] == "ready"


def test_artifact_contract_rejects_market_or_composer_probability_surface() -> None:
    artifact = _artifact()
    artifact["beta_strong"] = 0.5
    artifact["market_price"] = 0.55

    contract = validate_strong_observation_lr_artifact(artifact)

    assert contract["ok"] is False
    assert "strong_lr_artifact_contains_forbidden_market_or_composer_surface" in contract["issues"]


def test_runtime_validation_does_not_turn_strict_asof_or_oos_into_trade_gate() -> None:
    artifact = _artifact()
    artifact["approval_state"] = "draft"
    artifact.pop("training_manifest_sha256")
    artifact.pop("oos_evaluation_sha256")
    artifact["feature_evaluator"].pop("training_feature_manifest_sha256")
    artifact["feature_evaluator"].pop("oos_feature_ablation_sha256")

    release = validate_strong_observation_lr_artifact(artifact)
    runtime = validate_strong_observation_lr_runtime_artifact(artifact)

    assert release["ok"] is False
    assert runtime["ok"] is True
    assert runtime["validation_scope"] == "runtime_structural_only"


def test_strong_lr_input_binds_current_canonical_event_to_single_trajectory_owner() -> None:
    events = [_event(0, 29.0), _event(1, 30.0)]
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=events[:-1],
        decision_as_of="2026-07-03T07:00:00Z",
        temporal_role="pre_event",
    )
    input_row = build_strong_observation_lr_input(
        canonical_observation_event=events[-1],
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-snapshot-1",
        prior_context_id="prior-context-1",
        source_reliability_pre_state={"AMSC": {"state": "unknown"}},
    )

    assert input_row["ok"] is True
    assert input_row["canonical_observation_event_id"] == events[-1]["canonical_observation_event_id"]
    assert input_row["boundary"]["composer_owns_normalizer_delta_and_hard_condition"] is True

    misaligned = build_strong_observation_lr_input(
        canonical_observation_event=events[0],
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-snapshot-1",
        prior_context_id="prior-context-1",
        source_reliability_pre_state={},
    )
    assert misaligned["ok"] is False
    assert "strong_lr_input_trajectory_contains_current_event" in misaligned["block_reasons"]
