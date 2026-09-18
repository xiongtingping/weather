"""The one way an exit lane sends a SELL: intent, gate, gateway, ledger.

Two lanes sell -- charter 144's mis-bought-leg exit and the 0.998 recycle of
a won position -- and until 2026-09-17 each would have had its own copy of
the same twelve steps (the recycle lane's copy wrote the intent and nothing
else: no event, no reconciliation, so a fill would never have reached the
inventory).  One function, so a sell is recorded exactly as a buy is:
``record_order_event`` writes the event and the reconciliation row, the fill
lane follows the exchange order id, and ``inventory_producer`` nets the sale.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .contracts import content_sha256
from .execution_reconciliation import can_submit_next_order, record_order_event
from .order_submission import (
    BASKET_STRATEGY_ID,
    PLACE_ORDER_PATH,
    classify_submission_response,
    fill_quantities,
    live_intent_armed,
)

# A sell makes the gateway fetch the token's conditional balance before it
# signs; four of the first eleven live sells timed out at the basket lanes'
# 15s (2026-09-17 12:33Z).
TRANSPORT_TIMEOUT_SECONDS = 45.0

Transport = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


def wall_clock_utc() -> str:
    """The moment an order is sent: its expiry and its event clock."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def default_transport(path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """The one local gateway, reached the one approved way, with a sell's patience."""

    import sys  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    root = str(_Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from clob_order_service_client import ClobOrderServiceClient  # noqa: PLC0415

    if path != PLACE_ORDER_PATH:
        raise ValueError("exit_transport_unknown_path")
    return ClobOrderServiceClient(timeout_seconds=TRANSPORT_TIMEOUT_SECONDS).place_order(payload)


def send_exit_sell(
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
    order_type: str,
    post_only: bool,
    intent_body: Mapping[str, Any],
    transport: Transport,
    live_submit_authorized: bool,
    expiration: int | None = None,
    tick_size: float | None = None,
) -> dict[str, Any]:
    """Mint one SELL intent under its own cycle row and send it.

    ``intent_body`` is the lane's own record (its plan, its rule); the
    execution fields are added here.  Returns the receipt entry.  A refused
    or unauthorized send is recorded as its event, never raised.
    """

    sent_at = wall_clock_utc()
    cycle_id = f"{lane}_cycle_{content_sha256({'intent': intent_id})[:32]}"
    body = {
        **dict(intent_body),
        "lane": lane,
        "order_type": order_type,
        "post_only": bool(post_only),
        "live_submit_authorized": bool(live_submit_authorized),
        **({"expiration": int(expiration)} if expiration is not None else {}),
        **({"tick_size": float(tick_size)} if tick_size is not None else {}),
    }
    body_sha = content_sha256(body)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,plan_sha256,requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, f"{lane}_plan_{body_sha[:32]}", station_id, event_date, settlement_timezone,
         "basket", body_sha, 0.0, "submitting", sent_at, sent_at),
    )
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,cycle_id,bucket,"
        "token_id,side,order_action,limit_price,requested_size,requested_notional_usdc,state,"
        "live_submit_authorized,idempotency_key,intent_json,intent_sha256,created_at,"
        "target_execution_hard_deadline_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (intent_id, f"{lane}_decision_{station_id}_{event_date}", cycle_id, bucket, token_id,
         "YES", "SELL", float(price), float(shares), round(float(shares) * float(price), 6),
         "prepared", 1 if live_submit_authorized else 0, intent_id,
         json.dumps(body, ensure_ascii=False, sort_keys=True), body_sha, sent_at, None),
    )
    payload: dict[str, Any] = {
        "strategy_id": BASKET_STRATEGY_ID,
        "order_intent_id": intent_id,
        "idempotency_key": intent_id,
        "token_id": token_id,
        "outcome_side": "YES",
        "side": "SELL",
        "price": float(price),
        "size": float(shares),
        "order_type": order_type,
        "post_only": bool(post_only),
        **({"expiration": int(expiration)} if expiration is not None else {}),
        **({"tick_size": float(tick_size)} if tick_size is not None else {}),
    }
    entry: dict[str, Any] = {
        "order_intent_id": intent_id, "station_id": station_id, "event_date": event_date,
        "bucket": bucket, "shares": float(shares), "limit_price": float(price),
    }
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
        entry.update({"state": receipt.get("state"), "reason": reason, "exchange_post_started": False})
        _close_cycle(conn, cycle_id)
        return entry
    response = transport(PLACE_ORDER_PATH, payload)
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
        _close_cycle(conn, cycle_id)
        return entry
    fill = outcome.get("fill") or {}
    matched_size, matched_notional = fill_quantities(
        fill, event_type=outcome["event_type"], order_action="SELL", limit_price=float(price),
    )
    receipt = record_order_event(conn, order_intent_id=intent_id, event={
        "event_type": outcome["event_type"],
        "exchange_order_id": str(fill.get("exchange_order_id") or response.get("exchange_order_id") or ""),
        "observed_at": observed, "matched_size": matched_size, "matched_notional_usdc": matched_notional,
        "event_payload": {"idempotency_key": intent_id, "payload_sha256": content_sha256(payload),
                          "reason": outcome["reason"], "source": str(response.get("source") or ""),
                          "exchange_status": str(fill.get("status") or "")},
    })
    entry.update({"state": receipt.get("state"), "event_type": outcome["event_type"],
                  "matched_size": matched_size, "matched_notional_usdc": matched_notional,
                  "exchange_post_started": True})
    _close_cycle(conn, cycle_id)
    return entry


def lane_sold_and_open(
    conn: sqlite3.Connection, *, lane: str, station_id: str, event_date: str, bucket: str,
) -> tuple[float, bool]:
    """Shares a lane has already sold on the leg, and whether one of its sells
    is still open -- pending, or unresolved -- which blocks another."""

    rows = conn.execute(
        "SELECT i.state, COALESCE(r.state, ''), COALESCE(r.filled_size, 0)"
        " FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_action='SELL' AND i.bucket=? AND i.decision_id=?",
        (bucket, f"{lane}_decision_{station_id}_{event_date}"),
    ).fetchall()
    sold = 0.0
    open_order = False
    for intent_state, reconciliation_state, filled in rows:
        sold += float(filled or 0.0)
        if str(intent_state) in {"prepared", "submitting", "submitted", "reconcile_required"} and not str(
            reconciliation_state
        ).startswith("resolved_"):
            open_order = True
    return sold, open_order


def _close_cycle(conn: sqlite3.Connection, cycle_id: str) -> None:
    """A lane cycle is one order, and once that order has been answered the
    cycle has nothing further to do: ``settled`` is the table's one
    quiescent terminal state.  Left at ``submitted`` (as this did until
    2026-09-18) the row holds the station-day's market ingress shut for the
    rest of the day: charter 144's 2026-09-17 12:33Z exits froze the quotes
    of nine station-days, and every decision on them died on
    ``production_quote_expired``.  A pending order still holds the scope
    through the intent and reconciliation predicates; the cycle need not."""

    conn.execute(
        "UPDATE basket_target_execution_cycles SET state='settled', updated_at=? WHERE cycle_id=?",
        (wall_clock_utc(), cycle_id),
    )


__all__ = ["TRANSPORT_TIMEOUT_SECONDS", "default_transport", "lane_sold_and_open", "send_exit_sell", "wall_clock_utc"]
