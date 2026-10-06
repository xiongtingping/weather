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
    # Charter 110 replaced ladder MEMBERSHIP with an anchor and a direction.
    # Under charter 94's three rungs a limit could sit on bid-1t and be "on
    # the ladder" while the bid walked up past it; there is one rung now, so
    # the only question about a standing limit is which way its bid moved.
    kw = dict(limit_price=0.30, ask=0.40, tick=0.01, posted_at_utc=POSTED,
              dwell_seconds=300.0, far_ticks=3.0)
    # bid 0.29: the rung IS the anchor, kept whatever its age.
    on = lane.resting_rung_verdict(bid=0.29, as_of_utc="2026-09-06T12:00:00Z", **kw)
    assert (on["action"], on["reason"]) == ("keep", "on_anchor")
    # bid 0.31: the bid has risen past the rung.  Within the dwell it keeps
    # its queue place; past the dwell the rung follows the bid up, which
    # this lane spells as a cancel because it places no orders itself.
    early_up = lane.resting_rung_verdict(bid=0.31, as_of_utc="2026-09-06T10:02:00Z", **kw)
    late_up = lane.resting_rung_verdict(bid=0.31, as_of_utc="2026-09-06T12:00:00Z", **kw)
    assert (early_up["action"], early_up["reason"]) == ("keep", "bid_rose_within_dwell")
    assert (late_up["action"], late_up["reason"]) == ("cancel", "bid_rose_past_dwell")
    assert late_up["kernel_action"] == "reprice_up"
    # bid 0.28: the bid has FALLEN under the rung but is still near it.  The
    # rung does not follow it down -- that is how a limit is walked into the
    # population a collapsing ask sweeps -- so it simply waits.
    down = lane.resting_rung_verdict(bid=0.28, as_of_utc="2026-09-06T12:00:00Z", **kw)
    assert (down["action"], down["reason"]) == ("keep", "on_anchor")
    # bid 0.27: three ticks below, stale, and the direction says do not
    # re-post -- cancel outright.
    far_down = lane.resting_rung_verdict(bid=0.27, as_of_utc="2026-09-06T10:00:30Z", **kw)
    assert (far_down["action"], far_down["reason"]) == ("cancel", "bid_ran_far")
    # bid 0.34: four ticks above, stale the other way -- still a cancel here,
    # but recorded as the follow-up it is.
    far_up = lane.resting_rung_verdict(bid=0.34, as_of_utc="2026-09-06T10:00:30Z", **kw)
    assert (far_up["action"], far_up["reason"]) == ("cancel", "bid_rose_far")
    assert far_up["kernel_action"] == "reprice_up"
    # No bid, no anchor: charter 94 left the rung standing here, which is the
    # one state a collapsing ask sweeps a stale limit in.  It comes down.
    gone = lane.resting_rung_verdict(bid=0.0, as_of_utc="2026-09-06T12:00:00Z", **kw)
    assert (gone["action"], gone["reason"]) == ("cancel", "no_anchor")


def test_a_rung_the_ladder_clamped_to_the_bid_is_on_its_anchor() -> None:
    # ZBAA 2026-09-16 at 2026-09-14 12:02:33Z: 31C rested at 0.26 on a
    # 0.26/0.27 book, unchanged since the post.  bid+1t is the ask there, so
    # the ladder had clamped the rung to the bid (charter 110(一)); the rule
    # read the unclamped bid+1t as an anchor the bid had risen to and
    # cancelled the rung past the dwell, to re-post it on the ask.
    from bayes_basket_target.delta_v2_runtime import bid_anchored_ladder

    for bid, ask in ((0.26, 0.27), (0.31, 0.32), (0.07, 0.11), (0.50, 0.53)):
        ladder = bid_anchored_ladder(bid=bid, ask=ask, tick=0.01)
        assert len(ladder) == 1, (bid, ask, ladder)
        verdict = lane.resting_rung_verdict(
            limit_price=ladder[0], bid=bid, ask=ask, tick=0.01,
            posted_at_utc=POSTED, as_of_utc="2026-09-06T12:00:00Z",
            dwell_seconds=300.0, far_ticks=3.0,
        )
        assert (verdict["action"], verdict["reason"]) == ("keep", "on_anchor"), (
            bid, ask, verdict,
        )
    # The follow-up still fires when the bid really moved up under a rung:
    # posted on 0.25/0.26, the book now 0.26/0.27 -- and it follows to the
    # clamped anchor, not onto the ask.
    up = lane.resting_rung_verdict(
        limit_price=0.25, bid=0.26, ask=0.27, tick=0.01,
        posted_at_utc=POSTED, as_of_utc="2026-09-06T12:00:00Z",
        dwell_seconds=300.0, far_ticks=3.0,
    )
    assert (up["action"], up["reason"], up["kernel_action"]) == (
        "cancel", "bid_rose_past_dwell", "reprice_up",
    )
    assert up["ladder"] == [0.26]
    assert lane.RESTING_REPRICE_RULE_VERSION == "bid_anchored_up_only_v3"


def test_a_rung_that_is_the_best_bid_is_not_chased_by_itself() -> None:
    # The official book carries our own resting order.  VILK 2026-09-16 29C
    # rested 20 shares at 0.05 (posted on 0.04/0.06); at 14:19:52Z the book
    # read best bid 0.05 with exactly 20 shares -- our order, alone -- and
    # the v2 rule cancelled it as a bid that had risen past it.  EDDM 24C, 7
    # shares at 0.15, the same against a best bid of exactly 7 shares.
    kw = dict(tick=0.01, posted_at_utc=POSTED, as_of_utc="2026-09-06T12:00:00Z",
              dwell_seconds=300.0, far_ticks=3.0)
    own = lane.resting_rung_verdict(limit_price=0.05, bid=0.05, ask=0.08, **kw)
    assert (own["action"], own["reason"]) == ("keep", "on_anchor"), own
    own_wide = lane.resting_rung_verdict(limit_price=0.15, bid=0.15, ask=0.17, **kw)
    assert (own_wide["action"], own_wide["reason"]) == ("keep", "on_anchor"), own_wide
    # Others joining us at our price leave us first in the queue there.
    joined = lane.resting_rung_verdict(limit_price=0.37, bid=0.37, ask=0.39, **kw)
    assert joined["action"] == "keep", joined
    # One tick over us is a bid we are behind, and the rung follows it.
    over = lane.resting_rung_verdict(limit_price=0.24, bid=0.25, ask=0.28, **kw)
    assert (over["action"], over["reason"], over["kernel_action"]) == (
        "cancel", "bid_rose_past_dwell", "reprice_up",
    )


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


def test_inferred_cancel_from_not_found_is_declined_not_written(tmp_path) -> None:
    """A live order must not be ended by absence.

    2026-09-06 15:06:39Z: the gateway maps a not-found lookup to "cancelled"
    -- correct for a caller asking whether its own cancel landed, and wrong
    for this lane, which cancels nothing before it syncs.  Two rungs were
    written cancelled off one such read, LIMC 33C at 0.131 and KATL 84-85F at
    0.121, and both were still resting on the exchange eight minutes later.
    """

    view = lane._exchange_view({
        "ok": True,
        "status": "cancelled",
        "status_inferred_from_not_found": True,
    })
    assert view["status_inferred_from_not_found"] is True

    written: list[object] = []
    out = lane.sync_resting_intent(
        None,
        intent={
            "order_intent_id": "intent_x",
            "requested_size": 8.0,
            "filled_size": 0.0,
            "filled_notional_usdc": 0.0,
            "limit_price": 0.131,
            "exchange_order_id": "0xdead",
        },
        view=view,
        as_of_utc="2026-09-06T15:06:39Z",
        write=lambda: written.append(1),
    )
    assert out["state"] == "unknown"
    assert out["reason"] == "cancel_inferred_from_not_found_declined"
    assert written == [], "declining must not open a write transaction"


def test_a_rung_the_venue_expired_ends_as_an_expiry_and_not_a_cancel(
    monkeypatch,
) -> None:
    """GTD's own ending, which had no reader.

    ``expired`` was in neither the filled set nor the cancelled set, and it
    could not be: nothing this lane placed had an expiry, so the status was
    unreachable.  Reachable now, a status in neither set falls through to
    ``unknown`` -- which leaves the rung live in the ledger and holds its
    bucket shut behind the sequential gate for as long as the book keeps
    answering.  Recorded as EXPIRED rather than CANCEL_ACK because nobody
    asked for it, and telling the two apart is the whole reason to send GTD.
    """

    events: list[dict] = []
    monkeypatch.setattr(
        lane, "record_order_event",
        lambda conn, *, order_intent_id, event: events.append(event),
    )
    out = lane.sync_resting_intent(
        None,
        intent={
            "order_intent_id": "intent_gtd",
            "requested_size": 8.0,
            "filled_size": 0.0,
            "filled_notional_usdc": 0.0,
            "limit_price": 0.131,
            "exchange_order_id": "0xfeed",
        },
        view=lane._exchange_view({"ok": True, "status": "expired"}),
        as_of_utc="2026-09-11T02:00:00Z",
        write=lambda: _NullContext(),
    )

    assert out["state"] == "expired"
    assert [event["event_type"] for event in events] == ["EXPIRED"]
    assert events[0]["event_payload"]["exchange_status"] == "expired"


def test_a_venue_cancel_past_the_orders_own_expiry_is_the_expiry(
    monkeypatch,
) -> None:
    """The venue says ``canceled`` for a GTD order that ran out.

    LFPB 2026-09-14 29C: 13 shares at 0.08, GTD 2026-09-13T08:02:39Z
    (epoch 1789286559), read ``canceled`` at 08:03:49Z with no cancel sent
    and a book the rung rule would have kept it on.  Booked CANCEL_ACK, so
    the lane's expiry count read zero for the whole GTD era.
    """

    events: list[dict] = []
    monkeypatch.setattr(
        lane, "record_order_event",
        lambda conn, *, order_intent_id, event: events.append(event),
    )
    intent = {
        "order_intent_id": "intent_gtd",
        "requested_size": 13.0,
        "filled_size": 0.0,
        "filled_notional_usdc": 0.0,
        "limit_price": 0.08,
        "exchange_order_id": "0xe3a0",
        "expiration": 1789286559,
    }
    view = lane._exchange_view({"ok": True, "status": "canceled"})
    after = lane.sync_resting_intent(
        None, intent=intent, view=view,
        as_of_utc="2026-09-13T08:03:49.029Z", write=lambda: _NullContext(),
    )
    assert after["state"] == "expired"
    assert [event["event_type"] for event in events] == ["EXPIRED"]
    events.clear()
    # Before its horizon the same answer is a cancel.
    before = lane.sync_resting_intent(
        None, intent=intent, view=view,
        as_of_utc="2026-09-13T08:00:00Z", write=lambda: _NullContext(),
    )
    assert before["state"] == "cancelled"
    assert [event["event_type"] for event in events] == ["CANCEL_ACK"]


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_exchange_reported_cancel_is_still_recorded() -> None:
    """Only the inference is refused.  A status the exchange actually said is
    evidence and is still written, or the lane could never close a rung."""

    view = lane._exchange_view({"ok": True, "status": "canceled"})
    assert view["status_inferred_from_not_found"] is False


def test_the_pass_judges_on_the_live_book_when_it_is_newer(monkeypatch) -> None:
    """2026-10-06 09:11Z ZUUU 10-08 26°C: posted at 0.33 on the live book,
    cancelled 4 s later against a committed bid of 0.28 read 43 s before."""

    import bayes_basket_target.p1_tail_lane as tail
    from bayes_basket_target import resting_ladder_lifecycle as lifecycle

    committed = {"26°C": {"bid": 0.28, "ask": 0.47, "tick": 0.01, "observed_at": "2026-10-06T09:10:44Z"}}
    monkeypatch.setattr(lifecycle, "latest_book_by_bucket", lambda conn, **k: dict(committed))
    monkeypatch.setattr(tail, "_ws_manifest_root", lambda path: {"state": "connected"})
    live = {"buckets": [{"bucket": "26°C", "bid_price": 0.33, "ask_levels": [{"price": 0.36, "shares": 10}],
                         "price_tick_size": 0.01}], "observed_at": "2026-10-06T09:11:30Z"}
    monkeypatch.setattr(tail, "live_ws_snapshot", lambda *a, **k: live)
    load = lifecycle.live_book_loader(as_of_utc="2026-10-06T09:11:31Z")
    assert load(None, station_id="ZUUU", event_date="2026-10-08")["26°C"]["bid"] == 0.33
    # An older live book does not displace a newer committed one.
    live["observed_at"] = "2026-10-06T09:09:00Z"
    load = lifecycle.live_book_loader(as_of_utc="2026-10-06T09:11:31Z")
    assert load(None, station_id="ZUUU", event_date="2026-10-08")["26°C"]["bid"] == 0.28
    # No live scope: the committed book.
    monkeypatch.setattr(tail, "live_ws_snapshot", lambda *a, **k: None)
    load = lifecycle.live_book_loader(as_of_utc="2026-10-06T09:11:31Z")
    assert load(None, station_id="ZUUU", event_date="2026-10-08")["26°C"]["bid"] == 0.28
