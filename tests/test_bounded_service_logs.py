from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_live_service_launchers_share_bounded_log_policy() -> None:
    helper = (ROOT / "scripts/lib/bounded_service_logs.sh").read_text(encoding="utf-8")
    assert "WEATHER_SERVICE_LOG_MAX_BYTES=67108864" in helper
    assert "ulimit -f" in helper
    assert 'case "$log_path" in' in helper
    assert '"$ROOT/data/"*.log' in helper

    for launcher in (
        "scripts/market_ws_listener.sh",
        "scripts/clob_order_service.sh",
        "scripts/reported_trajectory_no_daemon.sh",
    ):
        text = (ROOT / launcher).read_text(encoding="utf-8")
        assert 'bounded_service_logs.sh' in text
        assert 'weather_prepare_bounded_service_logs' in text
