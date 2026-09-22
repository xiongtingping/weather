"""Charter 197 amendment: no new order while the egress is red; nothing at the venue is touched.

User ruling 2026-09-22 (「网络闪断期间暂停发新单：出口红灯时不发新单，已有的单不动」).
01:26-01:31Z that morning the resident posted into a tunnel that was up five
minutes out of fifteen hours: two UNKNOWN_POSTs, one still reconcile_required
hours later.  These tests hold the gate to the ruling: red holds every lane's
new order and records why; amber does not; a missing or stale watch never
holds (it is reported instead); release is the next read after the watch
turns; and nothing already at the venue is written to.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import certain_win_recycle as recycle  # noqa: E402
from bayes_basket_target import exit_orders  # noqa: E402
from bayes_basket_target import order_submission as sub  # noqa: E402
from bayes_basket_target import supply_watch as sw  # noqa: E402
from bayes_basket_target import tiebreak_lane  # noqa: E402
from bayes_basket_target.lane_orders import lane_decision_id, send_lane_buy  # noqa: E402
from bayes_basket_target.ssot import PRODUCTION_LEDGER_DDL  # noqa: E402

NOW = datetime.now(timezone.utc)


def _watch(path: Path, *, egress: str, age_sec: float = 30.0) -> Path:
    """A watch file as the sweep writes it, ``age_sec`` old."""

    rows = []
    if egress == "red":
        rows.append({"code": "egress_down", "health": "red", "label": "外网出口", "note": "x"})
    elif egress == "amber":
        rows.append({"code": "egress_partial", "health": "amber", "label": "外网出口", "note": "x"})
    path.write_text(json.dumps({
        "schema": sw.SCHEMA,
        "generated_at_utc": (NOW - timedelta(seconds=age_sec)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alerts": rows,
        "egress_health": {"red": "red", "amber": "amber"}.get(egress, "green"),
        "egress": {"state": {"red": "down", "amber": "partial"}.get(egress, "ok"),
                   "since_utc": "2026-09-21T16:05:00Z"},
    }))
    return path


@pytest.fixture()
def watch(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "supply_watch.json"
    monkeypatch.setenv(sw.WATCH_PATH_ENV, str(path))
    return path


# ---------------------------------------------------------------- the verdict


def test_only_a_fresh_red_egress_holds(watch) -> None:
    _watch(watch, egress="red")
    verdict = sw.egress_send_gate()
    assert verdict["hold"] is True and verdict["reason"] == "egress_red_hold"
    assert verdict["watch"] == "fresh" and verdict["egress_state"] == "down"

    _watch(watch, egress="amber")
    assert sw.egress_send_gate()["hold"] is False  # amber does not hold

    _watch(watch, egress="green")
    assert sw.egress_send_gate()["hold"] is False


def test_a_red_p0_supply_row_is_not_an_egress_hold(watch) -> None:
    """The ruling is about the network.  D+1 P0 expiring is red on the board
    and holds nothing: that is a missing forecast, not a dead line."""

    watch.write_text(json.dumps({
        "generated_at_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alerts": [{"code": "p0_fleet_expired_2026-09-23", "health": "red"}],
    }))
    assert sw.egress_send_gate()["hold"] is False


def test_a_silent_watch_never_holds_and_says_so(watch) -> None:
    verdict = sw.egress_send_gate()  # file missing
    assert verdict["hold"] is False and verdict["watch"] == "missing" and verdict["watch_invalid"]

    _watch(watch, egress="red", age_sec=sw.WATCH_FRESH_SEC + 60)
    verdict = sw.egress_send_gate()
    assert verdict["hold"] is False and verdict["watch"] == "stale" and verdict["watch_invalid"]

    watch.write_text("{not json")
    verdict = sw.egress_send_gate()
    assert verdict["hold"] is False and verdict["watch"] == "unreadable"


def test_the_freshness_bound_outlives_two_missed_sweeps_and_not_the_board_cadence() -> None:
    """After the restart the sweep writes every 300 s; three passes fit the
    bound, so one or two missed passes do not turn the gate off.  The board's
    own stand-in pass runs every 1800 s -- a red it wrote goes stale after
    fifteen minutes rather than holding orders for the rest of its half hour."""

    assert 3 * 300.0 <= sw.WATCH_FRESH_SEC < 1800.0


# ---------------------------------------------------------------- basket submit


def _book(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(PRODUCTION_LEDGER_DDL)
    rows = (
        ("i_buy", "BUY", "31°C", "tok31", "prepared"),
        ("i_sell", "SELL", "32°C", "tok32", "prepared"),
        # Already at the venue: the gate must never touch it.
        ("i_resting", "BUY", "33°C", "tok33", "submitted"),
    )
    for intent_id, action, bucket, token, state in rows:
        conn.execute(
            "INSERT INTO basket_target_order_intents("
            " order_intent_id, decision_id, cycle_id, bucket, token_id, side,"
            " order_action, limit_price, requested_size,"
            " requested_notional_usdc, state, live_submit_authorized,"
            " idempotency_key, intent_json, intent_sha256, created_at)"
            " VALUES(?,'d1','c1',?,?,'YES',?,0.02,25.0,0.5,?,1,?,?,?,'2026-09-01T00:00:00Z')",
            (intent_id, bucket, token, action, state, f"k_{intent_id}",
             '{"execution_candidate": "MAKER", "post_only": true, "order_type": "GTD", "expiration": 1789088400}',
             "a" * 64),
        )
    return conn


@pytest.fixture()
def armed(tmp_path, monkeypatch) -> None:
    flag = tmp_path / "intent.json"
    flag.write_text(json.dumps({"live_enabled": True}), encoding="utf-8")
    monkeypatch.setattr(sub, "LIVE_INTENT_PATH", flag)
    monkeypatch.setenv("PM_STRATEGY_LANES_CONFIG", str(tmp_path / "absent_lanes.json"))
    monkeypatch.setattr(sub, "live_station_admission", lambda station, conn=None: {"admitted": True, "reason": "test"})


def _submit(conn, sent):
    return sub.submit_prepared_intents(
        conn, cycle_id="c1",
        transport=lambda path, payload: (sent.append(payload["order_intent_id"])
                                         or {"ok": True, "accepted": True, "exchange_order_id": "0x9"}),
        live_submit_authorized=True, observed_at_utc="2026-09-22T01:30:00Z",
    )


def _events(conn, intent_id):
    return [
        (row[0], json.loads(row[1]))
        for row in conn.execute(
            "SELECT event_type, event_json FROM basket_target_order_events WHERE order_intent_id=?",
            (intent_id,),
        )
    ]


def test_red_holds_every_new_basket_order_and_records_why(tmp_path, watch, armed) -> None:
    conn = _book(tmp_path / "book.db")
    _watch(watch, egress="red")
    sent: list = []
    receipt = _submit(conn, sent)

    assert sent == []  # nothing reached the transport, BUY or SELL
    assert receipt["egress_gate"]["hold"] is True
    for intent_id in ("i_buy", "i_sell"):
        assert conn.execute(
            "SELECT state FROM basket_target_order_intents WHERE order_intent_id=?", (intent_id,)
        ).fetchone()[0] == "failed"
        [(event_type, body)] = _events(conn, intent_id)
        assert event_type == "NEVER_REACHED_EXCHANGE"
        assert body["reason"] == "egress_red_hold"
        assert body["egress_gate"]["egress_state"] == "down"
    # The order already at the venue: not re-sent, not cancelled, not written.
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents WHERE order_intent_id='i_resting'"
    ).fetchone()[0] == "submitted"
    assert _events(conn, "i_resting") == []


def test_amber_and_a_stale_watch_send_and_the_stale_one_is_audited(tmp_path, watch, armed) -> None:
    conn = _book(tmp_path / "amber.db")
    _watch(watch, egress="amber")
    sent: list = []
    _submit(conn, sent)
    assert sorted(sent) == ["i_buy", "i_sell"]
    [(event_type, body)] = _events(conn, "i_buy")
    assert event_type == "POST_ACK" and "egress_gate" not in body

    conn = _book(tmp_path / "stale.db")
    _watch(watch, egress="red", age_sec=sw.WATCH_FRESH_SEC + 120)
    sent = []
    receipt = _submit(conn, sent)
    assert sorted(sent) == ["i_buy", "i_sell"]  # a silent watch never stops trading
    assert receipt["egress_gate"]["watch_invalid"] is True
    [(event_type, body)] = _events(conn, "i_buy")
    assert event_type == "POST_ACK" and body["egress_gate"]["watch"] == "stale"


def test_release_is_the_next_read_after_the_watch_turns(tmp_path, watch, armed) -> None:
    conn = _book(tmp_path / "release.db")
    _watch(watch, egress="red")
    sent: list = []
    _submit(conn, sent)
    assert sent == []
    # The next pass mints fresh intents (the held ones ended failed).
    for intent_id, bucket, token in (("i_buy2", "31°C", "tok31"),):
        conn.execute(
            "INSERT INTO basket_target_order_intents("
            " order_intent_id, decision_id, cycle_id, bucket, token_id, side,"
            " order_action, limit_price, requested_size,"
            " requested_notional_usdc, state, live_submit_authorized,"
            " idempotency_key, intent_json, intent_sha256, created_at)"
            " VALUES(?,'d1','c1',?,?,'YES','BUY',0.02,25.0,0.5,'prepared',1,?,?,?,'2026-09-01T00:00:01Z')",
            (intent_id, bucket, token, f"k_{intent_id}",
             '{"execution_candidate": "MAKER", "post_only": true, "order_type": "GTD", "expiration": 1789088400}',
             "a" * 64),
        )
    _watch(watch, egress="green")
    _submit(conn, sent)
    assert sent == ["i_buy2"]


# ---------------------------------------------------------------- side lanes


def _lane_book(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(PRODUCTION_LEDGER_DDL)
    return conn


def _lane_buy(conn, sent, intent_id="tb1"):
    return send_lane_buy(
        conn, lane="tiebreak", intent_id=intent_id, station_id="ZSPD", event_date="2026-09-23",
        settlement_timezone="Asia/Shanghai", bucket="27°C", token_id="t27",
        price=0.45, shares=10.0, execution_candidate="MAKER", expiration=1789088400,
        tick_size=0.01,
        intent_body={"order_type": "GTD", "post_only": True, "rule": "r", "plan": {"id": intent_id}},
        transport=lambda path, payload: (sent.append(payload["order_intent_id"])
                                         or {"ok": True, "accepted": True, "exchange_order_id": "0x1"}),
        live_submit_authorized=True,
    )


def test_a_side_lane_buy_is_held_and_does_not_spend_the_station_days_sends(
    tmp_path, watch, armed, monkeypatch,
) -> None:
    monkeypatch.setattr("bayes_basket_target.lane_orders.live_intent_armed", lambda: True)
    conn = _lane_book(tmp_path / "lane.db")
    _watch(watch, egress="red")
    sent: list = []
    entry = _lane_buy(conn, sent)
    assert sent == [] and entry["reason"] == "egress_red_hold"
    assert entry["exchange_post_started"] is False
    [(event_type, body)] = _events(conn, "tb1")
    assert event_type == "NEVER_REACHED_EXCHANGE" and body["reason"] == "egress_red_hold"
    # Minted and recorded -- and not one of the station-day's three sends.
    assert tiebreak_lane.station_day_sends_total(conn, "ZSPD", "2026-09-23") == 1
    assert tiebreak_lane.egress_held_sends(conn, "ZSPD", "2026-09-23") == 1
    assert tiebreak_lane.station_day_sends(conn, "ZSPD", "2026-09-23") == 0
    assert tiebreak_lane.station_day_attempted(conn, "ZSPD", "2026-09-23") is False
    # The row's refusal is plumbing: retried on any fresh book once released.
    assert tiebreak_lane.is_retryable_refusal("send:egress_red_hold")
    assert tiebreak_lane.retry_needs_newer_book("send:egress_red_hold") is False

    _watch(watch, egress="green")
    entry = _lane_buy(conn, sent, intent_id="tb2")
    assert sent == ["tb2"] and entry["exchange_post_started"] is True
    assert tiebreak_lane.station_day_sends(conn, "ZSPD", "2026-09-23") == 1


def test_an_exit_sell_is_held_and_its_leg_caps_are_not_spent(tmp_path, watch, monkeypatch) -> None:
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: True)
    conn = _lane_book(tmp_path / "exit.db")
    _watch(watch, egress="red")
    sent: list = []
    entry = exit_orders.send_exit_sell(
        conn, lane=recycle.INTENT_PREFIX, intent_id="rc1", station_id="WMKK",
        event_date="2026-09-22", settlement_timezone="Asia/Kuala_Lumpur", bucket="30°C",
        token_id="t30", price=0.99, shares=6.0, order_type="GTC", post_only=False,
        intent_body={"rule": "r"},
        transport=lambda path, payload: (sent.append(payload["order_intent_id"]) or {"ok": True}),
        live_submit_authorized=True, tick_size=0.01,
    )
    assert sent == [] and entry["reason"] == "egress_red_hold"
    own = recycle._own_sells(conn, station_id="WMKK", event_date="2026-09-22", bucket="30°C", counted=set())
    assert own["failed"] == 0 and own["sends"] == 0 and own["offered"] == 0.0


def test_the_recycle_lane_holds_before_minting(tmp_path, watch, monkeypatch) -> None:
    """It re-plans every cycle from what is unsold, so it waits rather than
    minting a held intent per leg per cycle through a long outage."""

    conn = _lane_book(tmp_path / "recycle.db")
    plan = {"order_intent_id": "recycle998_x", "station_id": "WMKK", "event_date": "2026-09-22",
            "bucket": "30°C"}
    monkeypatch.setattr(recycle, "plan_certain_win_recycles", lambda *a, **k: [plan])
    _watch(watch, egress="red")
    receipt = recycle.execute_certain_win_recycles(
        conn, None, transport=lambda *a: pytest.fail("no send while red"),
        now_utc="2026-09-22T01:30:00Z", live_submit_authorized=True,
        position_reader=lambda token: pytest.fail("no position read while red"),
    )
    assert receipt["sent"] == 0 and receipt["deferred_to_next_cycle"] == 1
    assert receipt["receipts"] == [{
        "order_intent_id": "recycle998_x", "state": "held", "reason": "egress_red_hold",
        "station_id": "WMKK", "event_date": "2026-09-22", "bucket": "30°C",
        "exchange_post_started": False,
    }]
    assert conn.execute("SELECT COUNT(*) FROM basket_target_order_intents").fetchone()[0] == 0


# ---------------------------------------------------------------- p1_tail lane


def test_a_held_tail_take_is_not_the_buckets_one_take(tmp_path, watch, monkeypatch) -> None:
    if str(ROOT / "tests") not in sys.path:
        sys.path.insert(0, str(ROOT / "tests"))
    import test_charter_166_p1_tail_lane as t166  # noqa: PLC0415
    from bayes_basket_target import p1_tail_lane as tail  # noqa: PLC0415
    from bayes_basket_target.market_registry import apply_target_market_registry_schema  # noqa: PLC0415
    from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: PLC0415

    conn = sqlite3.connect(tmp_path / "tail.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    flag = tmp_path / "intent.json"
    flag.write_text(json.dumps({"live_enabled": True}), encoding="utf-8")
    monkeypatch.setattr(sub, "LIVE_INTENT_PATH", flag)
    monkeypatch.setattr(tail, "settle_stuck_lane_cycles", lambda conn: 0)
    monkeypatch.setenv("PM_TARGET_MARKET_MANIFEST", str(tmp_path / "no_manifest.json"))
    t166._seed(conn)
    sent: list = []

    def transport(path, payload):
        sent.append(payload["order_intent_id"])
        return {"ok": True, "accepted": True, "exchange_order_id": "0x7", "status": "matched",
                "making_amount": "4.7", "taking_amount": "10"}

    _watch(watch, egress="red")
    held = tail.execute_p1_tail_entries(
        conn, None, transport=transport, now_utc=t166.NOW,
        live_submit_authorized=True, admission=t166.ADMIT,
    )
    assert sent == []
    [entry] = held["receipts"]
    assert entry["reason"] == "egress_red_hold"
    refusal = conn.execute(
        f"SELECT refusal FROM {tail.EVALUATION_TABLE} WHERE order_intent_id=?",
        (entry["order_intent_id"],),
    ).fetchone()[0]
    assert refusal == "send:egress_red_hold"
    assert tail.station_day_bucket_attempted(conn, t166.ST, t166.ED, "27°C") is False
    assert tail.station_day_sends(conn, t166.ST, t166.ED) == 0

    # Released on the next envelope: the same bucket is taken.
    _watch(watch, egress="green")
    t166._seed(conn, envelope_id="env2", decided="2026-09-19T03:59:00.000Z",
               observed="2026-09-19T03:59:50.000Z")
    released = tail.execute_p1_tail_entries(
        conn, None, transport=transport, now_utc=t166.NOW,
        live_submit_authorized=True, admission=t166.ADMIT,
    )
    assert len(sent) == 1, released


def test_the_hold_reason_is_the_gates_own_constant() -> None:
    assert sw.EGRESS_HOLD_REASON == "egress_red_hold"
    assert tiebreak_lane.SEND_EGRESS_HOLD_REFUSAL == "send:" + sw.EGRESS_HOLD_REASON
    assert lane_decision_id("tiebreak", "A", "B") == "tiebreak_decision_A_B"
