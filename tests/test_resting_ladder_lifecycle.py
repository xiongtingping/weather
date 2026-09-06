"""Charter 94's dynamic adjustment: the resting ladder follows the bid."""

from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target import resting_ladder_lifecycle as lane
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.execution_reconciliation import (
    can_submit_next_order,
    record_order_event,
)

POSTED = "2026-09-06T10:00:05Z"


def _ledger(*, limit_price: float = 0.30, requested: float = 5.0) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("cycle_1", "plan_1", "KDAL", "2026-09-07", "America/Chicago", "basket",
         "a" * 64, 5.0, "submitted", "2026-09-06T10:00:00Z", "2026-09-06T10:00:00Z"),
    )
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
        "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
        "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
        "intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("intent_1", "decision_1", "cycle_1", "98-99°F", "tok", "YES", "BUY",
         limit_price, requested, limit_price * requested, "submitted", 1, "key_1",
         json.dumps({"post_only": True, "execution_candidate": "MAKER"}),
         "b" * 64, "2026-09-06T10:00:00Z"),
    )
    record_order_event(conn, order_intent_id="intent_1", event={
        "event_type": "POST_ACK", "exchange_order_id": "0xabc",
        "observed_at": POSTED, "matched_size": 0.0,
        "matched_notional_usdc": 0.0, "event_payload": {},
    })
    conn.commit()
    return conn


class _Client:
    def __init__(self, lookups, *, cancel_ok: bool = True) -> None:
        self.lookups = list(lookups)
        self.cancels: list[dict] = []
        self.cancel_ok = cancel_ok

    def lookup_exchange_order(self, request):
        assert request["order_id"] == "0xabc"
        return self.lookups.pop(0)

    def cancel_order(self, request):
        self.cancels.append(dict(request))
        return {"ok": self.cancel_ok, "cancelled": self.cancel_ok,
                "error": "" if self.cancel_ok else "gateway_http_error"}


def _live(matched: float = 0.0) -> dict:
    return {"ok": True, "status": "live", "response": {"size_matched": matched}}


def _book(bid: float, ask: float = 0.40, tick: float = 0.01):
    return lambda conn, *, station_id, event_date: {
        "98-99°F": {"bid": bid, "ask": ask, "tick": tick},
    }


def _state(conn) -> tuple[str, str, float]:
    row = conn.execute(
        "SELECT i.state, r.state, r.filled_size FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r"
        " ON r.order_intent_id=i.order_intent_id WHERE i.order_intent_id='intent_1'"
    ).fetchone()
    return str(row[0]), str(row[1]), float(row[2])


def test_the_verdict_keeps_rungs_on_the_ladder_and_cancels_the_ones_off_it() -> None:
    kw = dict(limit_price=0.30, ask=0.40, tick=0.01, posted_at_utc=POSTED,
              dwell_seconds=300.0, far_ticks=3.0)
    # bid 0.29: the rung is bid+1t, on the ladder, whatever its age.
    assert lane.resting_rung_verdict(bid=0.29, as_of_utc="2026-09-06T12:00:00Z", **kw)["action"] == "keep"
    # bid 0.31: the rung is bid-1t, still on the ladder.
    assert lane.resting_rung_verdict(bid=0.31, as_of_utc="2026-09-06T12:00:00Z", **kw)["reason"] == "on_ladder"
    # bid 0.32: two ticks off, within the dwell -> keep; past it -> cancel.
    early = lane.resting_rung_verdict(bid=0.32, as_of_utc="2026-09-06T10:02:00Z", **kw)
    late = lane.resting_rung_verdict(bid=0.32, as_of_utc="2026-09-06T10:10:00Z", **kw)
    assert (early["action"], early["reason"]) == ("keep", "off_ladder_within_dwell")
    assert (late["action"], late["reason"]) == ("cancel", "off_ladder_past_dwell")
    # bid 0.34: four ticks off -> cancelled at once.
    far = lane.resting_rung_verdict(bid=0.34, as_of_utc="2026-09-06T10:00:30Z", **kw)
    assert (far["action"], far["reason"]) == ("cancel", "bid_ran_far")
    # No bid, no anchor: the rung is left where it is.
    assert lane.resting_rung_verdict(bid=0.0, as_of_utc="2026-09-06T12:00:00Z", **kw)["reason"] == "no_anchor"


def test_a_rung_on_the_ladder_is_synced_and_kept() -> None:
    conn = _ledger()
    client = _Client([_live()])
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc="2026-09-06T12:00:00Z", client=client, book_loader=_book(0.29),
    )
    assert receipt["examined"] == 1 and receipt["kept"] == 1
    assert client.cancels == []
    assert _state(conn) == ("submitted", "pending", 0.0)


def test_a_rung_the_bid_ran_from_is_cancelled_confirmed_and_frees_the_bucket() -> None:
    conn = _ledger()
    client = _Client([_live(), {"ok": True, "status": "cancelled", "response": {"size_matched": 0}}])
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc="2026-09-06T10:00:30Z", client=client, book_loader=_book(0.34),
    )
    assert receipt["cancel_requested"] == 1 and receipt["cancel_confirmed"] == 1
    assert client.cancels[0]["order_id"] == "0xabc"
    assert client.cancels[0]["idempotency_key"].startswith("key_1:cancel:")
    assert _state(conn) == ("cancelled", "resolved_cancelled", 0.0)
    assert can_submit_next_order(conn, bucket="98-99°F", token_id="tok")["allowed"] is True


def test_a_partly_filled_rung_cancelled_keeps_its_fill_and_ends() -> None:
    conn = _ledger()
    client = _Client([_live(2.0), {"ok": True, "status": "cancelled", "response": {"size_matched": 2.0}}])
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc="2026-09-06T10:10:00Z", client=client, book_loader=_book(0.32),
    )
    assert receipt["synced_partial"] == 1 and receipt["cancel_confirmed"] == 1
    assert _state(conn) == ("cancelled", "resolved_partial", 2.0)
    assert can_submit_next_order(conn, bucket="98-99°F", token_id="tok")["allowed"] is True
    events = sorted(row[0] for row in conn.execute(
        "SELECT event_type FROM basket_target_order_events"
    ))
    assert events == sorted(["POST_ACK", "PARTIAL_FILL", "CANCEL_ACK"])


def test_a_rung_that_filled_while_resting_is_recorded_as_a_fill() -> None:
    conn = _ledger()
    client = _Client([{"ok": True, "status": "matched", "response": {"size_matched": 5.0}}])
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc="2026-09-06T10:30:00Z", client=client, book_loader=_book(0.34),
    )
    assert receipt["synced_filled"] == 1 and client.cancels == []
    assert _state(conn) == ("filled", "resolved_filled", 5.0)


def test_a_refused_cancel_leaves_the_rung_resting_for_the_next_pass() -> None:
    conn = _ledger()
    client = _Client([_live()], cancel_ok=False)
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc="2026-09-06T10:00:30Z", client=client, book_loader=_book(0.34),
    )
    assert receipt["cancel_requested"] == 1 and receipt["cancel_confirmed"] == 0
    assert receipt["failures"] and "cancel_not_ok" in receipt["failures"][0]["reason"]
    assert _state(conn) == ("submitted", "pending", 0.0)


def test_the_ledger_re_anchors_on_the_same_cadence() -> None:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import paper_trading_ledger as ledger  # noqa: PLC0415

    assert ledger.MAKER_RESTING_ANCHORED is True
    assert ledger.MAKER_RESTING_REPRICE_MINUTES == pytest.approx(
        lane.RESTING_REPRICE_DWELL_SECONDS / 60.0
    )
    assert ledger.RESTING_REPRICE_DWELL_SECONDS == lane.RESTING_REPRICE_DWELL_SECONDS
