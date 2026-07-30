from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "scripts/cutover_reported_trajectory_no_code_only_no_live.sh"
)


def test_code_only_cutover_never_restarts_non_target_services() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'launchctl kickstart -k "system/$LABEL"' in text
    assert 'launchctl kickstart -k "system/$MARKET_LABEL"' not in text
    assert 'launchctl kickstart -k "system/$ORDER_LABEL"' not in text
    assert "install_reported_trajectory_no_system_services.sh" not in text
    assert "cutover_reported_trajectory_no_system_generation.sh" not in text
    assert "bootout" not in text


def test_code_only_cutover_retains_no_live_pause_and_pid_guards() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'CONFIRM="${1:-}"' in text
    assert "--confirm-code-only-production-no-live" in text
    assert 'touch "$PAUSE"' in text
    assert 'rm -f "$PAUSE"' not in text
    assert "production_no_live" in text
    assert "live_submit_authorized" in text
    assert "authorizes_order" in text
    assert "market_pid_after" in text
    assert "order_pid_after" in text
    assert "code_only_cutover_non_target_pid_changed" in text


def test_code_only_cutover_updates_only_reported_trajectory_activation() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "UPDATE reported_trajectory_activation_state SET" in text
    assert "UPDATE basket_" not in text
    assert "INSERT INTO basket_" not in text
