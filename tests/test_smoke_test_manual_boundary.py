from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ui_smoke_test_requires_manual_intent_and_confirm_phrase():
    text = (ROOT / "ui" / "server.py").read_text()
    smoke_block = text.split("def auto_trade_smoke_test_http", 1)[1].split(
        "def _launchd_legacy_trade_labels", 1
    )[0]

    assert "AUTO_TRADE_SMOKE_TEST_CONFIRM_PHRASE" in text
    assert 'params.get("intent", [""])[0] == "manual_smoke_test"' in smoke_block
    assert "confirm_raw == confirm_phrase" in smoke_block
    assert "missing_manual_confirm" in smoke_block
    assert "confirm=1" not in smoke_block


def test_weatherctl_smoke_test_is_preflight_not_real_order_confirm():
    text = (ROOT / "scripts" / "weatherctl.sh").read_text()
    case_block = text.split("auto:smoke-test)", 1)[1].split(";;", 1)[0]

    assert "intent=manual_smoke_test" in case_block
    assert "confirm=1" not in case_block
    assert "confirm=<PM_AUTO_TRADE_SMOKE_TEST_CONFIRM_PHRASE>" in case_block
    assert "preflight only" in text


def test_smoke_test_handler_rejects_legacy_confirm_one_before_db_access():
    from ui import server

    payload = server.auto_trade_smoke_test_http(
        {
            "enable": ["1"],
            "intent": ["manual_smoke_test"],
            "confirm": ["1"],
        },
        "127.0.0.1",
    )

    assert payload["ok"] is False
    assert payload["error"] == "missing_manual_confirm"
    assert payload["request_enable"] is True
    assert payload["manual_intent"] is True
    assert payload["confirm_phrase_matched"] is False


def test_smoke_test_handler_env_enable_does_not_replace_request_enable(monkeypatch):
    from ui import server

    monkeypatch.setenv("PM_AUTO_TRADE_SMOKE_TEST_ENABLED", "1")
    payload = server.auto_trade_smoke_test_http(
        {
            "intent": ["manual_smoke_test"],
            "confirm": [server.AUTO_TRADE_SMOKE_TEST_CONFIRM_PHRASE],
        },
        "127.0.0.1",
    )

    assert payload["ok"] is False
    assert payload["error"] == "missing_manual_confirm"
    assert payload["request_enable"] is False
    assert payload["manual_intent"] is True
    assert payload["confirm_phrase_matched"] is True
