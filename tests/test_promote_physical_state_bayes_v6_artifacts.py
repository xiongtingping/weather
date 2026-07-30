from __future__ import annotations

import json

from physical_state_bayes_v6.artifact_contracts import (
    default_time_conditioned_observation_policy,
    validate_observation_artifact,
    validate_transition_artifact,
)
from scripts.promote_physical_state_bayes_v6_artifacts import promote


def test_promotion_requires_bound_oos_evidence(tmp_path) -> None:
    source = tmp_path / "candidates"
    source.mkdir()
    transition = {
        "schema": "physical_state_bayes_v6_transition_artifact_v1",
        "state": "candidate",
        "production_runtime_eligible": False,
        "shadow_only": False,
        "training_dataset_sha256": "1" * 64,
        "training_code_sha256": "2" * 64,
        "promotion_evidence_sha256": "",
        "transitions": {
            state: {}
            for state in (
                "warming", "slowing_warming", "temporary_platform", "temporary_cooling",
                "cooling_then_rebound", "weather_suppressed", "terminal_cooling", "final_locked",
            )
        },
    }
    transition = validate_transition_artifact(transition)
    models = {
        state: {
            "continuous_mean": [0.0, 8.0],
            "continuous_covariance": [[1.0, 0.0], [0.0, 4.0]],
            "weather_signature_probabilities": {"__other__": 1.0},
        }
        for state in transition["transitions"]
    }
    observation = validate_observation_artifact(
        {
            "schema": "physical_state_bayes_v6_observation_artifact_v1",
            "state": "candidate",
            "production_runtime_eligible": False,
            "shadow_only": False,
            "training_dataset_sha256": "1" * 64,
            "training_code_sha256": "2" * 64,
            "promotion_evidence_sha256": "",
            "quantized_observation_policy": {
                "schema": "quantized_integer_temperature_robust_likelihood_v1",
                "temperature_quantization_c": 1.0,
                "measurement_error_model": "independent_uniform_rounding_moment_matched_gaussian",
                "huberize_mahalanobis_tail": True,
                "robust_mahalanobis_radius": 3.0,
            },
            "time_conditioned_observation_policy": default_time_conditioned_observation_policy(),
            "state_models": models,
        }
    )
    (source / "transition_artifact.candidate.json").write_text(json.dumps(transition))
    (source / "observation_artifact.candidate.json").write_text(json.dumps(observation))
    (source / "oos_evaluation.json").write_text(
        json.dumps(
            {
                "ok": True,
                "state": "accepted_for_canonical_replay",
                "strict_holdout_not_used_for_training": True,
                "places_orders": False,
                "transition_artifact_sha256": transition["artifact_sha256"],
                "observation_artifact_sha256": observation["artifact_sha256"],
                "evaluation_sha256": "3" * 64,
            }
        )
    )
    output = tmp_path / "config"
    result = promote(source, output)
    assert result["ok"] is True
    assert result["activates_production"] is False
    assert validate_transition_artifact(json.loads((output / "physical_state_bayes_v6_transition_artifact.json").read_text()))["production_runtime_eligible"] is True
    assert validate_observation_artifact(json.loads((output / "physical_state_bayes_v6_observation_artifact.json").read_text()))["production_runtime_eligible"] is True
