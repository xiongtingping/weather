from __future__ import annotations

import pytest

from ui import server


@pytest.mark.parametrize(
    ("url", "country", "station"),
    [
        ("https://www.wunderground.com/hourly/cn/chengdu/ZUUU", "CN", "ZUUU"),
        ("https://www.wunderground.com/hourly/us/new-york/KLGA", "US", "KLGA"),
    ],
)
def test_parse_wunderground_hourly_context(url: str, country: str, station: str) -> None:
    context = server._parse_wunderground_hourly_context(url)

    assert context == {"country_code": country, "station_id": station}


def test_build_wunderground_observed_station_temp_payload_returns_intraday_max_and_current(
    monkeypatch,
) -> None:
    monkeypatch.setattr(server, "_fetch_text", lambda *args, **kwargs: "apiKey=test123")

    def fake_fetch_json(url, headers=None, timeout=20):
        if "observations/current" in url and "units=m" in url:
            return {
                "temperature": 25.0,
                "validTimeLocal": "2026-06-08T19:38:07+0800",
                "validTimeUtc": 1780918687,
            }
        if "observations/current" in url and "units=e" in url:
            return {
                "temperature": 77.0,
                "validTimeLocal": "2026-06-08T19:38:07+0800",
                "validTimeUtc": 1780918687,
            }
        if "observations/historical.json" in url and "units=m" in url:
            return {
                "observations": [
                    {"temp": 24.0, "valid_time_gmt": 1780902000},
                    {"temp": 27.0, "valid_time_gmt": 1780905600},
                ]
            }
        if "observations/historical.json" in url and "units=e" in url:
            return {
                "observations": [
                    {"temp": 75.0, "valid_time_gmt": 1780902000},
                    {"temp": 81.0, "valid_time_gmt": 1780905600},
                ]
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(server, "_fetch_json", fake_fetch_json)

    payload = server._build_wunderground_observed_station_temp_payload(
        "https://www.wunderground.com/hourly/cn/chengdu/ZUUU",
        "2026-06-08",
    )

    assert payload is not None
    assert payload["ok"] is True
    assert payload["observed_max_c"] == 27.0
    assert payload["observed_max_f"] == 81.0
    assert payload["current_temp_c"] == 25.0
    assert payload["current_temp_f"] == 77.0
    assert payload["forecast_max_c"] == 27.0
    assert payload["forecast_max_f"] == 81.0
    assert payload["station_mode"] == "wunderground_observed_intraday"


def test_build_wunderground_observed_station_temp_payload_returns_unavailable_when_all_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(server, "_fetch_text", lambda *args, **kwargs: "apiKey=test123")
    monkeypatch.setattr(server, "_fetch_json", lambda *args, **kwargs: {})

    payload = server._build_wunderground_observed_station_temp_payload(
        "https://www.wunderground.com/hourly/cn/chengdu/ZUUU",
        "2026-06-08",
    )

    assert payload is not None
    assert payload["ok"] is False
    assert payload["error"] == "wunderground_observed_data_unavailable"
    assert payload["na_reason"] == "wunderground_observed_data_unavailable"


def test_build_wunderground_observed_station_temp_payload_skips_non_hourly_source() -> None:
    payload = server._build_wunderground_observed_station_temp_payload(
        "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "2026-06-08",
    )

    assert payload is None


def test_ui_temperature_trajectory_status_keeps_partial_reason_for_forecast_only_payload() -> None:
    payload = {
        "weather_likelihood_refreshed_at": "2026-06-16T14:20:00+00:00",
        "weather_likelihood_feature_quality": "partial",
        "weather_likelihood_features": {
            "forecast_peak_temp": 25.16,
            "weather_com_forecast_peak_c": 25.0,
            "twc_forecast_peak_c": 25.0,
            "temperature_trajectory_na_reason": "observed_temperature_missing",
            "raw_large_debug_blob": {"ignored": True},
        },
    }
    distribution = [
        {"outcome": "24°C or below", "q_temperature_bucket_posterior": 0.3491},
        {"outcome": "25°C", "q_temperature_bucket_posterior": 0.2132},
    ]

    compact = server._compact_weather_likelihood_features_for_ui(
        payload["weather_likelihood_features"]
    )
    status = server._ui_temperature_trajectory_status_from_payload(payload, distribution)

    assert "raw_large_debug_blob" not in compact
    assert compact["forecast_peak_temp_c"] == 25.16
    assert compact["temperature_trajectory_na_reason"] == "observed_temperature_missing"
    assert status["available"] is False
    assert status["state"] == "partial"
    assert status["label"] == "数据不全"
    assert status["forecast_peak_temp_c"] == 25.16
    assert status["weather_likelihood_refreshed_at"] == "2026-06-16T14:20:00+00:00"
    assert "observed_temperature_missing" in status["missing_fields"]
    assert "temperature_momentum_or_rise_rate_missing" in status["missing_fields"]


def test_slim_ui_snapshot_summary_builds_temperature_trajectory_status_for_legacy_fact() -> None:
    legacy_fact_payload = {
        "slug": "highest-temperature-in-chengdu-on-june-17-2026",
        "event_date": "2026-06-17",
        "weather_likelihood_refreshed_at": "2026-06-16T14:20:00+00:00",
        "weather_likelihood_features": {
            "forecast_peak_temp": 25.16,
            "temperature_trajectory_na_reason": "observed_temperature_missing",
        },
        "distribution": [
            {"outcome": "24°C or below", "q_temperature_bucket_posterior": 0.3491},
            {"outcome": "25°C", "q_temperature_bucket_posterior": 0.2132},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(legacy_fact_payload)

    assert slim["weather_likelihood_features_compact"]["forecast_peak_temp_c"] == 25.16
    assert slim["temperature_trajectory_status"]["available"] is False
    assert slim["temperature_trajectory_status"]["state"] == "partial"
    assert slim["temperature_trajectory_status"]["forecast_peak_temp_c"] == 25.16
    assert "observed_temperature_missing" in slim["temperature_trajectory_status"]["missing_fields"]


def test_slim_ui_snapshot_summary_rebuilds_stale_temperature_trajectory_status() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-18-2026",
        "event_date": "2026-06-18",
        "weather_likelihood_refreshed_at": "2026-06-18T14:08:58+00:00",
        "weather_likelihood_features": {
            "observed_temp_c": 26.0,
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "temperature_momentum_effective_c_per_hour": -0.4,
            "weather_likelihood_refreshed_at": "2026-06-18T14:08:58+00:00",
        },
        "temperature_trajectory_status": {
            "observed_today_max_c": 28.89,
            "weather_likelihood_refreshed_at": "2026-06-18T13:56:25+00:00",
            "stale": True,
        },
        "distribution": [
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.5},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.3},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    status = slim["temperature_trajectory_status"]
    assert status["observed_today_max_c"] == 31.0
    assert status["weather_likelihood_refreshed_at"] == "2026-06-18T14:08:58+00:00"
    assert "stale" not in status


def test_slim_ui_snapshot_summary_syncs_root_temperature_trajectory_into_compact() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-9-2026",
        "event_date": "2026-07-09",
        "weather_likelihood_features": {
            "observed_temp_c": 33.0,
            "observed_today_max_c": 34.0,
            "temperature_trajectory_state": "platform",
            "temperature_trajectory_gate_state": "not_warming",
            "temperature_trajectory_stable_rate_c_per_hour": 0.75,
            "zuuu_metar_raw_amsc_temperature_point_count": 1,
            "zuuu_metar_raw_amsc_temperature_trajectory_realization_state": "insufficient_points",
        },
        "weather_likelihood_features_compact": {
            "zuuu_metar_raw_amsc_temperature_point_count": 1,
            "zuuu_metar_raw_amsc_temperature_trajectory_realization_state": "insufficient_points",
        },
        "temperature_trajectory": {
            "schema": "temperature_trajectory_summary_v1",
            "state": "platform",
            "gate_state": "not_warming",
            "gate_reason": "trajectory_state_platform",
            "stable_rate_c_per_hour": 0.75,
            "effective_rate_c_per_hour": 0.75,
            "adjusted_rate_c_per_hour": 0.0,
            "confidence": 0.7,
            "rolling_temperature_observation_summary": {
                "point_count": 5,
                "current_temp_c": 33.0,
                "rolling_max_c": 34.0,
                "rolling_slope_c_per_hour": 0.75,
            },
        },
        "distribution": [
            {"outcome": "37°C", "q_temperature_bucket_posterior": 0.25},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    compact = slim["weather_likelihood_features_compact"]
    assert compact["temperature_trajectory_state"] == "platform"
    assert compact["temperature_trajectory_gate_state"] == "not_warming"
    assert compact["temperature_trajectory_gate_reason"] == "trajectory_state_platform"
    assert compact["temperature_trajectory_point_count"] == 5
    assert compact["temperature_trajectory_current_temp_c"] == 33.0
    assert compact["temperature_trajectory_rolling_max_c"] == 34.0
    assert compact["temperature_trajectory_adjusted_rate_c_per_hour"] == 0.0
    assert compact["zuuu_metar_raw_amsc_temperature_point_count"] == 1
    assert compact["zuuu_metar_raw_amsc_temperature_trajectory_realization_state"] == "insufficient_points"
    assert slim["temperature_trajectory_status"]["rolling_point_count"] == 5


def test_slim_ui_snapshot_summary_keeps_locked_observed_trajectory_diagnostic() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-2-2026",
        "event_date": "2026-07-02",
        "q_bayes_temperature_trajectory_status": "locked_trusted_observed_after_peak_window",
        "q_bayes_temperature_trajectory_na_reason": "hard_observed_peak_locked_after_peak_window",
        "q_bayes_temperature_trajectory_enabled": False,
        "q_bayes_temperature_trajectory_evidence": 0.0,
        "q_bayes_temperature_trajectory_peak_estimator_source": "locked_trusted_observed",
        "weather_likelihood_features": {
            "observed_temp_c": 27.0,
            "observed_today_max_c": 34.0,
            "forecast_peak_temp": 31.2,
            "temperature_momentum_effective_c_per_hour": -1.0,
            "temperature_momentum_realization_state": "cooling",
            "temperature_momentum_posterior_gate_allowed": False,
            "temperature_momentum_posterior_gate_reason": "momentum_state_not_realized",
        },
        "distribution": [
            {"outcome": "34°C", "q_temperature_bucket_posterior": 0.96},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert slim["q_bayes_temperature_trajectory_status"] == "locked_trusted_observed_after_peak_window"
    assert slim["q_bayes_temperature_trajectory_na_reason"] == "hard_observed_peak_locked_after_peak_window"
    assert slim["q_bayes_temperature_trajectory_enabled"] is False
    status = slim["temperature_trajectory_status"]
    assert status["temperature_trajectory_posterior_status"] == "locked_trusted_observed_after_peak_window"
    assert status["temperature_trajectory_posterior_na_reason"] == "hard_observed_peak_locked_after_peak_window"
    assert "hard_observed_peak_locked_after_peak_window" in status["missing_fields"]
    assert "observed_temperature_missing" not in status["missing_fields"]


def test_ui_temperature_trajectory_status_uses_regime_v1_rate_when_feature_momentum_missing() -> None:
    payload = {
        "weather_likelihood_refreshed_at": "2026-07-02T03:06:49+00:00",
        "weather_likelihood_features": {
            "observed_temp_c": 29.0,
            "observed_today_max_c": 29.0,
            "forecast_peak_temp": 32.0,
        },
    }
    distribution = [
        {
            "outcome": "32°C",
            "q_bayes_temperature_trajectory_enabled": True,
            "q_bayes_temperature_trajectory_evidence": 4.25,
            "q_bayes_temperature_trajectory_status": "enabled",
            "q_bayes_temperature_trajectory_path_mu_c": 32.75,
            "q_bayes_temperature_trajectory_path_sigma_c": 0.77,
            "q_bayes_temperature_trajectory_rate_hat_c_per_hour": 2.24,
            "q_bayes_meta": {
                "temperature_trajectory_posterior_enabled": True,
                "temperature_trajectory_path_mu_c": 32.75,
                "temperature_trajectory_path_sigma_c": 0.77,
                "temperature_trajectory_rate_hat_c_per_hour": 2.24,
            },
        }
    ]

    status = server._ui_temperature_trajectory_status_from_payload(payload, distribution)

    assert status["available"] is True
    assert status["state"] == "available"
    assert status["trajectory_plumbing_mismatch"] is True
    assert "trajectory_plumbing_mismatch" in status["diagnostic_flags"]
    assert "temperature_momentum_or_rise_rate_missing" not in status["missing_fields"]
    assert status["temperature_trajectory_path_mu_c"] == 32.75
    assert status["temperature_trajectory_rate_hat_c_per_hour"] == 2.24


def test_chengdu_summary_missing_physical_weather_when_component_scale_absent() -> None:
    stale_summary = {
        "physical_weather_modifier": {
            "state": "cooling",
            "path_probability_scale": 0.923,
            "components": [
                {
                    "key": "observed_precipitation",
                    "peak_mu_delta_c": -0.35,
                }
            ],
        }
    }
    refreshed_summary = {
        "physical_weather_modifier": {
            "state": "cooling",
            "path_probability_scale": 0.923,
            "components": [
                {
                    "key": "observed_precipitation",
                    "peak_mu_delta_c": -0.35,
                    "path_probability_scale": 0.923,
                }
            ],
        }
    }

    assert server._chengdu_intraday_summary_missing_physical_weather_modifier(stale_summary) is True
    assert server._chengdu_intraday_summary_missing_physical_weather_modifier(refreshed_summary) is False

    compact = server._compact_chengdu_intraday_summary_for_ui_snapshot(stale_summary)
    component = compact["physical_weather_modifier"]["components"][0]
    assert component["path_probability_scale"] == 0.923


def test_ui_snapshot_summary_suppresses_untrusted_temperature_rise_spike() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-19-2026",
        "event_date": "2026-06-19",
        "weather_likelihood_features": {
            "observed_temp_c": 27.22,
            "observed_today_max_c": 28.0,
            "forecast_peak_temp": 33.0,
            "temperature_rise_rate_c_per_hour": -14.74,
            "temperature_rise_rate_raw_pair_c_per_hour": -14.74,
            "temperature_rise_rate_raw_pair_actual_hours": 2.0 / 60.0,
        },
        "distribution": [
            {"outcome": "33°C", "q_temperature_bucket_posterior": 0.5},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.3},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    compact = slim["weather_likelihood_features_compact"]
    status = slim["temperature_trajectory_status"]
    assert "temperature_rise_rate_c_per_hour" not in compact
    assert compact["temperature_rise_rate_trusted"] is False
    assert compact["temperature_rise_rate_na_reason"] == "unstable_temperature_rise_rate_suppressed"
    assert status["temperature_rise_rate_c_per_hour"] is None
    assert status["temperature_rise_rate_trusted"] is False
    assert "unstable_temperature_rise_rate_suppressed" in status["missing_fields"]


def test_ui_snapshot_summary_keeps_stable_window_temperature_rise_rate() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-19-2026",
        "event_date": "2026-06-19",
        "weather_likelihood_features": {
            "observed_temp_c": 27.22,
            "observed_today_max_c": 28.0,
            "forecast_peak_temp": 33.0,
            "temperature_rise_rate_c_per_hour": -0.23,
            "temperature_rise_rate_source": "wu_twc_observed_intraday:45m",
            "temperature_rise_rate_window": "45m",
        },
        "distribution": [
            {"outcome": "33°C", "q_temperature_bucket_posterior": 0.5},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.3},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    compact = slim["weather_likelihood_features_compact"]
    status = slim["temperature_trajectory_status"]
    assert compact["temperature_rise_rate_c_per_hour"] == -0.23
    assert compact["temperature_rise_rate_trusted"] is True
    assert status["temperature_rise_rate_c_per_hour"] == -0.23
    assert status["temperature_rise_rate_window"] == "45m"
