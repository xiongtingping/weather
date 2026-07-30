"""Immutable contracts for the identifiable physical state-space composer."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Mapping, Tuple

from .linalg import cholesky_psd


SCHEMA_VERSION = "identifiable_physical_state_bayes_v1"
POSTERIOR_SCHEMA = "physical_state_posterior_v1_gaussian_sum"
PREDICTION_SCHEMA = "physical_state_pre_event_prediction_v1"
LIKELIHOOD_SCHEMA = "physical_state_joint_observation_likelihood_v1"
PROJECTION_SCHEMA = "physical_reported_x_projection_v1"

STATE_NAMES = (
    "latent_temperature",
    "specific_humidity",
    "net_temperature_tendency",
    "surface_wetness_memory",
    "cold_pool_memory",
)
STATE_INDEX = {name: index for index, name in enumerate(STATE_NAMES)}

REGIMES = (
    "clear_heating",
    "cloud_attenuated",
    "precipitation_cold_pool",
    "warm_advection_or_mixing",
    "cold_advection",
    "post_rain_recovery",
    "evening_stable_decay",
)
REGIME_SET = frozenset(REGIMES)


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name}_non_finite")
    return result


@dataclass(frozen=True)
class ContinuousState:
    latent_temperature: float
    specific_humidity: float
    net_temperature_tendency: float
    surface_wetness_memory: float
    cold_pool_memory: float

    def __post_init__(self) -> None:
        values = tuple(_finite(getattr(self, name), name) for name in STATE_NAMES)
        if values[STATE_INDEX["specific_humidity"]] < 0.0:
            raise ValueError("specific_humidity_negative")
        wetness = values[STATE_INDEX["surface_wetness_memory"]]
        if not 0.0 <= wetness <= 1.0:
            raise ValueError("surface_wetness_out_of_bounds")
        if values[STATE_INDEX["cold_pool_memory"]] < 0.0:
            raise ValueError("cold_pool_memory_negative")

    def as_tuple(self) -> tuple[float, ...]:
        return tuple(float(getattr(self, name)) for name in STATE_NAMES)

    @classmethod
    def from_tuple(cls, values: Tuple[float, ...] | tuple[float, ...]) -> "ContinuousState":
        if len(values) != len(STATE_NAMES):
            raise ValueError("continuous_state_dimension_invalid")
        return cls(**dict(zip(STATE_NAMES, map(float, values))))


Covariance = tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class MixtureComponent:
    component_id: str
    normalized_weight: float
    regime_state: str
    forcing_scenario_id: str
    continuous_state_mean: ContinuousState
    continuous_state_covariance: Covariance
    bounded_parameter_draw_id: str = "fixed"

    def __post_init__(self) -> None:
        if not self.component_id or not self.forcing_scenario_id:
            raise ValueError("physical_component_identity_missing")
        weight = _finite(self.normalized_weight, "physical_component_weight")
        if weight < 0.0:
            raise ValueError("physical_component_weight_negative")
        if self.regime_state not in REGIME_SET:
            raise ValueError(f"physical_regime_unknown:{self.regime_state}")
        if len(self.continuous_state_covariance) != len(STATE_NAMES):
            raise ValueError("physical_covariance_dimension_invalid")
        for row in self.continuous_state_covariance:
            if len(row) != len(STATE_NAMES):
                raise ValueError("physical_covariance_dimension_invalid")
            if any(not math.isfinite(float(item)) for item in row):
                raise ValueError("physical_covariance_non_finite")
        cholesky_psd(self.continuous_state_covariance)


@dataclass(frozen=True)
class PhysicalStatePosterior:
    components: tuple[MixtureComponent, ...]
    representation: str = "gaussian_sum"
    state_schema: str = SCHEMA_VERSION
    normalization_error: float = 0.0
    uncertainty_breakdown: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.representation not in {"gaussian_sum", "rbpf_mixture"}:
            raise ValueError("physical_posterior_representation_invalid")
        if self.state_schema != SCHEMA_VERSION:
            raise ValueError("physical_posterior_schema_invalid")
        if not self.components:
            raise ValueError("physical_posterior_empty")
        if len({item.component_id for item in self.components}) != len(self.components):
            raise ValueError("physical_component_id_duplicate")
        error = abs(sum(item.normalized_weight for item in self.components) - 1.0)
        if error > 1e-9:
            raise ValueError("physical_posterior_not_normalized")
        if not math.isfinite(self.normalization_error) or self.normalization_error < 0.0:
            raise ValueError("physical_normalization_error_invalid")


@dataclass(frozen=True)
class PhysicalDriver:
    scenario_id: str
    forcing_generation_sha256: str
    net_tendency_target_c_per_hour: float
    specific_humidity_target: float
    wetting_rate_per_hour: float
    drying_rate_per_hour: float
    cold_pool_source_c_per_hour2: float
    process_noise_scale: float
    regime_transition_per_hour: Mapping[str, Mapping[str, float]]
    availability: float = 1.0
    na_reasons: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.scenario_id or len(self.forcing_generation_sha256) != 64:
            raise ValueError("physical_driver_identity_invalid")
        for name in (
            "net_tendency_target_c_per_hour",
            "specific_humidity_target",
            "wetting_rate_per_hour",
            "drying_rate_per_hour",
            "cold_pool_source_c_per_hour2",
            "process_noise_scale",
            "availability",
        ):
            _finite(getattr(self, name), f"physical_driver_{name}")
        if self.specific_humidity_target < 0.0:
            raise ValueError("physical_driver_specific_humidity_negative")
        if min(
            self.wetting_rate_per_hour,
            self.drying_rate_per_hour,
            self.cold_pool_source_c_per_hour2,
            self.process_noise_scale,
        ) < 0.0:
            raise ValueError("physical_driver_nonnegative_control_invalid")
        if not 0.0 <= self.availability <= 1.0:
            raise ValueError("physical_driver_availability_invalid")


@dataclass(frozen=True)
class CanonicalPhysicalObservation:
    canonical_event_id: str
    event_valid_at: str
    report_type: str
    is_contract_report_slot: bool
    reported_temperature_c: int | None
    reported_dewpoint_c: int | None = None
    pressure_pa: float | None = None
    cloud_fraction: float | None = None
    precipitation_present: bool | None = None
    convective_present: bool | None = None
    visibility_m: float | None = None
    wind_speed_mps: float | None = None
    wind_gust_mps: float | None = None
    pressure_tendency_pa_per_hour: float | None = None
    quality_state: str = "accepted_primary"
    field_na_reasons: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.canonical_event_id or not self.event_valid_at:
            raise ValueError("canonical_physical_observation_identity_missing")
        if self.report_type not in {"METAR", "SPECI"}:
            raise ValueError("canonical_physical_observation_report_type_invalid")
        for name in (
            "reported_temperature_c",
            "reported_dewpoint_c",
            "pressure_pa",
            "cloud_fraction",
            "visibility_m",
            "wind_speed_mps",
            "wind_gust_mps",
            "pressure_tendency_pa_per_hour",
        ):
            value = getattr(self, name)
            if value is not None:
                _finite(value, f"canonical_observation_{name}")
        if self.cloud_fraction is not None and not 0.0 <= self.cloud_fraction <= 1.0:
            raise ValueError("canonical_observation_cloud_fraction_invalid")


@dataclass(frozen=True)
class PreEventPredictiveState:
    schema: str
    previous_checkpoint_sha256: str
    canonical_event_id: str
    event_valid_at: str
    delta_minutes: float
    forcing_generation_sha256: str
    state_model_artifact_sha256: str
    posterior: PhysicalStatePosterior
    current_observation_consumed: bool = False


@dataclass(frozen=True)
class ComponentConditioning:
    component_id: str
    log_likelihood: float
    conditioned_mean: ContinuousState
    conditioned_covariance: Covariance
    component_trace: Mapping[str, float | str | bool | None]


@dataclass(frozen=True)
class JointObservationLikelihood:
    schema: str
    canonical_event_id: str
    pre_event_prediction_sha256: str
    observation_artifact_sha256: str
    component_conditioning: tuple[ComponentConditioning, ...]
    evidence_fields_consumed: tuple[str, ...]
    evidence_fields_marginalized: tuple[str, ...]
    current_event_consumption_count: int = 1


@dataclass(frozen=True)
class ConditionedPhysicalState:
    canonical_event_id: str
    pre_event_prediction_sha256: str
    observation_likelihood_sha256: str
    posterior: PhysicalStatePosterior
    observed_contract_max_c: int | None
    normalizer_log: float
    invariant_report: Mapping[str, bool]


@dataclass(frozen=True)
class ReportingSlot:
    valid_at: str
    is_contract_slot: bool
    encoding_step_c: float = 1.0
    report_probability: float = 1.0
    quality_acceptance_probability: float = 1.0
    missing_reason: str = ""

    def __post_init__(self) -> None:
        if not self.valid_at:
            raise ValueError("reporting_slot_valid_at_missing")
        if not math.isfinite(self.encoding_step_c) or self.encoding_step_c <= 0.0:
            raise ValueError("reporting_slot_encoding_step_invalid")
        for name in ("report_probability", "quality_acceptance_probability"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"reporting_slot_{name}_invalid")


@dataclass(frozen=True)
class ReportedXProjection:
    schema: str
    reported_x_distribution: Mapping[int, float]
    latent_continuous_peak_distribution: Mapping[str, float]
    observed_contract_max_c: int | None
    no_accepted_contract_report_probability: float
    reporting_operator_sha256: str
    path_projection_sha256: str
    continuous_support_completion: Mapping[str, object]
    hard_condition_applied: bool
    probability_sum_error: float


__all__ = [
    "CanonicalPhysicalObservation",
    "ComponentConditioning",
    "ConditionedPhysicalState",
    "ContinuousState",
    "Covariance",
    "JointObservationLikelihood",
    "LIKELIHOOD_SCHEMA",
    "MixtureComponent",
    "POSTERIOR_SCHEMA",
    "PREDICTION_SCHEMA",
    "PROJECTION_SCHEMA",
    "PhysicalDriver",
    "PhysicalStatePosterior",
    "PreEventPredictiveState",
    "REGIMES",
    "REGIME_SET",
    "ReportedXProjection",
    "ReportingSlot",
    "SCHEMA_VERSION",
    "STATE_INDEX",
    "STATE_NAMES",
]
