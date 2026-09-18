"""The one way a side lane sends a BUY: decision, cycle, intent, gate, gateway, ledger.

``exit_orders.send_exit_sell`` is the sell-side twin.  A lane that buys has
the same twelve steps and one more: the decision row.  The live book
(``scripts/live_book.py``) counts money under ``auto_trade_decisions`` and
reads the claim off the envelope the decision's forward evidence binding
names, so a buy with no decision row is a leg the board cannot see and a
settlement nothing scores.  The recycle and exit lanes sell what a basket
decision bought and ride on its row; a lane that opens a position of its own
writes its own.

The payload goes through ``build_place_order_payload`` rather than being
assembled here, so a lane's buy is bound to its execution family the way a
basket's is -- a FOK states its price cap, a resting rung its expiry -- and
a contradiction is refused by name instead of sent.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from .contracts import content_sha256
from .execution_reconciliation import (
    VENUE_SHARE_QUANTUM,
    can_submit_next_order,
    record_order_event,
)
from .exit_orders import Transport, wall_clock_utc
from .order_submission import (
    BASKET_STRATEGY_ID,
    PLACE_ORDER_PATH,
    OrderSubmissionError,
    build_place_order_payload,
    classify_submission_response,
    fill_quantities,
    live_intent_armed,
)


def lane_decision_id(lane: str, station_id: str, event_date: str) -> str:
    """One decision per lane per station-day, the exit lanes' own spelling."""

    return f"{lane}_decision_{station_id}_{event_date}"


def _insert_decision_row(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    cycle_id: str,
    plan_id: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    details: Mapping[str, Any],
    created_at: str,
) -> None:
    body = dict(details)
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,decided_state,will_order,"
        "target_execution_hard_deadline_at,reason_codes_json,details_json,"
        "decision_sha256,decision_scope_key,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            # ``prepared``, as the basket writes its own: the decision row is
            # the plan and is append-only; the order's life is on the intent
            # and the cycle.  Written ``submitted`` it would hold the
            # station-day's market ingress shut for good
            # (``target_auto_trade_decisions_not_quiescent``, 2026-09-18).
            # ``will_order`` 0 too: the no-live order-state ingress refuses a
            # scope with a live-authorised decision on file
            # (``target_auto_trade_decisions_live_order_authorized``); the
            # basket never writes 1 either -- live authority is on the intent.
            decision_id, cycle_id, plan_id, station_id, event_date,
            settlement_timezone, "basket", "prepared", 0, None, "[]",
            json.dumps(body, ensure_ascii=False, sort_keys=True),
            content_sha256(body), None, created_at,
        ),
    )


def send_lane_buy(
    conn: sqlite3.Connection,
    *,
    lane: str,
    intent_id: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    bucket: str,
    token_id: str,
    price: float,
    shares: float,
    execution_candidate: str,
    intent_body: Mapping[str, Any],
    transport: Transport,
    live_submit_authorized: bool,
    decision_details: Mapping[str, Any] | None = None,
    fok_price_cap: float | None = None,
    expiration: int | None = None,
    tick_size: float | None = None,
) -> dict[str, Any]:
    """Mint one BUY intent under its own cycle and decision, and send it.

    ``intent_body`` is the lane's own record (its plan, its rule); the
    execution fields are added here and validated by the payload builder.
    ``decision_details`` becomes the decision row's ``details_json`` -- the
    live book reads its ``decision_signature.stage`` and
    ``forward_evidence_binding`` -- and is written once per station-day: a
    second send on the same station-day rides the first decision.  Returns
    the receipt entry; a refused or unauthorized send is recorded as its
    event, never raised.
    """

    sent_at = wall_clock_utc()
    decision_id = lane_decision_id(lane, station_id, event_date)
    cycle_id = f"{lane}_cycle_{content_sha256({'intent': intent_id})[:32]}"
    body = {
        **dict(intent_body),
        "lane": lane,
        "execution_candidate": str(execution_candidate),
        "live_submit_authorized": bool(live_submit_authorized),
        **({"fok_price_cap": float(fok_price_cap)} if fok_price_cap is not None else {}),
        **({"expiration": int(expiration)} if expiration is not None else {}),
        **({"tick_size": float(tick_size)} if tick_size is not None else {}),
    }
    body_sha = content_sha256(body)
    plan_id = f"{lane}_plan_{body_sha[:32]}"
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,plan_sha256,requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, plan_id, station_id, event_date, settlement_timezone,
         "basket", body_sha, 0.0, "submitting", sent_at, sent_at),
    )
    if decision_details is not None and conn.execute(
        "SELECT 1 FROM auto_trade_decisions WHERE decision_id=?", (decision_id,)
    ).fetchone() is None:
        _insert_decision_row(
            conn, decision_id=decision_id, cycle_id=cycle_id, plan_id=plan_id,
            station_id=station_id, event_date=event_date,
            settlement_timezone=settlement_timezone, details=decision_details,
            created_at=sent_at,
        )
    notional = round(float(shares) * float(price), 6)
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,cycle_id,bucket,"
        "token_id,side,order_action,limit_price,requested_size,requested_notional_usdc,state,"
        "live_submit_authorized,idempotency_key,intent_json,intent_sha256,created_at,"
        "target_execution_hard_deadline_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (intent_id, decision_id, cycle_id, bucket, token_id,
         "YES", "BUY", float(price), float(shares), notional,
         "prepared", 1 if live_submit_authorized else 0, intent_id,
         json.dumps(body, ensure_ascii=False, sort_keys=True), body_sha, sent_at, None),
    )
    entry: dict[str, Any] = {
        "order_intent_id": intent_id, "decision_id": decision_id, "cycle_id": cycle_id,
        "station_id": station_id, "event_date": event_date, "bucket": bucket,
        "shares": float(shares), "limit_price": float(price),
        "requested_notional_usdc": notional,
    }
    intent_row = {
        **body,
        "order_intent_id": intent_id,
        "idempotency_key": intent_id,
        "token_id": token_id,
        "side": "YES",
        "order_action": "BUY",
        "limit_price": float(price),
        "requested_size": float(shares),
    }
    try:
        payload = build_place_order_payload(intent_row, strategy_id=BASKET_STRATEGY_ID)
    except OrderSubmissionError as exc:
        receipt = record_order_event(conn, order_intent_id=intent_id, event={
            "event_type": "NEVER_REACHED_EXCHANGE", "exchange_order_id": "", "observed_at": sent_at,
            "matched_size": 0.0, "matched_notional_usdc": 0.0,
            "event_payload": {"idempotency_key": intent_id, "reason": str(exc), "source": lane},
        })
        entry.update({"state": receipt.get("state"), "reason": str(exc), "exchange_post_started": False})
        _close_cycle(conn, cycle_id, decision_id)
        return entry
    gate = can_submit_next_order(
        conn, bucket=bucket, token_id=token_id, exclude_order_intent_id=intent_id, same_cycle_id=cycle_id,
    )
    armed = live_intent_armed()
    if gate.get("allowed") is not True or not live_submit_authorized or not armed:
        reason = (
            str(gate.get("reason") or "sequential_gate_blocked") if gate.get("allowed") is not True
            else "live_submit_not_authorized" if not live_submit_authorized
            else "live_intent_disarmed"
        )
        receipt = record_order_event(conn, order_intent_id=intent_id, event={
            "event_type": "NEVER_REACHED_EXCHANGE", "exchange_order_id": "", "observed_at": sent_at,
            "matched_size": 0.0, "matched_notional_usdc": 0.0,
            "event_payload": {"idempotency_key": intent_id, "payload_sha256": content_sha256(payload),
                              "reason": reason, "source": lane},
        })
        entry.update({"state": receipt.get("state"), "reason": reason, "exchange_post_started": False,
                      "payload_sha256": content_sha256(payload)})
        _close_cycle(conn, cycle_id, decision_id)
        return entry
    response = transport(PLACE_ORDER_PATH, payload)
    order_type = str(payload.get("order_type") or "")
    outcome = classify_submission_response(response, order_type=order_type)
    observed = wall_clock_utc()
    if not outcome["submitted"]:
        receipt = record_order_event(conn, order_intent_id=intent_id, event={
            "event_type": outcome["event_type"], "exchange_order_id": "", "observed_at": observed,
            "matched_size": 0.0, "matched_notional_usdc": 0.0,
            "event_payload": {"idempotency_key": intent_id, "payload_sha256": content_sha256(payload),
                              "reason": outcome["reason"], "source": str(response.get("source") or ""),
                              "http_status": response.get("http_status")},
        })
        entry.update({"state": receipt.get("state"), "event_type": outcome["event_type"],
                      "reason": outcome["reason"], "exchange_post_started": False})
        _close_cycle(conn, cycle_id, decision_id)
        return entry
    fill = outcome.get("fill") or {}
    matched_size, matched_notional = fill_quantities(
        fill, event_type=outcome["event_type"], order_action="BUY", limit_price=float(price),
    )
    fok_short = None
    if (
        order_type == "FOK" and outcome["event_type"] == "FILL"
        and 0.0 < matched_size < float(shares) - VENUE_SHARE_QUANTUM - 1e-9
    ):
        # A fill-or-kill the venue matched IS the whole order: nothing of it
        # rests.  The venue's matched size can sit under the size we sent
        # (EDDM 2026-09-19: 9.5 of 9.67 at the limit -- the venue's own
        # rounding and fee), and read as a partial fill it left the intent
        # at ``reconcile_required`` and the station-day's quotes frozen.
        # The request is restated as what the venue executed, with the size
        # sent kept on the event, so the ledger closes it as filled.
        fok_short = {"requested_size_at_send": float(shares), "requested_notional_at_send": notional}
        conn.execute(
            "UPDATE basket_target_order_intents SET requested_size=?, requested_notional_usdc=?"
            " WHERE order_intent_id=?",
            (float(matched_size), round(float(matched_notional), 6), intent_id),
        )
    receipt = record_order_event(conn, order_intent_id=intent_id, event={
        "event_type": outcome["event_type"],
        "exchange_order_id": str(fill.get("exchange_order_id") or response.get("exchange_order_id") or ""),
        "observed_at": observed, "matched_size": matched_size, "matched_notional_usdc": matched_notional,
        "event_payload": {"idempotency_key": intent_id, "payload_sha256": content_sha256(payload),
                          "reason": outcome["reason"], "source": str(response.get("source") or ""),
                          "exchange_status": str(fill.get("status") or ""),
                          **({"fok_short_fill": fok_short} if fok_short else {})},
    })
    entry.update({"state": receipt.get("state"), "event_type": outcome["event_type"],
                  "matched_size": matched_size, "matched_notional_usdc": matched_notional,
                  "exchange_post_started": True, **({"fok_short_fill": fok_short} if fok_short else {})})
    _close_cycle(conn, cycle_id, decision_id)
    return entry


def _close_cycle(conn: sqlite3.Connection, cycle_id: str, decision_id: str | None = None) -> None:
    """See ``exit_orders._close_cycle``: one order, answered, nothing further
    to do -- ``settled``, so the station-day's quotes keep refreshing.  The
    decision row is append-only and is written ``prepared`` to begin with."""

    conn.execute(
        "UPDATE basket_target_execution_cycles SET state='settled', updated_at=? WHERE cycle_id=?",
        (wall_clock_utc(), cycle_id),
    )


__all__ = ["lane_decision_id", "send_lane_buy"]
