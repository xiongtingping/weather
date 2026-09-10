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
import os
import sqlite3
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


def latest_book_by_bucket(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> dict[str, dict[str, float]]:
    """The newest committed official book of the scope, one row per bucket."""

    row = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " ORDER BY observed_at DESC, sequence DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        return {}
    out: dict[str, dict[str, float]] = {}
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
        }
    return out


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
        " r.exchange_order_id, r.filled_size, r.filled_notional_usdc, r.state"
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
        if intent.get("post_only") is not True:
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
            "order_action": str(row[10] or "BUY"),
            "intent_state": str(row[11]),
            "exchange_order_id": str(row[12]),
            "filled_size": float(row[13] or 0.0),
            "filled_notional_usdc": float(row[14] or 0.0),
            "reconciliation_state": str(row[15]),
        })
    return out


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
    cumulative = min(float(view["size_matched"]), requested)
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
    if status in _EXPIRED:
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
    """Every share this ledger has already booked on one token."""

    row = conn.execute(
        "SELECT COALESCE(SUM(r.filled_size), 0) FROM basket_target_order_intents i"
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
    claimed_this_pass = float(seen.get(token, 0.0))
    unattributed = max(0.0, held - recorded - claimed_this_pass)
    requested = float(intent["requested_size"])
    prior = float(intent["filled_size"])
    owed = max(0.0, requested - prior)
    take = min(unattributed, owed)
    price = float(intent["limit_price"])
    out.update({
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
        return {**out, "state": "departed_unfilled", "reason": "no_unattributed_shares"}

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
    seen[token] = claimed_this_pass + take
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
) -> dict[str, Any]:
    """One pass over every resting rung: sync it, then keep or cancel it."""

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
        "departed_filled": 0,
        "departed_partial": 0,
        "departed_unfilled": 0,
        "departed_shares": 0.0,
        "cancel_requested": 0,
        "cancel_confirmed": 0,
        "failures": [],
        "rungs": [],
        "places_orders": False,
        "calls_order_gateway": True,
        "orders_submitted": 0,
    }
    intents = load_resting_maker_intents(conn, limit=limit)
    receipt["examined"] = len(intents)
    if not intents:
        return receipt
    book_cache: dict[tuple[str, str], Mapping[str, Mapping[str, float]]] = {}
    # Shares this pass has already handed to a departed order, per token, so
    # two orders on one token cannot both claim the same position.
    attributed: dict[str, float] = {}
    for intent in intents:
        intent_id = intent["order_intent_id"]
        entry: dict[str, Any] = {
            "order_intent_id": intent_id,
            "station_id": intent["station_id"],
            "event_date": intent["event_date"],
            "bucket": intent["bucket"],
            "limit_price": intent["limit_price"],
        }
        try:
            view = _exchange_view(client.lookup_exchange_order({
                "order_id": intent["exchange_order_id"],
            }))
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
        scope = (intent["station_id"], intent["event_date"])
        if scope not in book_cache:
            book_cache[scope] = books(conn, station_id=scope[0], event_date=scope[1])
        book = book_cache[scope].get(intent["bucket"])
        if not book:
            entry["verdict"] = {"action": "keep", "reason": "no_book"}
            receipt["kept"] += 1
            receipt["rungs"].append(entry)
            continue
        verdict = resting_rung_verdict(
            limit_price=intent["limit_price"],
            bid=book["bid"], ask=book["ask"], tick=book["tick"],
            posted_at_utc=intent["posted_at_utc"], as_of_utc=as_of_utc,
            dwell_seconds=dwell_seconds, far_ticks=far_ticks,
        )
        entry["verdict"] = verdict
        if verdict["action"] != "cancel":
            receipt["kept"] += 1
            receipt["rungs"].append(entry)
            continue
        receipt["cancel_requested"] += 1
        try:
            cancel = client.cancel_order({
                "order_id": intent["exchange_order_id"],
                "idempotency_key": f"{intent['idempotency_key']}:cancel:{RESTING_REPRICE_RULE_VERSION}",
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
    "RESTING_REPRICE_DWELL_SECONDS",
    "RESTING_REPRICE_FAR_TICKS",
    "RESTING_REPRICE_RULE_VERSION",
    "SCHEMA",
    "latest_book_by_bucket",
    "load_resting_maker_intents",
    "reprice_resting_rungs",
    "resting_rung_verdict",
    "sync_resting_intent",
]
