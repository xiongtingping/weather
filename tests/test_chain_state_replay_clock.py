"""The chain-state reader answers at a clock (user ruling 2026-09-24: 「账本对齐」).

Production reads the basket's own history as of now.  A replay of a past
decision, and the paper ledger, ask the same reader at another clock: rows
written at or after it did not exist yet.  And a replay pins the book
generation the decision recorded, because one batch can write two quote rows
for a scope under identical stamps (NZWN and RCSS 2026-09-24 03:43:41.882Z).
"""
from __future__ import annotations

import json
import sqlite3

from bayes_basket_target.execution_reconciliation import record_order_event
from bayes_basket_target.production_io import (
    SsotChainState,
    _latest_snapshot_row,
    _snapshot_row_by_version,
)
from bayes_basket_target.ssot import apply_production_ssot_schema

ST, DAY, TZ = "LEMD", "2026-09-21", "Europe/Madrid"


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES('c1','plan_abc',?,?,?,'basket',?,5.0,'submitted',?,?)",
        (ST, DAY, TZ, "a" * 64, "2026-09-21T08:00:00Z", "2026-09-21T08:00:00Z"),
    )
    for intent, bucket, at in (
        ("intent_1", "32°C", "2026-09-21T09:00:00Z"),
        ("intent_2", "33°C", "2026-09-21T11:00:00Z"),
    ):
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
            "intent_json,intent_sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent, f"d_{intent}", "c1", bucket, f"tok_{bucket}", "YES", "BUY",
             0.5, 10.0, 5.0, "submitted", 1, f"k_{intent}",
             json.dumps({"post_only": True, "execution_candidate": "MAKER"}),
             "b" * 64, at),
        )
    conn.commit()
    return conn


def _decision(conn, *, at: str, identity: str) -> None:
    signature = {"stage": "P1", "probability_identity": identity,
                 "edge_state": "none", "basket_holding": "", "is_decision_moment": True}
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,decided_state,will_order,"
        "target_execution_hard_deadline_at,reason_codes_json,details_json,"
        "decision_sha256,decision_scope_key,created_at)"
        " VALUES(?,?,?,?,?,?,'basket','prepared',0,NULL,'[]',?,?,?,?)",
        (f"d_{at}", f"c_{at}", f"plan_abc_{at}", ST, DAY, TZ,
         json.dumps({"decision_signature": signature}), "c" * 64, f"s_{at}", at),
    )
    conn.commit()


def test_a_clock_does_not_see_the_decisions_made_after_it() -> None:
    conn = _store()
    _decision(conn, at="2026-09-21T09:00:00Z", identity="p-early")
    _decision(conn, at="2026-09-21T10:00:00Z", identity="p-late")
    now = SsotChainState(conn).last_decision_signature(
        station_id=ST, event_date=DAY, stage="P1")
    then = SsotChainState(conn, before_utc="2026-09-21T09:30:00Z").last_decision_signature(
        station_id=ST, event_date=DAY, stage="P1")
    assert now is not None and now["probability_identity"] == "p-late"
    assert then is not None and then["probability_identity"] == "p-early"


def test_a_clock_does_not_see_the_sends_or_fills_after_it() -> None:
    conn = _store()
    for intent, at in (("intent_1", "2026-09-21T09:00:05Z"), ("intent_2", "2026-09-21T11:00:05Z")):
        record_order_event(conn, order_intent_id=intent, event={
            "event_type": "POST_ACK", "exchange_order_id": f"0x{intent}",
            "observed_at": at, "matched_size": 0.0,
            "matched_notional_usdc": 0.0, "event_payload": {},
        })
    for intent, at in (("intent_1", "2026-09-21T09:05:00Z"), ("intent_2", "2026-09-21T11:05:00Z")):
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations(order_intent_id,"
            "exchange_order_id,state,filled_size,filled_notional_usdc,last_event_id,"
            "blocking_reason,updated_at) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(order_intent_id) DO UPDATE SET state=excluded.state,"
            " filled_size=excluded.filled_size,"
            " filled_notional_usdc=excluded.filled_notional_usdc,"
            " updated_at=excluded.updated_at",
            (intent, f"0x{intent}", "resolved_filled", 10.0, 5.0, None, None, at),
        )
    conn.commit()
    now, then = SsotChainState(conn), SsotChainState(conn, before_utc="2026-09-21T10:00:00Z")
    assert set(now.sent_shares(station_id=ST, event_date=DAY)) == {"32°C", "33°C"}
    assert set(then.sent_shares(station_id=ST, event_date=DAY)) == {"32°C"}
    assert set(now.token_holdings(station_id=ST, event_date=DAY)["holdings"]) == {
        "tok_32°C", "tok_33°C"}
    assert set(then.token_holdings(station_id=ST, event_date=DAY)["holdings"]) == {"tok_32°C"}
    filled, _ = then.holdings(station_id=ST, event_date=DAY,
                              observed_at_utc="2026-09-21T12:00:00Z")
    assert filled == {"32°C": 10.0}


def _quote(conn, snapshot_id: str, quote_version: str, at: str) -> None:
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot(snapshot_id,market_id,station_id,"
        "event_date,settlement_timezone,sequence,observed_at,quote_version,"
        "quote_observed_at,quote_expires_at,quote_freshness_policy_sha256,"
        "quote_source,inventory_version,order_state_version,remaining_budget_usdc,"
        "buckets_json,cash_phase_input_json,content_sha256,state,created_at)"
        " VALUES(?,?,?,?,?,1,?,?,?,?,?,'OFFICIAL_CLOB_WS','inv','ord',5.0,'[]','{}',?,"
        "'COMMITTED',?)",
        (snapshot_id, "m", ST, DAY, TZ, at, quote_version, at,
         "2026-09-21T04:00:00Z", "d" * 64, "e" * 64, at),
    )
    conn.commit()


def test_a_replay_reads_the_quote_generation_the_decision_recorded() -> None:
    conn = _store()
    stamp = "2026-09-21T03:43:41.882Z"
    _quote(conn, "ws_a", "q-recorded", stamp)
    _quote(conn, "ws_b", "q-sibling", stamp)
    evaluation = "2026-09-21T03:43:58.108Z"
    # By clock the two rows are indistinguishable; the tie picks the last
    # written, which is not the one the decision read.
    latest = _latest_snapshot_row(
        conn, "basket_official_ws_snapshot", ST, DAY, evaluation)
    assert latest["snapshot_id"] == "ws_b"
    pinned = _snapshot_row_by_version(
        conn, "basket_official_ws_snapshot", "quote_version", "q-recorded",
        ST, DAY, evaluation)
    assert pinned["snapshot_id"] == "ws_a"
