"""Charter 94's dynamic adjustment of the resting ladder (user ruling,
2026-09-06: "动态调整啊，而不是不撤不改").

A rung rests at bid+1t, bid or bid-1t of the book it was posted against.
Every pass this lane asks the gateway what the exchange did with each
resting maker order and writes the answer to the M6 reconciliation ledger
-- a fill, a partial fill, a cancel -- because nothing else polls a resting
order (the taker era never needed to).  Then, for a rung still resting, it
reads the ladder that stands on the current book: a rung that has fallen
out of it is cancelled -- after a dwell, or at once when the bid has run
far -- and the next compile of the scope, whose sequential gate the cancel
frees, posts the ladder that stands.  Rungs still on the ladder keep their
place in the queue.

Reads the exchange through the local gateway only (lookup and cancel on
one order id), writes only the reconciliation ledger.  It never places an
order.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import sqlite3
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .basket_kernel.delta_v2 import (
    LEG_ACTION_CANCEL,
    LEG_ACTION_REPRICE_UP,
    RESTING_RUNG_ACTION_RULE_VERSION,
    resting_rung_action_v1,
)
from .delta_v2_runtime import MAKER_LADDER_SHAPE, bid_anchored_ladder
from .execution_reconciliation import (
    fill_is_complete,
    record_order_event,
    resolve_cancelled_after_partial_fill,
)

SCHEMA = "bayes_basket_target_resting_reprice_v1"
RESTING_REPRICE_RULE_VERSION = RESTING_RUNG_ACTION_RULE_VERSION
DEPARTED_RECONCILE_RULE_VERSION = "departed_order_two_fact_v1"
# Charter 135 (二) (user ruling, 2026-09-14: "死桶挂单立即撤单"): a resting buy
# on a bucket the day's observed high has already ruled out is cancelled on
# the first pass that sees it, ahead of any book and without a dwell.
# Charter 125 made that bucket arithmetically dead for every new decision;
# nothing read it for the orders already standing, because this lane read
# the book and nothing else -- a 27°C bid posted at 11:30 kept resting after
# the 12:10 report read 28°C, until its GTD ran out, and anyone holding 27°C
# would sell into it.  The predicate is charter 125's own
# (``dead_buckets_for_upper_bounds``), never restated here.
DEAD_BUCKET_CANCEL_RULE_VERSION = "resting_buy_on_observed_dead_bucket_v1"
# Charter 135 (三) (user ruling, 2026-09-14: "盘口过期撤单"): a rung is priced
# against a book, and a rung whose book this lane cannot see -- none for the
# bucket, or the scope's newest committed book older than this -- is not
# being priced at all.  New orders already refuse a stale quote; the rungs
# already standing did not, because the book reader dropped the snapshot's
# clock and a missing book meant "keep".  Cancelled, and charter 135 (一)
# makes the cancel a decision moment, so the strategy re-seats once a fresh
# book is back.
#
# One hour, read off the store (2026-09-15, 59,775 consecutive committed
# snapshots per scope over 09-12..09-15): 60.6% arrive within five minutes,
# 93.4% within twenty, 99.3% within the hour; the 392 gaps longer than an
# hour are the outages this exists for (ZUUU 09-08..09 went 21.6 hours
# without a book).  A tighter bound would cancel on the rotation's ordinary
# 20-60 minute gaps (5.9%) and spend queue position for nothing.
RESTING_BOOK_STALE_SECONDS = float(
    os.environ.get("PM_RESTING_BOOK_STALE_SECONDS", "") or 3600.0
)
if RESTING_BOOK_STALE_SECONDS <= 0.0:
    raise ValueError(
        f"resting book stale bound must be positive: {RESTING_BOOK_STALE_SECONDS}"
    )
STALE_BOOK_CANCEL_RULE_VERSION = "resting_buy_on_unseen_book_v1"
DeadBucketLoader = Callable[..., tuple[tuple[str, ...], float | None]]
# How long a rung that has fallen out of the ladder is left standing before
# it is cancelled.  The ledger measured that re-posting every 15 minutes
# lost queue position for nothing; a one-tick flicker of the bid should not
# move a rung that was inside the ladder a moment ago.
RESTING_REPRICE_DWELL_SECONDS = float(
    os.environ.get("PM_RESTING_REPRICE_DWELL_SECONDS", "") or 300.0
)
# A rung this many ticks from the current bid is cancelled without waiting:
# the bid has run, the rung is not resting near anything any more.
RESTING_REPRICE_FAR_TICKS = float(
    os.environ.get("PM_RESTING_REPRICE_FAR_TICKS", "") or 3.0
)
# Charter 180: how many exchange lookups a production pass keeps in flight.
# The lookups are the pass's wall clock -- one gateway round trip (through
# the proxy to the exchange) each, ~1.3s, fifteen resting orders asked one
# after another: 19.9-20.0s every cycle (2026-09-21), the largest single span
# after charter 179.  Only the asking overlaps; every answer is still acted
# on one rung at a time, in the pass's order.  Six.  Charter 187 raised it
# to fifteen (user: 「调并发」) and it was put back the same hour: at six the
# pass ran 7-13s on each of 22 cycles (12:02-12:44Z), at fifteen three of
# eight passes ran 30s, 83s and 103s -- concurrent GETs came back empty,
# read as "unknown", and each sent its rung down the sequential departed-
# order reconcile (enumeration + position), 75s in one pass.
RESTING_LOOKUP_CONCURRENCY = int(
    os.environ.get("PM_RESTING_LOOKUP_CONCURRENCY", "") or 6
)
if RESTING_LOOKUP_CONCURRENCY < 1:
    raise ValueError(
        f"resting lookup concurrency must be >= 1: {RESTING_LOOKUP_CONCURRENCY}"
    )
EPS = 1e-9
_CANCELLED = frozenset({"cancelled", "canceled"})
_FILLED = frozenset({"matched", "filled"})
# The venue's own word for an order whose GTD expiry has passed.  It was in
# neither set until 2026-09-11 and could not be: nothing this lane placed had
# an expiry, so the status was unreachable.  Under GTD it is reachable on
# every rung that outlives its horizon, and a status in neither set falls
# through to ``unknown``, which leaves the rung live in the ledger and its
# bucket shut behind the sequential gate for as long as the book keeps
# answering.  Kept separate from ``_CANCELLED`` rather than folded into it,
# because an expiry is not a cancel: nobody asked for it, and the difference
# is the whole reason to send GTD.
_EXPIRED = frozenset({"expired"})
_RESTING = frozenset({"live", "open", "delayed", "unmatched", ""})
#: Order types the venue holds until filled or until their own horizon.  A
#: fill-or-kill is answered at the send and never rests.
_RESTING_ORDER_TYPES = frozenset({"GTD", "GTC"})

# Charter 250 (user ruling 2026-10-03 「卖单降频」): a resting order that is
# not a rung and carries no horizon of its own -- today the 0.998 recycle
# sells, GTC -- is asked of the venue at most once in this many seconds
# instead of on every pass.  Measured 2026-10-03: 27 of the 30 resting orders
# were such sells, some resting since 09-22, each a venue lookup a pass and
# six of them a departed-order reconcile on top (the point lookup answers
# empty), 14-22 s of every cycle in front of the decisions.  A rung, and a
# sell with a GTD horizon (charter 144's exits, which hold their station-day
# shut until synced), is still asked every pass.  The cost is the one stated
# with the ruling: a 0.998 sell's fill, and the cash it frees, is booked up
# to this long after it happens.  "0": every pass, as before.
GTC_SELL_LOOKUP_INTERVAL_SEC = float(
    os.environ.get("PM_RESTING_GTC_SELL_LOOKUP_INTERVAL_SEC", "") or 300.0
)
if GTC_SELL_LOOKUP_INTERVAL_SEC < 0:
    raise ValueError(
        f"GTC sell lookup interval must be >= 0: {GTC_SELL_LOOKUP_INTERVAL_SEC}"
    )
# order_intent_id -> monotonic clock of its last answered lookup (this process)
_GTC_SELL_LAST_LOOKUP: dict[str, float] = {}


def on_the_slow_clock(intent: Mapping[str, Any]) -> bool:
    """Whether charter 250's interval applies to this resting order."""

    return (
        not intent.get("ladder_rung", True)
        and str(intent.get("order_type") or "").upper() == "GTC"
        and str(intent.get("order_action") or "").upper() == "SELL"
    )



def _iso(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resting_rung_verdict(
    *,
    limit_price: float,
    bid: float,
    ask: float,
    tick: float,
    posted_at_utc: str,
    as_of_utc: str,
    dwell_seconds: float | None = None,
    far_ticks: float | None = None,
) -> dict[str, Any]:
    """Keep or cancel one resting rung against the anchor that stands.

    Pure, and a thin adapter over the kernel's ``resting_rung_action_v1``
    (charter 110): ``keep`` while the rung is on the anchor or its bid has
    only fallen a little; ``cancel`` with no bid at all, when the bid has
    run ``far_ticks`` away, and when the bid has RISEN past the dwell -- the
    last of those being the follow-up, which this lane expresses as a cancel
    because it places no orders of its own.

    What charter 110 changed here: the shipped rule kept a rung standing on
    a book with no bid (``no_anchor``), and that is exactly the state a
    collapsing ask sweeps a stale limit in; and it treated a bid that rose
    and a bid that fell alike, so the ladder chased a leaving market down
    while never following an arriving one up.
    """

    dwell = RESTING_REPRICE_DWELL_SECONDS if dwell_seconds is None else float(dwell_seconds)
    far = RESTING_REPRICE_FAR_TICKS if far_ticks is None else float(far_ticks)
    price = float(limit_price)
    ladder = bid_anchored_ladder(bid=float(bid), ask=float(ask), tick=float(tick))
    age = max(0.0, (_iso(as_of_utc) - _iso(posted_at_utc)).total_seconds())
    distance = abs(price - float(bid)) / float(tick) if float(tick) > 0 else 0.0
    verdict = {
        "rule": RESTING_REPRICE_RULE_VERSION,
        "limit_price": price,
        "bid": float(bid),
        "ask": float(ask),
        "ladder": ladder,
        "distance_ticks": round(distance, 6),
        "age_seconds": round(age, 3),
        "dwell_seconds": dwell,
        "far_ticks": far,
    }
    # Charter 110: the decision is the kernel's, so this lane and
    # ``execution_completion_verdict_v1`` cannot answer the same book two
    # ways.  This lane issues cancels and does not place orders, so a
    # ``reprice_up`` is a cancel here -- the next compile re-mints the rung
    # on the new anchor through the ordinary candidate path -- and it is
    # reported under its own reason so a receipt says which of the two it
    # was.
    action = resting_rung_action_v1(
        limit_price=price, bid=float(bid), ask=float(ask), tick=float(tick),
        age_seconds=age, dwell_seconds=dwell, far_ticks=far,
    )
    verdict["kernel_action"] = action["action"]
    if action["action"] in {LEG_ACTION_CANCEL, LEG_ACTION_REPRICE_UP}:
        return {**verdict, "action": "cancel", "reason": action["reason"]}
    return {**verdict, "action": "keep", "reason": action["reason"]}


def resting_rung_keep_or_cancel(
    *,
    intent: Mapping[str, Any],
    dead_buckets: tuple[str, ...] | frozenset[str] | set[str],
    running_high: float | None,
    load_book: Callable[[], Mapping[str, Any] | None],
    as_of_utc: str,
    stale_book_seconds: float,
    dwell_seconds: float | None = None,
    far_ticks: float | None = None,
) -> dict[str, Any]:
    """Keep or cancel one resting rung, in the order the rules bind.

    Charter 135 (二) first -- the day's thermometer needs no book -- then
    135 (三)'s stale or missing book, then charter 110's ladder verdict.
    Pure: the pass hands in the scope's dead buckets and a book reader, the
    paper ledger its own (user ruling 2026-09-24: 「账本对齐」), so a rung is
    kept or cancelled by one rule on both sides.  ``cancel_rule`` names the
    rule that answered (empty for a keep on a clockless missing book).
    """

    if (
        str(intent.get("order_action") or "BUY").upper() == "BUY"
        # A NO buy (charter 231) on a dead bucket is a certain win, not a
        # loss to stop; the rule is for the YES rungs it was written for.
        and str(intent.get("side") or "YES").upper() == "YES"
        and intent["bucket"] in dead_buckets
    ):
        return {
            "rule": DEAD_BUCKET_CANCEL_RULE_VERSION,
            "action": "cancel",
            "reason": "bucket_dead_by_observed_high",
            "observed_running_high_c": running_high,
            "limit_price": intent["limit_price"],
            "cancel_rule": DEAD_BUCKET_CANCEL_RULE_VERSION,
        }
    book = load_book()
    age = book_age_seconds(book, as_of_utc=as_of_utc)
    if age is not None and age > float(stale_book_seconds):
        # Charter 135 (三): nothing to price this rung against.  Ahead of the
        # book rule, which cannot answer without a book, and without a dwell
        # -- the bound already is the patience.
        return {
            "rule": STALE_BOOK_CANCEL_RULE_VERSION,
            "action": "cancel",
            "reason": "book_missing" if math.isinf(age) else "book_stale",
            "book_age_seconds": None if math.isinf(age) else round(age, 3),
            "stale_book_seconds": float(stale_book_seconds),
            "limit_price": intent["limit_price"],
            "cancel_rule": STALE_BOOK_CANCEL_RULE_VERSION,
        }
    if not book:
        return {"action": "keep", "reason": "no_book"}
    verdict = resting_rung_verdict(
        limit_price=intent["limit_price"],
        bid=book["bid"], ask=book["ask"], tick=book["tick"],
        posted_at_utc=intent["posted_at_utc"], as_of_utc=as_of_utc,
        dwell_seconds=dwell_seconds, far_ticks=far_ticks,
    )
    if age is not None:
        verdict["book_age_seconds"] = round(age, 3)
    verdict["cancel_rule"] = RESTING_REPRICE_RULE_VERSION
    return verdict


def latest_book_by_bucket(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> dict[str, dict[str, float]]:
    """The newest committed official book of the scope, one row per bucket."""

    row = conn.execute(
        "SELECT buckets_json, observed_at FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " ORDER BY observed_at DESC, sequence DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in json.loads(row[0] or "[]"):
        if not isinstance(entry, Mapping):
            continue
        levels = entry.get("ask_levels") or []
        ask = entry.get("best_ask")
        if ask is None and levels:
            ask = (levels[0] or {}).get("price")
        out[str(entry.get("bucket") or "")] = {
            "bid": float(entry.get("bid_price") or 0.0),
            "ask": float(ask or 0.0),
            "tick": float(entry.get("price_tick_size") or 0.001),
            # Charter 135 (三): the book's own clock, which this reader used
            # to drop -- without it a book from yesterday priced a rung the
            # same as one from a minute ago.
            "observed_at": str(row[1] or ""),
        }
    return out


def mirror_book(row: Mapping[str, Any]) -> dict[str, Any]:
    """The NO token's book, read off the YES book it is the complement of.

    Charter 231's NO rungs: the CLOB matches a NO bid against a YES bid and
    a NO ask against a YES ask through the complement, so NO bid is
    ``1 - YES ask`` and NO ask ``1 - YES bid`` (zero where the YES side it
    comes from is empty, which leaves the ladder without an anchor exactly
    as a missing YES side does).  Same clock and tick as the YES book.
    """

    tick = float(row.get("tick") or 0.001)
    digits = max(0, int(round(-math.log10(tick)))) if tick > 0 else 6
    yes_bid = float(row.get("bid") or 0.0)
    yes_ask = float(row.get("ask") or 0.0)
    return {
        **dict(row),
        "bid": round(1.0 - yes_ask, digits) if yes_ask > 0.0 else 0.0,
        "ask": round(1.0 - yes_bid, digits) if yes_bid > 0.0 else 0.0,
    }


def book_age_seconds(book: Mapping[str, Any] | None, *, as_of_utc: str) -> float | None:
    """How old the book a rung is priced against is; None when it cannot say.

    ``math.inf`` for no book at all.  None only for a book that carries no
    clock -- an injected reader that predates the field -- which is not
    evidence of staleness and is not treated as such.
    """

    if not book:
        return math.inf
    observed = str(book.get("observed_at") or "")
    if not observed:
        return None
    return max(0.0, (_iso(as_of_utc) - _iso(observed)).total_seconds())


def load_resting_maker_intents(
    conn: sqlite3.Connection, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Every post-only intent the exchange may still be holding.

    ``submitted`` with a pending reconciliation is a resting order;
    ``reconcile_required`` with a pending reconciliation is one that has
    filled in part and still rests.  What actually decides membership is
    the EXCHANGE ORDER ID: it is the handle both the lookup and the cancel
    need, and an intent without one cannot be acted on here at all.

    ``unknown`` is admitted alongside ``pending`` once that handle exists.
    A reconciliation is ``unknown`` when nobody knows whether money moved,
    and the ledger will not let a POST_ACK clear that -- rightly, since an
    ack says nothing about fills.  What clears it is asking the exchange,
    which is precisely what this lane does; excluding ``unknown`` meant the
    one state that most needs the question asked was the one state never
    admitted to ask it.  Before 2026-09-10 the exclusion was invisible
    because nothing ever wrote a handle onto an ``unknown`` row; the
    reconcile lane now does, for a submission whose POST timed out while
    the order was accepted, and this is where that order is picked up.
    """

    rows = conn.execute(
        "SELECT i.order_intent_id, cy.station_id, cy.event_date, i.bucket,"
        " i.token_id, i.limit_price, i.requested_size, i.idempotency_key,"
        " i.created_at, i.intent_json, i.order_action, i.state,"
        " r.exchange_order_id, r.filled_size, r.filled_notional_usdc, r.state, i.side"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_execution_cycles cy ON cy.cycle_id=i.cycle_id"
        " JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id=i.order_intent_id"
        " WHERE i.state IN ('submitted','reconcile_required')"
        "   AND r.state IN ('pending','unknown')"
        "   AND r.exchange_order_id IS NOT NULL AND r.exchange_order_id<>''"
        " ORDER BY i.created_at LIMIT ?",
        (int(limit),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            intent = json.loads(row[9] or "{}")
        except (TypeError, ValueError):
            intent = {}
        post_only = intent.get("post_only") is True
        order_type = str(intent.get("order_type") or "").strip().upper()
        order_action = str(row[10] or "BUY").strip().upper()
        # A post-only rung rests by construction.  An order that is not
        # post-only rests too when the book does not take it at once and it
        # carries a horizon the venue enforces -- charter 144's exit sells
        # (GTD, five minutes), charter 100's 0.998 recycle sells (GTC) -- and
        # the venue ends it on its own clock, telling nobody.  Until
        # 2026-09-18 only post-only intents were admitted here, so an exit
        # sell the venue had expired stayed ``submitted``/``pending`` in the
        # ledger for good and held its station-day's quotes shut through the
        # quiescence predicates (RKSI 2026-09-18: posted 09-17 12:33Z, gone
        # from the venue by 12:38Z, still open a day later).
        if not post_only and order_type not in _RESTING_ORDER_TYPES:
            continue
        posted = conn.execute(
            "SELECT MAX(observed_at) FROM basket_target_order_events"
            " WHERE order_intent_id=? AND event_type='POST_ACK'",
            (row[0],),
        ).fetchone()
        out.append({
            "order_intent_id": str(row[0]),
            "station_id": str(row[1]),
            "event_date": str(row[2]),
            "bucket": str(row[3]),
            "token_id": str(row[4]),
            "limit_price": float(row[5]),
            "requested_size": float(row[6]),
            "idempotency_key": str(row[7]),
            "posted_at_utc": str((posted[0] if posted else None) or row[8]),
            "order_action": order_action,
            # The outcome leg.  A NO rung (charter 231) is judged against the
            # YES book mirrored -- see ``mirror_book``.
            "side": str(row[16] or "YES").strip().upper(),
            "intent_state": str(row[11]),
            "post_only": post_only,
            "order_type": order_type,
            # Only a post-only BUY is a rung of charter 94's ladder; the
            # reprice and cancel rules below are for rungs.  Everything else
            # admitted here is synced and left to its own horizon.
            "ladder_rung": post_only and order_action == "BUY",
            "exchange_order_id": str(row[12]),
            "filled_size": float(row[13] or 0.0),
            "filled_notional_usdc": float(row[14] or 0.0),
            "reconciliation_state": str(row[15]),
            # The GTD horizon the order was sent with (epoch seconds), so a
            # venue "canceled" past it can be told from a cancel.
            "expiration": intent.get("expiration"),
        })
    return out


def _past_own_expiry(intent: Mapping[str, Any], as_of_utc: str) -> bool:
    """Whether the order's own GTD horizon has passed at ``as_of_utc``."""

    raw = intent.get("expiration")
    try:
        horizon = datetime.fromtimestamp(int(raw), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    return _iso(as_of_utc) >= horizon


#: What the gateway client writes into ``error`` when the request left this
#: process and no answer came back (``clob_order_service_client._post``).
GATEWAY_TRANSPORT_MARK = "gateway_transport"


def _gateway_unreachable(reason: Any) -> bool:
    """Whether a lookup failed on transport rather than being answered."""
    return GATEWAY_TRANSPORT_MARK in str(reason or "")


def _exchange_view(lookup: Mapping[str, Any]) -> dict[str, Any]:
    """Status and cumulative fill off a ``/lookup-exchange-order`` reply."""

    response = lookup.get("response")
    response = response if isinstance(response, Mapping) else {}
    status = str(lookup.get("status") or response.get("status") or "").strip().lower()
    matched = 0.0
    for key in ("size_matched", "sizeMatched", "matched_size"):
        value = response.get(key)
        if value not in (None, ""):
            try:
                matched = float(value)
            except (TypeError, ValueError):
                matched = 0.0
            break
    return {
        "ok": lookup.get("ok") is True,
        "status": status,
        "size_matched": max(0.0, matched),
        "error": str(lookup.get("error") or ""),
        # The gateway maps a not-found lookup to "cancelled" because that is
        # how the authenticated endpoint represents an order someone has just
        # cancelled.  This lane cancels nothing before it syncs, so for it the
        # same reply means only "the exchange did not answer with the order".
        "status_inferred_from_not_found": bool(
            lookup.get("status_inferred_from_not_found")
        ),
    }


def sync_resting_intent(
    conn: sqlite3.Connection,
    *,
    intent: Mapping[str, Any],
    view: Mapping[str, Any],
    as_of_utc: str,
    write: Callable[[], Any],
) -> dict[str, Any]:
    """Write what the exchange says about one resting order to the ledger.

    Increments only: the exchange reports the cumulative fill and the
    ledger already holds what earlier events recorded, so the event carries
    the difference.  A cancelled order with a fill is closed as a partial
    the way the reconciliation ledger knows it (``resolved_partial``), with
    the intent ended, so the bucket is free for the next rung.
    """

    intent_id = str(intent["order_intent_id"])
    requested = float(intent["requested_size"])
    prior = float(intent["filled_size"])
    # A later order lookup can omit or reset size_matched after cancellation;
    # the ledger's already confirmed fill remains a lower bound.
    cumulative = max(prior, min(float(view["size_matched"]), requested))
    increment = max(0.0, cumulative - prior)
    price = float(intent["limit_price"])
    status = str(view["status"])
    if not view["ok"]:
        return {"state": "unknown", "reason": view["error"] or "lookup_not_ok"}
    if status in _FILLED or (status in _RESTING and fill_is_complete(cumulative, requested)):
        if increment <= EPS and not fill_is_complete(prior, requested):
            return {"state": "unchanged", "reason": "matched_without_size"}
        if increment <= EPS:
            return {"state": "unchanged", "reason": "already_recorded"}
        with write():
            record_order_event(conn, order_intent_id=intent_id, event={
                "event_type": "FILL",
                "exchange_order_id": intent["exchange_order_id"],
                "observed_at": as_of_utc,
                "matched_size": increment,
                "matched_notional_usdc": increment * price,
                "event_payload": {
                    "recovered_by": "resting_ladder_lifecycle",
                    "cumulative_size_matched": cumulative,
                    "exchange_status": status,
                },
            })
        return {"state": "filled", "increment": increment}
    if status in _CANCELLED and view.get("status_inferred_from_not_found"):
        # Absence read as cancellation ends a live order in the ledger, and
        # the ledger is what frees the capital and the bucket for a re-post.
        # Measured 2026-09-06 15:06:39Z: two rungs were written cancelled off
        # one such read -- LIMC 33C at 0.131 and KATL 84-85F at 0.121 -- and
        # both were still resting on the exchange when it was asked again
        # eight minutes later.  This lane issues no cancel, so it has no
        # cancel of its own to corroborate the inference with; it declines it
        # and leaves the rung resting.  A rung the exchange really did cancel
        # keeps its intent open one more pass and is settled by the
        # reconciliation lane, which reads /data/order and answers not_found
        # honestly rather than inferring.
        return {
            "state": "unknown",
            "reason": "cancel_inferred_from_not_found_declined",
        }
    # The venue reports a GTD order that reached its horizon as ``canceled``,
    # not ``expired``: LFPB 2026-09-14 29C, 13 @ 0.08, GTD 2026-09-13T08:02:39Z,
    # read ``canceled`` at 08:03:49Z on a book (0.07/0.10) the rung rule kept
    # it on, with no cancel sent.  So ``_EXPIRED`` never matched and every
    # expiry was booked a cancel.  An exchange-said cancel on an order past its
    # own expiry is the expiry; one before it stays a cancel.
    if status in _EXPIRED or (
        status in _CANCELLED and _past_own_expiry(intent, as_of_utc)
    ):
        # The order reached its own horizon and the venue removed it.  Same
        # accounting as a cancel -- nothing rests, a partial is closed out --
        # and a different name, because the ledger has to be able to say
        # whether an order ended because we asked or because its clock ran
        # out.  ``EXPIRED`` has been in the event vocabulary and the state
        # machine since the ledger was written; this is its first producer.
        with write():
            record_order_event(conn, order_intent_id=intent_id, event={
                "event_type": "EXPIRED",
                "exchange_order_id": intent["exchange_order_id"],
                "observed_at": as_of_utc,
                "matched_size": increment,
                "matched_notional_usdc": increment * price,
                "event_payload": {
                    "recovered_by": "resting_ladder_lifecycle",
                    "cumulative_size_matched": cumulative,
                    "exchange_status": status,
                },
            })
            if cumulative > EPS and not fill_is_complete(cumulative, requested):
                resolve_cancelled_after_partial_fill(
                    conn,
                    order_intent_id=intent_id,
                    filled_size=cumulative,
                    filled_notional_usdc=(
                        float(intent["filled_notional_usdc"])
                        + increment * price
                    ),
                    resolved_at_utc=as_of_utc,
                )
        return {
            "state": "expired", "increment": increment, "cumulative": cumulative,
        }
    if status in _CANCELLED:
        with write():
            record_order_event(conn, order_intent_id=intent_id, event={
                "event_type": "CANCEL_ACK",
                "exchange_order_id": intent["exchange_order_id"],
                "observed_at": as_of_utc,
                "matched_size": increment,
                "matched_notional_usdc": increment * price,
                "event_payload": {
                    "recovered_by": "resting_ladder_lifecycle",
                    "cumulative_size_matched": cumulative,
                    "exchange_status": status,
                },
            })
            if cumulative > EPS and not fill_is_complete(cumulative, requested):
                resolve_cancelled_after_partial_fill(
                    conn,
                    order_intent_id=intent_id,
                    filled_size=cumulative,
                    filled_notional_usdc=float(intent["filled_notional_usdc"]) + increment * price,
                    resolved_at_utc=as_of_utc,
                )
        return {"state": "cancelled", "increment": increment, "cumulative": cumulative}
    if status in _RESTING:
        if increment > EPS:
            with write():
                record_order_event(conn, order_intent_id=intent_id, event={
                    "event_type": "PARTIAL_FILL",
                    "exchange_order_id": intent["exchange_order_id"],
                    "observed_at": as_of_utc,
                    "matched_size": increment,
                    "matched_notional_usdc": increment * price,
                    "event_payload": {
                        "recovered_by": "resting_ladder_lifecycle",
                        "cumulative_size_matched": cumulative,
                        "exchange_status": status,
                    },
                })
            return {"state": "resting_partial", "increment": increment}
        return {"state": "resting"}
    return {"state": "unknown", "reason": f"exchange_status:{status}"}



def recorded_fills_for_token(conn: sqlite3.Connection, token_id: str) -> float:
    """The position this ledger has already booked on one token: shares
    bought less shares sold.  Summed over both sides (as this did until
    2026-09-18) a sold share read as a bought one, so a token with an exit
    sell booked on it held less than the ledger claimed, and every departed
    buy asked about after that was answered ``unfilled``."""

    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN UPPER(COALESCE(i.order_action,'BUY'))='SELL'"
        "   THEN -r.filled_size ELSE r.filled_size END), 0)"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id = i.order_intent_id"
        " WHERE i.token_id = ?",
        (str(token_id),),
    ).fetchone()
    return float((row[0] if row else 0.0) or 0.0)


def reconcile_departed_intent(
    conn: sqlite3.Connection,
    *,
    intent: Mapping[str, Any],
    client: Any,
    as_of_utc: str,
    write: Callable[[], Any],
    attributed: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Close an intent the venue will not answer a point query about.

    Charter 110.  ``/lookup-exchange-order`` returns an empty body for an
    order the venue no longer holds, and ``sync_resting_intent`` reads that
    as ``unknown`` and keeps -- so the intent rests forever and holds its
    bucket shut through ``can_submit_next_order``.  Measured 2026-09-07:
    twelve of fourteen live rungs were in that state, the oldest since
    2026-09-06T15:53, and eleven of them had FILLED IN FULL while the ledger
    recorded zero -- 87.40 shares, about 18.30 USDC, owned and unknown.

    Nothing here is inferred from the silence.  Two facts decide it:

    * the exchange's own COMPLETE enumeration of what is live on this token
      (``/lookup-open-orders``); absence from a finished list is a fact,
      absence from a truncated one is nothing, which is why an incomplete
      enumeration refuses rather than concludes; and
    * the account's actual position in the token (``/lookup-position``),
      because a point lookup's answer expires -- OEJN 35C read MATCHED
      25.00/25 at 12:10Z and empty at 12:22Z -- and a position does not.

    The shares this order may claim are what the position holds beyond what
    the ledger has already booked on that token, capped at what this order
    still owes.  ``attributed`` carries what earlier orders took in the SAME
    pass, so two departed orders on one token cannot both claim the same
    shares.  A departed order with nothing left to claim is recorded EXPIRED,
    which says the order existed and stopped existing and does not claim to
    know why.
    """

    seen = attributed if attributed is not None else {}
    token = str(intent["token_id"])
    order_id = str(intent["exchange_order_id"])
    out: dict[str, Any] = {"rule": DEPARTED_RECONCILE_RULE_VERSION, "token_id": token}

    listing = client.lookup_open_orders({"token_id": token})
    out["enumeration"] = {
        "ok": bool(listing.get("ok")),
        "complete": listing.get("complete"),
        "total_open_orders": listing.get("total_open_orders"),
        "error": str(listing.get("error") or ""),
    }
    if not listing.get("ok") or listing.get("complete") is not True:
        return {**out, "state": "unknown", "reason": "enumeration_incomplete"}
    live_ids = {
        str(row.get("id") or row.get("orderID") or "").strip().lower()
        for row in (listing.get("open_orders") or [])
        if isinstance(row, Mapping)
    }
    if order_id.strip().lower() in live_ids:
        return {**out, "state": "live", "reason": "present_in_open_orders"}

    position = client.lookup_position({"token_id": token})
    out["position"] = {
        "ok": bool(position.get("ok")),
        "shares": position.get("shares"),
        "balance_raw": position.get("balance_raw"),
        "error": str(position.get("error") or ""),
    }
    if not position.get("ok"):
        return {**out, "state": "unknown", "reason": "position_unavailable"}

    held = float(position.get("shares") or 0.0)
    recorded = recorded_fills_for_token(conn, token)
    side = str(intent.get("order_action") or "BUY").strip().upper()
    seen_key = token if side == "BUY" else f"{token}:SELL"
    claimed_this_pass = float(seen.get(seen_key, 0.0))
    # A departed buy may claim what the account holds beyond the ledger's net
    # position; a departed sell may claim what the ledger's net position holds
    # beyond the account -- the shares that left.  Same evidence, read from
    # the side the order was on.
    if side == "SELL":
        unattributed = max(0.0, recorded - held - claimed_this_pass)
    else:
        unattributed = max(0.0, held - recorded - claimed_this_pass)
    requested = float(intent["requested_size"])
    prior = float(intent["filled_size"])
    owed = max(0.0, requested - prior)
    take = min(unattributed, owed)
    price = float(intent["limit_price"])
    out.update({
        "order_action": side,
        "held_shares": held,
        "recorded_shares": recorded,
        "claimed_earlier_this_pass": claimed_this_pass,
        "unattributed_shares": unattributed,
        "owed_shares": owed,
        "attributed_shares": take,
    })
    evidence = {
        "recovered_by": "resting_ladder_lifecycle.reconcile_departed_intent",
        "rule": DEPARTED_RECONCILE_RULE_VERSION,
        "enumeration": out["enumeration"],
        "position": out["position"],
        "recorded_shares_on_token": recorded,
        "claimed_earlier_this_pass": claimed_this_pass,
    }
    if take <= EPS:
        with write():
            record_order_event(conn, order_intent_id=str(intent["order_intent_id"]), event={
                "event_type": "EXPIRED",
                "exchange_order_id": order_id,
                "observed_at": as_of_utc,
                "matched_size": 0.0,
                "matched_notional_usdc": 0.0,
                "event_payload": evidence,
            })
            if EPS < prior < requested - EPS:
                # The prior fill is already booked.  EXPIRED confirms the
                # remaining order is absent, so close that known partial;
                # otherwise EXPIRED leaves it unknown and the next sweep
                # writes the same event forever.
                resolve_cancelled_after_partial_fill(
                    conn,
                    order_intent_id=str(intent["order_intent_id"]),
                    filled_size=prior,
                    filled_notional_usdc=float(intent["filled_notional_usdc"]),
                    resolved_at_utc=as_of_utc,
                )
        return {**out, "state": "departed_partial" if prior > EPS else "departed_unfilled",
                "reason": "no_unattributed_shares"}

    cumulative = prior + take
    with write():
        record_order_event(conn, order_intent_id=str(intent["order_intent_id"]), event={
            "event_type": "FILL",
            "exchange_order_id": order_id,
            "observed_at": as_of_utc,
            "matched_size": take,
            "matched_notional_usdc": take * price,
            "event_payload": evidence,
        })
        if not fill_is_complete(cumulative, requested):
            # Booked short of what it asked for and gone from the venue, so
            # it is the partial the reconciliation ledger already knows how
            # to close -- otherwise it stays ``pending`` and goes on holding
            # its bucket, which is the state this whole function exists to
            # end.
            resolve_cancelled_after_partial_fill(
                conn,
                order_intent_id=str(intent["order_intent_id"]),
                filled_size=cumulative,
                filled_notional_usdc=float(intent["filled_notional_usdc"]) + take * price,
                resolved_at_utc=as_of_utc,
            )
    seen[seen_key] = claimed_this_pass + take
    return {
        **out,
        "state": "departed_filled" if fill_is_complete(cumulative, requested)
        else "departed_partial",
        "reason": "position_beyond_recorded_fills",
        "increment": take,
    }


def reprice_resting_rungs(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    client: Any,
    write: Callable[[], Any] | None = None,
    book_loader: Callable[..., Mapping[str, Mapping[str, float]]] | None = None,
    dwell_seconds: float | None = None,
    far_ticks: float | None = None,
    limit: int = 200,
    dead_bucket_loader: DeadBucketLoader | None = None,
    stale_book_seconds: float | None = None,
    start_index: int = 0,
    lookup_concurrency: int = 1,
    gtc_sell_lookup_interval_sec: float | None = None,
) -> dict[str, Any]:
    """One pass over every resting rung: sync it, then keep or cancel it.

    ``start_index`` is where in the created_at order this pass begins; the
    pass wraps round so every rung is still visited.  It exists for the
    gateway breaker below: a pass that stopped early says where the next one
    should start (``next_start_index``), which is the rung after the one
    whose lookup failed, so a single order the exchange cannot answer for
    never keeps the others from being synced.

    ``dead_bucket_loader(conn, station_id=, event_date=, as_of_utc=)`` returns
    the scope's observed-dead buckets and the running high that killed them
    (charter 135 (二)).  Absent, no rung is cancelled for that reason -- the
    caller that owns the observation connection supplies it.

    ``lookup_concurrency`` (charter 180): above 1, once the pass's first
    lookup has come back without tripping the breaker, the remaining rungs'
    lookups are asked at once, that many in flight, and each answer is taken
    up when the pass reaches its rung.  The pass itself -- sync, verdict,
    cancel, breaker -- still runs one rung at a time in the same order, so
    it writes the receipt the one-at-a-time pass writes.  The lookup is the
    only work handed to another thread: it touches the client, never the
    connection.  At 1 each rung is asked when the pass reaches it.

    ``gtc_sell_lookup_interval_sec`` (charter 250): a GTC sell that is not a
    rung and was answered for less than this long ago is not asked again
    this pass -- it is listed kept, with how long ago, and the pass runs over
    the rest.  None takes ``GTC_SELL_LOOKUP_INTERVAL_SEC``; 0 asks every one.
    """

    write = write or (lambda: contextlib.nullcontext())
    books = book_loader or latest_book_by_bucket
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "rule": RESTING_REPRICE_RULE_VERSION,
        "ladder_shape": MAKER_LADDER_SHAPE,
        "state": "no_resting_rungs",
        "examined": 0,
        "synced_filled": 0,
        "synced_partial": 0,
        "synced_cancelled": 0,
        # Rungs the venue removed at their own GTD horizon.  Counted apart
        # from cancels so the horizon can be read against the fills: a lane
        # whose rungs mostly expire is one whose horizon is shorter than the
        # market it is waiting for.
        "synced_expired": 0,
        "kept": 0,
        # Orders admitted for syncing that are not rungs of the ladder (an
        # exit sell, any order that is not post-only): kept as they stand
        # and ended by the venue at their own horizon.  Also in ``kept``.
        "kept_not_ladder": 0,
        "departed_filled": 0,
        "departed_partial": 0,
        "departed_unfilled": 0,
        "departed_shares": 0.0,
        "cancel_requested": 0,
        "cancel_confirmed": 0,
        # Charter 135 (二): cancels asked for because the observed high had
        # already killed the rung's bucket.  Also counted in cancel_requested.
        "cancel_requested_dead_bucket": 0,
        "dead_bucket_rule": DEAD_BUCKET_CANCEL_RULE_VERSION,
        # Charter 135 (三): cancels asked for because the rung's book was
        # missing or older than the bound.  Also counted in cancel_requested.
        "cancel_requested_stale_book": 0,
        "stale_book_rule": STALE_BOOK_CANCEL_RULE_VERSION,
        "stale_book_seconds": (
            RESTING_BOOK_STALE_SECONDS if stale_book_seconds is None
            else float(stale_book_seconds)
        ),
        "failures": [],
        "rungs": [],
        "places_orders": False,
        "calls_order_gateway": True,
        "orders_submitted": 0,
    }
    intents = load_resting_maker_intents(conn, limit=limit)
    receipt["examined"] = len(intents)
    # Charter 250: GTC sells answered for inside the interval rest this pass.
    interval = float(
        GTC_SELL_LOOKUP_INTERVAL_SEC
        if gtc_sell_lookup_interval_sec is None
        else gtc_sell_lookup_interval_sec
    )
    receipt["gtc_sell_lookup_interval_sec"] = interval
    receipt["lookup_rested"] = 0
    resting_ids = {str(intent["order_intent_id"]) for intent in intents}
    for gone in [key for key in _GTC_SELL_LAST_LOOKUP if key not in resting_ids]:
        _GTC_SELL_LAST_LOOKUP.pop(gone, None)
    if interval > 0:
        clock = time.monotonic()
        asked: list[dict[str, Any]] = []
        for intent in intents:
            last = _GTC_SELL_LAST_LOOKUP.get(str(intent["order_intent_id"]))
            if (
                last is not None
                and clock - last < interval
                and on_the_slow_clock(intent)
            ):
                receipt["kept"] += 1
                receipt["kept_not_ladder"] += 1
                receipt["lookup_rested"] += 1
                receipt["rungs"].append({
                    "order_intent_id": intent["order_intent_id"],
                    "station_id": intent["station_id"],
                    "event_date": intent["event_date"],
                    "bucket": intent["bucket"],
                    "limit_price": intent["limit_price"],
                    "verdict": {
                        "action": "keep",
                        "reason": "not_a_ladder_rung",
                        "looked_up_seconds_ago": round(clock - last, 1),
                    },
                })
            else:
                asked.append(intent)
        intents = asked
    receipt["looked_up"] = len(intents)
    # Rungs this pass did not reach because the gateway stopped answering,
    # named so the board can see them, and where the next pass starts.
    receipt["deferred"] = []
    receipt["deferred_reason"] = ""
    receipt["start_index"] = 0
    receipt["next_start_index"] = 0
    if not intents:
        if receipt["examined"]:
            receipt["state"] = "no_change"
        return receipt
    start = int(start_index or 0) % len(intents)
    receipt["start_index"] = start
    ordered = intents[start:] + intents[:start]
    book_cache: dict[tuple[str, str], Mapping[str, Mapping[str, float]]] = {}
    dead_cache: dict[tuple[str, str], tuple[tuple[str, ...], float | None]] = {}
    concurrency = max(1, int(lookup_concurrency or 1))
    receipt["lookup_concurrency"] = concurrency
    prefetched: dict[int, Future] = {}
    pool: ThreadPoolExecutor | None = None

    def ask(intent: Mapping[str, Any]) -> Any:
        return client.lookup_exchange_order({
            "order_id": intent["exchange_order_id"],
        })
    # Shares this pass has already handed to a departed order, per token, so
    # two orders on one token cannot both claim the same position.
    attributed: dict[str, float] = {}

    def defer_rest(position: int, entry: dict[str, Any], reason: str) -> None:
        # Charter 174: the gateway breaker.  One transport failure means the
        # gateway (or the exchange behind it) is not answering, and every
        # remaining lookup would spend its full timeout learning the same
        # thing: measured 2026-09-21 00:14Z and 00:27Z, eleven resting
        # sells at roughly 35 seconds each made this pass 388 seconds of a
        # 650-second cycle, the largest single item, in front of every
        # decision.  So the pass stops here, the rungs it did not reach are
        # named, and the next pass starts one past this rung -- a rung the
        # exchange cannot answer for is visited last, not first, so it
        # cannot starve the others.  Nothing is written for a deferred
        # rung; it rests exactly as it did.
        entry["verdict"] = {"action": "deferred", "reason": reason[:160]}
        receipt["rungs"].append(entry)
        receipt["deferred"] = [
            str(item["order_intent_id"]) for item in ordered[position + 1:]
        ]
        receipt["deferred_reason"] = reason[:160]
        receipt["next_start_index"] = (start + position + 1) % len(intents)

    try:
        for position, intent in enumerate(ordered):
            if position == 1 and concurrency > 1 and pool is None:
                # The first lookup came back without tripping the breaker
                # (a trip breaks out of the loop before this line): the
                # gateway is answering, so the rest are asked now.
                pool = ThreadPoolExecutor(
                    max_workers=concurrency,
                    thread_name_prefix="resting_lookup",
                )
                for later, later_intent in enumerate(ordered[1:], start=1):
                    prefetched[later] = pool.submit(ask, later_intent)
            intent_id = intent["order_intent_id"]
            entry: dict[str, Any] = {
                "order_intent_id": intent_id,
                "station_id": intent["station_id"],
                "event_date": intent["event_date"],
                "bucket": intent["bucket"],
                "limit_price": intent["limit_price"],
            }
            try:
                answer = (
                    prefetched.pop(position).result()
                    if position in prefetched
                    else ask(intent)
                )
                view = _exchange_view(answer)
                synced = sync_resting_intent(
                    conn, intent=intent, view=view, as_of_utc=as_of_utc, write=write,
                )
            except Exception as exc:  # noqa: BLE001 -- one rung's problem
                receipt["failures"].append({
                    "order_intent_id": intent_id,
                    "reason": f"{type(exc).__name__}:{exc}"[:160],
                })
                continue
            entry["exchange_status"] = view["status"]
            entry["sync"] = synced
            if synced["state"] == "unknown" and _gateway_unreachable(
                synced.get("reason")
            ):
                defer_rest(position, entry, str(synced.get("reason") or ""))
                break
            if on_the_slow_clock(intent):
                # Answered for: charter 250's interval runs from here.
                _GTC_SELL_LAST_LOOKUP[str(intent_id)] = time.monotonic()
            if synced["state"] == "filled":
                receipt["synced_filled"] += 1
                receipt["rungs"].append(entry)
                continue
            if synced["state"] in {"cancelled", "expired"}:
                # An expiry ends the rung exactly as a cancel does, and is
                # counted apart from it: the two answer different questions
                # about the lane, one about our own decisions and one about
                # whether the horizon is set where the fills are.
                receipt[
                    "synced_expired" if synced["state"] == "expired"
                    else "synced_cancelled"
                ] += 1
                receipt["rungs"].append(entry)
                continue
            if synced["state"] == "resting_partial":
                receipt["synced_partial"] += 1
            if synced["state"] == "unknown":
                # The venue will not answer a point query about this order.
                # Charter 110: do not read that as "still resting" -- ask what IS
                # live, and ask what the account actually holds.
                try:
                    departed = reconcile_departed_intent(
                        conn, intent=intent, client=client,
                        as_of_utc=as_of_utc, write=write, attributed=attributed,
                    )
                except Exception as exc:  # noqa: BLE001 -- one rung's problem
                    receipt["failures"].append({
                        "order_intent_id": intent_id,
                        "reason": f"departed_reconcile:{type(exc).__name__}:{exc}"[:160],
                    })
                    if _gateway_unreachable(str(exc)):
                        defer_rest(position, entry, f"departed_reconcile:{exc}")
                        break
                    departed = {"state": "unknown", "reason": "reconcile_raised"}
                entry["departed"] = departed
                if departed["state"] == "departed_filled":
                    receipt["departed_filled"] += 1
                    receipt["departed_shares"] += float(departed.get("increment") or 0.0)
                elif departed["state"] == "departed_partial":
                    receipt["departed_partial"] += 1
                    receipt["departed_shares"] += float(departed.get("increment") or 0.0)
                elif departed["state"] == "departed_unfilled":
                    receipt["departed_unfilled"] += 1
                if departed["state"].startswith("departed"):
                    entry["verdict"] = {
                        "action": "resolved", "reason": departed["reason"],
                    }
                    receipt["rungs"].append(entry)
                    continue
            if synced["state"] not in {"resting", "resting_partial"}:
                entry["verdict"] = {"action": "keep", "reason": synced.get("reason", synced["state"])}
                receipt["kept"] += 1
                receipt["rungs"].append(entry)
                continue
            if not intent.get("ladder_rung", True):
                # Synced above; not a rung, so none of the ladder's rules apply.
                # Its horizon is the venue's to enforce, and the next pass reads
                # what the venue did.
                entry["verdict"] = {"action": "keep", "reason": "not_a_ladder_rung"}
                receipt["kept"] += 1
                receipt["kept_not_ladder"] += 1
                receipt["rungs"].append(entry)
                continue
            scope = (intent["station_id"], intent["event_date"])
            # Charter 135 (二): the day's thermometer first, the book second.  A
            # dead bucket needs no book to be cancelled -- a missing or stale
            # book is exactly when a rung on it should not be left standing.
            if dead_bucket_loader is not None and scope not in dead_cache:
                try:
                    dead, running_high = dead_bucket_loader(
                        conn, station_id=scope[0], event_date=scope[1],
                        as_of_utc=as_of_utc,
                    )
                    dead_cache[scope] = (
                        tuple(str(bucket) for bucket in dead),
                        None if running_high is None else float(running_high),
                    )
                except Exception as exc:  # noqa: BLE001 -- the book rule still runs
                    receipt["failures"].append({
                        "order_intent_id": intent_id,
                        "reason": f"dead_bucket_read:{type(exc).__name__}:{exc}"[:160],
                    })
                    dead_cache[scope] = ((), None)
            dead_buckets, running_high = dead_cache.get(scope, ((), None))

            def _book(scope: tuple[str, str] = scope) -> Mapping[str, Any] | None:
                if scope not in book_cache:
                    book_cache[scope] = books(conn, station_id=scope[0], event_date=scope[1])
                row = book_cache[scope].get(intent["bucket"])
                if row and str(intent.get("side") or "YES") == "NO":
                    return mirror_book(row)
                return row

            verdict = resting_rung_keep_or_cancel(
                intent=intent,
                dead_buckets=dead_buckets,
                running_high=running_high,
                load_book=_book,
                as_of_utc=as_of_utc,
                stale_book_seconds=float(receipt["stale_book_seconds"]),
                dwell_seconds=dwell_seconds,
                far_ticks=far_ticks,
            )
            cancel_rule = str(verdict.pop("cancel_rule", "") or "")
            if verdict["action"] == "cancel" and cancel_rule == DEAD_BUCKET_CANCEL_RULE_VERSION:
                receipt["cancel_requested_dead_bucket"] += 1
            elif verdict["action"] == "cancel" and cancel_rule == STALE_BOOK_CANCEL_RULE_VERSION:
                receipt["cancel_requested_stale_book"] += 1
            elif verdict.get("reason") == "no_book" and not cancel_rule:
                # Only reachable for a book that carries no clock (an injected
                # reader); a missing book from the store is cancelled above.
                entry["verdict"] = verdict
                receipt["kept"] += 1
                receipt["rungs"].append(entry)
                continue
            entry["verdict"] = verdict
            if verdict["action"] != "cancel":
                receipt["kept"] += 1
                receipt["rungs"].append(entry)
                continue
            receipt["cancel_requested"] += 1
            try:
                cancel = client.cancel_order({
                    "order_id": intent["exchange_order_id"],
                    "idempotency_key": f"{intent['idempotency_key']}:cancel:{cancel_rule}",
                })
                entry["cancel"] = {
                    "ok": cancel.get("ok") is True,
                    "error": str(cancel.get("error") or ""),
                }
                if cancel.get("ok") is not True:
                    receipt["failures"].append({
                        "order_intent_id": intent_id,
                        "reason": "cancel_not_ok:" + str(cancel.get("error") or "")[:120],
                    })
                    receipt["rungs"].append(entry)
                    continue
                confirm = _exchange_view(client.lookup_exchange_order({
                    "order_id": intent["exchange_order_id"],
                }))
                entry["confirm_status"] = confirm["status"]
                after = sync_resting_intent(
                    conn, intent=intent, view=confirm, as_of_utc=as_of_utc, write=write,
                )
                entry["after_cancel"] = after
                if after["state"] == "cancelled":
                    receipt["cancel_confirmed"] += 1
                elif after["state"] == "expired":
                    # The cancel and the expiry raced and the clock won.  The
                    # rung is gone either way; recording which one ended it is
                    # what keeps the horizon measurable.
                    receipt["synced_expired"] += 1
                elif after["state"] == "filled":
                    receipt["synced_filled"] += 1
            except Exception as exc:  # noqa: BLE001 -- next pass looks again
                receipt["failures"].append({
                    "order_intent_id": intent_id,
                    "reason": f"{type(exc).__name__}:{exc}"[:160],
                })
            receipt["rungs"].append(entry)
    finally:
        if pool is not None:
            # Answers nobody will read (the breaker stopped the pass)
            # are abandoned: queued lookups are dropped, running ones
            # finish on their own threads and touch only the client.
            pool.shutdown(wait=False, cancel_futures=True)
    receipt["deferred_count"] = len(receipt["deferred"])
    receipt["state"] = (
        "committed"
        if (receipt["cancel_confirmed"] or receipt["synced_filled"]
            or receipt["synced_cancelled"] or receipt["synced_partial"]
            or receipt["departed_filled"] or receipt["departed_partial"]
            or receipt["departed_unfilled"])
        else "no_change"
    )
    return receipt


__all__ = [
    "DEAD_BUCKET_CANCEL_RULE_VERSION",
    "mirror_book",
    "RESTING_BOOK_STALE_SECONDS",
    "RESTING_LOOKUP_CONCURRENCY",
    "GTC_SELL_LOOKUP_INTERVAL_SEC",
    "on_the_slow_clock",
    "RESTING_REPRICE_DWELL_SECONDS",
    "STALE_BOOK_CANCEL_RULE_VERSION",
    "book_age_seconds",
    "RESTING_REPRICE_FAR_TICKS",
    "RESTING_REPRICE_RULE_VERSION",
    "SCHEMA",
    "GATEWAY_TRANSPORT_MARK",
    "latest_book_by_bucket",
    "load_resting_maker_intents",
    "reprice_resting_rungs",
    "resting_rung_verdict",
    "sync_resting_intent",
]
