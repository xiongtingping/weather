"""Continuous-time, candidate-conditioned pre-event transition model."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from physical_latent_state_contract import LATENT_STATES

from .artifact_contracts import validate_transition_artifact
from .contracts import CandidateForcingSlice, DurationClocks, ScenarioTransition


@dataclass(frozen=True)
class ForecastScenarioControl:
    scenario_id: str
    responsibility: float
    forecast_rate_c_per_hour: float
    future_max_increment_c: float
    future_max_drawdown_c: float
    minutes_to_curve_peak: float
    forcing_availability: float
    precipitation_probability: float
    precipitation_feature_available: float
    cloud_suppression_probability: float
    cloud_feature_available: float
    cb_probability: float
    cb_feature_available: float


def _bounded_probability(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"scenario_probability_invalid:{name}")
    return result


def _finite_control(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"scenario_control_non_finite:{name}")
    return result


def _features(
    *,
    y_c: float,
    reported_regular_max_c: int | None,
    clocks: DurationClocks,
    scenario: ForecastScenarioControl,
) -> dict[str, float]:
    observed = float(reported_regular_max_c) if reported_regular_max_c is not None else y_c
    return {
        "remaining_warming_c": max(0.0, y_c - observed),
        "forecast_rate_c_per_hour": _finite_control(
            scenario.forecast_rate_c_per_hour, "forecast_rate_c_per_hour"
        ),
        "future_max_increment_c": _finite_control(
            scenario.future_max_increment_c, "future_max_increment_c"
        ),
        "future_max_drawdown_c": _finite_control(
            scenario.future_max_drawdown_c, "future_max_drawdown_c"
        ),
        "minutes_to_curve_peak_scaled": _finite_control(
            scenario.minutes_to_curve_peak, "minutes_to_curve_peak"
        ) / 60.0,
        "forcing_availability": _bounded_probability(
            scenario.forcing_availability, "forcing_availability"
        ),
        "precipitation_probability": _bounded_probability(
            scenario.precipitation_probability, "precipitation"
        ),
        "precipitation_feature_available": _bounded_probability(
            scenario.precipitation_feature_available, "precipitation_feature_available"
        ),
        "cloud_suppression_probability": _bounded_probability(
            scenario.cloud_suppression_probability, "cloud_suppression"
        ),
        "cloud_feature_available": _bounded_probability(
            scenario.cloud_feature_available, "cloud_feature_available"
        ),
        "cb_probability": _bounded_probability(
            scenario.cb_probability, "cb"
        ),
        "cb_feature_available": _bounded_probability(
            scenario.cb_feature_available, "cb_feature_available"
        ),
        "cloud_suppression_duration_hours": clocks.cloud_suppression_minutes / 60.0,
        "precipitation_duration_hours": clocks.precipitation_minutes / 60.0,
        "cb_duration_hours": clocks.cb_minutes / 60.0,
    }


def _scenario_matrix(
    *, artifact: Mapping[str, object], features: Mapping[str, float], delta_minutes: float
) -> dict[str, dict[str, float]]:
    hours = float(delta_minutes) / 60.0
    if not math.isfinite(hours) or hours < 0.0:
        raise ValueError("transition_delta_minutes_invalid")
    transitions = artifact["transitions"]
    assert isinstance(transitions, Mapping)
    matrix: dict[str, dict[str, float]] = {}
    for source in LATENT_STATES:
        raw_targets = transitions[source]
        assert isinstance(raw_targets, Mapping)
        rates: dict[str, float] = {}
        for target, raw_parameters in raw_targets.items():
            assert isinstance(raw_parameters, Mapping)
            base_rate = float(raw_parameters["base_rate_per_hour"])
            if base_rate == 0.0:
                rates[str(target)] = 0.0
                continue
            log_rate = math.log(base_rate)
            coefficients = raw_parameters["coefficients"]
            assert isinstance(coefficients, Mapping)
            log_rate += sum(float(coefficient) * features[name] for name, coefficient in coefficients.items())
            rates[str(target)] = math.exp(max(-30.0, min(30.0, log_rate)))
        total_rate = sum(rates.values())
        exit_probability = 0.0 if total_rate <= 0.0 else 1.0 - math.exp(-total_rate * hours)
        row = {state: 0.0 for state in LATENT_STATES}
        row[source] = 1.0 - exit_probability
        if total_rate > 0.0:
            for target, rate in rates.items():
                row[target] = exit_probability * rate / total_rate
        matrix[source] = row
    return matrix


def build_candidate_forcing_slices(
    *,
    y_grid_c: Sequence[float],
    reported_regular_max_c: int | None,
    pre_event_duration_clocks: DurationClocks,
    scenarios_by_y: Mapping[float, Sequence[ForecastScenarioControl]],
    delta_minutes: float,
    transition_artifact: Mapping[str, object],
) -> tuple[CandidateForcingSlice, ...]:
    artifact = validate_transition_artifact(transition_artifact)
    result: list[CandidateForcingSlice] = []
    for y_c in y_grid_c:
        scenarios = tuple(scenarios_by_y.get(float(y_c), ()))
        if not scenarios or abs(sum(item.responsibility for item in scenarios) - 1.0) > 1e-9:
            raise ValueError(f"scenario_responsibility_invalid:{y_c:g}")
        rows = []
        for scenario in scenarios:
            if (
                not scenario.scenario_id
                or not math.isfinite(scenario.responsibility)
                or scenario.responsibility < 0.0
            ):
                raise ValueError(f"scenario_identity_invalid:{y_c:g}")
            features = _features(
                y_c=float(y_c),
                reported_regular_max_c=reported_regular_max_c,
                clocks=pre_event_duration_clocks,
                scenario=scenario,
            )
            rows.append(
                ScenarioTransition(
                    scenario_id=scenario.scenario_id,
                    responsibility=float(scenario.responsibility),
                    matrix=_scenario_matrix(
                        artifact=artifact,
                        features=features,
                        delta_minutes=delta_minutes,
                    ),
                )
            )
        result.append(CandidateForcingSlice(y_c=float(y_c), scenarios=tuple(rows)))
    return tuple(result)


__all__ = ["ForecastScenarioControl", "build_candidate_forcing_slices"]
