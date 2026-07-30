"""Pure Predict -> emission Update -> reported-X projection for successor v6.

This module intentionally has no database, network, market, production runtime,
or wall-clock dependency.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Iterable, Mapping

from physical_latent_state_contract import LATENT_STATES

from .contracts import (
    SCHEMA_VERSION,
    CandidateForcingSlice,
    CanonicalObservationEmission,
    DurationClocks,
    JointCell,
    PostEventState,
    PreEventPrediction,
)


_TOL = 1e-9


def _report_integer_c(y_c: float) -> int:
    """Frozen positive-Celsius half-up report operator for the v6 contract."""

    return int(math.floor(y_c + 0.5))


def final_locked_reported_x_compatible(
    *,
    y_c: float,
    state: str,
    observed_reported_regular_max_c: int | None,
) -> bool:
    """Keep final-locked cells only on the already observed reported-X.

    This is a structural compatibility condition inside the joint state, not
    a current-temperature ceiling.  Non-final states remain eligible to carry
    soft probability for a future higher daily maximum.
    """

    return (
        observed_reported_regular_max_c is None
        or state != "final_locked"
        or _report_integer_c(y_c) == observed_reported_regular_max_c
    )


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: item.__dict__,
        ).encode("utf-8")
    ).hexdigest()


def _validate_joint(rows: Iterable[JointCell]) -> tuple[JointCell, ...]:
    joint = tuple(rows)
    if not joint:
        raise ValueError("joint_empty")
    total = sum(row.probability for row in joint)
    if not math.isfinite(total) or abs(total - 1.0) > _TOL:
        raise ValueError("joint_not_normalized")
    keys = {(row.y_c, row.state) for row in joint}
    if len(keys) != len(joint):
        raise ValueError("joint_cell_duplicate")
    return joint


def _validate_matrix(
    matrix: Mapping[str, Mapping[str, float]],
) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for source in LATENT_STATES:
        raw_row = matrix.get(source)
        if raw_row is None:
            raise ValueError(f"transition_row_missing:{source}")
        row = {target: float(raw_row.get(target, 0.0)) for target in LATENT_STATES}
        if any(not math.isfinite(value) or value < 0.0 for value in row.values()):
            raise ValueError(f"transition_probability_invalid:{source}")
        if abs(sum(row.values()) - 1.0) > _TOL:
            raise ValueError(f"transition_row_not_normalized:{source}")
        result[source] = row
    return result


def _candidate_matrix(candidate: CandidateForcingSlice) -> dict[str, dict[str, float]]:
    if not candidate.scenarios:
        raise ValueError(f"forcing_scenarios_missing:{candidate.y_c:g}")
    responsibility_total = sum(item.responsibility for item in candidate.scenarios)
    if abs(responsibility_total - 1.0) > _TOL:
        raise ValueError(f"scenario_responsibility_not_normalized:{candidate.y_c:g}")
    mixed = {source: {target: 0.0 for target in LATENT_STATES} for source in LATENT_STATES}
    for scenario in candidate.scenarios:
        if not scenario.scenario_id:
            raise ValueError("scenario_id_missing")
        if not math.isfinite(scenario.responsibility) or scenario.responsibility < 0.0:
            raise ValueError(f"scenario_responsibility_invalid:{scenario.scenario_id}")
        matrix = _validate_matrix(scenario.matrix)
        for source in LATENT_STATES:
            for target in LATENT_STATES:
                mixed[source][target] += scenario.responsibility * matrix[source][target]
    return _validate_matrix(mixed)


def predict_to_event(
    *,
    previous_joint: Iterable[JointCell],
    previous_checkpoint_sha256: str,
    previous_reported_regular_max_c: int | None,
    previous_y_lower_bound_c: float | None,
    duration_clocks_before: DurationClocks,
    event_id: str,
    event_valid_at: str,
    delta_minutes: float,
    forcing_generation_sha256: str,
    transition_artifact_sha256: str,
    candidate_forcing: Iterable[CandidateForcingSlice],
) -> PreEventPrediction:
    """Advance P(Z|Y) without reading any field from the current observation."""

    joint = _validate_joint(previous_joint)
    if not event_id or not event_valid_at:
        raise ValueError("event_identity_missing")
    if not math.isfinite(delta_minutes) or delta_minutes < 0.0:
        raise ValueError("delta_minutes_invalid")
    forcing_items = tuple(candidate_forcing)
    forcing_by_y = {item.y_c: item for item in forcing_items}
    if len(forcing_by_y) != len(forcing_items):
        raise ValueError("candidate_forcing_y_duplicate")
    y_values = {row.y_c for row in joint}
    if set(forcing_by_y) != y_values:
        raise ValueError("candidate_forcing_y_grid_mismatch")

    matrices = {y_c: _candidate_matrix(forcing_by_y[y_c]) for y_c in sorted(y_values)}
    predicted: dict[tuple[float, str], float] = defaultdict(float)
    for cell in joint:
        for target, probability in matrices[cell.y_c][cell.state].items():
            predicted[(cell.y_c, target)] += cell.probability * probability
    result = tuple(
        JointCell(y_c=y_c, state=state, probability=probability)
        for (y_c, state), probability in sorted(predicted.items())
        if probability > 0.0
    )
    _validate_joint(result)

    before_y = defaultdict(float)
    after_y = defaultdict(float)
    for cell in joint:
        before_y[cell.y_c] += cell.probability
    for cell in result:
        after_y[cell.y_c] += cell.probability
    if any(abs(before_y[y_c] - after_y[y_c]) > _TOL for y_c in before_y):
        raise ValueError("predict_changed_y_marginal")

    return PreEventPrediction(
        schema=SCHEMA_VERSION,
        previous_checkpoint_sha256=previous_checkpoint_sha256,
        event_id=event_id,
        event_valid_at=event_valid_at,
        delta_minutes=delta_minutes,
        previous_reported_regular_max_c=previous_reported_regular_max_c,
        previous_y_lower_bound_c=previous_y_lower_bound_c,
        duration_clocks_before=advance_duration_clocks_to_event(
            duration_clocks_before, delta_minutes
        ),
        joint=result,
        forcing_generation_sha256=forcing_generation_sha256,
        transition_artifact_sha256=transition_artifact_sha256,
        candidate_transition_trace={f"{y_c:g}": matrices[y_c] for y_c in sorted(matrices)},
    )


def pre_event_prediction_sha256(prediction: PreEventPrediction) -> str:
    return _sha(prediction)


def _advance_clock(previous: float, active: bool, delta_minutes: float) -> float:
    return previous + delta_minutes if active else 0.0


def advance_duration_clocks_to_event(
    previous: DurationClocks, delta_minutes: float
) -> DurationClocks:
    return DurationClocks(
        cloud_suppression_minutes=_advance_clock(
            previous.cloud_suppression_minutes,
            previous.cloud_suppression_active,
            delta_minutes,
        ),
        precipitation_minutes=_advance_clock(
            previous.precipitation_minutes,
            previous.precipitation_active,
            delta_minutes,
        ),
        cb_minutes=_advance_clock(previous.cb_minutes, previous.cb_active, delta_minutes),
        tcu_minutes=_advance_clock(previous.tcu_minutes, previous.tcu_active, delta_minutes),
        cloud_suppression_active=previous.cloud_suppression_active,
        precipitation_active=previous.precipitation_active,
        cb_active=previous.cb_active,
        tcu_active=previous.tcu_active,
        canonical_event_count=previous.canonical_event_count,
        physical_event_cluster_signature=previous.physical_event_cluster_signature,
        physical_event_cluster_minutes=_advance_clock(
            previous.physical_event_cluster_minutes,
            bool(previous.physical_event_cluster_signature),
            delta_minutes,
        ),
        physical_event_cluster_information_spent=previous.physical_event_cluster_information_spent,
    )


def update_with_observation(
    *,
    prediction: PreEventPrediction,
    emission: CanonicalObservationEmission,
) -> PostEventState:
    """Consume the current event exactly once; no post-emission transition exists."""

    prediction_hash = pre_event_prediction_sha256(prediction)
    if emission.pre_event_prediction_sha256 != prediction_hash:
        raise ValueError("emission_pre_event_binding_mismatch")
    if emission.event_id != prediction.event_id or emission.event_valid_at != prediction.event_valid_at:
        raise ValueError("emission_event_binding_mismatch")

    keys = {(cell.y_c, cell.state) for cell in prediction.joint}
    if set(emission.log_emission_by_cell) != keys:
        raise ValueError("emission_cell_grid_mismatch")
    scores = [float(emission.log_emission_by_cell[(cell.y_c, cell.state)]) for cell in prediction.joint]
    if any(not math.isfinite(score) for score in scores):
        raise ValueError("emission_log_score_non_finite")

    previous_reported_max = prediction.previous_reported_regular_max_c
    event_reported = (
        int(emission.reported_temperature_c)
        if emission.is_regular_metar and emission.reported_temperature_c is not None
        else None
    )
    reported_max = max(
        (value for value in (previous_reported_max, event_reported) if value is not None),
        default=None,
    )
    event_y_bound = float(event_reported) - 0.5 if event_reported is not None else None
    y_lower_bound = max(
        (value for value in (prediction.previous_y_lower_bound_c, event_y_bound) if value is not None),
        default=None,
    )
    max_score = max(scores)
    weights = [
        cell.probability * math.exp(score - max_score)
        if (
            (y_lower_bound is None or cell.y_c + _TOL >= y_lower_bound)
            and final_locked_reported_x_compatible(
                y_c=cell.y_c,
                state=cell.state,
                observed_reported_regular_max_c=reported_max,
            )
        )
        else 0.0
        for cell, score in zip(prediction.joint, scores)
    ]
    total = sum(weights)
    if not math.isfinite(total) or total <= 0.0:
        raise ValueError("hard_bound_or_emission_zeroed_all_mass")
    joint = tuple(
        JointCell(y_c=cell.y_c, state=cell.state, probability=weight / total)
        for cell, weight in zip(prediction.joint, weights)
        if weight > 0.0
    )
    _validate_joint(joint)
    clocks = prediction.duration_clocks_before
    cluster_change = emission.physical_event_cluster_change_point or clocks.canonical_event_count == 0
    cluster_spent = (
        emission.event_information_weight
        if cluster_change
        else clocks.physical_event_cluster_information_spent + emission.event_information_weight
    )
    after = DurationClocks(
        cloud_suppression_minutes=(
            clocks.cloud_suppression_minutes
            if emission.cloud_suppressed and clocks.cloud_suppression_active
            else 0.0
        ),
        precipitation_minutes=(
            clocks.precipitation_minutes
            if emission.precipitation_present and clocks.precipitation_active
            else 0.0
        ),
        cb_minutes=(
            clocks.cb_minutes if emission.cb_present and clocks.cb_active else 0.0
        ),
        tcu_minutes=(
            clocks.tcu_minutes if emission.tcu_present and clocks.tcu_active else 0.0
        ),
        cloud_suppression_active=emission.cloud_suppressed,
        precipitation_active=emission.precipitation_present,
        cb_active=emission.cb_present,
        tcu_active=emission.tcu_present,
        canonical_event_count=clocks.canonical_event_count + 1,
        physical_event_cluster_signature=emission.physical_event_cluster_signature,
        physical_event_cluster_minutes=(
            0.0 if cluster_change else clocks.physical_event_cluster_minutes
        ),
        physical_event_cluster_information_spent=cluster_spent,
    )
    return PostEventState(
        schema=SCHEMA_VERSION,
        event_id=emission.event_id,
        pre_event_prediction_sha256=prediction_hash,
        joint=joint,
        observed_reported_regular_max_c=reported_max,
        y_lower_bound_c=y_lower_bound,
        duration_clocks_after=after,
        observation_artifact_sha256=emission.observation_artifact_sha256,
        hard_y_lower_bound_c=y_lower_bound,
        normalizer_log=math.log(total) + max_score,
        invariant_report={
            "current_event_consumed_only_in_emission": True,
            "no_post_emission_state_transition": True,
            "hard_bound_applied_before_normalization": True,
            "final_locked_reported_x_compatibility_applied_before_normalization": True,
            "joint_normalized": True,
        },
    )


def project_reported_x(post_event: PostEventState) -> dict[int, float]:
    """Project the continuous Y grid to public integer reported-X support."""

    result: dict[int, float] = defaultdict(float)
    for cell in post_event.joint:
        result[_report_integer_c(cell.y_c)] += cell.probability
    total = sum(result.values())
    if abs(total - 1.0) > _TOL:
        raise ValueError("reported_x_projection_not_normalized")
    return dict(sorted(result.items()))


__all__ = [
    "advance_duration_clocks_to_event",
    "final_locked_reported_x_compatible",
    "predict_to_event",
    "pre_event_prediction_sha256",
    "project_reported_x",
    "update_with_observation",
]
