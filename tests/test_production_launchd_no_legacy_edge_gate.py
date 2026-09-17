from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETIRED_PLISTS = (
    ROOT / "launchd" / "com.weather.trade_engine.plist",
    ROOT / "launchd" / "com.xiong.weather.ui.plist",
)


def test_retired_trade_engine_and_ui_plists_are_absent() -> None:
    assert all(not plist.exists() for plist in RETIRED_PLISTS)
