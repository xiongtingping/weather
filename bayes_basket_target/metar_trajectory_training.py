"""Bounded expanding-window OOS training for METAR trajectory likelihood."""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .metar_trajectory import (
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
    METAR_TRAJECTORY_AUTHORITY_SCOPE,
    METAR_TRAJECTORY_METHOD_VERSION,
    build_metar_trajectory_likelihood_artifact,
    maturity_strength_for_sample_n,
)
from .metar_path_state import (
    ConditionalMetarPathState,
    condition_remaining_warming_distribution,
)


SCHEMA = "bayes_basket_metar_trajectory_walk_forward_training_v1"
MAX_TRAINING_FOLDS = 4_000
MAX_SMOOTHING_CANDIDATES = 101
EPS = 1e-12


def _number(name: str, value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(parsed):
        raise ContractError(f"{name}_invalid")
    if positive and parsed <= 0.0:
        raise ContractError(f"{name}_invalid")
    return parsed


def _distribution(name: str, raw: Any) -> dict[float, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise ContractError(f"{name}_missing")
    parsed = {
        _number(f"{name}_increment", key): _number(
            f"{name}_probability", value
        )
        for key, value in raw.items()
    }
    if len(parsed) != len(raw) or 0.0 not in parsed:
        raise ContractError(f"{name}_support_invalid")
    if any(key < 0.0 or value < 0.0 for key, value in parsed.items()):
        raise ContractError(f"{name}_value_invalid")
    if abs(sum(parsed.values()) - 1.0) > 1e-8:
        raise ContractError(f"{name}_not_normalized")
    return dict(sorted(parsed.items()))


def _losses(
    probabilities: Mapping[float, float],
    truth_increment: float,
) -> tuple[float, float, float]:
    if truth_increment not in probabilities:
        raise ContractError("metar_trajectory_training_truth_increment_unknown")
    ordered = tuple(sorted(probabilities))
    truth_index = ordered.index(truth_increment)
    nll = -math.log(max(probabilities[truth_increment], 1e-15))
    brier = sum(
        (
            probabilities[increment]
            - (1.0 if increment == truth_increment else 0.0)
        )
        ** 2
        for increment in ordered
    )
    cumulative = 0.0
    crps = 0.0
    for index, increment in enumerate(ordered[:-1]):
        cumulative += probabilities[increment]
        observed = 1.0 if index >= truth_index else 0.0
        width = ordered[index + 1] - increment
        if width <= 0.0:
            raise ContractError(
                "metar_trajectory_training_increment_support_invalid"
            )
        crps += (cumulative - observed) ** 2 * width
    return nll, brier, crps


def _posterior_distribution(
    rows: Sequence[Mapping[str, Any]],
    *,
    baseline: Mapping[float, float],
    smoothing: float,
) -> dict[float, float]:
    counts = {increment: 0.0 for increment in baseline}
    total = 0.0
    for row in rows:
        increment = float(row["remaining_warming_c"])
        weight = float(row["weight"])
        counts[increment] += weight
        total += weight
    denominator = total + smoothing
    if denominator <= EPS:
        return dict(baseline)
    return {
        increment: (
            counts[increment] + smoothing * baseline[increment]
        )
        / denominator
        for increment in baseline
    }


def _metrics(
    rows: Sequence[Mapping[str, Any]],
    distributions: Sequence[Mapping[float, float]],
) -> dict[str, float]:
    total_weight = sum(float(row["weight"]) for row in rows)
    if total_weight <= EPS:
        raise ContractError("metar_trajectory_training_weight_zero")
    totals = [0.0, 0.0, 0.0]
    for row, distribution in zip(rows, distributions):
        losses = _losses(
            distribution,
            float(row["remaining_warming_c"]),
        )
        for index, loss in enumerate(losses):
            totals[index] += float(row["weight"]) * loss
    return {
        "nll": totals[0] / total_weight,
        "brier_score": totals[1] / total_weight,
        "crps": totals[2] / total_weight,
    }


def _bucket_distribution(name: str, raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise ContractError(f"{name}_missing")
    parsed = {
        str(bucket): _number(f"{name}_{bucket}", value)
        for bucket, value in raw.items()
    }
    if (
        any(value < 0.0 for value in parsed.values())
        or abs(sum(parsed.values()) - 1.0) > 1e-8
    ):
        raise ContractError(f"{name}_not_normalized")
    return parsed


def _bucket_geometry(name: str, raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise ContractError(f"{name}_missing")
    return {
        str(bucket): _number(f"{name}_{bucket}", value)
        for bucket, value in raw.items()
    }


def _nearest_probability(
    distribution: Mapping[float, float],
    value: float,
) -> float:
    nearest = min(
        distribution,
        key=lambda increment: (abs(increment - value), increment),
    )
    return float(distribution[nearest])


def _posterior_bucket_probabilities(
    row: Mapping[str, Any],
    *,
    remaining_distribution: Mapping[float, float],
    baseline_distribution: Mapping[float, float],
    strength: float,
) -> dict[str, float]:
    root = row["root_probabilities"]
    centers = row["bucket_centers_c"]
    upper_bounds = row["bucket_upper_bounds_c"]
    running_high = float(row["running_high_c"])
    weighted: dict[str, float] = {}
    for bucket, probability in root.items():
        if upper_bounds[bucket] < running_high - EPS:
            weighted[bucket] = 0.0
            continue
        increment = max(0.0, centers[bucket] - running_high)
        numerator = _nearest_probability(
            remaining_distribution, increment
        )
        denominator = _nearest_probability(
            baseline_distribution, increment
        )
        ratio = 1.0 if denominator <= 0.0 else numerator / denominator
        ratio = max(0.25, min(4.0, ratio))
        weighted[bucket] = probability * math.exp(
            strength * math.log(ratio)
        )
    total = sum(weighted.values())
    if total <= EPS:
        raise ContractError(
            "metar_trajectory_training_posterior_zero_mass"
        )
    return {bucket: value / total for bucket, value in weighted.items()}


def _bucket_metrics(
    rows: Sequence[Mapping[str, Any]],
    distributions: Sequence[Mapping[str, float]],
) -> dict[str, float]:
    total_weight = sum(float(row["weight"]) for row in rows)
    totals = [0.0, 0.0, 0.0]
    for row, probabilities in zip(rows, distributions):
        truth_bucket = str(row["truth_bucket"])
        centers = row["bucket_centers_c"]
        if truth_bucket not in probabilities:
            raise ContractError(
                "metar_trajectory_training_truth_bucket_unknown"
            )
        ordered = tuple(
            sorted(probabilities, key=lambda bucket: centers[bucket])
        )
        truth_index = ordered.index(truth_bucket)
        nll = -math.log(max(probabilities[truth_bucket], 1e-15))
        brier = sum(
            (
                probabilities[bucket]
                - (1.0 if bucket == truth_bucket else 0.0)
            )
            ** 2
            for bucket in ordered
        )
        cumulative = 0.0
        crps = 0.0
        for index, bucket in enumerate(ordered[:-1]):
            cumulative += probabilities[bucket]
            observed = 1.0 if index >= truth_index else 0.0
            width = centers[ordered[index + 1]] - centers[bucket]
            if width <= 0.0:
                raise ContractError(
                    "metar_trajectory_training_bucket_geometry_invalid"
                )
            crps += (cumulative - observed) ** 2 * width
        weight = float(row["weight"])
        for index, loss in enumerate((nll, brier, crps)):
            totals[index] += weight * loss
    return {
        "nll": totals[0] / total_weight,
        "brier_score": totals[1] / total_weight,
        "crps": totals[2] / total_weight,
    }


def _objective(
    metrics: Mapping[str, float],
    support_range: float,
) -> float:
    return (
        float(metrics["nll"])
        + float(metrics["brier_score"])
        + float(metrics["crps"]) / max(support_range, EPS)
    )


def _select_smoothing(
    scored_rows: Sequence[Mapping[str, Any]],
    candidates: Sequence[float],
    *,
    baseline: Mapping[float, float],
) -> float:
    if not scored_rows:
        return max(candidates)
    support_range = max(baseline) - min(baseline)
    ranked: list[tuple[float, float]] = []
    for smoothing in candidates:
        predictions = [
            _posterior_distribution(
                scored_rows[:index],
                baseline=baseline,
                smoothing=smoothing,
            )
            for index in range(len(scored_rows))
        ]
        ranked.append(
            (
                _objective(
                    _metrics(scored_rows, predictions),
                    support_range,
                ),
                smoothing,
            )
        )
    return min(ranked)[1]


def train_metar_trajectory_artifact(
    *,
    folds: Iterable[Mapping[str, Any]],
    smoothing_candidates: Iterable[float],
    minimum_training_folds: int,
    minimum_effective_sample_n: float,
    artifact_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Train one future artifact using strict dated expanding-window OOS."""

    raw_folds = list(folds)
    if not raw_folds or len(raw_folds) > MAX_TRAINING_FOLDS:
        raise ContractError("metar_trajectory_training_fold_count_invalid")
    candidates = sorted({
        _number(
            "metar_trajectory_training_smoothing_candidate",
            value,
            positive=True,
        )
        for value in smoothing_candidates
    })
    if not candidates or len(candidates) > MAX_SMOOTHING_CANDIDATES:
        raise ContractError(
            "metar_trajectory_training_smoothing_grid_invalid"
        )
    if (
        isinstance(minimum_training_folds, bool)
        or minimum_training_folds < 1
    ):
        raise ContractError(
            "metar_trajectory_training_minimum_folds_invalid"
        )
    minimum_effective_n = _number(
        "metar_trajectory_training_minimum_effective_sample_n",
        minimum_effective_sample_n,
        positive=True,
    )
    baseline = _distribution(
        "metar_trajectory_training_baseline_distribution",
        artifact_parameters.get("hard_exclusion_baseline_distribution"),
    )
    method_version = str(
        artifact_parameters.get("method_version")
        or METAR_TRAJECTORY_METHOD_VERSION
    )
    conditional_path_model = method_version in {
        METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
        METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    }
    hourly_curve_model = (
        method_version
        == METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
    )
    parsed: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for raw in raw_folds:
        if not isinstance(raw, Mapping):
            raise ContractError("metar_trajectory_training_fold_invalid")
        target_date = str(raw.get("target_local_date") or "")
        try:
            date.fromisoformat(target_date)
        except ValueError as exc:
            raise ContractError(
                "metar_trajectory_training_target_date_invalid"
            ) from exc
        if target_date in seen_dates:
            raise ContractError(
                "metar_trajectory_training_duplicate_target_date"
            )
        seen_dates.add(target_date)
        truth_id = str(raw.get("evaluation_truth_id") or "").strip()
        training_cutoff_raw = str(
            raw.get("training_cutoff_utc") or ""
        )
        evaluation_settled_raw = str(
            raw.get("evaluation_truth_settled_at_utc") or ""
        )
        try:
            training_cutoff = datetime.fromisoformat(
                training_cutoff_raw.replace("Z", "+00:00")
            )
            evaluation_settled = datetime.fromisoformat(
                evaluation_settled_raw.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ContractError(
                "metar_trajectory_training_truth_time_invalid"
            ) from exc
        if (
            training_cutoff.tzinfo is None
            or evaluation_settled.tzinfo is None
            or training_cutoff >= evaluation_settled
        ):
            raise ContractError(
                "metar_trajectory_training_truth_time_invalid"
            )
        training_truths_raw = raw.get("trajectory_training_truths")
        if (
            not isinstance(training_truths_raw, Sequence)
            or isinstance(
                training_truths_raw, (str, bytes, bytearray)
            )
            or not training_truths_raw
            or len(training_truths_raw) > MAX_TRAINING_FOLDS
        ):
            raise ContractError(
                "metar_trajectory_training_truth_lineage_invalid"
            )
        training_truths: list[dict[str, str]] = []
        for training_truth in training_truths_raw:
            if not isinstance(training_truth, Mapping):
                raise ContractError(
                    "metar_trajectory_training_truth_lineage_invalid"
                )
            training_id = str(
                training_truth.get("truth_id") or ""
            ).strip()
            training_date_raw = str(
                training_truth.get("target_local_date") or ""
            )
            training_settled_raw = str(
                training_truth.get("settled_at_utc") or ""
            )
            try:
                training_date = date.fromisoformat(training_date_raw)
                training_settled = datetime.fromisoformat(
                    training_settled_raw.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ContractError(
                    "metar_trajectory_training_truth_lineage_invalid"
                ) from exc
            if (
                not training_id
                or training_id == truth_id
                or training_date >= date.fromisoformat(target_date)
                or training_settled.tzinfo is None
                or training_settled > training_cutoff
            ):
                raise ContractError(
                    "metar_trajectory_training_truth_lineage_invalid"
                )
            training_truths.append({
                "truth_id": training_id,
                "target_local_date": training_date_raw,
                "settled_at_utc": training_settled_raw,
            })
        training_truths.sort(
            key=lambda value: (
                value["target_local_date"],
                value["truth_id"],
            )
        )
        training_ids = tuple(
            value["truth_id"] for value in training_truths
        )
        if (
            not truth_id
            or len(training_ids) != len(set(training_ids))
        ):
            raise ContractError(
                "metar_trajectory_training_truth_lineage_invalid"
            )
        increment = _number(
            "metar_trajectory_training_remaining_warming_c",
            raw.get("remaining_warming_c"),
        )
        if increment not in baseline:
            raise ContractError(
                "metar_trajectory_training_truth_increment_unknown"
            )
        probability_fields = (
            "root_probabilities",
            "bucket_centers_c",
            "bucket_upper_bounds_c",
            "truth_bucket",
            "running_high_c",
        )
        supplied_probability_fields = tuple(
            raw.get(field) is not None for field in probability_fields
        )
        if any(supplied_probability_fields) and not all(
            supplied_probability_fields
        ):
            raise ContractError(
                "metar_trajectory_training_probability_oos_incomplete"
            )
        probability_payload: dict[str, Any] = {}
        if all(supplied_probability_fields):
            root_probabilities = _bucket_distribution(
                "metar_trajectory_training_root_probabilities",
                raw.get("root_probabilities"),
            )
            bucket_centers = _bucket_geometry(
                "metar_trajectory_training_bucket_centers_c",
                raw.get("bucket_centers_c"),
            )
            bucket_upper_bounds = _bucket_geometry(
                "metar_trajectory_training_bucket_upper_bounds_c",
                raw.get("bucket_upper_bounds_c"),
            )
            if (
                set(root_probabilities) != set(bucket_centers)
                or set(root_probabilities) != set(bucket_upper_bounds)
                or str(raw.get("truth_bucket") or "")
                not in root_probabilities
            ):
                raise ContractError(
                    "metar_trajectory_training_probability_oos_scope_invalid"
                )
            probability_payload = {
                "root_probabilities": root_probabilities,
                "bucket_centers_c": bucket_centers,
                "bucket_upper_bounds_c": bucket_upper_bounds,
                "truth_bucket": str(raw.get("truth_bucket")),
                "running_high_c": _number(
                    "metar_trajectory_training_running_high_c",
                    raw.get("running_high_c"),
                ),
            }
        path_state_payload: dict[str, Any] = {}
        if raw.get("conditional_path_state") is not None:
            path_state = ConditionalMetarPathState.from_mapping(
                raw.get("conditional_path_state")
            )
            if datetime.fromisoformat(
                path_state.as_of_utc.replace("Z", "+00:00")
            ) != training_cutoff:
                raise ContractError(
                    "metar_trajectory_training_path_state_cutoff_mismatch"
                )
            path_state_payload = {"conditional_path_state": path_state}
        parsed.append({
            "fold_id": str(raw.get("fold_id") or "").strip(),
            "target_local_date": target_date,
            "evaluation_truth_id": truth_id,
            "evaluation_truth_settled_at_utc": evaluation_settled_raw,
            "training_cutoff_utc": training_cutoff_raw,
            "trajectory_training_truths": training_truths,
            "canonical_observation_sequence_sha256": str(
                raw.get("canonical_observation_sequence_sha256") or ""
            ).lower(),
            "remaining_warming_c": increment,
            "weight": _number(
                "metar_trajectory_training_fold_weight",
                raw.get("weight", 1.0),
                positive=True,
            ),
            **probability_payload,
            **path_state_payload,
        })
    parsed.sort(key=lambda row: (row["target_local_date"], row["fold_id"]))
    if any(not row["fold_id"] for row in parsed):
        raise ContractError("metar_trajectory_training_fold_id_missing")
    if any(
        len(row["canonical_observation_sequence_sha256"]) != 64
        or any(
            char not in "0123456789abcdef"
            for char in row["canonical_observation_sequence_sha256"]
        )
        for row in parsed
    ):
        raise ContractError(
            "metar_trajectory_training_observation_lineage_invalid"
        )
    incomplete_path_fold_count = 0
    incomplete_future_curve_fold_count = 0
    if conditional_path_model:
        incomplete_path_fold_count = sum(
            not isinstance(row.get("conditional_path_state"), ConditionalMetarPathState)
            or not row["conditional_path_state"].complete
            for row in parsed
        )
        complete_path_rows = [
            row
            for row in parsed
            if isinstance(row.get("conditional_path_state"), ConditionalMetarPathState)
            and row["conditional_path_state"].complete
        ]
        if hourly_curve_model:
            incomplete_future_curve_fold_count = sum(
                not row["conditional_path_state"].future_curve_complete
                for row in complete_path_rows
            )
            complete_path_rows = [
                row
                for row in complete_path_rows
                if row["conditional_path_state"].future_curve_complete
            ]
        parsed = complete_path_rows
        if not parsed:
            raise ContractError(
                "metar_trajectory_training_complete_hourly_curve_folds_missing"
                if hourly_curve_model
                else "metar_trajectory_training_complete_path_folds_missing"
            )
    trained_through = datetime.fromisoformat(
        str(artifact_parameters.get("trained_through_utc") or "").replace(
            "Z", "+00:00"
        )
    )
    if trained_through.tzinfo is None:
        raise ContractError(
            "metar_trajectory_training_trained_through_timezone_required"
        )
    if any(
        date.fromisoformat(row["target_local_date"])
        > trained_through.date()
        for row in parsed
    ):
        raise ContractError(
            "metar_trajectory_training_future_truth_leakage"
        )

    predictions: list[Mapping[float, float]] = []
    selected_by_fold: list[dict[str, Any]] = []
    for index, row in enumerate(parsed):
        prior = parsed[:index]
        smoothing = (
            _select_smoothing(
                prior,
                candidates,
                baseline=baseline,
            )
            if len(prior) >= minimum_training_folds
            else max(candidates)
        )
        prediction = (
            _posterior_distribution(
                prior,
                baseline=baseline,
                smoothing=smoothing,
            )
            if len(prior) >= minimum_training_folds
            else dict(baseline)
        )
        predictions.append(prediction)
        selected_by_fold.append({
            "fold_id": row["fold_id"],
            "target_local_date": row["target_local_date"],
            "prior_fold_count": len(prior),
            "selected_smoothing": smoothing,
        })
    baseline_metrics = _metrics(
        parsed, [baseline for _ in parsed]
    )
    candidate_metrics = _metrics(parsed, predictions)
    increment_stable_oos_gain = all(
        candidate_metrics[name] <= baseline_metrics[name] + 1e-12
        for name in ("brier_score", "nll", "crps")
    )
    weights = [float(row["weight"]) for row in parsed]
    effective_sample_n = sum(weights) ** 2 / sum(
        value * value for value in weights
    )
    final_smoothing = _select_smoothing(
        parsed,
        candidates,
        baseline=baseline,
    )
    final_distribution = _posterior_distribution(
        parsed,
        baseline=baseline,
        smoothing=final_smoothing,
    )
    tail_increment = max(baseline)
    observed_tail = (
        sum(
            row["weight"]
            for row in parsed
            if row["remaining_warming_c"] == tail_increment
        )
        / sum(weights)
    )
    tail_calibration_error = abs(
        final_distribution[tail_increment] - observed_tail
    )
    probability_oos_available = all(
        "root_probabilities" in row for row in parsed
    )
    probability_baseline_metrics: dict[str, float] | None = None
    probability_candidate_metrics: dict[str, float] | None = None
    stable_oos_gain = False
    # The maturity the artifact will actually carry, not the configured
    # ceiling.  The OOS gain below has to be measured at the strength that
    # will be applied in production or the gate is scoring a model nobody
    # runs -- and with the ramp in place the two now differ most exactly
    # where it matters, on the small samples this gate newly admits.
    ramped_maturity_strength = maturity_strength_for_sample_n(
        effective_sample_n,
        full_strength=_number(
            "metar_trajectory_training_maturity_strength",
            artifact_parameters.get("maturity_strength"),
        ),
    )
    if probability_oos_available:
        strength = _number(
            "metar_trajectory_training_base_strength",
            artifact_parameters.get("base_strength"),
        ) * ramped_maturity_strength
        probability_baseline_metrics = _bucket_metrics(
            parsed,
            [
                _posterior_bucket_probabilities(
                    row,
                    remaining_distribution=baseline,
                    baseline_distribution=baseline,
                    strength=0.0,
                )
                for row in parsed
            ],
        )
        probability_predictions = predictions
        if conditional_path_model:
            probability_predictions = [
                condition_remaining_warming_distribution(
                    prediction,
                    row["conditional_path_state"],
                    coefficients=(
                        artifact_parameters.get("path_model_coefficients")
                        or {}
                    ),
                    sigma_c=artifact_parameters.get("path_model_sigma_c"),
                )
                for row, prediction in zip(parsed, predictions)
            ]
        probability_candidate_metrics = _bucket_metrics(
            parsed,
            [
                _posterior_bucket_probabilities(
                    row,
                    remaining_distribution=prediction,
                    baseline_distribution=baseline,
                    strength=strength,
                )
                for row, prediction in zip(parsed, probability_predictions)
            ],
        )
        stable_oos_gain = all(
            probability_candidate_metrics[name]
            <= probability_baseline_metrics[name] + 1e-12
            for name in ("brier_score", "nll", "crps")
        )
    gate_passed = (
        probability_oos_available
        and stable_oos_gain
        and effective_sample_n + EPS >= minimum_effective_n
    )
    payload = dict(artifact_parameters)
    payload.update({
        # Ship the strength the gate scored.  Leaving the configured ceiling
        # here would let production apply more correction than any fold ever
        # justified.
        "maturity_strength": ramped_maturity_strength,
        "remaining_warming_distribution": {
            format(key, ".12g"): value
            for key, value in final_distribution.items()
        },
        "hard_exclusion_baseline_distribution": {
            format(key, ".12g"): value
            for key, value in baseline.items()
        },
        "sample_n": len(parsed),
        "effective_sample_n": effective_sample_n,
        "oos_metrics": (
            probability_candidate_metrics
            if probability_candidate_metrics is not None
            else candidate_metrics
        ),
        "tail_calibration_error": tail_calibration_error,
        "promotion_state": (
            "FORMAL_PROMOTED" if gate_passed else "CANDIDATE_ONLY"
        ),
        "authority_scope": METAR_TRAJECTORY_AUTHORITY_SCOPE,
        "activated": gate_passed,
        "method_version": method_version,
    })
    artifact = build_metar_trajectory_likelihood_artifact(payload)
    evidence = {
        "schema": SCHEMA,
        "fold_count": len(parsed),
        "input_fold_count": len(raw_folds),
        "incomplete_path_fold_count": incomplete_path_fold_count,
        "incomplete_future_curve_fold_count": (
            incomplete_future_curve_fold_count
        ),
        "minimum_training_folds": minimum_training_folds,
        "minimum_effective_sample_n": minimum_effective_n,
        "smoothing_candidates": candidates,
        "selected_by_fold": selected_by_fold,
        "final_smoothing": final_smoothing,
        "effective_sample_n": effective_sample_n,
        "baseline_oos_metrics": baseline_metrics,
        "candidate_oos_metrics": candidate_metrics,
        "increment_distribution_stable_oos_gain": (
            increment_stable_oos_gain
        ),
        "formal_probability_oos_available": probability_oos_available,
        "probability_baseline_oos_metrics": (
            probability_baseline_metrics
        ),
        "probability_candidate_oos_metrics": (
            probability_candidate_metrics
        ),
        "stable_oos_gain": stable_oos_gain,
        "oos_gate_passed": gate_passed,
        "oof_folds": [
            {
                key: row[key]
                for key in (
                    "fold_id",
                    "target_local_date",
                    "evaluation_truth_id",
                    "evaluation_truth_settled_at_utc",
                    "training_cutoff_utc",
                    "trajectory_training_truths",
                    "canonical_observation_sequence_sha256",
                )
            }
            for row in parsed
        ],
        "artifact_id": artifact.artifact_id,
        "artifact_content_sha256": artifact.content_sha256,
    }
    evidence["content_sha256"] = content_sha256(evidence)
    return {
        "artifact": artifact.to_dict(),
        "training_evidence": evidence,
    }


__all__ = [
    "MAX_SMOOTHING_CANDIDATES",
    "MAX_TRAINING_FOLDS",
    "SCHEMA",
    "train_metar_trajectory_artifact",
]
