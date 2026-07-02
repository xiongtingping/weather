from datetime import datetime, timedelta, timezone

from scan_weather_market import _apply_temperature_trajectory_rates


def test_temperature_rise_rate_uses_stable_window_not_last_pair_spike():
    base = datetime(2026, 6, 19, 2, 0, tzinfo=timezone.utc)
    pairs = [
        (base - timedelta(minutes=60), 30.0),
        (base - timedelta(minutes=30), 31.0),
        (base - timedelta(minutes=2), 32.0),
        (base, 31.5),
    ]
    features = {}

    _apply_temperature_trajectory_rates(features, pairs, source="unit_test")

    assert round(features["temperature_rise_rate_raw_pair_c_per_hour"], 2) == -15.0
    assert round(features["temperature_rise_rate_c_per_hour"], 2) == 1.5
    assert features["temperature_rise_rate_window"] == "1h"
    assert features["temperature_rise_rate_source"] == "unit_test:1h"
    assert "2m" in features["temperature_momentum_available_horizons"]


def test_temperature_rise_rate_does_not_promote_short_window_only_noise():
    base = datetime(2026, 6, 19, 2, 0, tzinfo=timezone.utc)
    pairs = [
        (base - timedelta(minutes=2), 32.0),
        (base, 31.5),
    ]
    features = {}

    _apply_temperature_trajectory_rates(features, pairs, source="unit_test")

    assert round(features["temperature_rise_rate_raw_pair_c_per_hour"], 2) == -15.0
    assert "temperature_rise_rate_c_per_hour" not in features
    assert features["temperature_rise_rate_na_reason"] == "stable_window_missing"
    assert features["temperature_momentum_realization_state"] == "short_window_only"
    assert features["temperature_momentum_posterior_gate_allowed"] is False
    assert features["temperature_momentum_posterior_gate_reason"] == "sub_30m_windows_not_used_for_live_momentum"


def test_temperature_momentum_short_window_warming_is_posterior_watch_only():
    base = datetime(2026, 6, 19, 2, 0, tzinfo=timezone.utc)
    pairs = [
        (base - timedelta(minutes=15), 30.0),
        (base - timedelta(minutes=10), 30.2),
        (base, 31.0),
    ]
    features = {}

    _apply_temperature_trajectory_rates(features, pairs, source="unit_test")

    assert features["temperature_momentum_realization_state"] == "short_window_only"
    assert features["temperature_momentum_posterior_gate_allowed"] is False
    assert features["temperature_momentum_posterior_gate_state"] == "diagnostic_only"
    assert features["temperature_momentum_posterior_gate_reason"] == "sub_30m_windows_not_used_for_live_momentum"
    assert features["temperature_momentum_posterior_confirmed_window_count"] == 0
    assert features["temperature_momentum_diagnostic_only_windows"] == "15m,10m,5m"
    assert "temperature_momentum_posterior_effective_c_per_hour" not in features
    assert "temperature_rise_rate_c_per_hour" not in features
    assert features["temperature_rise_rate_na_reason"] == "stable_window_missing"
