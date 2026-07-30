from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_rkpk_jit_ws_no_live.sh"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_cutover_only_restarts_target_service() -> None:
    source = _source()
    assert '/bin/launchctl bootout "system/$TARGET_LABEL"' in source
    assert '/bin/launchctl bootstrap system "$TARGET_PLIST"' in source
    assert 'bootout "system/$MARKET_LABEL"' not in source
    assert 'bootout "system/$ORDER_LABEL"' not in source
    assert 'bootstrap system "$MARKET' not in source
    assert 'bootstrap system "$ORDER' not in source


def test_cutover_migrates_and_replays_target_only_capital_epoch() -> None:
    source = _source()
    assert "migrate_target_no_live_capital_epoch" in source
    assert 'runtime.get("capital_scope") != "target_owned_only"' in source
    assert 'runtime.get("reads_shared_live_capital") is not False' in source
    assert 'float(runtime.get("target_remaining_budget_usdc", 0.0)) != 12.0' in source
    assert 'replay_result.get("state") != "UNCHANGED_ZERO_WRITE"' in source
    assert 'replay_result.get("rows_written") != 0' in source


def test_cutover_preserves_pause_and_non_target_pids_on_success_and_error() -> None:
    source = _source()
    assert "rkpk_jit_ws_non_target_pid_changed_on_error" in source
    assert "rkpk_jit_ws_non_target_pid_changed" in source
    assert "market_ws_pid_before" in source
    assert "market_ws_pid_after" in source
    assert "clob_order_service_pid_before" in source
    assert "clob_order_service_pid_after" in source
    assert "live_pause_sha256_before" in source
    assert "live_pause_sha256_after" in source
    assert "rkpk_jit_ws_live_pause_content_changed" in source
    assert 'rm "$LIVE_PAUSE"' not in source


def test_cutover_receipt_retains_no_live_boundary() -> None:
    source = _source()
    assert '"schema": "rkpk_jit_ws_no_live_cutover_receipt_v2"' in source
    assert '"capital_epoch_migration": capital_receipt' in source
    assert '"execution_mode": "production_no_live"' in source
    assert '"live_submit_authorized": False' in source
    assert '"calls_order_gateway": False' in source
    assert '"orders_submitted": 0' in source
