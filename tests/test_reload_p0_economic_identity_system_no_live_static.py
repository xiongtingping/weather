from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts/reload_p0_economic_identity_system_no_live.sh"
)


def test_reload_is_two_service_only_and_keeps_order_service_frozen() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'kickstart -k "system/$MARKET_LABEL"' in text
    assert 'kickstart -k "system/$TARGET_LABEL"' in text
    assert 'kickstart -k "system/$ORDER_LABEL"' not in text
    assert "p0_economic_reload_order_service_pid_changed" in text


def test_reload_fences_live_disk_and_database_integrity() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "/usr/bin/touch \"$LIVE_PAUSE\"" in text
    assert "production_no_live" in text
    assert "live_submit_authorized" in text
    assert "authorizes_order" in text
    assert "2 * 1024 * 1024 * 1024" in text
    assert text.count("PRAGMA quick_check") == 2


def test_reload_requires_new_economic_identity_and_zero_duplicates() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "economic_quote_sha256" in text
    assert "p0_economic_reload_duplicate_economic_write" in text
    assert "orders_submitted" in text
    assert "--confirm-p0-economic-identity-production-no-live" in text
