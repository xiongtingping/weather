from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT / "scripts/cutover_delta_v2_official_tick_grid_no_live.sh"
)


def test_tick_grid_cutover_is_target_only_and_no_live() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert (
        "--confirm-delta-v2-official-tick-grid-no-live" in text
    )
    assert (
        "delta_basket_score_v2_rkpk_no_live_bootstrap_v3.json"
        in text
    )
    assert (
        "bayes_basket_target_target_v2_station_policy_no_live_v2.json"
        in text
    )
    assert '/bin/launchctl bootout "system/$TARGET_LABEL"' in text
    assert '/bin/launchctl bootstrap system "$TARGET_PLIST"' in text
    assert 'bootout "system/$MARKET_LABEL"' not in text
    assert 'bootout "system/$ORDER_LABEL"' not in text
    assert "delta_v2_tick_grid_protected_service_pid_changed" in text
    assert "live_submit_authorized" in text
    assert "calls_order_gateway" in text
    assert "orders_submitted" in text
    assert "live_pause_retained" in text
    assert "PRAGMA quick_check" in text
    assert "PRAGMA foreign_key_check" in text
    assert "PRAGMA journal_mode" in text
    assert "price=0.004" in text
    assert "p004of1000" in text
    assert "DELTA_BASKET_SCORE_V2" in text
    assert "cash_template_caps_target" in text
    assert "ready_no_live" in text
    assert "write_bounded_json" in text
