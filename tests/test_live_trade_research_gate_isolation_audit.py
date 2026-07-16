from __future__ import annotations

from pathlib import Path

from scripts.live_trade_research_gate_isolation_audit import build_live_trade_research_gate_isolation_audit


def test_current_live_trade_modules_do_not_reference_strict_asof_or_oos_gates() -> None:
    report = build_live_trade_research_gate_isolation_audit()
    assert report["ok"] is True
    assert report["boundary"]["oos_is_offline_only"] is True


def test_audit_fails_closed_when_research_gate_is_added_to_live_module(tmp_path: Path) -> None:
    live_paths = (
        "bot/main.py", "scan_weather_market.py", "weather_strategy_runtime.py", "target_realization_v3_runtime.py",
        "scripts/auto_trade_watcher.py", "scripts/auto_trade_worker.sh", "scripts/clob_order_service.mjs", "scripts/polymarket_order.mjs",
    )
    for relative in live_paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime_only\n", encoding="utf-8")
    (tmp_path / "scripts/auto_trade_watcher.py").write_text("if strict_asof_ready: pass\n", encoding="utf-8")
    report = build_live_trade_research_gate_isolation_audit(root=tmp_path)
    assert report["ok"] is False
    assert report["violations"][0]["path"] == "scripts/auto_trade_watcher.py"
