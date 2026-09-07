"""An order the venue will not discuss is resolved from two facts, or not at all.

Charter 110.  ``/lookup-exchange-order`` returns an empty body for an order
the venue no longer holds; ``sync_resting_intent`` reads that as ``unknown``
and keeps, so the intent rests forever and holds its bucket shut through
``can_submit_next_order``.  Measured on the live book 2026-09-07: twelve of
fourteen rungs were in that state, the oldest since 2026-09-06T15:53, and
eleven of them had FILLED IN FULL against a ledger recording zero -- 87.40
shares, about 18.30 USDC, owned and unknown.

Nothing is inferred from the silence.  Two facts decide it: the exchange's
own COMPLETE enumeration of what is live on the token, and the account's
actual position in it.  Either one missing, and this refuses to conclude.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import resting_ladder_lifecycle as lane  # noqa: E402
from bayes_basket_target.atomic_ledger import (  # noqa: E402
    apply_target_ledger_schema,
)
from bayes_basket_target.execution_reconciliation import (  # noqa: E402
    can_submit_next_order,
    record_order_event,
)

TOKEN = "tok-77"


@contextlib.contextmanager
def _nowrite():
    yield


class _Client:
    """Answers the two questions; the point lookup is deliberately absent."""

    def __init__(self, *, live_ids=(), shares=0.0, complete=True,
                 listing_ok=True, position_ok=True):
        self.live_ids = [str(x) for x in live_ids]
        self.shares = float(shares)
        self.complete = complete
        self.listing_ok = listing_ok
        self.position_ok = position_ok
        self.calls: list[str] = []

    def lookup_open_orders(self, request):
        self.calls.append("open_orders")
        if not self.listing_ok:
            return {"ok": False, "error": "clob_open_orders_response_invalid"}
        return {
            "ok": True,
            "complete": self.complete,
            "open_orders": [{"id": oid} for oid in self.live_ids],
            "total_open_orders": len(self.live_ids),
        }

    def lookup_position(self, request):
        self.calls.append("position")
        if not self.position_ok:
            return {"ok": False, "error": "clob_position_evidence_invalid"}
        return {
            "ok": True, "shares": self.shares,
            "balance_raw": str(int(self.shares * 1_000_000)), "decimals": 6,
        }


def _ledger(intents):
    """Built the way tests/test_resting_ladder_lifecycle.py builds it -- the
    schema's own CHECK constraints (64-char shas) are not worth re-deriving."""

    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("cycle_1", "plan_1", "OEJN", "2026-09-08", "Asia/Riyadh", "basket",
         "a" * 64, 5.0, "submitted", "2026-09-07T00:00:00Z",
         "2026-09-07T00:00:00Z"),
    )
    for index, one in enumerate(intents):
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,"
            "requested_size,requested_notional_usdc,state,"
            "live_submit_authorized,idempotency_key,intent_json,intent_sha256,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (one["id"], "decision_1", "cycle_1", one["bucket"], TOKEN, "YES",
             "BUY", one["price"], one["size"], one["price"] * one["size"],
             "submitted", 1, one["id"] + "-key",
             json.dumps({"post_only": True, "execution_candidate": "MAKER"}),
             chr(ord("b") + index) * 64, "2026-09-07T00:00:00Z"),
        )
        record_order_event(conn, order_intent_id=one["id"], event={
            "event_type": "POST_ACK", "exchange_order_id": one["ex"],
            "observed_at": "2026-09-07T00:00:01Z", "matched_size": 0.0,
            "matched_notional_usdc": 0.0, "event_payload": {},
        })
    conn.commit()
    return conn


def _intent(conn, intent_id):
    row = conn.execute(
        "SELECT i.order_intent_id, cy.station_id, cy.event_date, i.bucket,"
        " i.token_id, i.limit_price, i.requested_size,"
        " COALESCE(r.filled_size,0), COALESCE(r.filled_notional_usdc,0),"
        " r.exchange_order_id"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_execution_cycles cy ON cy.cycle_id=i.cycle_id"
        " LEFT JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_intent_id=?", (intent_id,),
    ).fetchone()
    return {
        "order_intent_id": row[0], "station_id": row[1], "event_date": row[2],
        "bucket": row[3], "token_id": row[4], "limit_price": row[5],
        "requested_size": row[6], "filled_size": row[7],
        "filled_notional_usdc": row[8], "exchange_order_id": row[9],
    }


def _state(conn, intent_id):
    return conn.execute(
        "SELECT i.state, r.state, r.filled_size"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r USING(order_intent_id)"
        " WHERE i.order_intent_id=?", (intent_id,),
    ).fetchone()


ONE = [{"id": "int-a", "ex": "0xaaa", "bucket": "35°C", "price": 0.04, "size": 25.0}]


def test_a_departed_order_the_position_covers_is_booked_as_filled():
    """OEJN 35C's shape: gone from the book, twenty-five shares on the
    account, zero in the ledger."""

    conn = _ledger(ONE)
    client = _Client(live_ids=[], shares=25.0)
    out = lane.reconcile_departed_intent(
        conn, intent=_intent(conn, "int-a"), client=client,
        as_of_utc="2026-09-07T12:00:00Z", write=_nowrite,
    )
    assert out["state"] == "departed_filled"
    assert out["attributed_shares"] == pytest.approx(25.0)
    assert _state(conn, "int-a") == ("filled", "resolved_filled", 25.0)
    # And the bucket it was holding shut is free.
    assert can_submit_next_order(
        conn, bucket="35°C", token_id=TOKEN,
    )["allowed"] is True


def test_a_departed_order_the_position_does_not_cover_is_expired_not_cancelled():
    """EGLC 22C's shape: gone, and nothing on the account.  EXPIRED says the
    order existed and stopped existing; it does not claim to know why, which
    is all two facts support."""

    conn = _ledger([{**ONE[0], "size": 91.0}])
    client = _Client(live_ids=[], shares=0.0)
    out = lane.reconcile_departed_intent(
        conn, intent=_intent(conn, "int-a"), client=client,
        as_of_utc="2026-09-07T12:00:00Z", write=_nowrite,
    )
    assert out["state"] == "departed_unfilled"
    assert _state(conn, "int-a") == ("cancelled", "resolved_cancelled", 0.0)
    events = [
        row[0] for row in conn.execute(
            "SELECT event_type FROM basket_target_order_events"
            " WHERE order_intent_id='int-a'"
        )
    ]
    assert "EXPIRED" in events and "CANCEL_ACK" not in events


def test_only_the_shares_the_ledger_has_not_already_booked_are_claimed():
    """LTAC 25C's shape: six requested, 5.6 already recorded, six held.  The
    order may claim the 0.4 nobody has booked, not the six on the account."""

    conn = _ledger([{"id": "int-a", "ex": "0xaaa", "bucket": "25°C",
                     "price": 0.191, "size": 6.0}])
    record_order_event(conn, order_intent_id="int-a", event={
        "event_type": "PARTIAL_FILL", "exchange_order_id": "0xaaa",
        "observed_at": "2026-09-07T01:00:00Z", "matched_size": 5.6,
        "matched_notional_usdc": 5.6 * 0.191, "event_payload": {},
    })
    out = lane.reconcile_departed_intent(
        conn, intent=_intent(conn, "int-a"), client=_Client(shares=6.0),
        as_of_utc="2026-09-07T12:00:00Z", write=_nowrite,
    )
    assert out["recorded_shares"] == pytest.approx(5.6)
    assert out["attributed_shares"] == pytest.approx(0.4)
    assert _state(conn, "int-a") == ("filled", "resolved_filled", 6.0)


def test_two_departed_orders_on_one_token_cannot_claim_the_same_shares():
    """The position is per token, not per order.  Without the pass ledger the
    second order books shares the first already took."""

    conn = _ledger([
        {"id": "int-a", "ex": "0xaaa", "bucket": "35°C", "price": 0.04, "size": 5.0},
        {"id": "int-b", "ex": "0xbbb", "bucket": "35°C", "price": 0.04, "size": 5.0},
    ])
    client = _Client(live_ids=[], shares=5.0)
    attributed: dict[str, float] = {}
    first = lane.reconcile_departed_intent(
        conn, intent=_intent(conn, "int-a"), client=client,
        as_of_utc="2026-09-07T12:00:00Z", write=_nowrite, attributed=attributed,
    )
    second = lane.reconcile_departed_intent(
        conn, intent=_intent(conn, "int-b"), client=client,
        as_of_utc="2026-09-07T12:00:00Z", write=_nowrite, attributed=attributed,
    )
    assert first["state"] == "departed_filled"
    assert second["state"] == "departed_unfilled", (
        "five shares are on the account and the first order already took them"
    )
    assert _state(conn, "int-b")[2] == pytest.approx(0.0)


def test_a_partial_that_stays_short_is_closed_rather_than_left_pending():
    """A FILL that does not complete leaves the row ``pending``, which goes
    on holding the bucket -- the exact state this function exists to end."""

    conn = _ledger(ONE)
    out = lane.reconcile_departed_intent(
        conn, intent=_intent(conn, "int-a"), client=_Client(shares=10.0),
        as_of_utc="2026-09-07T12:00:00Z", write=_nowrite,
    )
    assert out["state"] == "departed_partial"
    state, recon, filled = _state(conn, "int-a")
    assert (state, recon) == ("cancelled", "resolved_partial")
    assert filled == pytest.approx(10.0)
    assert can_submit_next_order(
        conn, bucket="35°C", token_id=TOKEN,
    )["allowed"] is True


def test_an_incomplete_enumeration_concludes_nothing():
    """Absence from a truncated list is not absence.  This is the whole
    reason the gateway reports ``complete``."""

    conn = _ledger(ONE)
    client = _Client(live_ids=[], shares=25.0, complete=False)
    out = lane.reconcile_departed_intent(
        conn, intent=_intent(conn, "int-a"), client=client,
        as_of_utc="2026-09-07T12:00:00Z", write=_nowrite,
    )
    assert out["state"] == "unknown"
    assert out["reason"] == "enumeration_incomplete"
    assert _state(conn, "int-a")[1] == "pending", "untouched"
    assert "position" not in client.calls, "no point asking once it cannot conclude"


def test_an_order_still_in_the_live_list_is_left_alone():
    conn = _ledger(ONE)
    client = _Client(live_ids=["0xAAA"], shares=25.0)
    out = lane.reconcile_departed_intent(
        conn, intent=_intent(conn, "int-a"), client=client,
        as_of_utc="2026-09-07T12:00:00Z", write=_nowrite,
    )
    assert out["state"] == "live"
    assert _state(conn, "int-a")[1] == "pending"


def test_an_unavailable_position_concludes_nothing():
    """Knowing an order left is not knowing what it did.  Without the second
    fact this books neither a fill nor an expiry."""

    conn = _ledger(ONE)
    out = lane.reconcile_departed_intent(
        conn, intent=_intent(conn, "int-a"),
        client=_Client(live_ids=[], position_ok=False),
        as_of_utc="2026-09-07T12:00:00Z", write=_nowrite,
    )
    assert out["state"] == "unknown"
    assert out["reason"] == "position_unavailable"
    assert _state(conn, "int-a")[1] == "pending"
