"""Sell the legs the completion rule bought outside its own definition.

User ruling 2026-09-17 ("卖"), on the day charter 143 went live.  Its first
two versions completed legs the station-day had never sent an order for --
the seat label had moved and the rule read the new window's unheld rungs as
missing -- and sized one completion to a held rung the dollar minimum had
lifted.  Eighteen orders, fifteen fills, $30.43, on legs the rule's own v2
definition (a leg this station-day SENT, at no more than its sent size)
would not have bought.

This lane sells exactly those shares, every one of them (user ruling, same
day: "不符合缺腿条件的全部卖出" -- the proposed guard of selling only where
the bid stood above the served probability was overruled).  The order is a
limit at the best bid for the whole remainder: the bid's own depth fills at
once, the rest rests until the order expires and is re-priced on the next
cycle's bid.  The served probability rides on the receipt as evidence.

Mechanics, in the shape of the other live lanes: a deterministic intent per
leg per cycle in ``basket_target_order_intents``, a GTD limit sell (a FOK
sell is refused by the gateway's limit-only boundary; a GTD at the bid
fills what the bid holds and expires), events and reconciliation rows
through ``record_order_event`` so the fill lane, the inventory and the
dashboard see it as any other order.  What this lane has already sold is
read back off its own intents, so a fill the reconciliation has not folded
into inventory yet cannot be sold twice.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import ContractError, content_sha256
from .execution_reconciliation import can_submit_next_order, record_order_event
from .inventory_producer import ledger_holdings_by_token
from .order_submission import (
    BASKET_STRATEGY_ID,
    PLACE_ORDER_PATH,
    classify_submission_response,
    fill_quantities,
    live_intent_armed,
)
from .production_io import load_chain_sent_shares
from .settlement_time import settlement_day_state

MIS_BOUGHT_LEG_EXIT = (
    os.environ.get("PM_MIS_BOUGHT_LEG_EXIT", "").strip().lower() or "on"
)
if MIS_BOUGHT_LEG_EXIT not in {"on", "off"}:
    raise RuntimeError(f"unknown mis-bought leg exit mode: {MIS_BOUGHT_LEG_EXIT}")
SCHEMA = "mis_bought_leg_exit_plan_v1"
RULE_VERSION = "mis_bought_leg_exit_all_at_bid_v2"
INTENT_PREFIX = "misbuyexit"
# The completion rule's live versions whose fills this lane may sell; a
# fill under any other winner is a seat, not a mis-buy.
COMPLETION_RULES = frozenset({
    "broken_basket_completion_by_held_count_v1",
    "broken_basket_completion_sent_legs_v2",
})
# Charter 143 went live at 02:00:42Z; nothing before it was a completion by
# held count.
MIS_BUY_WINDOW_START_UTC = "2026-09-17T02:00:42Z"
# A bid older than this is not a price anyone will pay: the order is priced
# on it and expires unfilled if the book has moved, but a stale bid can also
# be a book that has since fallen through, and the sell would rest under it.
MAX_BID_AGE_SECONDS = 900.0
# The venue's floor on a GTD (180s); a little longer so the bid has a
# chance to refill under a partially filled remainder.
GTD_HORIZON_SECONDS = 300
DEFAULT_MIN_ORDER_SHARES = 5.0


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def mis_bought_legs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every share a completion decision bought outside the v2 definition.

    A fill under a completion winner on a bucket the station-day had not
    sent before that decision is a mis-buy in full; a fill beyond the size
    the bucket had been sent at is a mis-buy for the excess.  Aggregated per
    (station, event date, bucket, token).
    """

    legs: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT d.station_id, d.event_date, d.settlement_timezone, d.created_at,"
        " i.bucket, i.token_id, COALESCE(r.filled_size, 0), i.order_intent_id"
        " FROM basket_target_order_intents i"
        " JOIN auto_trade_decisions d ON d.decision_id=i.decision_id"
        " LEFT JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_action='BUY' AND d.created_at>=?"
        "   AND COALESCE(r.filled_size, 0)>0"
        " ORDER BY d.created_at",
        (MIS_BUY_WINDOW_START_UTC,),
    ).fetchall()
    fact_cache: dict[tuple[str, str, str], str | None] = {}
    for station_id, event_date, timezone_name, decided_at, bucket, token_id, filled, intent_id in rows:
        key = (str(station_id), str(event_date), str(decided_at))
        if key not in fact_cache:
            fact = conn.execute(
                "SELECT feature_json FROM basket_policy_training_fact"
                " WHERE station_id=? AND event_date=? AND created_at_utc=?",
                key,
            ).fetchone()
            rule = None
            if fact:
                try:
                    plan = json.loads(fact[0]).get("plan") or {}
                    rule = (plan.get("target_solution") or {}).get(
                        "winner_completion_rule"
                    )
                except (ValueError, AttributeError):
                    rule = None
            fact_cache[key] = rule
        if fact_cache[key] not in COMPLETION_RULES:
            continue
        sent = load_chain_sent_shares(
            conn, station_id=str(station_id), event_date=str(event_date),
            before_utc=str(decided_at),
        )
        filled_shares = float(filled or 0.0)
        if str(bucket) not in sent:
            excess, kind = filled_shares, "never_sent"
        elif filled_shares > float(sent[str(bucket)]) + 1e-9:
            excess, kind = filled_shares - float(sent[str(bucket)]), "over_sent_size"
        else:
            continue
        leg_key = (str(station_id), str(event_date), str(bucket), str(token_id))
        entry = legs.setdefault(leg_key, {
            "station_id": str(station_id),
            "event_date": str(event_date),
            "settlement_timezone": str(timezone_name or ""),
            "bucket": str(bucket),
            "token_id": str(token_id),
            "mis_bought_shares": 0.0,
            "fills": [],
        })
        entry["mis_bought_shares"] += excess
        entry["fills"].append({
            "order_intent_id": str(intent_id), "decided_at": str(decided_at),
            "filled_shares": filled_shares, "mis_bought_shares": excess, "kind": kind,
            "rule": fact_cache[key],
        })
    return list(legs.values())


def _sold_by_this_lane(conn: sqlite3.Connection, *, station_id: str, event_date: str, bucket: str) -> tuple[float, bool]:
    """Shares this lane has already sold on the leg, and whether one of its
    sells is still open (pending or unresolved) -- which blocks another."""

    rows = conn.execute(
        "SELECT i.state, COALESCE(r.state, ''), COALESCE(r.filled_size, 0)"
        " FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_intent_id LIKE ? AND i.order_action='SELL'"
        "   AND i.bucket=? AND i.decision_id=?",
        (INTENT_PREFIX + "_%", bucket, _decision_id(station_id, event_date)),
    ).fetchall()
    sold = 0.0
    open_order = False
    for intent_state, reconciliation_state, filled in rows:
        sold += float(filled or 0.0)
        if str(intent_state) in {"prepared", "submitting", "submitted", "reconcile_required"} and str(
            reconciliation_state
        ) not in {"resolved_filled", "resolved_cancelled", "resolved_failed", "resolved_expired", "resolved_not_placed"}:
            open_order = True
    return sold, open_order


def _decision_id(station_id: str, event_date: str) -> str:
    return f"{INTENT_PREFIX}_decision_{station_id}_{event_date}"


def _registry_min_order_shares(conn: sqlite3.Connection, *, station_id: str, event_date: str, token_id: str) -> float:
    row = conn.execute(
        "SELECT json_extract(registry_json,'$.order_min_size')"
        " FROM basket_target_market_registry"
        " WHERE station_id=? AND event_date=? AND yes_token=?",
        (station_id, event_date, token_id),
    ).fetchone()
    try:
        value = float(row[0]) if row and row[0] is not None else DEFAULT_MIN_ORDER_SHARES
    except (TypeError, ValueError):
        value = DEFAULT_MIN_ORDER_SHARES
    return value if math.isfinite(value) and value > 0 else DEFAULT_MIN_ORDER_SHARES


def served_claim(conn: sqlite3.Connection, *, station_id: str, event_date: str, bucket: str, now_utc: str) -> float | None:
    """The probability the model serves for the bucket now, or None."""

    from .production_io import (  # noqa: PLC0415 -- heavy module, lane-local
        load_materialized_probability_envelopes,
        target_registry_source_lane,
    )
    from .stages import select_current_stage  # noqa: PLC0415

    try:
        envelopes = load_materialized_probability_envelopes(
            conn, station_id=station_id, event_date=event_date,
            evaluation_at_utc=now_utc,
            expected_source_lane=target_registry_source_lane(
                conn, station_id=station_id, event_date=event_date,
            ),
        )
        envelope = select_current_stage(envelopes, at_utc=now_utc)
        value = float(envelope.probabilities.get(bucket, 0.0) or 0.0)
    except Exception:  # noqa: BLE001 -- no served belief, no sale
        return None
    return value if math.isfinite(value) else None


def latest_bids(conn: sqlite3.Connection, *, station_id: str, event_date: str, bucket: str, now_utc: str) -> tuple[list[tuple[float, float]], float | None]:
    """The bucket's bid ladder (price desc) off the newest committed book,
    and the book's age in seconds; ``[]`` and ``None`` without a book."""

    row = conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND observed_at<=?"
        " ORDER BY observed_at DESC, rowid DESC LIMIT 1",
        (station_id, event_date, now_utc),
    ).fetchone()
    if not row:
        return [], None
    try:
        buckets = json.loads(row[1] or "[]")
    except ValueError:
        return [], None
    levels: list[tuple[float, float]] = []
    for item in buckets:
        if not isinstance(item, Mapping) or str(item.get("bucket")) != bucket:
            continue
        for level in item.get("bid_levels") or ():
            try:
                price, shares = float(level["price"]), float(level["shares"])
            except (KeyError, TypeError, ValueError):
                continue
            if price > 0.0 and shares > 0.0:
                levels.append((price, shares))
    levels.sort(key=lambda level: -level[0])
    age = (_utc(now_utc) - _utc(row[0])).total_seconds()
    return levels, age


def plan_mis_bought_leg_exits(
    conn: sqlite3.Connection,
    *,
    now_utc: str,
    claim_reader: Callable[..., float | None] = served_claim,
    bid_reader: Callable[..., tuple[list[tuple[float, float]], float | None]] = latest_bids,
) -> dict[str, Any]:
    """The sells this cycle should place, and why each leg was or was not one.

    ``claim_reader`` is evidence only since the ruling of 2026-09-17 ("全部
    卖出"); it no longer gates a sale.
    """

    plans: list[dict[str, Any]] = []
    readings: list[dict[str, Any]] = []
    if MIS_BOUGHT_LEG_EXIT != "on":
        return {"schema": SCHEMA, "rule": RULE_VERSION, "mode": MIS_BOUGHT_LEG_EXIT, "plans": [], "readings": []}
    for leg in mis_bought_legs(conn):
        station_id, event_date, bucket, token_id = (
            leg["station_id"], leg["event_date"], leg["bucket"], leg["token_id"],
        )
        reading: dict[str, Any] = {
            "station_id": station_id, "event_date": event_date, "bucket": bucket,
            "mis_bought_shares": round(leg["mis_bought_shares"], 6),
        }
        readings.append(reading)
        timezone_name = leg["settlement_timezone"]
        if not timezone_name:
            reading["skipped"] = "no_settlement_timezone"
            continue
        try:
            state = settlement_day_state(event_date, as_of_utc=now_utc, settlement_timezone=timezone_name)["state"]
        except Exception:  # noqa: BLE001
            reading["skipped"] = "settlement_state_unknown"
            continue
        if state == "closed":
            # Settlement or the recycle lane owns a closed day.
            reading["skipped"] = "day_closed"
            continue
        sold, open_order = _sold_by_this_lane(conn, station_id=station_id, event_date=event_date, bucket=bucket)
        reading["sold_shares"] = round(sold, 6)
        if open_order:
            reading["skipped"] = "sell_open"
            continue
        holdings = ledger_holdings_by_token(conn, station_id=station_id, event_date=event_date)
        held = float(((holdings.get("holdings") or {}).get(token_id) or {}).get("shares", 0.0) or 0.0)
        remaining = min(leg["mis_bought_shares"] - sold, held)
        reading["held_shares"] = round(held, 6)
        reading["remaining_shares"] = round(max(0.0, remaining), 6)
        min_shares = _registry_min_order_shares(conn, station_id=station_id, event_date=event_date, token_id=token_id)
        if remaining + 1e-9 < min_shares:
            reading["skipped"] = f"below_min_order:{min_shares:g}"
            continue
        # Evidence only: the probability the model serves for the leg now.
        claim = claim_reader(conn, station_id=station_id, event_date=event_date, bucket=bucket, now_utc=now_utc)
        reading["claim"] = claim
        levels, age = bid_reader(conn, station_id=station_id, event_date=event_date, bucket=bucket, now_utc=now_utc)
        reading["bid_age_seconds"] = age
        if not levels or age is None or age < 0.0 or age > MAX_BID_AGE_SECONDS:
            reading["skipped"] = "no_fresh_bid"
            continue
        # The whole remainder at the best bid: its depth fills at once, the
        # rest rests until expiry and is re-priced next cycle.  Walking the
        # ladder down for a full fill would sell into the dust levels under
        # a thin bid; resting at the best bid sells at the market's own
        # price or not at all this cycle.
        limit_price = float(levels[0][0])
        reading["best_bid"] = limit_price
        reading["bid_depth_shares"] = float(levels[0][1])
        size = math.floor(remaining * 100.0) / 100.0
        if size + 1e-9 < min_shares:
            reading["skipped"] = f"below_min_order:{min_shares:g}"
            continue
        minted_at = _utc(now_utc).replace(second=0, microsecond=0)
        intent_id = INTENT_PREFIX + "_" + content_sha256({
            "schema": SCHEMA, "station_id": station_id, "event_date": event_date,
            "bucket": bucket, "token_id": token_id, "minute": _iso(minted_at),
        })[:40]
        plan = {
            "schema": SCHEMA,
            "rule": RULE_VERSION,
            "order_intent_id": intent_id,
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "bucket": bucket,
            "token_id": token_id,
            "shares": size,
            "limit_price": limit_price,
            "claim": claim,
            "best_bid": levels[0][0],
            "bid_age_seconds": age,
            "mis_bought_shares": round(leg["mis_bought_shares"], 6),
            "sold_before_shares": round(sold, 6),
            "held_shares": round(held, 6),
            "fills": leg["fills"],
        }
        reading["planned"] = {"shares": size, "limit_price": limit_price}
        plans.append(plan)
    return {"schema": SCHEMA, "rule": RULE_VERSION, "mode": MIS_BOUGHT_LEG_EXIT, "plans": plans, "readings": readings}


def execute_mis_bought_leg_exits(
    conn: sqlite3.Connection,
    *,
    transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    now_utc: str,
    live_submit_authorized: bool,
    claim_reader: Callable[..., float | None] = served_claim,
    bid_reader: Callable[..., tuple[list[tuple[float, float]], float | None]] = latest_bids,
) -> dict[str, Any]:
    """Mint the intents, send the GTD sells, record what came back."""

    planned = plan_mis_bought_leg_exits(conn, now_utc=now_utc, claim_reader=claim_reader, bid_reader=bid_reader)
    armed = live_intent_armed()
    receipts: list[dict[str, Any]] = []
    submitted = 0
    for plan in planned["plans"]:
        intent_id = plan["order_intent_id"]
        if conn.execute(
            "SELECT 1 FROM basket_target_order_intents WHERE order_intent_id=?", (intent_id,)
        ).fetchone():
            receipts.append({"order_intent_id": intent_id, "state": "already_minted"})
            continue
        cycle_id = f"{INTENT_PREFIX}_cycle_{content_sha256({'intent': intent_id})[:32]}"
        plan_sha = content_sha256(plan)
        conn.execute(
            "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,event_date,"
            "settlement_timezone,strategy_lane,plan_sha256,requested_reserve_usdc,state,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, f"{INTENT_PREFIX}_plan_{plan_sha[:32]}", plan["station_id"], plan["event_date"],
             plan["settlement_timezone"], "basket", plan_sha, 0.0, "submitting", now_utc, now_utc),
        )
        expiration = int(_utc(now_utc).timestamp()) + GTD_HORIZON_SECONDS
        intent_body = {
            "schema": "mis_bought_leg_exit_intent_v1",
            "rule": RULE_VERSION,
            "execution_candidate": "EXIT_GTD",
            "order_type": "GTD",
            "post_only": False,
            "expiration": expiration,
            "live_submit_authorized": bool(live_submit_authorized),
            "plan": plan,
        }
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,cycle_id,bucket,"
            "token_id,side,order_action,limit_price,requested_size,requested_notional_usdc,state,"
            "live_submit_authorized,idempotency_key,intent_json,intent_sha256,created_at,"
            "target_execution_hard_deadline_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent_id, _decision_id(plan["station_id"], plan["event_date"]), cycle_id, plan["bucket"],
             plan["token_id"], "YES", "SELL", plan["limit_price"], plan["shares"],
             round(plan["shares"] * plan["limit_price"], 6), "prepared", 1 if live_submit_authorized else 0,
             intent_id, json.dumps(intent_body, ensure_ascii=False, sort_keys=True), content_sha256(intent_body),
             now_utc, None),
        )
        gate = can_submit_next_order(conn, bucket=plan["bucket"], token_id=plan["token_id"], exclude_order_intent_id=intent_id, same_cycle_id=cycle_id)
        payload = {
            "strategy_id": BASKET_STRATEGY_ID,
            "order_intent_id": intent_id,
            "idempotency_key": intent_id,
            "token_id": plan["token_id"],
            "outcome_side": "YES",
            "side": "SELL",
            "price": plan["limit_price"],
            "size": plan["shares"],
            "order_type": "GTD",
            "post_only": False,
            "expiration": expiration,
        }
        entry: dict[str, Any] = {"order_intent_id": intent_id, "station_id": plan["station_id"], "bucket": plan["bucket"], "shares": plan["shares"], "limit_price": plan["limit_price"]}
        if gate.get("allowed") is not True or not live_submit_authorized or not armed:
            reason = (
                str(gate.get("reason") or "sequential_gate_blocked") if gate.get("allowed") is not True
                else "live_submit_not_authorized" if not live_submit_authorized else "live_intent_disarmed"
            )
            receipt = record_order_event(conn, order_intent_id=intent_id, event={
                "event_type": "NEVER_REACHED_EXCHANGE", "exchange_order_id": "", "observed_at": now_utc,
                "matched_size": 0.0, "matched_notional_usdc": 0.0,
                "event_payload": {"idempotency_key": intent_id, "payload_sha256": content_sha256(payload), "reason": reason, "source": "mis_bought_leg_exit"},
            })
            entry.update({"state": receipt.get("state"), "reason": reason})
            receipts.append(entry)
            _close_cycle(conn, cycle_id, now_utc)
            continue
        response = transport(PLACE_ORDER_PATH, payload)
        outcome = classify_submission_response(response, order_type="GTD")
        if not outcome["submitted"]:
            receipt = record_order_event(conn, order_intent_id=intent_id, event={
                "event_type": outcome["event_type"], "exchange_order_id": "", "observed_at": now_utc,
                "matched_size": 0.0, "matched_notional_usdc": 0.0,
                "event_payload": {"idempotency_key": intent_id, "payload_sha256": content_sha256(payload), "reason": outcome["reason"], "source": str(response.get("source") or ""), "http_status": response.get("http_status")},
            })
            entry.update({"state": receipt.get("state"), "event_type": outcome["event_type"], "reason": outcome["reason"]})
            receipts.append(entry)
            _close_cycle(conn, cycle_id, now_utc)
            continue
        fill = outcome.get("fill") or {}
        matched_size, matched_notional = fill_quantities(fill, event_type=outcome["event_type"], order_action="SELL", limit_price=float(plan["limit_price"]))
        receipt = record_order_event(conn, order_intent_id=intent_id, event={
            "event_type": outcome["event_type"],
            "exchange_order_id": str(fill.get("exchange_order_id") or response.get("exchange_order_id") or ""),
            "observed_at": now_utc, "matched_size": matched_size, "matched_notional_usdc": matched_notional,
            "event_payload": {"idempotency_key": intent_id, "payload_sha256": content_sha256(payload), "reason": outcome["reason"], "source": str(response.get("source") or ""), "exchange_status": str(fill.get("status") or "")},
        })
        submitted += 1
        entry.update({"state": receipt.get("state"), "event_type": outcome["event_type"], "matched_size": matched_size, "matched_notional_usdc": matched_notional})
        receipts.append(entry)
        _close_cycle(conn, cycle_id, now_utc)
    return {
        "schema": "mis_bought_leg_exit_receipt_v1",
        "rule": RULE_VERSION,
        "mode": MIS_BOUGHT_LEG_EXIT,
        "planned": len(planned["plans"]),
        "submitted": submitted,
        "live_intent_armed": bool(armed),
        "receipts": receipts,
        "readings": planned["readings"],
    }


def _close_cycle(conn: sqlite3.Connection, cycle_id: str, now_utc: str) -> None:
    conn.execute(
        "UPDATE basket_target_execution_cycles SET state='submitted', updated_at=? WHERE cycle_id=?",
        (now_utc, cycle_id),
    )


__all__ = [
    "MIS_BOUGHT_LEG_EXIT",
    "RULE_VERSION",
    "mis_bought_legs",
    "plan_mis_bought_leg_exits",
    "execute_mis_bought_leg_exits",
]
