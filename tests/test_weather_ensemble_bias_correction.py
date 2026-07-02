import math
import sqlite3
from datetime import date, timedelta

import numpy as np

from weather_ensemble_bias_correction import (
    LocalAdjustmentConfig,
    QuantileMappingModel,
    apply_seasonal_bias_correction,
    apply_quantile_mapping,
    apply_residual_error_expansion,
    bucket_probabilities_from_members,
    calibrate_members_and_bucket_probabilities,
    extract_open_meteo_member_values,
    fit_quantile_mapping,
    fit_residual_error_models,
    fit_seasonal_bias_correction,
    load_station_temperature_qm_history,
    summarize_qm_training_targets,
)


def _history(days: int, *, model_bias: float = 2.0):
    rows = []
    start = date(2026, 1, 1)
    for idx in range(days):
        actual = 20.0 + idx * 0.1
        rows.append(
            {
                "event_date": (start + timedelta(days=idx)).isoformat(),
                "forecast_values": [actual - model_bias - 0.2, actual - model_bias, actual - model_bias + 0.2],
                "actual": actual,
            }
        )
    return rows


def test_extract_open_meteo_daily_decimal_members() -> None:
    payload = {
        "daily": {
            "temperature_2m_max_member01_ncep_gefs_seamless": [28.125],
            "temperature_2m_max_member02_ncep_gefs_seamless": [28.875],
            "temperature_2m_max_member01_icon_seamless_eps": [30.25],
        }
    }

    values = extract_open_meteo_member_values(
        payload,
        "temperature_2m_max",
        model_suffixes=["ncep_gefs_seamless"],
    )

    np.testing.assert_allclose(values, np.array([28.125, 28.875]))


def test_extract_open_meteo_hourly_temperature_members_to_daily_max() -> None:
    payload = {
        "hourly": {
            "temperature_2m_member01_ncep_gefs_seamless": [22.25, 28.75, 27.1],
            "temperature_2m_member02_ncep_gefs_seamless": [21.5, 29.125, 28.5],
        }
    }

    values = extract_open_meteo_member_values(
        payload,
        "temperature_2m",
        model_suffixes=["ncep_gefs_seamless"],
        aggregation="max",
    )

    np.testing.assert_allclose(values, np.array([28.75, 29.125]))


def test_quantile_mapping_requires_real_sample_floor() -> None:
    qm_models, meta = fit_quantile_mapping({"gfs_seamless": _history(10)}, min_sample_days=60)

    assert qm_models == {}
    assert meta["status"] == "not_enough_samples"
    assert meta["model_meta"]["gfs_seamless"]["sample_days"] == 10
    assert meta["model_meta"]["gfs_seamless"]["min_sample_days"] == 60


def test_quantile_mapping_preserves_member_distribution_not_mean_only() -> None:
    qm_models, fit_meta = fit_quantile_mapping({"gfs_seamless": _history(60)}, min_sample_days=60)
    assert fit_meta["status"] == "ok"

    corrected, apply_meta = apply_quantile_mapping(
        {"gfs_seamless": np.array([23.0, 24.0, 25.0])},
        qm_models,
    )

    assert apply_meta["member_coverage"] == 1.0
    assert corrected["gfs_seamless"].shape == (3,)
    assert corrected["gfs_seamless"][0] < corrected["gfs_seamless"][1] < corrected["gfs_seamless"][2]
    assert np.std(corrected["gfs_seamless"]) > 0


def test_residual_error_expansion_adds_historical_uncertainty() -> None:
    history = {
        "gfs_seamless": [
            {
                "event_date": (date(2026, 1, 1) + timedelta(days=idx)).isoformat(),
                "forecast_values": [24.0 + idx * 0.01, 24.2 + idx * 0.01],
                "actual": 25.0 + idx * 0.01 + ((idx % 5) - 2) * 0.4,
            }
            for idx in range(60)
        ]
    }
    qm_models, qm_meta = fit_quantile_mapping(history, min_sample_days=60)
    assert qm_meta["status"] == "ok"

    residual_models, residual_meta = fit_residual_error_models(
        history,
        qm_models,
        min_residual_days=30,
        residual_window_days=60,
        quantile_count=9,
    )
    assert residual_meta["status"] == "ok"

    expanded, apply_meta = apply_residual_error_expansion(
        {"gfs_seamless": np.array([27.0, 28.0])},
        residual_models,
    )

    assert apply_meta["member_coverage"] == 1.0
    assert expanded["gfs_seamless"].shape == (18,)
    assert np.std(expanded["gfs_seamless"]) > np.std(np.array([27.0, 28.0]))


def test_seasonal_bias_correction_shrinks_month_residual_toward_overall() -> None:
    history = {
        "gfs_seamless": [
            {
                "event_date": (date(2026, 1, 1) + timedelta(days=idx)).isoformat(),
                "forecast_values": [20.0],
                "actual": 20.0,
            }
            for idx in range(40)
        ]
        + [
            {
                "event_date": (date(2026, 6, 1) + timedelta(days=idx)).isoformat(),
                "forecast_values": [20.0],
                "actual": 24.0,
            }
            for idx in range(20)
        ]
    }
    qm_models = {
        "gfs_seamless": QuantileMappingModel(
            model="gfs_seamless",
            forecast_sorted=(0.0, 40.0),
            observed_sorted=(0.0, 40.0),
            sample_days=60,
            forecast_sample_count=60,
            observed_sample_count=60,
            start_date="2026-01-01",
            end_date="2026-06-20",
        )
    }

    seasonal_models, seasonal_meta = fit_seasonal_bias_correction(
        history,
        qm_models,
        min_sample_days=10,
        min_seasonal_sample_days=10,
        shrinkage_days=20,
        max_abs_adjustment=3.0,
    )
    assert seasonal_meta["status"] == "ok"
    june_meta = seasonal_meta["model_meta"]["gfs_seamless"]["month_meta"]["6"]
    assert june_meta["shrinkage_alpha"] == 0.5
    assert 0.0 < june_meta["adjustment"] < june_meta["raw_mean_residual"]

    corrected, apply_meta = apply_seasonal_bias_correction(
        {"gfs_seamless": np.array([20.0])},
        seasonal_models,
        target_event_date="2026-06-15",
        market_unit="C",
    )
    assert apply_meta["member_coverage"] == 1.0
    assert corrected["gfs_seamless"][0] > 20.0
    assert corrected["gfs_seamless"][0] < 23.1


def test_local_corrections_and_integer_bucket_probabilities_sum_to_one() -> None:
    result = calibrate_members_and_bucket_probabilities(
        {"gfs_seamless": np.array([27.2, 28.4, 29.9])},
        {"gfs_seamless": _history(60, model_bias=1.0)},
        ["27°C or below", "28°C", "29°C", "30°C or higher"],
        weights={"gfs_seamless": 1.0},
        rounding_rule="whole_degrees",
        market_unit="C",
        min_sample_days=60,
        local_config=LocalAdjustmentConfig(
            enabled=True,
            lapse_rate_enabled=True,
            station_elevation_m=500.0,
            model_elevation_m=300.0,
            lapse_rate_c_per_m=-0.0065,
            cloud_radiative_enabled=True,
            cloud_cover_pct=25.0,
            night_radiation_cooling_max_c=0.4,
            heat_island_enabled=True,
            urban_heat_island_c=0.6,
        ),
    )

    probs = result["bucket_probabilities"]
    assert math.isclose(sum(probs.values()), 1.0, abs_tol=0.0001)
    assert set(probs) == {"27°C or below", "28°C", "29°C", "30°C or higher"}
    local_meta = result["provenance"]["local_adjustment"]
    assert set(local_meta["components_c"]) == {
        "lapse_rate",
        "cloud_night_radiation",
        "urban_heat_island",
    }
    assert result["provenance"]["bucket_probability"]["rounding_rule"] == "whole_degrees"
    assert result["provenance"]["residual_error"]["fit"]["version"] == "historical_residual_error_expansion_v1"


def test_raw_seasonal_bias_base_disables_residual_expansion_to_avoid_double_correction() -> None:
    result = calibrate_members_and_bucket_probabilities(
        {"gfs_seamless": np.array([20.0, 21.0])},
        {
            "gfs_seamless": [
                {
                    "event_date": (date(2026, 6, 1) + timedelta(days=idx)).isoformat(),
                    "forecast_values": [20.0],
                    "actual": 22.0,
                }
                for idx in range(60)
            ]
        },
        ["20°C", "21°C", "22°C", "23°C"],
        rounding_rule="whole_degrees",
        market_unit="C",
        target_event_date="2026-06-20",
        min_sample_days=30,
        seasonal_correction_enabled=True,
        seasonal_correction_base="raw",
        min_seasonal_sample_days=10,
        residual_expansion_enabled=True,
    )

    seasonal = result["provenance"]["seasonal_bias_correction"]
    residual = result["provenance"]["residual_error"]
    assert seasonal["base"] == "raw"
    assert seasonal["apply"]["applied"] is True
    assert residual["fit"]["status"] == "disabled_for_raw_seasonal_bias_base"


def test_bucket_probabilities_use_floor_settlement_rule() -> None:
    probs, meta = bucket_probabilities_from_members(
        {"gfs_seamless": np.array([27.99, 28.01, 29.0])},
        ["27°C", "28°C", "29°C"],
        rounding_rule="whole_degrees",
    )

    assert probs == {"27°C": 1 / 3, "28°C": 1 / 3, "29°C": 1 / 3}
    assert math.isclose(meta["probability_sum"], 1.0, abs_tol=0.0001)


def test_bucket_probabilities_can_use_source_aligned_rounded_rule() -> None:
    probs, meta = bucket_probabilities_from_members(
        {"gfs_seamless": np.array([27.6, 28.4, 28.6])},
        ["27°C", "28°C", "29°C"],
        rounding_rule="rounded_degrees",
    )

    assert probs == {"27°C": 0.0, "28°C": 2 / 3, "29°C": 1 / 3}
    assert meta["rounding_rule"] == "rounded_degrees"
    assert math.isclose(meta["probability_sum"], 1.0, abs_tol=0.0001)


def test_load_station_temperature_qm_history_uses_wu_twc_target_and_latest_cache(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE event_daily_temps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date TEXT,
                station_id TEXT,
                market_var TEXT,
                market_unit TEXT,
                settlement_actual REAL,
                resolution_source TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE model_member_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT,
                loc_key TEXT,
                event_date TEXT,
                market_var TEXT,
                market_unit TEXT,
                members_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO event_daily_temps(
                event_date, station_id, market_var, market_unit, settlement_actual, resolution_source, created_at
            ) VALUES (?, 'ZUUU', 'temperature_2m_max', 'C', ?, ?, ?)
            """,
            [
                ("2026-05-01", 25.0, "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU", "2026-05-02T00:00:00+00:00"),
                ("2026-05-02", 26.0, "open-meteo-archive", "2026-05-03T00:00:00+00:00"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO model_member_cache(
                model, loc_key, event_date, market_var, market_unit, members_json, created_at
            ) VALUES ('gfs_seamless', '30.5583,103.9460', ?, 'temperature_2m_max', 'C', ?, ?)
            """,
            [
                ("2026-05-01", "[20.0]", "2026-05-01T00:00:00+00:00"),
                ("2026-05-01", "[21.0, 22.0]", "2026-05-01T01:00:00+00:00"),
                ("2026-05-02", "[23.0]", "2026-05-02T00:00:00+00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    history = load_station_temperature_qm_history(
        db_path,
        loc_key="30.5583,103.9460",
        station_id="ZUUU",
    )

    assert list(history) == ["gfs_seamless"]
    assert history["gfs_seamless"] == [
        {
            "event_date": "2026-05-01",
            "forecast_values": [21.0, 22.0],
            "actual": 25.0,
            "actual_source": "wu_twc_final_settlement",
            "actual_source_detail": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
            "actual_table": "event_daily_temps",
            "training_target_source": "wu_twc_final_settlement",
            "training_target_role": "source_aligned_qm_observed_final_settlement",
            "target_weight": 1,
            "target_weight_reason": "",
        }
    ]


def test_load_station_temperature_qm_history_falls_back_to_event_runs_final_settlement(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE event_daily_temps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date TEXT,
                station_id TEXT,
                market_var TEXT,
                market_unit TEXT,
                settlement_actual REAL,
                resolution_source TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date TEXT,
                station_id TEXT,
                market_var TEXT,
                market_unit TEXT,
                settlement_actual REAL,
                resolution_source TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE model_member_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT,
                loc_key TEXT,
                event_date TEXT,
                market_var TEXT,
                market_unit TEXT,
                members_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO event_runs(
                event_date, station_id, market_var, market_unit, settlement_actual, resolution_source, created_at
            ) VALUES (?, 'ZUUU', 'temperature_2m_max', 'C', ?, ?, ?)
            """,
            [
                ("2026-06-18", 31.0, "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU", "2026-06-19T00:00:00+00:00"),
                ("2026-06-22", 33.0, "wu_twc_final_settlement", "2026-06-23T00:00:00+00:00"),
                ("2026-06-23", 29.0, "open-meteo-archive", "2026-06-24T00:00:00+00:00"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO model_member_cache(
                model, loc_key, event_date, market_var, market_unit, members_json, created_at
            ) VALUES ('gfs_seamless', '30.5583,103.9460', ?, 'temperature_2m_max', 'C', ?, ?)
            """,
            [
                ("2026-06-18", "[28.0, 29.0]", "2026-06-18T02:00:00+00:00"),
                ("2026-06-22", "[30.0, 31.0]", "2026-06-22T02:00:00+00:00"),
                ("2026-06-23", "[27.0, 28.0]", "2026-06-23T02:00:00+00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    history = load_station_temperature_qm_history(
        db_path,
        loc_key="30.5583,103.9460",
        station_id="ZUUU",
        end_before="2026-06-25",
        recent_target_window_days=7,
        recent_target_weight=30,
    )
    rows = history["gfs_seamless"]

    assert [row["event_date"] for row in rows] == ["2026-06-18", "2026-06-22"]
    assert [row["actual"] for row in rows] == [31.0, 33.0]
    assert {row["actual_table"] for row in rows} == {"event_runs"}
    assert {row["training_target_source"] for row in rows} == {"wu_twc_final_settlement"}
    assert {row["target_weight"] for row in rows} == {30}
    assert {row["target_weight_reason"] for row in rows} == {"recent_wu_twc_final_settlement_window"}

    summary = summarize_qm_training_targets(history)
    assert summary["sample_days"] == 2
    assert summary["actual_tables"] == {"event_runs": 2}
    assert summary["target_sources"] == {"wu_twc_final_settlement": 2}
    assert summary["target_weight_sum"] == 60
    assert summary["weighted_day_count"] == 2
