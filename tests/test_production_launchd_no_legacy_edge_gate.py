from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PLISTS = (
    ROOT / "launchd" / "com.weather.trade_engine.plist",
    ROOT / "launchd" / "com.weather.auto_trade_entry_1m.plist",
    ROOT / "launchd" / "com.xiong.weather.ui.plist",
)
FORBIDDEN = ("PM_EDGE_REALIZABLE_V2_ENABLED", "PM_EDGE_REALIZABLE_V2_GATE_ENTRY")


def test_production_daemon_plists_do_not_inject_retired_edge_gate_environment() -> None:
    for plist in PRODUCTION_PLISTS:
        text = plist.read_text(encoding="utf-8")
        assert all(token not in text for token in FORBIDDEN), plist


def test_production_wrappers_unset_retired_edge_gate_after_env_loading() -> None:
    for wrapper in ("trade_engine_loop.sh", "auto_trade_entry_1m.sh", "ui_server_forever.sh"):
        text = (ROOT / "scripts" / wrapper).read_text(encoding="utf-8")
        assert "PM_EDGE_REALIZABLE_*|PM_EDGE_TRADEABILITY_*|PM_LCB_GLOBAL_*|PM_ENTRY_USABLE_EDGE_*" in text
