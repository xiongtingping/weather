from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETIRED_PLISTS = (
    ROOT / "launchd" / "com.weather.trade_engine.plist",
    ROOT / "launchd" / "com.xiong.weather.ui.plist",
)


def test_retired_trade_engine_and_ui_plists_are_absent() -> None:
    assert all(not plist.exists() for plist in RETIRED_PLISTS)


def test_production_wrappers_unset_retired_edge_gate_after_env_loading() -> None:
    for wrapper in ("trade_engine_loop.sh", "auto_trade_entry_1m.sh", "ui_server_forever.sh"):
        text = (ROOT / "scripts" / wrapper).read_text(encoding="utf-8")
        assert "PM_EDGE_REALIZABLE_*|PM_EDGE_TRADEABILITY_*|PM_LCB_GLOBAL_*|PM_ENTRY_USABLE_EDGE_*" in text
