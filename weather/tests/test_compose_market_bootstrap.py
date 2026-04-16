from scan_weather_market import _should_bootstrap_market_for_compose_error


def test_should_bootstrap_market_for_compose_error() -> None:
    assert _should_bootstrap_market_for_compose_error("missing_snapshot") is True
    assert _should_bootstrap_market_for_compose_error("market_snapshot_expired") is True
    assert _should_bootstrap_market_for_compose_error("snapshot_time_mismatch") is True
    assert _should_bootstrap_market_for_compose_error("model_snapshot_expired") is False
    assert _should_bootstrap_market_for_compose_error(None) is False
