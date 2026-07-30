"""Forward-only replacement of weak physical forcing generations."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from .contracts import MixtureComponent, PhysicalStatePosterior, STATE_NAMES
from .core import canonical_sha256


def _state_moments(posterior: PhysicalStatePosterior) -> dict[str, Any]:
    """Return scenario-marginal first and second raw moments for auditing."""

    means = [0.0 for _ in STATE_NAMES]
    seconds = [[0.0 for _ in STATE_NAMES] for _ in STATE_NAMES]
    regime_mass: dict[str, float] = {}
    parameter_mass: dict[str, float] = {}
    for component in posterior.components:
        weight = float(component.normalized_weight)
        mean = component.continuous_state_mean.as_tuple()
        covariance = component.continuous_state_covariance
        regime_mass[component.regime_state] = regime_mass.get(component.regime_state, 0.0) + weight
        parameter_mass[component.bounded_parameter_draw_id] = (
            parameter_mass.get(component.bounded_parameter_draw_id, 0.0) + weight
        )
        for i in range(len(STATE_NAMES)):
            means[i] += weight * mean[i]
            for j in range(len(STATE_NAMES)):
                seconds[i][j] += weight * (float(covariance[i][j]) + mean[i] * mean[j])
    return {
        "mean": dict(zip(STATE_NAMES, means)),
        "second_raw_moment": {
            STATE_NAMES[i]: {
                STATE_NAMES[j]: seconds[i][j] for j in range(len(STATE_NAMES))
            }
            for i in range(len(STATE_NAMES))
        },
        "regime_mass": dict(sorted(regime_mass.items())),
        "bounded_parameter_draw_mass": dict(sorted(parameter_mass.items())),
    }


def replace_future_forcing_generation(
    *,
    posterior_at_effective_time: PhysicalStatePosterior,
    revised_scenario_weights: Mapping[str, float],
    revised_forcing_generation_sha256: str,
    effective_from: str,
) -> tuple[PhysicalStatePosterior, dict[str, Any]]:
    """Rebind future scenarios without using a forecast revision as likelihood.

    The new forcing generation is independent of the already-conditioned
    physical state at ``effective_from``.  The exact product mixture preserves
    the scenario-marginal state distribution, regime mass and bounded-parameter
    mass while establishing the revised scenario marginal.
    """

    generation = str(revised_forcing_generation_sha256 or "").strip()
    if len(generation) != 64 or not str(effective_from or "").strip():
        raise ValueError("physical_forcing_rebind_identity_invalid")
    weights = {
        str(key): float(value)
        for key, value in revised_scenario_weights.items()
        if str(key).strip()
    }
    if not weights or any(value < 0.0 for value in weights.values()):
        raise ValueError("physical_forcing_rebind_scenario_weights_invalid")
    total = sum(weights.values())
    if total <= 0.0 or abs(total - 1.0) > 1e-9:
        raise ValueError("physical_forcing_rebind_scenario_weights_not_normalized")

    components: list[MixtureComponent] = []
    for previous in posterior_at_effective_time.components:
        for scenario_id, scenario_weight in sorted(weights.items()):
            if scenario_weight <= 0.0:
                continue
            identity = {
                "previous_component_id": previous.component_id,
                "revised_forcing_generation_sha256": generation,
                "revised_scenario_id": scenario_id,
                "effective_from": effective_from,
            }
            components.append(
                MixtureComponent(
                    component_id="phys-rebind-" + canonical_sha256(identity),
                    normalized_weight=previous.normalized_weight * scenario_weight,
                    regime_state=previous.regime_state,
                    forcing_scenario_id=scenario_id,
                    continuous_state_mean=previous.continuous_state_mean,
                    continuous_state_covariance=previous.continuous_state_covariance,
                    bounded_parameter_draw_id=previous.bounded_parameter_draw_id,
                )
            )
    rebound = PhysicalStatePosterior(
        components=tuple(components),
        representation=posterior_at_effective_time.representation,
        normalization_error=posterior_at_effective_time.normalization_error,
        uncertainty_breakdown={
            **dict(posterior_at_effective_time.uncertainty_breakdown),
            "forcing_revision_scenario_count": float(
                sum(1 for value in weights.values() if value > 0.0)
            ),
        },
    )
    before = _state_moments(posterior_at_effective_time)
    after = _state_moments(rebound)
    maximum_error = 0.0
    for name in STATE_NAMES:
        maximum_error = max(
            maximum_error,
            abs(float(before["mean"][name]) - float(after["mean"][name])),
        )
        for other in STATE_NAMES:
            maximum_error = max(
                maximum_error,
                abs(
                    float(before["second_raw_moment"][name][other])
                    - float(after["second_raw_moment"][name][other])
                ),
            )
    for surface in ("regime_mass", "bounded_parameter_draw_mass"):
        for key in set(before[surface]) | set(after[surface]):
            maximum_error = max(
                maximum_error,
                abs(float(before[surface].get(key, 0.0)) - float(after[surface].get(key, 0.0))),
            )
    if maximum_error > 1e-9:
        raise ValueError("physical_forcing_rebind_state_marginal_changed")
    scenario_mass: dict[str, float] = {}
    for component in rebound.components:
        scenario_mass[component.forcing_scenario_id] = (
            scenario_mass.get(component.forcing_scenario_id, 0.0)
            + component.normalized_weight
        )
    scenario_error = max(
        abs(scenario_mass.get(key, 0.0) - value) for key, value in weights.items()
    )
    if scenario_error > 1e-9:
        raise ValueError("physical_forcing_rebind_scenario_marginal_mismatch")
    audit = {
        "schema": "physical_future_forcing_rebind_audit_v1",
        "effective_from": str(effective_from),
        "revised_forcing_generation_sha256": generation,
        "revised_scenario_weights": dict(sorted(weights.items())),
        "state_marginal_before_sha256": canonical_sha256(before),
        "state_marginal_after_sha256": canonical_sha256(after),
        "state_marginal_max_abs_error": maximum_error,
        "scenario_marginal_max_abs_error": scenario_error,
        "state_marginal_preserved": True,
        "future_forcing_independent_of_conditioned_state_at_effective_time": True,
        "forecast_revision_used_as_likelihood": False,
        "current_observation_consumed": False,
        "component_count_before": len(posterior_at_effective_time.components),
        "component_count_after": len(rebound.components),
        "rebound_posterior_sha256": canonical_sha256(asdict(rebound)),
    }
    return rebound, audit


__all__ = ["replace_future_forcing_generation"]
