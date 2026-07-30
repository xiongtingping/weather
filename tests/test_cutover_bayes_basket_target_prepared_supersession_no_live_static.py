from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "scripts"
    / "cutover_bayes_basket_target_prepared_supersession_no_live.sh"
)


def test_cutover_is_target_only_and_retains_no_live_boundary() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'launchctl bootout "system/$TARGET_LABEL"' in text
    assert 'launchctl bootstrap system "$INSTALLED_PLIST"' in text
    assert 'launchctl bootout "system/$MARKET_LABEL"' not in text
    assert 'launchctl bootout "system/$ORDER_LABEL"' not in text
    assert 'touch "$PAUSE"' in text
    assert "live_pause_retained=true" in text
    assert "--confirm-prepared-supersession-production-no-live" in text
    assert "prepared_supersession_receipt_not_materialized" in text
    assert "prepared_supersession_pool_reservation_mismatch" in text
    assert '"calls_order_gateway": False' in text
    assert '"orders_submitted": 0' in text
