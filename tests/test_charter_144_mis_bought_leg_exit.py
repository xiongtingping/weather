"""Charter 144: the legs the completion rule bought outside its own definition
are sold back, all of them, at the best bid (user rulings 2026-09-17: "卖",
then "不符合缺腿条件的全部卖出").
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import mis_bought_leg_exit as lane
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.execution_reconciliation import record_order_event
from bayes_basket_target.inventory_producer import ledger_holdings_by_token

ROOT = Path(__file__).resolve().parents[1]
ST, ED, TZ = "ZUUU", "2026-09-18", "Asia/Shanghai"
TOKENS = {"24°C": "t24", "25°C": "t25", "27°C": "t27"}
V2 = "broken_basket_completion_sent_legs_v2"


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> None:
    """Insert ``values``, filling every other NOT NULL column with a
    type-appropriate placeholder so the fixture survives the ledger's
    constraints without restating them."""

    row = dict(values)
    for _cid, name, ctype, notnull, default, _pk in conn.execute(f"PRAGMA table_info({table})"):
        if name in row or not notnull or default is not None:
            continue
        lname = str(name)
        if "sha256" in lname:
            row[name] = "a" * 64
        elif lname.endswith("_json"):
            row[name] = "{}"
        elif str(ctype).upper().startswith(("INT", "REAL", "NUM")):
            row[name] = 1
        else:
            row[name] = "x"
    columns = list(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        [row[c] for c in columns],
    )


def _buy(conn, *, cycle_id: str, decision_id: str, bucket: str, size: float, at: str,
         filled: float | None, events: tuple[str, ...]) -> str:
    intent_id = f"i_{cycle_id}_{bucket}"
    _insert(conn, "basket_target_order_intents", {
        "order_intent_id": intent_id, "decision_id": decision_id, "cycle_id": cycle_id,
        "bucket": bucket, "token_id": TOKENS[bucket], "side": "YES", "order_action": "BUY",
        "limit_price": 0.2, "requested_size": size, "requested_notional_usdc": 0.2 * size,
        "state": "submitted", "live_submit_authorized": 1, "idempotency_key": intent_id,
        "intent_json": json.dumps({}), "created_at": at,
    })
    for n, event in enumerate(events):
        record_order_event(conn, order_intent_id=intent_id, event={
            "event_type": event, "exchange_order_id": f"0x{intent_id}",
            "observed_at": at, "matched_size": float(filled or 0.0) if event == "FILL" else 0.0,
            "matched_notional_usdc": 0.2 * float(filled or 0.0) if event == "FILL" else 0.0,
            "event_payload": {},
        })
    return intent_id


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute("CREATE TABLE basket_policy_training_fact(station_id TEXT, event_date TEXT, created_at_utc TEXT, feature_json TEXT)")
    conn.execute("CREATE TABLE basket_target_market_registry(station_id TEXT, event_date TEXT, yes_token TEXT, registry_json TEXT)")
    for bucket, token in TOKENS.items():
        conn.execute("INSERT INTO basket_target_market_registry VALUES(?,?,?,?)", (ST, ED, token, json.dumps({"order_min_size": 5})))
    # The opening: 24C and 25C sent and filled on 2026-09-16.
    _insert(conn, "basket_target_execution_cycles", {"cycle_id": "c0", "plan_id": "p0", "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "strategy_lane": "basket", "requested_reserve_usdc": 1.0, "state": "settled", "created_at": "2026-09-16T22:15:14Z", "updated_at": "2026-09-16T22:15:14Z"})
    _insert(conn, "auto_trade_decisions", {"decision_id": "d0", "cycle_id": "c0", "plan_id": "p0", "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "strategy_lane": "basket", "decided_state": "submitted", "will_order": 1, "created_at": "2026-09-16T22:15:14Z"})
    _buy(conn, cycle_id="c0", decision_id="d0", bucket="24°C", size=15.0, at="2026-09-16T22:15:14Z", filled=15.0, events=("POST_ACK", "FILL"))
    _buy(conn, cycle_id="c0", decision_id="d0", bucket="25°C", size=6.0, at="2026-09-16T22:15:14Z", filled=6.0, events=("POST_ACK", "FILL"))
    # A completion at 08:43Z that bought 27C, never sent before: a mis-buy in full.
    for cycle, decision, at, bucket, size, filled in (
        ("c1", "d1", "2026-09-17T08:43:21.649Z", "27°C", 6.0, 6.208334),
        # And one at 09:00Z that bought 25C to 11 against the 6 it was sent at: 5 over.
        ("c2", "d2", "2026-09-17T09:00:00.000Z", "25°C", 11.0, 11.0),
    ):
        _insert(conn, "basket_target_execution_cycles", {"cycle_id": cycle, "plan_id": "p" + cycle, "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "strategy_lane": "basket", "requested_reserve_usdc": 1.0, "state": "settled", "created_at": at, "updated_at": at})
        _insert(conn, "auto_trade_decisions", {"decision_id": decision, "cycle_id": cycle, "plan_id": "p" + cycle, "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "strategy_lane": "basket", "decided_state": "submitted", "will_order": 1, "created_at": at})
        conn.execute("INSERT INTO basket_policy_training_fact VALUES(?,?,?,?)", (ST, ED, at, json.dumps({"plan": {"target_solution": {"winner_completion_rule": V2}}})))
        _buy(conn, cycle_id=cycle, decision_id=decision, bucket=bucket, size=size, at=at, filled=filled, events=("FILL",))
    return conn


def _bids(levels_by_bucket, age=100.0):
    def reader(conn, *, station_id, event_date, bucket, now_utc):
        return list(levels_by_bucket.get(bucket, [])), age
    return reader


def _claim(value):
    def reader(conn, *, station_id, event_date, bucket, now_utc):
        return value
    return reader


NOW = "2026-09-17T12:00:00.000Z"


def test_the_rule_ships_on() -> None:
    assert lane.MIS_BOUGHT_LEG_EXIT == "on"


def test_mis_bought_legs_are_the_never_sent_fill_and_the_excess_over_the_sent_size() -> None:
    legs = {(leg["bucket"]): leg for leg in lane.mis_bought_legs(_store())}
    assert set(legs) == {"27°C", "25°C"}
    assert legs["27°C"]["mis_bought_shares"] == pytest.approx(6.208334)
    assert legs["27°C"]["fills"][0]["kind"] == "never_sent"
    assert legs["25°C"]["mis_bought_shares"] == pytest.approx(5.0)
    assert legs["25°C"]["fills"][0]["kind"] == "over_sent_size"


def test_every_mis_bought_leg_is_sold_at_the_best_bid_whatever_the_model_says() -> None:
    conn = _store()
    out = lane.plan_mis_bought_leg_exits(
        conn, now_utc=NOW, claim_reader=_claim(0.9),
        bid_reader=_bids({"27°C": [(0.21, 7.4), (0.20, 50.0)], "25°C": [(0.19, 50.0)]}),
    )
    plans = {p["bucket"]: p for p in out["plans"]}
    assert set(plans) == {"27°C", "25°C"}
    assert plans["27°C"]["shares"] == pytest.approx(6.2) and plans["27°C"]["limit_price"] == 0.21
    assert plans["25°C"]["shares"] == pytest.approx(5.0) and plans["25°C"]["limit_price"] == 0.19
    assert plans["27°C"]["claim"] == 0.9  # evidence, not a gate


def test_no_fresh_bid_no_sale_and_a_closed_day_is_left_to_settlement() -> None:
    conn = _store()
    stale = lane.plan_mis_bought_leg_exits(conn, now_utc=NOW, claim_reader=_claim(None), bid_reader=_bids({"27°C": [(0.21, 7.4)]}, age=3600.0))
    assert stale["plans"] == []
    assert {r["bucket"]: r.get("skipped") for r in stale["readings"]} == {"27°C": "no_fresh_bid", "25°C": "no_fresh_bid"}
    closed = lane.plan_mis_bought_leg_exits(conn, now_utc="2026-09-18T20:00:00.000Z", claim_reader=_claim(None), bid_reader=_bids({"27°C": [(0.21, 7.4)]}))
    assert closed["plans"] == [] and all(r.get("skipped") == "day_closed" for r in closed["readings"])


def test_a_leg_under_the_exchange_minimum_is_not_sold(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _store()
    conn.execute("UPDATE basket_target_market_registry SET registry_json=? WHERE yes_token='t25'", (json.dumps({"order_min_size": 6}),))
    out = lane.plan_mis_bought_leg_exits(conn, now_utc=NOW, claim_reader=_claim(None), bid_reader=_bids({"25°C": [(0.19, 50.0)], "27°C": [(0.21, 9.0)]}))
    assert {p["bucket"] for p in out["plans"]} == {"27°C"}
    assert next(r for r in out["readings"] if r["bucket"] == "25°C")["skipped"] == "below_min_order:6"


def test_off_sells_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lane, "MIS_BOUGHT_LEG_EXIT", "off")
    out = lane.plan_mis_bought_leg_exits(_store(), now_utc=NOW, claim_reader=_claim(None), bid_reader=_bids({"27°C": [(0.21, 9.0)]}))
    assert out["plans"] == [] and out["mode"] == "off"


def test_the_sell_is_a_gtd_at_the_bid_recorded_like_any_order_and_never_sold_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _store()
    monkeypatch.setattr(lane, "live_intent_armed", lambda: True)
    sent: list[dict] = []

    def transport(path, payload):
        sent.append(dict(payload))
        if payload["token_id"] == "t27":
            # Matched at once: the exchange reports shares and the USDC received.
            return {"ok": True, "orderID": "0xsell27", "status": "matched", "size_matched": payload["size"],
                    "exchange_response": {"takingAmount": round(payload["size"] * payload["price"], 6)}}
        # Accepted and resting.
        return {"ok": True, "orderID": "0xsell25", "status": "live"}

    bids = _bids({"27°C": [(0.21, 7.4)], "25°C": [(0.19, 50.0)]})
    receipt = lane.execute_mis_bought_leg_exits(conn, transport=transport, now_utc=NOW, live_submit_authorized=True, claim_reader=_claim(None), bid_reader=bids)
    assert receipt["planned"] == 2 and receipt["submitted"] == 2
    by_token = {p["token_id"]: p for p in sent}
    assert by_token["t27"] == {
        "strategy_id": "weather_basket_yes_v2", "order_intent_id": by_token["t27"]["order_intent_id"],
        "idempotency_key": by_token["t27"]["order_intent_id"], "token_id": "t27", "outcome_side": "YES",
        "side": "SELL", "price": 0.21, "size": 6.2, "order_type": "GTD", "post_only": False,
        "expiration": by_token["t27"]["expiration"],
    }
    assert by_token["t27"]["expiration"] == int(lane._utc(NOW).timestamp()) + lane.GTD_HORIZON_SECONDS
    assert by_token["t27"]["order_intent_id"].startswith("misbuyexit_")
    rows = conn.execute(
        "SELECT i.bucket, i.order_action, i.state, r.state, r.filled_size"
        " FROM basket_target_order_intents i JOIN basket_target_order_reconciliations r"
        " ON r.order_intent_id=i.order_intent_id WHERE i.order_intent_id LIKE 'misbuyexit_%' ORDER BY i.bucket"
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("25°C", "SELL"), ("27°C", "SELL")]
    filled = next(r for r in rows if r[0] == "27°C")
    assert filled[2] == "filled" and filled[3] == "resolved_filled" and filled[4] == pytest.approx(6.2)
    # The inventory nets the sale: 27C is gone from the book.
    holdings = ledger_holdings_by_token(conn, station_id=ST, event_date=ED)["holdings"]
    assert "t27" not in holdings or holdings["t27"]["shares"] < 0.01
    assert holdings["t25"]["shares"] == pytest.approx(17.0)
    assert conn.execute("SELECT state FROM basket_target_execution_cycles WHERE cycle_id LIKE 'misbuyexit_%'").fetchall() == [("submitted",), ("submitted",)]
    # Next cycle: 27C is sold (below the minimum remainder), 25C's sell is still open.
    again = lane.plan_mis_bought_leg_exits(conn, now_utc="2026-09-17T12:20:00.000Z", claim_reader=_claim(None), bid_reader=bids)
    assert again["plans"] == []
    assert {r["bucket"]: r.get("skipped") for r in again["readings"]} == {"27°C": "below_min_order:5", "25°C": "sell_open"}


def test_disarmed_or_unauthorized_never_reaches_the_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _store()
    monkeypatch.setattr(lane, "live_intent_armed", lambda: False)
    calls: list = []
    receipt = lane.execute_mis_bought_leg_exits(conn, transport=lambda p, payload: calls.append(payload), now_utc=NOW, live_submit_authorized=True, claim_reader=_claim(None), bid_reader=_bids({"27°C": [(0.21, 7.4)]}))
    assert calls == [] and receipt["submitted"] == 0
    assert receipt["receipts"][0]["reason"] == "live_intent_disarmed"
    assert conn.execute("SELECT event_type FROM basket_target_order_events WHERE order_intent_id LIKE 'misbuyexit_%'").fetchall() == [("NEVER_REACHED_EXCHANGE",)]


def test_the_daemon_runs_the_lane_after_the_recycle_on_the_writer_connection() -> None:
    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    recycle = source.index("execute_certain_win_recycles(")
    exit_call = source.index("execute_mis_bought_leg_exits(")
    assert recycle < exit_call
    assert '"mis_bought_leg_exit": exit_receipt' in source
    assert "load_chain_sent_shares" in inspect.getsource(lane.mis_bought_legs)
