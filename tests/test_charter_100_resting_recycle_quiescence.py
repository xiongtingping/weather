"""Charter 100 (2026-09-18, 「一直挂」 / 「改为0.99」): a recycle 0.99 sell the
venue holds does not make its station-day non-quiescent.

The order-state predicates refuse a station-day a zero-pending snapshot --
and so stop its quotes renewing -- while any order of it is open.  A resting
recycle sell rests on every held leg for as long as the market trades; left
blocking, it would freeze every held station-day's quotes all day (RKSI
2026-09-18, one charter 144 sell).  Exactly that shape is let through:
everything else that blocked still blocks, including a recycle sell whose
state is unknown and a reconciliation whose intent cannot be found.
"""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.execution_reconciliation import record_order_event
from bayes_basket_target.order_state_producer import (
    TARGET_INTENT_BLOCKING_STATES,
    TARGET_RECONCILIATION_BLOCKING_STATES,
    _SCOPE_PREDICATES,
)

ST, ED = "ZUUU", "2026-09-16"
AT = "2026-09-16T09:30:00Z"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,event_date,settlement_timezone,"
        "strategy_lane,plan_sha256,requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES('c1','p1',?,?,'Asia/Shanghai','basket',?,0,'settled',?,?)",
        (ST, ED, "a" * 64, AT, AT),
    )
    return conn


def _order(conn, intent_id: str, action: str, event_type: str) -> None:
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,cycle_id,bucket,token_id,side,"
        "order_action,limit_price,requested_size,requested_notional_usdc,state,live_submit_authorized,"
        "idempotency_key,intent_json,intent_sha256,created_at) VALUES(?,?,'c1','25°C','t25','YES',?,0.99,6,5.94,"
        "'prepared',1,?,'{}',?,?)",
        (intent_id, f"d_{intent_id}", action, intent_id, "a" * 64, AT),
    )
    record_order_event(conn, order_intent_id=intent_id, event={
        "event_type": event_type, "exchange_order_id": f"0x{intent_id}", "observed_at": AT,
        "matched_size": 0.0, "matched_notional_usdc": 0.0, "event_payload": {},
    })


def _blocks(conn) -> dict[str, bool]:
    out = {}
    for table, states in (
        ("basket_target_order_intents", TARGET_INTENT_BLOCKING_STATES),
        ("basket_target_order_reconciliations", TARGET_RECONCILIATION_BLOCKING_STATES),
    ):
        sql = _SCOPE_PREDICATES[table].format(states=",".join("?" for _ in states))
        out[table] = conn.execute(sql, (*sorted(states), ST, ST, ED, ED)).fetchone() is not None
    return out


def test_a_resting_recycle_sell_lets_its_station_day_renew() -> None:
    conn = _conn()
    _order(conn, "recycle998_abc", "SELL", "POST_ACK")
    assert conn.execute(
        "SELECT i.state, r.state FROM basket_target_order_intents i JOIN basket_target_order_reconciliations r"
        " ON r.order_intent_id=i.order_intent_id"
    ).fetchall() == [("submitted", "pending")]
    assert _blocks(conn) == {"basket_target_order_intents": False, "basket_target_order_reconciliations": False}


@pytest.mark.parametrize(("intent_id", "action", "event_type"), [
    ("recycle998_abc", "SELL", "UNKNOWN_POST"),   # nobody knows whether it is at the venue
    ("misbuyexit_abc", "SELL", "POST_ACK"),       # another lane's resting sell
    ("intent_abc", "BUY", "POST_ACK"),            # a basket rung
])
def test_everything_else_that_blocked_still_blocks(intent_id: str, action: str, event_type: str) -> None:
    conn = _conn()
    _order(conn, intent_id, action, event_type)
    assert any(_blocks(conn).values())


def test_a_reconciliation_whose_intent_cannot_be_found_still_blocks() -> None:
    conn = _conn()
    _order(conn, "recycle998_abc", "SELL", "POST_ACK")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("UPDATE basket_target_order_reconciliations SET order_intent_id='recycle998_orphan'")
    assert _blocks(conn)["basket_target_order_reconciliations"] is True
