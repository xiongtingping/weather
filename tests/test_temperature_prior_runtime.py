from __future__ import annotations

import math
import sqlite3

from temperature_prior_runtime import (
    SCHEMA_VERSION,
    build_temperature_prior_distribution,
)


def test_temperature_prior_uses_only_past_settled_history() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_runs(
            id INTEGER PRIMARY KEY,
            event_date TEXT,
            settlement_actual REAL,
            created_at TEXT,
            station_id TEXT,
            city TEXT,
            market_var TEXT,
            market_unit TEXT,
            rounding_rule TEXT
        )
        """
    )
    rows = [
        ("2026-06-20", 29.0, "2026-06-21T00:00:00+00:00"),
        ("2026-06-21", 30.0, "2026-06-22T00:00:00+00:00"),
        ("2026-06-22", 30.0, "2026-06-23T00:00:00+00:00"),
        ("2026-06-23", 31.0, "2026-06-24T00:00:00+00:00"),
        ("2026-06-24", 35.0, "2026-06-24T23:59:00+00:00"),
    ]
    for event_date, actual, created_at in rows:
        conn.execute(
            """
            INSERT INTO event_runs(
                event_date, settlement_actual, created_at, station_id, city,
                market_var, market_unit, rounding_rule
            ) VALUES (?, ?, ?, 'ZUUU', 'Chengdu', 'temperature_2m_max', 'C', 'rounded_degrees')
            """,
            (event_date, actual, created_at),
        )

    result = build_temperature_prior_distribution(
        buckets=["29°C", "30°C", "31°C", "35°C"],
        event_date="2026-06-24",
        as_of="2026-06-24T08:00:00+00:00",
        conn=conn,
        city="Chengdu",
        station_id="ZUUU",
        market_var="temperature_2m_max",
        market_unit="C",
        min_samples=3,
        smoothing=0.0,
    )
    conn.close()

    assert result["schema"] == SCHEMA_VERSION
    assert result["state"] == "available"
    assert result["guards"]["reads_market_prices"] is False
    assert result["guards"]["reads_forecast_sources"] is False
    assert result["guards"]["reads_current_observations"] is False
    prior = result["prior_distribution"]
    assert math.isclose(sum(prior.values()), 1.0, abs_tol=1e-12)
    assert prior["30°C"] == 0.5
    assert prior["35°C"] == 0.0
    assert result["prior_meta"]["sample_count"] == 4
    assert all(row["event_date"] < "2026-06-24" for row in result["prior_meta"]["sample_rows"])


def test_temperature_prior_reads_daily_history_without_rounding_rule_column() -> None:
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
        ("2026-06-20", 29.0),
        ("2026-06-21", 30.0),
        ("2026-06-22", 30.0),
    ]
    for event_date, actual in rows:
        conn.execute(
            """
            INSERT INTO event_daily_temps(
                event_date, settlement_actual, created_at, station_id, city,
                market_var, market_unit
            ) VALUES (?, ?, '2026-06-23T00:00:00+00:00', 'ZUUU', 'Chengdu', 'temperature_2m_max', 'C')
            """,
            (event_date, actual),
        )

    result = build_temperature_prior_distribution(
        buckets=["29°C", "30°C", "31°C"],
        event_date="2026-06-24",
        as_of="2026-06-24T08:00:00+00:00",
        conn=conn,
        city="Chengdu",
        station_id="ZUUU",
        market_var="temperature_2m_max",
        market_unit="C",
        min_samples=2,
        smoothing=0.0,
    )
    conn.close()

    assert result["state"] == "available"
    assert result["prior_meta"]["sample_count"] == 3
    assert {row["source_table"] for row in result["prior_meta"]["sample_rows"]} == {"event_daily_temps"}
    assert math.isclose(result["prior_distribution"]["30°C"], 2 / 3, abs_tol=1e-12)


def test_temperature_prior_shrinks_sparse_date_window_to_broader_history() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_runs(
            id INTEGER PRIMARY KEY,
            event_date TEXT,
            settlement_actual REAL,
            created_at TEXT,
            station_id TEXT,
            city TEXT,
            market_var TEXT,
            market_unit TEXT,
            rounding_rule TEXT
        )
        """
    )
    rows = [
        ("2026-07-02", 35.0),
        ("2026-07-01", 35.0),
        ("2026-06-10", 29.0),
        ("2026-06-11", 29.0),
        ("2026-06-12", 29.0),
        ("2026-06-13", 29.0),
        ("2026-06-14", 29.0),
        ("2026-06-15", 29.0),
    ]
    for event_date, actual in rows:
        conn.execute(
            """
            INSERT INTO event_runs(
                event_date, settlement_actual, created_at, station_id, city,
                market_var, market_unit, rounding_rule
            ) VALUES (?, ?, '2026-07-02T00:00:00+00:00', 'ZUUU', 'Chengdu', 'temperature_2m_max', 'C', 'rounded_degrees')
            """,
            (event_date, actual),
        )

    result = build_temperature_prior_distribution(
        buckets=["29°C", "35°C"],
        event_date="2026-07-03",
        as_of="2026-07-03T00:00:00+00:00",
        conn=conn,
        city="Chengdu",
        station_id="ZUUU",
        date_window_days=2,
        min_samples=6,
        smoothing=0.0,
    )
    conn.close()

    assert result["state"] == "available"
    assert result["prior_meta"]["date_window_weight"] == round(2 / 6, 12)
    assert "date_window_below_min_samples_shrink_to_broader_history" in result["na_reasons"]
    assert math.isclose(result["prior_distribution"]["35°C"], 0.5, abs_tol=1e-12)


def test_temperature_prior_conditions_on_weather_type_history() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_runs(
            id INTEGER PRIMARY KEY,
            event_date TEXT,
            settlement_actual REAL,
            created_at TEXT,
            station_id TEXT,
            city TEXT,
            market_var TEXT,
            market_unit TEXT,
            rounding_rule TEXT,
            weather_type TEXT
        )
        """
    )
    rows = [
        ("2026-06-20", 29.0, "low_cloud_wet_layer"),
        ("2026-06-21", 29.0, "low_cloud_wet_layer"),
        ("2026-06-22", 29.0, "low_cloud_wet_layer"),
        ("2026-06-23", 33.0, "subtropical_high_open"),
        ("2026-06-24", 33.0, "subtropical_high_open"),
        ("2026-06-25", 33.0, "subtropical_high_open"),
    ]
    for event_date, actual, weather_type in rows:
        conn.execute(
            """
            INSERT INTO event_runs(
                event_date, settlement_actual, created_at, station_id, city,
                market_var, market_unit, rounding_rule, weather_type
            ) VALUES (?, ?, '2026-06-26T00:00:00+00:00', 'ZUUU', 'Chengdu', 'temperature_2m_max', 'C', 'rounded_degrees', ?)
            """,
            (event_date, actual, weather_type),
        )

    result = build_temperature_prior_distribution(
        buckets=["29°C", "33°C"],
        event_date="2026-07-03",
        as_of="2026-07-03T00:00:00+00:00",
        conn=conn,
        city="Chengdu",
        station_id="ZUUU",
        date_window_days=30,
        min_samples=3,
        smoothing=0.0,
        weather_type="subtropical_high_open",
    )
    conn.close()

    assert result["prior_distribution"]["33°C"] == 1.0
    trace = result["prior_meta"]["conditioning_trace"]
    assert trace == [
        {
            "condition": "weather_type",
            "value": "subtropical_high_open",
            "state": "applied",
            "weight": 1.0,
            "sample_count": 3,
            "bucket_counts": {"29°C": 0.0, "33°C": 3.0},
        }
    ]


def test_temperature_prior_applies_site_terrain_urban_temperature_bias() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_runs(
            id INTEGER PRIMARY KEY,
            event_date TEXT,
            settlement_actual REAL,
            created_at TEXT,
            station_id TEXT,
            city TEXT,
            market_var TEXT,
            market_unit TEXT,
            rounding_rule TEXT
        )
        """
    )
    for event_date in ("2026-06-20", "2026-06-21", "2026-06-22"):
        conn.execute(
            """
            INSERT INTO event_runs(
                event_date, settlement_actual, created_at, station_id, city,
                market_var, market_unit, rounding_rule
            ) VALUES (?, 30.0, '2026-06-23T00:00:00+00:00', 'ZUUU', 'Chengdu', 'temperature_2m_max', 'C', 'rounded_degrees')
            """,
            (event_date,),
        )

    result = build_temperature_prior_distribution(
        buckets=["30°C", "31°C"],
        event_date="2026-07-03",
        as_of="2026-07-03T00:00:00+00:00",
        conn=conn,
        city="Chengdu",
        station_id="ZUUU",
        date_window_days=30,
        min_samples=3,
        smoothing=0.0,
        site_temperature_bias_c=0.4,
        terrain_temperature_bias_c=0.4,
        urban_heat_island_c=0.2,
    )
    conn.close()

    assert result["prior_distribution"] == {"30°C": 0.0, "31°C": 1.0}
    assert result["prior_meta"]["temperature_bias_c"] == 1.0
    assert result["prior_meta"]["sample_rows"][0]["adjusted_actual_c"] == 31.0
