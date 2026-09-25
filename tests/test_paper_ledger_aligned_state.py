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
from datetime import datetime, timedelta
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
    venue = L.PaperVenue(books=None, settlements={}, fill_model="none")
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
    return L.PaperOrder(
        order_id="m", station_id=ST, event_date=DAY, bucket="31°C", token_id="t",
        candidate="MAKER", limit_price=0.30, size=10.0,
        placed_at="2026-09-21T10:00:00Z", expires_at=expires_at,
        ladder_rung=True, decision_created_at="2026-09-21T10:00:00Z",
        settlement_timezone=TZ,
    )


def _curves(fill_after_seconds: float) -> dict:
    # Every draw lands on the one step: the order fills this long after it
    # was posted, whatever its hash.  Placed 10:00Z on a Madrid day that ends
    # 22:00Z, the order is in the same-day band; no label yet, so "any".
    step = {"t_seconds": [fill_after_seconds], "F": [1.0]}
    return {"curves": {"in_day|any": step, "in_day|won": step, "in_day|lost": step},
            "content_sha256": "c" * 64}


def test_a_resting_maker_fills_at_the_time_its_curve_draws() -> None:
    books = _books([
        ("2026-09-21T10:05:00Z", 0.35, 0.30),
        ("2026-09-21T10:10:00Z", 0.35, 0.30),
    ])
    venue = L.PaperVenue(books=books, settlements={}, curves=_curves(420.0))
    order = _maker("2026-09-21T12:00:00Z")
    venue.post_maker(order)
    assert order.fill_at == "2026-09-21T10:07:00.000Z"
    venue.advance(order, until="2026-09-21T10:06:00Z")
    assert order.state == "resting" and order.filled_by() == (0.0, 0.0)
    venue.advance(order, until="2026-09-21T10:20:00Z")
    assert order.state == "filled"
    assert order.filled_by() == pytest.approx((10.0, 3.0))  # at its own limit
    assert order.ended_at == "2026-09-21T10:07:00.000Z"


def test_a_maker_production_cancels_before_its_draw_never_fills() -> None:
    """Production's own resting rule runs first: a book with no bid left
    cancels the rung (charter 110), and the fill the curve drew for later
    never happens."""

    books = _books([("2026-09-21T10:05:00Z", 0.35, 0.0)])
    venue = L.PaperVenue(books=books, settlements={}, curves=_curves(3600.0))
    order = _maker("2026-09-21T12:00:00Z")
    venue.post_maker(order)
    venue.advance(order, until="2026-09-21T11:30:00Z")
    assert order.state == "cancelled" and order.filled_by() == (0.0, 0.0)
    assert order.ends_by("2026-09-21T11:30:00Z") == 1  # charter 135 (一) counts it


def test_a_resting_maker_whose_draw_is_past_its_horizon_expires() -> None:
    books = _books([("2026-09-21T10:05:00Z", 0.35, 0.30)])
    venue = L.PaperVenue(books=books, settlements={}, curves=_curves(3600.0))
    order = _maker("2026-09-21T10:30:00Z")
    venue.post_maker(order)
    venue.advance(order, until="2026-09-21T11:00:00Z")
    assert order.state == "expired" and order.ended_at == "2026-09-21T10:30:00Z"
    assert order.filled_by() == (0.0, 0.0)


def test_the_curve_is_chosen_by_whether_the_bucket_settled() -> None:
    """Adverse selection lives in the venue: the same order draws on the
    lost-bucket curve when its bucket did not settle, and only the venue
    reads the label."""

    books = _books([])
    curves = {"curves": {
        "in_day|won": {"t_seconds": [60.0], "F": [1.0]},
        "in_day|lost": {"t_seconds": [120.0], "F": [1.0]},
        "in_day|any": {"t_seconds": [90.0], "F": [1.0]},
    }}
    venue = L.PaperVenue(books=books, settlements={(ST, DAY): {"settled_bucket": "32°C"}}, curves=curves)
    order = _maker("2026-09-21T12:00:00Z")
    venue.post_maker(order)
    assert venue.maker_curve_key(order) == "in_day|lost"
    assert order.fill_at == "2026-09-21T10:02:00.000Z"


def test_the_lower_bound_never_fills_a_maker() -> None:
    venue = L.PaperVenue(books=_books([("2026-09-21T10:05:00Z", 0.35, 0.30)]), settlements={}, fill_model="none")
    order = _maker("2026-09-21T12:00:00Z")
    venue.post_maker(order)
    venue.advance(order, until="2026-09-21T11:00:00Z")
    assert order.fill_at is None and order.filled_by() == (0.0, 0.0)


def test_a_book_keeps_three_numbers_and_a_past_day_is_let_go() -> None:
    books = _books([
        ("2026-09-21T10:05:00Z", 0.35, 0.30),
        ("2026-09-21T10:10:00Z", 0.35, 0.30),
    ])
    rows = books.books(ST, DAY)
    # One tuple for one (bid, ask, tick), however many books repeat it.
    assert rows[0][1].rows["31°C"] is rows[1][1].rows["31°C"]
    assert rows[1][1].get("31°C") == {
        "bid": 0.30, "ask": 0.35, "tick": 0.01, "observed_at": "2026-09-21T10:10:00Z",
    }
    assert rows[1][1].get("30°C") is None
    assert books.mark(ST, DAY, "31°C", at="2026-09-21T10:06:00Z") == pytest.approx(0.325)
    books.forget_before(f"{DAY}T23:00:00Z")
    assert (ST, DAY) in books._cache  # the walk is still on its day
    later = (datetime.fromisoformat(DAY) + timedelta(days=3)).date().isoformat()
    books.forget_before(f"{later}T00:00:00Z")
    assert (ST, DAY) not in books._cache
    # Every later moment reads the day's last book, without reading the day:
    # a held day whose label is late is marked at every decision.
    store, books.ssot = books.ssot, None
    assert books.latest(ST, DAY, f"{later}T00:00:00Z").observed_at == "2026-09-21T10:10:00Z"
    assert books.mark(ST, DAY, "31°C", at=f"{later}T00:00:00Z") == pytest.approx(0.325)
    # Anything earlier is read again.
    books.ssot = store
    assert books.latest(ST, DAY, "2026-09-21T10:06:00Z").observed_at == "2026-09-21T10:05:00Z"


def _decisions_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    # Production's column order: ``created_at`` after ``details_json``.
    conn.execute(
        "CREATE TABLE auto_trade_decisions (decision_id TEXT PRIMARY KEY,"
        " cycle_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL UNIQUE,"
        " station_id TEXT NOT NULL, event_date TEXT NOT NULL,"
        " settlement_timezone TEXT NOT NULL, strategy_lane TEXT NOT NULL,"
        " decided_state TEXT NOT NULL, will_order INTEGER NOT NULL,"
        " reason_codes_json TEXT NOT NULL, details_json TEXT NOT NULL,"
        " decision_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX auto_trade_decisions_scope_idx"
        " ON auto_trade_decisions(station_id,event_date,created_at)"
    )
    conn.execute(
        "CREATE TABLE basket_policy_training_fact(decision_id TEXT, cycle_id TEXT,"
        " stage TEXT, decision_at_utc TEXT, feature_json TEXT)"
    )
    rows = [
        # (id, plan, station, day, created)
        ("d1", "plan_1", "ZUUU", "2026-09-22", "2026-09-20T01:00:00Z"),
        ("d2", "intent_2", "ZUUU", "2026-09-22", "2026-09-20T02:00:00Z"),
        ("d3", "plan_3", "EGLC", "2026-09-22", "2026-09-19T23:00:00Z"),
        ("d4", "plan_4", "EGLC", "2026-09-23", "2026-09-21T01:00:00Z"),
        ("d5", "plan_5", "ZUUU", "2026-09-22", "2026-09-22T05:00:00Z"),
    ]
    for decision_id, plan_id, station, day, created in rows:
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, "c" + decision_id, plan_id, station, day, "UTC", "basket",
             "prepared", 0, "[]", "{}", "0" * 64, created),
        )
    return conn


def test_the_decision_list_is_seeks_whole_days_in_production_order() -> None:
    conn = _decisions_db()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    walked = list(L._production_decisions(
        conn, from_event_date="2026-09-22", to_event_date="2026-09-22",
    ))
    conn.set_trace_callback(None)
    assert [r["decision_id"] for r in walked] == ["d3", "d1", "d5"]  # created order
    # No statement reads the decisions table end to end: ``created_at`` is
    # behind the JSON, so a scan on it reads every decision's details.
    for sql in {s for s in statements if "auto_trade_decisions" in s}:
        plan = " ".join(
            str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql)
        )
        assert "SCAN auto_trade_decisions" not in plan or "COVERING INDEX" in plan, (sql, plan)
    # A later rowid floor keeps a scope whole when it has anything after it.
    ids = [r["decision_id"] for r in L._production_decisions(conn, since_rowid=4)]
    assert ids == ["d1", "d5"]
    assert [r["in_range"] for r in L._production_decisions(conn, since_rowid=4)] == [False, True]
