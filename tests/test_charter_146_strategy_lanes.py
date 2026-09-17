"""Charter 146: one switch per strategy lane (user ruling 2026-09-18).

The tiebreak lane goes live and every other strategy is paused -- closed,
not deleted.  The basket's BUYs are held at the send with the reason on the
event; the two sell lanes and the tiebreak lane are skipped or run by the
daemon on the same switches.  A missing file is the defaults; an unreadable
one pauses everything, because this file authorises spending.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import order_submission as sub  # noqa: E402
from bayes_basket_target import strategy_lanes as lanes  # noqa: E402


def _lanes_file(tmp_path: Path, table: dict, name: str = "lanes.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"schema": lanes.SCHEMA, "lanes": table}), encoding="utf-8")
    return path


def test_missing_is_the_defaults_and_unreadable_pauses_everything(tmp_path) -> None:
    absent = lanes.load_strategy_lanes(tmp_path / "absent.json")
    assert absent["source"] == "defaults"
    assert all(absent["lanes"][name]["enabled"] is True for name in lanes.KNOWN_LANES)
    assert absent["lanes"]["tiebreak"]["stake_usdc"] == 5.0
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert lanes.load_strategy_lanes(broken)["source"] == "unreadable"
    assert all(lanes.lane_enabled(name, broken) is False for name in lanes.KNOWN_LANES)
    shapeless = _lanes_file(tmp_path, {"basket": True}, "shapeless.json")
    assert lanes.load_strategy_lanes(shapeless)["source"] == "unreadable"


def test_the_switch_is_read_fresh_and_only_true_is_on(tmp_path) -> None:
    path = _lanes_file(tmp_path, {"basket": {"enabled": False}, "tiebreak": {"enabled": True, "stake_usdc": 7.5}})
    assert lanes.lane_enabled("basket", path) is False
    assert lanes.lane_enabled("tiebreak", path) is True
    assert lanes.lane_settings("tiebreak", path)["stake_usdc"] == 7.5
    # Lanes the file does not name keep their defaults.
    assert lanes.lane_enabled("certain_win_recycle", path) is True
    path.write_text(json.dumps({"lanes": {"basket": {"enabled": "yes"}}}), encoding="utf-8")
    assert lanes.lane_enabled("basket", path) is False
    path.write_text(json.dumps({"lanes": {"basket": {"enabled": True}}}), encoding="utf-8")
    assert lanes.lane_enabled("basket", path) is True
    with pytest.raises(ValueError):
        lanes.lane_enabled("no_such_lane", path)


def test_the_env_override_points_at_a_copy(tmp_path, monkeypatch) -> None:
    path = _lanes_file(tmp_path, {"basket": {"enabled": False}})
    monkeypatch.setenv(lanes.ENV_VAR, str(path))
    assert lanes.config_path() == path
    assert lanes.lane_enabled("basket") is False


def test_the_served_file_pauses_the_three_and_runs_the_tiebreak() -> None:
    """The operator file as committed: the ruling itself."""

    served = lanes.load_strategy_lanes(lanes.DEFAULT_PATH)
    assert served["source"] == "file"
    assert served["lanes"]["basket"]["enabled"] is False
    assert served["lanes"]["certain_win_recycle"]["enabled"] is False
    assert served["lanes"]["mis_bought_leg_exit"]["enabled"] is False
    assert served["lanes"]["tiebreak"]["enabled"] is True
    assert served["lanes"]["tiebreak"]["stake_usdc"] == 5.0
    assert served["lanes"]["tiebreak"]["min_market_share_on_model_mode"] == 0.40


# ---------------------------------------------------------------------------
# The basket's BUYs are held at the send.


def _book(path: Path) -> sqlite3.Connection:
    """One prepared live BUY and one prepared SELL on production's own schema
    (the kill-switch test's fixture, with the live flag up)."""

    from bayes_basket_target.ssot import PRODUCTION_LEDGER_DDL  # noqa: PLC0415

    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(PRODUCTION_LEDGER_DDL)
    for intent_id, action, bucket, token in (("i_buy", "BUY", "31°C", "tok31"), ("i_sell", "SELL", "32°C", "tok32")):
        conn.execute(
            "INSERT INTO basket_target_order_intents("
            " order_intent_id, decision_id, cycle_id, bucket, token_id, side,"
            " order_action, limit_price, requested_size,"
            " requested_notional_usdc, state, live_submit_authorized,"
            " idempotency_key, intent_json, intent_sha256, created_at)"
            " VALUES(?,'d1','c1',?,?,'YES',?,0.02,25.0,0.5,"
            " 'prepared',1,?,?,?,'2026-09-01T00:00:00Z')",
            (intent_id, bucket, token, action, f"k_{intent_id}",
             '{"execution_candidate": "MAKER", "post_only": true, "order_type": "GTD", "expiration": 1789088400}',
             "a" * 64),
        )
    return conn


def test_a_paused_basket_lane_holds_every_buy_and_names_the_switch(tmp_path, monkeypatch) -> None:
    conn = _book(tmp_path / "book.db")
    armed = tmp_path / "intent.json"
    armed.write_text(json.dumps({"live_enabled": True}), encoding="utf-8")
    monkeypatch.setattr(sub, "LIVE_INTENT_PATH", armed)
    monkeypatch.setenv(lanes.ENV_VAR, str(_lanes_file(tmp_path, {"basket": {"enabled": False}})))
    monkeypatch.setattr(sub, "live_station_admission", lambda station, conn=None: {"admitted": True, "reason": "test"})
    sent: list = []

    def transport(path, payload):
        sent.append(payload["order_intent_id"])
        return {"ok": True, "accepted": True, "exchange_order_id": "0x1"}

    receipt = sub.submit_prepared_intents(
        conn, cycle_id="c1", transport=transport, live_submit_authorized=True,
        observed_at_utc="2026-09-01T00:00:00Z",
    )
    assert receipt["strategy_lane_basket_enabled"] is False
    by_id = {row["order_intent_id"]: row for row in receipt["submissions"]}
    # The BUY never reaches the transport, ends failed, and the event names the switch.
    assert by_id["i_buy"]["reason"] == "strategy_lane_paused:basket"
    assert by_id["i_buy"]["exchange_post_started"] is False
    assert conn.execute("SELECT state FROM basket_target_order_intents WHERE order_intent_id='i_buy'").fetchone()[0] == "failed"
    event = conn.execute(
        "SELECT event_type, event_json FROM basket_target_order_events WHERE order_intent_id='i_buy'"
    ).fetchone()
    assert event[0] == "NEVER_REACHED_EXCHANGE" and "strategy_lane_paused:basket" in event[1]
    # The pause refuses admissions only: the exit still goes out.
    assert sent == ["i_sell"]
    # And it is told apart from the arming flag and the contract.
    assert receipt["live_intent_armed"] is True and receipt["live_submit_authorized"] is True


def test_an_enabled_basket_lane_changes_nothing(tmp_path, monkeypatch) -> None:
    conn = _book(tmp_path / "book2.db")
    armed = tmp_path / "intent.json"
    armed.write_text(json.dumps({"live_enabled": True}), encoding="utf-8")
    monkeypatch.setattr(sub, "LIVE_INTENT_PATH", armed)
    monkeypatch.setenv(lanes.ENV_VAR, str(tmp_path / "absent.json"))
    monkeypatch.setattr(sub, "live_station_admission", lambda station, conn=None: {"admitted": True, "reason": "test"})
    sent: list = []
    receipt = sub.submit_prepared_intents(
        conn, cycle_id="c1",
        transport=lambda path, payload: (sent.append(payload["order_intent_id"]) or {"ok": True, "accepted": True, "exchange_order_id": "0x2"}),
        live_submit_authorized=True, observed_at_utc="2026-09-01T00:00:00Z",
    )
    assert receipt["strategy_lane_basket_enabled"] is True
    assert sorted(sent) == ["i_buy", "i_sell"]


# ---------------------------------------------------------------------------
# The daemon runs each lane on its switch.


def test_the_daemon_skips_paused_lanes_and_runs_the_tiebreak(tmp_path, monkeypatch) -> None:
    import weather_basket_yes_daemon as daemon  # noqa: PLC0415

    from bayes_basket_target.ssot import PRODUCTION_LEDGER_DDL  # noqa: PLC0415

    conn = sqlite3.connect(tmp_path / "target.db", isolation_level=None)
    conn.executescript(PRODUCTION_LEDGER_DDL)

    class Worker:
        commit_conn = conn
        canonical_source_conn = None

        def run(self):
            return {"ok": True, "state": "committed"}

    monkeypatch.setattr(daemon, "PRODUCTION_EXECUTION_MODE", "production_live")
    monkeypatch.setattr(daemon, "assert_connection_target_capacity", lambda connection, recover_oversized_wal=True: None)
    ran: list = []

    def fake_execute(connection, canonical, *, transport, now_utc, live_submit_authorized):
        ran.append((connection is conn, live_submit_authorized))
        return {"schema": "tiebreak_lane_receipt_v1", "evaluated": 0}

    import bayes_basket_target.tiebreak_lane as tiebreak  # noqa: PLC0415

    monkeypatch.setattr(tiebreak, "execute_tiebreak_entries", fake_execute)
    monkeypatch.setenv(lanes.ENV_VAR, str(_lanes_file(tmp_path, {
        "basket": {"enabled": False}, "certain_win_recycle": {"enabled": False},
        "mis_bought_leg_exit": {"enabled": False}, "tiebreak": {"enabled": True},
    })))
    result = daemon.run_target_production_cycle(Worker())
    assert result["certain_win_recycle"] == {"state": "lane_paused", "lane": "certain_win_recycle"}
    assert result["mis_bought_leg_exit"] == {"state": "lane_paused", "lane": "mis_bought_leg_exit"}
    assert result["tiebreak_lane"]["schema"] == "tiebreak_lane_receipt_v1"
    assert ran == [(True, True)]
    # Flip the tiebreak switch: the lane is skipped and says so.
    monkeypatch.setenv(lanes.ENV_VAR, str(_lanes_file(tmp_path, {"tiebreak": {"enabled": False}}, "off.json")))
    result = daemon.run_target_production_cycle(Worker())
    assert result["tiebreak_lane"] == {"state": "lane_paused", "lane": "tiebreak"}
    assert len(ran) == 1


# ---------------------------------------------------------------------------
# The board's dial and lane switches (control server).


def _control(tmp_path, monkeypatch):
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import live_control_server as control  # noqa: PLC0415

    intent = tmp_path / "live_intent.json"
    intent.write_text(json.dumps({"schema": "basket_target_live_intent_v1", "live_enabled": False,
                                  "station_scope": "explicit", "station_allowlist": [],
                                  "minimum_settlements_to_qualify": 5}), encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"portfolio_risk_fraction": 0.3, "authority_scope": "x"}), encoding="utf-8")
    runtime = tmp_path / "no_live_runtime.json"
    live_runtime = tmp_path / "live_runtime.json"
    for path, mode in ((runtime, "production_no_live"), (live_runtime, "production_live")):
        path.write_text(json.dumps({"execution_mode": mode, "target_remaining_budget_usdc": 100.0}), encoding="utf-8")
    lanes_path = _lanes_file(tmp_path, {"basket": {"enabled": False, "note": "kept"}, "tiebreak": {"enabled": True, "stake_usdc": 5.0}}, "lanes_ctl.json")
    monkeypatch.setattr(control, "INTENT_PATH", intent)
    monkeypatch.setattr(control, "POLICY_PATH", policy)
    monkeypatch.setattr(control, "RUNTIME_PATH", runtime)
    monkeypatch.setattr(control, "LIVE_RUNTIME_PATH", live_runtime)
    monkeypatch.setattr(control, "SSOT_PATH", tmp_path / "absent.db")
    monkeypatch.setattr(control, "EXCLUSION_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(control, "PAUSE_FLAG_PATH", tmp_path / "paused.flag")
    monkeypatch.setattr(control, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(control, "STRATEGY_LANES_PATH", lanes_path)
    monkeypatch.setattr(control, "_EXECUTION_MODE", "production_live")
    return control, lanes_path


def test_the_stake_dial_writes_the_lane_file_and_keeps_every_other_field(tmp_path, monkeypatch) -> None:
    control, lanes_path = _control(tmp_path, monkeypatch)
    before = control.state()
    assert before["tiebreak_stake"]["stake_usdc"] == 5.0
    assert before["tiebreak_stake"]["bounds"] == [1.0, 100.0]
    assert before["strategy_lanes"]["lanes"]["basket"]["enabled"] is False
    after = control.set_tiebreak_stake(7)
    assert after["tiebreak_stake"]["stake_usdc"] == 7.0
    doc = json.loads(lanes_path.read_text(encoding="utf-8"))
    assert doc["lanes"]["tiebreak"]["stake_usdc"] == 7.0
    assert doc["lanes"]["basket"] == {"enabled": False, "note": "kept"}
    assert doc["lanes"]["tiebreak"]["enabled"] is True
    assert "updated_at_utc" in doc
    # And the lane reads exactly what the board wrote.
    assert lanes.lane_settings("tiebreak", lanes_path)["stake_usdc"] == 7.0
    for bad in (0.5, 101, "x", float("nan")):
        with pytest.raises(ValueError):
            control.set_tiebreak_stake(bad)
    assert json.loads(lanes_path.read_text(encoding="utf-8"))["lanes"]["tiebreak"]["stake_usdc"] == 7.0


def test_a_lane_switch_flips_one_lane_and_is_audited(tmp_path, monkeypatch) -> None:
    control, lanes_path = _control(tmp_path, monkeypatch)
    out = control.set_lane_enabled("basket", True, by="test")
    assert out["strategy_lanes"]["lanes"]["basket"]["enabled"] is True
    assert lanes.lane_enabled("basket", lanes_path) is True
    assert lanes.lane_enabled("tiebreak", lanes_path) is True
    out = control.set_lane_enabled("tiebreak", False)
    assert out["tiebreak_stake"]["enabled"] is False
    assert lanes.lane_enabled("tiebreak", lanes_path) is False
    with pytest.raises(ValueError):
        control.set_lane_enabled("no_such_lane", True)
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(a.get("knob") == "strategy_lane:basket" for a in audit)


def test_an_unreadable_lane_file_is_not_edited_into_shape(tmp_path, monkeypatch) -> None:
    control, lanes_path = _control(tmp_path, monkeypatch)
    lanes_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        control.set_tiebreak_stake(5)
    assert lanes_path.read_text(encoding="utf-8") == "{not json"
