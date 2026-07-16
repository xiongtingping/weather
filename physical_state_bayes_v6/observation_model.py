"""Single joint canonical-observation emission for production-successor v6."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .artifact_contracts import validate_observation_artifact
from .contracts import CanonicalObservationEmission, PreEventPrediction
from .core import pre_event_prediction_sha256


@dataclass(frozen=True)
class ObservationFeatureVector:
    current_step_rate_c_per_hour: float | None
    dewpoint_depression_c: float | None
    weather_signature: str


def _bivariate_log_density(
    values: tuple[float, float], mean: list[float], covariance: list[list[float]]
) -> float:
    a, b = float(covariance[0][0]), float(covariance[0][1])
    c, d = float(covariance[1][0]), float(covariance[1][1])
    determinant = a * d - b * c
    x0, x1 = values[0] - float(mean[0]), values[1] - float(mean[1])
    quadratic = (d * x0 * x0 - (b + c) * x0 * x1 + a * x1 * x1) / determinant
    return -math.log(2.0 * math.pi) - 0.5 * math.log(determinant) - 0.5 * quadratic


def _continuous_log_density(
    values: tuple[float | None, float | None],
    mean: list[float],
    covariance: list[list[float]],
) -> float:
    available = [index for index, value in enumerate(values) if value is not None]
    if not available:
        return 0.0
    if len(available) == 2:
        return _bivariate_log_density(
            (float(values[0]), float(values[1])), mean, covariance
        )
    index = available[0]
    variance = float(covariance[index][index])
    difference = float(values[index]) - float(mean[index])
    return -0.5 * (math.log(2.0 * math.pi * variance) + difference * difference / variance)


def build_joint_observation_emission(
    *,
    prediction: PreEventPrediction,
    is_regular_metar: bool,
    reported_temperature_c: float | None,
    cloud_suppressed: bool,
    precipitation_present: bool,
    cb_present: bool,
    features: ObservationFeatureVector,
    observation_artifact: Mapping[str, object],
) -> CanonicalObservationEmission:
    artifact = validate_observation_artifact(observation_artifact)
    values = (
        float(features.current_step_rate_c_per_hour)
        if features.current_step_rate_c_per_hour is not None
        else None,
        float(features.dewpoint_depression_c)
        if features.dewpoint_depression_c is not None
        else None,
    )
    if any(value is not None and not math.isfinite(value) for value in values) or not features.weather_signature:
        raise ValueError("observation_feature_vector_invalid")
    models = artifact["state_models"]
    assert isinstance(models, Mapping)
    scores: dict[tuple[float, str], float] = {}
    for cell in prediction.joint:
        model = models[cell.state]
        assert isinstance(model, Mapping)
        signature_probabilities = model["weather_signature_probabilities"]
        assert isinstance(signature_probabilities, Mapping)
        weather_probability = float(
            signature_probabilities.get(
                features.weather_signature, signature_probabilities["__other__"]
            )
        )
        continuous = _continuous_log_density(
            values,
            list(model["continuous_mean"]),
            [list(row) for row in model["continuous_covariance"]],
        )
        scores[(cell.y_c, cell.state)] = continuous + math.log(weather_probability)
    return CanonicalObservationEmission(
        event_id=prediction.event_id,
        pre_event_prediction_sha256=pre_event_prediction_sha256(prediction),
        event_valid_at=prediction.event_valid_at,
        is_regular_metar=is_regular_metar,
        reported_temperature_c=reported_temperature_c,
        cloud_suppressed=cloud_suppressed,
        precipitation_present=precipitation_present,
        cb_present=cb_present,
        log_emission_by_cell=scores,
        observation_artifact_sha256=str(artifact["artifact_sha256"]),
    )


__all__ = ["ObservationFeatureVector", "build_joint_observation_emission"]
