from __future__ import annotations

import sqlite3

from scripts.temperature_prior_policy_confirmation import _parse_int_list, review_payload


def _conn_with_daily_history() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_daily_temps(
            id INTEGER PRIMARY KEY,
            event_date TEXT,
            settlement_actual REAL,
            created_at TEXT,
            station_id TEXT,
            city TEXT,
            market_var TEXT,
            market_unit TEXT
        )
        """
    )
    rows = [
        ("2026-07-01", 38.0),
        ("2026-07-02", 38.0),
        ("2026-07-03", 38.0),
        ("2026-07-04", 35.0),
    ]
    for event_date, actual in rows:
        conn.execute(
            """
            INSERT INTO event_daily_temps(
                event_date, settlement_actual, created_at, station_id, city,
                market_var, market_unit
            ) VALUES (?, ?, '2026-07-05T00:00:00+00:00', 'ZUUU', 'chengdu', 'temperature_2m_max', 'C')
            """,
            (event_date, actual),
        )
    return conn


def _payload() -> dict:
    labels = ["34°C", "35°C", "36°C", "37°C", "38°C"]
    return {
        "fact_id": 107070,
        "slug": "highest-temperature-in-chengdu-on-july-9-2026",
        "event_date": "2026-07-09",
        "fact_as_of": "2026-07-09T02:31:00+00:00",
        "target_bucket": "37°C",
        "distribution": [
            {
                "bucket": label,
                "probability": {"34°C": 0.23, "35°C": 0.14, "36°C": 0.20, "37°C": 0.25, "38°C": 0.18}[label],
                "prior": 0.2,
                "forecast_weak_likelihood": 1.0,
                "metar_strong_likelihood": 1.0,
                "hard_mask": 1.0,
            }
            for label in labels
        ],
        "layer_contributions": {
            "forecast_weak_likelihood": {
                "beta": 0.4,
                "distribution": {label: 1.0 for label in labels},
            },
            "metar_strong_likelihood": {
                "beta": 0.65,
                "distribution": {label: 1.0 for label in labels},
            },
        },
        "prior_meta": {
            "station_id": "ZUUU",
            "city": "chengdu",
            "market_var": "temperature_2m_max",
            "market_unit": "C",
            "rounding_rule": "rounded_degrees",
            "history_days": 730,
            "date_window_days": 15,
            "min_samples": 3,
            "smoothing": 0.0,
            "sample_count": 33,
            "raw_candidate_count": 33,
            "effective_sample_count": 11,
            "date_window_sample_count": 11,
            "date_window_weight": 1.0,
        },
        "temperature_trajectory": {
            "state": "platform",
            "gate_state": "not_warming",
            "gate_reason": "trajectory_state_platform",
            "effective_rate_c_per_hour": 0.0,
            "adjusted_rate_c_per_hour": 0.0,
            "rolling_temperature_observation_summary": {
                "point_count": 5,
                "current_temp_c": 34.0,
                "rolling_max_c": 34.0,
            },
        },
    }


def test_parse_int_list_keeps_valid_unique_values() -> None:
    assert _parse_int_list("3,30,bad,30,60") == [3, 30, 60]
    assert _parse_int_list("") == [3, 30, 60]


def test_policy_confirmation_reports_runtime_prior_top_change_and_trajectory_blocker() -> None:
    conn = _conn_with_daily_history()
    try:
        report = review_payload(_payload(), conn=conn, min_samples_values=[3], top_n=3)
    finally:
        conn.close()

    assert report["ok"] is False
    assert "stored_posterior_top3_noncontiguous" in report["blockers"]
    assert "stored_target_not_supported_by_current_trajectory" in report["blockers"]
    assert "runtime_prior_min_samples_3_top_bucket_differs_from_stored_ssot" in report["blockers"]
    assert report["trajectory_assessment"]["assessment"] == "does_not_independently_support_target"
    scenario = report["scenarios"]["runtime_prior_min_samples_3"]
    assert scenario["prior_meta"]["sample_count"] == 4
    assert scenario["prior_meta"]["date_window_sample_count"] == 4
    assert scenario["posterior"]["top_buckets"][0]["bucket"] == "38°C"
    assert scenario["posterior"]["target_rank"]["rank"] == 5
