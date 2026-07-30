"""Canonical checkpoint serialization for the physical posterior."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping, Sequence

from .contracts import (
    ContinuousState,
    MixtureComponent,
    PhysicalStatePosterior,
    SCHEMA_VERSION,
)
from .core import canonical_sha256


def posterior_payload(posterior: PhysicalStatePosterior) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "representation": posterior.representation,
        "components": [asdict(item) for item in posterior.components],
        "normalization_error": posterior.normalization_error,
        "uncertainty_breakdown": dict(posterior.uncertainty_breakdown),
    }


def posterior_sha256(posterior: PhysicalStatePosterior | Mapping[str, Any]) -> str:
    payload = posterior_payload(posterior) if isinstance(posterior, PhysicalStatePosterior) else dict(posterior)
    return canonical_sha256(payload)


def posterior_from_payload(value: Mapping[str, Any]) -> PhysicalStatePosterior:
    if value.get("schema") != SCHEMA_VERSION:
        raise ValueError("physical_checkpoint_state_schema_invalid")
    rows = value.get("components")
    if not isinstance(rows, Sequence):
        raise ValueError("physical_checkpoint_components_missing")
    components = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("physical_checkpoint_component_invalid")
        mean = raw.get("continuous_state_mean")
        covariance = raw.get("continuous_state_covariance")
        if not isinstance(mean, Mapping) or not isinstance(covariance, Sequence):
            raise ValueError("physical_checkpoint_component_state_missing")
        components.append(
            MixtureComponent(
                component_id=str(raw.get("component_id") or ""),
                normalized_weight=float(raw.get("normalized_weight")),
                regime_state=str(raw.get("regime_state") or ""),
                forcing_scenario_id=str(raw.get("forcing_scenario_id") or ""),
                continuous_state_mean=ContinuousState(**{key: float(item) for key, item in mean.items()}),
                continuous_state_covariance=tuple(tuple(float(item) for item in row) for row in covariance),
                bounded_parameter_draw_id=str(raw.get("bounded_parameter_draw_id") or "fixed"),
            )
        )
    return PhysicalStatePosterior(
        components=tuple(components),
        representation=str(value.get("representation") or "gaussian_sum"),
        state_schema=SCHEMA_VERSION,
        normalization_error=float(value.get("normalization_error") or 0.0),
        uncertainty_breakdown=(
            dict(value.get("uncertainty_breakdown"))
            if isinstance(value.get("uncertainty_breakdown"), Mapping)
            else {}
        ),
    )


__all__ = ["posterior_from_payload", "posterior_payload", "posterior_sha256"]

