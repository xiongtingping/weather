"""Charter 250: a resting GTC sell that is not a rung is asked on a slower clock.

User ruling 2026-10-03 「卖单降频」.  27 of the 30 resting orders were charter
100's 0.998 recycle sells (GTC, some resting since 09-22); each was a venue
lookup on every pass, and six whose point lookup answers empty were a
departed-order reconcile on top -- 14-22 s of every production cycle, in
front of the decisions.  Such a sell is now asked at most once in
``GTC_SELL_LOOKUP_INTERVAL_SEC``; a rung, and a sell with a GTD horizon, is
asked on every pass as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bayes_basket_target import resting_ladder_lifecycle as lane
from bayes_basket_target.execution_reconciliation import record_order_event

from tests.test_charter_180_resting_lookups_overlap import (  # noqa: E402
    NOW,
    POSTED,
    _Client,
    _book,
    _ledger,
)

ROOT = Path(__file__).resolve().parents[1]
CHARTER = "250"


@pytest.fixture(autouse=True)
def _fresh_clock():
    lane._GTC_SELL_LAST_LOOKUP.clear()
    yield
    lane._GTC_SELL_LAST_LOOKUP.clear()


def _sell(conn, index: int, *, order_type: str = "GTC", price: float = 0.998) -> None:
    body = {"post_only": False, "order_type": order_type, "lane": "recycle998"}
    if order_type == "GTD":
        body["expiration"] = 4102444800  # far ahead: still inside its horizon
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
        "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
        "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
        "intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"sell_{index:02d}", "decision_1", "cycle_1", f"s{index:02d}",
         f"stok{index:02d}", "YES", "SELL", price, 5.0, 5.0 * price, "submitted", 1,
         f"skey_{index:02d}", json.dumps(body), "c" * 64, f"2026-09-06T10:01:{index:02d}Z"),
    )
    record_order_event(conn, order_intent_id=f"sell_{index:02d}", event={
        "event_type": "POST_ACK", "exchange_order_id": f"0xs{index:02d}",
        "observed_at": POSTED, "matched_size": 0.0,
        "matched_notional_usdc": 0.0, "event_payload": {},
    })
    conn.commit()


def _pass(conn, client, **overrides):
    return lane.reprice_resting_rungs(
        conn, as_of_utc=NOW, client=client, book_loader=_book(set()), **overrides,
    )


def test_the_rule_names_gtc_sells_that_are_not_rungs() -> None:
    assert lane.on_the_slow_clock({"ladder_rung": False, "order_type": "GTC", "order_action": "SELL"})
    assert not lane.on_the_slow_clock({"ladder_rung": True, "order_type": "GTC", "order_action": "BUY"})
    assert not lane.on_the_slow_clock({"ladder_rung": False, "order_type": "GTD", "order_action": "SELL"})
    assert not lane.on_the_slow_clock({"ladder_rung": False, "order_type": "GTC", "order_action": "BUY"})
    assert lane.GTC_SELL_LOOKUP_INTERVAL_SEC == 300.0


def test_a_gtc_sell_answered_for_rests_until_the_interval_has_run() -> None:
    conn = _ledger(2)          # two rungs
    _sell(conn, 0)
    _sell(conn, 1)
    client = _Client({})
    first = _pass(conn, client)
    assert sorted(client.asked) == ["0x00", "0x01", "0xs00", "0xs01"]
    assert first["examined"] == 4 and first["looked_up"] == 4 and first["lookup_rested"] == 0
    assert first["kept_not_ladder"] == 2

    client.asked.clear()
    second = _pass(conn, client)
    # The rungs are asked again; the sells rest, and say so on the receipt.
    assert sorted(client.asked) == ["0x00", "0x01"]
    assert second["examined"] == 4 and second["looked_up"] == 2 and second["lookup_rested"] == 2
    assert second["kept"] == 4 and second["kept_not_ladder"] == 2
    rested = [r for r in second["rungs"] if "looked_up_seconds_ago" in (r.get("verdict") or {})]
    assert sorted(r["order_intent_id"] for r in rested) == ["sell_00", "sell_01"]
    assert all(r["verdict"] == {**r["verdict"], "action": "keep", "reason": "not_a_ladder_rung"} for r in rested)

    # The interval has run for one of them: it is asked again, the other rests.
    lane._GTC_SELL_LAST_LOOKUP["sell_00"] -= lane.GTC_SELL_LOOKUP_INTERVAL_SEC + 1
    client.asked.clear()
    third = _pass(conn, client)
    assert sorted(client.asked) == ["0x00", "0x01", "0xs00"]
    assert third["lookup_rested"] == 1


def test_a_sell_with_its_own_horizon_and_every_rung_are_asked_every_pass() -> None:
    conn = _ledger(1)
    _sell(conn, 0, order_type="GTD", price=0.55)
    client = _Client({})
    _pass(conn, client)
    client.asked.clear()
    again = _pass(conn, client)
    assert sorted(client.asked) == ["0x00", "0xs00"]
    assert again["lookup_rested"] == 0 and lane._GTC_SELL_LAST_LOOKUP == {}


def test_zero_asks_every_order_every_pass() -> None:
    conn = _ledger(0)
    _sell(conn, 0)
    client = _Client({})
    _pass(conn, client, gtc_sell_lookup_interval_sec=0)
    _pass(conn, client, gtc_sell_lookup_interval_sec=0)
    assert client.asked == ["0xs00", "0xs00"]


def test_a_fill_is_booked_on_the_pass_that_asks() -> None:
    conn = _ledger(0)
    _sell(conn, 0)
    client = _Client({})
    _pass(conn, client)
    # The venue matched it; inside the interval nobody asks, so nothing moves.
    client.answers["0xs00"] = {"ok": True, "status": "matched", "response": {"size_matched": 5.0}}
    resting = _pass(conn, client)
    assert resting["synced_filled"] == 0 and resting["lookup_rested"] == 1
    assert resting["state"] == "no_change"
    lane._GTC_SELL_LAST_LOOKUP["sell_00"] -= lane.GTC_SELL_LOOKUP_INTERVAL_SEC + 1
    asked = _pass(conn, client)
    assert asked["synced_filled"] == 1 and asked["state"] == "committed"
    # Gone from the resting set: its clock is dropped with it.
    _pass(conn, client)
    assert "sell_00" not in lane._GTC_SELL_LAST_LOOKUP


def test_an_order_the_gateway_did_not_answer_for_is_asked_again_next_pass() -> None:
    conn = _ledger(0)
    _sell(conn, 0)
    dead = {"ok": False, "error": "gateway_transport:TimeoutError", "transport_ambiguous": True}
    client = _Client({"0xs00": dead})
    _pass(conn, client)
    assert "sell_00" not in lane._GTC_SELL_LAST_LOOKUP
    client.answers.pop("0xs00")
    client.asked.clear()
    _pass(conn, client)
    assert client.asked == ["0xs00"]


def test_the_charter_carries_the_ruling() -> None:
    text = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    body = text[text.index(f"\n{CHARTER}. "):]
    for phrase in ("卖单降频", "on_the_slow_clock", "GTC_SELL_LOOKUP_INTERVAL_SEC",
                   "PM_RESTING_GTC_SELL_LOOKUP_INTERVAL_SEC", "300"):
        assert phrase in body
    assert "占号" not in body
