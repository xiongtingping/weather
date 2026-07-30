"""Adapters from frozen v6 canonical/weak inputs into the physical kernel."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Any, Mapping, Sequence

from .contracts import (
    CanonicalPhysicalObservation,
    ContinuousState,
    MixtureComponent,
    PhysicalDriver,
    PhysicalStatePosterior,
    REGIMES,
)
from .core import canonical_sha256


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("parsed_observation")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(_text(event.get("parsed_observation_json")))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _cloud_fraction(payload: Mapping[str, Any]) -> float | None:
    direct = _finite(payload.get("cloud_fraction"))
    if direct is not None:
        return min(1.0, max(0.0, direct / 100.0 if direct > 1.0 else direct))
    cloud = _text(payload.get("cloud_text")).upper()
    if "OVC" in cloud or "VV" in cloud:
        return 1.0
    if "BKN" in cloud:
        return 0.75
    if "SCT" in cloud:
        return 0.375
    if "FEW" in cloud:
        return 0.125
    # CAVOK is an official clear-sky/visibility condition. Treating it as
    # missing discards a strong regime observation after a clear METAR.
    if any(code in cloud for code in ("CAVOK", "SKC", "CLR", "NSC", "NCD")):
        return 0.0
    return None


def _pressure_pa(payload: Mapping[str, Any]) -> float | None:
    direct = _finite(payload.get("pressure_pa"))
    if direct is not None:
        return direct
    qnh = _finite(payload.get("qnh_hpa") or payload.get("pressure_hpa"))
    if qnh is not None:
        return qnh * 100.0
    altimeter = _finite(payload.get("altimeter_inhg"))
    return altimeter * 3386.389 if altimeter is not None else None


def canonical_observation_from_event(event: Mapping[str, Any]) -> CanonicalPhysicalObservation:
    payload = _payload(event)
    report_type = _text(event.get("report_type")).upper()
    temperature = _finite(payload.get("temperature_c"))
    dewpoint = _finite(payload.get("dewpoint_c"))
    if temperature is not None and not temperature.is_integer():
        raise ValueError("physical_adapter_reported_temperature_not_integer")
    if dewpoint is not None and not dewpoint.is_integer():
        raise ValueError("physical_adapter_reported_dewpoint_not_integer")
    weather = _text(payload.get("weather_text")).upper()
    cloud = _text(payload.get("cloud_text")).upper()
    precipitation = (
        any(code in weather for code in ("RA", "DZ", "SN", "SG", "PL", "GR", "GS"))
        if weather or "weather_text" in payload
        else None
    )
    convective = (
        any(code in weather or code in cloud for code in ("TS", "CB", "TCU"))
        if weather or cloud
        else None
    )
    explicit_contract = event.get("is_contract_report_slot")
    is_contract = bool(explicit_contract) if explicit_contract is not None else report_type == "METAR"
    na_reasons = payload.get("field_na_reasons")
    return CanonicalPhysicalObservation(
        canonical_event_id=_text(event.get("canonical_observation_event_id")),
        event_valid_at=_text(event.get("valid_time")),
        report_type=report_type,
        is_contract_report_slot=is_contract,
        reported_temperature_c=int(temperature) if temperature is not None else None,
        reported_dewpoint_c=int(dewpoint) if dewpoint is not None else None,
        pressure_pa=_pressure_pa(payload),
        cloud_fraction=_cloud_fraction(payload),
        precipitation_present=precipitation,
        convective_present=convective,
        visibility_m=_finite(payload.get("visibility_m")),
        wind_speed_mps=_finite(payload.get("wind_speed_mps")),
        wind_gust_mps=_finite(payload.get("wind_gust_mps")),
        pressure_tendency_pa_per_hour=_finite(payload.get("pressure_tendency_pa_per_hour")),
        quality_state=(
            "accepted_revision"
            if _text(event.get("state")) == "accepted_revision"
            else "accepted_primary"
        ),
        field_na_reasons=dict(na_reasons) if isinstance(na_reasons, Mapping) else {},
    )


_APPROVED_NESTED_DRIVER_FIELDS = (
    "dewpoint_c",
    "pressure_pa",
    "pressure_hpa",
    "specific_humidity",
    "precipitation_probability",
    "cloud_suppression_probability",
    "cloud_cover_probability",
)


def _physical_driver_surface(point: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only approved non-temperature controls from source point schemas.

    Official NWP curve points store direct physical fields under
    ``physical_regime.physical_evidence``.  The physical state model must not
    consume ``temperature_c`` or any peak/temperature-trajectory field, but it
    may consume explicitly approved exogenous controls.  Keep this adapter
    schema-aware so a nested source payload is not silently treated as a
    missing-driver payload.
    """

    result = dict(point)
    physical = point.get("physical_regime")
    physical = physical if isinstance(physical, Mapping) else {}
    evidence = physical.get("physical_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    for field in _APPROVED_NESTED_DRIVER_FIELDS:
        if result.get(field) is None:
            value = physical.get(field)
            if value is None:
                value = evidence.get(field)
            if value is not None:
                result[field] = value
    if result.get("cloud_suppression_probability") is None:
        cloud_cover = _finite(evidence.get("total_cloud_cover_pct"))
        if cloud_cover is not None:
            result["cloud_suppression_probability"] = min(1.0, max(0.0, cloud_cover / 100.0))
    return result


def _curve_points(path: Mapping[str, Any]) -> list[tuple[datetime, dict[str, Any]]]:
    rows = []
    for raw in path.get("hourly_curve") or []:
        if not isinstance(raw, Mapping):
            continue
        valid = _time(raw.get("valid_time_utc") or raw.get("valid_time"))
        if valid is not None:
            rows.append((valid, _physical_driver_surface(raw)))
    return sorted(rows, key=lambda item: item[0])


def _interpolate(points: Sequence[tuple[datetime, Mapping[str, Any]]], when: datetime, field: str) -> float | None:
    values = [(time, _finite(row.get(field))) for time, row in points]
    values = [(time, value) for time, value in values if value is not None]
    if not values:
        return None
    exact = [value for time, value in values if time == when]
    if exact:
        return float(exact[-1])
    before = [(time, value) for time, value in values if time < when]
    after = [(time, value) for time, value in values if time > when]
    if not before or not after:
        return None
    left_time, left = before[-1]
    right_time, right = after[0]
    fraction = (when - left_time).total_seconds() / (right_time - left_time).total_seconds()
    return float(left) + fraction * (float(right) - float(left))


def _specific_humidity_from_dewpoint(dewpoint_c: float, pressure_pa: float = 100000.0) -> float:
    vapor_hpa = 6.112 * math.exp(17.67 * dewpoint_c / (dewpoint_c + 243.5))
    pressure_hpa = pressure_pa / 100.0
    return 0.622 * vapor_hpa / max(1e-6, pressure_hpa - 0.378 * vapor_hpa)


def _paths(weak_likelihood: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    container = weak_likelihood.get("forecast_scenario_paths")
    if not isinstance(container, Mapping):
        raise ValueError("physical_adapter_forecast_paths_missing")
    return {
        _text(row.get("scenario_id")): dict(row)
        for row in container.get("paths") or []
        if isinstance(row, Mapping) and _text(row.get("scenario_id"))
    }


def _physical_temperature_prior(
    prior_context: Mapping[str, Any],
) -> tuple[tuple[float, float], ...] | None:
    """Read the already-materialized physical-X prior for P(Z0).

    The v6 input builder owns the station/basin/season prior and binds it by
    content hash.  The physical composer must consume that surface as the
    initial latent-temperature measure; weak NWP/TAF drivers remain controls
    for the subsequent pre-event prediction.  Empty context is retained only
    for pure-kernel/evaluation callers that intentionally exercise the adapter
    without the production P0 envelope.
    """

    if not prior_context:
        return None
    # The offline replay adapter may expose legacy Y only to recover scenario
    # weights.  It is explicitly not a physical Z0 prior and must never alter
    # the latent-temperature initialization.
    if prior_context.get("evaluation_only_legacy_y_used_for_scenario_weighting") is True:
        return None
    if (
        prior_context.get("schema") != "full_physical_p0_prior_context_v1"
        or prior_context.get("state") != "available"
        or prior_context.get("production_runtime_eligible") is not True
        or prior_context.get("shadow_only") is not False
        or len(_text(prior_context.get("artifact_sha256"))) != 64
        or len(_text(prior_context.get("prior_model_artifact_sha256"))) != 64
    ):
        raise ValueError("physical_adapter_prior_context_not_production_bound")
    continuous = prior_context.get("continuous_distribution")
    if not isinstance(continuous, Mapping):
        raise ValueError("physical_adapter_prior_continuous_distribution_missing")
    rows = continuous.get("grid")
    if not isinstance(rows, Sequence) or not rows:
        raise ValueError("physical_adapter_prior_temperature_grid_missing")
    parsed: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("physical_adapter_prior_temperature_row_invalid")
        temperature = _finite(row.get("temp_c"))
        mass = _finite(row.get("mass"))
        if temperature is None or mass is None or mass < 0.0:
            raise ValueError("physical_adapter_prior_temperature_mass_invalid")
        parsed.append((temperature, mass))
    if len({temperature for temperature, _ in parsed}) != len(parsed):
        raise ValueError("physical_adapter_prior_temperature_duplicate")
    parsed.sort(key=lambda item: item[0])
    total = sum(mass for _, mass in parsed)
    if not math.isfinite(total) or total <= 0.0:
        raise ValueError("physical_adapter_prior_temperature_mass_zero")
    normalized = tuple((temperature, mass / total) for temperature, mass in parsed if mass > 0.0)
    if not normalized or abs(sum(mass for _, mass in normalized) - 1.0) > 1e-9:
        raise ValueError("physical_adapter_prior_temperature_mass_not_normalized")
    return normalized


def drivers_from_weak_likelihood(
    *,
    weak_likelihood: Mapping[str, Any],
    valid_at: str,
    scenario_ids: Sequence[str],
    state_model_artifact: Mapping[str, Any],
) -> dict[str, PhysicalDriver]:
    when = _time(valid_at)
    if when is None:
        raise ValueError("physical_adapter_driver_time_invalid")
    paths = _paths(weak_likelihood)
    generation = _text(
        (weak_likelihood.get("forecast_scenario_paths") or {}).get("sha256")
        if isinstance(weak_likelihood.get("forecast_scenario_paths"), Mapping)
        else ""
    )
    if len(generation) != 64:
        raise ValueError("physical_adapter_driver_generation_invalid")
    defaults = state_model_artifact.get("missing_driver_defaults")
    transitions = state_model_artifact.get("regime_transition_per_hour")
    if not isinstance(defaults, Mapping) or not isinstance(transitions, Mapping):
        raise ValueError("physical_adapter_state_model_driver_surface_missing")
    result = {}
    for scenario_id in sorted(set(scenario_ids)):
        path = paths.get(scenario_id, {})
        points = _curve_points(path)
        # The source curve is deliberately not converted into a temperature
        # slope or extrapolated temperature path.  The physical state model
        # owns its transition; source fields below only provide approved
        # exogenous controls such as humidity/cloud/precipitation.
        rate = None
        dewpoint = _interpolate(points, when, "dewpoint_c")
        q = _interpolate(points, when, "specific_humidity")
        precipitation = _interpolate(points, when, "precipitation_probability")
        cloud = _interpolate(points, when, "cloud_suppression_probability")
        if cloud is None:
            cloud_cover = _interpolate(points, when, "total_cloud_cover_pct")
            cloud = cloud_cover / 100.0 if cloud_cover is not None else None
        missing = []
        if rate is None:
            missing.append("net_tendency_target_c_per_hour")
            rate = float(defaults["net_tendency_target_c_per_hour"])
        if q is None and dewpoint is not None:
            q = _specific_humidity_from_dewpoint(dewpoint)
        if q is None:
            missing.append("specific_humidity_target")
            q = float(defaults["specific_humidity_target"])
        if precipitation is None:
            missing.append("precipitation_probability")
            precipitation = float(defaults["precipitation_probability"])
        if precipitation > 1.0:
            precipitation /= 100.0
        precipitation = min(1.0, max(0.0, precipitation))
        if cloud is None:
            missing.append("cloud_suppression_probability")
            cloud = float(defaults["cloud_suppression_probability"])
        if cloud > 1.0:
            cloud /= 100.0
        cloud = min(1.0, max(0.0, cloud))
        result[scenario_id] = PhysicalDriver(
            scenario_id=scenario_id,
            forcing_generation_sha256=generation,
            net_tendency_target_c_per_hour=float(rate),
            specific_humidity_target=float(q),
            wetting_rate_per_hour=(
                float(state_model_artifact["driver_coefficients"]["wetting_from_precipitation"])
                * precipitation
            ),
            drying_rate_per_hour=(
                float(state_model_artifact["driver_coefficients"]["drying_clear_fraction"])
                * (1.0 - cloud)
            ),
            cold_pool_source_c_per_hour2=(
                float(state_model_artifact["driver_coefficients"]["cold_pool_from_precipitation"])
                * precipitation
            ),
            process_noise_scale=(
                1.0 + len(missing) * float(state_model_artifact["missing_driver_noise_increment"])
            ),
            regime_transition_per_hour=transitions,
            availability=max(0.0, 1.0 - len(missing) / 4.0),
            na_reasons={field: "weak_driver_field_missing_approved_surface" for field in missing},
        )
    return result


def _scenario_weights(
    *, prior_context: Mapping[str, Any], weak_likelihood: Mapping[str, Any], scenario_ids: Sequence[str]
) -> tuple[dict[str, float], str]:
    explicit = weak_likelihood.get("physical_scenario_prior_weights")
    if isinstance(explicit, Mapping):
        weights = {scenario: float(explicit.get(scenario, 0.0)) for scenario in scenario_ids}
        method = "physical_scenario_prior_weights"
    elif any(
        _finite(path.get("source_mode_probability")) is not None
        for path in _paths(weak_likelihood).values()
    ):
        paths = _paths(weak_likelihood)
        weights = {
            scenario: max(
                0.0,
                float(
                    _finite(paths[scenario].get("source_mode_probability"))
                    or 0.0
                ),
            )
            for scenario in scenario_ids
        }
        method = "forecast_scenario_mode_probabilities"
    else:
        if weak_likelihood.get("evaluation_only_allow_legacy_scenario_weight_adapter") is not True:
            raise ValueError("physical_scenario_prior_weight_surface_missing")
        responsibilities = weak_likelihood.get("candidate_scenario_responsibilities")
        prior_grid = (prior_context.get("continuous_distribution") or {}).get("grid") if isinstance(prior_context.get("continuous_distribution"), Mapping) else []
        prior_mass = {
            float(row["temp_c"]): float(row["mass"])
            for row in prior_grid or [] if isinstance(row, Mapping)
        }
        weights = {scenario: 0.0 for scenario in scenario_ids}
        if isinstance(responsibilities, Mapping):
            for row in responsibilities.get("rows") or []:
                if not isinstance(row, Mapping):
                    continue
                mass = prior_mass.get(float(row.get("continuous_y_c")), 0.0)
                for scenario in row.get("scenarios") or []:
                    if isinstance(scenario, Mapping) and _text(scenario.get("scenario_id")) in weights:
                        weights[_text(scenario.get("scenario_id"))] += mass * float(scenario.get("responsibility") or 0.0)
        method = "evaluation_only_prior_weighted_scenario_responsibility_adapter"
    total = sum(weights.values())
    if total <= 0.0:
        weights = {scenario: 1.0 / len(scenario_ids) for scenario in scenario_ids}
        if weak_likelihood.get("evaluation_only_allow_legacy_scenario_weight_adapter") is not True:
            raise ValueError("physical_scenario_prior_weights_zero_mass")
        method = "evaluation_only_equal_weight_missing_scenario_surface"
    else:
        weights = {scenario: value / total for scenario, value in weights.items()}
    return weights, method


def scenario_weights_from_weak_likelihood(
    *,
    weak_likelihood: Mapping[str, Any],
    prior_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], str, str]:
    """Return the explicit forcing-scenario distribution for one generation.

    Production physical revisions get their weights from the physical
    scenario surface carried by the weak forecast generation.  The optional
    prior context only supports the explicitly evaluation-only adapter already
    guarded inside :func:`_scenario_weights`.
    """

    paths = _paths(weak_likelihood)
    if not paths:
        raise ValueError("physical_scenario_paths_empty")
    scenario_ids = tuple(sorted(paths))
    weights, method = _scenario_weights(
        prior_context=dict(prior_context or {}),
        weak_likelihood=weak_likelihood,
        scenario_ids=scenario_ids,
    )
    container = weak_likelihood.get("forecast_scenario_paths")
    generation = _text(container.get("sha256")) if isinstance(container, Mapping) else ""
    if len(generation) != 64:
        raise ValueError("physical_scenario_generation_invalid")
    return weights, method, generation


def initialize_physical_posterior(
    *,
    prior_context: Mapping[str, Any],
    weak_likelihood: Mapping[str, Any],
    state_reference_valid_at: str,
    state_model_artifact: Mapping[str, Any],
) -> tuple[PhysicalStatePosterior, dict[str, Any]]:
    paths = _paths(weak_likelihood)
    if not paths:
        raise ValueError("physical_p0_scenario_paths_empty")
    scenario_ids = tuple(sorted(paths))
    weights, weight_method = _scenario_weights(
        prior_context=prior_context, weak_likelihood=weak_likelihood, scenario_ids=scenario_ids
    )
    drivers = drivers_from_weak_likelihood(
        weak_likelihood=weak_likelihood,
        valid_at=state_reference_valid_at,
        scenario_ids=scenario_ids,
        state_model_artifact=state_model_artifact,
    )
    when = _time(state_reference_valid_at)
    assert when is not None
    regime_map = state_model_artifact.get("forecast_regime_map")
    defaults = state_model_artifact.get("initial_state_defaults")
    covariance_by_regime = state_model_artifact.get("initial_covariance_by_regime")
    if not all(isinstance(item, Mapping) for item in (regime_map, defaults, covariance_by_regime)):
        raise ValueError("physical_p0_state_model_initialization_surface_missing")
    raw_draws = state_model_artifact.get("bounded_parameter_draws")
    parameter_draws = (
        [dict(row) for row in raw_draws if isinstance(row, Mapping)]
        if isinstance(raw_draws, Sequence)
        else [
            {
                "draw_id": "fixed",
                "normalized_weight": 1.0,
                "latent_temperature_bias_c": 0.0,
                "net_temperature_tendency_bias_c_per_hour": 0.0,
                "initial_covariance_scale": 1.0,
                "process_noise_scale_multiplier": 1.0,
            }
        ]
    )
    components = []
    prior_temperature = _physical_temperature_prior(prior_context)
    default_initial_temperature_by_scenario: dict[str, float] = {}
    for scenario_id in scenario_ids:
        path = paths[scenario_id]
        points = _curve_points(path)
        default_initial_temperature_by_scenario[scenario_id] = float(
            defaults["latent_temperature"]
        )
        forecast_regime = _text(path.get("forecast_weather_regime"))
        regime = _text(regime_map.get(forecast_regime) or defaults["regime_state"])
        if regime not in REGIMES:
            raise ValueError(f"physical_p0_regime_invalid:{regime}")
        driver = drivers[scenario_id]
        wetness = min(1.0, max(0.0, float(defaults["surface_wetness_memory"]) + driver.wetting_rate_per_hour))
        cold_pool = max(0.0, float(defaults["cold_pool_memory"]) + driver.cold_pool_source_c_per_hour2)
        initial_temperature_rows = prior_temperature or ((float(defaults["latent_temperature"]), 1.0),)
        for prior_temperature_c, prior_mass in initial_temperature_rows:
            for draw in parameter_draws:
                draw_id = _text(draw.get("draw_id"))
                draw_weight = float(draw.get("normalized_weight") or 0.0)
                covariance_scale = float(draw.get("initial_covariance_scale", 1.0))
                covariance = tuple(
                    tuple(float(item) * covariance_scale for item in row)
                    for row in covariance_by_regime[regime]
                )
                components.append(
                    MixtureComponent(
                        component_id="phys-p0-" + canonical_sha256(
                            {
                                "scenario": scenario_id,
                                "prior_temperature_c": prior_temperature_c,
                                "parameter_draw": draw_id,
                                "reference": state_reference_valid_at,
                                "weight_method": weight_method,
                                "prior_context_id": prior_context.get("prior_context_id"),
                            }
                        ),
                        normalized_weight=weights[scenario_id] * prior_mass * draw_weight,
                        regime_state=regime,
                        forcing_scenario_id=scenario_id,
                        continuous_state_mean=ContinuousState(
                            latent_temperature=float(prior_temperature_c)
                            + float(draw.get("latent_temperature_bias_c", 0.0)),
                            specific_humidity=driver.specific_humidity_target,
                            net_temperature_tendency=(
                                driver.net_tendency_target_c_per_hour
                                + float(
                                    draw.get(
                                        "net_temperature_tendency_bias_c_per_hour", 0.0
                                    )
                                )
                            ),
                            surface_wetness_memory=wetness,
                            cold_pool_memory=cold_pool,
                        ),
                        continuous_state_covariance=covariance,
                        bounded_parameter_draw_id=draw_id,
                    )
                )
    posterior = PhysicalStatePosterior(
        components=tuple(components),
        uncertainty_breakdown={
            "scenario_weight_entropy": -sum(
                weight * math.log(max(weight, 1e-300)) for weight in weights.values()
            )
        },
    )
    return posterior, {
        "scenario_weight_method": weight_method,
        "scenario_weights": weights,
        "physical_temperature_prior_used": prior_temperature is not None,
        "physical_temperature_prior_context_id": _text(prior_context.get("prior_context_id")),
        "physical_temperature_prior_mass_sha256": (
            canonical_sha256(prior_temperature) if prior_temperature is not None else ""
        ),
        "default_initial_temperature_used": prior_temperature is None,
        "default_initial_temperature_by_scenario": default_initial_temperature_by_scenario,
        "source_temperature_path_consumed": False,
        "bounded_parameter_draws": [
            {
                "draw_id": _text(row.get("draw_id")),
                "normalized_weight": float(row.get("normalized_weight") or 0.0),
            }
            for row in parameter_draws
        ],
        "parameter_uncertainty_is_explicit_mixture": True,
        "driver_generation_sha256": next(iter(drivers.values())).forcing_generation_sha256,
        "initialization_does_not_read_canonical_observation": True,
        "market_inputs_consumed": False,
    }


__all__ = [
    "canonical_observation_from_event",
    "drivers_from_weak_likelihood",
    "initialize_physical_posterior",
    "scenario_weights_from_weak_likelihood",
]
