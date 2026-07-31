from __future__ import annotations

from datetime import date
import json

import pytest

from bayes_basket_target.contracts import ContractError
from scripts.run_bayes_basket_target_station_explicit_18z_oos import (
    _evidence_args,
    _load_aggregator_rows,
    _load_model_rows,
    _point_oos,
)


def test_model_evidence_arguments_group_repeated_models() -> None:
    grouped = _evidence_args([
        "ICON=/tmp/icon-a.json",
        "CMA=/tmp/cma.json",
        "ICON=/tmp/icon-b.json",
    ])
    assert [path.name for path in grouped["ICON"]] == [
        "icon-a.json",
        "icon-b.json",
    ]
    assert grouped["CMA"][0].name == "cma.json"


def test_explicit_aggregator_rows_require_exact_d2_18z(tmp_path) -> None:
    event_date = date(2026, 7, 29)
    payload = {
        "schema": "bayes_basket_target_historical_aggregator_rebuild_evidence_v1",
        "provider_id": "cma_grapes_global_open_meteo",
        "model_id": "CMA",
        "blockers": [],
        "source": {
            "run_cycle": "18Z",
            "run_day_offset": 2,
            "single_run_parameter_explicit": True,
        },
        "model_rows": [{
            "station_id": "CYYZ",
            "event_date": event_date.isoformat(),
            "target_timezone": "America/Toronto",
            "model_run_initialization_utc": "2026-07-27T18:00:00Z",
            "daily_curve_complete": True,
            "sample_count": 24,
            "forecast_daily_max_c": 30.0,
            "receipt_sha256": "b" * 64,
        }, {
            "station_id": "MPMG",
            "event_date": event_date.isoformat(),
            "target_timezone": "America/Panama",
            "model_run_initialization_utc": "2026-07-27T18:00:00Z",
            "daily_curve_complete": True,
            "sample_count": 24,
            "forecast_daily_max_c": 33.0,
            "receipt_sha256": "a" * 64,
        }],
    }
    path = tmp_path / "cma.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    rows, _inputs = _load_aggregator_rows(
        [path],
        model_id="CMA",
        station_id="MPMG",
        target_timezone="America/Panama",
    )
    assert rows[event_date.isoformat()]["model_run_initialization_utc"] == (
        "2026-07-27T18:00:00Z"
    )

    payload["model_rows"][1]["model_run_initialization_utc"] = (
        "2026-07-27T12:00:00Z"
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="model_scope_mismatch"):
        _load_aggregator_rows(
            [path],
            model_id="CMA",
            station_id="MPMG",
            target_timezone="America/Panama",
        )


def test_truth_cli_requires_exactly_one_truth_input() -> None:
    from scripts.run_bayes_basket_target_station_explicit_18z_oos import main

    with pytest.raises(SystemExit):
        main([
            "--station-id", "NZWN",
            "--registry", "/tmp/registry.json",
            "--model-evidence", "ICON=/tmp/icon.json",
            "--model-evidence", "CMA=/tmp/cma.json",
            "--as-of-utc", "2026-07-29T00:00:00Z",
            "--output", "/tmp/oos.json",
        ])


def test_non_gfs_model_loader_preserves_aggregator_lineage(
    tmp_path,
) -> None:
    payload = {
        "schema": "bayes_basket_target_historical_aggregator_rebuild_evidence_v1",
        "provider_id": "icon_global_open_meteo",
        "model_id": "ICON",
        "blockers": [],
        "source": {
            "run_cycle": "18Z",
            "run_day_offset": 2,
            "single_run_parameter_explicit": True,
        },
        "model_rows": [{
            "station_id": "NZWN",
            "event_date": "2026-07-28",
            "target_timezone": "Pacific/Auckland",
            "model_run_initialization_utc": "2026-07-26T18:00:00Z",
            "daily_curve_complete": True,
            "sample_count": 24,
            "forecast_daily_max_c": 12.0,
            "receipt_sha256": "b" * 64,
        }],
    }
    path = tmp_path / "icon.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    rows, _inputs = _load_model_rows(
        [path],
        model_id="ICON",
        station_id="NZWN",
        target_timezone="Pacific/Auckland",
    )
    assert rows["2026-07-28"]["provider_id"] == "icon_global_open_meteo"


def test_native_gem_loader_accepts_real_12z_cycle(tmp_path) -> None:
    payload = {
        "schema": "bayes_basket_target_historical_native_rebuild_evidence_v1",
        "provider_id": "gem_gdps_official",
        "model_id": "GEM",
        "run_cycle": "12Z",
        "run_day_offset": 2,
        "blockers": [],
        "model_rows": [{
            "station_id": "WSSS",
            "event_date": "2026-07-28",
            "target_timezone": "Asia/Singapore",
            "model_run_initialization_utc": "2026-07-26T12:00:00Z",
            "daily_curve_complete": True,
            "sample_count": 24,
            "forecast_daily_max_c": 31.0,
            "receipt_sha256": "c" * 64,
            "model_run_lineage_sha256": "d" * 64,
        }],
    }
    path = tmp_path / "gem.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    rows, _inputs = _load_model_rows(
        [path],
        model_id="GEM",
        station_id="WSSS",
        target_timezone="Asia/Singapore",
        run_cycle="12Z",
    )

    assert rows["2026-07-28"]["provider_id"] == "gem_gdps_official"
    assert rows["2026-07-28"]["source_class"] == (
        "official_native_historical_rebuild"
    )


def test_regional_loader_accepts_closed_d1_exact_run_only(tmp_path) -> None:
    payload = {
        "schema":
            "bayes_basket_target_historical_regional_exact_run_evidence_v1",
        "provider_id": "jma_msm_open_meteo",
        "model_id": "JMA-MSM",
        "blockers": [],
        "source": {
            "run_cycle": "12Z",
            "run_day_offset": 1,
            "single_run_parameter_explicit": True,
        },
        "model_rows": [{
            "station_id": "RKPK",
            "event_date": "2026-07-28",
            "target_timezone": "Asia/Seoul",
            "model_run_initialization_utc": "2026-07-27T12:00:00Z",
            "provider_id": "jma_msm_open_meteo",
            "model_pool_class": "regional",
            "future_data_used": False,
            "daily_curve_complete": True,
            "sample_count": 24,
            "forecast_daily_max_c": 31.0,
            "receipt_sha256": "e" * 64,
        }],
    }
    path = tmp_path / "jma-msm.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    rows, _inputs = _load_model_rows(
        [path],
        model_id="JMA-MSM",
        station_id="RKPK",
        target_timezone="Asia/Seoul",
        run_cycle="12Z",
        lead_days=1,
    )
    assert rows["2026-07-28"]["model_pool_class"] == "regional"
    assert rows["2026-07-28"]["model_run_initialization_utc"] == (
        "2026-07-27T12:00:00Z"
    )

    payload["model_rows"][0]["future_data_used"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="model_scope_mismatch"):
        _load_model_rows(
            [path],
            model_id="JMA-MSM",
            station_id="RKPK",
            target_timezone="Asia/Seoul",
            run_cycle="12Z",
            lead_days=1,
        )


def test_point_oos_requires_both_mae_and_rmse_non_degradation() -> None:
    result = _point_oos(
        oos={
            "holdout_dates": ["2026-07-28"],
            "calibration_artifacts": [
                {"model_id": "ICON", "bias_c": 0.0},
                {"model_id": "CMA", "bias_c": 0.0},
            ],
            "research_winner": {
                "selected_models": ["ICON", "CMA"],
                "selected_weights": {"ICON": 0.7, "CMA": 0.3},
            },
        },
        exact_join_rows=[{
            "event_date": "2026-07-28",
            "models": {
                "ICON": {"forecast_tmax_c": 11.0},
                "CMA": {"forecast_tmax_c": 9.0},
            },
        }],
        truths={"2026-07-28": {"official_tmax_c": 10.0}},
    )

    assert result["selected_mae_c"] == pytest.approx(0.4)
    assert result["uniform_mae_c"] == pytest.approx(0.0)
    assert result["passed_vs_uniform"] is False
