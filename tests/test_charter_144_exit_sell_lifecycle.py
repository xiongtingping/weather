"""Charter 144 (四): a lane's GTD/GTC sell rests at the venue as a rung does,
and the resting-order lifecycle syncs it -- or the venue's expiry is never
learnt and the station-day's quotes stay frozen.

RKSI 2026-09-18: charter 144's exit sell (5 @ 0.21, GTD five minutes, not
post-only) was posted 09-17 12:33:07Z.  The venue took it off its book by
12:38Z; the ledger held it ``submitted``/``pending`` a day later, the
quiescence predicates refused every new quote for RKSI 09-18, and every
decision on it died ``production_quote_expired``.  ``load_resting_maker_intents``
admitted post-only intents only.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3

import pytest

from bayes_basket_target import resting_ladder_lifecycle as lane
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.execution_reconciliation import record_order_event
from bayes_basket_target.tiebreak_lane import settle_stuck_lane_cycles

TOKEN = "tok_rksi_29c"
BUCKET = "29°C"
SELL_ID = "misbuyexit_1"
EX_SELL = "0xsell"
POSTED = "2026-09-17T12:33:07Z"
EXPIRY = 1789648687  # 2026-09-17T12:38:07Z
LATER = "2026-09-17T13:30:00Z"


@contextlib.contextmanager
def _nowrite():
    yield


def _cycle(conn, cycle_id: str, *, state: str) -> None:
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, "plan_" + cycle_id, "RKSI", "2026-09-18", "Asia/Seoul", "basket",
         "a" * 64, 5.0, state, "2026-09-17T02:00:00Z", "2026-09-17T02:00:00Z"),
    )


_SHA = [0]


def _intent(conn, intent_id: str, *, cycle_id: str, action: str, size: float,
            price: float, intent_json: dict, exchange_order_id: str,
            filled: float | None = None) -> None:
    _SHA[0] += 1
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
        "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
        "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
        "intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (intent_id, "decision_" + cycle_id, cycle_id, BUCKET, TOKEN, "YES", action,
         price, size, price * size, "submitted", 1, intent_id + "-key",
         json.dumps(intent_json), format(_SHA[0], "x").rjust(64, "c"), POSTED),
    )
    record_order_event(conn, order_intent_id=intent_id, event={
        "event_type": "POST_ACK", "exchange_order_id": exchange_order_id,
        "observed_at": POSTED, "matched_size": 0.0,
        "matched_notional_usdc": 0.0, "event_payload": {},
    })
    if filled is not None:
        record_order_event(conn, order_intent_id=intent_id, event={
            "event_type": "FILL", "exchange_order_id": exchange_order_id,
            "observed_at": POSTED, "matched_size": filled,
            "matched_notional_usdc": filled * price, "event_payload": {},
        })


def _ledger(*, sell_order_type: str = "GTD") -> sqlite3.Connection:
    """The bought leg (5 shares, booked) and charter 144's exit sell of it,
    posted and unanswered."""

    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    _cycle(conn, "cycle_buy", state="settled")
    _intent(conn, "intent_buy", cycle_id="cycle_buy", action="BUY", size=5.0,
            price=0.25, intent_json={"post_only": True, "execution_candidate": "MAKER"},
            exchange_order_id="0xbuy", filled=5.0)
    _cycle(conn, "misbuyexit_cycle_1", state="submitted")
    _intent(conn, SELL_ID, cycle_id="misbuyexit_cycle_1", action="SELL", size=5.0,
            price=0.21, intent_json={
                "post_only": False, "order_type": sell_order_type,
                "expiration": EXPIRY, "execution_candidate": "EXIT_GTD",
            }, exchange_order_id=EX_SELL)
    conn.commit()
    return conn


class _Client:
    def __init__(self, *, lookup, live_ids=(), shares: float = 0.0) -> None:
        self.lookup = dict(lookup)
        self.live_ids = [str(x) for x in live_ids]
        self.shares = float(shares)
        self.cancels: list[dict] = []

    def lookup_exchange_order(self, request):
        return dict(self.lookup)

    def lookup_open_orders(self, request):
        return {"ok": True, "complete": True, "total_open_orders": len(self.live_ids),
                "open_orders": [{"id": oid} for oid in self.live_ids]}

    def lookup_position(self, request):
        return {"ok": True, "shares": self.shares,
                "balance_raw": str(int(self.shares * 1_000_000)), "decimals": 6}

    def cancel_order(self, request):
        self.cancels.append(dict(request))
        return {"ok": True, "cancelled": True, "error": ""}


def _book(conn, *, station_id, event_date):
    # A book on which charter 110's rung rule would cancel a BUY rung at
    # 0.21 outright: the bid three ticks above it, long past the dwell.
    return {BUCKET: {"bid": 0.30, "ask": 0.32, "tick": 0.01}}


def _state(conn, intent_id: str) -> tuple[str, str, float]:
    row = conn.execute(
        "SELECT i.state, r.state, COALESCE(r.filled_size,0)"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id=i.order_intent_id WHERE i.order_intent_id=?",
        (intent_id,),
    ).fetchone()
    return str(row[0]), str(row[1]), float(row[2])


def _events(conn, intent_id: str) -> list[tuple[str, float]]:
    return [
        (str(r[0]), float(r[1])) for r in conn.execute(
            "SELECT event_type, matched_size FROM basket_target_order_events"
            " WHERE order_intent_id=? ORDER BY observed_at, event_id", (intent_id,),
        )
    ]


def _cycle_state(conn, cycle_id: str) -> str:
    return str(conn.execute(
        "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?", (cycle_id,),
    ).fetchone()[0])


def test_the_lifecycle_admits_a_gtd_sell_that_is_not_post_only_and_not_a_fok() -> None:
    conn = _ledger()
    _cycle(conn, "tiebreak_cycle_1", state="settled")
    _intent(conn, "tiebreak_1", cycle_id="tiebreak_cycle_1", action="BUY", size=10.0,
            price=0.49, intent_json={"post_only": False, "order_type": "FOK"},
            exchange_order_id="0xfok")
    resting = lane.load_resting_maker_intents(conn)
    assert [r["order_intent_id"] for r in resting] == [SELL_ID]
    only = resting[0]
    assert only["order_action"] == "SELL"
    assert only["order_type"] == "GTD"
    assert only["post_only"] is False
    assert only["ladder_rung"] is False
    assert only["expiration"] == EXPIRY
    # A GTC sell (charter 100's 0.998 recycle) is admitted the same way.
    gtc = _ledger(sell_order_type="GTC")
    assert [r["order_type"] for r in lane.load_resting_maker_intents(gtc)] == ["GTC"]


def test_a_sell_the_venue_ended_at_its_horizon_is_closed_as_expired_and_frees_the_day() -> None:
    conn = _ledger()
    client = _Client(lookup={"ok": True, "status": "canceled", "response": {"size_matched": 0}})
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc=LATER, client=client, write=_nowrite, book_loader=_book,
    )
    assert receipt["examined"] == 1
    assert receipt["synced_expired"] == 1
    assert receipt["cancel_requested"] == 0 and client.cancels == []
    assert _state(conn, SELL_ID) == ("cancelled", "resolved_cancelled", 0.0)
    assert _events(conn, SELL_ID)[-1] == ("EXPIRED", 0.0)
    # The lane cycle can now settle, and with it the station-day is quiescent.
    assert settle_stuck_lane_cycles(conn) == 1
    assert _cycle_state(conn, "misbuyexit_cycle_1") == "settled"


def test_a_sell_still_resting_is_kept_and_never_run_through_the_ladder_rules() -> None:
    conn = _ledger()
    client = _Client(lookup={"ok": True, "status": "live", "response": {"size_matched": 0}})
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc=LATER, client=client, write=_nowrite, book_loader=_book,
        dead_bucket_loader=lambda conn, *, station_id, event_date, as_of_utc: ((BUCKET,), 31.0),
    )
    assert receipt["examined"] == 1
    assert receipt["kept"] == 1 and receipt["kept_not_ladder"] == 1
    assert receipt["cancel_requested"] == 0 and client.cancels == []
    assert receipt["rungs"][0]["verdict"] == {"action": "keep", "reason": "not_a_ladder_rung"}
    assert _state(conn, SELL_ID) == ("submitted", "pending", 0.0)


def test_a_departed_sell_claims_the_shares_that_left_the_account() -> None:
    # RKSI 2026-09-18 as the venue answered it on 09-18 07:32Z: the point
    # lookup empty, no open order on the token, the position zero where the
    # ledger books five bought.  Five shares left; the sell is what sold them.
    conn = _ledger()
    client = _Client(
        lookup={"ok": False, "status": "unknown", "error": "clob_order_lookup_empty_response"},
        live_ids=(), shares=0.0,
    )
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc=LATER, client=client, write=_nowrite, book_loader=_book,
    )
    assert receipt["departed_filled"] == 1
    assert receipt["departed_shares"] == pytest.approx(5.0)
    departed = receipt["rungs"][0]["departed"]
    assert departed["order_action"] == "SELL"
    assert departed["recorded_shares"] == pytest.approx(5.0)
    assert departed["held_shares"] == pytest.approx(0.0)
    assert departed["attributed_shares"] == pytest.approx(5.0)
    assert _state(conn, SELL_ID) == ("filled", "resolved_filled", 5.0)
    assert _events(conn, SELL_ID)[-1] == ("FILL", 5.0)
    assert client.cancels == []
    assert settle_stuck_lane_cycles(conn) == 1
    assert _cycle_state(conn, "misbuyexit_cycle_1") == "settled"


def test_a_departed_sell_with_the_position_intact_is_expired_unfilled() -> None:
    conn = _ledger()
    client = _Client(
        lookup={"ok": False, "status": "unknown", "error": "clob_order_lookup_empty_response"},
        live_ids=(), shares=5.0,
    )
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc=LATER, client=client, write=_nowrite, book_loader=_book,
    )
    assert receipt["departed_unfilled"] == 1
    assert _state(conn, SELL_ID) == ("cancelled", "resolved_cancelled", 0.0)
    assert _events(conn, SELL_ID)[-1] == ("EXPIRED", 0.0)


def test_the_ledgers_position_on_a_token_nets_sells_against_buys() -> None:
    # Bought 5, sold 2 -> the ledger's position is 3.  A departed BUY of 2
    # with the account holding 5 has 2 to claim; summed over both sides the
    # ledger would have "held" 7 and answered unfilled.
    conn = _ledger()
    record_order_event(conn, order_intent_id=SELL_ID, event={
        "event_type": "FILL", "exchange_order_id": EX_SELL,
        "observed_at": LATER, "matched_size": 2.0,
        "matched_notional_usdc": 0.42, "event_payload": {},
    })
    conn.execute(
        "UPDATE basket_target_order_intents SET state='cancelled' WHERE order_intent_id=?",
        (SELL_ID,),
    )
    conn.execute(
        "UPDATE basket_target_order_reconciliations SET state='resolved_partial'"
        " WHERE order_intent_id=?", (SELL_ID,),
    )
    assert lane.recorded_fills_for_token(conn, TOKEN) == pytest.approx(3.0)
    _cycle(conn, "cycle_buy_2", state="submitted")
    _intent(conn, "intent_buy_2", cycle_id="cycle_buy_2", action="BUY", size=2.0,
            price=0.24, intent_json={"post_only": True, "execution_candidate": "MAKER"},
            exchange_order_id="0xbuy2")
    conn.commit()
    client = _Client(
        lookup={"ok": False, "status": "unknown", "error": "clob_order_lookup_empty_response"},
        live_ids=(), shares=5.0,
    )
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc="2026-09-17T14:00:00Z", client=client, write=_nowrite, book_loader=_book,
    )
    assert receipt["departed_filled"] == 1
    assert _state(conn, "intent_buy_2") == ("filled", "resolved_filled", 2.0)
