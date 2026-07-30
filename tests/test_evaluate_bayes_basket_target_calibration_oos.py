from __future__ import annotations

from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256, model_set_hash
from bayes_basket_target.calibration_evidence import _bucket_distribution, _probability_metrics
from scripts.evaluate_bayes_basket_target_calibration_oos import evaluate


def _payload() -> dict[str, object]:
    station = "KAAA"
    model = "ECMWF"
    dates = [f"2026-07-{day:02d}" for day in range(14, 25)]
    model_rows = [
        {
            "model_run_id": f"run-{day}",
            "station_id": station,
            "model_id": model,
            "target_local_date": target_date,
        }
        for day, target_date in enumerate(dates, start=14)
    ]
    residuals = [
        {
            "residual_id": f"residual-{day}",
            "model_run_id": f"run-{day}",
            "station_id": station,
            "model_id": model,
            "season_bin": "summer",
            "qc_status": "ACCEPTED",
            "paired_asof_utc": f"2026-07-{day - 1:02d}T00:00:00Z",
        }
        for day in range(14, 25)
    ]
    payload: dict[str, object] = {
        "schema": "bayes_basket_target_calibration_evidence_v1",
        "as_of_utc": "2026-07-26T00:00:00Z",
        "target_timezone": "America/New_York",
        "scope": {
            "mode": "allowlist",
            "included_station_ids": [station],
            "excluded_source_lanes": ["amsc"],
        },
        "station_registry": [{"station_id": station, "source_lane": "AWC"}],
        "calibration_model_rows": model_rows,
        "residual_evidence": residuals,
        "truth_evidence": [],
        "oos_reports": [{
            "station_id": station,
            "model_id": model,
            "season_bin": "summer",
            "train_n": 8,
            "holdout_n": 3,
            "trained_through_utc": "2026-07-21T00:00:00Z",
            "raw": {"n": 3, "mae_c": 1.0, "rmse_c": 1.2},
            "calibrated": {"n": 3, "mae_c": 0.8, "rmse_c": 1.0},
        }],
        "calibration_artifacts": [{
            "calibration_artifact_id": "cal-1",
            "station_id": station,
            "model_id": model,
            "season_bin": "summer",
            "trained_through_utc": "2026-07-21T00:00:00Z",
        }],
        "checks": {"published_receive_timestamps_complete": False},
    }
    payload["receipt_sha256"] = content_sha256(payload)
    return payload


def test_oos_reconstructs_target_dates_from_model_lineage() -> None:
    result = evaluate(_payload(), input_path=Path("calibration.json"))

    assert result["promotion_gate"]["evaluated"] is True
    assert result["checks"]["chronological_holdout_no_leakage"] is True
    assert result["report_rows"][0]["train_dates"][-1] == "2026-07-21"
    assert result["report_rows"][0]["holdout_dates"] == [
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
    ]
    assert "native_historical_arrival_receipts_incomplete" in result["promotion_gate"]["blockers"]


def test_oos_accepts_explicit_amsc_formal_scope() -> None:
    payload = _payload()
    payload["scope"] = {
        "mode": "allowlist",
        "included_station_ids": ["KAAA"],
        "excluded_source_lanes": ["awc"],
    }
    payload["station_registry"] = [{"station_id": "KAAA", "source_lane": "AMSC"}]

    result = evaluate(payload, input_path=Path("calibration.json"))

    assert result["checks"]["explicit_formal_truth_scope"] is True


def test_oos_rejects_conflicting_residual_and_model_target_dates() -> None:
    payload = _payload()
    residuals = list(payload["residual_evidence"])
    residuals[0] = {**residuals[0], "target_local_date": "2026-07-25"}
    payload["residual_evidence"] = residuals
    with pytest.raises(ContractError, match="residual_model_target_date_mismatch"):
        evaluate(payload, input_path=Path("calibration.json"))


def test_oos_materializes_exact_three_model_ensemble_metrics() -> None:
    payload = _payload()
    station = "KAAA"
    dates = [f"2026-07-{day:02d}" for day in range(14, 25)]
    models = ("ECMWF", "GFS", "ICON")
    payload["calibration_model_rows"] = [
        {
            "model_run_id": f"{model}-run-{day}",
            "station_id": station,
            "model_id": model,
            "target_local_date": target_date,
            "raw_tmax_c": 20.0 + (0.1 if model == "ECMWF" else 0.2 if model == "GFS" else 0.3),
        }
        for model in models
        for day, target_date in enumerate(dates, start=14)
    ]
    payload["residual_evidence"] = [
        {
            "residual_id": f"{model}-residual-{day}",
            "model_run_id": f"{model}-run-{day}",
            "station_id": station,
            "model_id": model,
            "season_bin": "summer",
            "qc_status": "ACCEPTED",
            "paired_asof_utc": f"2026-07-{day - 1:02d}T00:00:00Z",
        }
        for model in models
        for day in range(14, 25)
    ]
    payload["truth_evidence"] = [
        {"truth_id": f"truth-{day}", "station_id": station, "local_date": target_date, "official_tmax_c": 20.0}
        for day, target_date in enumerate(dates, start=14)
    ]
    payload["oos_reports"] = [
        {
            "station_id": station,
            "model_id": model,
            "season_bin": "summer",
            "train_n": 8,
            "holdout_n": 3,
            "trained_through_utc": "2026-07-21T00:00:00Z",
            "raw": {"n": 3, "mae_c": 1.0, "rmse_c": 1.0},
            "calibrated": {"n": 3, "mae_c": 0.8, "rmse_c": 0.8},
        }
        for model in models
    ]
    payload["calibration_artifacts"] = [
        {
            "calibration_artifact_id": f"cal-{model}",
            "station_id": station,
            "model_id": model,
            "season_bin": "summer",
            "trained_through_utc": "2026-07-21T00:00:00Z",
            "bias_c": 0.0,
            "residual_scale_c": 1.0,
        }
        for model in models
    ]
    holdout_dates = dates[8:]
    buckets = tuple(range(13, 29))
    probability_rows = [
        (
            target,
            {
                model: _bucket_distribution(
                    mean=20.0 + (0.1 if model == "ECMWF" else 0.2 if model == "GFS" else 0.3),
                    scale=1.0,
                    buckets=buckets,
                )
                for model in models
            },
            20,
        )
        for target in holdout_dates
    ]
    weights = {model: 1 / 3 for model in models}
    probability_metrics = _probability_metrics(
        probability_rows, weights, recent_dates=set(holdout_dates)
    )
    selection_identity = {
        "selection_evidence_id": "selection-1",
        "station_id": station,
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "method_version": "rolling_origin_probability_stack_v4_five_model_pool_min2",
        "train_start_date": dates[0],
        "train_end_date": dates[7],
        "recent_start_date": dates[5],
        "recent_end_date": dates[7],
        "holdout_start_date": dates[8],
        "holdout_end_date": dates[-1],
        "candidate_scores": [{"models": list(models)}],
        "selected_models": list(models),
        "selected_weights": weights,
        "oos_metrics": {
            key: probability_metrics[key] for key in ("n", "brier_score", "nll", "crps")
        },
        "residual_covariance": {},
        "residual_correlation": {},
        "effective_sample_n": 8.0,
        "previous_ensemble_artifact_sha256": None,
        "max_weight_delta": 0.2,
        "actual_max_weight_delta": 0.0,
    }
    selection_sha = content_sha256(selection_identity)
    payload["ensemble_selection_evidence"] = [{
        **selection_identity,
        "content_sha256": selection_sha,
    }]
    payload["ensemble_artifacts"] = [{
        "ensemble_artifact_id": "ens-1",
        "station_id": station,
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "candidate_models": list(models),
        "supported_masks": {
            model_set_hash(models): weights,
        },
        "selection_evidence_sha256": selection_sha,
    }]

    result = evaluate(payload, input_path=Path("calibration.json"))

    assert result["checks"]["ensemble_oos_metrics_materialized"] is True
    assert result["checks"]["ensemble_probability_oos_materialized"] is True
    assert result["counts"]["ensemble_oos_scope_count"] == 1
    assert result["ensemble_aggregate_metrics"]["holdout_n"] == 3
    assert "ensemble_oos_metrics_not_materialized" not in result["promotion_gate"]["blockers"]
