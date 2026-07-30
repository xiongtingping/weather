"""Future physical paths and frozen AMSC reporting-operator projection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Any, Mapping, Sequence

from .contracts import (
    PROJECTION_SCHEMA,
    ContinuousState,
    PhysicalDriver,
    PhysicalStatePosterior,
    REGIMES,
    ReportedXProjection,
    ReportingSlot,
    STATE_INDEX,
    STATE_NAMES,
)
from .core import (
    canonical_sha256,
    parameter_draw_by_id,
    validate_state_model_artifact,
)


REPORTING_OPERATOR_ARTIFACT_SCHEMA = "amsc_frozen_reporting_operator_v1"
SUPPORT_COMPLETION_SCHEMA = "bounded_continuous_quadrature_support_completion_v1"


@dataclass
class _Path:
    weight: float
    scenario_id: str
    regime: str
    state: list[float]
    max_pmf: dict[int | None, float]
    continuous_peak_c: float
    quantile_z: float
    parameter_draw_id: str


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("reporting_operator_time_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _artifact_sha(value: Mapping[str, Any]) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != "artifact_sha256"})


def validate_reporting_operator_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    artifact = dict(value)
    issues: list[str] = []
    if artifact.get("schema") != REPORTING_OPERATOR_ARTIFACT_SCHEMA:
        issues.append("reporting_operator_schema_invalid")
    declared = str(artifact.get("artifact_sha256") or "")
    computed = _artifact_sha(artifact)
    if declared and declared != computed:
        issues.append("reporting_operator_sha256_mismatch")
    quantiles = artifact.get("path_quantiles")
    if not isinstance(quantiles, Sequence) or not quantiles:
        issues.append("reporting_operator_path_quantiles_missing")
        quantiles = []
    else:
        total = 0.0
        for row in quantiles:
            if not isinstance(row, Mapping):
                issues.append("reporting_operator_path_quantile_invalid")
                continue
            z = float(row.get("z") or 0.0)
            weight = float(row.get("weight") or 0.0)
            if not math.isfinite(z) or not math.isfinite(weight) or weight <= 0.0:
                issues.append("reporting_operator_path_quantile_invalid")
            total += weight
        if abs(total - 1.0) > 1e-9:
            issues.append("reporting_operator_path_quantiles_not_normalized")
    maximum = int(artifact.get("max_path_branches") or 0)
    minimum = float(artifact.get("minimum_path_weight") or -1.0)
    if maximum <= 0 or not math.isfinite(minimum) or minimum < 0.0:
        issues.append("reporting_operator_path_pruning_invalid")
    if artifact.get("temperature_encoding") != "positive_celsius_half_up_integer":
        issues.append("reporting_operator_temperature_encoding_invalid")
    if artifact.get("path_merge_key") not in {
        "scenario_regime_quantile",
        "scenario_regime_parameter_quantile",
    }:
        issues.append("reporting_operator_path_merge_key_invalid")
    completion = artifact.get("continuous_support_completion")
    if not isinstance(completion, Mapping):
        issues.append("reporting_operator_continuous_support_completion_missing")
    else:
        if completion.get("schema") != SUPPORT_COMPLETION_SCHEMA:
            issues.append("reporting_operator_continuous_support_completion_schema_invalid")
        if completion.get("kernel") != "bounded_student_t_on_integer_reported_x":
            issues.append("reporting_operator_continuous_support_completion_kernel_invalid")
        try:
            completion_mass = float(completion.get("total_mass"))
            degrees_of_freedom = float(completion.get("degrees_of_freedom"))
            minimum_scale_c = float(completion.get("minimum_scale_c"))
        except (TypeError, ValueError):
            completion_mass = degrees_of_freedom = minimum_scale_c = math.nan
        if not math.isfinite(completion_mass) or not 0.0 < completion_mass <= 1e-3:
            issues.append("reporting_operator_continuous_support_completion_mass_invalid")
        if not math.isfinite(degrees_of_freedom) or degrees_of_freedom <= 2.0:
            issues.append("reporting_operator_continuous_support_completion_df_invalid")
        if not math.isfinite(minimum_scale_c) or not 0.0 < minimum_scale_c <= 5.0:
            issues.append("reporting_operator_continuous_support_completion_scale_invalid")
        if completion.get("apply_only_with_future_contract_slots") is not True:
            issues.append("reporting_operator_continuous_support_completion_scope_invalid")
    if issues:
        raise ValueError(";".join(sorted(set(issues))))
    artifact["artifact_sha256"] = computed
    return artifact


def _transition_probabilities(driver: PhysicalDriver, source: str, hours: float) -> dict[str, float]:
    row = driver.regime_transition_per_hour.get(source)
    if not isinstance(row, Mapping):
        raise ValueError(f"reporting_driver_transition_row_missing:{source}")
    rates = {target: float(row.get(target, 0.0)) for target in REGIMES if target != source}
    if any(not math.isfinite(value) or value < 0.0 for value in rates.values()):
        raise ValueError(f"reporting_driver_transition_rate_invalid:{source}")
    total = sum(rates.values())
    exit_probability = 0.0 if total <= 0.0 else 1.0 - math.exp(-total * hours)
    result = {target: 0.0 for target in REGIMES}
    result[source] = 1.0 - exit_probability
    if total > 0.0:
        for target, rate in rates.items():
            result[target] = exit_probability * rate / total
    return result


def _evolve(
    state: Sequence[float],
    *,
    source_regime: str,
    target_regime: str,
    driver: PhysicalDriver,
    hours: float,
    artifact: Mapping[str, Any],
    quantile_z: float,
    parameter_draw_id: str,
    parameter_draw: Mapping[str, Any] | None = None,
) -> list[float]:
    values = list(map(float, state))
    t = STATE_INDEX["latent_temperature"]
    q = STATE_INDEX["specific_humidity"]
    g = STATE_INDEX["net_temperature_tendency"]
    w = STATE_INDEX["surface_wetness_memory"]
    c = STATE_INDEX["cold_pool_memory"]
    memory = artifact["memory_hours"]
    rho_g = math.exp(-hours / float(memory["net_temperature_tendency"][target_regime]))
    rho_q = math.exp(-hours / float(memory["specific_humidity"][target_regime]))
    rho_c = math.exp(-hours / float(memory["cold_pool_memory"][target_regime]))
    parameter_draw = parameter_draw or parameter_draw_by_id(artifact, parameter_draw_id)
    target_g = (
        driver.net_tendency_target_c_per_hour
        + float(artifact["regime_tendency_offset_c_per_hour"][target_regime])
        + float(parameter_draw["net_temperature_tendency_bias_c_per_hour"])
    )
    endogenous_target_g = float(
        artifact["regime_tendency_offset_c_per_hour"][target_regime]
    ) + float(parameter_draw["net_temperature_tendency_bias_c_per_hour"])
    availability = driver.availability
    next_g = rho_g * values[g] + (1.0 - rho_g) * (
        availability * target_g + (1.0 - availability) * endogenous_target_g
    )
    # ``quantile_z`` selects an initial state-space quadrature node.  It is
    # not a fresh process shock at every reporting slot: reusing it here
    # would add the same-sign temperature noise repeatedly and manufacture a
    # false multi-hour warm drift.  Process uncertainty belongs in the
    # covariance/posterior transition, not in a deterministic path as a new
    # shock on every slot.
    next_t = values[t] + 0.5 * hours * (values[g] + next_g)
    next_q = rho_q * values[q] + (1.0 - rho_q) * (
        availability * driver.specific_humidity_target + (1.0 - availability) * values[q]
    )
    next_w = values[w] + hours * (
        driver.wetting_rate_per_hour * (1.0 - values[w])
        - driver.drying_rate_per_hour * values[w]
    )
    next_c = rho_c * values[c] + hours * driver.cold_pool_source_c_per_hour2
    raw = (next_t, next_q, next_g, next_w, next_c)
    bounds = artifact["state_bounds"]
    return [
        min(float(bounds[name][1]), max(float(bounds[name][0]), float(raw[index])))
        for index, name in enumerate(STATE_NAMES)
    ]


def _encoded_integer_c(value: float, step: float) -> int:
    encoded = math.floor(value / step + 0.5) * step
    if abs(encoded - round(encoded)) > 1e-9:
        raise ValueError("reported_x_non_integer_encoding_not_supported")
    return int(round(encoded))


def _apply_report_operator(path: _Path, slot: ReportingSlot) -> None:
    if not slot.is_contract_slot:
        return
    accepted_probability = slot.report_probability * slot.quality_acceptance_probability
    reported = _encoded_integer_c(
        path.state[STATE_INDEX["latent_temperature"]], slot.encoding_step_c
    )
    updated: dict[int | None, float] = {}
    for previous, probability in path.max_pmf.items():
        missing_mass = probability * (1.0 - accepted_probability)
        if missing_mass > 0.0:
            updated[previous] = updated.get(previous, 0.0) + missing_mass
        new_max = reported if previous is None else max(previous, reported)
        reported_mass = probability * accepted_probability
        if reported_mass > 0.0:
            updated[new_max] = updated.get(new_max, 0.0) + reported_mass
    path.max_pmf = updated


def _complete_continuous_support(
    distribution: Mapping[int, float],
    *,
    observed_contract_max_c: int | None,
    state_model_artifact: Mapping[str, Any],
    reporting_operator_artifact: Mapping[str, Any],
    has_future_contract_slot: bool,
) -> tuple[dict[int, float], dict[str, Any]]:
    """Restore bounded continuous support omitted by finite path quadrature.

    The physical posterior is a continuous Gaussian mixture, but the reporting
    integral is evaluated with a finite set of deterministic path nodes.  A
    literal discrete projection can therefore manufacture exact-zero buckets
    that are not mathematical zeros of the underlying posterior.  Reallocate
    a frozen, very small portion of mass to a bounded Student-t completion
    kernel.  This changes neither the hard lower bound nor the dominant path
    probabilities and is skipped once no future contract slot remains.
    """

    cleaned = {
        int(bucket): float(probability)
        for bucket, probability in distribution.items()
        if math.isfinite(float(probability)) and float(probability) > 0.0
    }
    if not cleaned or not has_future_contract_slot:
        return cleaned, {
            "applied": False,
            "reason": "empty_distribution" if not cleaned else "no_future_contract_slot",
        }
    completion = reporting_operator_artifact["continuous_support_completion"]
    completion_mass = float(completion["total_mass"])
    degrees_of_freedom = float(completion["degrees_of_freedom"])
    minimum_scale = float(completion["minimum_scale_c"])
    state_bounds = state_model_artifact["state_bounds"]["latent_temperature"]
    encoded_lower = _encoded_integer_c(float(state_bounds[0]), 1.0)
    encoded_upper = _encoded_integer_c(float(state_bounds[1]), 1.0)
    lower = max(
        encoded_lower,
        int(observed_contract_max_c) if observed_contract_max_c is not None else encoded_lower,
    )
    upper = max(lower, encoded_upper)
    primary_total = sum(cleaned.values())
    primary = {bucket: probability / primary_total for bucket, probability in cleaned.items()}
    center = sum(bucket * probability for bucket, probability in primary.items())
    variance = sum(
        (bucket - center) ** 2 * probability for bucket, probability in primary.items()
    )
    scale = max(minimum_scale, math.sqrt(max(0.0, variance)))
    exponent = -(degrees_of_freedom + 1.0) / 2.0
    raw_kernel = {
        bucket: (
            1.0
            + ((bucket - center) / scale) ** 2 / degrees_of_freedom
        )
        ** exponent
        for bucket in range(lower, upper + 1)
    }
    kernel_total = sum(raw_kernel.values())
    kernel = {bucket: weight / kernel_total for bucket, weight in raw_kernel.items()}
    completed = {
        bucket: (1.0 - completion_mass) * primary.get(bucket, 0.0)
        + completion_mass * kernel[bucket]
        for bucket in kernel
    }
    total = sum(completed.values())
    completed = {bucket: probability / total for bucket, probability in completed.items()}
    return completed, {
        "applied": True,
        "schema": completion["schema"],
        "kernel": completion["kernel"],
        "total_mass": completion_mass,
        "degrees_of_freedom": degrees_of_freedom,
        "scale_c": scale,
        "support_lower_c": lower,
        "support_upper_c": upper,
    }


def _prune(paths: list[_Path], maximum: int, minimum: float) -> tuple[list[_Path], float]:
    kept = [path for path in paths if path.weight >= minimum]
    dropped = sum(path.weight for path in paths if path.weight < minimum)
    if len(kept) > maximum:
        kept.sort(key=lambda path: (-path.weight, path.scenario_id, path.regime, path.continuous_peak_c))
        dropped += sum(path.weight for path in kept[maximum:])
        kept = kept[:maximum]
    total = sum(path.weight for path in kept)
    if total <= 0.0:
        raise ValueError("reporting_path_pruning_zeroed_all_mass")
    for path in kept:
        path.weight /= total
    return kept, dropped


def _merge_paths(paths: list[_Path]) -> list[_Path]:
    """Moment-match histories with the same scenario/regime/quantile.

    The discrete scenario, regime and fixed quadrature branch remain intact;
    only numerically redundant histories inside that exact cell are merged.
    Their reported-max distributions are mixed conditionally, so the frozen
    reporting operator remains explicit rather than being replaced by a mean
    maximum.
    """

    grouped: dict[tuple[str, str, str, float], list[_Path]] = {}
    for path in paths:
        grouped.setdefault(
            (
                path.scenario_id,
                path.regime,
                path.parameter_draw_id,
                path.quantile_z,
            ),
            [],
        ).append(path)
    result = []
    for key, rows in sorted(grouped.items()):
        total = sum(path.weight for path in rows)
        if total <= 0.0:
            continue
        state = [
            sum(path.weight * path.state[index] for path in rows) / total
            for index in range(len(STATE_NAMES))
        ]
        max_pmf: dict[int | None, float] = {}
        for path in rows:
            conditional_weight = path.weight / total
            for maximum, probability in path.max_pmf.items():
                max_pmf[maximum] = max_pmf.get(maximum, 0.0) + conditional_weight * probability
        result.append(
            _Path(
                weight=total,
                scenario_id=key[0],
                regime=key[1],
                state=state,
                max_pmf=max_pmf,
                continuous_peak_c=sum(
                    path.weight * path.continuous_peak_c for path in rows
                ) / total,
                quantile_z=key[3],
                parameter_draw_id=key[2],
            )
        )
    return result


def project_future_paths(
    *,
    posterior: PhysicalStatePosterior,
    state_reference_valid_at: str,
    reporting_slots: Sequence[ReportingSlot],
    drivers_by_scenario: Mapping[str, Sequence[PhysicalDriver]],
    observed_contract_max_c: int | None,
    state_model_artifact: Mapping[str, Any],
    reporting_operator_artifact: Mapping[str, Any],
) -> ReportedXProjection:
    """Project physical paths through frozen encoding, QC and missing-report slots."""

    state_artifact = validate_state_model_artifact(state_model_artifact)
    reporting_artifact = validate_reporting_operator_artifact(reporting_operator_artifact)
    slots = tuple(sorted(reporting_slots, key=lambda item: _time(item.valid_at)))
    if len({_time(item.valid_at) for item in slots}) != len(slots):
        raise ValueError("reporting_slot_time_duplicate")
    scenarios = {item.forcing_scenario_id for item in posterior.components}
    if set(drivers_by_scenario) != scenarios:
        raise ValueError("reporting_driver_scenario_set_mismatch")
    if any(len(drivers_by_scenario[scenario]) != len(slots) for scenario in scenarios):
        raise ValueError("reporting_driver_slot_count_mismatch")
    reference = _time(state_reference_valid_at)
    if slots and _time(slots[0].valid_at) < reference:
        raise ValueError("reporting_slot_precedes_state_reference")

    paths: list[_Path] = []
    t_index = STATE_INDEX["latent_temperature"]
    g_index = STATE_INDEX["net_temperature_tendency"]
    parameter_draw_cache = {
        draw_id: parameter_draw_by_id(state_artifact, draw_id)
        for draw_id in {
            component.bounded_parameter_draw_id for component in posterior.components
        }
    }
    for component in posterior.components:
        mean = component.continuous_state_mean.as_tuple()
        variance_t = max(0.0, component.continuous_state_covariance[t_index][t_index])
        sigma_t = math.sqrt(variance_t)
        tg_slope = (
            component.continuous_state_covariance[g_index][t_index] / sigma_t
            if sigma_t > 1e-12
            else 0.0
        )
        for row in reporting_artifact["path_quantiles"]:
            z = float(row["z"])
            state = list(mean)
            state[t_index] += z * sigma_t
            state[g_index] += z * tg_slope
            paths.append(
                _Path(
                    weight=component.normalized_weight * float(row["weight"]),
                    scenario_id=component.forcing_scenario_id,
                    regime=component.regime_state,
                    state=state,
                    max_pmf={observed_contract_max_c: 1.0},
                    continuous_peak_c=state[t_index],
                    quantile_z=z,
                    parameter_draw_id=component.bounded_parameter_draw_id,
                )
            )

    previous_time = reference
    total_pruned_mass = 0.0
    for slot_index, slot in enumerate(slots):
        current_time = _time(slot.valid_at)
        hours = (current_time - previous_time).total_seconds() / 3600.0
        if hours < 0.0:
            raise ValueError("reporting_slot_out_of_order")
        transition_cache = {
            scenario_id: {
                regime: _transition_probabilities(
                    drivers_by_scenario[scenario_id][slot_index], regime, hours
                )
                for regime in REGIMES
            }
            for scenario_id in scenarios
        }
        branched: list[_Path] = []
        for path in paths:
            driver = drivers_by_scenario[path.scenario_id][slot_index]
            transitions = transition_cache[path.scenario_id][path.regime]
            for target_regime, probability in transitions.items():
                if probability <= 0.0:
                    continue
                state = _evolve(
                    path.state,
                    source_regime=path.regime,
                    target_regime=target_regime,
                    driver=driver,
                    hours=hours,
                    artifact=state_artifact,
                    quantile_z=path.quantile_z,
                    parameter_draw_id=path.parameter_draw_id,
                    parameter_draw=parameter_draw_cache[path.parameter_draw_id],
                )
                child = _Path(
                    weight=path.weight * probability,
                    scenario_id=path.scenario_id,
                    regime=target_regime,
                    state=state,
                    max_pmf=dict(path.max_pmf),
                    continuous_peak_c=max(path.continuous_peak_c, state[t_index]),
                    quantile_z=path.quantile_z,
                    parameter_draw_id=path.parameter_draw_id,
                )
                _apply_report_operator(child, slot)
                branched.append(child)
        paths, dropped = _prune(
            _merge_paths(branched),
            int(reporting_artifact["max_path_branches"]),
            float(reporting_artifact["minimum_path_weight"]),
        )
        total_pruned_mass += dropped
        previous_time = current_time

    raw_x: dict[int, float] = {}
    no_report = 0.0
    continuous_peaks: dict[str, float] = {}
    for path in paths:
        peak_key = f"{path.continuous_peak_c:.3f}"
        continuous_peaks[peak_key] = continuous_peaks.get(peak_key, 0.0) + path.weight
        for maximum, conditional_probability in path.max_pmf.items():
            mass = path.weight * conditional_probability
            if maximum is None:
                no_report += mass
            elif mass > 0.0:
                raw_x[int(maximum)] = raw_x.get(int(maximum), 0.0) + mass
    accepted = sum(raw_x.values())
    if accepted > 0.0:
        distribution = {bucket: probability / accepted for bucket, probability in sorted(raw_x.items())}
    else:
        distribution = {}
    distribution, support_completion = _complete_continuous_support(
        distribution,
        observed_contract_max_c=observed_contract_max_c,
        state_model_artifact=state_artifact,
        reporting_operator_artifact=reporting_artifact,
        has_future_contract_slot=any(
            slot.is_contract_slot
            and slot.report_probability * slot.quality_acceptance_probability > 0.0
            for slot in slots
        ),
    )
    if observed_contract_max_c is not None and any(bucket < observed_contract_max_c for bucket in distribution):
        raise ValueError("reported_x_hard_condition_violation")
    probability_error = abs(sum(distribution.values()) - (1.0 if distribution else 0.0))
    lineage = {
        "posterior_sha256": canonical_sha256(posterior),
        "state_reference_valid_at": state_reference_valid_at,
        "reporting_slots": [asdict(item) for item in slots],
        "driver_generation_sha256": sorted(
            {driver.forcing_generation_sha256 for rows in drivers_by_scenario.values() for driver in rows}
        ),
        "state_model_artifact_sha256": state_artifact["artifact_sha256"],
        "reporting_operator_sha256": reporting_artifact["artifact_sha256"],
        "observed_contract_max_c": observed_contract_max_c,
        "bounded_parameter_draw_ids": sorted(
            {path.parameter_draw_id for path in paths}
        ),
        "pruned_path_mass_accumulator": total_pruned_mass,
        "continuous_support_completion": support_completion,
        "reported_x_distribution": distribution,
    }
    return ReportedXProjection(
        schema=PROJECTION_SCHEMA,
        reported_x_distribution=distribution,
        latent_continuous_peak_distribution=dict(sorted(continuous_peaks.items(), key=lambda item: float(item[0]))),
        observed_contract_max_c=observed_contract_max_c,
        no_accepted_contract_report_probability=no_report,
        reporting_operator_sha256=str(reporting_artifact["artifact_sha256"]),
        path_projection_sha256=canonical_sha256(lineage),
        continuous_support_completion=support_completion,
        hard_condition_applied=observed_contract_max_c is not None,
        probability_sum_error=probability_error,
    )


__all__ = [
    "REPORTING_OPERATOR_ARTIFACT_SCHEMA",
    "SUPPORT_COMPLETION_SCHEMA",
    "project_future_paths",
    "validate_reporting_operator_artifact",
]
