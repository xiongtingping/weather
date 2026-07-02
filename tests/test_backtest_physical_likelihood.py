import json
import sqlite3

from scripts.backtest_physical_likelihood import (
    _derived_feature_group_status,
    _extract_events,
    _extract_snapshot_plan_events,
    _feature_quality,
    _merge_events_with_overlay,
)


def test_feature_quality_counts_zero_mrms_rate_as_observed_precip_evidence() -> None:
    features = {
        "observed_temp": 29.5,
        "temperature_rise_rate_c_per_hour": 1.2,
        "cti_observed": 0.72,
        "cloud_cover_low_pct": 20.0,
        "radar_precip_rate_mm_per_h": 0.0,
        "wind_speed_mps": 4.0,
        "wind_direction_deg": 220.0,
        "dewpoint_c": 21.0,
    }

    groups = _derived_feature_group_status(features)

    assert groups["precipitation_evaporative_cooling"]["available"] is True
    assert "radar_precip_rate_mm_per_h" in groups["precipitation_evaporative_cooling"]["present_keys"]
    assert _feature_quality(features) == "complete"


def test_feature_quality_does_not_treat_false_raining_flag_alone_as_precip_evidence() -> None:
    features = {
        "observed_temp": 29.5,
        "cti_observed": 0.72,
        "cloud_cover_low_pct": 20.0,
        "is_raining_observed": False,
        "wind_speed_mps": 4.0,
        "dewpoint_c": 21.0,
    }

    groups = _derived_feature_group_status(features)

    assert groups["precipitation_evaporative_cooling"]["available"] is False
    assert _feature_quality(features) == "partial"


def test_feature_quality_does_not_treat_open_meteo_precip_proxy_as_complete() -> None:
    features = {
        "observed_temp": 29.5,
        "temperature_rise_rate_c_per_hour": 1.2,
        "cti_observed": 0.72,
        "cloud_cover_low_pct": 20.0,
        "observed_precip_rate_mm_per_h": 0.1,
        "precipitation_rate_mm_per_h": 0.1,
        "is_raining": True,
        "radar_qpe_proxy_source": "open_meteo_current_hourly_precip",
        "wind_speed_mps": 4.0,
        "wind_direction_deg": 220.0,
        "dewpoint_c": 21.0,
    }

    groups = _derived_feature_group_status(features)

    assert groups["precipitation_evaporative_cooling"]["available"] is False
    assert groups["precipitation_evaporative_cooling"]["evidence_state"] == "proxy"
    assert _feature_quality(features) == "partial"


def test_snapshot_plan_overlay_is_read_only_complete_event_source(tmp_path) -> None:
    plan_path = tmp_path / "physical_backtest_snapshot_apply_plan_latest.json"
    plan = {
        "candidate_projection": {
            "physical_backtest_apply_plan": {
                "ok": True,
                "read_only": True,
                "writes_performed": False,
                "rows": [
                    {
                        "slug": "highest-temperature-in-testville-on-may-20-2026",
                        "event_date": "2026-05-20",
                        "feature_quality": "complete",
                        "candidate_settlement_actual": 21.0,
                        "settlement_actual_source": "weather_com_history_api",
                        "resolution_source_kind": "wunderground_history",
                        "market_unit": "C",
                        "snapshot_payload": {
                            "market_unit": "C",
                            "market_var": "temperature_2m_max",
                            "weather_likelihood_feature_quality": "complete",
                            "weather_likelihood_features": {
                                "observed_temp": 20.5,
                                "observed_today_max_c": 21.0,
                                "temperature_rise_rate_c_per_hour": 0.5,
                                "observation_hour_local": 15.0,
                                "solar_radiation_observed": 650.0,
                                "shortwave_radiation_observed": 650.0,
                                "cloud_cover_low_pct": 10.0,
                                "cloud_cover_mid_pct": 20.0,
                                "observed_precip_rate_mm_per_h": 0.0,
                                "precipitation_rate_mm_per_h": 0.0,
                                "wind_speed_mps": 4.0,
                                "wind_direction_deg": 220.0,
                                "pressure_hpa": 1010.0,
                                "dewpoint_c": 15.0,
                            },
                            "rows": [
                                {"outcome": "20°C or below", "q_raw_static": 0.45, "q_raw": 0.45},
                                {"outcome": "21°C", "q_raw_static": 0.55, "q_raw": 0.55},
                            ],
                        },
                    }
                ],
            }
        }
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    events, diagnostics = _extract_snapshot_plan_events(plan_path, "2026-05-01", "2026-05-31")

    assert diagnostics["read_only"] is True
    assert diagnostics["writes_performed"] is False
    assert diagnostics["candidate_rows"] == 1
    assert diagnostics["included_events"] == 1
    assert events[0]["event_source"] == "read_only_snapshot_plan_overlay"
    assert events[0]["feature_quality"] == "complete"
    assert events[0]["saved_feature_snapshot"] is True


def test_snapshot_plan_overlay_replaces_same_formal_event(tmp_path) -> None:
    formal = {
        "slug": "same-slug",
        "event_date": "2026-05-20",
        "feature_quality": "minimal",
        "event_source": "formal_db",
    }
    overlay = {
        "slug": "same-slug",
        "event_date": "2026-05-20",
        "feature_quality": "complete",
        "event_source": "read_only_snapshot_plan_overlay",
    }

    merged, diagnostics = _merge_events_with_overlay([formal], [overlay])

    assert diagnostics["formal_db_events"] == 1
    assert diagnostics["overlay_events"] == 1
    assert diagnostics["overlay_replacements"] == 1
    assert diagnostics["overlay_additions"] == 0
    assert merged == [overlay]


def test_snapshot_plan_overlay_can_use_formal_db_actual_fallback(tmp_path) -> None:
    plan_path = tmp_path / "physical_backtest_snapshot_apply_plan_latest.json"
    plan = {
        "candidate_projection": {
            "physical_backtest_apply_plan": {
                "ok": True,
                "read_only": True,
                "writes_performed": False,
                "rows": [
                    {
                        "slug": "highest-temperature-in-testville-on-may-20-2026",
                        "event_date": "2026-05-20",
                        "feature_quality": "complete",
                        "candidate_settlement_actual": None,
                        "settlement_actual_source": "existing_db",
                        "resolution_source_kind": "wunderground_history",
                        "market_unit": "F",
                        "snapshot_payload": {
                            "market_unit": "F",
                            "market_var": "temperature_2m_max",
                            "weather_likelihood_feature_quality": "complete",
                            "weather_likelihood_features": {
                                "observed_temp": 70.0,
                                "observed_today_max_c": 21.0,
                                "observed_today_max_source": "weather_com_history_api",
                                "observed_today_max_trusted": True,
                                "temperature_observation_final_daily_max": True,
                                "temperature_rise_rate_c_per_hour": 0.5,
                                "solar_radiation_observed": 650.0,
                                "cloud_cover_low_pct": 10.0,
                                "observed_precip_rate_mm_per_h": 0.0,
                                "wind_speed_mps": 4.0,
                                "dewpoint_c": 15.0,
                            },
                            "rows": [
                                {"outcome": "68°F or below", "q_raw_static": 0.2, "q_raw": 0.2},
                                {"outcome": "between 69-70°F", "q_raw_static": 0.8, "q_raw": 0.8},
                            ],
                        },
                    }
                ],
            }
        }
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    events, diagnostics = _extract_snapshot_plan_events(
        plan_path,
        "2026-05-01",
        "2026-05-31",
        fallback_actual_c_by_event={("highest-temperature-in-testville-on-may-20-2026", "2026-05-20"): 21.0},
    )

    assert diagnostics["fallback_actual_events"] == 1
    assert diagnostics["skipped_counts"] == {}
    assert len(events) == 1
    assert events[0]["snapshot_plan_settlement_actual_source"] == "formal_db_existing_actual"
    assert events[0]["feature_quality"] == "partial"
    assert events[0]["posterior_probs"][1] > 0.0


def test_extract_events_ignores_unclosed_settlement_actual(tmp_path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE event_runs(
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            market_var TEXT,
            market_unit TEXT,
            settlement_actual REAL,
            market_closed INTEGER,
            payload_json TEXT
        )
        """
    )
    payload = {
        "rows": [
            {"outcome": "21°C", "q_raw_static": 0.6},
            {"outcome": "22°C", "q_raw_static": 0.4},
        ]
    }
    conn.execute(
        """
        INSERT INTO event_runs(
            id, slug, event_date, market_var, market_unit,
            settlement_actual, market_closed, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "highest-temperature-in-testville-on-may-20-2026",
            "2026-05-20",
            "temperature_2m_max",
            "C",
            21.0,
            1,
            json.dumps(payload),
        ),
    )
    conn.execute(
        """
        INSERT INTO event_runs(
            id, slug, event_date, market_var, market_unit,
            settlement_actual, market_closed, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            2,
            "highest-temperature-in-testville-on-may-20-2026",
            "2026-05-20",
            "temperature_2m_max",
            "C",
            99.0,
            0,
            json.dumps(payload),
        ),
    )
    conn.commit()

    events = _extract_events(conn, "2026-05-01", "2026-05-31")

    assert len(events) == 1
    assert events[0]["settlement_actual_c"] == 21.0


def test_snapshot_plan_overlay_uses_source_matched_daily_actual(tmp_path) -> None:
    plan_path = tmp_path / "physical_backtest_snapshot_apply_plan_latest.json"
    resolution_source = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"
    plan = {
        "candidate_projection": {
            "physical_backtest_apply_plan": {
                "ok": True,
                "read_only": True,
                "writes_performed": False,
                "rows": [
                    {
                        "slug": "highest-temperature-in-chengdu-on-june-6-2026",
                        "event_date": "2026-06-06",
                        "feature_quality": "complete",
                        "candidate_settlement_actual": None,
                        "settlement_actual_source": "existing_db",
                        "resolution_source": resolution_source,
                        "resolution_source_kind": "wunderground_history",
                        "market_unit": "C",
                        "snapshot_payload": {
                            "market_unit": "C",
                            "market_var": "temperature_2m_max",
                            "weather_likelihood_feature_quality": "complete",
                            "weather_likelihood_features": {
                                "observed_temp": 36.1,
                                "observed_today_max_c": 36.1111111111111,
                                "observed_today_max_source": "weather_com_history_api",
                                "observed_today_max_trusted": True,
                                "temperature_observation_final_daily_max": True,
                                "temperature_rise_rate_c_per_hour": 0.0,
                                "solar_radiation_observed": 700.0,
                                "cloud_cover_low_pct": 10.0,
                                "observed_precip_rate_mm_per_h": 0.0,
                                "wind_speed_mps": 4.0,
                                "dewpoint_c": 18.0,
                            },
                            "rows": [
                                {"outcome": "36°C", "q_raw_static": 0.4},
                                {"outcome": "37°C", "q_raw_static": 0.6},
                            ],
                        },
                    }
                ],
            }
        }
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    events, diagnostics = _extract_snapshot_plan_events(
        plan_path,
        "2026-06-01",
        "2026-06-10",
        source_matched_actual_c_by_source_event={(resolution_source.lower(), "2026-06-06"): 36.1111111111111},
    )

    assert diagnostics["source_matched_actual_events"] == 1
    assert diagnostics["actual_feature_mismatch_events"] == 0
    assert len(events) == 1
    assert events[0]["snapshot_plan_settlement_actual_source"] == "event_daily_temps_resolution_source_match"
    assert events[0]["feature_quality"] == "complete"
