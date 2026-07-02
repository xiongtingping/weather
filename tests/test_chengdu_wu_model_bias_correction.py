import math
import json
import sqlite3

from scripts.chengdu_wu_model_bias_correction import (
    collect_d0_afternoon_high_temp_underestimation,
    extract_wu_daily_summary_from_html,
)


def test_extract_wu_daily_summary_from_app_root_state() -> None:
    html = """
    <html><body>
    <script id="app-root-state" type="application/json">
    {
      "2616226211": {
        "b": {
          "validTimeLocal": ["2026-06-08T07:00:00+0800", "2026-06-07T07:00:00+0800"],
          "temperatureMax": [80, 71],
          "temperatureMin": [62, 60]
        },
        "h": {},
        "s": 200,
        "st": "OK",
        "u": "https://api.weather.com/v3/wx/conditions/historical/dailysummary/30day?apiKey=x&icaoCode=ZUUU&units=e&language=EN&format=json",
        "rt": "json"
      }
    }
    </script>
    </body></html>
    """

    parsed = extract_wu_daily_summary_from_html(html)

    assert parsed["ok"] is True
    assert parsed["row_count"] == 2
    assert parsed["unit_raw"] == "F"
    assert parsed["rows"][0]["event_date"] == "2026-06-08"
    assert math.isclose(parsed["rows"][0]["temperature_max_c"], 26.666666666666668)


def test_extract_wu_daily_summary_reports_missing_state() -> None:
    parsed = extract_wu_daily_summary_from_html("<html></html>")

    assert parsed["ok"] is False
    assert parsed["reason"] == "app_root_state_missing"
    assert parsed["rows"] == []


def test_collect_d0_afternoon_high_temp_underestimation_flags_today_like_low_peak() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            fact_as_of TEXT,
            created_at TEXT,
            fact_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            run_date TEXT,
            created_at TEXT,
            payload_json TEXT
        )
        """
    )
    old_payload = {
        "weather_likelihood_features_compact": {
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "weather_com_forecast_peak_c": 31.2,
            "wu_forecast_peak_c": 30.8,
        }
    }
    latest_payload = {
        "weather_likelihood_features_compact": {
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "weather_com_forecast_peak_c": 31.2,
            "twc_forecast_peak_c": 31.0,
            "wu_forecast_peak_c": 30.9,
            "temperature_momentum_realization_state": "sustained_warming",
        }
    }
    conn.executemany(
        """
        INSERT INTO event_fact_history
        (id, run_date, event_date, slug, fact_as_of, created_at, fact_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                1,
                "2026-06-22",
                "2026-06-22",
                "highest-temperature-in-chengdu-on-june-22-2026",
                "2026-06-22T05:00:00+00:00",
                "2026-06-22 13:00:00",
                json.dumps(old_payload),
            ),
            (
                2,
                "2026-06-22",
                "2026-06-22",
                "highest-temperature-in-chengdu-on-june-22-2026",
                "2026-06-22T08:00:00+00:00",
                "2026-06-22 16:00:00",
                json.dumps(latest_payload),
            ),
        ],
    )
    conn.execute(
        "INSERT INTO event_runs (slug, event_date) VALUES (?, ?)",
        ("highest-temperature-in-chengdu-on-june-22-2026", "2026-06-22"),
    )
    report = collect_d0_afternoon_high_temp_underestimation(
        conn,
        start_date="2026-06-22",
        end_date="2026-06-22",
        actuals={},
        threshold_c=1.5,
        min_observed_c=30.0,
    )

    assert report["underestimated_count"] == 1
    assert report["checked_stage_snapshots"] == 2
    row = report["rows"][0]
    assert row["snapshot_id"] == 2
    assert row["afternoon_stage"] == "late_16_18"
    assert row["high_temp_day_tier"] == "extreme_33_plus"
    assert row["reference_actual_or_observed_max_c"] == 33.0
    assert row["settlement_actual_available"] is False
    assert row["soft_peak_max_c"] == 31.2
    assert math.isclose(row["conservative_all_source_gap_c"], 1.8)
    assert math.isclose(row["underestimate_gap_c"], 2.1)
    assert row["underestimated_sources"] == {
        "twc_forecast": 2.0,
        "weather_com": 1.8,
        "wu_forecast": 2.1,
    }
    assert row["temperature_momentum_state"] == "sustained_warming"
    assert report["strata"]["by_afternoon_stage"]["late_16_18"]["underestimated_count"] == 1
    assert report["strata"]["by_high_temp_day_tier"]["extreme_33_plus"]["underestimated_count"] == 1
    assert report["strata"]["by_soft_source"]["wu_forecast"]["checked_count"] == 2
    assert report["strata"]["by_soft_source"]["wu_forecast"]["underestimated_count"] == 1
    assert report["shadow_parameter_gate"]["status"] == "insufficient_samples"


def test_collect_d0_afternoon_high_temp_underestimation_gate_marks_stable_source_candidate() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            fact_as_of TEXT,
            created_at TEXT,
            fact_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            run_date TEXT,
            created_at TEXT,
            payload_json TEXT
        )
        """
    )
    rows = []
    row_id = 1
    for day in range(1, 11):
        event_date = f"2026-06-{day:02d}"
        slug = f"highest-temperature-in-chengdu-on-june-{day}-2026"
        conn.execute("INSERT INTO event_runs (slug, event_date) VALUES (?, ?)", (slug, event_date))
        for hour in (4, 6, 8):
            payload = {
                "weather_likelihood_features_compact": {
                    "observed_today_max_c": 33.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "weather_com_forecast_peak_c": 32.6,
                    "wu_forecast_peak_c": 31.0,
                    "temperature_momentum_realization_state": "sustained_warming",
                }
            }
            rows.append(
                (
                    row_id,
                    event_date,
                    event_date,
                    slug,
                    f"{event_date}T{hour:02d}:30:00+00:00",
                    f"{event_date} {hour + 8:02d}:30:00",
                    json.dumps(payload),
                )
            )
            row_id += 1
    conn.executemany(
        """
        INSERT INTO event_fact_history
        (id, run_date, event_date, slug, fact_as_of, created_at, fact_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    report = collect_d0_afternoon_high_temp_underestimation(
        conn,
        start_date="2026-06-01",
        end_date="2026-06-10",
        actuals={},
        threshold_c=1.5,
        min_observed_c=30.0,
    )

    gate = report["shadow_parameter_gate"]
    assert report["checked_stage_snapshots"] == 30
    assert gate["status"] == "dry_run_shadow_candidate_requires_confirmation"
    assert gate["runtime_probability_write"] is False
    assert gate["stable_sources"]["wu_forecast"]["checked_count"] == 30
    assert gate["stable_sources"]["wu_forecast"]["candidate_peak_uplift_c"] == 2.0


def test_collect_d0_afternoon_high_temp_underestimation_falls_back_to_event_runs_payload() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            fact_as_of TEXT,
            created_at TEXT,
            fact_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            run_date TEXT,
            created_at TEXT,
            payload_json TEXT
        )
        """
    )
    payload = {
        "weather_likelihood_features_compact": {
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "wu_forecast_peak_c": 31.0,
        }
    }
    conn.execute(
        """
        INSERT INTO event_runs (id, slug, event_date, run_date, created_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "highest-temperature-in-chengdu-on-june-22-2026",
            "2026-06-22",
            "2026-06-22",
            "2026-06-22T08:30:00+00:00",
            json.dumps(payload),
        ),
    )

    report = collect_d0_afternoon_high_temp_underestimation(
        conn,
        start_date="2026-06-22",
        end_date="2026-06-22",
        actuals={},
        threshold_c=1.5,
        min_observed_c=30.0,
    )

    assert report["checked_stage_snapshots"] == 1
    assert report["snapshot_source_counts"] == {"event_runs_payload_json": 1}
    assert report["rows"][0]["snapshot_source"] == "event_runs_payload_json"
    assert report["rows"][0]["underestimated_sources"] == {"wu_forecast": 2.0}
