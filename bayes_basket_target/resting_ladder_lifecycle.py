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

from .delta_v2_runtime import MAKER_LADDER_SHAPE, bid_anchored_ladder
from .execution_reconciliation import (
    fill_is_complete,
    record_order_event,
    resolve_cancelled_after_partial_fill,
)

SCHEMA = "bayes_basket_target_resting_reprice_v1"
RESTING_REPRICE_RULE_VERSION = "bid_anchored_membership_v1"
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
    """Keep or cancel one resting rung against the ladder that stands.

    Pure.  ``keep`` when the rung is on the current bid-anchored ladder,
    when the book has no bid to anchor on, or when it fell out of the
    ladder less than the dwell ago and is still near the bid; ``cancel``
    when it is off the ladder past the dwell, or ``far_ticks`` or more from
    the bid whatever its age.
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
    if not ladder:
        return {**verdict, "action": "keep", "reason": "no_anchor"}
    if any(abs(price - rung) <= float(tick) / 2.0 + EPS for rung in ladder):
        return {**verdict, "action": "keep", "reason": "on_ladder"}
    if distance + EPS >= far:
        return {**verdict, "action": "cancel", "reason": "bid_ran_far"}
    if age + EPS >= dwell:
        return {**verdict, "action": "cancel", "reason": "off_ladder_past_dwell"}
    return {**verdict, "action": "keep", "reason": "off_ladder_within_dwell"}


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
    filled in part and still rests.  Both carry the exchange order id the
    POST_ACK wrote, which is the handle the lookup and the cancel need.
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
        "   AND r.state='pending'"
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
        "kept": 0,
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
        if synced["state"] == "cancelled":
            receipt["synced_cancelled"] += 1
            receipt["rungs"].append(entry)
            continue
        if synced["state"] == "resting_partial":
            receipt["synced_partial"] += 1
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
            or receipt["synced_cancelled"] or receipt["synced_partial"])
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
