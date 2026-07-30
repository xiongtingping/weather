from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_monitor_installer_is_user_domain_and_contains_no_sudo() -> None:
    text = (ROOT / "scripts/install_monitor_launchdaemon.sh").read_text()
    assert 'DEST_DIR="$HOME/Library/LaunchAgents"' in text
    assert 'GUI_ROOT="gui/${UID_VALUE}"' in text
    assert "launchctl bootstrap \"$GUI_ROOT\" \"$DEST\"" in text
    assert "sudo " not in text
    assert "/Library/LaunchDaemons" not in text


def test_gui_entrypoint_waits_for_legacy_system_pid_before_starting_writer() -> None:
    text = (ROOT / "scripts/weather_monitor_launchagent_entrypoint.sh").read_text()
    assert 'SYSTEM_LABEL="system/com.weather.monitor.daemon"' in text
    assert "waiting_for_legacy_system_exit" in text
    assert 'exec "$ROOT/scripts/polymarket_monitor_daemon.sh"' in text
    assert text.index("while launchctl print") < text.index('exec "$ROOT/scripts/polymarket_monitor_daemon.sh"')


def test_retired_system_image_fails_closed_after_migration_sentinel() -> None:
    text = (ROOT / "scripts/polymarket_monitor_daemon.sh").read_text()
    assert "monitor_user_domain_migration.enabled" in text
    assert 'MONITOR_LAUNCH_DOMAIN="${PM_MONITOR_LAUNCH_DOMAIN:-system/com.weather.monitor.daemon}"' in text
    assert "retired system-domain image refused" in text


def test_active_hotfix_cutover_contains_no_sudo_and_requires_gui_monitor() -> None:
    text = (ROOT / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh").read_text()
    assert "sudo " not in text
    assert 'MONITOR_DOMAIN="gui/$(id -u)/com.weather.monitor.daemon"' in text
    assert "legacy system monitor still running" in text
