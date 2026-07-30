"""Pure Predict -> joint emission -> condition kernel for physical state Bayes."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    LIKELIHOOD_SCHEMA,
    PREDICTION_SCHEMA,
    CanonicalPhysicalObservation,
    ComponentConditioning,
    ConditionedPhysicalState,
    ContinuousState,
    JointObservationLikelihood,
    MixtureComponent,
    PhysicalDriver,
    PhysicalStatePosterior,
    PreEventPredictiveState,
    REGIMES,
    STATE_INDEX,
    STATE_NAMES,
)
from .linalg import add, determinant, identity, inverse, matmul, matvec, outer, symmetrize, transpose


STATE_MODEL_ARTIFACT_SCHEMA = "identifiable_physical_state_model_artifact_v1"
OBSERVATION_ARTIFACT_SCHEMA = "identifiable_physical_observation_artifact_v1"
_SQRT_2PI = math.sqrt(2.0 * math.pi)
_MIN_PROBABILITY = 1e-300


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: asdict(item) if hasattr(item, "__dataclass_fields__") else str(item),
        ).encode("utf-8")
    ).hexdigest()


def _artifact_sha(value: Mapping[str, Any]) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != "artifact_sha256"})


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("physical_time_timezone_missing")
    return parsed.astimezone(timezone.utc)


def validate_state_model_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    artifact = dict(value)
    issues: list[str] = []
    if artifact.get("schema") != STATE_MODEL_ARTIFACT_SCHEMA:
        issues.append("state_model_artifact_schema_invalid")
    declared = str(artifact.get("artifact_sha256") or "")
    computed = _artifact_sha(artifact)
    if declared and declared != computed:
        issues.append("state_model_artifact_sha256_mismatch")
    maximum = float(artifact.get("max_substep_minutes") or 0.0)
    if not math.isfinite(maximum) or maximum <= 0.0:
        issues.append("state_model_max_substep_invalid")
    memory = artifact.get("memory_hours")
    if not isinstance(memory, Mapping):
        issues.append("state_model_memory_missing")
        memory = {}
    for name in ("net_temperature_tendency", "specific_humidity", "cold_pool_memory"):
        value_by_regime = memory.get(name)
        if not isinstance(value_by_regime, Mapping) or set(value_by_regime) != set(REGIMES):
            issues.append(f"state_model_memory_regime_set_invalid:{name}")
            continue
        if any(float(item) <= 0.0 or not math.isfinite(float(item)) for item in value_by_regime.values()):
            issues.append(f"state_model_memory_value_invalid:{name}")
    covariance = artifact.get("process_covariance_per_hour")
    if not isinstance(covariance, Mapping) or set(covariance) != set(REGIMES):
        issues.append("state_model_process_covariance_regime_set_invalid")
    else:
        for regime, matrix in covariance.items():
            try:
                MixtureComponent(
                    component_id="validation",
                    normalized_weight=1.0,
                    regime_state=str(regime),
                    forcing_scenario_id="validation",
                    continuous_state_mean=ContinuousState(20.0, 0.01, 0.0, 0.5, 0.0),
                    continuous_state_covariance=tuple(tuple(float(item) for item in row) for row in matrix),
                )
            except (TypeError, ValueError) as exc:
                issues.append(f"state_model_process_covariance_invalid:{regime}:{exc}")
    offsets = artifact.get("regime_tendency_offset_c_per_hour")
    if not isinstance(offsets, Mapping) or set(offsets) != set(REGIMES):
        issues.append("state_model_regime_offset_set_invalid")
    bounds = artifact.get("state_bounds")
    if not isinstance(bounds, Mapping) or set(bounds) != set(STATE_NAMES):
        issues.append("state_model_bounds_invalid")
    else:
        for name, row in bounds.items():
            if not isinstance(row, Sequence) or len(row) != 2 or float(row[0]) >= float(row[1]):
                issues.append(f"state_model_bound_invalid:{name}")
    parameter_draws = artifact.get("bounded_parameter_draws")
    if parameter_draws is not None:
        if not isinstance(parameter_draws, Sequence) or not parameter_draws:
            issues.append("state_model_parameter_draws_invalid")
        else:
            draw_ids: set[str] = set()
            total_weight = 0.0
            for raw in parameter_draws:
                if not isinstance(raw, Mapping):
                    issues.append("state_model_parameter_draw_invalid")
                    continue
                draw_id = str(raw.get("draw_id") or "").strip()
                try:
                    weight = float(raw.get("normalized_weight"))
                    temperature_bias = float(raw.get("latent_temperature_bias_c", 0.0))
                    tendency_bias = float(
                        raw.get("net_temperature_tendency_bias_c_per_hour", 0.0)
                    )
                    initial_scale = float(raw.get("initial_covariance_scale", 1.0))
                    process_scale = float(raw.get("process_noise_scale_multiplier", 1.0))
                except (TypeError, ValueError):
                    issues.append("state_model_parameter_draw_invalid")
                    continue
                if not draw_id or draw_id in draw_ids:
                    issues.append("state_model_parameter_draw_identity_invalid")
                draw_ids.add(draw_id)
                if (
                    not all(
                        math.isfinite(item)
                        for item in (
                            weight,
                            temperature_bias,
                            tendency_bias,
                            initial_scale,
                            process_scale,
                        )
                    )
                    or weight <= 0.0
                    or abs(temperature_bias) > 12.0
                    or abs(tendency_bias) > 6.0
                    or not 0.25 <= initial_scale <= 4.0
                    or not 0.25 <= process_scale <= 4.0
                ):
                    issues.append("state_model_parameter_draw_bounds_invalid")
                total_weight += weight
            if abs(total_weight - 1.0) > 1e-9:
                issues.append("state_model_parameter_draws_not_normalized")
    if issues:
        raise ValueError(";".join(sorted(set(issues))))
    artifact["artifact_sha256"] = computed
    return artifact


def parameter_draw_by_id(
    artifact: Mapping[str, Any], draw_id: str
) -> dict[str, float | str]:
    """Return one bounded physical-parameter draw with a fixed fallback."""

    draws = artifact.get("bounded_parameter_draws")
    if draws is None and draw_id in {"", "fixed", "moment_matched"}:
        return {
            "draw_id": "fixed",
            "normalized_weight": 1.0,
            "latent_temperature_bias_c": 0.0,
            "net_temperature_tendency_bias_c_per_hour": 0.0,
            "initial_covariance_scale": 1.0,
            "process_noise_scale_multiplier": 1.0,
        }
    for raw in draws or []:
        if isinstance(raw, Mapping) and str(raw.get("draw_id") or "") == draw_id:
            return {
                "draw_id": draw_id,
                "normalized_weight": float(raw.get("normalized_weight")),
                "latent_temperature_bias_c": float(
                    raw.get("latent_temperature_bias_c", 0.0)
                ),
                "net_temperature_tendency_bias_c_per_hour": float(
                    raw.get("net_temperature_tendency_bias_c_per_hour", 0.0)
                ),
                "initial_covariance_scale": float(
                    raw.get("initial_covariance_scale", 1.0)
                ),
                "process_noise_scale_multiplier": float(
                    raw.get("process_noise_scale_multiplier", 1.0)
                ),
            }
    raise ValueError(f"state_model_parameter_draw_missing:{draw_id}")


def validate_observation_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    artifact = dict(value)
    issues: list[str] = []
    if artifact.get("schema") != OBSERVATION_ARTIFACT_SCHEMA:
        issues.append("physical_observation_artifact_schema_invalid")
    declared = str(artifact.get("artifact_sha256") or "")
    computed = _artifact_sha(artifact)
    if declared and declared != computed:
        issues.append("physical_observation_artifact_sha256_mismatch")
    q_sigma = float(artifact.get("specific_humidity_observation_sigma") or 0.0)
    if not math.isfinite(q_sigma) or q_sigma <= 0.0:
        issues.append("physical_observation_q_sigma_invalid")
    models = artifact.get("regime_joint_models")
    if not isinstance(models, Mapping) or set(models) != set(REGIMES):
        issues.append("physical_observation_regime_model_set_invalid")
    else:
        required = {
            "cloud_fraction_mean", "cloud_fraction_sigma", "precipitation_probability",
            "convective_probability", "visibility_log_mean", "visibility_log_sigma",
            "wind_speed_mean", "wind_speed_sigma", "wind_gust_mean", "wind_gust_sigma",
            "pressure_tendency_mean", "pressure_tendency_sigma",
        }
        for regime, model in models.items():
            if not isinstance(model, Mapping) or not required.issubset(model):
                issues.append(f"physical_observation_regime_model_incomplete:{regime}")
    if issues:
        raise ValueError(";".join(sorted(set(issues))))
    artifact["artifact_sha256"] = computed
    return artifact


def _clip_state(values: Sequence[float], artifact: Mapping[str, Any]) -> list[float]:
    bounds = artifact["state_bounds"]
    return [
        min(float(bounds[name][1]), max(float(bounds[name][0]), float(values[index])))
        for index, name in enumerate(STATE_NAMES)
    ]


def _transition_probabilities(driver: PhysicalDriver, source: str, hours: float) -> dict[str, float]:
    raw = driver.regime_transition_per_hour.get(source)
    if not isinstance(raw, Mapping):
        raise ValueError(f"physical_driver_transition_row_missing:{source}")
    unknown = set(raw) - set(REGIMES)
    if unknown:
        raise ValueError(f"physical_driver_transition_target_unknown:{source}")
    rates = {target: float(raw.get(target, 0.0)) for target in REGIMES if target != source}
    if any(not math.isfinite(value) or value < 0.0 for value in rates.values()):
        raise ValueError(f"physical_driver_transition_rate_invalid:{source}")
    total_rate = sum(rates.values())
    exit_probability = 0.0 if total_rate <= 0.0 else 1.0 - math.exp(-total_rate * hours)
    result = {target: 0.0 for target in REGIMES}
    result[source] = 1.0 - exit_probability
    if total_rate > 0.0:
        for target, rate in rates.items():
            result[target] = exit_probability * rate / total_rate
    return result


def _propagate_component(
    component: MixtureComponent,
    *,
    target_regime: str,
    transition_probability: float,
    driver: PhysicalDriver,
    hours: float,
    artifact: Mapping[str, Any],
) -> MixtureComponent:
    mean = list(component.continuous_state_mean.as_tuple())
    g_index = STATE_INDEX["net_temperature_tendency"]
    q_index = STATE_INDEX["specific_humidity"]
    t_index = STATE_INDEX["latent_temperature"]
    w_index = STATE_INDEX["surface_wetness_memory"]
    c_index = STATE_INDEX["cold_pool_memory"]
    memory = artifact["memory_hours"]
    rho_g = math.exp(-hours / float(memory["net_temperature_tendency"][target_regime]))
    rho_q = math.exp(-hours / float(memory["specific_humidity"][target_regime]))
    rho_c = math.exp(-hours / float(memory["cold_pool_memory"][target_regime]))
    availability = driver.availability
    parameter_draw = parameter_draw_by_id(
        artifact, component.bounded_parameter_draw_id
    )
    target_g = (
        driver.net_tendency_target_c_per_hour
        + float(artifact["regime_tendency_offset_c_per_hour"][target_regime])
        + float(parameter_draw["net_temperature_tendency_bias_c_per_hour"])
    )
    endogenous_target_g = (
        float(artifact["regime_tendency_offset_c_per_hour"][target_regime])
        + float(parameter_draw["net_temperature_tendency_bias_c_per_hour"])
    )
    new_g = rho_g * mean[g_index] + (1.0 - rho_g) * (
        availability * target_g + (1.0 - availability) * endogenous_target_g
    )
    new_q = rho_q * mean[q_index] + (1.0 - rho_q) * (
        availability * driver.specific_humidity_target + (1.0 - availability) * mean[q_index]
    )
    new_t = mean[t_index] + 0.5 * hours * (mean[g_index] + new_g)
    new_w = mean[w_index] + hours * (
        driver.wetting_rate_per_hour * (1.0 - mean[w_index])
        - driver.drying_rate_per_hour * mean[w_index]
    )
    new_c = rho_c * mean[c_index] + hours * driver.cold_pool_source_c_per_hour2
    propagated_mean = _clip_state((new_t, new_q, new_g, new_w, new_c), artifact)

    size = len(STATE_NAMES)
    transition = identity(size)
    transition[t_index][g_index] = 0.5 * hours * (1.0 + rho_g)
    transition[g_index][g_index] = rho_g
    transition[q_index][q_index] = rho_q
    transition[w_index][w_index] = max(
        0.0,
        1.0 - hours * (driver.wetting_rate_per_hour + driver.drying_rate_per_hour),
    )
    transition[c_index][c_index] = rho_c
    covariance = [list(row) for row in component.continuous_state_covariance]
    covariance = matmul(matmul(transition, covariance), transpose(transition))
    process = artifact["process_covariance_per_hour"][target_regime]
    noise_scale = (
        hours
        * driver.process_noise_scale
        * (1.0 + (1.0 - availability))
        * float(parameter_draw["process_noise_scale_multiplier"])
    )
    covariance = add(covariance, [[float(item) * noise_scale for item in row] for row in process])
    covariance = symmetrize(covariance)
    component_id = "phys-" + canonical_sha256(
        {
            "previous": component.component_id,
            "target_regime": target_regime,
            "scenario": driver.scenario_id,
            "hours": hours,
            "mean": propagated_mean,
            "covariance": covariance,
            "bounded_parameter_draw_id": component.bounded_parameter_draw_id,
        }
    )
    return MixtureComponent(
        component_id=component_id,
        normalized_weight=component.normalized_weight * transition_probability,
        regime_state=target_regime,
        forcing_scenario_id=driver.scenario_id,
        continuous_state_mean=ContinuousState.from_tuple(tuple(propagated_mean)),
        continuous_state_covariance=tuple(tuple(item for item in row) for row in covariance),
        bounded_parameter_draw_id=component.bounded_parameter_draw_id,
    )


def _moment_match(rows: Sequence[MixtureComponent], component_id: str) -> MixtureComponent:
    total = sum(item.normalized_weight for item in rows)
    if total <= 0.0:
        raise ValueError("physical_mixture_merge_zero_mass")
    normalized = [item.normalized_weight / total for item in rows]
    means = [item.continuous_state_mean.as_tuple() for item in rows]
    mean = [sum(weight * values[index] for weight, values in zip(normalized, means)) for index in range(len(STATE_NAMES))]
    covariance = [[0.0 for _ in STATE_NAMES] for _ in STATE_NAMES]
    for weight, item, values in zip(normalized, rows, means):
        delta = [value - center for value, center in zip(values, mean)]
        between = outer(delta, delta)
        for i in range(len(STATE_NAMES)):
            for j in range(len(STATE_NAMES)):
                covariance[i][j] += weight * (item.continuous_state_covariance[i][j] + between[i][j])
    return MixtureComponent(
        component_id=component_id,
        normalized_weight=total,
        regime_state=rows[0].regime_state,
        forcing_scenario_id=rows[0].forcing_scenario_id,
        continuous_state_mean=ContinuousState.from_tuple(tuple(mean)),
        continuous_state_covariance=tuple(tuple(row) for row in symmetrize(covariance)),
        bounded_parameter_draw_id=rows[0].bounded_parameter_draw_id,
    )


def _merge_and_normalize(rows: Iterable[MixtureComponent]) -> PhysicalStatePosterior:
    grouped: dict[tuple[str, str, str], list[MixtureComponent]] = {}
    for item in rows:
        if item.normalized_weight <= 0.0:
            continue
        grouped.setdefault(
            (
                item.forcing_scenario_id,
                item.regime_state,
                item.bounded_parameter_draw_id,
            ),
            [],
        ).append(item)
    merged = [
        _moment_match(
            items,
            "phys-"
            + canonical_sha256(
                {
                    "scenario": key[0],
                    "regime": key[1],
                    "parameter_draw": key[2],
                    "members": [item.component_id for item in items],
                }
            ),
        )
        for key, items in sorted(grouped.items())
    ]
    total = sum(item.normalized_weight for item in merged)
    if total <= 0.0:
        raise ValueError("physical_prediction_zero_mass")
    normalized = tuple(replace(item, normalized_weight=item.normalized_weight / total) for item in merged)
    return PhysicalStatePosterior(components=normalized, normalization_error=abs(total - 1.0))


def predict_to_event(
    *,
    previous_posterior: PhysicalStatePosterior,
    previous_checkpoint_sha256: str,
    previous_valid_at: str,
    canonical_event_id: str,
    event_valid_at: str,
    drivers: Mapping[str, PhysicalDriver],
    state_model_artifact: Mapping[str, Any],
) -> PreEventPredictiveState:
    """Advance the physical state without reading the current observation."""

    artifact = validate_state_model_artifact(state_model_artifact)
    start = _time(previous_valid_at)
    end = _time(event_valid_at)
    delta_minutes = (end - start).total_seconds() / 60.0
    if delta_minutes < 0.0:
        raise ValueError("physical_predict_out_of_order_event")
    if not canonical_event_id or len(previous_checkpoint_sha256) != 64:
        raise ValueError("physical_predict_identity_invalid")
    scenario_ids = {item.forcing_scenario_id for item in previous_posterior.components}
    if set(drivers) != scenario_ids:
        raise ValueError("physical_predict_driver_scenario_set_mismatch")
    forcing_hashes = {driver.forcing_generation_sha256 for driver in drivers.values()}
    if len(forcing_hashes) != 1:
        raise ValueError("physical_predict_driver_generation_mismatch")
    maximum = float(artifact["max_substep_minutes"])
    steps = max(1, math.ceil(delta_minutes / maximum))
    substep_hours = delta_minutes / steps / 60.0
    posterior = previous_posterior
    for _ in range(steps):
        propagated: list[MixtureComponent] = []
        for component in posterior.components:
            driver = drivers[component.forcing_scenario_id]
            transitions = _transition_probabilities(driver, component.regime_state, substep_hours)
            for target_regime, probability in transitions.items():
                if probability <= 0.0:
                    continue
                propagated.append(
                    _propagate_component(
                        component,
                        target_regime=target_regime,
                        transition_probability=probability,
                        driver=driver,
                        hours=substep_hours,
                        artifact=artifact,
                    )
                )
        posterior = _merge_and_normalize(propagated)
    return PreEventPredictiveState(
        schema=PREDICTION_SCHEMA,
        previous_checkpoint_sha256=previous_checkpoint_sha256,
        canonical_event_id=canonical_event_id,
        event_valid_at=event_valid_at,
        delta_minutes=delta_minutes,
        forcing_generation_sha256=next(iter(forcing_hashes)),
        state_model_artifact_sha256=str(artifact["artifact_sha256"]),
        posterior=posterior,
        current_observation_consumed=False,
    )


def pre_event_prediction_sha256(prediction: PreEventPredictiveState) -> str:
    return canonical_sha256(prediction)


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / _SQRT_2PI


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _truncate_temperature(
    mean: Sequence[float], covariance: Sequence[Sequence[float]], lower: float, upper: float
) -> tuple[list[float], list[list[float]], float]:
    index = STATE_INDEX["latent_temperature"]
    variance = max(float(covariance[index][index]), 1e-9)
    sigma = math.sqrt(variance)
    alpha = (lower - mean[index]) / sigma
    beta = (upper - mean[index]) / sigma
    raw_mass = _normal_cdf(beta) - _normal_cdf(alpha)
    if raw_mass <= 1e-14:
        # erf loses the difference of two extreme tail CDFs.  Use the
        # moment-matched uniform encoding interval as the numerically stable
        # equivalent and keep the conditioned temperature inside the exact
        # encoded interval.  This path is deterministic and still consumes
        # the temperature only once.
        center = 0.5 * (lower + upper)
        width = upper - lower
        updated_mean, updated_covariance, log_density = _scalar_gaussian_update(
            mean,
            covariance,
            state_index=index,
            observed=center,
            observation_variance=width * width / 12.0,
        )
        updated_mean[index] = min(upper, max(lower, updated_mean[index]))
        return updated_mean, updated_covariance, log_density + math.log(width)
    mass = max(_MIN_PROBABILITY, raw_mass)
    lam = (_normal_pdf(alpha) - _normal_pdf(beta)) / mass
    # Multivariate Gaussian conditioning on one coordinate lying in [a,b].
    # Var(X|a<X<b) = var * (1 + term - lambda^2), therefore the
    # covariance rank-one reduction is (lambda^2 - term), not their sum.
    term = (alpha * _normal_pdf(alpha) - beta * _normal_pdf(beta)) / mass
    delta = lam * lam - term
    column = [float(covariance[row][index]) for row in range(len(STATE_NAMES))]
    conditioned_mean = [float(value) + column[row] / sigma * lam for row, value in enumerate(mean)]
    reduction = outer(column, column)
    conditioned_covariance = [
        [float(covariance[i][j]) - reduction[i][j] / variance * delta for j in range(len(STATE_NAMES))]
        for i in range(len(STATE_NAMES))
    ]
    return conditioned_mean, symmetrize(conditioned_covariance), math.log(mass)


def _scalar_gaussian_update(
    mean: Sequence[float],
    covariance: Sequence[Sequence[float]],
    *,
    state_index: int,
    observed: float,
    observation_variance: float,
) -> tuple[list[float], list[list[float]], float]:
    innovation_variance = float(covariance[state_index][state_index]) + observation_variance
    if innovation_variance <= 0.0:
        raise ValueError("physical_observation_innovation_variance_invalid")
    innovation = observed - float(mean[state_index])
    gain = [float(covariance[row][state_index]) / innovation_variance for row in range(len(STATE_NAMES))]
    updated_mean = [float(value) + gain[row] * innovation for row, value in enumerate(mean)]
    updated_covariance = [
        [float(covariance[i][j]) - gain[i] * float(covariance[state_index][j]) for j in range(len(STATE_NAMES))]
        for i in range(len(STATE_NAMES))
    ]
    log_likelihood = -0.5 * (
        math.log(2.0 * math.pi * innovation_variance) + innovation * innovation / innovation_variance
    )
    return updated_mean, symmetrize(updated_covariance), log_likelihood


def _specific_humidity(dewpoint_c: float, pressure_pa: float) -> float:
    vapor_pressure_hpa = 6.112 * math.exp(17.67 * dewpoint_c / (dewpoint_c + 243.5))
    pressure_hpa = pressure_pa / 100.0
    denominator = max(1e-6, pressure_hpa - 0.378 * vapor_pressure_hpa)
    return 0.622 * vapor_pressure_hpa / denominator


def _gaussian_log_score(value: float, mean: float, sigma: float) -> float:
    sigma = max(float(sigma), 1e-9)
    residual = (float(value) - float(mean)) / sigma
    return -0.5 * residual * residual - math.log(sigma * _SQRT_2PI)


def _bernoulli_log_score(value: bool, probability: float) -> float:
    probability = min(1.0 - 1e-9, max(1e-9, float(probability)))
    return math.log(probability if value else 1.0 - probability)


def _regime_joint_log_score(
    observation: CanonicalPhysicalObservation,
    model: Mapping[str, Any],
) -> tuple[float, list[str], list[str]]:
    score = 0.0
    consumed: list[str] = []
    marginalized: list[str] = []
    gaussian_fields = (
        ("cloud_fraction", observation.cloud_fraction, "cloud_fraction_mean", "cloud_fraction_sigma", False),
        ("visibility_m", observation.visibility_m, "visibility_log_mean", "visibility_log_sigma", True),
        ("wind_speed_mps", observation.wind_speed_mps, "wind_speed_mean", "wind_speed_sigma", False),
        ("wind_gust_mps", observation.wind_gust_mps, "wind_gust_mean", "wind_gust_sigma", False),
        ("pressure_tendency_pa_per_hour", observation.pressure_tendency_pa_per_hour, "pressure_tendency_mean", "pressure_tendency_sigma", False),
    )
    for field, value, mean_field, sigma_field, log_transform in gaussian_fields:
        if value is None:
            marginalized.append(field)
            continue
        transformed = math.log(max(1.0, float(value))) if log_transform else float(value)
        score += _gaussian_log_score(transformed, float(model[mean_field]), float(model[sigma_field]))
        consumed.append(field)
    for field, value, probability_field in (
        ("precipitation_present", observation.precipitation_present, "precipitation_probability"),
        ("convective_present", observation.convective_present, "convective_probability"),
    ):
        if value is None:
            marginalized.append(field)
            continue
        score += _bernoulli_log_score(bool(value), float(model[probability_field]))
        consumed.append(field)
    # A clear-sky METAR is an observation of the regime state, not a forecast
    # temperature input. The optional structural likelihood keeps a clear
    # state from diffusing into cold/evening regimes during the unobserved
    # projection window. Older pure-kernel test artifacts omit it.
    clear_sky_likelihood = model.get("clear_sky_observation_likelihood")
    clear_sky_observed = (
        observation.cloud_fraction is not None
        and observation.cloud_fraction <= 0.01
        and observation.precipitation_present is False
        and observation.convective_present is not True
    )
    if clear_sky_observed and clear_sky_likelihood is not None:
        score += _bernoulli_log_score(True, float(clear_sky_likelihood))
        consumed.append("clear_sky_observation_likelihood")
    return score, consumed, marginalized


def build_joint_observation_likelihood(
    *,
    prediction: PreEventPredictiveState,
    observation: CanonicalPhysicalObservation,
    observation_artifact: Mapping[str, Any],
) -> JointObservationLikelihood:
    """Consume every available current-event field in one joint emission."""

    if prediction.current_observation_consumed:
        raise ValueError("physical_current_observation_already_consumed")
    if observation.canonical_event_id != prediction.canonical_event_id:
        raise ValueError("physical_observation_event_binding_mismatch")
    if observation.event_valid_at != prediction.event_valid_at:
        raise ValueError("physical_observation_time_binding_mismatch")
    if observation.quality_state not in {"accepted_primary", "accepted_revision"}:
        raise ValueError("physical_observation_quality_not_accepted")
    artifact = validate_observation_artifact(observation_artifact)
    prediction_hash = pre_event_prediction_sha256(prediction)
    conditioning: list[ComponentConditioning] = []
    all_consumed: set[str] = set()
    all_marginalized: set[str] = set()
    for component in prediction.posterior.components:
        mean = list(component.continuous_state_mean.as_tuple())
        covariance = [list(row) for row in component.continuous_state_covariance]
        temperature_log_score = 0.0
        humidity_log_score = 0.0
        consumed: list[str] = []
        marginalized: list[str] = []
        if observation.reported_temperature_c is not None:
            center = float(observation.reported_temperature_c)
            mean, covariance, temperature_log_score = _truncate_temperature(
                mean, covariance, center - 0.5, center + 0.5
            )
            consumed.append("reported_temperature_c")
        else:
            marginalized.append("reported_temperature_c")
        if observation.reported_dewpoint_c is not None and observation.pressure_pa is not None:
            q_observed = _specific_humidity(
                float(observation.reported_dewpoint_c), float(observation.pressure_pa)
            )
            mean, covariance, humidity_log_score = _scalar_gaussian_update(
                mean,
                covariance,
                state_index=STATE_INDEX["specific_humidity"],
                observed=q_observed,
                observation_variance=float(artifact["specific_humidity_observation_sigma"]) ** 2,
            )
            consumed.extend(("reported_dewpoint_c", "pressure_pa_for_specific_humidity"))
        else:
            if observation.reported_dewpoint_c is None:
                marginalized.append("reported_dewpoint_c")
            if observation.pressure_pa is None:
                marginalized.append("pressure_pa_for_specific_humidity")
        regime_score, regime_consumed, regime_marginalized = _regime_joint_log_score(
            observation, artifact["regime_joint_models"][component.regime_state]
        )
        consumed.extend(regime_consumed)
        marginalized.extend(regime_marginalized)
        clipped = _clip_state(mean, {
            "state_bounds": artifact.get("state_bounds") or {
                "latent_temperature": [-80.0, 70.0],
                "specific_humidity": [0.0, 0.06],
                "net_temperature_tendency": [-20.0, 20.0],
                "surface_wetness_memory": [0.0, 1.0],
                "cold_pool_memory": [0.0, 20.0],
            }
        })
        total_score = temperature_log_score + humidity_log_score + regime_score
        conditioning.append(
            ComponentConditioning(
                component_id=component.component_id,
                log_likelihood=total_score,
                conditioned_mean=ContinuousState.from_tuple(tuple(clipped)),
                conditioned_covariance=tuple(tuple(row) for row in covariance),
                component_trace={
                    "temperature_interval_log_likelihood": temperature_log_score,
                    "specific_humidity_log_likelihood": humidity_log_score,
                    "joint_weather_regime_log_likelihood": regime_score,
                    "total_log_likelihood": total_score,
                    "temperature_and_derived_rate_double_counted": False,
                    "regime_state": component.regime_state,
                },
            )
        )
        all_consumed.update(consumed)
        all_marginalized.update(marginalized)
    all_marginalized.difference_update(all_consumed)
    return JointObservationLikelihood(
        schema=LIKELIHOOD_SCHEMA,
        canonical_event_id=observation.canonical_event_id,
        pre_event_prediction_sha256=prediction_hash,
        observation_artifact_sha256=str(artifact["artifact_sha256"]),
        component_conditioning=tuple(conditioning),
        evidence_fields_consumed=tuple(sorted(all_consumed)),
        evidence_fields_marginalized=tuple(sorted(all_marginalized)),
        current_event_consumption_count=1,
    )


def observation_likelihood_sha256(likelihood: JointObservationLikelihood) -> str:
    return canonical_sha256(likelihood)


def condition_pre_event_state(
    *,
    prediction: PreEventPredictiveState,
    likelihood: JointObservationLikelihood,
    observation: CanonicalPhysicalObservation,
    previous_observed_contract_max_c: int | None,
) -> ConditionedPhysicalState:
    prediction_hash = pre_event_prediction_sha256(prediction)
    if likelihood.pre_event_prediction_sha256 != prediction_hash:
        raise ValueError("physical_likelihood_prediction_binding_mismatch")
    if likelihood.canonical_event_id != observation.canonical_event_id:
        raise ValueError("physical_likelihood_event_binding_mismatch")
    if likelihood.current_event_consumption_count != 1:
        raise ValueError("physical_current_event_consumption_count_invalid")
    conditioning = {item.component_id: item for item in likelihood.component_conditioning}
    if set(conditioning) != {item.component_id for item in prediction.posterior.components}:
        raise ValueError("physical_likelihood_component_set_mismatch")
    raw_logs = [
        math.log(max(_MIN_PROBABILITY, item.normalized_weight))
        + conditioning[item.component_id].log_likelihood
        for item in prediction.posterior.components
    ]
    offset = max(raw_logs)
    raw_weights = [math.exp(value - offset) for value in raw_logs]
    normalizer = sum(raw_weights)
    if not math.isfinite(normalizer) or normalizer <= 0.0:
        raise ValueError("physical_observation_zeroed_all_mass")
    components = []
    for prior, raw_weight in zip(prediction.posterior.components, raw_weights):
        update = conditioning[prior.component_id]
        components.append(
            MixtureComponent(
                component_id="phys-" + canonical_sha256(
                    {"prior": prior.component_id, "event": observation.canonical_event_id, "likelihood": update.log_likelihood}
                ),
                normalized_weight=raw_weight / normalizer,
                regime_state=prior.regime_state,
                forcing_scenario_id=prior.forcing_scenario_id,
                continuous_state_mean=update.conditioned_mean,
                continuous_state_covariance=update.conditioned_covariance,
                bounded_parameter_draw_id=prior.bounded_parameter_draw_id,
            )
        )
    posterior = PhysicalStatePosterior(components=tuple(components))
    observed = previous_observed_contract_max_c
    if observation.is_contract_report_slot and observation.reported_temperature_c is not None:
        observed = max(
            (value for value in (observed, int(observation.reported_temperature_c)) if value is not None),
            default=None,
        )
    return ConditionedPhysicalState(
        canonical_event_id=observation.canonical_event_id,
        pre_event_prediction_sha256=prediction_hash,
        observation_likelihood_sha256=observation_likelihood_sha256(likelihood),
        posterior=posterior,
        observed_contract_max_c=observed,
        normalizer_log=offset + math.log(normalizer),
        invariant_report={
            "pre_event_predict_excluded_current_observation": not prediction.current_observation_consumed,
            "current_event_consumed_exactly_once": True,
            "temperature_quantization_condition_applied": observation.reported_temperature_c is not None,
            "non_contract_speci_does_not_raise_x_bound": not (
                observation.report_type == "SPECI" and not observation.is_contract_report_slot
                and observed != previous_observed_contract_max_c
            ),
            "no_post_emission_transition": True,
            "joint_normalized": True,
        },
    )


__all__ = [
    "OBSERVATION_ARTIFACT_SCHEMA",
    "STATE_MODEL_ARTIFACT_SCHEMA",
    "build_joint_observation_likelihood",
    "canonical_sha256",
    "condition_pre_event_state",
    "observation_likelihood_sha256",
    "pre_event_prediction_sha256",
    "predict_to_event",
    "parameter_draw_by_id",
    "validate_observation_artifact",
    "validate_state_model_artifact",
]
