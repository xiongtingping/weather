"""Bounded walk-forward training for the P0 historical same-day meta layer."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .p0_historical_meta import (
    P0HistoricalMetaCombinationArtifact,
    build_p0_historical_meta_combination_artifact,
)


SCHEMA = "bayes_basket_p0_historical_meta_walk_forward_training_v1"
MAX_TRAINING_FOLDS = 2_000
MAX_COEFFICIENT_CANDIDATES = 101
EPS = 1e-12


def _number(name: str, value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(parsed) or (parsed <= 0.0 if positive else parsed < 0.0):
        raise ContractError(f"{name}_invalid")
    return parsed


def _probabilities(name: str, raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise ContractError(f"{name}_missing")
    parsed = {
        str(bucket): _number(f"{name}_{bucket}", value)
        for bucket, value in raw.items()
    }
    total = sum(parsed.values())
    if total <= EPS or abs(total - 1.0) > 1e-8:
        raise ContractError(f"{name}_not_normalized")
    return parsed


def _centers(raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise ContractError("historical_meta_training_centers_missing")
    parsed: dict[str, float] = {}
    for bucket, value in raw.items():
        if isinstance(value, bool):
            raise ContractError("historical_meta_training_center_invalid")
        try:
            center = float(value)
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "historical_meta_training_center_invalid"
            ) from exc
        if not math.isfinite(center):
            raise ContractError("historical_meta_training_center_invalid")
        parsed[str(bucket)] = center
    if len(set(parsed.values())) != len(parsed):
        raise ContractError("historical_meta_training_centers_duplicate")
    return dict(sorted(parsed.items(), key=lambda item: (item[1], item[0])))


def _training_truths(
    name: str,
    raw: Any,
    *,
    evaluation_truth_id: str,
    evaluation_date: date,
    training_cutoff: datetime,
) -> tuple[dict[str, str], ...]:
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes, bytearray))
        or not raw
        or len(raw) > MAX_TRAINING_FOLDS
    ):
        raise ContractError(
            "historical_meta_training_truth_lineage_invalid"
        )
    parsed: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ContractError(
                "historical_meta_training_truth_lineage_invalid"
            )
        truth_id = str(item.get("truth_id") or "").strip()
        target_date_raw = str(item.get("target_local_date") or "")
        settled_raw = str(item.get("settled_at_utc") or "")
        try:
            target_date = date.fromisoformat(target_date_raw)
            settled = datetime.fromisoformat(
                settled_raw.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ContractError(
                "historical_meta_training_truth_lineage_invalid"
            ) from exc
        if (
            not truth_id
            or truth_id == evaluation_truth_id
            or target_date >= evaluation_date
            or settled.tzinfo is None
            or settled > training_cutoff
        ):
            raise ContractError(
                "historical_meta_training_truth_lineage_invalid"
            )
        parsed.append({
            "truth_id": truth_id,
            "target_local_date": target_date_raw,
            "settled_at_utc": settled_raw,
        })
    if len({item["truth_id"] for item in parsed}) != len(parsed):
        raise ContractError(
            "historical_meta_training_truth_lineage_invalid"
        )
    return tuple(
        sorted(
            parsed,
            key=lambda item: (
                item["target_local_date"],
                item["truth_id"],
            ),
        )
    )


def _expected_center(
    probabilities: Mapping[str, float],
    centers: Mapping[str, float],
) -> float:
    return sum(
        probabilities[bucket] * centers[bucket]
        for bucket in centers
    )


def _tilt(
    probabilities: Mapping[str, float],
    centers: Mapping[str, float],
    *,
    climatology_probabilities: Mapping[str, float],
    coefficient: float,
    max_abs_theta: float,
) -> dict[str, float]:
    model_center = _expected_center(probabilities, centers)
    climate_center = _expected_center(climatology_probabilities, centers)
    theta = max(
        -max_abs_theta,
        min(max_abs_theta, coefficient * (climate_center - model_center)),
    )
    if abs(theta) <= EPS:
        return dict(probabilities)
    scale = max(max(centers.values()) - min(centers.values()), EPS)
    logits: dict[str, float] = {}
    maximum = -math.inf
    for bucket, probability in probabilities.items():
        value = (
            -math.inf
            if probability <= 0.0
            else math.log(probability)
            + theta * ((centers[bucket] - model_center) / scale)
        )
        logits[bucket] = value
        maximum = max(maximum, value)
    weights = {
        bucket: 0.0 if value == -math.inf else math.exp(value - maximum)
        for bucket, value in logits.items()
    }
    total = sum(weights.values())
    if total <= EPS:
        raise ContractError("historical_meta_training_tilt_zero_total")
    return {bucket: value / total for bucket, value in weights.items()}


def _losses(
    probabilities: Mapping[str, float],
    centers: Mapping[str, float],
    truth_bucket: str,
) -> tuple[float, float, float]:
    if truth_bucket not in probabilities:
        raise ContractError("historical_meta_training_truth_bucket_unknown")
    ordered = tuple(centers)
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
    truth_index = ordered.index(truth_bucket)
    crps = 0.0
    for index, bucket in enumerate(ordered[:-1]):
        cumulative += probabilities[bucket]
        observation_cdf = 1.0 if index >= truth_index else 0.0
        width = centers[ordered[index + 1]] - centers[bucket]
        if width <= 0.0:
            raise ContractError(
                "historical_meta_training_centers_not_increasing"
            )
        crps += (cumulative - observation_cdf) ** 2 * width
    return nll, brier, crps


def _metrics(
    rows: Sequence[dict[str, Any]],
    probabilities_by_fold: Sequence[Mapping[str, float]],
) -> dict[str, float]:
    total_weight = sum(row["weight"] for row in rows)
    if total_weight <= EPS:
        raise ContractError("historical_meta_training_weight_zero")
    totals = [0.0, 0.0, 0.0]
    for row, probabilities in zip(rows, probabilities_by_fold):
        losses = _losses(
            probabilities,
            row["bucket_centers_c"],
            row["truth_bucket"],
        )
        for index, loss in enumerate(losses):
            totals[index] += row["weight"] * loss
    return {
        "nll": totals[0] / total_weight,
        "brier_score": totals[1] / total_weight,
        "crps": totals[2] / total_weight,
    }


def _objective(metrics: Mapping[str, float], temperature_range: float) -> float:
    return (
        float(metrics["nll"])
        + float(metrics["brier_score"])
        + float(metrics["crps"]) / max(temperature_range, EPS)
    )


def _select_coefficient(
    rows: Sequence[dict[str, Any]],
    candidates: Sequence[float],
    *,
    max_abs_theta: float,
) -> float:
    if not rows:
        return 0.0
    temperature_range = max(
        max(row["bucket_centers_c"].values())
        - min(row["bucket_centers_c"].values())
        for row in rows
    )
    ranked: list[tuple[float, float, float]] = []
    for coefficient in candidates:
        probabilities = [
            _tilt(
                row["model_probabilities"],
                row["bucket_centers_c"],
                climatology_probabilities=row[
                    "climatology_probabilities"
                ],
                coefficient=coefficient,
                max_abs_theta=max_abs_theta,
            )
            for row in rows
        ]
        metric = _metrics(rows, probabilities)
        ranked.append(
            (
                _objective(metric, temperature_range),
                abs(coefficient),
                coefficient,
            )
        )
    return min(ranked)[2]


def train_p0_historical_meta_artifact(
    *,
    folds: Iterable[Mapping[str, Any]],
    coefficient_candidates: Iterable[float],
    minimum_meta_training_folds: int,
    artifact_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Fit one final coefficient and score it with strict expanding-window OOS.

    Each evaluation fold selects its coefficient using only earlier dated
    folds. The final coefficient is then fitted on all settled folds for the
    future activation window. No target/future truth is consumed.
    """

    raw_folds = list(folds)
    if not raw_folds or len(raw_folds) > MAX_TRAINING_FOLDS:
        raise ContractError("historical_meta_training_fold_count_invalid")
    candidates = sorted(
        {
            float(value)
            for value in coefficient_candidates
            if not isinstance(value, bool) and math.isfinite(float(value))
        }
    )
    if (
        not candidates
        or len(candidates) > MAX_COEFFICIENT_CANDIDATES
        or 0.0 not in candidates
    ):
        raise ContractError(
            "historical_meta_training_coefficient_grid_invalid"
        )
    if (
        isinstance(minimum_meta_training_folds, bool)
        or minimum_meta_training_folds < 1
    ):
        raise ContractError(
            "historical_meta_training_minimum_folds_invalid"
        )
    parsed: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for raw in raw_folds:
        if not isinstance(raw, Mapping):
            raise ContractError("historical_meta_training_fold_invalid")
        target_date = str(raw.get("target_local_date") or "")
        try:
            date.fromisoformat(target_date)
        except ValueError as exc:
            raise ContractError(
                "historical_meta_training_target_date_invalid"
            ) from exc
        if target_date in seen_dates:
            raise ContractError(
                "historical_meta_training_duplicate_target_date"
            )
        seen_dates.add(target_date)
        centers = _centers(raw.get("bucket_centers_c"))
        model = _probabilities(
            "historical_meta_training_model_probabilities",
            raw.get("model_probabilities"),
        )
        climate = _probabilities(
            "historical_meta_training_climatology_probabilities",
            raw.get("climatology_probabilities"),
        )
        if set(model) != set(centers) or set(climate) != set(centers):
            raise ContractError(
                "historical_meta_training_bucket_contract_mismatch"
            )
        evaluation_truth_id = str(
            raw.get("evaluation_truth_id") or ""
        ).strip()
        training_cutoff_raw = str(raw.get("training_cutoff_utc") or "")
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
                "historical_meta_training_truth_time_invalid"
            ) from exc
        if (
            training_cutoff.tzinfo is None
            or evaluation_settled.tzinfo is None
            or training_cutoff >= evaluation_settled
        ):
            raise ContractError(
                "historical_meta_training_truth_time_invalid"
            )
        model_training_truths = _training_truths(
            "model_training_truths",
            raw.get("model_training_truths"),
            evaluation_truth_id=evaluation_truth_id,
            evaluation_date=date.fromisoformat(target_date),
            training_cutoff=training_cutoff,
        )
        climate_training_truths = _training_truths(
            "climatology_training_truths",
            raw.get("climatology_training_truths"),
            evaluation_truth_id=evaluation_truth_id,
            evaluation_date=date.fromisoformat(target_date),
            training_cutoff=training_cutoff,
        )
        model_training = tuple(
            item["truth_id"] for item in model_training_truths
        )
        climate_training = tuple(
            item["truth_id"] for item in climate_training_truths
        )
        if (
            not evaluation_truth_id
            or not model_training
            or not climate_training
            or evaluation_truth_id in model_training
            or evaluation_truth_id in climate_training
        ):
            raise ContractError(
                "historical_meta_training_truth_lineage_invalid"
            )
        parsed.append({
            "fold_id": str(raw.get("fold_id") or "").strip(),
            "target_local_date": target_date,
            "evaluation_truth_id": evaluation_truth_id,
            "evaluation_truth_settled_at_utc": evaluation_settled_raw,
            "training_cutoff_utc": training_cutoff_raw,
            "model_probability_sha256": content_sha256({
                "bucket_centers_c": centers,
                "probabilities": model,
            }),
            "model_training_truth_ids": model_training,
            "climatology_training_truth_ids": climate_training,
            "model_training_truths": model_training_truths,
            "climatology_training_truths": climate_training_truths,
            "climatology_artifact_sha256": str(
                raw.get("climatology_artifact_sha256") or ""
            ).lower(),
            "model_probabilities": model,
            "climatology_probabilities": climate,
            "bucket_centers_c": centers,
            "truth_bucket": str(raw.get("truth_bucket") or ""),
            "weight": _number(
                "historical_meta_training_fold_weight",
                raw.get("weight", 1.0),
                positive=True,
            ),
        })
    parsed.sort(key=lambda row: (row["target_local_date"], row["fold_id"]))
    if any(not row["fold_id"] for row in parsed):
        raise ContractError("historical_meta_training_fold_id_missing")
    trained_through = datetime.fromisoformat(
        str(artifact_parameters.get("trained_through_utc") or "").replace(
            "Z", "+00:00"
        )
    )
    if trained_through.tzinfo is None:
        raise ContractError(
            "historical_meta_training_trained_through_timezone_required"
        )
    if any(
        date.fromisoformat(row["target_local_date"])
        > trained_through.date()
        for row in parsed
    ):
        raise ContractError("historical_meta_training_future_truth_leakage")
    max_abs_theta = _number(
        "historical_meta_training_max_abs_theta",
        artifact_parameters.get("max_abs_theta"),
    )
    oos_probabilities: list[Mapping[str, float]] = []
    selected_by_fold: list[dict[str, Any]] = []
    for index, row in enumerate(parsed):
        prior = parsed[:index]
        coefficient = (
            _select_coefficient(
                prior,
                candidates,
                max_abs_theta=max_abs_theta,
            )
            if len(prior) >= minimum_meta_training_folds
            else 0.0
        )
        oos_probabilities.append(
            _tilt(
                row["model_probabilities"],
                row["bucket_centers_c"],
                climatology_probabilities=row[
                    "climatology_probabilities"
                ],
                coefficient=coefficient,
                max_abs_theta=max_abs_theta,
            )
        )
        selected_by_fold.append({
            "fold_id": row["fold_id"],
            "target_local_date": row["target_local_date"],
            "prior_fold_count": len(prior),
            "selected_coefficient": coefficient,
        })
    baseline_metrics = _metrics(
        parsed, [row["model_probabilities"] for row in parsed]
    )
    candidate_metrics = _metrics(parsed, oos_probabilities)
    final_coefficient = _select_coefficient(
        parsed,
        candidates,
        max_abs_theta=max_abs_theta,
    )
    weights = [row["weight"] for row in parsed]
    effective_sample_n = sum(weights) ** 2 / sum(
        value * value for value in weights
    )
    training_lineage_folds = [
        {
            key: row[key]
            for key in (
                "fold_id",
                "target_local_date",
                "evaluation_truth_id",
                "evaluation_truth_settled_at_utc",
                "training_cutoff_utc",
                "model_probability_sha256",
                "model_training_truth_ids",
                "climatology_training_truth_ids",
                "model_training_truths",
                "climatology_training_truths",
                "climatology_artifact_sha256",
            )
        }
        for row in parsed
    ]
    oof_folds = [
        {
            key: row[key]
            for key in (
                "fold_id",
                "target_local_date",
                "evaluation_truth_id",
                "model_probability_sha256",
                "model_training_truth_ids",
                "climatology_training_truth_ids",
                "climatology_artifact_sha256",
            )
        }
        for row in parsed
    ]
    artifact = build_p0_historical_meta_combination_artifact(
        **dict(artifact_parameters),
        oof_folds=oof_folds,
        effective_sample_n=effective_sample_n,
        baseline_oos_metrics=baseline_metrics,
        candidate_oos_metrics=candidate_metrics,
        center_delta_to_theta=final_coefficient,
    )
    evidence = {
        "schema": SCHEMA,
        "fold_count": len(parsed),
        "minimum_meta_training_folds": minimum_meta_training_folds,
        "coefficient_candidates": candidates,
        "selected_by_fold": selected_by_fold,
        "training_lineage_folds": training_lineage_folds,
        "final_coefficient": final_coefficient,
        "effective_sample_n": effective_sample_n,
        "baseline_oos_metrics": baseline_metrics,
        "candidate_oos_metrics": candidate_metrics,
        "stable_oos_gain": artifact.stable_oos_gain,
        "artifact_id": artifact.artifact_id,
        "artifact_content_sha256": artifact.content_sha256,
    }
    evidence["content_sha256"] = content_sha256(evidence)
    return {"artifact": artifact.to_dict(), "training_evidence": evidence}


__all__ = [
    "MAX_COEFFICIENT_CANDIDATES",
    "MAX_TRAINING_FOLDS",
    "SCHEMA",
    "train_p0_historical_meta_artifact",
]
