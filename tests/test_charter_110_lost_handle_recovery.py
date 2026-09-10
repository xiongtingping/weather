"""An order the exchange accepted while the reply was unreadable still has an owner.

A POST that times out is written ``UNKNOWN_POST``: the request left the
process, the answer did not come back, and the intent parks at
``reconcile_required`` with NO exchange order id.  The id is the handle both
the lookup and the cancel need, so without it the order -- which may well be
live -- is invisible to every automatic lane:

* ``load_resting_maker_intents`` selects ``r.state='pending'`` AND a
  non-empty ``exchange_order_id``; an UNKNOWN_POST intent has neither.
* ``_book_verdict_for_intent`` used to end on ``if open_orders: return
  {"event": None}``, deferring to the resting lane -- which, per the line
  above, was never going to see it.
* the gateway's own memory of the submission is the only other route to the
  id, and it is pruned on a TTL.

Measured 2026-09-10: ZSQD 27C for event date 09-11, BUY 7 @ 0.16, accepted
by the exchange at 2026-09-09T13:38:19Z, still resting seventeen hours
later with ``size_matched`` zero, holding its bucket shut, while
``/order-status`` and ``/reconcile-order`` both answered ``found: false``
for its key.

The handle is now recovered from the completed enumeration -- but only when
the books can only mean one order.  These tests pin both halves: that it is
recovered, and every way it refuses to guess.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import clob_order_service_client  # noqa: E402
from bayes_basket_target import production_worker  # noqa: E402
from bayes_basket_target.execution_reconciliation import (  # noqa: E402
    can_submit_next_order,
    record_order_event,
)
from bayes_basket_target.resting_ladder_lifecycle import (  # noqa: E402
    load_resting_maker_intents,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402

TOKEN = "tok-zsqd-27"
POSTED = "2026-09-09T13:36:40.532Z"
NOW = "2026-09-10T07:30:00Z"
LIVE_ID = "0x43213831f132443abd36f344778ab4319d54cae124daf4bdb21010f59f03751b"
VENUE_CREATED = 1788961099
VENUE_CREATED_ISO = "2026-09-09T13:38:19Z"


def _live_order(**overrides) -> dict:
    return {
        "id": LIVE_ID, "status": "LIVE", "side": "BUY", "price": "0.16",
        "original_size": "7", "size_matched": "0", "outcome": "Yes",
        "asset_id": TOKEN, "created_at": VENUE_CREATED, **overrides,
    }


def _conn(intents: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles("
        "cycle_id,plan_id,station_id,event_date,settlement_timezone,"
        "strategy_lane,plan_sha256,requested_reserve_usdc,state,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("cycle-1", "plan-1", "ZSQD", "2026-09-11", "Asia/Shanghai",
         "basket", "0" * 64, 0.0, "reconcile_required", POSTED, POSTED),
    )
    for index, intent in enumerate(intents):
        conn.execute(
            "INSERT INTO basket_target_order_intents("
            "order_intent_id,decision_id,cycle_id,bucket,token_id,side,"
            "order_action,limit_price,requested_size,requested_notional_usdc,"
            "state,live_submit_authorized,idempotency_key,intent_json,"
            "intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent["order_intent_id"], "decision-1", "cycle-1",
                intent.get("bucket", "27C"), intent.get("token_id", TOKEN),
                "YES", "BUY", intent.get("limit_price", 0.16),
                intent.get("requested_size", 7.0),
                intent.get("limit_price", 0.16)
                * intent.get("requested_size", 7.0),
                "prepared", 1, intent["order_intent_id"] + "-key",
                '{"post_only": true}', f"{index:064d}", POSTED,
            ),
        )
    return conn


def _timed_out(conn: sqlite3.Connection, intent_id: str) -> None:
    """The ledger an UNKNOWN_POST leaves: parked, and with no handle."""

    with conn:
        record_order_event(
            conn,
            order_intent_id=intent_id,
            event={
                "event_type": "UNKNOWN_POST", "exchange_order_id": None,
                "observed_at": POSTED, "matched_size": 0.0,
                "matched_notional_usdc": 0.0,
                "event_payload": {"exchange_status": "timeout"},
            },
        )
    assert conn.execute(
        "SELECT i.state,COALESCE(r.exchange_order_id,''),r.state"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_intent_id=?",
        (intent_id,),
    ).fetchone() == ("reconcile_required", "", "unknown")


class _ForgetfulGateway:
    """The gateway after its TTL: it no longer knows the key.

    ``/reconcile-order`` answering ``{found: false}`` is exactly what the
    live gateway said about the ZSQD key, because the row that held the
    signed order's deterministic id had been pruned.  The enumeration does
    not depend on that memory, which is the whole point of asking it.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.orders: list[dict] = list(kwargs.get("orders") or ())
        self.complete = kwargs.get("complete", True)

    def _post(self, path, payload):
        assert path == "/reconcile-order"
        return {"ok": False, "found": False, "pending": False}

    def lookup_open_orders(self, request):
        return {
            "ok": True, "complete": self.complete,
            "total_open_orders": len(self.orders),
            "open_orders": list(self.orders),
        }

    def lookup_position(self, request):  # pragma: no cover -- not reached
        return {"ok": True, "shares": 0.0}


def _run(monkeypatch, conn, orders, *, complete=True) -> dict:
    monkeypatch.setattr(
        clob_order_service_client, "ClobOrderServiceClient",
        lambda *a, **k: _ForgetfulGateway(orders=orders, complete=complete),
    )
    worker = production_worker.TargetProductionWorker.__new__(
        production_worker.TargetProductionWorker
    )
    worker.commit_conn = conn
    return worker._reconcile_unresolved_intents(as_of_utc=NOW)


def _recon(conn, intent_id="intent-a"):
    return conn.execute(
        "SELECT i.state,COALESCE(r.exchange_order_id,''),r.state"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_intent_id=?",
        (intent_id,),
    ).fetchone()


def test_the_sole_live_order_gives_the_intent_its_handle_back(monkeypatch):
    conn = _conn([{"order_intent_id": "intent-a"}])
    _timed_out(conn, "intent-a")
    assert load_resting_maker_intents(conn) == []

    receipt = _run(monkeypatch, conn, [_live_order()])

    assert receipt["handle_recovered"] == 1
    # A live order is not a closed one, and must not be counted as one.
    assert receipt["closed"] == 0
    assert receipt["closed_by_book"] == 0
    assert receipt["state"] == "committed"
    assert receipt["places_orders"] is False

    # The handle lands; the reconciliation stays ``unknown``, and should.
    # An ack says the exchange took the order, never what it did with it,
    # and ``record_order_event`` refuses to let a POST_ACK clear an unknown
    # for exactly that reason.  What clears it is asking the exchange --
    # which the intent is now eligible to be asked about.
    assert _recon(conn) == ("reconcile_required", LIVE_ID, "unknown")
    assert conn.execute(
        "SELECT event_type,matched_size,COALESCE(exchange_order_id,'')"
        " FROM basket_target_order_events ORDER BY observed_at"
    ).fetchall() == [
        ("UNKNOWN_POST", 0.0, ""), ("POST_ACK", 0.0, LIVE_ID),
    ]

    # The whole point: the lane that can sync it or cancel it can now see it.
    resting = load_resting_maker_intents(conn)
    assert [r["order_intent_id"] for r in resting] == ["intent-a"]
    assert resting[0]["exchange_order_id"] == LIVE_ID
    # And it is told how long the rung has really been sitting.  The reprice
    # rule measures dwell from this field; stamped with this pass's clock it
    # would read as freshly posted and earn a full dwell all over again.
    assert resting[0]["posted_at_utc"] == VENUE_CREATED_ISO

    # And the bucket stays shut, because the order really is resting.
    gate = can_submit_next_order(conn, bucket="27C", token_id=TOKEN)
    assert gate["allowed"] is False


def test_no_fill_is_claimed_by_a_post_ack(monkeypatch):
    """A partly filled live order still hands over a zero-fill POST_ACK.

    What the venue has matched is the resting lane's to record from the
    exchange; adopting the handle must not also book the fill, or the first
    sync will add it a second time.
    """

    conn = _conn([{"order_intent_id": "intent-a"}])
    _timed_out(conn, "intent-a")

    _run(monkeypatch, conn, [_live_order(size_matched="3")])

    assert conn.execute(
        "SELECT filled_size,filled_notional_usdc"
        " FROM basket_target_order_reconciliations"
    ).fetchall() == [(0.0, 0.0)]
    payload = json.loads(conn.execute(
        "SELECT event_json FROM basket_target_order_events"
        " WHERE event_type='POST_ACK'"
    ).fetchone()[0])
    # Written down, so the first sync can be checked against it.
    assert payload["venue_size_matched"] == "3"
    assert payload["recovered_by"] == "intent_reconcile_sole_live_order"
    assert payload["observed_at_source"] == "venue_created_at"
    assert payload["recovered_at"] == NOW


def test_an_empty_enumeration_is_still_the_departed_order_path(monkeypatch):
    """Nothing live and nothing held means the order never reached the
    exchange -- Charter 110's existing verdict, untouched by any of this."""

    conn = _conn([{"order_intent_id": "intent-a"}])
    _timed_out(conn, "intent-a")

    receipt = _run(monkeypatch, conn, [])

    assert receipt["handle_recovered"] == 0
    assert receipt["closed"] == 1
    assert _recon(conn) == ("failed", "", "resolved_not_placed")


@pytest.mark.parametrize("orders,why", [
    ([_live_order(), _live_order(id="0xother")], "two live orders"),
    ([_live_order(side="SELL")], "side"),
    ([_live_order(price="0.17")], "price"),
    ([_live_order(original_size="8")], "size"),
    ([_live_order(id="")], "no id"),
    ([_live_order(price="abc")], "unreadable price"),
])
def test_it_refuses_whenever_the_order_is_not_uniquely_this_intents(
    monkeypatch, orders, why,
):
    conn = _conn([{"order_intent_id": "intent-a"}])
    _timed_out(conn, "intent-a")

    receipt = _run(monkeypatch, conn, orders)

    assert receipt["handle_recovered"] == 0, why
    assert _recon(conn) == ("reconcile_required", "", "unknown"), why


def test_a_truncated_enumeration_recovers_nothing(monkeypatch):
    conn = _conn([{"order_intent_id": "intent-a"}])
    _timed_out(conn, "intent-a")

    receipt = _run(monkeypatch, conn, [_live_order()], complete=False)

    assert receipt["handle_recovered"] == 0
    assert _recon(conn) == ("reconcile_required", "", "unknown")


def test_a_second_unresolved_intent_on_the_token_stops_it(monkeypatch):
    """Two claimants cannot be told apart by a price and a size."""

    conn = _conn([
        {"order_intent_id": "intent-a"},
        {"order_intent_id": "intent-b"},
    ])
    _timed_out(conn, "intent-a")
    _timed_out(conn, "intent-b")

    receipt = _run(monkeypatch, conn, [_live_order()])

    assert receipt["handle_recovered"] == 0
    assert _recon(conn, "intent-a") == ("reconcile_required", "", "unknown")
    assert _recon(conn, "intent-b") == ("reconcile_required", "", "unknown")


def test_an_id_another_intent_already_carries_is_never_adopted(monkeypatch):
    """The order belongs to whoever the ledger already says it belongs to."""

    conn = _conn([
        {"order_intent_id": "intent-a"},
        {"order_intent_id": "intent-b", "bucket": "28C"},
    ])
    _timed_out(conn, "intent-a")
    with conn:
        record_order_event(
            conn,
            order_intent_id="intent-b",
            event={
                "event_type": "POST_ACK", "exchange_order_id": LIVE_ID,
                "observed_at": POSTED, "matched_size": 0.0,
                "matched_notional_usdc": 0.0, "event_payload": {},
            },
        )

    receipt = _run(monkeypatch, conn, [_live_order()])

    assert receipt["handle_recovered"] == 0
    assert _recon(conn, "intent-a") == ("reconcile_required", "", "unknown")


def test_a_venue_without_a_creation_time_falls_back_to_this_pass(monkeypatch):
    """Reported, not invented.  An unstamped order dwells from now, and the
    payload says which clock the stamp came from."""

    conn = _conn([{"order_intent_id": "intent-a"}])
    _timed_out(conn, "intent-a")

    _run(monkeypatch, conn, [_live_order(created_at=None)])

    stamp, payload = conn.execute(
        "SELECT observed_at,event_json FROM basket_target_order_events"
        " WHERE event_type='POST_ACK'"
    ).fetchone()
    assert stamp == NOW
    assert json.loads(payload)["observed_at_source"] == "as_of_utc"


def test_a_creation_stamp_in_the_future_is_refused(monkeypatch):
    """Milliseconds read as seconds lands tens of millennia out.

    A stamp after this pass would make dwell negative, which clamps to
    zero, which reads as "just posted" on every pass forever -- the exact
    thing the venue stamp was adopted to prevent.
    """

    conn = _conn([{"order_intent_id": "intent-a"}])
    _timed_out(conn, "intent-a")

    _run(monkeypatch, conn, [_live_order(created_at=VENUE_CREATED * 1000)])

    stamp, payload = conn.execute(
        "SELECT observed_at,event_json FROM basket_target_order_events"
        " WHERE event_type='POST_ACK'"
    ).fetchone()
    assert stamp == NOW
    assert json.loads(payload)["observed_at_source"] == "as_of_utc"
    # The number the venue actually sent is still on the record.
    assert json.loads(payload)["venue_created_at"] == VENUE_CREATED * 1000
