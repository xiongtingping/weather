"""Regularized contextual stacking for external forecast source distributions.

The model learns source shares by minimizing bucket log loss.  Context uses a
cyclic day-of-year basis, lead time, and explicitly declared continuous fields.
All fitted artifacts are research-only until a separate OOS release gate passes.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Mapping, Sequence

import numpy as np


SCHEMA_VERSION = "forecast_source_contextual_weight_artifact_v1"
EPS = 1e-12


def _float(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def _day_of_year(value: Any) -> int:
    try:
        return date.fromisoformat(str(value or "")[:10]).timetuple().tm_yday
    except ValueError:
        return 1


def _feature_names(*, seasonal_order: int, context_fields: Sequence[str], include_lead_time: bool = True) -> list[str]:
    names = ["intercept"]
    for order in range(1, max(0, int(seasonal_order)) + 1):
        names.extend((f"doy_sin_{order}", f"doy_cos_{order}"))
    if include_lead_time:
        names.append("lead_hours")
    names.extend(str(field) for field in context_fields)
    return names


def _raw_feature_row(
    row: Mapping[str, Any],
    *,
    seasonal_order: int,
    context_fields: Sequence[str],
    include_lead_time: bool = True,
) -> list[float]:
    doy = _day_of_year(row.get("event_date_local"))
    angle = 2.0 * math.pi * (doy - 1.0) / 365.2425
    values = [1.0]
    for order in range(1, max(0, int(seasonal_order)) + 1):
        values.extend((math.sin(order * angle), math.cos(order * angle)))
    if include_lead_time:
        values.append(float(_float(row.get("lead_hours")) or 0.0))
    context = row.get("context") if isinstance(row.get("context"), Mapping) else {}
    for field in context_fields:
        value = _float(context.get(field))
        values.append(float(value) if value is not None else float("nan"))
    return values


def _fit_design(
    rows: Sequence[Mapping[str, Any]],
    *,
    seasonal_order: int,
    context_fields: Sequence[str],
    include_lead_time: bool = True,
) -> tuple[np.ndarray, list[float], list[float], list[str]]:
    matrix = np.asarray(
        [
            _raw_feature_row(row, seasonal_order=seasonal_order, context_fields=context_fields, include_lead_time=include_lead_time)
            for row in rows
        ],
        dtype=float,
    )
    if matrix.size == 0:
        return np.zeros((0, len(_feature_names(seasonal_order=seasonal_order, context_fields=context_fields, include_lead_time=include_lead_time)))), [], [], _feature_names(seasonal_order=seasonal_order, context_fields=context_fields, include_lead_time=include_lead_time)
    means = np.zeros(matrix.shape[1], dtype=float)
    scales = np.ones(matrix.shape[1], dtype=float)
    for index in range(1, matrix.shape[1]):
        column = matrix[:, index]
        finite = column[np.isfinite(column)]
        mean = float(np.mean(finite)) if len(finite) else 0.0
        scale = float(np.std(finite)) if len(finite) else 1.0
        means[index] = mean
        scales[index] = scale if scale > 1e-9 else 1.0
        matrix[:, index] = np.where(np.isfinite(column), column, mean)
    matrix[:, 1:] = (matrix[:, 1:] - means[1:]) / scales[1:]
    return matrix, means.tolist(), scales.tolist(), _feature_names(seasonal_order=seasonal_order, context_fields=context_fields, include_lead_time=include_lead_time)


def _transform_design(row: Mapping[str, Any], artifact: Mapping[str, Any]) -> np.ndarray:
    context_fields = [str(value) for value in artifact.get("context_fields") or []]
    seasonal_order = int(artifact.get("seasonal_order") or 0)
    include_lead_time = artifact.get("include_lead_time") is not False
    values = np.asarray(
        _raw_feature_row(row, seasonal_order=seasonal_order, context_fields=context_fields, include_lead_time=include_lead_time),
        dtype=float,
    )
    means = np.asarray(artifact.get("feature_means") or [], dtype=float)
    scales = np.asarray(artifact.get("feature_scales") or [], dtype=float)
    if len(values) != len(means) or len(values) != len(scales):
        raise ValueError("contextual_weight_artifact_feature_dimension_mismatch")
    values[1:] = np.where(np.isfinite(values[1:]), values[1:], means[1:])
    values[1:] = (values[1:] - means[1:]) / np.where(scales[1:] > 1e-9, scales[1:], 1.0)
    feature_names = [str(value) for value in artifact.get("feature_names") or []]
    support = artifact.get("context_support") if isinstance(artifact.get("context_support"), Mapping) else {}
    for field, state in support.items():
        if isinstance(state, Mapping) and state.get("active") is False and str(field) in feature_names:
            values[feature_names.index(str(field))] = 0.0
    return values


def _actual_probability_matrix(samples: Sequence[Mapping[str, Any]], entities: Sequence[str]) -> tuple[np.ndarray, np.ndarray]:
    probabilities = np.zeros((len(samples), len(entities)), dtype=float)
    available = np.zeros((len(samples), len(entities)), dtype=bool)
    for row_index, sample in enumerate(samples):
        winner = str(sample.get("winning_bucket") or "")
        distributions = sample.get("distributions") if isinstance(sample.get("distributions"), Mapping) else {}
        for entity_index, entity in enumerate(entities):
            distribution = distributions.get(entity)
            if not isinstance(distribution, Mapping) or winner not in distribution:
                continue
            value = _float(distribution.get(winner))
            if value is None:
                continue
            probabilities[row_index, entity_index] = max(EPS, float(value))
            available[row_index, entity_index] = True
    return probabilities, available


def _hierarchical_softmax(
    logits: np.ndarray,
    available: np.ndarray,
    provider_family_indices: Sequence[int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Two-stage family/member softmax, invariant to duplicate interfaces."""

    family_indices = np.asarray(provider_family_indices, dtype=int)
    family_count = int(np.max(family_indices)) + 1 if len(family_indices) else 0
    source_weights = np.zeros_like(logits)
    family_weights = np.zeros((len(logits), family_count), dtype=float)
    member_weights = np.zeros_like(logits)
    for row_index in range(len(logits)):
        active_families: list[int] = []
        family_logits: list[float] = []
        for family_index in range(family_count):
            members = available[row_index] & (family_indices == family_index)
            if not np.any(members):
                continue
            active_families.append(family_index)
            member_logits = logits[row_index, members]
            member_exp = np.exp(member_logits - np.max(member_logits))
            member_weights[row_index, members] = member_exp / np.sum(member_exp)
            family_logits.append(float(np.mean(member_logits)))
        if not active_families:
            continue
        family_logits_array = np.asarray(family_logits, dtype=float)
        family_exp = np.exp(family_logits_array - np.max(family_logits_array))
        active_family_weights = family_exp / np.sum(family_exp)
        for family_index, family_weight in zip(active_families, active_family_weights):
            family_weights[row_index, family_index] = family_weight
            members = available[row_index] & (family_indices == family_index)
            source_weights[row_index, members] = family_weight * member_weights[row_index, members]
    return source_weights, family_weights, member_weights


def _bounded_weights(raw: Mapping[str, float], *, floor: float, cap: float) -> dict[str, float]:
    if not raw:
        return {}
    names = list(raw)
    values = np.asarray([max(0.0, float(raw[name])) for name in names], dtype=float)
    if float(np.sum(values)) <= EPS:
        values[:] = 1.0
    values /= float(np.sum(values))
    count = len(names)
    effective_floor = min(max(0.0, float(floor)), 1.0 / count)
    effective_cap = max(min(1.0, float(cap)), 1.0 / count)
    assigned = np.full(count, effective_floor, dtype=float)
    capacity = np.full(count, effective_cap - effective_floor, dtype=float)
    remaining = max(0.0, 1.0 - float(np.sum(assigned)))
    active = np.ones(count, dtype=bool)
    scores = np.maximum(values, EPS)
    for _iteration in range(count + 1):
        if remaining <= 1e-12 or not np.any(active):
            break
        active_scores = np.where(active, scores, 0.0)
        total_score = float(np.sum(active_scores))
        proposed = remaining * active_scores / total_score if total_score > EPS else remaining * active.astype(float) / int(np.sum(active))
        overflowing = active & (proposed > capacity + 1e-12)
        if not np.any(overflowing):
            assigned += proposed
            remaining = 0.0
            break
        assigned[overflowing] += capacity[overflowing]
        remaining -= float(np.sum(capacity[overflowing]))
        capacity[overflowing] = 0.0
        active[overflowing] = False
    if remaining > 1e-9:
        assigned += remaining / count
    assigned /= float(np.sum(assigned))
    return {name: float(value) for name, value in zip(names, assigned)}


def _apply_family_caps(
    source_weights: Mapping[str, float],
    *,
    provider_families: Mapping[str, str],
    family_floor: float,
    family_cap: float,
) -> dict[str, float]:
    family_totals: dict[str, float] = {}
    for source, weight in source_weights.items():
        family = str(provider_families.get(source) or source)
        family_totals[family] = family_totals.get(family, 0.0) + float(weight)
    bounded_families = _bounded_weights(family_totals, floor=family_floor, cap=family_cap)
    output: dict[str, float] = {}
    for family, target_total in bounded_families.items():
        members = [source for source in source_weights if str(provider_families.get(source) or source) == family]
        raw_total = sum(float(source_weights[source]) for source in members)
        for source in members:
            share = float(source_weights[source]) / raw_total if raw_total > EPS else 1.0 / len(members)
            output[source] = float(target_total) * share
    total = sum(output.values())
    return {source: weight / total for source, weight in output.items()} if total > EPS else output


def _apply_source_bounds_within_families(
    source_weights: Mapping[str, float],
    *,
    provider_families: Mapping[str, str],
    source_floor: float,
    source_cap: float,
) -> dict[str, float]:
    by_family: dict[str, list[str]] = {}
    for source in source_weights:
        by_family.setdefault(str(provider_families.get(source) or source), []).append(source)
    output: dict[str, float] = {}
    for members in by_family.values():
        family_total = sum(float(source_weights[source]) for source in members)
        if family_total <= EPS:
            continue
        shares = {source: float(source_weights[source]) / family_total for source in members}
        bounded = _bounded_weights(
            shares,
            floor=max(0.0, float(source_floor)) / family_total,
            cap=max(0.0, float(source_cap)) / family_total,
        )
        for source, share in bounded.items():
            output[source] = family_total * share
    total = sum(output.values())
    return {source: value / total for source, value in output.items()} if total > EPS else output


def _project_bounded_mapping(
    raw: Mapping[str, float],
    *,
    lower: Mapping[str, float],
    upper: Mapping[str, float],
) -> dict[str, float]:
    names = list(raw)
    if not names:
        return {}
    values = np.asarray([max(0.0, float(raw[name])) for name in names], dtype=float)
    values = values / float(np.sum(values)) if float(np.sum(values)) > EPS else np.full(len(names), 1.0 / len(names))
    lows = np.asarray([max(0.0, float(lower.get(name, 0.0))) for name in names], dtype=float)
    highs = np.asarray([min(1.0, max(lows[index], float(upper.get(name, 1.0)))) for index, name in enumerate(names)], dtype=float)
    if float(np.sum(lows)) > 1.0:
        lows /= float(np.sum(lows))
    if float(np.sum(highs)) < 1.0:
        highs = np.maximum(highs, 1.0 / len(names))
    lows = np.minimum(lows, highs)
    left = float(np.min(values - highs)) - 1.0
    right = float(np.max(values - lows)) + 1.0
    for _ in range(100):
        midpoint = (left + right) / 2.0
        projected = np.clip(values - midpoint, lows, highs)
        if float(np.sum(projected)) > 1.0:
            left = midpoint
        else:
            right = midpoint
    projected = np.clip(values - right, lows, highs)
    projected /= float(np.sum(projected))
    return {name: float(value) for name, value in zip(names, projected)}


def _apply_joint_source_family_bounds(
    source_weights: Mapping[str, float],
    *,
    provider_families: Mapping[str, str],
    source_floor: float,
    source_cap: float,
    family_floor: float,
    family_cap: float,
) -> dict[str, float]:
    by_family: dict[str, list[str]] = {}
    for source in source_weights:
        by_family.setdefault(str(provider_families.get(source) or source), []).append(source)
    family_raw = {
        family: sum(max(0.0, float(source_weights[source])) for source in members)
        for family, members in by_family.items()
    }
    family_lower = {
        family: max(0.0, float(family_floor), len(members) * max(0.0, float(source_floor)))
        for family, members in by_family.items()
    }
    family_upper = {
        family: min(1.0, float(family_cap), len(members) * min(1.0, float(source_cap)))
        for family, members in by_family.items()
    }
    family_targets = _project_bounded_mapping(family_raw, lower=family_lower, upper=family_upper)
    output: dict[str, float] = {}
    for family, members in by_family.items():
        target = family_targets[family]
        member_raw = {source: max(0.0, float(source_weights[source])) for source in members}
        member_shares = _bounded_weights(
            member_raw,
            floor=max(0.0, float(source_floor)) / max(target, EPS),
            cap=min(1.0, float(source_cap)) / max(target, EPS),
        )
        for source, share in member_shares.items():
            output[source] = target * share
    total = sum(output.values())
    return {source: value / total for source, value in output.items()} if total > EPS else output


def fit_contextual_stacking(
    samples: Sequence[Mapping[str, Any]],
    *,
    entities: Sequence[str] | None = None,
    provider_families: Mapping[str, str] | None = None,
    context_fields: Sequence[str] = (),
    seasonal_order: int = 2,
    include_lead_time: bool = True,
    learning_rate: float = 0.03,
    max_iterations: int = 1500,
    l2_intercept: float = 0.05,
    l2_context: float = 1.0,
    source_floor: float = 0.05,
    source_cap: float = 0.45,
    family_floor: float = 0.05,
    family_cap: float = 0.70,
    minimum_context_ess: int = 20,
    early_stopping_tolerance: float = 1e-7,
    early_stopping_patience: int = 3,
    minimum_iterations: int = 200,
) -> dict[str, Any]:
    entity_names = sorted(
        set(entities or [])
        or {
            str(entity)
            for sample in samples
            for entity in (sample.get("distributions") or {}).keys()
        }
    )
    families = {entity: str((provider_families or {}).get(entity) or entity) for entity in entity_names}
    family_names = sorted(set(families.values()))
    family_indices = [family_names.index(families[entity]) for entity in entity_names]
    design, means, scales, feature_names = _fit_design(
        samples,
        seasonal_order=seasonal_order,
        context_fields=context_fields,
        include_lead_time=include_lead_time,
    )
    actual_probabilities, available = _actual_probability_matrix(samples, entity_names)
    sample_weights = np.asarray(
        [max(0.0, float(_float(sample.get("sample_weight")) or 1.0)) for sample in samples],
        dtype=float,
    )
    usable = np.sum(available, axis=1) > 0
    design = design[usable]
    actual_probabilities = actual_probabilities[usable]
    available = available[usable]
    sample_weights = sample_weights[usable]
    if len(sample_weights) and float(np.sum(sample_weights)) <= EPS:
        sample_weights[:] = 1.0
    total_sample_weight = float(np.sum(sample_weights)) if len(sample_weights) else 0.0
    coefficients = np.zeros((len(entity_names), len(feature_names)), dtype=float)
    context_support: dict[str, dict[str, Any]] = {}
    for field in context_fields:
        count = sum(
            1
            for sample in samples
            if isinstance(sample.get("context"), Mapping)
            and _float(sample.get("context", {}).get(field)) is not None
        )
        active = count >= max(1, int(minimum_context_ess))
        context_support[str(field)] = {
            "effective_sample_count": count,
            "minimum_effective_sample_count": max(1, int(minimum_context_ess)),
            "active": active,
            "fallback": None if active else "parent_global_feature_mean",
        }
        if not active and str(field) in feature_names and len(design):
            design[:, feature_names.index(str(field))] = 0.0
    first_moment = np.zeros_like(coefficients)
    second_moment = np.zeros_like(coefficients)
    penalties = np.full(len(feature_names), max(0.0, float(l2_context)), dtype=float)
    if len(penalties):
        penalties[0] = max(0.0, float(l2_intercept))
    objective_history = []
    stable_checkpoints = 0
    previous_checkpoint_objective: float | None = None
    iterations_completed = 0
    converged = False
    if len(design) and entity_names:
        for iteration in range(1, max(1, int(max_iterations)) + 1):
            iterations_completed = iteration
            logits = design @ coefficients.T
            weights, family_weights, member_weights = _hierarchical_softmax(logits, available, family_indices)
            mixture = np.sum(weights * actual_probabilities, axis=1)
            mixture = np.maximum(mixture, EPS)
            loss = float(np.sum(sample_weights * -np.log(mixture)) / max(total_sample_weight, EPS))
            regularization = 0.5 * float(np.sum((coefficients**2) * penalties[None, :])) / max(total_sample_weight, 1.0)
            objective = loss + regularization
            checkpoint = iteration == 1 or iteration % 50 == 0 or iteration == max_iterations
            if checkpoint:
                objective_history.append({"iteration": iteration, "objective": objective, "nll": loss})
                if previous_checkpoint_objective is not None and abs(objective - previous_checkpoint_objective) <= max(0.0, float(early_stopping_tolerance)):
                    stable_checkpoints += 1
                else:
                    stable_checkpoints = 0
                previous_checkpoint_objective = objective
            gradient_logits = np.zeros_like(weights)
            family_index_array = np.asarray(family_indices, dtype=int)
            for row_index in range(len(design)):
                probability_gradient = -actual_probabilities[row_index] / mixture[row_index]
                family_expected_gradients: dict[int, float] = {}
                for family_index in range(len(family_names)):
                    members = available[row_index] & (family_index_array == family_index)
                    if not np.any(members):
                        continue
                    family_expected_gradients[family_index] = float(
                        np.sum(member_weights[row_index, members] * probability_gradient[members])
                    )
                overall_gradient = sum(
                    family_weights[row_index, family_index] * value
                    for family_index, value in family_expected_gradients.items()
                )
                for family_index, family_gradient in family_expected_gradients.items():
                    members = available[row_index] & (family_index_array == family_index)
                    member_count = int(np.sum(members))
                    family_weight = family_weights[row_index, family_index]
                    gradient_logits[row_index, members] = (
                        family_weight
                        * member_weights[row_index, members]
                        * (probability_gradient[members] - family_gradient)
                        + family_weight * (family_gradient - overall_gradient) / member_count
                    )
            weighted_design = design * sample_weights[:, None]
            gradient = (gradient_logits.T @ weighted_design) / max(total_sample_weight, EPS)
            gradient += coefficients * penalties[None, :] / max(total_sample_weight, 1.0)
            first_moment = 0.9 * first_moment + 0.1 * gradient
            second_moment = 0.999 * second_moment + 0.001 * (gradient**2)
            first_hat = first_moment / (1.0 - 0.9**iteration)
            second_hat = second_moment / (1.0 - 0.999**iteration)
            coefficients -= float(learning_rate) * first_hat / (np.sqrt(second_hat) + 1e-8)
            coefficients -= np.mean(coefficients, axis=0, keepdims=True)
            if (
                checkpoint
                and iteration >= max(1, int(minimum_iterations))
                and stable_checkpoints >= max(1, int(early_stopping_patience))
            ):
                converged = True
                break
    artifact = {
        "schema": SCHEMA_VERSION,
        "version": "contextual_stacking_research_v1",
        "research_only": True,
        "production_ready": False,
        "runtime_integrated": False,
        "source_data_policy": "external_only",
        "entities": entity_names,
        "provider_families": families,
        "context_fields": [str(value) for value in context_fields],
        "context_support": context_support,
        "sparse_context_fallback_enabled": True,
        "seasonal_order": max(0, int(seasonal_order)),
        "include_lead_time": bool(include_lead_time),
        "feature_names": feature_names,
        "feature_means": means,
        "feature_scales": scales,
        "coefficients": coefficients.tolist(),
        "weight_constraints": {
            "source_floor": float(source_floor),
            "source_cap": float(source_cap),
            "family_floor": float(family_floor),
            "family_cap": float(family_cap),
        },
        "training": {
            "sample_count_input": len(samples),
            "sample_count_usable": int(np.sum(usable)),
            "effective_sample_weight_sum": total_sample_weight,
            "learning_rate": float(learning_rate),
            "max_iterations": int(max_iterations),
            "iterations_completed": int(iterations_completed),
            "early_stopping_tolerance": float(early_stopping_tolerance),
            "early_stopping_patience": int(early_stopping_patience),
            "minimum_iterations": int(minimum_iterations),
            "converged": bool(converged),
            "l2_intercept": float(l2_intercept),
            "l2_context": float(l2_context),
            "objective_history": objective_history,
        },
        "gates": {
            "strict_oos_evaluated": False,
            "nll_improved": False,
            "rps_improved": False,
            "promotion_eligible": False,
        },
    }
    predicted_training_weights = [
        predict_contextual_weights(artifact, sample, available_entities=entity_names)
        for sample in samples
    ]
    prediction_weights = [max(0.0, float(_float(sample.get("sample_weight")) or 1.0)) for sample in samples]
    prediction_weight_sum = sum(prediction_weights)
    artifact["global_weights"] = (
        {
            entity: float(
                sum(weight * predicted.get(entity, 0.0) for weight, predicted in zip(prediction_weights, predicted_training_weights))
                / max(prediction_weight_sum, EPS)
            )
            for entity in entity_names
        }
        if predicted_training_weights
        else predict_contextual_weights(
            artifact,
            {"event_date_local": "2001-01-01", "lead_hours": 0.0, "context": {}},
            available_entities=entity_names,
        )
    )
    return artifact


def predict_contextual_weights(
    artifact: Mapping[str, Any],
    context_row: Mapping[str, Any],
    *,
    available_entities: Sequence[str] | None = None,
) -> dict[str, float]:
    entities = [str(value) for value in artifact.get("entities") or []]
    available_set = set(str(value) for value in (available_entities or entities))
    active = [entity for entity in entities if entity in available_set]
    if not active:
        return {}
    values = _transform_design(context_row, artifact)
    coefficients = np.asarray(artifact.get("coefficients") or [], dtype=float)
    if coefficients.shape != (len(entities), len(values)):
        raise ValueError("contextual_weight_artifact_coefficient_dimension_mismatch")
    logits = coefficients @ values
    active_indices = [entities.index(entity) for entity in active]
    active_logits = logits[active_indices][None, :]
    active_families = [str((artifact.get("provider_families") or {}).get(entity) or entity) for entity in active]
    family_names = sorted(set(active_families))
    active_family_indices = [family_names.index(family) for family in active_families]
    raw_values = _hierarchical_softmax(
        active_logits,
        np.ones_like(active_logits, dtype=bool),
        active_family_indices,
    )[0][0]
    raw = {entity: float(value) for entity, value in zip(active, raw_values)}
    constraints = artifact.get("weight_constraints") if isinstance(artifact.get("weight_constraints"), Mapping) else {}
    return _apply_joint_source_family_bounds(
        raw,
        provider_families=artifact.get("provider_families") if isinstance(artifact.get("provider_families"), Mapping) else {},
        source_floor=float(constraints.get("source_floor") or 0.0),
        source_cap=float(constraints.get("source_cap") or 1.0),
        family_floor=float(constraints.get("family_floor") or 0.0),
        family_cap=float(constraints.get("family_cap") or 1.0),
    )


def constrain_source_weights(
    raw_weights: Mapping[str, Any],
    *,
    provider_families: Mapping[str, str] | None = None,
    source_floor: float = 0.05,
    source_cap: float = 0.45,
    family_floor: float = 0.05,
    family_cap: float = 0.70,
) -> dict[str, float]:
    """Normalize source weights and apply source/family governance bounds.

    This is also used by the static champion lane so the challenger cannot win
    merely because only one side deduplicates correlated provider interfaces.
    """

    cleaned = {
        str(source): max(0.0, float(value))
        for source, value in raw_weights.items()
        if _float(value) is not None
    }
    family_counts: dict[str, int] = {}
    for source in cleaned:
        family = str((provider_families or {}).get(source) or source)
        family_counts[family] = family_counts.get(family, 0) + 1
    duplicate_neutral = {
        source: value / family_counts[str((provider_families or {}).get(source) or source)]
        for source, value in cleaned.items()
    }
    return _apply_joint_source_family_bounds(
        duplicate_neutral,
        provider_families=provider_families or {},
        source_floor=source_floor,
        source_cap=source_cap,
        family_floor=family_floor,
        family_cap=family_cap,
    )


def mix_source_distributions(
    source_distributions: Mapping[str, Mapping[str, Any]],
    source_weights: Mapping[str, float],
) -> dict[str, float]:
    labels = sorted({str(label) for distribution in source_distributions.values() for label in distribution})
    output = {label: 0.0 for label in labels}
    used_weight = 0.0
    for source, weight in source_weights.items():
        distribution = source_distributions.get(source)
        if not isinstance(distribution, Mapping):
            continue
        used_weight += float(weight)
        for label in labels:
            output[label] += float(weight) * max(0.0, float(distribution.get(label) or 0.0))
    total = sum(output.values())
    if used_weight <= EPS or total <= EPS:
        return {}
    return {label: value / total for label, value in output.items()}


__all__ = [
    "SCHEMA_VERSION",
    "fit_contextual_stacking",
    "mix_source_distributions",
    "predict_contextual_weights",
    "constrain_source_weights",
]
