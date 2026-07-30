from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_reported_trajectory_no_system_generation.sh"


def test_schema_transaction_is_committed_before_activation_lock():
    text = SCRIPT.read_text(encoding="utf-8")
    schema_call = text.index("apply_reported_trajectory_no_schema(conn)")
    activation_lock = text.index('conn.execute("BEGIN IMMEDIATE")', schema_call)
    between = text[schema_call:activation_lock]
    assert "conn.commit()" in between


def test_system_installer_waits_for_all_three_services_and_fails_closed():
    text = (ROOT / "scripts/install_reported_trajectory_no_system_services.sh").read_text(
        encoding="utf-8"
    )
    generation_text = SCRIPT.read_text(encoding="utf-8")
    assert "019f7a84-53ec-7183-9c5f-7f9cfa62e1f0" not in text
    assert "protected_live_line_present" not in text
    assert "019f7a84-53ec-7183-9c5f-7f9cfa62e1f0" not in generation_text
    assert "generation cutover refused" not in generation_text
    assert "wait_for_system_service_absent" in text
    assert "all_system_services_loaded" in text
    assert 'launchctl print "system/$label"' in text
    assert "state = running" in text
    assert "system_domain_cutover_not_ready" in text
    assert "all system owners stopped" in text
    assert 'label == "com.weather.reported_trajectory_no"' in text
    assert 'env["PM_TARGET_PRODUCTION_WRITER"] = "1"' in text
    assert 'env["PM_TARGET_EXECUTION_MODE"] = "production_no_live"' in text
    assert 'env["PM_CLOB_ORDER_SERVICE_READ_ONLY"] = "1"' in text
    assert "system_domain_cutover_ok_no_live_pause_retained" in text
    assert 'label == "com.weather.clob_order_service"' in text


def test_clob_order_service_template_is_system_domain():
    text = (ROOT / "launchd/com.weather.clob_order_service.plist").read_text(
        encoding="utf-8"
    )
    assert "<key>WEATHER_SERVICE_DOMAIN</key>" in text
    assert "<string>system</string>" in text
    assert "<string>user</string>" not in text
