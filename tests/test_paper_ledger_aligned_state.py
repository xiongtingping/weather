"""The paper ledger's chain state answers exactly as the SSOT's would.

User ruling 2026-09-24 「账本对齐」: the ledger decides through production's own
compile entries and swaps in only what it simulated.  So the object it swaps
in must answer every question ``SsotChainState`` answers -- the same way, for
the same history -- or the ledger is measuring a strategy production does not
run.  One history is written twice here, once into an SSOT store the way the
atomic ledger and the order service write it and once into the paper state
the way the ledger records it, and every answer is compared at several
clocks.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target.consolidation_chain import (  # noqa: E402
    ensure_schema as ensure_chain_schema,
    record_chain_last_action,
)
from bayes_basket_target.execution_reconciliation import record_order_event  # noqa: E402
from bayes_basket_target.production_io import SsotChainState  # noqa: E402
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402

import paper_ledger_aligned as L  # noqa: E402

ST, DAY, TZ = "LEMD", "2026-09-21", "Europe/Madrid"
BUCKETS = ("30°C", "31°C", "32°C", "33°C", "34°C")

# (intent, bucket, size, limit, candidate, placed, events[(kind, at, size)])
ORDERS = (
    ("intent_a", "31°C", 10.0, 0.30, "MAKER", "2026-09-21T08:00:00Z",
     (("POST_ACK", "2026-09-21T08:00:00Z", 0.0), ("FILL", "2026-09-21T08:30:00Z", 10.0))),
    ("intent_b", "32°C", 12.0, 0.40, "MAKER", "2026-09-21T08:00:00Z",
     (("POST_ACK", "2026-09-21T08:00:00Z", 0.0), ("CANCEL_ACK", "2026-09-21T09:10:00Z", 0.0))),
    ("intent_c", "32°C", 6.0, 0.45, "TAKER_FOK", "2026-09-21T10:00:00Z",
     (("FILL", "2026-09-21T10:00:00Z", 6.0),)),
    ("intent_d", "33°C", 8.0, 0.20, "MAKER", "2026-09-21T11:00:00Z",
     (("POST_ACK", "2026-09-21T11:00:00Z", 0.0),)),
)
# (created_at, stage, identity, core, direction, moment, order_ends)
DECISIONS = (
    ("2026-09-21T07:59:59Z", "P0", "id-p0", ["31°C", "32°C"], "UP", True, "ended:0"),
    ("2026-09-21T09:59:59Z", "P1", "id-p1a", ["32°C"], "NEUTRAL", True, "ended:1"),
    ("2026-09-21T10:59:59Z", "P1", "id-p1b", ["32°C", "33°C"], "UP", False, "ended:1"),
)
CLOCKS = (
    "2026-09-21T07:00:00Z", "2026-09-21T08:15:00Z", "2026-09-21T09:30:00Z",
    "2026-09-21T10:30:00Z", "2026-09-21T11:30:00Z",
)


def _details(stage, identity, core, direction, moment, ends):
    return {
        "schema": "bayes_basket_production_decision_v1",
        "core_buckets": core,
        "direction": direction,
        "decision_signature": {
            "stage": stage, "probability_identity": identity,
            "edge_state": "admissible", "basket_holding": "",
            "is_decision_moment": moment, "order_ends": ends,
        },
    }


def _ssot() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    ensure_chain_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES('c1','plan_abc',?,?,?,'basket',?,5.0,'submitted',?,?)",
        (ST, DAY, TZ, "a" * 64, "2026-09-21T07:00:00Z", "2026-09-21T07:00:00Z"),
    )
    for intent, bucket, size, limit, candidate, placed, events in ORDERS:
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
            "intent_json,intent_sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent, f"d_{intent}", "c1", bucket, f"tok_{bucket}", "YES", "BUY",
             limit, size, size * limit, "submitted", 1, f"k_{intent}",
             json.dumps({"execution_candidate": candidate,
                         "post_only": candidate == "MAKER"}),
             "b" * 64, placed),
        )
    conn.commit()
    for intent, bucket, size, limit, candidate, placed, events in ORDERS:
        for kind, at, filled in events:
            record_order_event(conn, order_intent_id=intent, event={
                "event_type": kind, "exchange_order_id": f"0x{intent}",
                "observed_at": at, "matched_size": filled,
                "matched_notional_usdc": filled * limit, "event_payload": {},
            })
            if kind == "FILL":
                conn.execute(
                    "INSERT INTO basket_target_order_reconciliations(order_intent_id,"
                    "exchange_order_id,state,filled_size,filled_notional_usdc,"
                    "last_event_id,blocking_reason,updated_at) VALUES(?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(order_intent_id) DO UPDATE SET state=excluded.state,"
                    " filled_size=excluded.filled_size,"
                    " filled_notional_usdc=excluded.filled_notional_usdc,"
                    " updated_at=excluded.updated_at",
                    (intent, f"0x{intent}", "resolved_filled", filled,
                     filled * limit, None, None, at),
                )
    for created, stage, identity, core, direction, moment, ends in DECISIONS:
        conn.execute(
            "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,"
            "event_date,settlement_timezone,strategy_lane,decided_state,will_order,"
            "target_execution_hard_deadline_at,reason_codes_json,details_json,"
            "decision_sha256,decision_scope_key,created_at)"
            " VALUES(?,?,?,?,?,?,'basket','prepared',0,NULL,'[]',?,?,?,?)",
            (f"d_{created}", f"c_{created}", f"plan_{created}", ST, DAY, TZ,
             json.dumps(_details(stage, identity, core, direction, moment, ends)),
             "c" * 64, f"s_{created}", created),
        )
    record_chain_last_action(
        conn, station_id=ST, event_date=DAY, probability_identity="id-p1a",
        decided_at_utc="2026-09-21T09:59:59Z",
    )
    conn.execute(
        "INSERT INTO basket_target_consolidation_chain_tilt(station_id,event_date,"
        "tilt_factor,signals_lit_json,decided_at_utc,plan_sha256) VALUES(?,?,?,?,?,?)",
        (ST, DAY, 0.5, "[]", "2026-09-21T07:59:59Z", ""),
    )
    conn.commit()
    return conn


def _paper() -> L.PaperChainState:
    state = L.PaperChainState(sqlite3.connect(":memory:"), capital="production")
    chain = state.day(ST, DAY)
    for created, stage, identity, core, direction, moment, ends in DECISIONS:
        chain.decisions.append(
            (created, _details(stage, identity, core, direction, moment, ends))
        )
    for intent, bucket, size, limit, candidate, placed, events in ORDERS:
        order = L.PaperOrder(
            order_id=intent, station_id=ST, event_date=DAY, bucket=bucket,
            token_id=f"tok_{bucket}", candidate=candidate, limit_price=limit,
            size=size, placed_at=placed, expires_at=None,
            ladder_rung=candidate == "MAKER", decision_created_at=placed,
        )
        for kind, at, filled in events:
            order.event(at, kind, filled, filled * limit)
            if kind in {"CANCEL_ACK", "EXPIRED"} or (kind == "FILL" and filled >= size):
                order.ended_at = at
        chain.orders.append(order)
    chain.last_action = ("2026-09-21T09:59:59Z", "id-p1a")
    chain.tilt = ("2026-09-21T07:59:59Z", 0.5)
    return state


@pytest.mark.parametrize("clock", CLOCKS)
def test_every_chain_question_gets_the_ssot_answer(clock: str) -> None:
    ssot = SsotChainState(_ssot(), before_utc=clock)
    paper = _paper()
    paper.before_utc = clock
    scope = {"station_id": ST, "event_date": DAY}

    assert paper.sent_shares(**scope) == ssot.sent_shares(**scope)
    assert paper.holdings(**scope, observed_at_utc=clock) == ssot.holdings(
        **scope, observed_at_utc=clock
    )
    assert {
        token: (entry["shares"], entry["cost_usdc"])
        for token, entry in paper.token_holdings(**scope)["holdings"].items()
    } == {
        token: (entry["shares"], entry["cost_usdc"])
        for token, entry in ssot.token_holdings(**scope)["holdings"].items()
    }
    assert paper.prior_core(**scope, canonical_buckets=BUCKETS) == ssot.prior_core(
        **scope, canonical_buckets=BUCKETS
    )
    for stage in ("P0", "P0-M", "P1"):
        assert paper.last_decision_signature(**scope, stage=stage) == (
            ssot.last_decision_signature(**scope, stage=stage)
        ), stage
    assert paper.last_acted_probability_identity(**scope) == (
        ssot.last_acted_probability_identity(**scope)
    )
    assert paper.chain_tilt(**scope) == ssot.chain_tilt(**scope)


def test_the_paper_account_is_not_capped_by_the_real_one() -> None:
    """The basket's legs in the real account are capped by what the account
    still holds (another lane's sell can take one out); the paper account is
    the basket's alone, so its legs stand whatever the real row says."""

    paper = _paper()
    paper.before_utc = "2026-09-21T11:30:00Z"
    filled, cost = paper.book_holdings(
        station_id=ST, event_date=DAY, observed_at_utc="2026-09-21T11:30:00Z",
        account_filled={"31°C": 0.0, "32°C": 0.0},
    )
    assert filled["31°C"] == 10.0 and filled["32°C"] == 6.0
    assert cost["32°C"] == pytest.approx(6.0 * 0.45)


def test_a_resting_order_is_the_pending_the_compile_sees() -> None:
    paper = _paper()
    paper.before_utc = "2026-09-21T11:30:00Z"
    pending, notional = paper.pending_orders(
        station_id=ST, event_date=DAY,
        account_pending={b: 0.0 for b in BUCKETS},
        account_pending_notional={b: 0.0 for b in BUCKETS},
    )
    assert pending["33°C"] == 8.0 and notional["33°C"] == pytest.approx(1.6)
    assert pending["32°C"] == 0.0  # cancelled at 09:10


def test_a_fok_crosses_the_decision_book_all_or_nothing() -> None:
    venue = L.PaperVenue(books=None, settlements={}, fill_model="touch")
    book = [{"bucket": "32°C", "ask_levels": [
        {"price": 0.40, "shares": 4.0}, {"price": 0.44, "shares": 5.0},
        {"price": 0.50, "shares": 50.0},
    ]}]

    def _order(size: float) -> L.PaperOrder:
        return L.PaperOrder(
            order_id="o", station_id=ST, event_date=DAY, bucket="32°C",
            token_id="t", candidate="TAKER_FOK", limit_price=0.45, size=size,
            placed_at="2026-09-21T10:00:00Z", expires_at=None,
            ladder_rung=False, decision_created_at="2026-09-21T10:00:00Z",
        )

    filled = _order(6.0)
    venue.submit_fok(filled, book_buckets=book)
    assert filled.state == "filled"
    assert filled.filled_by() == pytest.approx((6.0, 4 * 0.40 + 2 * 0.44))
    killed = _order(10.0)
    venue.submit_fok(killed, book_buckets=book)
    assert killed.state == "refused" and killed.filled_by() == (0.0, 0.0)
    assert not killed.was_sent  # a refusal reached no book (charter 136 五)


def _books(rows) -> "L.BookStore":
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot(station_id TEXT, event_date TEXT,"
        " state TEXT, observed_at TEXT, buckets_json TEXT)"
    )
    for observed_at, ask, bid in rows:
        conn.execute(
            "INSERT INTO basket_official_ws_snapshot VALUES(?,?,?,?,?)",
            (ST, DAY, "COMMITTED", observed_at, json.dumps([{
                "bucket": "31°C", "bid_price": bid, "price_tick_size": 0.01,
                "ask_levels": [{"price": ask, "shares": 50.0}],
            }])),
        )
    return L.BookStore(conn)


def _maker(expires_at: str) -> L.PaperOrder:
    order = L.PaperOrder(
        order_id="m", station_id=ST, event_date=DAY, bucket="31°C", token_id="t",
        candidate="MAKER", limit_price=0.30, size=10.0,
        placed_at="2026-09-21T10:00:00Z", expires_at=expires_at,
        ladder_rung=True, decision_created_at="2026-09-21T10:00:00Z",
    )
    return order


def test_a_resting_maker_fills_when_a_later_book_offers_at_its_limit() -> None:
    books = _books([
        ("2026-09-21T10:05:00Z", 0.35, 0.30),
        ("2026-09-21T10:10:00Z", 0.30, 0.29),
    ])
    venue = L.PaperVenue(books=books, settlements={}, fill_model="touch")
    order = _maker("2026-09-21T12:00:00Z")
    venue.post_maker(order)
    venue.advance(order, until="2026-09-21T10:07:00Z")
    assert order.state == "resting" and order.filled_by() == (0.0, 0.0)
    venue.advance(order, until="2026-09-21T10:20:00Z")
    assert order.state == "filled"
    assert order.filled_by() == pytest.approx((10.0, 3.0))  # at its own limit
    assert order.ended_at == "2026-09-21T10:10:00Z"


def test_a_resting_maker_nobody_sells_to_expires_at_its_own_horizon() -> None:
    books = _books([("2026-09-21T10:05:00Z", 0.35, 0.30)])
    venue = L.PaperVenue(books=books, settlements={}, fill_model="touch")
    order = _maker("2026-09-21T10:30:00Z")
    venue.post_maker(order)
    venue.advance(order, until="2026-09-21T11:00:00Z")
    assert order.state == "expired" and order.ended_at == "2026-09-21T10:30:00Z"
    assert order.ends_by("2026-09-21T11:00:00Z") == 1  # charter 135 (一) counts it
