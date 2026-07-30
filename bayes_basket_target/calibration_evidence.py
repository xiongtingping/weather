"""Build target-lane calibration evidence without creating production authority.

The historical files available today are useful for fitting and replay, but
they do not prove canonical publication/receipt timing and the observation
file is an IEM mirror.  This module therefore emits content-addressed
candidate artifacts and explicit rejected promotion decisions.  It never
opens SQLite, reads market state, or calls an order gateway.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .calibration import (
    fit_model_calibration_artifact,
    validate_formal_ensemble_artifact,
)
from .contracts import (
    ContractError,
    FORMAL_ENSEMBLE_METHOD_VERSION,
    MIN_FORMAL_ENSEMBLE_MODELS,
    MODEL_IDS,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    ModelEnsembleSelectionEvidence,
    ResidualObservation,
    content_sha256,
    model_set_hash,
    timestamp_lte,
)
from .strict_asof_replay import (
    evaluate_strict_asof_replay,
    lead_bucket_for_maturity,
    materialize_model_station_days,
    materialize_observation_station_days,
)
from .et_history import event_day_window
from .historical_model_rows import load_historical_model_rows, select_preferred_model_rows
from .model_arrival import INDEX_SCHEMA, load_model_arrival_receipts, model_arrival_key
from .model_sources import MODEL_ROLES, model_id_for_provider, provider_role, provider_source_class


SCHEMA = "bayes_basket_target_calibration_evidence_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
CALIBRATION_GATE_VERSION = "bayes_basket_calibration_promotion_gate_v1"
# Mean-bias correction is deliberately more data-hungry than residual-scale
# estimation.  With fewer than this many closed settlement days, keep the forecast
# center unchanged and use the historical residuals only for dispersion.
MIN_BIAS_CORRECTION_SAMPLE_N = 20
ENSEMBLE_SELECTION_METHOD = FORMAL_ENSEMBLE_METHOD_VERSION
ENSEMBLE_MAX_WEIGHT_DELTA = 0.20
ENSEMBLE_RECENT_FRACTION = 0.35
ENSEMBLE_SHRINKAGE_KAPPA = 8.0
MODEL_PROVIDER_MAP = {
    "ecmwf_ifs025_official": "ECMWF",
    "gfs_0p25_official": "GFS",
    "icon_global_official": "ICON",
    "icon_global_open_meteo": "ICON",
    "gem_global_open_meteo": "GEM",
    "cma_grapes_global_open_meteo": "CMA",
}
REQUIRED_PROMOTION_BLOCKERS = (
    "truth_source_not_canonical_ingress",
    "published_receive_timestamps_missing",
    "available_model_set_below_two_models",
    "oos_promotion_gate_not_evaluated",
)


def load_historical_rebuild_model_rows(
    evidence: Mapping[str, Any],
    *,
    as_of_utc: str,
    target_timezone: str,
) -> list[dict[str, Any]]:
    """Normalize the same complete closed-day rows used by settlement history."""

    return load_historical_model_rows(
        evidence,
        as_of_utc=as_of_utc,
        station_timezone_map={
            str(row.get("station_id") or "").strip().upper(): target_timezone
            for row in evidence.get("model_rows", evidence.get("model_points", []))
            if isinstance(row, Mapping)
        },
    )


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("calibration_evidence_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("calibration_evidence_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _settlement_day_boundary_utc(
    event_date: str,
    *,
    target_timezone: str,
    boundary: str,
) -> str:
    """Return the registry-local settlement start/end as an exact UTC instant."""

    window = event_day_window(
        event_date,
        as_of_utc="9999-12-30T00:00:00Z",
        target_timezone=target_timezone,
    )
    key = {
        "start": "target_day_start_utc",
        "end": "target_day_end_utc",
    }.get(boundary)
    if key is None:
        raise ContractError("calibration_settlement_boundary_invalid")
    return str(window[key])


def _paired_available_at_utc(
    *,
    event_date: str,
    target_timezone: str,
    truth: Mapping[str, Any],
    model: Mapping[str, Any],
) -> str:
    """Compute when a residual first became knowable without future truth.

    A residual does not exist at model initialization.  It becomes available
    only after the registry-local settlement day has closed and every supplied
    truth/model receipt or canonical commit is visible.
    """

    candidates = [
        _settlement_day_boundary_utc(
            event_date,
            target_timezone=target_timezone,
            boundary="end",
        )
    ]
    for row, fields in (
        (truth, ("received_at_utc", "canonical_committed_at_utc")),
        (model, ("published_at_utc", "received_at_utc")),
    ):
        for field in fields:
            value = str(row.get(field) or "").strip()
            if value:
                candidates.append(_utc_text(_utc(value)))
    return max(candidates, key=_utc)


def _model_id(provider_id: str) -> str:
    try:
        return model_id_for_provider(str(provider_id).strip().lower())
    except (KeyError, ContractError) as exc:
        raise ContractError("calibration_evidence_model_provider_invalid") from exc


def _truth_source_lane(value: Any, *, canonical_input: bool) -> str:
    """Normalize truth source without inventing AMSC/AWC provenance.

    A missing lane is acceptable only for an explicitly non-canonical
    cross-check candidate, where it remains visibly quarantined.  Canonical
    ingress must carry its AMSC/AWC lane; silently defaulting to AMSC would
    create false lineage and could hide a source-routing defect.
    """

    lane = str(value or "").strip().upper()
    if lane in {"AMSC", "AWC"}:
        return lane
    if canonical_input:
        raise ContractError("canonical_truth_source_lane_missing")
    return "UNSPECIFIED_CROSSCHECK_ONLY"


def _model_row_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("provider_id") or "").strip().lower(),
        str(row.get("station_id") or "").strip().upper(),
        str(row.get("event_date") or row.get("target_local_date") or "").strip(),
        str(row.get("model_run_initialization_utc") or row.get("run_initialization_utc") or "").strip().replace("+00:00", "Z"),
        str(row.get("valid_time_utc") or "").strip().replace("+00:00", "Z"),
    )


def _merge_model_rows(
    native_rows: Iterable[Mapping[str, Any]],
    historical_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge model sources once by the exact station/day/run/valid key."""

    by_key: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for source_rows in (historical_rows, native_rows):
        for raw in source_rows:
            if not isinstance(raw, Mapping):
                raise ContractError("calibration_model_row_invalid")
            row = dict(raw)
            key = _model_row_key(row)
            if not all(key):
                raise ContractError("calibration_model_row_identity_incomplete")
            existing = by_key.get(key)
            if existing is not None:
                existing_mode = str(existing.get("capture_mode") or "").strip().lower()
                current_mode = str(row.get("capture_mode") or "").strip().lower()
                if existing_mode != current_mode and {existing_mode, current_mode} <= {"", "historical_rebuild", "prospective"}:
                    if current_mode == "prospective":
                        by_key[key] = row
                    continue
                comparison_fields = (
                    "temperature_2m_c",
                    "raw_message_sha256",
                    "receipt_sha256",
                    "published_at_utc",
                    "received_at_utc",
                )
                if any(str(existing.get(field) or "") != str(row.get(field) or "") for field in comparison_fields):
                    raise ContractError("calibration_model_row_exact_key_conflict")
                continue
            by_key[key] = row
    return [by_key[key] for key in sorted(by_key)]


def _multi_model_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep station-day generations containing at least two model roles.

    Model availability is explicit and may vary by date.  The function never
    requires all five roles, and an aggregator remains labelled as such.
    """

    by_station_day_run: dict[tuple[str, str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for raw in rows:
        row = dict(raw)
        key = (
            str(row.get("station_id") or "").strip(),
            str(row.get("event_date") or "").strip(),
            str(row.get("model_run_initialization_utc") or row.get("run_initialization_utc") or "").strip(),
        )
        provider = str(row.get("provider_id") or "").strip().lower()
        if not all(key) or not provider:
            continue
        by_station_day_run[key][provider].append(row)
    selected: list[dict[str, Any]] = []
    station_day_candidates: dict[tuple[str, str], list[tuple[str, dict[str, list[dict[str, Any]]]]]] = defaultdict(list)
    for (station, event_date, run_init), provider_rows in by_station_day_run.items():
        if len({provider_role(provider) for provider in provider_rows}) >= (
            MIN_FORMAL_ENSEMBLE_MODELS
        ):
            station_day_candidates[(station, event_date)].append((run_init, provider_rows))
    for key, candidates in sorted(station_day_candidates.items()):
        def score(item: tuple[str, dict[str, list[dict[str, Any]]]]) -> tuple[int, int, int, str]:
            run_init, provider_rows = item
            native_count = sum(
                1
                for provider in provider_rows
                if provider_source_class(provider) != "aggregator_republisher"
            )
            return (native_count, len(provider_rows), len(run_init), run_init)

        run_init, provider_rows = max(candidates, key=score)
        del run_init
        selected_by_role: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        for provider, provider_rows_for_model in provider_rows.items():
            role = provider_role(provider)
            previous = selected_by_role.get(role)
            if previous is None or provider == "icon_global_official":
                selected_by_role[role] = (provider, provider_rows_for_model)
        for _provider, provider_rows_for_model in selected_by_role.values():
            selected.extend(provider_rows_for_model)
    return sorted(
        selected,
        key=lambda row: (
            str(row.get("station_id") or ""),
            str(row.get("event_date") or ""),
            str(row.get("provider_id") or ""),
            str(row.get("model_run_initialization_utc") or row.get("run_initialization_utc") or ""),
            str(row.get("valid_time_utc") or ""),
        ),
    )


def _artifact_mapping(artifact: ModelCalibrationArtifact) -> dict[str, Any]:
    return {
        "calibration_artifact_id": artifact.calibration_artifact_id,
        "station_id": artifact.station_id,
        "model_id": artifact.model_id,
        "season_bin": artifact.season_bin,
        "lead_bucket": artifact.lead_bucket,
        "run_cycle": artifact.run_cycle,
        "settlement_definition": artifact.settlement_definition,
        "trained_through_utc": artifact.trained_through_utc,
        "sample_n": artifact.sample_n,
        "effective_sample_n": artifact.effective_sample_n,
        "bias_c": artifact.bias_c,
        "residual_scale_c": artifact.residual_scale_c,
        "tail_parameters": dict(artifact.tail_parameters),
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "content_sha256": artifact.content_sha256,
        "promoted": artifact.promoted,
    }


def _ensemble_mapping(artifact: ModelEnsembleArtifact) -> dict[str, Any]:
    return {
        "ensemble_artifact_id": artifact.ensemble_artifact_id,
        "station_id": artifact.station_id,
        "season_bin": artifact.season_bin,
        "lead_bucket": artifact.lead_bucket,
        "run_cycle": artifact.run_cycle,
        "settlement_definition": artifact.settlement_definition,
        "candidate_models": list(artifact.candidate_models),
        "supported_masks": {key: dict(value) for key, value in artifact.supported_masks.items()},
        "residual_covariance": {key: dict(value) for key, value in artifact.residual_covariance.items()},
        "correlation_penalty": artifact.correlation_penalty,
        "selection_evidence_sha256": artifact.selection_evidence_sha256,
        "previous_ensemble_artifact_sha256": artifact.previous_ensemble_artifact_sha256,
        "weight_method": artifact.weight_method,
        "effective_sample_n": artifact.effective_sample_n,
        "max_weight_delta": artifact.max_weight_delta,
        "train_start_date": artifact.train_start_date,
        "train_end_date": artifact.train_end_date,
        "recent_start_date": artifact.recent_start_date,
        "recent_end_date": artifact.recent_end_date,
        "holdout_start_date": artifact.holdout_start_date,
        "holdout_end_date": artifact.holdout_end_date,
        "oos_metrics": dict(artifact.oos_metrics),
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "content_sha256": artifact.content_sha256,
        "promoted": artifact.promoted,
    }


def _selection_mapping(artifact: ModelEnsembleSelectionEvidence) -> dict[str, Any]:
    return {
        "selection_evidence_id": artifact.selection_evidence_id,
        "station_id": artifact.station_id,
        "season_bin": artifact.season_bin,
        "lead_bucket": artifact.lead_bucket,
        "run_cycle": artifact.run_cycle,
        "settlement_definition": artifact.settlement_definition,
        "method_version": artifact.method_version,
        "train_start_date": artifact.train_start_date,
        "train_end_date": artifact.train_end_date,
        "recent_start_date": artifact.recent_start_date,
        "recent_end_date": artifact.recent_end_date,
        "holdout_start_date": artifact.holdout_start_date,
        "holdout_end_date": artifact.holdout_end_date,
        "candidate_scores": [dict(item) for item in artifact.candidate_scores],
        "selected_models": list(artifact.selected_models),
        "selected_weights": dict(artifact.selected_weights),
        "oos_metrics": dict(artifact.oos_metrics),
        "residual_covariance": {
            key: dict(value) for key, value in artifact.residual_covariance.items()
        },
        "residual_correlation": {
            key: dict(value) for key, value in artifact.residual_correlation.items()
        },
        "effective_sample_n": artifact.effective_sample_n,
        "previous_ensemble_artifact_sha256": artifact.previous_ensemble_artifact_sha256,
        "max_weight_delta": artifact.max_weight_delta,
        "actual_max_weight_delta": artifact.actual_max_weight_delta,
        "content_sha256": artifact.content_sha256,
    }


def _metrics(residuals: Iterable[float], bias_c: float = 0.0) -> dict[str, float | int]:
    values = [float(value) for value in residuals]
    if not values:
        return {"n": 0, "mae_c": None, "rmse_c": None}
    corrected = [value - bias_c for value in values]
    return {
        "n": len(values),
        "mae_c": sum(abs(value) for value in corrected) / len(corrected),
        "rmse_c": math.sqrt(sum(value * value for value in corrected) / len(corrected)),
    }


def _simplex_grid(
    models: tuple[str, ...],
    *,
    denominator: int = 20,
) -> Iterable[dict[str, float]]:
    """Yield a deterministic, bounded non-negative simplex grid."""

    def compositions(
        total: int,
        parts: int,
        prefix: tuple[int, ...] = (),
    ) -> Iterable[tuple[int, ...]]:
        if parts == 1:
            yield (*prefix, total)
            return
        for value in range(total + 1):
            yield from compositions(total - value, parts - 1, (*prefix, value))

    minimum_units = 0 if len(models) == 1 else 1
    remaining = denominator - minimum_units * len(models)
    if remaining < 0:
        raise ContractError("ensemble_weight_grid_invalid")
    for values in compositions(remaining, len(models)):
        yield {
            model: (value + minimum_units) / denominator
            for model, value in zip(models, values)
        }


def _normal_cdf(value: float, *, mean: float, scale: float) -> float:
    return 0.5 * (1.0 + math.erf((value - mean) / (scale * math.sqrt(2.0))))


def _bucket_distribution(*, mean: float, scale: float, buckets: tuple[int, ...]) -> dict[int, float]:
    probabilities = {
        bucket: max(
            0.0,
            _normal_cdf(bucket + 0.5, mean=mean, scale=scale)
            - _normal_cdf(bucket - 0.5, mean=mean, scale=scale),
        )
        for bucket in buckets
    }
    total = sum(probabilities.values())
    if total <= 0.0:
        raise ContractError("ensemble_bucket_probability_zero_mass")
    return {bucket: value / total for bucket, value in probabilities.items()}


def _calibration_parameters(
    *,
    model: str,
    dates: list[str],
    residuals_by_model_date: Mapping[tuple[str, str], float],
) -> tuple[float, float]:
    values = [float(residuals_by_model_date[(model, target)]) for target in dates]
    if not values:
        raise ContractError("ensemble_calibration_training_rows_missing")
    mean = sum(values) / len(values)
    rho = len(values) / (len(values) + ENSEMBLE_SHRINKAGE_KAPPA)
    bias = rho * mean if len(values) >= MIN_BIAS_CORRECTION_SAMPLE_N else 0.0
    variance = sum((value - mean) ** 2 for value in values) / max(1, len(values) - 1)
    shrunk_variance = rho * variance + (1.0 - rho) * 1.5**2
    return bias, max(0.001, math.sqrt(shrunk_variance))


def _probability_metrics(
    rows: list[tuple[str, Mapping[str, Mapping[int, float]], int]],
    weights: Mapping[str, float],
    *,
    recent_dates: set[str],
) -> dict[str, float | int]:
    if not rows:
        raise ContractError("ensemble_probability_score_rows_missing")
    totals = {
        "brier_score": 0.0, "nll": 0.0, "crps": 0.0,
        "recent_brier_score": 0.0, "recent_nll": 0.0,
        "recent_crps": 0.0,
    }
    recent_n = 0
    for target, model_probabilities, truth_bucket in rows:
        buckets = tuple(sorted(next(iter(model_probabilities.values()))))
        combined = {
            bucket: sum(
                float(weights[model]) * float(model_probabilities[model][bucket])
                for model in weights
            )
            for bucket in buckets
        }
        brier = sum(
            (combined[bucket] - (1.0 if bucket == truth_bucket else 0.0)) ** 2
            for bucket in buckets
        )
        nll = -math.log(max(combined.get(truth_bucket, 0.0), 1e-12))
        cumulative = 0.0
        crps = 0.0
        for bucket in buckets:
            cumulative += combined[bucket]
            crps += (cumulative - (1.0 if truth_bucket <= bucket else 0.0)) ** 2
        totals["brier_score"] += brier
        totals["nll"] += nll
        totals["crps"] += crps
        if target in recent_dates:
            recent_n += 1
            totals["recent_brier_score"] += brier
            totals["recent_nll"] += nll
            totals["recent_crps"] += crps
    n = len(rows)
    for key in ("brier_score", "nll", "crps"):
        totals[key] /= n
    for key in ("recent_brier_score", "recent_nll", "recent_crps"):
        totals[key] = totals[key] / recent_n if recent_n else totals[key.replace("recent_", "")]
    totals["n"] = n
    totals["recent_n"] = recent_n
    return totals


def _residual_matrices(
    *,
    models: tuple[str, ...],
    dates: list[str],
    residuals_by_model_date: Mapping[tuple[str, str], float],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    centered: dict[str, list[float]] = {}
    for model in models:
        values = [float(residuals_by_model_date[(model, target)]) for target in dates]
        mean = sum(values) / len(values)
        centered[model] = [value - mean for value in values]
    covariance: dict[str, dict[str, float]] = {}
    correlation: dict[str, dict[str, float]] = {}
    denominator = max(1, len(dates) - 1)
    for left in models:
        covariance[left] = {}
        correlation[left] = {}
        for right in models:
            cov = sum(
                a * b for a, b in zip(centered[left], centered[right])
            ) / denominator
            var_left = sum(value * value for value in centered[left]) / denominator
            var_right = sum(value * value for value in centered[right]) / denominator
            corr = cov / math.sqrt(var_left * var_right) if var_left > 0 and var_right > 0 else (
                1.0 if left == right else 0.0
            )
            covariance[left][right] = cov
            correlation[left][right] = max(-1.0, min(1.0, corr))
    return covariance, correlation


def _correlation_concentration(
    weights: Mapping[str, float],
    correlation: Mapping[str, Mapping[str, float]],
) -> float:
    models = tuple(sorted(weights))
    return sum(
        float(weights[left]) * float(weights[right])
        * max(0.0, float(correlation[left][right]))
        for index, left in enumerate(models)
        for right in models[index + 1:]
    )


def _effective_sample_n(dates: list[str], recent_dates: set[str]) -> float:
    sample_weights = [2.0 if target in recent_dates else 1.0 for target in dates]
    return sum(sample_weights) ** 2 / sum(value * value for value in sample_weights)


def _actual_weight_delta(
    weights: Mapping[str, float],
    previous_weights: Mapping[str, float] | None,
) -> float:
    if previous_weights is None:
        return 0.0
    return max(
        abs(float(weights.get(model, 0.0)) - float(previous_weights.get(model, 0.0)))
        for model in MODEL_IDS
    )


def _fit_probability_weights(
    *,
    models: tuple[str, ...],
    rows: list[tuple[str, Mapping[str, Mapping[int, float]], int]],
    recent_dates: set[str],
    residual_correlation: Mapping[str, Mapping[str, float]],
    effective_sample_n: float,
    previous_weights: Mapping[str, float] | None,
    max_weight_delta: float,
) -> tuple[dict[str, float], dict[str, float | int]]:
    best_weights: dict[str, float] | None = None
    best_metrics: dict[str, float | int] | None = None
    best_objective = math.inf
    uniform = 1.0 / len(models)
    shrinkage_strength = ENSEMBLE_SHRINKAGE_KAPPA / (
        effective_sample_n + ENSEMBLE_SHRINKAGE_KAPPA
    )
    for weights in _simplex_grid(models):
        if _actual_weight_delta(weights, previous_weights) > max_weight_delta + 1e-12:
            continue
        metrics = _probability_metrics(rows, weights, recent_dates=recent_dates)
        correlation_cost = _correlation_concentration(weights, residual_correlation)
        shrinkage_cost = sum((weights[model] - uniform) ** 2 for model in models)
        objective = (
            0.35 * float(metrics["brier_score"])
            + 0.20 * float(metrics["nll"])
            + 0.15 * float(metrics["crps"])
            + 0.15 * float(metrics["recent_brier_score"])
            + 0.075 * float(metrics["recent_nll"])
            + 0.075 * float(metrics["recent_crps"])
            + 0.05 * correlation_cost
            + 0.10 * shrinkage_strength * shrinkage_cost
        )
        tie_key = tuple(-weights[model] for model in models)
        best_tie_key = tuple(-best_weights[model] for model in models) if best_weights else ()
        if objective < best_objective - 1e-12 or (
            abs(objective - best_objective) <= 1e-12 and tie_key < best_tie_key
        ):
            best_weights = weights
            best_metrics = {
                **metrics,
                "correlation_concentration": correlation_cost,
                "shrinkage_strength": shrinkage_strength,
                "objective": objective,
            }
            best_objective = objective
    if best_weights is None or best_metrics is None:
        raise ContractError("ensemble_weight_delta_constraint_no_feasible_weights")
    return best_weights, best_metrics


def _rolling_probability_rows(
    *,
    models: tuple[str, ...],
    dates: list[str],
    forecasts_by_model_date: Mapping[tuple[str, str], float],
    residuals_by_model_date: Mapping[tuple[str, str], float],
    truths_by_date: Mapping[str, float],
    minimum_train_n: int = 2,
) -> list[tuple[str, Mapping[str, Mapping[int, float]], int]]:
    rows: list[tuple[str, Mapping[str, Mapping[int, float]], int]] = []
    for cutoff in range(minimum_train_n, len(dates)):
        training_dates = dates[:cutoff]
        target = dates[cutoff]
        parameters = {
            model: _calibration_parameters(
                model=model,
                dates=training_dates,
                residuals_by_model_date=residuals_by_model_date,
            )
            for model in models
        }
        truth_bucket = int(math.floor(float(truths_by_date[target]) + 0.5))
        lower = min(
            truth_bucket,
            *(math.floor(forecasts_by_model_date[(model, target)] + bias - 6.0 * scale)
              for model, (bias, scale) in parameters.items()),
        ) - 1
        upper = max(
            truth_bucket,
            *(math.ceil(forecasts_by_model_date[(model, target)] + bias + 6.0 * scale)
              for model, (bias, scale) in parameters.items()),
        ) + 1
        buckets = tuple(range(int(lower), int(upper) + 1))
        probabilities = {
            model: _bucket_distribution(
                mean=forecasts_by_model_date[(model, target)] + parameters[model][0],
                scale=parameters[model][1],
                buckets=buckets,
            )
            for model in models
        }
        rows.append((target, probabilities, truth_bucket))
    return rows


def _fit_rolling_probability_ensemble(
    *,
    models: tuple[str, ...],
    history_dates: list[str],
    forecasts_by_model_date: Mapping[tuple[str, str], float],
    residuals_by_model_date: Mapping[tuple[str, str], float],
    truths_by_date: Mapping[str, float],
    previous_weights: Mapping[str, float] | None,
    max_weight_delta: float = ENSEMBLE_MAX_WEIGHT_DELTA,
) -> tuple[
    dict[str, float],
    dict[str, tuple[float, float]],
    dict[str, float | int],
]:
    """Fit the canonical rolling-origin probability ensemble for one fold.

    This is the single training implementation shared by formal P0 evidence,
    TAF crossfit, and TAF transport validation.  Callers provide only closed
    history dates; the target being predicted must not be present.
    """

    if len(models) < MIN_FORMAL_ENSEMBLE_MODELS:
        raise ContractError("ensemble_minimum_model_count_not_met")
    if len(history_dates) < 3:
        raise ContractError("ensemble_rolling_origin_history_insufficient")
    if len(set(history_dates)) != len(history_dates):
        raise ContractError("ensemble_rolling_origin_history_duplicate")
    if history_dates != sorted(history_dates):
        raise ContractError("ensemble_rolling_origin_history_not_sorted")
    rolling_rows = _rolling_probability_rows(
        models=models,
        dates=history_dates,
        forecasts_by_model_date=forecasts_by_model_date,
        residuals_by_model_date=residuals_by_model_date,
        truths_by_date=truths_by_date,
    )
    if not rolling_rows:
        raise ContractError("ensemble_rolling_origin_folds_missing")
    recent_n = max(
        1,
        int(math.ceil(len(history_dates) * ENSEMBLE_RECENT_FRACTION)),
    )
    recent_dates = set(history_dates[-recent_n:])
    _covariance, correlation = _residual_matrices(
        models=models,
        dates=history_dates,
        residuals_by_model_date=residuals_by_model_date,
    )
    weights, metrics = _fit_probability_weights(
        models=models,
        rows=rolling_rows,
        recent_dates=recent_dates,
        residual_correlation=correlation,
        effective_sample_n=_effective_sample_n(
            [row[0] for row in rolling_rows],
            recent_dates,
        ),
        previous_weights=previous_weights,
        max_weight_delta=max_weight_delta,
    )
    parameters = {
        model: _calibration_parameters(
            model=model,
            dates=history_dates,
            residuals_by_model_date=residuals_by_model_date,
        )
        for model in models
    }
    return weights, parameters, metrics


def _fixed_probability_rows(
    *,
    models: tuple[str, ...],
    train_dates: list[str],
    evaluation_dates: list[str],
    forecasts_by_model_date: Mapping[tuple[str, str], float],
    residuals_by_model_date: Mapping[tuple[str, str], float],
    truths_by_date: Mapping[str, float],
) -> list[tuple[str, Mapping[str, Mapping[int, float]], int]]:
    parameters = {
        model: _calibration_parameters(
            model=model, dates=train_dates,
            residuals_by_model_date=residuals_by_model_date,
        )
        for model in models
    }
    rows = []
    for target in evaluation_dates:
        truth_bucket = int(math.floor(float(truths_by_date[target]) + 0.5))
        lower = min(
            truth_bucket,
            *(math.floor(forecasts_by_model_date[(model, target)] + bias - 6.0 * scale)
              for model, (bias, scale) in parameters.items()),
        ) - 1
        upper = max(
            truth_bucket,
            *(math.ceil(forecasts_by_model_date[(model, target)] + bias + 6.0 * scale)
              for model, (bias, scale) in parameters.items()),
        ) + 1
        buckets = tuple(range(int(lower), int(upper) + 1))
        rows.append((
            target,
            {
                model: _bucket_distribution(
                    mean=forecasts_by_model_date[(model, target)] + parameters[model][0],
                    scale=parameters[model][1],
                    buckets=buckets,
                )
                for model in models
            },
            truth_bucket,
        ))
    return rows


def _promotion_decision(*, artifact_kind: str, artifact_id: str, evidence_sha256: str, decided_at_utc: str) -> dict[str, Any]:
    identity = {
        "schema": "bayes_basket_calibration_promotion_decision_v1",
        "artifact_kind": artifact_kind,
        "artifact_id": artifact_id,
        "decision": "REJECTED",
        "decision_seq": 1,
        "gate_version": CALIBRATION_GATE_VERSION,
        "reason_codes": list(REQUIRED_PROMOTION_BLOCKERS),
        "evidence_sha256": evidence_sha256,
        "production_authority": False,
        "live_order_authorized": False,
    }
    receipt_sha = content_sha256(identity)
    return {
        **identity,
        "decided_at_utc": decided_at_utc,
        "receipt_sha256": receipt_sha,
        "content_sha256": content_sha256({**identity, "receipt_sha256": receipt_sha}),
    }


def build_calibration_evidence(
    model_rows: Iterable[Mapping[str, Any]],
    observation_rows: Iterable[Mapping[str, Any]],
    *,
    as_of_utc: str,
    target_timezone: str,
    input_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    canonical_observation_evidence: Mapping[str, Any] | None = None,
    model_arrival_evidence: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    historical_model_rows: Iterable[Mapping[str, Any]] | None = None,
    previous_ensemble_artifacts: Iterable[Mapping[str, Any]] | None = None,
    station_lane_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create deterministic calibration candidates and a fail-closed gate.

    ``model_rows`` and ``observation_rows`` are the frozen source files used
    by the strict-as-of replay.  The function intentionally does not infer
    missing publication/receipt times; that omission is part of the evidence
    and keeps every candidate out of production activation.
    """

    as_of = _utc(as_of_utc)
    models = _merge_model_rows(
        native_rows=model_rows,
        historical_rows=list(historical_model_rows or []),
    )
    if not str(target_timezone or "").strip():
        raise ContractError("calibration_settlement_timezone_missing")
    for row in models:
        if str(row.get("target_timezone") or "").strip() != target_timezone:
            raise ContractError("calibration_model_settlement_timezone_mismatch")
        for field in ("published_at_utc", "received_at_utc"):
            value = str(row.get(field) or "").strip()
            if value and _utc(value) > as_of:
                raise ContractError("calibration_model_available_after_asof")
    models = select_preferred_model_rows(models)
    arrival_audit: dict[str, Any] = {
        "schema": INDEX_SCHEMA,
        "decision_at_utc": _utc_text(as_of),
        "accepted_count": 0,
        "blocked_count": 0,
        "blocked": [],
        "all_prospective": False,
        "promotion_allowed": False,
        "production_authority": False,
    }
    arrival_rows: list[dict[str, Any]] = []
    if model_arrival_evidence is not None:
        if isinstance(model_arrival_evidence, Mapping):
            raw_arrivals = model_arrival_evidence.get("arrivals")
            if raw_arrivals is None:
                raw_arrivals = model_arrival_evidence.get("model_arrivals")
            if raw_arrivals is None:
                raw_arrivals = model_arrival_evidence.get("arrival")
        else:
            raw_arrivals = model_arrival_evidence
        if isinstance(raw_arrivals, Mapping):
            raw_arrivals = [raw_arrivals]
        if not isinstance(raw_arrivals, Iterable) or isinstance(raw_arrivals, (str, bytes, bytearray)):
            raise ContractError("model_arrival_evidence_rows_invalid")
        arrival_rows, arrival_audit = load_model_arrival_receipts(
            raw_arrivals,
            decision_at_utc=_utc_text(as_of),
        )
    arrival_index = {
        model_arrival_key(row): row
        for row in arrival_rows
    }
    if arrival_index:
        enriched_models: list[dict[str, Any]] = []
        for raw in models:
            candidate = dict(raw)
            key = (
                str(candidate.get("provider_id") or "").strip().lower(),
                str(candidate.get("station_id") or "").strip(),
                str(candidate.get("event_date") or "").strip(),
                str(candidate.get("model_run_initialization_utc") or "").strip().replace("+00:00", "Z"),
                str(candidate.get("valid_time_utc") or "").strip().replace("+00:00", "Z"),
            )
            arrival = arrival_index.get(key)
            if arrival is not None:
                raw_sha = str(candidate.get("raw_message_sha256") or "").strip().lower()
                if raw_sha and raw_sha not in set(arrival["raw_payload_sha256s"]):
                    raise ContractError("model_arrival_raw_payload_lineage_mismatch")
                receipt_sha = str(candidate.get("receipt_sha256") or "").strip().lower()
                if receipt_sha and receipt_sha != arrival["receipt_sha256"]:
                    raise ContractError("model_arrival_receipt_lineage_mismatch")
                candidate["published_at_utc"] = arrival["published_at_utc"]
                candidate["received_at_utc"] = arrival["received_at_utc"]
            enriched_models.append(candidate)
        models = enriched_models
    cross_check_observations = list(observation_rows)
    canonical_input = canonical_observation_evidence
    canonical_observation_rows: list[dict[str, Any]] = []
    if canonical_input is not None:
        from bayes_basket_target.source_policy import normalize_station_lane_map

        lane_map = normalize_station_lane_map(station_lane_map)
        if not lane_map:
            raise ContractError("canonical_observation_station_lane_map_required")
        if canonical_input.get("schema") != "bayes_basket_target_canonical_observation_evidence_v1":
            raise ContractError("canonical_observation_evidence_schema_invalid")
        if canonical_input.get("state") != "EXPORTED":
            raise ContractError("canonical_observation_evidence_blocked")
        checks = canonical_input.get("checks")
        if not isinstance(checks, Mapping) or checks.get("strict_timestamp_ordering") is not True:
            raise ContractError("canonical_observation_evidence_timestamp_check_failed")
        for collection_name in ("observations", "truth_rows"):
            for item in canonical_input.get(collection_name) or []:
                if not isinstance(item, Mapping):
                    raise ContractError(f"canonical_observation_{collection_name}_row_invalid")
                station_id = str(item.get("station_id") or "").strip().upper()
                source_lane = str(item.get("source_lane") or "").strip().lower()
                if station_id not in lane_map:
                    raise ContractError("canonical_observation_station_lane_missing")
                if source_lane != lane_map[station_id]:
                    raise ContractError("canonical_observation_source_lane_mismatch")
        canonical_truth_ids = {
            (
                str(item.get("source_lane") or "").upper(),
                str(item.get("station_id") or ""),
                str(item.get("event_date") or ""),
            ): str(item.get("truth_id") or "")
            for item in canonical_input.get("truth_rows") or []
            if isinstance(item, Mapping)
        }
        for item in canonical_input.get("observations") or []:
            if not isinstance(item, Mapping) or item.get("qc_status") != "ACCEPTED":
                continue
            event_date = str(item.get("event_date_local") or "").strip()
            if not event_date:
                raise ContractError("canonical_observation_event_date_missing")
            truth_id = canonical_truth_ids.get(
                (str(item.get("source_lane") or "").upper(), str(item.get("station_id") or ""), event_date),
                "",
            )
            if not truth_id:
                raise ContractError("canonical_observation_truth_id_missing")
            first_seen_at = str(item.get("first_seen_at_utc") or "").strip()
            committed_at = str(item.get("canonical_committed_at_utc") or "").strip()
            if not first_seen_at or not committed_at:
                raise ContractError(
                    "canonical_observation_availability_timestamp_missing"
                )
            if _utc(first_seen_at) > as_of or _utc(committed_at) > as_of:
                raise ContractError("canonical_observation_available_after_asof")
            raw_sha = str(item.get("raw_sha256") or "").strip().lower()
            canonical_observation_rows.append({
                "station_id": item.get("station_id"),
                "event_date": event_date,
                "valid_time_utc": item.get("valid_at_utc"),
                "temperature_c": item.get("reported_temperature_c"),
                "raw_sha256": raw_sha,
                "source_raw_sha256": raw_sha,
                "source_lane": item.get("source_lane"),
                "source_role": "CANONICAL_AMSC_AWC_INGRESS",
                "observation_id": item.get("observation_id"),
                "truth_id": truth_id,
                "received_at_utc": item.get("first_seen_at_utc"),
                "canonical_committed_at_utc": item.get("canonical_committed_at_utc"),
            })
        if not canonical_observation_rows:
            raise ContractError("canonical_observation_evidence_no_accepted_rows")
    observations = canonical_observation_rows or cross_check_observations
    # Model-local calibration may use one provider's complete historical
    # rows, while the ensemble gate still requires one exact three-model run.
    # Keep the strict replay useful in the partial-history case instead of
    # discarding all available residuals merely because ICON is missing.
    replay_models = _multi_model_rows(models)
    replay_input_rows = replay_models or models
    replay = evaluate_strict_asof_replay(
        replay_input_rows,
        observations,
        as_of_utc=_utc_text(as_of),
        target_timezone=target_timezone,
        fail_on_incomplete_model_set=False,
    )
    run_cycle = str(replay.get("run_cycle") or "").strip()
    if not run_cycle:
        raise ContractError("strict_asof_run_cycle_missing")
    replay_lead_bucket = lead_bucket_for_maturity(str(replay.get("maturity_key") or ""))
    model_days, model_quality = materialize_model_station_days(models, target_timezone=target_timezone)
    truth_days, truth_quality = materialize_observation_station_days(observations)
    cross_check_days, cross_check_quality = materialize_observation_station_days(cross_check_observations)
    # The formal date boundary is the registry settlement timezone, not the
    # UTC calendar date or the operational/reporting ET boundary.
    anchor_local_date = as_of.astimezone(ZoneInfo(target_timezone)).date()

    grouped_models: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    quarantined_model_days: list[dict[str, Any]] = []
    for row in model_days:
        event_date = str(row.get("event_date") or "").strip()
        window = event_day_window(
            event_date,
            as_of_utc=_utc_text(as_of),
            target_timezone=target_timezone,
        )
        if window["state"] == "closed":
            grouped_models[(row["station_id"], event_date)].append(row)
        else:
            quarantined_model_days.append({
                "station_id": str(row.get("station_id") or ""),
                "event_date": event_date,
                "state": window["state"],
                "reason": "non_closed_settlement_day_excluded_from_calibration",
            })

    joined: list[dict[str, Any]] = []
    multi_model_joined: list[dict[str, Any]] = []
    model_set_blockers: list[dict[str, Any]] = []
    for key, rows in sorted(grouped_models.items()):
        truth = truth_days.get(key)
        if truth is None:
            continue
        providers = {row["provider_id"] for row in rows}
        provider_roles_present = {provider_role(provider) for provider in providers}
        multi_model_join = (
            len(provider_roles_present) >= MIN_FORMAL_ENSEMBLE_MODELS
        )
        if not multi_model_join:
            model_set_blockers.append({
                "station_id": key[0],
                "event_date": key[1],
                "available_providers": sorted(providers),
                "missing_providers": sorted(MODEL_ROLES - provider_roles_present),
                "missing_model_roles": sorted(MODEL_ROLES - provider_roles_present),
                "reason": "available_model_set_below_two_models",
            })
        selected_rows: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            role = provider_role(row["provider_id"])
            previous = selected_rows.get(role)
            if previous is None or row["provider_id"] == "icon_global_official":
                selected_rows[role] = row
        for row in sorted(selected_rows.values(), key=lambda item: item["provider_id"]):
            model_id = _model_id(row["provider_id"])
            joined_row = {
                "station_id": key[0],
                "event_date": key[1],
                "season_bin": truth["season"],
                "model_id": model_id,
                "raw_tmax_c": row["forecast_daily_max_c"],
                "official_tmax_c": truth["observed_daily_max_c"],
                "residual_c": truth["observed_daily_max_c"] - row["forecast_daily_max_c"],
                "run_init_utc": row["run_initialization_utc"],
                "maturity_key": row["maturity_key"],
                "published_at_utc": row.get("published_at_utc") or "",
                "received_at_utc": row.get("received_at_utc") or "",
                "provider_id": row["provider_id"],
                "provider_role": provider_role(row["provider_id"]),
                "source_class": (
                    (row.get("source_classes") or [""])[0]
                    or provider_source_class(row["provider_id"])
                ),
                "aggregator_or_republisher": bool(row.get("aggregator_or_republisher", False)),
                "open_meteo": bool(row.get("open_meteo", False)),
                "source_url": (row.get("source_urls") or [""])[0],
                "raw_object_ref": (row.get("raw_object_refs") or [""])[0],
                "model_lineage_sha256": row["model_run_lineage_sha256"],
                "truth_lineage_sha256": truth["sample_lineage_sha256"],
                "truth_id": truth.get("truth_id") or f"truth-{key[0]}-{key[1]}",
                "paired_asof_utc": _paired_available_at_utc(
                    event_date=key[1],
                    target_timezone=target_timezone,
                    truth=truth,
                    model=row,
                ),
                "capture_modes": list(row.get("capture_modes") or []),
                "historical_replay_eligible": bool(row.get("historical_replay_eligible", True)),
                "prospective_strict_asof_eligible": bool(row.get("prospective_strict_asof_eligible", False)),
            }
            if _utc(joined_row["paired_asof_utc"]) > as_of:
                raise ContractError("calibration_residual_available_after_asof")
            # Keep provider-local residuals for offline calibration.  A
            # station-day can feed formal selection once at least two model
            # roles are available; missing auxiliary roles do not erase it.
            joined.append(joined_row)
            if multi_model_join:
                multi_model_joined.append(joined_row)

    station_city: dict[str, str] = {}
    for row in models:
        station_city.setdefault(str(row.get("station_id") or ""), str(row.get("city") or ""))
    station_ids = sorted({row["station_id"] for row in joined})
    station_registry = []
    for station_id in station_ids:
        identity = {
            "station_id": station_id,
            "icao_code": station_id,
            "city_label": station_city.get(station_id, ""),
            "timezone_name": target_timezone,
            "source_lane": _truth_source_lane((truth_days.get((station_id, next(
                (item["event_date"] for item in joined if item["station_id"] == station_id), ""
            ))) or {}).get("source_lane"), canonical_input=canonical_input is not None),
            "settlement_definition": SETTLEMENT_DEFINITION,
            "registry_version": "bayes_basket_target_calibration_evidence_v1",
            "source_role": "CANONICAL_AMSC_AWC_INGRESS" if canonical_input is not None else "IEM_OFFLINE_MIRROR_CROSSCHECK_ONLY",
        }
        station_registry.append({**identity, "content_sha256": content_sha256(identity)})

    truth_evidence = []
    for key, truth in sorted(truth_days.items()):
        if date.fromisoformat(key[1]) >= anchor_local_date or key[0] not in station_ids:
            continue
        truth_id = truth.get("truth_id") or f"truth-{key[0]}-{key[1]}"
        identity = {
            "truth_id": truth_id,
            "station_id": key[0],
            "local_date": key[1],
            "target_timezone": target_timezone,
            "official_tmax_c": truth["observed_daily_max_c"],
            "source_lane": _truth_source_lane(
                truth.get("source_lane"), canonical_input=canonical_input is not None
            ),
            "source_role": str(truth.get("source_role") or (
                "CANONICAL_AMSC_AWC_INGRESS" if canonical_input is not None else "IEM_OFFLINE_MIRROR_CROSSCHECK_ONLY"
            )),
            "settlement_definition": SETTLEMENT_DEFINITION,
            "revision_no": 0,
            "published_at_utc": truth.get("published_at_utc"),
            "received_at_utc": truth.get("received_at_utc"),
            "canonical_committed_at_utc": truth.get("canonical_committed_at_utc"),
            "timestamps_complete": bool(truth.get("received_at_utc") and truth.get("canonical_committed_at_utc")),
            "qc_status": str(truth.get("qc_status") or ("ACCEPTED" if canonical_input is not None else "QUARANTINED")),
            "source_payload_sha256": truth.get("source_payload_sha256") or truth["sample_lineage_sha256"],
            "observation_ids": truth.get("observation_ids") or [],
        }
        truth_evidence.append({**identity, "content_sha256": content_sha256(identity)})

    calibration_model_rows = []
    residual_evidence = []
    residual_objects: list[ResidualObservation] = []
    for row in joined:
        maturity_key = str(row.get("maturity_key") or "").strip()
        lead_bucket = lead_bucket_for_maturity(maturity_key)
        model_run_id = f"run-{row['station_id']}-{row['event_date']}-{row['model_id']}-{run_cycle}"
        truth_id = row["truth_id"]
        model_identity = {
            "model_run_id": model_run_id,
            "model_id": row["model_id"],
            "station_id": row["station_id"],
            "target_local_date": row["event_date"],
            "target_timezone": target_timezone,
            "run_init_utc": row["run_init_utc"],
            "maturity_key": maturity_key,
            "published_at_utc": row.get("published_at_utc") or None,
            "received_at_utc": row.get("received_at_utc") or None,
            "timestamps_complete": bool(row.get("published_at_utc") and row.get("received_at_utc")),
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "raw_tmax_c": row["raw_tmax_c"],
            "settlement_definition": SETTLEMENT_DEFINITION,
            "provider_id": row.get("provider_id") or "",
            "provider_role": row.get("provider_role") or provider_role(row.get("provider_id")),
            "source_class": row.get("source_class") or "",
            "aggregator_or_republisher": bool(row.get("aggregator_or_republisher", False)),
            "open_meteo": bool(row.get("open_meteo", False)),
            "source_url": row.get("source_url") or "",
            "raw_object_ref": row.get("raw_object_ref") or "",
            "source_object_sha256": row["model_lineage_sha256"],
            "asof_status": (
                "PROSPECTIVE_STRICT_ASOF"
                if row.get("prospective_strict_asof_eligible")
                else "HISTORICAL_REPLAY_ONLY"
                if row.get("historical_replay_eligible")
                else "MATURITY_ONLY_NOT_ELIGIBLE"
            ),
            "capture_modes": list(row.get("capture_modes") or []),
            "historical_replay_eligible": bool(row.get("historical_replay_eligible", True)),
            "prospective_strict_asof_eligible": bool(row.get("prospective_strict_asof_eligible", False)),
        }
        calibration_model_rows.append({**model_identity, "content_sha256": content_sha256(model_identity)})
        residual_id = f"residual-{row['station_id']}-{row['event_date']}-{row['model_id']}-{run_cycle}"
        residual_identity = {
            "residual_id": residual_id,
            "truth_id": truth_id,
            "model_run_id": model_run_id,
            "station_id": row["station_id"],
            "model_id": row["model_id"],
            "season_bin": row["season_bin"],
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "maturity_key": maturity_key,
            "e_y_minus_f_raw_c": row["residual_c"],
            "paired_asof_utc": row["paired_asof_utc"],
            "qc_status": "ACCEPTED",
            "historical_replay_eligible": bool(row.get("historical_replay_eligible", True)),
            "prospective_strict_asof_eligible": bool(row.get("prospective_strict_asof_eligible", False)),
            "pair_sha256": content_sha256({"truth": row["truth_lineage_sha256"], "model": row["model_lineage_sha256"]}),
        }
        residual_evidence.append({**residual_identity, "content_sha256": content_sha256(residual_identity)})
        residual_objects.append(ResidualObservation.from_mapping({
            "residual_id": residual_id,
            "station_id": row["station_id"],
            "model_id": row["model_id"],
            "season_bin": row["season_bin"],
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "residual_c": row["residual_c"],
            "target_local_date": row["event_date"],
            "paired_asof_utc": row["paired_asof_utc"],
            "qc_status": "ACCEPTED",
        }))

    valid_from = _utc_text(as_of)
    valid_until = _utc_text(as_of + timedelta(days=30))
    artifacts: list[ModelCalibrationArtifact] = []
    oos_reports: list[dict[str, Any]] = []
    by_scope: dict[tuple[str, str, str], list[ResidualObservation]] = defaultdict(list)
    for item in residual_objects:
        by_scope[(item.station_id, item.model_id, item.season_bin)].append(item)
    scope_counts = {
        f"{station_id}|{model_id}|{season_bin}": len(scoped)
        for (station_id, model_id, season_bin), scoped in sorted(by_scope.items())
    }
    closed_event_dates = sorted({item.target_local_date for item in residual_objects})
    minimum_scope_sample_n = 4
    minimum_bias_correction_sample_n = MIN_BIAS_CORRECTION_SAMPLE_N
    insufficient_scope_keys = sorted(
        key for key, count in scope_counts.items() if count < minimum_scope_sample_n
    )
    eligible_scope_count = sum(
        1 for count in scope_counts.values() if count >= minimum_scope_sample_n
    )
    calibration_window = {
        "closed_settlement_event_day_count": len(closed_event_dates),
        "closed_settlement_event_dates": closed_event_dates,
        "joined_station_day_count": len({(item.station_id, item.target_local_date) for item in residual_objects}),
        "residual_count": len(residual_objects),
        "scope_count": len(scope_counts),
        "scope_sample_counts": scope_counts,
        "minimum_scope_sample_n": minimum_scope_sample_n,
        "minimum_bias_correction_sample_n": minimum_bias_correction_sample_n,
        "eligible_scope_count": eligible_scope_count,
        "bias_correction_eligible_scope_count": sum(
            1 for count in scope_counts.values() if count >= minimum_bias_correction_sample_n
        ),
        "insufficient_scope_count": len(insufficient_scope_keys),
        "insufficient_scope_keys": insufficient_scope_keys,
        "state": (
            "INSUFFICIENT_CLOSED_SETTLEMENT_HISTORY"
            if len(closed_event_dates) < minimum_scope_sample_n
            else "INSUFFICIENT_SCOPE_SAMPLES"
            if eligible_scope_count == 0
            else "PARTIAL_SCOPE_CANDIDATE_READY"
            if insufficient_scope_keys
            else "CANDIDATE_SAMPLE_READY"
        ),
        "promotion_allowed": False,
    }
    for scope, scoped in sorted(by_scope.items()):
        scoped = sorted(scoped, key=lambda item: item.target_local_date)
        if len(scoped) < 4:
            continue
        train_n = max(2, int(len(scoped) * 0.8))
        holdout = scoped[train_n:]
        if not holdout:
            continue
        holdout_start = holdout[0].target_local_date
        training_cutoff = _settlement_day_boundary_utc(
            holdout_start,
            target_timezone=target_timezone,
            boundary="start",
        )
        train = [
            item
            for item in scoped[:train_n]
            if timestamp_lte(item.paired_asof_utc, training_cutoff)
        ]
        if len(train) < 2:
            continue
        if max(item.target_local_date for item in train) >= min(
            item.target_local_date for item in holdout
        ):
            raise ContractError("calibration_train_holdout_date_overlap")
        trained_through = max(
            (item.paired_asof_utc for item in train),
            key=_utc,
        )
        artifact_id = "cal-candidate-" + "-".join(scope) + "-" + replay["replay_sha256"][:12]
        artifact = fit_model_calibration_artifact(
            train,
            artifact_id=artifact_id,
            trained_through_utc=trained_through,
            valid_from_utc=valid_from,
            valid_until_utc=valid_until,
            parent_scale_c=1.5,
            kappa=8.0,
            bias_enabled=len(train) >= minimum_bias_correction_sample_n,
            promoted=False,
        )
        artifacts.append(artifact)
        raw_metrics = _metrics((item.residual_c for item in holdout))
        calibrated_metrics = _metrics((item.residual_c for item in holdout), artifact.bias_c)
        oos_reports.append({
            "station_id": scope[0],
            "model_id": scope[1],
            "season_bin": scope[2],
            "train_n": len(train),
            "holdout_n": len(holdout),
            "trained_through_utc": trained_through,
            "training_cutoff_utc": training_cutoff,
            "train_end_date": train[-1].target_local_date,
            "holdout_start_date": holdout_start,
            "strict_asof_train_holdout_disjoint": True,
            "raw": raw_metrics,
            "calibrated": calibrated_metrics,
            "promotion_grade": False,
        })

    model_set_complete = (
        bool(multi_model_joined)
        and replay.get("state") == "EVALUATED"
    )
    if not model_set_complete:
        # Provider-local artifacts remain quarantined research candidates.
        # Only the ensemble list is blocked until at least one comparable
        # multi-model scope exists.
        ensemble_artifacts = []

    ensemble_artifacts: list[ModelEnsembleArtifact] = []
    ensemble_selection_evidence: list[ModelEnsembleSelectionEvidence] = []
    ensemble_selection_reports: list[dict[str, Any]] = []
    previous_by_scope: dict[tuple[str, str, str, str, str], ModelEnsembleArtifact] = {}
    for raw_previous in previous_ensemble_artifacts or []:
        previous = ModelEnsembleArtifact.from_mapping(raw_previous)
        validate_formal_ensemble_artifact(previous)
        previous_by_scope[(
            previous.station_id, previous.season_bin, previous.lead_bucket,
            previous.run_cycle, previous.settlement_definition,
        )] = previous
    by_ensemble_scope: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in artifacts:
        by_ensemble_scope[(item.station_id, item.season_bin)].add(item.model_id)
    for (station_id, season_bin), models_for_scope in sorted(by_ensemble_scope.items()):
        if (
            not model_set_complete
            or len(models_for_scope) < MIN_FORMAL_ENSEMBLE_MODELS
        ):
            continue
        members = tuple(sorted(models_for_scope))
        residuals_by_model_date: dict[tuple[str, str], float] = {}
        forecasts_by_model_date: dict[tuple[str, str], float] = {}
        truths_by_date: dict[str, float] = {}
        date_sets: list[set[str]] = []
        for model in members:
            scoped_rows = by_scope[(station_id, model, season_bin)]
            model_dates: set[str] = set()
            for row in scoped_rows:
                residuals_by_model_date[(model, row.target_local_date)] = float(row.residual_c)
                model_dates.add(row.target_local_date)
            date_sets.append(model_dates)
        for row in joined:
            if row["station_id"] != station_id or row["season_bin"] != season_bin:
                continue
            target = str(row["event_date"])
            model = str(row["model_id"])
            forecasts_by_model_date[(model, target)] = float(row["raw_tmax_c"])
            truths_by_date[target] = float(row["official_tmax_c"])
        common_dates = sorted(set.intersection(*date_sets))
        outer_train_n = max(2, int(len(common_dates) * 0.8))
        outer_train_dates = common_dates[:outer_train_n]
        holdout_dates = common_dates[outer_train_n:]
        if len(outer_train_dates) < 3 or not holdout_dates:
            continue
        ensemble_training_cutoff = _settlement_day_boundary_utc(
            holdout_dates[0],
            target_timezone=target_timezone,
            boundary="start",
        )
        paired_by_model_date = {
            (row.model_id, row.target_local_date): row.paired_asof_utc
            for model in members
            for row in by_scope[(station_id, model, season_bin)]
        }
        outer_train_dates = [
            target
            for target in outer_train_dates
            if all(
                timestamp_lte(
                    paired_by_model_date[(model, target)],
                    ensemble_training_cutoff,
                )
                for model in members
            )
        ]
        if len(outer_train_dates) < 3:
            continue
        recent_n = max(1, int(math.ceil(len(outer_train_dates) * ENSEMBLE_RECENT_FRACTION)))
        recent_dates = set(outer_train_dates[-recent_n:])
        residual_covariance_all, residual_correlation_all = _residual_matrices(
            models=members,
            dates=outer_train_dates,
            residuals_by_model_date=residuals_by_model_date,
        )
        previous = previous_by_scope.get((
            station_id, season_bin, replay_lead_bucket, run_cycle,
            SETTLEMENT_DEFINITION,
        ))
        previous_weights = None
        previous_sha = None
        if previous is not None:
            if _utc(previous.valid_from_utc) > as_of:
                raise ContractError("previous_ensemble_available_after_asof")
            if previous.holdout_end_date >= holdout_dates[0]:
                raise ContractError("previous_ensemble_holdout_overlap")
            previous_sha = previous.content_sha256
            previous_weights = previous.supported_masks.get(
                model_set_hash(previous.candidate_models)
            )
        candidate_rows: list[dict[str, Any]] = []
        for size in range(1, len(members) + 1):
            for subset in itertools.combinations(members, size):
                formal_selection_eligible = (
                    len(subset) >= MIN_FORMAL_ENSEMBLE_MODELS
                )
                rolling_rows = _rolling_probability_rows(
                    models=subset,
                    dates=outer_train_dates,
                    forecasts_by_model_date=forecasts_by_model_date,
                    residuals_by_model_date=residuals_by_model_date,
                    truths_by_date=truths_by_date,
                )
                if not rolling_rows:
                    raise ContractError("ensemble_rolling_origin_folds_missing")
                validation_dates = [row[0] for row in rolling_rows]
                n_eff = _effective_sample_n(validation_dates, recent_dates)
                try:
                    if formal_selection_eligible:
                        fitted, _parameters, metrics = (
                            _fit_rolling_probability_ensemble(
                                models=subset,
                                history_dates=outer_train_dates,
                                forecasts_by_model_date=forecasts_by_model_date,
                                residuals_by_model_date=residuals_by_model_date,
                                truths_by_date=truths_by_date,
                                previous_weights=previous_weights,
                                max_weight_delta=ENSEMBLE_MAX_WEIGHT_DELTA,
                            )
                        )
                    else:
                        subset_correlation = {
                            model: {
                                other: residual_correlation_all[model][other]
                                for other in subset
                            }
                            for model in subset
                        }
                        fitted, metrics = _fit_probability_weights(
                            models=subset,
                            rows=rolling_rows,
                            recent_dates=recent_dates,
                            residual_correlation=subset_correlation,
                            effective_sample_n=n_eff,
                            previous_weights=previous_weights,
                            max_weight_delta=ENSEMBLE_MAX_WEIGHT_DELTA,
                        )
                except ContractError as exc:
                    if str(exc) != "ensemble_weight_delta_constraint_no_feasible_weights":
                        raise
                    candidate_rows.append({
                        "models": list(subset),
                        "eligible": False,
                        "formal_selection_eligible": False,
                        "candidate_role": (
                            "single_model_ablation"
                            if len(subset) == 1
                            else "formal_ensemble"
                        ),
                        "reason": str(exc),
                    })
                    continue
                candidate_rows.append({
                    "models": list(subset),
                    "eligible": True,
                    "formal_selection_eligible": formal_selection_eligible,
                    "candidate_role": (
                        "single_model_ablation"
                        if len(subset) == 1
                        else "formal_ensemble"
                    ),
                    "rolling_origin_fold_count": len(rolling_rows),
                    "weights": fitted,
                    "effective_sample_n": n_eff,
                    **metrics,
                })
        eligible_candidates = [
            item
            for item in candidate_rows
            if item.get("eligible") is True
            and item.get("formal_selection_eligible") is True
        ]
        if not eligible_candidates:
            raise ContractError("ensemble_selection_no_eligible_candidate")
        winner = min(
            eligible_candidates,
            key=lambda item: (
                float(item["objective"]),
                len(item["models"]),
                tuple(item["models"]),
            ),
        )
        selected_models = tuple(str(item) for item in winner["models"])
        selected_weights = {
            str(model): float(weight)
            for model, weight in dict(winner["weights"]).items()
        }
        masks: dict[str, dict[str, float]] = {}
        for size in range(MIN_FORMAL_ENSEMBLE_MODELS, len(selected_models) + 1):
            for subset in itertools.combinations(selected_models, size):
                if subset == selected_models:
                    fitted = selected_weights
                else:
                    fitted, _parameters, _metrics_unused = (
                        _fit_rolling_probability_ensemble(
                        models=subset,
                        history_dates=outer_train_dates,
                        forecasts_by_model_date=forecasts_by_model_date,
                        residuals_by_model_date=residuals_by_model_date,
                        truths_by_date=truths_by_date,
                        previous_weights=None,
                        max_weight_delta=1.0,
                        )
                    )
                masks[model_set_hash(subset)] = fitted
        selected_covariance = {
            model: {
                other: residual_covariance_all[model][other]
                for other in selected_models
            }
            for model in selected_models
        }
        selected_correlation = {
            model: {
                other: residual_correlation_all[model][other]
                for other in selected_models
            }
            for model in selected_models
        }
        holdout_rows = _fixed_probability_rows(
            models=selected_models,
            train_dates=outer_train_dates,
            evaluation_dates=holdout_dates,
            forecasts_by_model_date=forecasts_by_model_date,
            residuals_by_model_date=residuals_by_model_date,
            truths_by_date=truths_by_date,
        )
        holdout_metrics = _probability_metrics(
            holdout_rows, selected_weights, recent_dates=set(holdout_dates)
        )
        holdout_oos_metrics = {
            key: holdout_metrics[key]
            for key in ("n", "brier_score", "nll", "crps")
        }
        actual_delta = _actual_weight_delta(selected_weights, previous_weights)
        selection_key = content_sha256({
            "method_version": ENSEMBLE_SELECTION_METHOD,
            "station_id": station_id,
            "season_bin": season_bin,
            "lead_bucket": replay_lead_bucket,
            "run_cycle": run_cycle,
            "train_dates": outer_train_dates,
            "holdout_dates": holdout_dates,
            "selected_models": list(selected_models),
            "selected_weights": selected_weights,
            "previous_ensemble_artifact_sha256": previous_sha,
        })[:12]
        selection_identity = {
            "selection_evidence_id": (
                f"ens-selection-{station_id}-{season_bin}-"
                f"{replay['replay_sha256'][:12]}-{selection_key}"
            ),
            "station_id": station_id,
            "season_bin": season_bin,
            "lead_bucket": replay_lead_bucket,
            "run_cycle": run_cycle,
            "settlement_definition": SETTLEMENT_DEFINITION,
            "method_version": ENSEMBLE_SELECTION_METHOD,
            "train_start_date": outer_train_dates[0],
            "train_end_date": outer_train_dates[-1],
            "recent_start_date": min(recent_dates),
            "recent_end_date": max(recent_dates),
            "holdout_start_date": holdout_dates[0],
            "holdout_end_date": holdout_dates[-1],
            "candidate_scores": candidate_rows,
            "selected_models": list(selected_models),
            "selected_weights": selected_weights,
            "oos_metrics": holdout_oos_metrics,
            "residual_covariance": selected_covariance,
            "residual_correlation": selected_correlation,
            "effective_sample_n": float(winner["effective_sample_n"]),
            "previous_ensemble_artifact_sha256": previous_sha,
            "max_weight_delta": ENSEMBLE_MAX_WEIGHT_DELTA,
            "actual_max_weight_delta": actual_delta,
        }
        selection_artifact = ModelEnsembleSelectionEvidence.from_mapping({
            **selection_identity,
            "content_sha256": content_sha256(selection_identity),
        })
        ensemble_selection_evidence.append(selection_artifact)
        ensemble_selection_reports.append({
            **selection_identity,
            "selected_model_set_hash": model_set_hash(selected_models),
            "selection_evidence_sha256": selection_artifact.content_sha256,
            "training_cutoff_utc": ensemble_training_cutoff,
            "strict_asof_train_holdout_disjoint": True,
            "online_model_chasing_allowed": False,
        })
        selection_version = content_sha256({
            "method": ENSEMBLE_SELECTION_METHOD,
            "station_id": station_id,
            "season_bin": season_bin,
            "lead_bucket": replay_lead_bucket,
            "run_cycle": run_cycle,
            "selected_models": list(selected_models),
            "supported_masks": masks,
            "selection_evidence_sha256": selection_artifact.content_sha256,
        })[:12]
        identity = {
            "ensemble_artifact_id": (
                f"ens-candidate-{station_id}-{season_bin}-"
                f"{replay['replay_sha256'][:12]}-{selection_version}"
            ),
            "station_id": station_id,
            "season_bin": season_bin,
            "lead_bucket": replay_lead_bucket,
            "run_cycle": run_cycle,
            "settlement_definition": SETTLEMENT_DEFINITION,
            "candidate_models": list(selected_models),
            "supported_masks": masks,
            "residual_covariance": selected_covariance,
            "correlation_penalty": float(winner["correlation_concentration"]),
            "selection_evidence_sha256": selection_artifact.content_sha256,
            "previous_ensemble_artifact_sha256": previous_sha,
            "weight_method": ENSEMBLE_SELECTION_METHOD,
            "effective_sample_n": float(winner["effective_sample_n"]),
            "max_weight_delta": ENSEMBLE_MAX_WEIGHT_DELTA,
            "train_start_date": outer_train_dates[0],
            "train_end_date": outer_train_dates[-1],
            "recent_start_date": min(recent_dates),
            "recent_end_date": max(recent_dates),
            "holdout_start_date": holdout_dates[0],
            "holdout_end_date": holdout_dates[-1],
            "oos_metrics": holdout_oos_metrics,
            "valid_from_utc": valid_from,
            "valid_until_utc": valid_until,
            "promoted": False,
        }
        ensemble_artifacts.append(ModelEnsembleArtifact.from_mapping({
            **identity,
            "content_sha256": content_sha256(identity),
        }))

    evidence_identity = {
        "schema": SCHEMA,
        "as_of_utc": _utc_text(as_of),
        "target_timezone": target_timezone,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "strict_asof_replay_sha256": replay["replay_sha256"],
        "input_artifacts": dict(input_artifacts or {}),
        "station_registry": station_registry,
        "truth_evidence": truth_evidence,
        "temporal_boundary": {
            "target_timezone": target_timezone,
            "as_of_utc": _utc_text(as_of),
            "closed_settlement_event_days_only": True,
            "maturity_keys": sorted({row["maturity_key"] for row in model_days}),
            "quarantined_non_closed_model_day_count": len(quarantined_model_days),
            "quarantined_non_closed_model_days": quarantined_model_days[:500],
        },
        "calibration_model_rows": calibration_model_rows,
        "model_arrival_evidence": arrival_rows,
        "residual_evidence": residual_evidence,
        "calibration_window": calibration_window,
        "calibration_artifacts": [_artifact_mapping(item) for item in artifacts],
        "ensemble_artifacts": [_ensemble_mapping(item) for item in ensemble_artifacts],
        "ensemble_selection_evidence": [
            _selection_mapping(item) for item in ensemble_selection_evidence
        ],
        "ensemble_selection_reports": ensemble_selection_reports,
        "oos_reports": oos_reports,
        "promotion_decisions": [],
        "checks": {
            "strict_asof_replay_pass": bool(
                replay["windows"]["mutually_exclusive"]
                and replay["windows"]["same_maturity"]
                and replay["crossseason"]["strict_asof"]
                and replay["crossseason"]["lineage_complete"]
                and model_set_complete
            ),
            "model_lineage_complete": bool(model_quality["all_model_runs_lineage_complete"]),
            "truth_lineage_complete": bool(truth_quality["all_samples_lineage_complete"]),
            "residual_count_positive": bool(residual_evidence),
            "residual_pairing_strict_asof": bool(residual_evidence)
            and all(
                timestamp_lte(item["paired_asof_utc"], _utc_text(as_of))
                for item in residual_evidence
            ),
            "train_holdout_disjoint": all(
                item.get("strict_asof_train_holdout_disjoint") is True
                for item in [*oos_reports, *ensemble_selection_reports]
            ),
            # This flag answers whether at least one model-local scope has
            # enough closed historical samples.  The separate
            # ``model_provider_set_complete`` check remains the hard gate for
            # any ensemble or production promotion.
            "calibration_window_sufficient": eligible_scope_count > 0,
            "candidate_artifact_count_positive": bool(artifacts),
            "all_candidates_unpromoted": all(not item.promoted for item in artifacts + ensemble_artifacts),
            "published_receive_timestamps_complete": bool(calibration_model_rows)
            and all(item["timestamps_complete"] for item in calibration_model_rows),
            "truth_source_canonical_ingress": canonical_input is not None,
            "multi_model_available": (
                len({item["model_id"] for item in calibration_model_rows})
                >= MIN_FORMAL_ENSEMBLE_MODELS
            ),
            "candidate_model_pool": list(MODEL_IDS),
            "model_provider_set_complete": model_set_complete,
            "oos_promotion_gate_complete": False,
            "promotion_allowed": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "source_quality": {
            "observation_source": "CANONICAL_AMSC_AWC_INGRESS" if canonical_input is not None else "IEM_OFFLINE_MIRROR_CROSSCHECK_ONLY",
            "model_source": (
                "FROZEN_NWP_REPLAY_WITH_EXPLICIT_AGGREGATOR_LINEAGE"
                if any(bool(row.get("aggregator_or_republisher")) for row in calibration_model_rows)
                else "FROZEN_OFFICIAL_NWP_REPLAY"
            ),
            "aggregator_model_row_count": sum(
                1 for row in calibration_model_rows if row.get("aggregator_or_republisher")
            ),
            "native_model_row_count": sum(
                1 for row in calibration_model_rows if not row.get("aggregator_or_republisher")
            ),
            "model_arrival_source": "PROSPECTIVE_NATIVE_PROVIDER_RECEIPTS" if arrival_rows else "NOT_CAPTURED",
            "model_arrival_audit": arrival_audit,
            "model_provider_set_blockers": model_set_blockers[:100],
            "status": "EVIDENCE_ONLY_QUARANTINED" if canonical_input is None else "CANONICAL_TRUTH_WITH_PROMOTION_CLOSED",
            "canonical_observation_evidence_sha256": (
                canonical_input.get("receipt_sha256") if canonical_input is not None else None
            ),
            "cross_check_observation_quality": cross_check_quality,
            "cross_check_station_days": len(cross_check_days),
        },
        "calibration_activations": [],
        "boundary": {
            "offline_research_only": True,
            "uses_production_database": False,
            "writes_production_state": False,
            "reads_probability_state": False,
            "reads_market_state": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    evidence_sha = content_sha256(evidence_identity)
    decisions = [
        _promotion_decision(
            artifact_kind="MODEL_CALIBRATION",
            artifact_id=item.calibration_artifact_id,
            evidence_sha256=evidence_sha,
            decided_at_utc=_utc_text(as_of),
        )
        for item in artifacts
    ] + [
        _promotion_decision(
            artifact_kind="MODEL_ENSEMBLE",
            artifact_id=item.ensemble_artifact_id,
            evidence_sha256=evidence_sha,
            decided_at_utc=_utc_text(as_of),
        )
        for item in ensemble_artifacts
    ]
    final_identity = {**evidence_identity, "promotion_decisions": decisions, "evidence_sha256": evidence_sha}
    return {**final_identity, "receipt_sha256": content_sha256(final_identity)}


def build_blocked_calibration_evidence(
    *,
    as_of_utc: str,
    target_timezone: str,
    input_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    canonical_observation_evidence: Mapping[str, Any] | None = None,
    model_arrival_evidence: Mapping[str, Any] | None = None,
    reason_code: str,
) -> dict[str, Any]:
    """Emit a durable, fail-closed receipt when replay inputs are invalid.

    A malformed or timezone-incompatible legacy research file must not make
    the CLI crash without evidence, and must never be converted into a
    candidate artifact.  This helper deliberately emits the same boundary
    shape as a normal calibration receipt with every promotion and production
    side effect disabled.
    """

    as_of = _utc(as_of_utc)
    arrival_rows = []
    if isinstance(model_arrival_evidence, Mapping):
        raw_arrivals = model_arrival_evidence.get("arrivals")
        if isinstance(raw_arrivals, list):
            arrival_rows = [dict(row) for row in raw_arrivals if isinstance(row, Mapping)]
    evidence_identity = {
        "schema": SCHEMA,
        "as_of_utc": _utc_text(as_of),
        "target_timezone": target_timezone,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "strict_asof_replay_sha256": content_sha256({
            "state": "BLOCKED_EVIDENCE",
            "reason_code": reason_code,
            "as_of_utc": _utc_text(as_of),
            "target_timezone": target_timezone,
        }),
        "input_artifacts": dict(input_artifacts or {}),
        "station_registry": [],
        "truth_evidence": [],
        "calibration_model_rows": [],
        "model_arrival_evidence": arrival_rows,
        "residual_evidence": [],
        "calibration_window": {
            "closed_settlement_event_day_count": 0,
            "closed_settlement_event_dates": [],
            "joined_station_day_count": 0,
            "residual_count": 0,
            "scope_count": 0,
            "scope_sample_counts": {},
            "minimum_scope_sample_n": 4,
            "insufficient_scope_count": 0,
            "insufficient_scope_keys": [],
            "state": "BLOCKED_EVIDENCE",
            "promotion_allowed": False,
        },
        "calibration_artifacts": [],
        "ensemble_artifacts": [],
        "oos_reports": [],
        "promotion_decisions": [],
        "checks": {
            "strict_asof_replay_pass": False,
            "model_lineage_complete": False,
            "truth_lineage_complete": False,
            "residual_count_positive": False,
            "calibration_window_sufficient": False,
            "candidate_artifact_count_positive": False,
            "all_candidates_unpromoted": True,
            "published_receive_timestamps_complete": False,
            "truth_source_canonical_ingress": canonical_observation_evidence is not None,
            "multi_model_available": False,
            "candidate_model_pool": list(MODEL_IDS),
            "model_provider_set_complete": False,
            "oos_promotion_gate_complete": False,
            "promotion_allowed": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "source_quality": {
            "observation_source": "CANONICAL_AMSC_AWC_INGRESS" if canonical_observation_evidence is not None else "NOT_EVALUATED",
            "model_source": "FROZEN_OFFICIAL_NWP_REPLAY",
            "model_arrival_source": "PROSPECTIVE_NATIVE_PROVIDER_RECEIPTS" if arrival_rows else "NOT_CAPTURED",
            "model_arrival_audit": {},
            "model_provider_set_blockers": [],
            "status": "VALID_BLOCKED_EVIDENCE",
            "failure_reason": reason_code,
            "canonical_observation_evidence_sha256": (
                canonical_observation_evidence.get("receipt_sha256")
                if isinstance(canonical_observation_evidence, Mapping)
                else None
            ),
        },
        "calibration_activations": [],
        "boundary": {
            "offline_research_only": True,
            "uses_production_database": False,
            "writes_production_state": False,
            "reads_probability_state": False,
            "reads_market_state": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    evidence_sha = content_sha256(evidence_identity)
    final_identity = {**evidence_identity, "evidence_sha256": evidence_sha}
    return {**final_identity, "receipt_sha256": content_sha256(final_identity)}


__all__ = ["SCHEMA", "build_blocked_calibration_evidence", "build_calibration_evidence"]
