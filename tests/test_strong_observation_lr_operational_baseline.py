from __future__ import annotations

from strong_observation_lr_artifact_contract import validate_strong_observation_lr_runtime_artifact
from strong_observation_lr_fit_runtime import LATENT_STATES
from strong_observation_lr_operational_baseline import build_neutral_strong_lr_operational_baseline


def test_neutral_strong_lr_baseline_is_valid_and_claims_no_trained_skill() -> None:
    artifact = build_neutral_strong_lr_operational_baseline()

    assert validate_strong_observation_lr_runtime_artifact(artifact)["ok"] is True
    assert artifact["baseline_contract"]["not_trained_or_oos_promoted"] is True
    assert artifact["default_raw_log_score"] == 0.0
    assert artifact["post_state_transition"] == {state: {state: 1.0} for state in LATENT_STATES}
    for definition in artifact["feature_evaluator"]["feature_definitions"].values():
        for adjustment in definition["value_score_adjustments"].values():
            assert set(adjustment["raw_log_score_adjustment_by_temperature_and_pre_state"].values()) == {0.0}
