"""Exact persistent P(Y, Z, scenario) checkpoint operations for production v6."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict
from typing import Any, Iterable, Mapping

from physical_latent_state_contract import LATENT_STATES

from .contracts import (
    SCHEMA_VERSION,
    CandidateForcingSlice,
    CanonicalObservationEmission,
    DurationClocks,
    JointCell,
    PreEventPrediction,
)
from .core import (
    advance_duration_clocks_to_event,
    final_locked_reported_x_compatible,
    pre_event_prediction_sha256,
    update_with_observation,
)


SCENARIO_JOINT_SCHEMA = "physical_state_bayes_v6_scenario_joint_checkpoint_v1"
SCENARIO_TRACE_SCHEMA = "physical_state_bayes_v6_persistent_scenario_trace_v1"
_TOL = 1e-9


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value or "[]")
    if isinstance(value, Mapping):
        value = value.get("rows")
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        raise ValueError("scenario_joint_rows_invalid")
    return [row for row in value if isinstance(row, Mapping)]


def validate_scenario_joint(value: Any) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    keys: set[tuple[float, str, str]] = set()
    for raw in _rows(value):
        row = {
            "continuous_y_c": float(raw.get("continuous_y_c")),
            "state": str(raw.get("state") or "").strip(),
            "scenario_id": str(raw.get("scenario_id") or "").strip(),
            "probability": float(raw.get("probability")),
        }
        key = (row["continuous_y_c"], row["state"], row["scenario_id"])
        if (
            row["state"] not in LATENT_STATES
            or not row["scenario_id"]
            or not all(math.isfinite(float(row[name])) for name in ("continuous_y_c", "probability"))
            or row["probability"] < 0.0
            or key in keys
        ):
            raise ValueError("scenario_joint_cell_invalid")
        keys.add(key)
        if row["probability"] > 0.0:
            result.append(row)
    if not result or abs(sum(row["probability"] for row in result) - 1.0) > _TOL:
        raise ValueError("scenario_joint_not_normalized")
    return tuple(sorted(result, key=lambda row: (row["continuous_y_c"], row["state"], row["scenario_id"])))


def marginalize_scenario_joint(value: Any) -> tuple[JointCell, ...]:
    marginal: dict[tuple[float, str], float] = defaultdict(float)
    for row in validate_scenario_joint(value):
        marginal[(row["continuous_y_c"], row["state"])] += row["probability"]
    return tuple(JointCell(y, state, probability) for (y, state), probability in sorted(marginal.items()))


def scenario_joint_sha256(value: Any) -> str:
    return _sha(list(validate_scenario_joint(value)))


def _responsibilities(value: Any) -> dict[float, tuple[tuple[str, float], ...]]:
    result: dict[float, tuple[tuple[str, float], ...]] = {}
    for row in _rows(value):
        y = float(row.get("continuous_y_c"))
        scenarios = tuple(
            (str(item.get("scenario_id") or "").strip(), float(item.get("responsibility")))
            for item in (row.get("scenarios") or []) if isinstance(item, Mapping)
        )
        if (
            not scenarios
            or any(not sid or not math.isfinite(weight) or weight < 0.0 for sid, weight in scenarios)
            or len({sid for sid, _ in scenarios}) != len(scenarios)
            or abs(sum(weight for _, weight in scenarios) - 1.0) > _TOL
        ):
            raise ValueError(f"scenario_responsibilities_invalid:{y:g}")
        result[y] = scenarios
    if not result:
        raise ValueError("scenario_responsibilities_missing")
    return result


def initialize_scenario_joint(
    joint: Iterable[JointCell], responsibilities: Any
) -> tuple[dict[str, Any], ...]:
    by_y = _responsibilities(responsibilities)
    result = []
    for cell in joint:
        if cell.y_c not in by_y:
            raise ValueError(f"scenario_responsibilities_y_missing:{cell.y_c:g}")
        for scenario_id, weight in by_y[cell.y_c]:
            probability = cell.probability * weight
            if probability > 0.0:
                result.append({
                    "continuous_y_c": cell.y_c, "state": cell.state,
                    "scenario_id": scenario_id, "probability": probability,
                })
    return validate_scenario_joint(result)


def _matrix(candidate: CandidateForcingSlice, scenario_id: str) -> Mapping[str, Mapping[str, float]]:
    matches = [scenario.matrix for scenario in candidate.scenarios if scenario.scenario_id == scenario_id]
    if len(matches) != 1:
        raise ValueError(f"scenario_joint_forcing_identity_missing:{candidate.y_c:g}:{scenario_id}")
    matrix = matches[0]
    for source in LATENT_STATES:
        row = matrix.get(source)
        if row is None or abs(sum(float(row.get(target, 0.0)) for target in LATENT_STATES) - 1.0) > _TOL:
            raise ValueError(f"scenario_joint_transition_invalid:{scenario_id}:{source}")
    return matrix


def predict_scenario_joint_to_event(
    *, previous_scenario_joint: Any, previous_checkpoint_sha256: str,
    previous_reported_regular_max_c: int | None, previous_y_lower_bound_c: float | None,
    duration_clocks_before: DurationClocks, event_id: str, event_valid_at: str,
    delta_minutes: float, forcing_generation_sha256: str, transition_artifact_sha256: str,
    candidate_forcing: Iterable[CandidateForcingSlice],
) -> tuple[PreEventPrediction, tuple[dict[str, Any], ...]]:
    previous = validate_scenario_joint(previous_scenario_joint)
    forcing = tuple(candidate_forcing)
    by_y = {item.y_c: item for item in forcing}
    if set(by_y) != {row["continuous_y_c"] for row in previous}:
        raise ValueError("scenario_joint_forcing_y_grid_mismatch")
    predicted: dict[tuple[float, str, str], float] = defaultdict(float)
    effective_numerators: dict[tuple[float, str, str], float] = defaultdict(float)
    effective_denominators: dict[tuple[float, str], float] = defaultdict(float)
    for row in previous:
        y, source, scenario_id, mass = (
            row["continuous_y_c"], row["state"], row["scenario_id"], row["probability"]
        )
        matrix = _matrix(by_y[y], scenario_id)
        effective_denominators[(y, source)] += mass
        for target in LATENT_STATES:
            probability = float(matrix[source].get(target, 0.0))
            if probability < 0.0 or not math.isfinite(probability):
                raise ValueError("scenario_joint_transition_probability_invalid")
            predicted[(y, target, scenario_id)] += mass * probability
            effective_numerators[(y, source, target)] += mass * probability
    predicted_rows = validate_scenario_joint([
        {"continuous_y_c": y, "state": state, "scenario_id": scenario_id, "probability": probability}
        for (y, state, scenario_id), probability in predicted.items() if probability > 0.0
    ])
    marginal = marginalize_scenario_joint(predicted_rows)
    trace: dict[str, dict[str, dict[str, float]]] = {}
    for y in sorted(by_y):
        trace[f"{y:g}"] = {}
        for source in LATENT_STATES:
            denominator = effective_denominators[(y, source)]
            trace[f"{y:g}"][source] = {
                target: effective_numerators[(y, source, target)] / denominator
                if denominator > 0.0 else float(by_y[y].scenarios[0].matrix[source].get(target, 0.0))
                for target in LATENT_STATES
            }
    prediction = PreEventPrediction(
        schema=SCHEMA_VERSION, previous_checkpoint_sha256=previous_checkpoint_sha256,
        event_id=event_id, event_valid_at=event_valid_at, delta_minutes=delta_minutes,
        previous_reported_regular_max_c=previous_reported_regular_max_c,
        previous_y_lower_bound_c=previous_y_lower_bound_c,
        duration_clocks_before=advance_duration_clocks_to_event(duration_clocks_before, delta_minutes),
        joint=marginal, forcing_generation_sha256=forcing_generation_sha256,
        transition_artifact_sha256=transition_artifact_sha256, candidate_transition_trace=trace,
    )
    return prediction, predicted_rows


def update_scenario_joint_with_observation(
    *, prediction: PreEventPrediction, predicted_scenario_joint: Any,
    emission: CanonicalObservationEmission,
) -> tuple[Any, tuple[dict[str, Any], ...], dict[str, Any]]:
    predicted = validate_scenario_joint(predicted_scenario_joint)
    if marginalize_scenario_joint(predicted) != prediction.joint:
        raise ValueError("scenario_joint_prediction_marginal_mismatch")
    post = update_with_observation(prediction=prediction, emission=emission)
    event_reported = int(emission.reported_temperature_c) if emission.is_regular_metar else None
    reported_max = max((x for x in (prediction.previous_reported_regular_max_c, event_reported) if x is not None), default=None)
    event_bound = float(event_reported) - 0.5 if event_reported is not None else None
    y_bound = max((x for x in (prediction.previous_y_lower_bound_c, event_bound) if x is not None), default=None)
    scores = [float(emission.log_emission_by_cell[(row["continuous_y_c"], row["state"])]) for row in predicted]
    anchor = max(scores)
    weighted = []
    for row, score in zip(predicted, scores, strict=True):
        allowed = (y_bound is None or row["continuous_y_c"] + _TOL >= y_bound) and final_locked_reported_x_compatible(
            y_c=row["continuous_y_c"], state=row["state"], observed_reported_regular_max_c=reported_max
        )
        weighted.append(row["probability"] * math.exp(score - anchor) if allowed else 0.0)
    total = sum(weighted)
    if total <= 0.0:
        raise ValueError("scenario_joint_observation_eliminated_all_mass")
    posterior = validate_scenario_joint([
        {**row, "probability": weight / total} for row, weight in zip(predicted, weighted, strict=True) if weight > 0.0
    ])
    guard = (
        emission.observation_feature_trace.get("time_conditioned_likelihood", {})
        .get("adverse_y_coupling_guard", {})
        if isinstance(emission.observation_feature_trace, Mapping) else {}
    )
    scenario_guard_applied = bool(isinstance(guard, Mapping) and guard.get("applied"))
    if scenario_guard_applied:
        prior_s: dict[str, float] = defaultdict(float)
        after_s: dict[str, float] = defaultdict(float)
        y_s: dict[str, float] = defaultdict(float)
        for row in predicted:
            sid = row["scenario_id"]
            prior_s[sid] += row["probability"]
            y_s[sid] += row["probability"] * row["continuous_y_c"]
        for row in posterior:
            after_s[row["scenario_id"]] += row["probability"]
        ordered = sorted(prior_s, key=lambda sid: (y_s[sid] / prior_s[sid], sid))
        blocks = [
            {"ids": [sid], "weight": prior_s[sid],
             "value": math.log(after_s[sid] / prior_s[sid])}
            for sid in ordered if after_s.get(sid, 0.0) > 0.0
        ]
        # Weighted PAVA projection onto non-increasing evidence versus a
        # scenario's prior expected Y.  Adverse weather may distinguish
        # paths, but cannot become a delayed reward for the hotter path.
        index = 0
        while index + 1 < len(blocks):
            if blocks[index]["value"] + _TOL >= blocks[index + 1]["value"]:
                index += 1
                continue
            left, right = blocks[index], blocks[index + 1]
            weight_sum = left["weight"] + right["weight"]
            merged = {
                "ids": left["ids"] + right["ids"], "weight": weight_sum,
                "value": (left["value"] * left["weight"] + right["value"] * right["weight"]) / weight_sum,
            }
            blocks[index:index + 2] = [merged]
            index = max(0, index - 1)
        projected = {sid: block["value"] for block in blocks for sid in block["ids"]}
        correction = {
            sid: math.exp(projected[sid] - math.log(after_s[sid] / prior_s[sid]))
            for sid in projected
        }
        by_cell: dict[tuple[float, str], list[dict[str, Any]]] = defaultdict(list)
        for row in posterior:
            by_cell[(row["continuous_y_c"], row["state"])].append(row)
        guarded = []
        for cell, rows in by_cell.items():
            denominator = sum(row["probability"] * correction.get(row["scenario_id"], 1.0) for row in rows)
            target_mass = next(item.probability for item in post.joint if (item.y_c, item.state) == cell)
            for row in rows:
                guarded.append({**row, "probability": target_mass * row["probability"] * correction.get(row["scenario_id"], 1.0) / denominator})
        posterior = validate_scenario_joint(guarded)
    marginal = marginalize_scenario_joint(posterior)
    expected = {(x.y_c, x.state): x.probability for x in post.joint}
    actual = {(x.y_c, x.state): x.probability for x in marginal}
    error = max(abs(expected.get(key, 0.0) - actual.get(key, 0.0)) for key in set(expected) | set(actual))
    if error > _TOL:
        raise ValueError("scenario_joint_post_marginal_mismatch")
    def scenario_marginal(rows: Any) -> dict[str, float]:
        result: dict[str, float] = defaultdict(float)
        for row in rows:
            result[row["scenario_id"]] += row["probability"]
        return dict(result)
    before = scenario_marginal(predicted)
    after = scenario_marginal(posterior)
    trace = {
        "schema": SCENARIO_TRACE_SCHEMA,
        "rows": [{
            "scenario_id": sid, "probability_before": before.get(sid, 0.0),
            "probability_after": after.get(sid, 0.0),
            "delta_probability": after.get(sid, 0.0) - before.get(sid, 0.0),
            "log_responsibility_ratio": (
                math.log(after[sid] / before[sid])
                if before.get(sid, 0.0) > 0.0 and after.get(sid, 0.0) > 0.0 else None
            ),
        } for sid in sorted(set(before) | set(after))],
        "before_probability_total": sum(before.values()), "after_probability_total": sum(after.values()),
        "marginal_consistency_max_abs_error": error, "marginal_matches_production_posterior": error <= _TOL,
        "carried_forward_to_next_event": True, "checkpoint_dimension": "P(Y,Z,scenario)",
        "adverse_scenario_hot_path_reward_guard_applied": scenario_guard_applied,
        "frontend_recompute_allowed": False,
    }
    return post, posterior, trace


__all__ = [
    "SCENARIO_JOINT_SCHEMA", "SCENARIO_TRACE_SCHEMA", "initialize_scenario_joint",
    "marginalize_scenario_joint", "predict_scenario_joint_to_event",
    "scenario_joint_sha256", "update_scenario_joint_with_observation", "validate_scenario_joint",
]
