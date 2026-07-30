from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_monitor_refreshes_context_after_target_capture_and_never_blocks_physical() -> None:
    monitor = (ROOT / "scripts/polymarket_monitor_daemon.sh").read_text()
    refresh = (ROOT / "scripts/refresh_active_v6_market_context.py").read_text()
    target = monitor.index("run_resolution_capture_stage target_rule")
    context = monitor.index("refresh_active_v6_market_context.py")
    assert target < context
    assert "failed_nonblocking" in monitor
    assert '"blocks_physical_x": False' in refresh
    assert '"external_disk_required": False' in refresh
    assert '"linearization_point": "atomic_market_context_activation_replace"' in refresh
