import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "egress_proxy_watchdog.py"


@pytest.fixture
def watchdog(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("egress_proxy_watchdog", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "proxy_endpoint", lambda: ("127.0.0.1", 1082))
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    calls = []
    monkeypatch.setattr(module, "revive", lambda: calls.append(1) or ["vpn start: ok"])
    module.calls = calls
    return module


def test_one_refusal_does_not_act_two_in_a_row_do(watchdog, monkeypatch):
    monkeypatch.setattr(watchdog, "port_listening", lambda h, p: False)
    watchdog.main()
    assert watchdog.calls == []
    watchdog.main()
    assert watchdog.calls == [1]


def test_attempts_are_spaced(watchdog, monkeypatch):
    monkeypatch.setattr(watchdog, "port_listening", lambda h, p: False)
    for _ in range(4):
        watchdog.main()
    assert watchdog.calls == [1]


def test_a_listening_port_resets_the_streak(watchdog, monkeypatch):
    up = {"v": False}
    monkeypatch.setattr(watchdog, "port_listening", lambda h, p: up["v"])
    watchdog.main()
    up["v"] = True
    watchdog.main()
    up["v"] = False
    watchdog.main()
    assert watchdog.calls == []
    assert watchdog.read_state()["refusals"] == 1
