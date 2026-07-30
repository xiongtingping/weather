#!/usr/bin/env python3
"""Strict holdout and physics-invariant evaluation for v6 artifact candidates."""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_latent_state_contract import LATENT_STATES
from physical_state_bayes_v6.artifact_contracts import validate_observation_artifact, validate_transition_artifact
from physical_state_bayes_v6.contracts import DurationClocks
from physical_state_bayes_v6.observation_model import _continuous_log_density
from physical_state_bayes_v6.transition_model import ForecastScenarioControl, build_candidate_forcing_slices
from scripts.build_physical_state_bayes_v6_artifact_candidates import (
    DEFAULT_INPUT,
    DEFAULT_OUTPUT,
    _finite,
    _label,
    _rows,
    _sha,
    _weather_signature,
)


SCHEMA_VERSION = "physical_state_bayes_v6_artifact_oos_evaluation_v1"


def _logsumexp(values: list[float]) -> float:
    anchor = max(values)
    return anchor + math.log(sum(math.exp(value - anchor) for value in values))


def _emission_log_score(model: Mapping[str, Any], row: Mapping[str, Any]) -> float:
    values = (
        float(row["temperature_delta_1h"]),
        float(row["temperature_c"]) - float(row["dewpoint_c"]),
    )
    continuous = _continuous_log_density(
        values,
        list(model["continuous_mean"]),
        [list(item) for item in model["continuous_covariance"]],
        ((0.0, 0.0), (0.0, 0.0)),
        float("inf"),
    )
    probabilities = model["weather_signature_probabilities"]
    signature = _weather_signature(row)
    return continuous + math.log(float(probabilities.get(signature, probabilities["__other__"])))


def _scenario(**overrides: float) -> ForecastScenarioControl:
    values = {
        "scenario_id": "oos-control",
        "responsibility": 1.0,
        "forecast_rate_c_per_hour": 0.0,
        "future_max_increment_c": 0.0,
        "future_max_drawdown_c": 0.0,
        "minutes_to_curve_peak": 0.0,
        "forcing_availability": 0.0,
        "precipitation_probability": 0.0,
        "precipitation_feature_available": 0.0,
        "cloud_suppression_probability": 0.0,
        "cloud_feature_available": 0.0,
        "cb_probability": 0.0,
        "cb_feature_available": 0.0,
    }
    values.update(overrides)
    return ForecastScenarioControl(**values)


def _matrix(
    artifact: Mapping[str, Any],
    *,
    y_c: float,
    observed_max_c: int,
    clocks: DurationClocks,
    scenario: ForecastScenarioControl,
) -> Mapping[str, Mapping[str, float]]:
    return build_candidate_forcing_slices(
        y_grid_c=[y_c],
        reported_regular_max_c=observed_max_c,
        pre_event_duration_clocks=clocks,
        scenarios_by_y={y_c: [scenario]},
        delta_minutes=60.0,
        transition_artifact=artifact,
    )[0].scenarios[0].matrix


def _zero_coefficient_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(artifact))
    value.pop("artifact_sha256", None)
    for targets in value["transitions"].values():
        for parameters in targets.values():
            parameters["coefficients"] = {}
    value["artifact_sha256"] = _sha({key: item for key, item in value.items() if key != "artifact_sha256"})
    return validate_transition_artifact(value)


def evaluate(
    input_path: Path,
    artifact_dir: Path,
    *,
    holdout_start: str = "2025-01-01",
    holdout_end_exclusive: str = "2026-01-01",
) -> dict[str, Any]:
    transition = validate_transition_artifact(
        json.loads((artifact_dir / "transition_artifact.candidate.json").read_text(encoding="utf-8"))
    )
    observation = validate_observation_artifact(
        json.loads((artifact_dir / "observation_artifact.candidate.json").read_text(encoding="utf-8"))
    )
    baseline_transition = _zero_coefficient_artifact(transition)
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _rows(input_path):
        day = str(row.get("event_date_local") or "")[:10]
        if not holdout_start <= day < holdout_end_exclusive:
            continue
        if row.get("core_qc_bad") or not row.get("daily_max_label_eligible"):
            continue
        if any(_finite(row.get(field)) is None for field in ("temperature_c", "dewpoint_c", "temperature_delta_1h")):
            continue
        by_day[day].append(row)
    labelled: list[tuple[dict[str, Any], str]] = []
    sequences: list[tuple[list[dict[str, Any]], list[str]]] = []
    for rows in by_day.values():
        rows.sort(key=lambda item: str(item.get("valid_time_utc") or ""))
        labels = [
            _label(
                row,
                rows[index - 1] if index > 0 else None,
                rows[index + 1] if index + 1 < len(rows) else None,
            )
            for index, row in enumerate(rows)
        ]
        labelled.extend(zip(rows, labels, strict=True))
        sequences.append((rows, labels))
    training_counts = {
        state: max(0, int(observation["state_models"][state].get("training_row_count") or 0))
        for state in LATENT_STATES
    }
    total_training = sum(training_counts.values())
    priors = {state: (training_counts[state] + 0.5) / (total_training + 0.5 * len(LATENT_STATES)) for state in LATENT_STATES}
    observation_nll = 0.0
    baseline_nll = 0.0
    correct = 0
    majority = max(priors, key=priors.get)
    state_counts: Counter[str] = Counter()
    for row, truth in labelled:
        scores = {
            state: math.log(priors[state]) + _emission_log_score(observation["state_models"][state], row)
            for state in LATENT_STATES
        }
        observation_nll += _logsumexp(list(scores.values())) - scores[truth]
        baseline_nll += -math.log(priors[truth])
        correct += max(scores, key=scores.get) == truth
        state_counts[truth] += 1
    transition_nll = 0.0
    transition_baseline_nll = 0.0
    transition_count = 0
    epsilon = 1e-12
    for rows, labels in sequences:
        cloud_minutes = precipitation_minutes = cb_minutes = 0.0
        cloud_active = precipitation_active = cb_active = False
        for index, (row, source) in enumerate(zip(rows, labels, strict=True)):
            if index + 1 >= len(rows):
                break
            cloud = (_finite(row.get("cloud_cover_pct")) or 0.0) >= 62.5
            precipitation = bool(row.get("current_precipitation_reported"))
            cb = bool(row.get("current_convective_weather") or row.get("convective_cloud_reported"))
            cloud_minutes = cloud_minutes + 60.0 if cloud and cloud_active else 0.0
            precipitation_minutes = precipitation_minutes + 60.0 if precipitation and precipitation_active else 0.0
            cb_minutes = cb_minutes + 60.0 if cb and cb_active else 0.0
            cloud_active, precipitation_active, cb_active = cloud, precipitation, cb
            clocks = DurationClocks(
                cloud_suppression_minutes=cloud_minutes,
                precipitation_minutes=precipitation_minutes,
                cb_minutes=cb_minutes,
                cloud_suppression_active=cloud,
                precipitation_active=precipitation,
                cb_active=cb,
            )
            y_c = float(row["final_daily_max_c"])
            observed = int(float(row["observed_max_to_time_c"]) + 0.5)
            target = labels[index + 1]
            candidate_matrix = _matrix(
                transition,
                y_c=y_c,
                observed_max_c=observed,
                clocks=clocks,
                scenario=_scenario(),
            )
            baseline_matrix = _matrix(
                baseline_transition,
                y_c=y_c,
                observed_max_c=observed,
                clocks=clocks,
                scenario=_scenario(),
            )
            transition_nll += -math.log(max(epsilon, float(candidate_matrix[source][target])))
            transition_baseline_nll += -math.log(max(epsilon, float(baseline_matrix[source][target])))
            transition_count += 1
    probe_clocks = DurationClocks()
    clear = _matrix(transition, y_c=38.0, observed_max_c=34, clocks=probe_clocks, scenario=_scenario(forcing_availability=1.0))
    rain = _matrix(
        transition,
        y_c=38.0,
        observed_max_c=34,
        clocks=probe_clocks,
        scenario=_scenario(
            forcing_availability=1.0,
            precipitation_probability=1.0,
            precipitation_feature_available=1.0,
            cloud_suppression_probability=1.0,
            cloud_feature_available=1.0,
        ),
    )
    warming_future = _matrix(
        transition,
        y_c=38.0,
        observed_max_c=34,
        clocks=probe_clocks,
        scenario=_scenario(
            forcing_availability=1.0,
            forecast_rate_c_per_hour=2.0,
            future_max_increment_c=4.0,
            minutes_to_curve_peak=120.0,
        ),
    )
    monotonic = {
        "rain_increases_warming_to_weather_suppressed": rain["warming"].get("weather_suppressed", 0.0) > clear["warming"].get("weather_suppressed", 0.0),
        "warming_future_reduces_warming_to_terminal": warming_future["warming"].get("terminal_cooling", 0.0) < clear["warming"].get("terminal_cooling", 0.0),
        "warming_future_increases_temporary_cooling_to_rebound": warming_future["temporary_cooling"].get("cooling_then_rebound", 0.0) > clear["temporary_cooling"].get("cooling_then_rebound", 0.0),
    }
    count = len(labelled)
    metrics = {
        "holdout_row_count": count,
        "holdout_event_day_count": len(by_day),
        "state_counts": dict(state_counts),
        "observation_mean_nll": observation_nll / max(1, count),
        "observation_prior_only_mean_nll": baseline_nll / max(1, count),
        "observation_accuracy": correct / max(1, count),
        "observation_majority_accuracy": state_counts[majority] / max(1, count),
        "transition_count": transition_count,
        "transition_mean_nll": transition_nll / max(1, transition_count),
        "transition_zero_coefficient_mean_nll": transition_baseline_nll / max(1, transition_count),
    }
    gates = {
        "holdout_rows_at_least_5000": count >= 5000,
        "observation_nll_beats_prior_only": metrics["observation_mean_nll"] < metrics["observation_prior_only_mean_nll"],
        "observation_accuracy_beats_majority": metrics["observation_accuracy"] > metrics["observation_majority_accuracy"],
        "transition_nll_not_worse_than_zero_coefficient": metrics["transition_mean_nll"] <= metrics["transition_zero_coefficient_mean_nll"] + 1e-9,
        **monotonic,
    }
    core = {
        "schema": SCHEMA_VERSION,
        "ok": all(gates.values()),
        "state": "accepted_for_canonical_replay" if all(gates.values()) else "rejected",
        "holdout_start": holdout_start,
        "holdout_end_exclusive": holdout_end_exclusive,
        "transition_artifact_sha256": transition["artifact_sha256"],
        "observation_artifact_sha256": observation["artifact_sha256"],
        "metrics": metrics,
        "gates": gates,
        "strict_holdout_not_used_for_training": holdout_start >= str(transition.get("training_end_exclusive") or ""),
        "places_orders": False,
    }
    return {**core, "evaluation_sha256": _sha(core)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--artifact-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT / "oos_evaluation.json"))
    args = parser.parse_args()
    result = evaluate(Path(args.input).resolve(), Path(args.artifact_dir).resolve())
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
