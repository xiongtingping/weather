from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = (
    ROOT / "scripts/cutover_delta_basket_score_v2_global_no_live.sh"
)


def test_global_delta_v2_cutover_keeps_no_live_and_service_boundaries() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--confirm-global-delta-v2-production-no-live" in text
    assert "delta_basket_score_v2_global_no_live_bootstrap_v1.json" in text
    assert 'launchctl bootout "system/$TARGET_LABEL"' in text
    assert 'launchctl bootstrap system "$TARGET_PLIST"' in text
    assert 'launchctl bootout "system/$MARKET_LABEL"' not in text
    assert 'launchctl bootout "system/$ORDER_LABEL"' not in text
    assert "oos_gate_passed" in text
    assert "future_preselection_waits_for_settlement=false" in text
    assert "registry_export_decoupled_from_nwp=true" in text
    assert "current_catalog_only" in text
    assert "forward_oos_blocks_production_no_live=false" in text
    assert "live_pause_retained=true" in text
    assert "basket_target_order_events" in text
    assert "MAX_DATABASE_BYTES" in text
    assert "MAX_GROWTH_BYTES" in text
