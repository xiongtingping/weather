#!/usr/bin/env python3
"""Evaluate an isolated formal-truth calibration candidate with a chronological OOS audit.

This command reads one immutable calibration evidence packet and writes one
bounded audit packet.  It does not fit new parameters, open SQLite, alter an
activation, materialize P0/P1, read markets, or call an order gateway.

The audit intentionally distinguishes:

* ``oos_evaluation_complete``: every candidate scope has a non-empty
  chronological holdout with no train/holdout overlap; and
* ``promotion_gate_passed``: the stricter model/ensemble/arrival gates needed
  for production calibration are all satisfied.

Historical replay can complete the first check while the second remains
closed.  In particular, retrospective downloads do not become native arrival
receipts, and a per-scope regression cannot be hidden by an aggregate average.
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration_evidence import (  # noqa: E402
    _bucket_distribution,
    _probability_metrics,
)
from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    ModelEnsembleSelectionEvidence,
    content_sha256,
    model_set_hash,
)
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_calibration_oos_audit_v1"
DEFAULT_INPUT = ROOT / "output/audits/bayes_basket_target_calibration_evidence.json"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_calibration_oos_audit.json"


def _int(name: str, value: Any, *, minimum: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result < minimum:
        raise ContractError(f"{name}_below_minimum")
    return result


def _number(name: str, value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result) or result < 0.0:
        raise ContractError(f"{name}_invalid")
    return result


def _finite_number(name: str, value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid")
    return result


def _error_metrics(errors: list[float]) -> dict[str, float | int]:
    if not errors:
        return {"n": 0, "mae_c": None, "rmse_c": None}
    return {
        "n": len(errors),
        "mae_c": sum(abs(value) for value in errors) / len(errors),
        "rmse_c": math.sqrt(sum(value * value for value in errors) / len(errors)),
    }


def _scope_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("station_id") or "").strip().upper(),
        str(item.get("model_id") or "").strip().upper(),
        str(item.get("season_bin") or "").strip(),
    )


def _target_date(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    return result


def _load_input(path: Path) -> dict[str, Any]:
    value = load_bounded_json(path.expanduser().resolve())
    if not isinstance(value, Mapping):
        raise ContractError("calibration_oos_input_mapping_required")
    payload = dict(value)
    if payload.get("schema") != "bayes_basket_target_calibration_evidence_v1":
        raise ContractError("calibration_oos_input_schema_invalid")
    supplied = str(payload.get("receipt_sha256") or "").strip()
    if supplied != content_sha256({key: item for key, item in payload.items() if key != "receipt_sha256"}):
        raise ContractError("calibration_oos_input_receipt_mismatch")
    return payload


def _scope(payload: Mapping[str, Any]) -> tuple[tuple[str, ...], dict[str, Any]]:
    raw_scope = payload.get("scope")
    if not isinstance(raw_scope, Mapping):
        raise ContractError("calibration_oos_scope_missing")
    stations = tuple(sorted({str(item).strip().upper() for item in (raw_scope.get("included_station_ids") or []) if str(item).strip()}))
    if not stations:
        raise ContractError("calibration_oos_station_scope_empty")
    if raw_scope.get("mode") != "allowlist":
        raise ContractError("calibration_oos_formal_scope_not_explicit")
    registry = payload.get("station_registry")
    if not isinstance(registry, list) or not registry:
        raise ContractError("calibration_oos_station_registry_missing")
    registry_by_station = {
        str(item.get("station_id") or "").strip().upper(): item
        for item in registry
        if isinstance(item, Mapping)
    }
    if set(registry_by_station) != set(stations):
        raise ContractError("calibration_oos_station_registry_scope_mismatch")
    included_lanes = {
        str(item.get("source_lane") or "").strip().lower()
        for item in registry_by_station.values()
    }
    if not included_lanes or not included_lanes <= {"amsc", "awc"}:
        raise ContractError("calibration_oos_formal_truth_lane_invalid")
    declared_excluded = {
        str(item).strip().lower()
        for item in (raw_scope.get("excluded_source_lanes") or [])
        if str(item).strip()
    }
    if declared_excluded != {"amsc", "awc"} - included_lanes:
        raise ContractError("calibration_oos_formal_lane_scope_mismatch")
    return stations, dict(raw_scope)


def evaluate(payload: Mapping[str, Any], *, input_path: Path) -> dict[str, Any]:
    stations, scope = _scope(payload)
    raw_residual_rows = [item for item in (payload.get("residual_evidence") or []) if isinstance(item, Mapping)]
    model_rows = [item for item in (payload.get("calibration_model_rows") or []) if isinstance(item, Mapping)]
    reports = [item for item in (payload.get("oos_reports") or []) if isinstance(item, Mapping)]
    artifacts = [item for item in (payload.get("calibration_artifacts") or []) if isinstance(item, Mapping)]
    if not raw_residual_rows or not model_rows or not reports or not artifacts:
        raise ContractError("calibration_oos_candidate_inputs_empty")

    # Residual evidence intentionally contains only the residual identity and
    # its lineage.  The target day is authoritative in the paired model row;
    # recover it by model_run_id instead of treating an absent field as an
    # empty date.  This keeps the audit compatible with the canonical evidence
    # packet while refusing ambiguous or conflicting joins.
    model_date_by_run: dict[str, str] = {}
    model_scope_by_run: dict[str, tuple[str, str]] = {}
    for row in model_rows:
        run_id = str(row.get("model_run_id") or "").strip()
        if not run_id:
            raise ContractError("calibration_oos_model_run_id_missing")
        target = _target_date("calibration_oos_model_target_local_date", row.get("target_local_date"))
        scope_key = (
            str(row.get("station_id") or "").strip().upper(),
            str(row.get("model_id") or "").strip().upper(),
        )
        if not all(scope_key):
            raise ContractError("calibration_oos_model_scope_missing")
        previous = model_date_by_run.get(run_id)
        if previous is not None and previous != target:
            raise ContractError("calibration_oos_model_run_target_date_conflict")
        previous_scope = model_scope_by_run.get(run_id)
        if previous_scope is not None and previous_scope != scope_key:
            raise ContractError("calibration_oos_model_run_scope_conflict")
        model_date_by_run[run_id] = target
        model_scope_by_run[run_id] = scope_key

    truth_date_by_id = {
        str(row.get("truth_id") or "").strip(): _target_date(
            "calibration_oos_truth_local_date", row.get("local_date")
        )
        for row in (payload.get("truth_evidence") or [])
        if isinstance(row, Mapping) and str(row.get("truth_id") or "").strip()
    }
    residual_rows: list[dict[str, Any]] = []
    for raw in raw_residual_rows:
        row = dict(raw)
        run_id = str(row.get("model_run_id") or "").strip()
        truth_id = str(row.get("truth_id") or "").strip()
        explicit_target = str(row.get("target_local_date") or "").strip()
        target = explicit_target or model_date_by_run.get(run_id) or truth_date_by_id.get(truth_id)
        target = _target_date("calibration_oos_residual_target_local_date", target)
        if explicit_target and model_date_by_run.get(run_id) not in (None, explicit_target):
            raise ContractError("calibration_oos_residual_model_target_date_mismatch")
        expected_scope = model_scope_by_run.get(run_id)
        actual_scope = (
            str(row.get("station_id") or "").strip().upper(),
            str(row.get("model_id") or "").strip().upper(),
        )
        if expected_scope is not None and expected_scope != actual_scope:
            raise ContractError("calibration_oos_residual_model_scope_mismatch")
        row["target_local_date"] = target
        residual_rows.append(row)

    season_by_run: dict[str, str] = {}
    for row in residual_rows:
        run_id = str(row.get("model_run_id") or "").strip()
        season = str(row.get("season_bin") or "").strip()
        if not run_id or not season:
            raise ContractError("calibration_oos_residual_season_missing")
        previous = season_by_run.get(run_id)
        if previous is not None and previous != season:
            raise ContractError("calibration_oos_model_run_season_conflict")
        season_by_run[run_id] = season

    truth_by_scope_date: dict[tuple[str, str], float] = {}
    for raw_truth in (payload.get("truth_evidence") or []):
        if not isinstance(raw_truth, Mapping):
            continue
        station = str(raw_truth.get("station_id") or "").strip().upper()
        local_date = _target_date("calibration_oos_truth_local_date", raw_truth.get("local_date"))
        key = (station, local_date)
        value = _finite_number("calibration_oos_truth_temperature", raw_truth.get("official_tmax_c"))
        previous = truth_by_scope_date.get(key)
        if previous is not None and previous != value:
            raise ContractError("calibration_oos_truth_duplicate_conflict")
        truth_by_scope_date[key] = value

    model_forecasts: dict[tuple[str, str, str], dict[str, float]] = defaultdict(dict)
    if payload.get("ensemble_artifacts"):
        for raw_model in model_rows:
            run_id = str(raw_model.get("model_run_id") or "").strip()
            station = str(raw_model.get("station_id") or "").strip().upper()
            model_id = str(raw_model.get("model_id") or "").strip().upper()
            target = model_date_by_run[run_id]
            season = season_by_run.get(run_id) or str(raw_model.get("season_bin") or "").strip()
            if not season:
                raise ContractError("calibration_oos_model_season_missing")
            model_forecasts[(station, season, target)][model_id] = _finite_number(
                "calibration_oos_model_raw_temperature", raw_model.get("raw_tmax_c")
            )

    residuals_by_key: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in residual_rows:
        key = (
            str(row.get("station_id") or "").strip().upper(),
            str(row.get("model_id") or "").strip().upper(),
            str(row.get("season_bin") or "").strip(),
        )
        if key[0] not in stations:
            raise ContractError("calibration_oos_residual_outside_formal_scope")
        residuals_by_key[key].append(row)
    for rows in residuals_by_key.values():
        rows.sort(key=lambda row: str(row.get("target_local_date") or ""))

    artifact_keys = [_scope_key(item) for item in artifacts]
    report_keys = [_scope_key(item) for item in reports]
    if any(not all(key) for key in artifact_keys + report_keys):
        raise ContractError("calibration_oos_scope_key_incomplete")
    if len(set(artifact_keys)) != len(artifact_keys):
        raise ContractError("calibration_oos_duplicate_artifact_scope")
    if len(set(report_keys)) != len(report_keys):
        raise ContractError("calibration_oos_duplicate_report_scope")
    artifacts_by_key = dict(zip(artifact_keys, artifacts))
    artifact_scope_set = set(artifact_keys)
    report_scope_set = set(report_keys)
    report_rows: list[dict[str, Any]] = []
    ensemble_report_rows: list[dict[str, Any]] = []
    ensemble_chronology_failures: list[str] = []
    ensemble_metric_failures: list[str] = []
    total_raw_mae = total_cal_mae = 0.0
    total_raw_rmse_sq = total_cal_rmse_sq = 0.0
    total_holdout_n = 0
    improved = worsened = tied = 0
    chronology_failures: list[str] = []
    metric_failures: list[str] = []
    for report in reports:
        key = _scope_key(report)
        train_n = _int("oos_train_n", report.get("train_n"), minimum=1)
        holdout_n = _int("oos_holdout_n", report.get("holdout_n"), minimum=1)
        rows = residuals_by_key.get(key, [])
        if len(rows) < train_n + holdout_n:
            chronology_failures.append("|".join(key) + ":row_count")
        train_dates = [str(row.get("target_local_date") or "") for row in rows[:train_n]]
        holdout_dates = [str(row.get("target_local_date") or "") for row in rows[train_n:train_n + holdout_n]]
        all_dates = train_dates + holdout_dates
        if (
            not train_dates
            or not holdout_dates
            or max(train_dates) >= min(holdout_dates)
            or len(set(train_dates) & set(holdout_dates))
            or len(set(all_dates)) != len(all_dates)
        ):
            chronology_failures.append("|".join(key) + ":date_overlap_or_order")
        artifact = artifacts_by_key.get(key)
        trained_through = str((artifact or {}).get("trained_through_utc") or "")
        if trained_through and holdout_dates and trained_through[:10] >= min(holdout_dates):
            chronology_failures.append("|".join(key) + ":artifact_training_boundary")
        raw = report.get("raw")
        calibrated = report.get("calibrated")
        if not isinstance(raw, Mapping) or not isinstance(calibrated, Mapping):
            metric_failures.append("|".join(key) + ":metrics_missing")
            continue
        raw_mae = _number("raw_mae_c", raw.get("mae_c"))
        cal_mae = _number("calibrated_mae_c", calibrated.get("mae_c"))
        raw_rmse = _number("raw_rmse_c", raw.get("rmse_c"))
        cal_rmse = _number("calibrated_rmse_c", calibrated.get("rmse_c"))
        observed_n = _int("raw_metric_n", raw.get("n"), minimum=1)
        if observed_n != holdout_n or _int("calibrated_metric_n", calibrated.get("n"), minimum=1) != holdout_n:
            metric_failures.append("|".join(key) + ":metric_n_mismatch")
        delta = cal_mae - raw_mae
        if delta < 0:
            improved += 1
        elif delta > 0:
            worsened += 1
        else:
            tied += 1
        total_raw_mae += raw_mae * holdout_n
        total_cal_mae += cal_mae * holdout_n
        total_raw_rmse_sq += raw_rmse * raw_rmse * holdout_n
        total_cal_rmse_sq += cal_rmse * cal_rmse * holdout_n
        total_holdout_n += holdout_n
        report_rows.append({
            "scope": "|".join(key),
            "train_n": train_n,
            "holdout_n": holdout_n,
            "train_dates": train_dates,
            "holdout_dates": holdout_dates,
            "raw_mae_c": raw_mae,
            "calibrated_mae_c": cal_mae,
            "mae_delta_c": delta,
            "raw_rmse_c": raw_rmse,
            "calibrated_rmse_c": cal_rmse,
        })

    artifact_bias_by_key = {
        _scope_key(item): _finite_number("calibration_oos_artifact_bias", item.get("bias_c"))
        for item in artifacts
    } if payload.get("ensemble_artifacts") else {}
    artifact_scale_by_key = {
        _scope_key(item): _number(
            "calibration_oos_artifact_residual_scale", item.get("residual_scale_c")
        )
        for item in artifacts
    } if payload.get("ensemble_artifacts") else {}
    raw_selection_evidence = [
        item for item in (payload.get("ensemble_selection_evidence") or [])
        if isinstance(item, Mapping)
    ]
    selection_by_sha: dict[str, ModelEnsembleSelectionEvidence] = {}
    for raw_selection in raw_selection_evidence:
        selected = ModelEnsembleSelectionEvidence.from_mapping(raw_selection)
        selection_by_sha[selected.content_sha256] = selected
    ensemble_total_raw_mae = ensemble_total_cal_mae = 0.0
    ensemble_total_raw_rmse_sq = ensemble_total_cal_rmse_sq = 0.0
    ensemble_total_uniform_mae = ensemble_total_uniform_rmse_sq = 0.0
    ensemble_holdout_n = 0
    ensemble_improved = ensemble_worsened = ensemble_tied = 0
    selection_improved = selection_worsened = selection_tied = 0
    probability_scope_passed = probability_scope_failed = 0
    for ensemble in (item for item in (payload.get("ensemble_artifacts") or []) if isinstance(item, Mapping)):
        ensemble_key = (
            str(ensemble.get("station_id") or "").strip().upper(),
            str(ensemble.get("season_bin") or "").strip(),
        )
        scope_label = "|".join(ensemble_key)
        selected_models = tuple(
            sorted(
                str(item).strip().upper()
                for item in (ensemble.get("candidate_models") or [])
                if str(item).strip()
            )
        )
        if not selected_models:
            ensemble_metric_failures.append(scope_label + ":selected_model_set_missing")
            continue
        selected_model_mask = model_set_hash(selected_models)
        weights_by_mask = ensemble.get("supported_masks")
        weights = weights_by_mask.get(selected_model_mask) if isinstance(weights_by_mask, Mapping) else None
        if (
            not isinstance(weights, Mapping)
            or set(str(key).strip().upper() for key in weights) != set(selected_models)
        ):
            ensemble_metric_failures.append(scope_label + ":selected_model_mask_missing")
            continue
        normalized_weights = {str(key).strip().upper(): _number("ensemble_oos_weight", value) for key, value in weights.items()}
        if abs(sum(normalized_weights.values()) - 1.0) > 1e-8:
            ensemble_metric_failures.append(scope_label + ":weights_not_normalized")
            continue
        selection_sha = str(ensemble.get("selection_evidence_sha256") or "").strip().lower()
        selection = selection_by_sha.get(selection_sha)
        if selection is None:
            ensemble_metric_failures.append(scope_label + ":selection_evidence_binding_missing")
            continue
        if (
            selection.selected_models != selected_models
            or dict(selection.selected_weights) != normalized_weights
            or selection.station_id.upper() != ensemble_key[0]
            or selection.season_bin != ensemble_key[1]
            or selection.lead_bucket != str(ensemble.get("lead_bucket") or "")
            or selection.run_cycle != str(ensemble.get("run_cycle") or "")
            or selection.settlement_definition
            != str(ensemble.get("settlement_definition") or "")
        ):
            ensemble_metric_failures.append(scope_label + ":selection_evidence_scope_mismatch")
            continue
        dates = sorted({
            target
            for (station, season, target), models_for_day in model_forecasts.items()
            if (station, season) == ensemble_key and set(models_for_day) >= set(normalized_weights)
        })
        train_n = max(2, int(len(dates) * 0.8)) if dates else 0
        holdout_dates = dates[train_n:]
        train_dates = dates[:train_n]
        if not train_dates or not holdout_dates:
            ensemble_chronology_failures.append(scope_label + ":holdout_empty")
            continue
        if max(train_dates) >= min(holdout_dates) or len(set(train_dates) & set(holdout_dates)):
            ensemble_chronology_failures.append(scope_label + ":date_overlap_or_order")
            continue
        bias_by_model = {
            model: artifact_bias_by_key.get((ensemble_key[0], model, ensemble_key[1]))
            for model in normalized_weights
        }
        if any(value is None for value in bias_by_model.values()):
            ensemble_metric_failures.append(scope_label + ":member_calibration_missing")
            continue
        scale_by_model = {
            model: artifact_scale_by_key.get((ensemble_key[0], model, ensemble_key[1]))
            for model in normalized_weights
        }
        if any(value is None or float(value) <= 0 for value in scale_by_model.values()):
            ensemble_metric_failures.append(scope_label + ":member_residual_scale_missing")
            continue
        if (
            selection.train_start_date != train_dates[0]
            or selection.train_end_date != train_dates[-1]
            or selection.holdout_start_date != holdout_dates[0]
            or selection.holdout_end_date != holdout_dates[-1]
        ):
            ensemble_chronology_failures.append(scope_label + ":selection_window_mismatch")
            continue
        holdout_raw_errors: list[float] = []
        holdout_calibrated_errors: list[float] = []
        holdout_uniform_candidate_errors: list[float] = []
        # Compare the OOS-selected weights with an equal-weight baseline over
        # the same selected mask.  This stays valid for any 2–5 model winner
        # and never silently substitutes the retired fixed three-model pool.
        benchmark_models = tuple(sorted(normalized_weights))
        benchmark_biases = {
            model: artifact_bias_by_key.get((ensemble_key[0], model, ensemble_key[1]))
            for model in benchmark_models
        }
        benchmark_scales = {
            model: artifact_scale_by_key.get((ensemble_key[0], model, ensemble_key[1]))
            for model in benchmark_models
        }
        selected_probability_rows = []
        uniform_probability_rows = []
        for target in holdout_dates:
            truth = truth_by_scope_date.get((ensemble_key[0], target))
            forecasts = model_forecasts.get((ensemble_key[0], ensemble_key[1], target), {})
            if truth is None or not set(normalized_weights) <= set(forecasts):
                ensemble_metric_failures.append(scope_label + ":holdout_join_incomplete")
                continue
            raw_forecast = sum(normalized_weights[model] * forecasts[model] for model in normalized_weights)
            calibrated_forecast = sum(
                normalized_weights[model] * (forecasts[model] + float(bias_by_model[model]))
                for model in normalized_weights
            )
            holdout_raw_errors.append(truth - raw_forecast)
            holdout_calibrated_errors.append(truth - calibrated_forecast)
            if (
                set(benchmark_models) <= set(forecasts)
                and all(value is not None for value in benchmark_biases.values())
                and all(value is not None and float(value) > 0 for value in benchmark_scales.values())
            ):
                uniform_forecast = sum(
                    forecasts[model] + float(benchmark_biases[model])
                    for model in benchmark_models
                ) / len(benchmark_models)
                holdout_uniform_candidate_errors.append(truth - uniform_forecast)
            truth_bucket = int(math.floor(float(truth) + 0.5))
            distribution_models = tuple(sorted(set(benchmark_models) | set(normalized_weights)))
            lower = min(
                truth_bucket,
                *(math.floor(
                    forecasts[model] + float(artifact_bias_by_key[(ensemble_key[0], model, ensemble_key[1])])
                    - 6.0 * float(artifact_scale_by_key[(ensemble_key[0], model, ensemble_key[1])])
                ) for model in distribution_models),
            ) - 1
            upper = max(
                truth_bucket,
                *(math.ceil(
                    forecasts[model] + float(artifact_bias_by_key[(ensemble_key[0], model, ensemble_key[1])])
                    + 6.0 * float(artifact_scale_by_key[(ensemble_key[0], model, ensemble_key[1])])
                ) for model in distribution_models),
            ) + 1
            buckets = tuple(range(int(lower), int(upper) + 1))
            all_probabilities = {
                model: _bucket_distribution(
                    mean=forecasts[model] + float(
                        artifact_bias_by_key[(ensemble_key[0], model, ensemble_key[1])]
                    ),
                    scale=float(
                        artifact_scale_by_key[(ensemble_key[0], model, ensemble_key[1])]
                    ),
                    buckets=buckets,
                )
                for model in distribution_models
            }
            selected_probability_rows.append((
                target,
                {model: all_probabilities[model] for model in normalized_weights},
                truth_bucket,
            ))
            uniform_probability_rows.append((
                target,
                {model: all_probabilities[model] for model in benchmark_models},
                truth_bucket,
            ))
        if len(holdout_raw_errors) != len(holdout_dates):
            continue
        if len(holdout_uniform_candidate_errors) != len(holdout_dates):
            ensemble_metric_failures.append(
                scope_label + ":uniform_candidate_benchmark_incomplete"
            )
            continue
        raw_metrics = _error_metrics(holdout_raw_errors)
        calibrated_metrics = _error_metrics(holdout_calibrated_errors)
        uniform_metrics = _error_metrics(holdout_uniform_candidate_errors)
        selected_probability_metrics = _probability_metrics(
            selected_probability_rows,
            normalized_weights,
            recent_dates=set(holdout_dates),
        )
        uniform_probability_metrics = _probability_metrics(
            uniform_probability_rows,
            {model: 1.0 / len(benchmark_models) for model in benchmark_models},
            recent_dates=set(holdout_dates),
        )
        probability_non_degradation = all(
            float(selected_probability_metrics[key])
            <= float(uniform_probability_metrics[key]) + 1e-12
            for key in ("brier_score", "nll", "crps")
        )
        if probability_non_degradation:
            probability_scope_passed += 1
        else:
            probability_scope_failed += 1
        for metric in ("brier_score", "nll", "crps"):
            if abs(
                float(selection.oos_metrics[metric])
                - float(selected_probability_metrics[metric])
            ) > 1e-9:
                ensemble_metric_failures.append(
                    scope_label + ":selection_probability_metric_mismatch:" + metric
                )
        raw_mae = float(raw_metrics["mae_c"])
        cal_mae = float(calibrated_metrics["mae_c"])
        raw_rmse = float(raw_metrics["rmse_c"])
        cal_rmse = float(calibrated_metrics["rmse_c"])
        uniform_mae = float(uniform_metrics["mae_c"])
        uniform_rmse = float(uniform_metrics["rmse_c"])
        if cal_mae < raw_mae:
            ensemble_improved += 1
        elif cal_mae > raw_mae:
            ensemble_worsened += 1
        else:
            ensemble_tied += 1
        if cal_mae < uniform_mae - 1e-12:
            selection_improved += 1
        elif cal_mae > uniform_mae + 1e-12:
            selection_worsened += 1
        else:
            selection_tied += 1
        ensemble_total_raw_mae += raw_mae * len(holdout_dates)
        ensemble_total_cal_mae += cal_mae * len(holdout_dates)
        ensemble_total_raw_rmse_sq += raw_rmse * raw_rmse * len(holdout_dates)
        ensemble_total_cal_rmse_sq += cal_rmse * cal_rmse * len(holdout_dates)
        ensemble_total_uniform_mae += uniform_mae * len(holdout_dates)
        ensemble_total_uniform_rmse_sq += uniform_rmse * uniform_rmse * len(holdout_dates)
        ensemble_holdout_n += len(holdout_dates)
        ensemble_report_rows.append({
            "scope": scope_label,
            "selected_models": list(selected_models),
            "selected_model_set_hash": selected_model_mask,
            "train_n": train_n,
            "holdout_n": len(holdout_dates),
            "train_dates": train_dates,
            "holdout_dates": holdout_dates,
            "weights": normalized_weights,
            "raw_mae_c": raw_mae,
            "calibrated_mae_c": cal_mae,
            "mae_delta_c": cal_mae - raw_mae,
            "raw_rmse_c": raw_rmse,
            "calibrated_rmse_c": cal_rmse,
            "uniform_candidate_mask_mae_c": uniform_mae,
            "uniform_candidate_mask_rmse_c": uniform_rmse,
            "selection_mae_improvement_vs_uniform_c": uniform_mae - cal_mae,
            "selection_rmse_improvement_vs_uniform_c": uniform_rmse - cal_rmse,
            "probability_oos": {
                key: selected_probability_metrics[key]
                for key in ("n", "brier_score", "nll", "crps")
            },
            "uniform_candidate_mask_probability_oos": {
                key: uniform_probability_metrics[key]
                for key in ("n", "brier_score", "nll", "crps")
            },
            "probability_non_degradation_vs_uniform": probability_non_degradation,
            "selection_evidence_sha256": selection_sha,
        })

    if total_holdout_n <= 0:
        raise ContractError("calibration_oos_holdout_empty")
    aggregate = {
        "holdout_n": total_holdout_n,
        "raw_mae_c": total_raw_mae / total_holdout_n,
        "calibrated_mae_c": total_cal_mae / total_holdout_n,
        "mae_improvement_c": (total_raw_mae - total_cal_mae) / total_holdout_n,
        "raw_rmse_c": math.sqrt(total_raw_rmse_sq / total_holdout_n),
        "calibrated_rmse_c": math.sqrt(total_cal_rmse_sq / total_holdout_n),
        "rmse_improvement_c": math.sqrt(total_raw_rmse_sq / total_holdout_n) - math.sqrt(total_cal_rmse_sq / total_holdout_n),
    }
    ensemble_aggregate = {
        "holdout_n": ensemble_holdout_n,
        "raw_mae_c": ensemble_total_raw_mae / ensemble_holdout_n if ensemble_holdout_n else None,
        "calibrated_mae_c": ensemble_total_cal_mae / ensemble_holdout_n if ensemble_holdout_n else None,
        "mae_improvement_c": (
            (ensemble_total_raw_mae - ensemble_total_cal_mae) / ensemble_holdout_n
            if ensemble_holdout_n else None
        ),
        "raw_rmse_c": math.sqrt(ensemble_total_raw_rmse_sq / ensemble_holdout_n) if ensemble_holdout_n else None,
        "calibrated_rmse_c": math.sqrt(ensemble_total_cal_rmse_sq / ensemble_holdout_n) if ensemble_holdout_n else None,
        "rmse_improvement_c": (
            math.sqrt(ensemble_total_raw_rmse_sq / ensemble_holdout_n)
            - math.sqrt(ensemble_total_cal_rmse_sq / ensemble_holdout_n)
            if ensemble_holdout_n else None
        ),
        "uniform_candidate_mask_mae_c": (
            ensemble_total_uniform_mae / ensemble_holdout_n
            if ensemble_holdout_n else None
        ),
        "uniform_candidate_mask_rmse_c": (
            math.sqrt(ensemble_total_uniform_rmse_sq / ensemble_holdout_n)
            if ensemble_holdout_n else None
        ),
        "selection_mae_improvement_vs_uniform_c": (
            (ensemble_total_uniform_mae - ensemble_total_cal_mae) / ensemble_holdout_n
            if ensemble_holdout_n else None
        ),
        "selection_rmse_improvement_vs_uniform_c": (
            math.sqrt(ensemble_total_uniform_rmse_sq / ensemble_holdout_n)
            - math.sqrt(ensemble_total_cal_rmse_sq / ensemble_holdout_n)
            if ensemble_holdout_n else None
        ),
    }
    ensemble_artifacts = [
        item for item in (payload.get("ensemble_artifacts") or []) if isinstance(item, Mapping)
    ]
    ensemble_oos_complete = (
        bool(ensemble_artifacts)
        and len(ensemble_report_rows) == len(ensemble_artifacts)
        and not ensemble_chronology_failures
        and not ensemble_metric_failures
    )
    ensemble_probability_oos_complete = (
        ensemble_oos_complete
        and probability_scope_passed == len(ensemble_artifacts)
        and probability_scope_failed == 0
    )
    checks = {
        "input_receipt_valid": True,
        "explicit_formal_truth_scope": True,
        "all_candidate_scopes_reported": report_scope_set == artifact_scope_set and len(report_rows) == len(artifacts),
        "all_reports_have_positive_holdout": all(int(item.get("holdout_n") or 0) > 0 for item in report_rows),
        "chronological_holdout_no_leakage": not chronology_failures,
        "metric_rows_consistent": not metric_failures,
        "aggregate_mae_improves": aggregate["mae_improvement_c"] > 0.0,
        "aggregate_rmse_improves": aggregate["rmse_improvement_c"] > 0.0,
        "all_scope_mae_non_degradation": worsened == 0,
        "ensemble_oos_metrics_materialized": ensemble_oos_complete,
        "ensemble_selection_non_degradation_vs_uniform": selection_worsened == 0,
        "ensemble_probability_oos_materialized": ensemble_probability_oos_complete,
        "native_historical_arrival_complete": bool((payload.get("checks") or {}).get("published_receive_timestamps_complete") is True),
    }
    blockers = []
    if chronology_failures:
        blockers.append("oos_chronology_incomplete")
    if metric_failures:
        blockers.append("oos_metric_rows_invalid")
    if not checks["all_scope_mae_non_degradation"]:
        blockers.append("oos_scope_non_degradation_failed")
    if not checks["ensemble_oos_metrics_materialized"]:
        blockers.append("ensemble_oos_metrics_not_materialized")
    if not checks["ensemble_selection_non_degradation_vs_uniform"]:
        blockers.append("ensemble_selection_scope_non_degradation_failed")
    if not checks["ensemble_probability_oos_materialized"]:
        blockers.append("ensemble_probability_oos_gate_failed")
    if not checks["native_historical_arrival_complete"]:
        blockers.append("native_historical_arrival_receipts_incomplete")
    identity = {
        "schema": SCHEMA,
        "as_of_utc": str(payload.get("as_of_utc") or ""),
        "target_timezone": str(payload.get("target_timezone") or ""),
        "scope": scope,
        "input_artifact": {"path": str(input_path.expanduser().resolve()), "receipt_sha256": str(payload.get("receipt_sha256") or "")},
        "method": {
            "split": "per_station_model_season_chronological_80_20",
            "training_rows_precede_holdout": True,
            "uses_historical_replay_only": True,
            "does_not_infer_native_arrival": True,
        },
        "counts": {
            "station_count": len(stations),
            "artifact_scope_count": len(artifacts),
            "oos_report_count": len(report_rows),
            "holdout_observation_count": total_holdout_n,
            "improved_scope_count": improved,
            "worsened_scope_count": worsened,
            "tied_scope_count": tied,
            "ensemble_artifact_scope_count": len(ensemble_artifacts),
            "ensemble_oos_scope_count": len(ensemble_report_rows),
            "ensemble_improved_scope_count": ensemble_improved,
            "ensemble_worsened_scope_count": ensemble_worsened,
            "ensemble_tied_scope_count": ensemble_tied,
            "selection_improved_vs_uniform_scope_count": selection_improved,
            "selection_worsened_vs_uniform_scope_count": selection_worsened,
            "selection_tied_vs_uniform_scope_count": selection_tied,
            "probability_oos_passed_scope_count": probability_scope_passed,
            "probability_oos_failed_scope_count": probability_scope_failed,
        },
        "aggregate_metrics": aggregate,
        "ensemble_aggregate_metrics": ensemble_aggregate,
        "checks": checks,
        "chronology_failures": chronology_failures[:100],
        "metric_failures": metric_failures[:100],
        "ensemble_chronology_failures": ensemble_chronology_failures[:100],
        "ensemble_metric_failures": ensemble_metric_failures[:100],
        "promotion_gate": {
            "evaluated": not chronology_failures and not metric_failures,
            "passed": not blockers,
            "blockers": blockers,
            "production_calibration_allowed": False,
        },
        "boundary": {
            "offline_research_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    identity["report_rows"] = report_rows
    identity["ensemble_report_rows"] = ensemble_report_rows
    digest = content_sha256(identity)
    return {**identity, "receipt_sha256": content_sha256({**identity, "evidence_sha256": digest}), "evidence_sha256": digest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        input_path = args.input.expanduser().resolve()
        result = evaluate(_load_input(input_path), input_path=input_path)
        write_bounded_json(args.output.expanduser().resolve(), result)
        print({"schema": result["schema"], "output": str(args.output.expanduser().resolve()), "oos_evaluation_complete": result["promotion_gate"]["evaluated"], "promotion_gate_passed": result["promotion_gate"]["passed"], "blockers": result["promotion_gate"]["blockers"]})
        return 0 if result["promotion_gate"]["evaluated"] else 2
    except (OSError, TypeError, ValueError, ContractError) as exc:
        print({"schema": SCHEMA, "state": "BLOCKED", "reason": f"{type(exc).__name__}:{exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
