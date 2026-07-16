"""Immutable contracts for the production-successor v6 probability core."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Mapping, Tuple

from physical_latent_state_contract import LATENT_STATE_SET


SCHEMA_VERSION = "physical_state_bayes_v6_production_successor_v1"


@dataclass(frozen=True)
class JointCell:
    y_c: float
    state: str
    probability: float

    def __post_init__(self) -> None:
        if self.state not in LATENT_STATE_SET:
            raise ValueError(f"unknown_latent_state:{self.state}")
        if not math.isfinite(self.y_c):
            raise ValueError("continuous_y_non_finite")
        if not math.isfinite(self.probability) or self.probability < 0.0:
            raise ValueError("joint_probability_negative_or_non_finite")


@dataclass(frozen=True)
class DurationClocks:
    cloud_suppression_minutes: float = 0.0
    precipitation_minutes: float = 0.0
    cb_minutes: float = 0.0
    cloud_suppression_active: bool = False
    precipitation_active: bool = False
    cb_active: bool = False

    def __post_init__(self) -> None:
        values = (
            self.cloud_suppression_minutes,
            self.precipitation_minutes,
            self.cb_minutes,
        )
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ValueError("duration_clock_negative")


@dataclass(frozen=True)
class ScenarioTransition:
    scenario_id: str
    responsibility: float
    matrix: Mapping[str, Mapping[str, float]]


@dataclass(frozen=True)
class CandidateForcingSlice:
    """Frozen scenario responsibilities and transition kernels for one Y."""

    y_c: float
    scenarios: Tuple[ScenarioTransition, ...]


@dataclass(frozen=True)
class PreEventPrediction:
    schema: str
    previous_checkpoint_sha256: str
    event_id: str
    event_valid_at: str
    delta_minutes: float
    previous_reported_regular_max_c: int | None
    previous_y_lower_bound_c: float | None
    duration_clocks_before: DurationClocks
    joint: Tuple[JointCell, ...]
    forcing_generation_sha256: str
    transition_artifact_sha256: str
    candidate_transition_trace: Mapping[str, Mapping[str, Mapping[str, float]]]


@dataclass(frozen=True)
class CanonicalObservationEmission:
    """One event's already-joint log emission, bound to its pre-event state."""

    event_id: str
    pre_event_prediction_sha256: str
    event_valid_at: str
    is_regular_metar: bool
    reported_temperature_c: float | None
    cloud_suppressed: bool
    precipitation_present: bool
    cb_present: bool
    log_emission_by_cell: Mapping[tuple[float, str], float]
    observation_artifact_sha256: str

    def __post_init__(self) -> None:
        if not self.event_id or not self.pre_event_prediction_sha256 or not self.event_valid_at:
            raise ValueError("observation_emission_identity_missing")
        if self.is_regular_metar and self.reported_temperature_c is None:
            raise ValueError("regular_metar_reported_temperature_missing")
        if self.reported_temperature_c is not None:
            value = float(self.reported_temperature_c)
            if not math.isfinite(value) or not value.is_integer():
                raise ValueError("reported_temperature_not_integer_c")


@dataclass(frozen=True)
class PostEventState:
    schema: str
    event_id: str
    pre_event_prediction_sha256: str
    joint: Tuple[JointCell, ...]
    observed_reported_regular_max_c: int | None
    y_lower_bound_c: float | None
    duration_clocks_after: DurationClocks
    observation_artifact_sha256: str
    hard_y_lower_bound_c: float | None
    normalizer_log: float
    invariant_report: Mapping[str, bool] = field(default_factory=dict)


__all__ = [
    "SCHEMA_VERSION",
    "CandidateForcingSlice",
    "CanonicalObservationEmission",
    "DurationClocks",
    "JointCell",
    "PostEventState",
    "PreEventPrediction",
    "ScenarioTransition",
]
