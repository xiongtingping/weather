from __future__ import annotations

from pathlib import Path

from scripts import auto_trade_watcher


ROOT = Path(__file__).resolve().parents[1]


def test_default_patterns_only_match_trade_actions() -> None:
    patterns = auto_trade_watcher.compile_patterns(auto_trade_watcher.DEFAULT_TRADE_PATTERNS)

    def matched(line: str) -> bool:
        return any(pattern.search(line) for pattern in patterns)

    assert matched("[auto-trade] entry Highest temperature in Chengdu? YES 26°C buy=10 @ 0.25")
    assert matched("[auto-trade] exit Highest temperature in Chengdu? YES 26°C sell=10 @ 0.55")
    assert matched("[auto-trade] exit retry submitted Highest temperature in Chengdu? YES 26°C sell=10 @ 0.55")
    assert matched("[auto-trade] synced fill decision backfilled trade_id=abc")

    assert not matched("[auto-trade] exit skip Highest temperature in Chengdu? YES 26°C reason=exit_no_trigger")
    assert not matched("[auto-trade] event timings highest-temperature-in-chengdu total=35.0s")
    assert not matched("2026-06-21 [model_scan] new market signal, bypass auto-trade yield")


def test_sms_sender_uses_env_recipient_without_heavy_import(monkeypatch) -> None:
    monkeypatch.setenv("PM_SMS_ENABLED", "1")
    monkeypatch.setenv("PM_SMS_RECIPIENT", "+10000000000")
    monkeypatch.setitem(__import__("sys").modules, "scan_weather_market", None)

    sender, enabled, recipient = auto_trade_watcher.load_sms_sender()

    assert sender is auto_trade_watcher._send_sms_message
    assert enabled is True
    assert recipient == "+10000000000"


def test_sms_sender_does_not_import_scanner_when_recipient_missing(monkeypatch) -> None:
    monkeypatch.setenv("PM_SMS_ENABLED", "1")
    monkeypatch.delenv("PM_SMS_RECIPIENT", raising=False)
    monkeypatch.setitem(__import__("sys").modules, "scan_weather_market", None)

    sender, enabled, recipient = auto_trade_watcher.load_sms_sender()

    assert sender is auto_trade_watcher._send_sms_message
    assert enabled is True
    assert recipient == ""
    assert "scan_weather_market" not in (ROOT / "scripts" / "auto_trade_watcher.py").read_text(encoding="utf-8")

