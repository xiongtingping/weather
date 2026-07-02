import sqlite3
from argparse import Namespace
from pathlib import Path

from scripts.calibrate_bayes_channel_weights_walk_forward import _rps, build_report


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            city TEXT,
            station_id TEXT,
            resolution_source TEXT,
            rounding_rule TEXT,
            market_var TEXT,
            market_unit TEXT,
            settlement_actual REAL,
            payload_json TEXT,
            created_at TEXT,
            event_end_utc TEXT,
            market_closed INTEGER,
            tz TEXT,
            lon REAL
        );
        CREATE TABLE event_daily_temps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            title TEXT,
            city TEXT,
            station_id TEXT,
            resolution_source TEXT,
            market_var TEXT,
            market_unit TEXT,
            settlement_actual REAL,
            created_at TEXT
        );
        """
    )
    payload_hit_obs = """
    {
      "market_var": "temperature_2m_max",
      "market_unit": "C",
      "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
      "rows": [
        {"outcome": "29°C", "q_raw_static": 0.8, "q_observed_nowcast": 0.1, "q_bayes_physical_likelihood_prob": 0.8},
        {"outcome": "30°C", "q_raw_static": 0.2, "q_observed_nowcast": 0.9, "q_bayes_physical_likelihood_prob": 0.2}
      ]
    }
    """
    payload_miss_obs = """
    {
      "market_var": "temperature_2m_max",
      "market_unit": "C",
      "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
      "rows": [
        {"outcome": "29°C", "q_raw_static": 0.8, "q_observed_nowcast": 0.1, "q_bayes_physical_likelihood_prob": 0.8},
        {"outcome": "30°C", "q_raw_static": 0.2, "q_observed_nowcast": 0.9, "q_bayes_physical_likelihood_prob": 0.2}
      ]
    }
    """
    rows = [
        ("2026-06-01", "highest-temperature-in-chengdu-on-june-1-2026", payload_hit_obs, "2026-06-01T00:00:00+00:00"),
        ("2026-06-02", "highest-temperature-in-chengdu-on-june-2-2026", payload_miss_obs, "2026-06-01T12:00:00+00:00"),
        ("2026-06-03", "highest-temperature-in-chengdu-on-june-3-2026", payload_miss_obs, "2026-06-02T01:00:00+00:00"),
    ]
    for event_date, slug, payload, created_at in rows:
        conn.execute(
            """
            INSERT INTO event_runs(
              run_date, event_date, slug, city, station_id, resolution_source,
              rounding_rule, market_var, market_unit, settlement_actual, payload_json,
              created_at, event_end_utc, market_closed, tz, lon
            ) VALUES (?, ?, ?, 'chengdu', 'ZUUU',
              'https://www.wunderground.com/history/daily/cn/chengdu/ZUUU',
              'nearest', 'temperature_2m_max', 'C', NULL, ?, ?, NULL, 0,
              'Asia/Shanghai', 104.0)
            """,
            (event_date, event_date, slug, payload, created_at),
        )
    actuals = [
        ("2026-06-01", "highest-temperature-in-chengdu-on-june-1-2026", 30.0, "2026-06-02T00:00:00+00:00"),
        ("2026-06-02", "highest-temperature-in-chengdu-on-june-2-2026", 29.0, "2026-06-03T00:00:00+00:00"),
        ("2026-06-03", "highest-temperature-in-chengdu-on-june-3-2026", 29.0, "2026-06-04T00:00:00+00:00"),
    ]
    for event_date, slug, actual, created_at in actuals:
        conn.execute(
            """
            INSERT INTO event_daily_temps(
              run_date, event_date, slug, city, station_id, resolution_source,
              market_var, market_unit, settlement_actual, created_at
            ) VALUES (?, ?, ?, 'chengdu', 'ZUUU',
              'https://www.wunderground.com/history/daily/cn/chengdu/ZUUU',
              'temperature_2m_max', 'C', ?, ?)
            """,
            (event_date, event_date, slug, actual, created_at),
        )
    conn.commit()
    conn.close()


def _args(db_path: Path, out_path: Path) -> Namespace:
    return Namespace(
        db=db_path,
        out=out_path,
        days=10000,
        include_current_day=True,
        city="chengdu",
        station_id="ZUUU",
        snapshot_sources="event_runs",
        baseline_keys="q_raw_static,q_model_round,q_raw,q_round",
        peak_hour=16.0,
        thin_minutes=0.0,
        base_strength=1.0,
        wu_grid="0,4",
        taf_grid="0,4",
        obs_grid="0,4",
        phys_grid="0,4",
        min_samples=1,
        min_market_days=1,
        min_train_samples=1,
        min_train_market_days=1,
        min_channel_samples=1,
        min_nonzero_selected=1,
        min_phys_d0_candidate_samples=30,
        min_phys_d0_candidate_market_days=5,
        max_metric_regression=0.0,
        allow_post_event_snapshots=False,
        allow_merged_settlement_snapshots=False,
        strict_exit=False,
    )


def test_rps_uses_one_hot_cdf_not_cumulative_counts() -> None:
    assert _rps([0.0, 1.0, 0.0], [False, True, False]) == 0.0
    assert 0.0 < _rps([1.0, 0.0, 0.0], [False, True, False]) <= 1.0


def test_walk_forward_trains_only_on_labels_visible_before_created_at(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path)
    report = build_report(_args(db_path, tmp_path / "report.json"))

    samples = {row["event_date"]: row for row in report["recent_evaluated_samples"]}
    assert samples["2026-06-02"]["train_sample_count"] == 0
    assert samples["2026-06-03"]["train_sample_count"] == 1


def test_uncovered_wu_and_taf_channels_stay_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path)
    report = build_report(_args(db_path, tmp_path / "report.json"))

    assert report["channel_coverage"]["wu_fcst"] == 0
    assert report["channel_coverage"]["taf"] == 0
    assert report["recommendations"]["S_wu_fcst"]["recommended_weight"] == 0.0
    assert report["recommendations"]["S_taf"]["recommended_weight"] == 0.0
    assert "channel_sample_count_below_min" in report["recommendations"]["S_wu_fcst"]["reasons"]


def test_s_phys_quality_audit_rejects_legacy_empty_feature_snapshots(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path)
    report = build_report(_args(db_path, tmp_path / "report.json"))

    audit = report["s_phys_quality_audit"]
    assert audit["sample_counts"]["all"] >= 1
    assert audit["sample_counts"]["candidate"] == 0
    assert audit["rejected_reason_counts"]["weather_likelihood_refreshed_at_missing"] >= 1
    assert audit["rejected_reason_counts"]["weather_likelihood_features_empty"] >= 1
    gate = audit["d0_candidate_activation_gate"]
    assert gate["activated"] is False
    assert gate["recommended_weight"] == 0.0
    assert "phys_candidate_sample_count_below_min" in gate["reasons"]


def test_nested_wu_and_taf_forecast_fields_count_as_channel_coverage(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path)
    conn = sqlite3.connect(db_path)
    payload = """
    {
      "market_var": "temperature_2m_max",
      "market_unit": "C",
      "rounding_rule": "whole_degrees",
      "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
      "wu_forecast_peak": {"ok": true, "value_c": 32.0, "source": "wunderground_forecast_daily_peak"},
      "taf_forecast_peak": {"ok": true, "value_c": 32.0, "source": "zuuu_taf_awc_tx_peak"},
      "rows": [
        {"outcome": "30°C or below", "q_raw_static": 0.5},
        {"outcome": "31°C", "q_raw_static": 0.3},
        {"outcome": "32°C", "q_raw_static": 0.2}
      ]
    }
    """
    conn.execute(
        """
        INSERT INTO event_runs(
          run_date, event_date, slug, city, station_id, resolution_source,
          rounding_rule, market_var, market_unit, settlement_actual, payload_json,
          created_at, event_end_utc, market_closed, tz, lon
        ) VALUES ('2026-06-03', '2026-06-04',
          'highest-temperature-in-chengdu-on-june-4-2026', 'chengdu', 'ZUUU',
          'https://www.wunderground.com/history/daily/cn/chengdu/ZUUU',
          'whole_degrees', 'temperature_2m_max', 'C', NULL, ?,
          '2026-06-03T10:00:00+00:00', NULL, 0, 'Asia/Shanghai', 104.0)
        """,
        (payload,),
    )
    conn.execute(
        """
        INSERT INTO event_daily_temps(
          run_date, event_date, slug, city, station_id, resolution_source,
          market_var, market_unit, settlement_actual, created_at
        ) VALUES ('2026-06-04', '2026-06-04',
          'highest-temperature-in-chengdu-on-june-4-2026', 'chengdu', 'ZUUU',
          'https://www.wunderground.com/history/daily/cn/chengdu/ZUUU',
          'temperature_2m_max', 'C', 32.0, '2026-06-05T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    report = build_report(_args(db_path, tmp_path / "report.json"))

    assert report["channel_coverage"]["wu_fcst"] >= 1
    assert report["channel_coverage"]["taf"] >= 1
