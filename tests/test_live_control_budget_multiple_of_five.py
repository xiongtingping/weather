"""The budget knob moves in whole multiples of 5 USDC.

Operator ruling, 2026-09-16: "顶部预算改为5的整数".  The board's stepper
lands every click on the grid, but the rule lives in the control server so a
value that did not come from the board cannot put an odd cent back.  A value
already off the grid (114.29, from before the ruling) is left as it is --
refusing to read it would blank the knob -- and only a new write must obey.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import live_control_server as control  # noqa: E402


def _bench(tmp_path: Path, monkeypatch) -> dict:
    """A control plane on temp files, wired to nothing that trades."""

    intent = tmp_path / "live_intent.json"
    intent.write_text(json.dumps({
        "schema": "basket_target_live_intent_v1",
        "live_enabled": False,
        # Explicit scope keeps live_stations off the production database.
        "station_scope": "explicit",
        "station_allowlist": ["ZBAA"],
        "minimum_settlements_to_qualify": 5,
    }), encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "portfolio_risk_fraction": 0.3,
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
    }), encoding="utf-8")
    runtime = tmp_path / "no_live_runtime.json"
    live_runtime = tmp_path / "live_runtime.json"
    for path, mode in ((runtime, "production_no_live"),
                       (live_runtime, "production_live")):
        path.write_text(json.dumps({
            "execution_mode": mode,
            "target_remaining_budget_usdc": 114.29,
        }), encoding="utf-8")
    audit = tmp_path / "audit.jsonl"

    monkeypatch.setattr(control, "INTENT_PATH", intent)
    monkeypatch.setattr(control, "POLICY_PATH", policy)
    monkeypatch.setattr(control, "RUNTIME_PATH", runtime)
    monkeypatch.setattr(control, "LIVE_RUNTIME_PATH", live_runtime)
    monkeypatch.setattr(control, "SSOT_PATH", tmp_path / "absent.db")
    monkeypatch.setattr(control, "EXCLUSION_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(control, "PAUSE_FLAG_PATH", tmp_path / "paused.flag")
    monkeypatch.setattr(control, "AUDIT_PATH", audit)
    monkeypatch.setattr(control, "_EXECUTION_MODE", "production_live")
    return {"runtime": runtime, "live_runtime": live_runtime, "audit": audit}


@pytest.mark.parametrize("value", [114.29, 112.5, 1.0, 64.99999])
def test_an_off_grid_budget_is_refused_and_nothing_is_written(
    tmp_path, monkeypatch, value
) -> None:
    bench = _bench(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="multiple of 5"):
        control.set_budget(value)

    for path in (bench["runtime"], bench["live_runtime"]):
        assert json.loads(path.read_text())["target_remaining_budget_usdc"] == 114.29
    assert not bench["audit"].exists()


@pytest.mark.parametrize("value", [0.0, 5.0, 110.0, 115.0, 2000.0])
def test_a_multiple_of_five_is_written_to_both_twins(
    tmp_path, monkeypatch, value
) -> None:
    bench = _bench(tmp_path, monkeypatch)

    out = control.set_budget(value)

    for path in (bench["runtime"], bench["live_runtime"]):
        assert json.loads(path.read_text())["target_remaining_budget_usdc"] == value
    assert out["budget"]["target_remaining_budget_usdc"] == value
    assert out["budget"]["multiple"] == 5.0


def test_the_state_still_reads_a_value_from_before_the_ruling(
    tmp_path, monkeypatch
) -> None:
    _bench(tmp_path, monkeypatch)

    budget = control.state()["budget"]

    assert budget["target_remaining_budget_usdc"] == 114.29
    assert budget["step"] == 5.0
