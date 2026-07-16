"""Content-addressed artifact contracts for production-successor v6."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from physical_latent_state_contract import LATENT_STATES


TRANSITION_ARTIFACT_SCHEMA = "physical_state_bayes_v6_transition_artifact_v1"
OBSERVATION_ARTIFACT_SCHEMA = "physical_state_bayes_v6_observation_artifact_v1"
TRANSITION_FEATURES = frozenset(
    {
        "remaining_warming_c",
        "forecast_rate_c_per_hour",
        "future_max_increment_c",
        "future_max_drawdown_c",
        "minutes_to_curve_peak_scaled",
        "forcing_availability",
        "precipitation_probability",
        "precipitation_feature_available",
        "cloud_suppression_probability",
        "cloud_feature_available",
        "cb_probability",
        "cb_feature_available",
        "cloud_suppression_duration_hours",
        "precipitation_duration_hours",
        "cb_duration_hours",
    }
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in dict(value).items() if key != "artifact_sha256"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def artifact_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _common_issues(value: Mapping[str, Any], schema: str) -> list[str]:
    issues: list[str] = []
    if _text(value.get("schema")) != schema:
        issues.append("artifact_schema_invalid")
    if _text(value.get("state")) not in {"candidate", "approved"}:
        issues.append("artifact_state_invalid")
    if value.get("production_runtime_eligible") is True and (
        _text(value.get("state")) != "approved" or value.get("shadow_only") is not False
    ):
        issues.append("production_artifact_not_approved")
    declared = _text(value.get("artifact_sha256"))
    if declared and declared != artifact_sha256(value):
        issues.append("artifact_sha256_mismatch")
    for field in ("training_dataset_sha256", "training_code_sha256", "promotion_evidence_sha256"):
        if value.get("production_runtime_eligible") is True and len(_text(value.get(field))) != 64:
            issues.append(f"production_artifact_lineage_invalid:{field}")
    return issues


def validate_transition_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    artifact = dict(value)
    issues = _common_issues(artifact, TRANSITION_ARTIFACT_SCHEMA)
    transitions = artifact.get("transitions")
    if not isinstance(transitions, Mapping) or set(transitions) != set(LATENT_STATES):
        issues.append("transition_source_state_set_invalid")
        transitions = {}
    for source in LATENT_STATES:
        targets = transitions.get(source) if isinstance(transitions, Mapping) else None
        if not isinstance(targets, Mapping):
            issues.append(f"transition_targets_invalid:{source}")
            continue
        if source == "final_locked" and targets:
            issues.append("final_locked_must_be_absorbing")
        for target, parameters in targets.items():
            if target not in LATENT_STATES or target == source or not isinstance(parameters, Mapping):
                issues.append(f"transition_target_invalid:{source}:{target}")
                continue
            base_rate = _finite(parameters.get("base_rate_per_hour"))
            if base_rate is None or base_rate < 0.0:
                issues.append(f"transition_base_rate_invalid:{source}:{target}")
            coefficients = parameters.get("coefficients")
            if not isinstance(coefficients, Mapping):
                issues.append(f"transition_coefficients_invalid:{source}:{target}")
                continue
            unknown = set(coefficients) - TRANSITION_FEATURES
            if unknown:
                issues.append(f"transition_feature_unknown:{source}:{target}")
            if any(_finite(item) is None for item in coefficients.values()):
                issues.append(f"transition_coefficient_non_finite:{source}:{target}")
    if issues:
        raise ValueError(";".join(sorted(set(issues))))
    artifact["artifact_sha256"] = artifact_sha256(artifact)
    return artifact


def validate_observation_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    artifact = dict(value)
    issues = _common_issues(artifact, OBSERVATION_ARTIFACT_SCHEMA)
    models = artifact.get("state_models")
    if not isinstance(models, Mapping) or set(models) != set(LATENT_STATES):
        issues.append("observation_state_model_set_invalid")
        models = {}
    for state in LATENT_STATES:
        model = models.get(state) if isinstance(models, Mapping) else None
        if not isinstance(model, Mapping):
            issues.append(f"observation_state_model_invalid:{state}")
            continue
        mean = model.get("continuous_mean")
        covariance = model.get("continuous_covariance")
        if not isinstance(mean, list) or len(mean) != 2 or any(_finite(item) is None for item in mean):
            issues.append(f"observation_mean_invalid:{state}")
        if (
            not isinstance(covariance, list)
            or len(covariance) != 2
            or any(not isinstance(row, list) or len(row) != 2 for row in covariance)
        ):
            issues.append(f"observation_covariance_invalid:{state}")
        else:
            values = [[_finite(item) for item in row] for row in covariance]
            if any(item is None for row in values for item in row):
                issues.append(f"observation_covariance_non_finite:{state}")
            else:
                a, b, c, d = values[0][0], values[0][1], values[1][0], values[1][1]
                assert a is not None and b is not None and c is not None and d is not None
                if abs(b - c) > 1e-9 or a <= 0.0 or d <= 0.0 or a * d - b * c <= 1e-9:
                    issues.append(f"observation_covariance_not_positive_definite:{state}")
        signatures = model.get("weather_signature_probabilities")
        if not isinstance(signatures, Mapping) or "__other__" not in signatures:
            issues.append(f"observation_weather_signature_missing:{state}")
        elif any(_finite(item) is None or float(item) <= 0.0 for item in signatures.values()):
            issues.append(f"observation_weather_probability_invalid:{state}")
        elif abs(sum(float(item) for item in signatures.values()) - 1.0) > 1e-8:
            issues.append(f"observation_weather_probability_not_normalized:{state}")
    if issues:
        raise ValueError(";".join(sorted(set(issues))))
    artifact["artifact_sha256"] = artifact_sha256(artifact)
    return artifact


__all__ = [
    "OBSERVATION_ARTIFACT_SCHEMA",
    "TRANSITION_ARTIFACT_SCHEMA",
    "TRANSITION_FEATURES",
    "artifact_sha256",
    "validate_observation_artifact",
    "validate_transition_artifact",
]
