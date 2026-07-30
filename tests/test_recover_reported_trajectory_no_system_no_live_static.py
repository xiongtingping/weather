from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "scripts"
    / "recover_reported_trajectory_no_system_no_live.sh"
)


def test_recovery_is_target_only_and_preserves_no_live_boundary() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--confirm-target-only-production-no-live-recovery" in text
    assert 'launchctl bootstrap system "$TARGET_PLIST"' in text
    assert 'launchctl bootout "system/$TARGET_LABEL"' in text
    assert 'launchctl bootout "system/$MARKET_LABEL"' not in text
    assert 'launchctl bootout "system/$ORDER_LABEL"' not in text
    assert 'launchctl kickstart -k "system/$MARKET_LABEL"' not in text
    assert 'launchctl kickstart -k "system/$ORDER_LABEL"' not in text
    assert "PM_TARGET_PRODUCTION_WRITER" in text
    assert "http://127.0.0.1:7897" in text
    assert "target_remaining_budget_usdc" in text
    assert "live_submit_authorized=1" in text
    assert "basket_target_order_events" in text
    assert "basket_target_execution_receipts" in text
    assert "minimum_free_disk_guard" in text


def test_recovery_has_exit_signal_rollback_and_pid_freeze() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "trap rollback_target_only EXIT INT TERM" in text
    assert 'market_after="$(service_pid "$MARKET_LABEL")"' in text
    assert 'order_after="$(service_pid "$ORDER_LABEL")"' in text
    assert '"$market_after" != "$market_before"' in text
    assert '"$order_after" != "$order_before"' in text
    assert "reported_trajectory_no_target_only_recovery_ok" in text
