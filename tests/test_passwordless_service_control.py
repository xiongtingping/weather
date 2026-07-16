from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "weather_service_privileged_helper.sh"
INSTALLER = ROOT / "scripts" / "install_passwordless_service_control.sh"
CONTROL = ROOT / "scripts" / "weather_service_control.sh"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_privileged_helper_rejects_unknown_action_before_launchctl() -> None:
    result = _run(HELPER, "restart-arbitrary-service")
    assert result.returncode == 64
    assert "action not allowed" in result.stderr


def test_user_control_rejects_unknown_service() -> None:
    result = _run(CONTROL, "restart", "arbitrary-service")
    assert result.returncode == 64
    assert "用法" in result.stderr


def test_sudoers_contract_is_exact_and_helper_uses_fixed_labels() -> None:
    installer = INSTALLER.read_text()
    helper = HELPER.read_text()

    rule = "$TARGET_USER ALL=(root) NOPASSWD: $HELPER restart-monitor"
    assert rule in installer
    assert "system/com.weather.monitor.daemon" in helper
    assert "com.polymarket.bot.daemon" not in helper
    assert "restart-bot" not in helper
    assert 'exec /bin/launchctl kickstart -k' in helper
    assert '"$@"' not in helper


def test_user_control_never_falls_back_to_prompting_sudo() -> None:
    control = CONTROL.read_text()
    assert '/usr/bin/sudo -n "$HELPER" "$action"' in control
    assert "/usr/bin/sudo \"$HELPER\"" not in control


def test_bot_control_uses_current_user_trade_engine_without_sudo() -> None:
    control = CONTROL.read_text()
    assert 'bot_service="gui/${uid}/com.weather.trade_engine"' in control
    assert '/bin/launchctl kickstart -k "$bot_service"' in control
    assert "com.polymarket.bot.daemon" not in control
