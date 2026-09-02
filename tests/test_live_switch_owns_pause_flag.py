"""The LIVE switch is the whole control, in both directions.

Two gates stopped a live order and both belonged to the operator: the intent
flag in ``config/trading/live_intent_v1.json`` and ``data/live_paused.flag``,
which the gateway reads at every place-order.  The switch moved only the
first, so the board could say ARMED over a gateway refusing every order --
which is what it said on 2026-09-02, in those words: "合同已 live · 暂停旗在位,
网关不发单".  Operator ruling the same day: turning the switch on is the whole
act of going live.

What is asserted here is the behaviour that ruling requires, and the ordering
that makes it safe: every intermediate state between the two writes is a
stopped one, in both directions.
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


def _bench(tmp_path: Path, monkeypatch, *, armed: bool, paused: bool) -> dict:
    """A control plane on temp files, wired to nothing that trades."""

    intent = tmp_path / "live_intent.json"
    intent.write_text(json.dumps({
        "schema": "basket_target_live_intent_v1",
        "live_enabled": armed,
        "armed_by": "dashboard" if armed else None,
        "armed_at_utc": "2026-09-02T00:00:00Z" if armed else None,
        # Explicit scope keeps live_stations off the production database:
        # this file is about the two gates, not about membership.
        "station_scope": "explicit",
        "station_allowlist": ["ZBAA"],
        "minimum_settlements_to_qualify": 5,
    }), encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "portfolio_risk_fraction": 0.35,
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
    }), encoding="utf-8")
    runtime = tmp_path / "no_live_runtime.json"
    runtime.write_text(json.dumps({
        "execution_mode": "production_no_live",
        "target_remaining_budget_usdc": 114.29,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
    }), encoding="utf-8")
    live_runtime = tmp_path / "live_runtime.json"
    live_runtime.write_text(json.dumps({
        "execution_mode": "production_live",
        "target_remaining_budget_usdc": 114.29,
        "live_submit_authorized": True,
        "places_orders": True,
        "calls_order_gateway": True,
    }), encoding="utf-8")
    flag = tmp_path / "live_paused.flag"
    if paused:
        flag.touch()
    audit = tmp_path / "audit.jsonl"

    monkeypatch.setattr(control, "INTENT_PATH", intent)
    monkeypatch.setattr(control, "POLICY_PATH", policy)
    monkeypatch.setattr(control, "RUNTIME_PATH", runtime)
    monkeypatch.setattr(control, "LIVE_RUNTIME_PATH", live_runtime)
    monkeypatch.setattr(control, "STATION_CAP_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(control, "EXCLUSION_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(control, "PAUSE_FLAG_PATH", flag)
    monkeypatch.setattr(control, "AUDIT_PATH", audit)
    monkeypatch.setattr(control, "_EXECUTION_MODE", "production_live")
    return {"intent": intent, "flag": flag, "audit": audit}


def _audit_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line
    ]


def test_arming_lifts_the_gateway_pause_flag(tmp_path, monkeypatch) -> None:
    """One click, not a click and then a file to remember deleting."""

    bench = _bench(tmp_path, monkeypatch, armed=False, paused=True)
    out = control.set_live(True, "LIVE", "dashboard")

    assert bench["flag"].exists() is False
    assert json.loads(bench["intent"].read_text())["live_enabled"] is True
    assert out["live"]["enabled"] is True
    assert out["live"]["submission_paused"] is False
    assert out["live"]["submitting"] is True


def test_disarming_places_it_back(tmp_path, monkeypatch) -> None:
    """The stop has to reach the gateway, not only the worker's flag.

    On 2026-08-31 the flag went down at 13:29 and a real order left the
    process at 21:25, because the gateway was never told.
    """

    bench = _bench(tmp_path, monkeypatch, armed=True, paused=False)
    out = control.set_live(False, "", "dashboard")

    assert bench["flag"].exists() is True
    assert json.loads(bench["intent"].read_text())["live_enabled"] is False
    assert out["live"]["enabled"] is False
    assert out["live"]["submission_paused"] is True
    assert out["live"]["submitting"] is False


def test_the_intermediate_state_is_stopped_in_both_directions(
    tmp_path, monkeypatch
) -> None:
    """The ordering is the safety property, so it is asserted directly.

    A crash between the two writes must not leave a gateway that sends
    without an operator who said so.  Arming writes the intent while the
    pause is still up; disarming places the pause before the intent moves.
    """

    seen: list[bool] = []
    original = control._write_atomic

    def watch(path, payload):
        if path == control.INTENT_PATH:
            seen.append(control.PAUSE_FLAG_PATH.is_file())
        original(path, payload)

    bench = _bench(tmp_path, monkeypatch, armed=False, paused=True)
    monkeypatch.setattr(control, "_write_atomic", watch)
    control.set_live(True, "LIVE", "dashboard")
    # Arming: paused while the intent is written, so the window is
    # armed-and-paused, which sends nothing.
    assert seen == [True]

    seen.clear()
    control.set_live(False, "", "dashboard")
    # Disarming: already paused when the intent is written, so the window is
    # paused-and-armed, which also sends nothing.
    assert seen == [True]
    assert bench["flag"].exists() is True


def test_a_refused_arm_moves_neither_gate(tmp_path, monkeypatch) -> None:
    """No token, no arm -- and no half-opened gateway either."""

    bench = _bench(tmp_path, monkeypatch, armed=False, paused=True)
    with pytest.raises(ValueError):
        control.set_live(True, "", "dashboard")

    assert bench["flag"].exists() is True
    assert json.loads(bench["intent"].read_text())["live_enabled"] is False
    assert _audit_rows(bench["audit"]) == []


def test_the_audit_records_both_gates(tmp_path, monkeypatch) -> None:
    """The trail has to say what moved, or the switch is unaccountable."""

    bench = _bench(tmp_path, monkeypatch, armed=False, paused=True)
    control.set_live(True, "LIVE", "operator")
    row = _audit_rows(bench["audit"])[-1]

    assert row["knob"] == "live_enabled"
    assert (row["before"], row["after"]) == (False, True)
    assert row["by"] == "operator"
    assert row["pause_flag_before"] is True
    assert row["pause_flag_after"] is False
    assert row["pause_flag_moved"] is True


def test_an_arm_that_cannot_lift_the_pause_is_not_a_success(
    tmp_path, monkeypatch
) -> None:
    """Armed-and-paused is stopped, so it must not answer like a go.

    This is the exact state the change exists to end: a switch reading
    ARMED over a gateway that refuses every order.  If the lift fails the
    caller is told, the trail carries the reason, and the derived answer
    still says nothing is being submitted.
    """

    bench = _bench(tmp_path, monkeypatch, armed=False, paused=True)

    def refuse():
        raise PermissionError("read-only data directory")

    monkeypatch.setattr(control, "_lift_pause", refuse)
    with pytest.raises(RuntimeError, match="nothing will be sent"):
        control.set_live(True, "LIVE", "dashboard")

    assert bench["flag"].exists() is True
    row = _audit_rows(bench["audit"])[-1]
    assert row["pause_flag_after"] is True
    assert "PermissionError" in row["pause_flag_error"]
    assert control.state()["live"]["submitting"] is False


def test_a_hand_placed_flag_still_stops_everything(
    tmp_path, monkeypatch
) -> None:
    """The e-stop survives: the switch owns the flag, it did not replace it.

    An operator who touches the file while the switch says ARMED gets the
    system stopped, and the state says the two disagree rather than picking
    one of them to believe.
    """

    bench = _bench(tmp_path, monkeypatch, armed=True, paused=False)
    assert control.state()["live"]["submitting"] is True

    bench["flag"].touch()
    after = control.state()["live"]
    assert after["enabled"] is True
    assert after["submission_paused"] is True
    assert after["submitting"] is False


def test_the_contract_block_reads_the_file_the_mode_selects(
    tmp_path, monkeypatch
) -> None:
    """Reading the no-live twin under a live contract inverts the answer.

    The panel's arming text is computed from these three fields, so a block
    that always read the no-live file described a system that had stopped
    being that system -- the same mistake production_worker.py already
    carries a comment about.  The payload names the file it read.
    """

    _bench(tmp_path, monkeypatch, armed=False, paused=True)
    live = control.state()["contracts"]
    assert live["runtime_file"] == "live_runtime.json"
    assert live["live_submit_authorized"] is True

    monkeypatch.setattr(control, "_EXECUTION_MODE", "production_no_live")
    no_live = control.state()["contracts"]
    assert no_live["runtime_file"] == "no_live_runtime.json"
    assert no_live["live_submit_authorized"] is False
