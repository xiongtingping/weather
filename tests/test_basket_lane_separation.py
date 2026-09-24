"""The basket reads its own history only (user ruling 2026-09-24:
「生产是多个策略混合，要区分开来」).

Every lane writes the same order tables and the same ``strategy_lane='basket'``
decision rows; the plan-id prefix is what tells them apart.  Each test seeds a
basket record and another lane's record on one station-day and asserts the
basket reader sees only its own.
"""
from __future__ import annotations

import json
import sqlite3

from bayes_basket_target.execution_reconciliation import record_order_event
from bayes_basket_target.production_io import (
    basket_own_fills,
    basket_plan_sql,
    load_chain_sent_shares,
    load_last_decision_signature,
    load_order_end_count,
)
from bayes_basket_target.ssot import apply_production_ssot_schema

ST, DAY, TZ = "LEMD", "2026-09-21", "Europe/Madrid"


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    return conn


def _cycle(conn, cycle_id: str, plan_id: str) -> None:
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,'basket',?,5.0,'submitted',?,?)",
        (cycle_id, plan_id, ST, DAY, TZ, "a" * 64,
         "2026-09-20T10:00:00Z", "2026-09-20T10:00:00Z"),
    )


def _intent(conn, intent_id: str, cycle_id: str, bucket: str, size: float,
            at: str = "2026-09-20T10:00:00Z") -> None:
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
        "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
        "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
        "intent_json,intent_sha256,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (intent_id, f"d_{intent_id}", cycle_id, bucket, f"tok_{bucket}", "YES",
         "BUY", 0.5, size, size * 0.5, "submitted", 1, f"k_{intent_id}",
         json.dumps({"post_only": True, "execution_candidate": "MAKER"}),
         "b" * 64, at),
    )


def _two_lanes(conn) -> None:
    _cycle(conn, "c_basket", "plan_abc")
    _cycle(conn, "c_tiebreak", "tiebreak_plan_xyz")
    _intent(conn, "intent_1", "c_basket", "32°C", 10.0)
    _intent(conn, "tiebreak_1", "c_tiebreak", "31°C", 25.0)
    conn.commit()


def test_the_predicate_is_exact_not_a_like_wildcard() -> None:
    conn = sqlite3.connect(":memory:")
    rows = [("plan_1",), ("planX1",), ("tiebreak_plan_1",), ("p1_cons",)]
    conn.execute("CREATE TABLE t(plan_id TEXT)")
    conn.executemany("INSERT INTO t VALUES(?)", rows)
    kept = [r[0] for r in conn.execute("SELECT plan_id FROM t WHERE " + basket_plan_sql("plan_id"))]
    assert kept == ["plan_1"]


def test_another_lanes_send_is_not_the_baskets_send_history() -> None:
    conn = _store()
    _two_lanes(conn)
    for intent in ("intent_1", "tiebreak_1"):
        record_order_event(conn, order_intent_id=intent, event={
            "event_type": "POST_ACK", "exchange_order_id": f"0x{intent}",
            "observed_at": "2026-09-20T10:00:05Z", "matched_size": 0.0,
            "matched_notional_usdc": 0.0, "event_payload": {},
        })
    assert load_chain_sent_shares(conn, station_id=ST, event_date=DAY) == {"32°C": 10.0}


def test_another_lanes_expiry_is_not_a_basket_decision_moment() -> None:
    conn = _store()
    _two_lanes(conn)
    record_order_event(conn, order_intent_id="tiebreak_1", event={
        "event_type": "EXPIRED", "exchange_order_id": "0xt",
        "observed_at": "2026-09-20T11:00:00Z", "matched_size": 0.0,
        "matched_notional_usdc": 0.0, "event_payload": {},
    })
    assert load_order_end_count(conn, station_id=ST, event_date=DAY) == 0
    record_order_event(conn, order_intent_id="intent_1", event={
        "event_type": "CANCEL_ACK", "exchange_order_id": "0xb",
        "observed_at": "2026-09-20T11:05:00Z", "matched_size": 0.0,
        "matched_notional_usdc": 0.0, "event_payload": {},
    })
    assert load_order_end_count(conn, station_id=ST, event_date=DAY) == 1


def _decision(conn, *, plan_id: str, at: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,decided_state,will_order,"
        "target_execution_hard_deadline_at,reason_codes_json,details_json,"
        "decision_sha256,decision_scope_key,created_at)"
        " VALUES(?,?,?,?,?,?,'basket','prepared',0,NULL,'[]',?,?,?,?)",
        (f"d_{plan_id}_{at}", f"c_{plan_id}_{at}", plan_id, ST, DAY, TZ,
         json.dumps(details), "c" * 64, f"s_{plan_id}_{at}", at),
    )
    conn.commit()


def test_another_lanes_newer_decision_row_does_not_hide_the_baskets_signature() -> None:
    conn = _store()
    signature = {"stage": "P1", "probability_identity": "p-basket",
                 "edge_state": "none", "basket_holding": "", "is_decision_moment": True}
    _decision(conn, plan_id="plan_abc", at="2026-09-21T09:00:00Z",
              details={"decision_signature": signature})
    # A later row of another lane that carries a signature-shaped field must
    # not be read as the basket's last decision.
    _decision(conn, plan_id="p1_consensus_decision_LEMD_2026-09-21", at="2026-09-21T09:30:00Z",
              details={"decision_signature": {**signature, "probability_identity": "p-other"}})
    got = load_last_decision_signature(conn, station_id=ST, event_date=DAY, stage="P1")
    assert got is not None and got["probability_identity"] == "p-basket"


def _fill(conn, intent_id: str, size: float, notional: float, at: str) -> None:
    conn.execute(
        "INSERT INTO basket_target_order_reconciliations(order_intent_id,"
        "exchange_order_id,state,filled_size,filled_notional_usdc,last_event_id,"
        "blocking_reason,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (intent_id, f"0x{intent_id}", "resolved_filled", size, notional, None, None, at),
    )
    conn.commit()


def test_the_basket_holds_only_what_its_own_orders_filled() -> None:
    conn = _store()
    _two_lanes(conn)
    _fill(conn, "intent_1", 10.0, 5.0, "2026-09-20T10:05:00Z")
    _fill(conn, "tiebreak_1", 25.0, 4.5, "2026-09-20T10:06:00Z")
    filled, cost = basket_own_fills(conn, station_id=ST, event_date=DAY,
                                    observed_at_utc="2026-09-20T12:00:00Z")
    assert filled == {"32°C": 10.0}
    assert cost == {"32°C": 5.0}


def test_a_fill_after_the_snapshot_is_left_to_the_next_one_not_an_error() -> None:
    conn = _store()
    _two_lanes(conn)
    _fill(conn, "intent_1", 10.0, 5.0, "2026-09-20T13:00:00Z")
    filled, _ = basket_own_fills(conn, station_id=ST, event_date=DAY,
                                 observed_at_utc="2026-09-20T12:00:00Z")
    assert filled == {}


def test_forward_day_holdings_read_only_the_baskets_own_fills() -> None:
    from bayes_basket_target.inventory_producer import ledger_holdings_by_token
    from bayes_basket_target.production_io import basket_intent_ids

    conn = _store()
    _two_lanes(conn)
    _fill(conn, "intent_1", 10.0, 5.0, "2026-09-20T10:05:00Z")
    _fill(conn, "tiebreak_1", 25.0, 4.5, "2026-09-20T10:06:00Z")
    everyone = ledger_holdings_by_token(conn, station_id=ST, event_date=DAY)
    assert set(everyone["holdings"]) == {"tok_32°C", "tok_31°C"}
    basket = ledger_holdings_by_token(
        conn, station_id=ST, event_date=DAY,
        intent_ids=basket_intent_ids(conn, station_id=ST, event_date=DAY),
    )
    assert set(basket["holdings"]) == {"tok_32°C"}
    assert basket["holdings"]["tok_32°C"]["shares"] == 10.0
