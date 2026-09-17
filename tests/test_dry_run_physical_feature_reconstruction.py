import json
import sqlite3

from scripts import dry_run_physical_feature_reconstruction as dry_run


def _row(payload):
    return {
        "payload_json": payload,
        "lat": 30.558257,
        "lon": 103.945966,
        "tz": "Asia/Shanghai",
        "event_date": "2026-06-01",
        "station_id": "ZUUU",
        "slug": "highest-temperature-in-chengdu-on-june-1-2026",
        "city": "ZUUU",
    }


def test_dry_run_default_does_not_fetch_archive(monkeypatch) -> None:
    payload = {
        "market_unit": "C",
        "model_members": {"m1": [31.0, 32.0]},
        "rows": [
            {"outcome": "32°C", "q_raw": 1.0},
        ],
    }

    def fail_if_called(*args, **kwargs):
        raise AssertionError("archive fetch should be opt-in")

    monkeypatch.setattr(dry_run, "_features_from_open_meteo_archive", fail_if_called)

    event = dry_run._analyze_event(
        _row(payload),
        fetch_open_meteo_archive=False,
        timeout=1.0,
    )

    assert event["projected_feature_quality"] == "minimal"
    assert event["source_attempts"]["open_meteo_archive"]["status"] == "not_fetched"


def test_candidate_source_summary_unions_discovery_cache_and_event_runs(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "weather_events_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "updated_at": "2026-06-11T00:00:00+00:00",
                "events": [
                    {
                        "slug": "highest-temperature-in-atlanta-on-june-2-2026",
                        "title": "Highest temperature in Atlanta on June 2?",
                        "endDate": "2026-06-02T12:00:00Z",
                    },
                    {
                        "slug": "rain-in-atlanta-on-june-2-2026",
                        "title": "Rain in Atlanta?",
                        "endDate": "2026-06-02T12:00:00Z",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dry_run, "WEATHER_EVENTS_CACHE_PATHS", (cache_path,))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE weather_market_discovery(
            slug TEXT,
            event_date TEXT,
            title TEXT,
            status TEXT,
            token_count INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO weather_market_discovery(slug, event_date, title, status, token_count)
        VALUES('highest-temperature-in-ankara-on-june-1-2026', '2026-06-01', 'Highest temperature in Ankara on June 1?', 'known_recent', 0)
        """
    )
    event_rows = [
        {
            "slug": "highest-temperature-in-chengdu-on-june-1-2026",
            "event_date": "2026-06-01",
        }
    ]

    summary = dry_run._candidate_source_summary(
        conn,
        "2026-06-01",
        "2026-06-03",
        event_rows,
    )

    assert summary["event_runs_settled_high_temp_days"] == 1
    assert summary["weather_market_discovery_high_temp_days"] == 1
    assert summary["weather_events_cache_high_temp_days"] == 1
    assert summary["combined_high_temp_candidate_days"] == 3
    assert summary["combined_candidates_not_in_event_runs"] == 2
    assert summary["combined_candidates_by_date"] == {"2026-06-01": 2, "2026-06-02": 1}


def test_candidate_projection_summary_counts_missing_reasons_without_fetching(monkeypatch) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("candidate projection should not fetch archive unless requested")

    monkeypatch.setattr(dry_run, "_features_from_open_meteo_archive", fail_if_called)

    summary = dry_run._candidate_projection_summary(
        [
            {
                "slug": "highest-temperature-in-atlanta-on-june-2-2026",
                "event_date": "2026-06-02",
                "resolution_source": "https://www.wunderground.com/history/daily/us/atlanta/KATL",
                "station_id": "KATL",
                "lat": 33.6,
                "lon": -84.4,
                "market_var": "temperature_2m_max",
                "market_unit": "C",
                "outcome_labels": ["28°C or below", "29°C"],
            },
            {
                "slug": "highest-temperature-in-nowhere-on-june-2-2026",
                "event_date": "2026-06-02",
                "market_var": "temperature_2m_max",
            },
        ],
        fetch_open_meteo_archive=False,
        timeout=1.0,
        max_candidates=0,
    )

    assert summary["counts"]["candidate_records_scanned"] == 2
    assert summary["counts"]["metadata_ready"] == 1
    assert summary["counts"]["feature_reconstruction_ready"] == 1
    assert summary["source_status_counts"] == {"open_meteo_archive:not_applicable": 1, "open_meteo_archive:not_fetched": 1}
    assert summary["missing_reason_counts"]["resolution_source_missing"] == 1
    assert summary["missing_reason_counts"]["outcomes_missing"] == 1


def test_weather_com_temperature_features_marks_official_daily_max() -> None:
    features = dry_run._weather_com_temperature_features(
        [
            {"obsTimeLocal": "2026-06-02 14:00:00", "imperial": {"temp": 80}},
            {"obsTimeLocal": "2026-06-02 15:00:00", "imperial": {"temp": 83}},
            {"obsTimeLocal": "2026-06-02 16:00:00", "imperial": {"temp": 82}},
        ]
    )

    assert round(features["observed_today_max_c"], 3) == round((83 - 32) * 5 / 9, 3)
    assert features["observed_today_max_source"] == "weather_com_history_api"
    assert features["observed_today_max_trusted"] is True
    assert features["temperature_observation_final_daily_max"] is True
    assert features["observation_hour_local"] == 15.0
    assert round(features["temperature_rise_rate_c_per_hour"], 3) == round((83 - 80) * 5 / 9, 3)


def test_candidate_apply_plan_ok_after_100_ready_rows() -> None:
    projection = {
        "backtest_row_ready_preview": True,
        "settlement_actual_ready": False,
        "settlement_preview_ready": True,
        "settlement_preview_actual": 81.0,
        "settlement_preview": {"status": "ok", "value": 81.0, "unit": "F", "source": "weather_com_history_api"},
        "slug": "highest-temperature-in-atlanta-on-june-2-2026",
        "event_date": "2026-06-02",
        "title": "Highest temperature in Atlanta on June 2?",
        "station_id": "KATL",
        "resolution_source": "https://www.wunderground.com/history/daily/us/atlanta/KATL",
        "resolution_source_kind": "wunderground_history",
        "market_unit": "F",
        "outcome_count": 2,
        "yes_price_count": 2,
        "yes_price_digest": "prices",
        "projected_feature_quality": "complete",
        "feature_key_count": 12,
        "feature_digest": "features",
    }

    plan = dry_run._candidate_physical_backtest_apply_plan(
        [dict(projection, slug=f"highest-temperature-in-atlanta-on-june-{idx}-2026", event_date=f"2026-06-{idx:02d}") for idx in range(1, 101)],
        max_rows=3,
    )

    assert plan["ok"] is True
    assert plan["ready_row_count"] == 100
    assert plan["row_sample_count"] == 3
    assert plan["blockers"] == []
