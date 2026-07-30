from __future__ import annotations

import json
from pathlib import Path

import ui.server as server


def _write_config(path: Path, *, state: str = "disabled", budget: float = 30.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "weather_copy_follow_config_v1",
                "strategy_id": "weather_copy_follow_v1",
                "state": state,
                "city_cap_usdc": budget,
                "source": {"user": "0xsource"},
            }
        ),
        encoding="utf-8",
    )


def test_copy_follow_ui_control_isolated_from_auto_trade_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config" / "copy_follow_v1.json"
    pause_path = tmp_path / "data" / "copy_follow_paused.flag"
    _write_config(config_path)
    monkeypatch.setattr(server, "COPY_FOLLOW_CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "COPY_FOLLOW_PAUSE_FILE", pause_path)

    initial = server._copy_follow_effective_payload()
    assert initial["enabled"] is False
    assert initial["city_cap_usdc"] == 30.0
    assert initial["budget_options_usdc"] == [30.0, 50.0, 100.0, 200.0]

    enabled = server._set_copy_follow_config({"enabled": ["1"], "city_cap_usdc": ["100"]})
    assert enabled["enabled"] is True
    assert enabled["city_cap_usdc"] == 100.0
    assert not pause_path.exists()

    for budget in (30, 50, 100, 200, 30):
        updated = server._set_copy_follow_config({"city_cap_usdc": [str(budget)]})
        assert updated["ok"] is True
        assert updated["city_cap_usdc"] == float(budget)

    pause_path.parent.mkdir(parents=True, exist_ok=True)
    pause_path.write_text("disabled\n", encoding="utf-8")
    assert server._copy_follow_effective_payload()["enabled"] is False
    pause_path.unlink()
    assert server._copy_follow_effective_payload()["enabled"] is True

    disabled = server._set_copy_follow_config({"enabled": ["0"]})
    assert disabled["enabled"] is False
    assert pause_path.exists()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["state"] == "disabled"
    assert saved["city_cap_usdc"] == 30.0

    for invalid_budget in ("0", "25", "201", "NaN", ""):
        assert server._set_copy_follow_config({"city_cap_usdc": [invalid_budget]})["ok"] is False
    assert server._set_copy_follow_config({"enabled": ["maybe"]})["ok"] is False


def test_copy_follow_controls_are_present_in_main_ui():
    html = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'id="copyFollowEnabled"' in html
    assert 'data-budget="30"' in html
    assert 'data-budget="50"' in html
    assert 'data-budget="100"' in html
    assert 'data-budget="200"' in html
    assert "/api/copy-follow-config" in html
    assert "不影响 Bayes 自动交易" in html
