import importlib.util
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "train_temperature_path_calibration.py"
SPEC = importlib.util.spec_from_file_location("train_temperature_path_calibration", SCRIPT)
trainer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(trainer)


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT,
            station_id TEXT,
            event_date TEXT,
            tz TEXT,
            settlement_actual REAL,
            market_closed INTEGER,
            payload_json TEXT,
            created_at TEXT
        )
        """
    )
    return conn


def _insert(conn: sqlite3.Connection, *, actual=31.0, payload=None, created_at="2026-06-21T05:00:00+00:00", event_date="2026-06-21"):
    conn.execute(
        """
        INSERT INTO event_runs (
            city, station_id, event_date, tz, settlement_actual, market_closed, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Chengdu",
            "ZUUU",
            event_date,
            "Asia/Shanghai",
            actual,
            1,
            json.dumps(payload or {"weather_likelihood_features": {"forecast_peak_temp_c": 30.0}}),
            created_at,
        ),
    )


def test_training_samples_exclude_post_event_and_missing_peak(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _make_db(db_path)
    _insert(conn, actual=31.0, payload={"weather_likelihood_features": {"forecast_peak_temp_c": 30.0}})
    _insert(
        conn,
        actual=33.0,
        payload={"weather_likelihood_features": {"forecast_peak_temp_c": 20.0}},
        created_at="2026-06-22T16:00:00+00:00",
    )
    _insert(conn, actual=29.0, payload={"weather_likelihood_features": {}})
    conn.commit()
    conn.close()

    samples = list(trainer.iter_training_samples(db_path, city="Chengdu", station="ZUUU"))

    assert len(samples) == 1
    assert samples[0]["peak_mu_source"] == "forecast_peak_temp_c"
    assert samples[0]["predicted_peak_mu_c"] == 30.0
    assert samples[0]["settlement_actual_c"] == 31.0
    assert samples[0]["residual_c"] == 1.0
    assert round(samples[0]["hours_to_event_end"], 3) == 11.0


def test_build_calibration_schema_and_bucket_stats(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _make_db(db_path)
    _insert(conn, actual=31.0, payload={"weather_likelihood_features": {"forecast_peak_temp_c": 30.0}})
    _insert(conn, actual=30.0, payload={"weather_likelihood_features": {"forecast_peak_temp_c": 31.0}})
    _insert(
        conn,
        actual=32.0,
        payload={"weather_likelihood_features": {"peak_estimator_mu_c": 31.5}},
        created_at="2026-06-21T08:00:00+00:00",
    )
    conn.commit()
    conn.close()

    samples = list(trainer.iter_training_samples(db_path))
    calibration = trainer.build_calibration(samples, min_sample_n=2)

    assert calibration["version"] == "temperature_path_error_calibration_v1"
    assert calibration["mode"] == "candidate_read_only"
    assert calibration["sample_n"] == 3
    assert calibration["min_sample_n"] == 2
    assert calibration["default_sigma_c"] > 0
    assert "generated_at" in calibration
    forecast_bucket = next(
        row for row in calibration["buckets"]
        if row["peak_mu_source"] == "forecast_peak_temp_c" and row["local_hour_min"] == 10.0
    )
    assert forecast_bucket["sample_n"] == 2
    assert forecast_bucket["mae_c"] == 1.0
    assert forecast_bucket["rmse_c"] == 1.0
    assert forecast_bucket["sigma_c"] == 1.0
    assert forecast_bucket["usable"] is True
    assert calibration["by_peak_mu_source"]["forecast_peak_temp_c"]["sample_n"] == 2
    report = calibration["coverage_report"]
    assert report["sample_n"] == 3
    assert report["near_close_sample_n"] == 0
    assert report["early_sample_n"] == 2
    assert report["hours_to_event_end_buckets"][2]["hours_to_event_end_min"] == 8.0
    assert report["hours_to_event_end_buckets"][1]["sample_n"] == 1
    assert report["hours_to_event_end_buckets"][2]["sample_n"] == 2
    assert report["by_peak_mu_source_hours_to_event_end"]["forecast_peak_temp_c"][2]["sample_n"] == 2


def test_coverage_report_flags_near_close_dominated_samples():
    samples = [
        {
            "station_id": "ZUUU",
            "peak_mu_source": "peak_estimator_mu_c",
            "local_hour": 22.5,
            "hours_to_event_end": 1.5,
            "residual_c": 0.1,
        },
        {
            "station_id": "ZUUU",
            "peak_mu_source": "peak_estimator_mu_c",
            "local_hour": 23.0,
            "hours_to_event_end": 0.9,
            "residual_c": 0.2,
        },
        {
            "station_id": "ZUUU",
            "peak_mu_source": "forecast_peak_temp_c",
            "local_hour": 11.0,
            "hours_to_event_end": 12.0,
            "residual_c": 1.0,
        },
    ]

    calibration = trainer.build_calibration(samples, min_sample_n=2)
    report = calibration["coverage_report"]

    assert report["near_close_sample_n"] == 2
    assert report["near_close_warning"] is True
    assert report["hours_to_event_end_buckets"][0]["sample_n"] == 2
    assert report["hours_to_event_end_buckets"][2]["sample_n"] == 1
    assert report["by_peak_mu_source_hours_to_event_end"]["peak_estimator_mu_c"][0]["usable"] is True
    assert report["by_peak_mu_source_hours_to_event_end"]["peak_estimator_mu_c"][0]["deployable_candidate"] is False
    assert (
        report["by_peak_mu_source_hours_to_event_end"]["peak_estimator_mu_c"][0]["deploy_block_reason"]
        == "sigma_below_conservative_floor"
    )


def test_main_refuses_official_write_without_explicit_allow(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _make_db(db_path)
    _insert(conn)
    conn.commit()
    conn.close()

    code = trainer.main([
        "--db",
        str(db_path),
        "--output",
        str(trainer.OFFICIAL_CALIBRATION_PATH),
    ])

    assert code == 2
