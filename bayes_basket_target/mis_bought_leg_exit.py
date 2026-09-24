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
from .exit_orders import (
    TRANSPORT_TIMEOUT_SECONDS,
    default_transport,
    lane_sold_and_open,
    send_exit_sell,
    wall_clock_utc,
)
from .inventory_producer import ledger_holdings_by_token
from .order_submission import live_intent_armed
from .supply_watch import EGRESS_HOLD_REASON, egress_send_gate
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
# chance to refill under a partially filled remainder.  Counted from the
# moment each order is SENT, not from the cycle's clock: the first live
# batch (2026-09-17 12:33Z) sent eleven sells one after another, the
# gateway took a quarter of a minute over each, and the last two were
# refused ``gtd_expiration_too_soon:166s`` on an expiry stamped two minutes
# earlier.
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
    fact_cache: dict[tuple[str, str, str], tuple[str | None, bool]] = {}
    for station_id, event_date, timezone_name, decided_at, bucket, token_id, filled, intent_id in rows:
        key = (str(station_id), str(event_date), str(decided_at))
        if key not in fact_cache:
            fact = conn.execute(
                "SELECT feature_json FROM basket_policy_training_fact"
                " WHERE station_id=? AND event_date=? AND created_at_utc=?",
                key,
            ).fetchone()
            rule = None
            taf_entry = False
            if fact:
                try:
                    plan = json.loads(fact[0]).get("plan") or {}
                    rule = (plan.get("target_solution") or {}).get(
                        "winner_completion_rule"
                    )
                    # Charter 145: charter 90's TX-entry span rides the
                    # completion channel and buys legs the chain never sent
                    # -- on TAF authority.  Those are a seat, not a mis-buy.
                    taf_entry = bool(
                        (plan.get("completion_143_after_lift") or {}).get("taf_entry")
                    )
                except (ValueError, AttributeError):
                    rule, taf_entry = None, False
            fact_cache[key] = (rule, taf_entry)
        if fact_cache[key][0] not in COMPLETION_RULES or fact_cache[key][1]:
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
            "rule": fact_cache[key][0],
        })
    return list(legs.values())


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
        sold, open_order = lane_sold_and_open(conn, lane=INTENT_PREFIX, station_id=station_id, event_date=event_date, bucket=bucket)
        reading["sold_shares"] = round(sold, 6)
        if open_order:
            reading["skipped"] = "sell_open"
            continue
        # The basket's own shares only: a mis-bought leg is the basket's, and
        # another lane holding the same token must never be sold by this lane.
        from .production_io import basket_intent_ids  # noqa: PLC0415

        holdings = ledger_holdings_by_token(
            conn, station_id=station_id, event_date=event_date,
            intent_ids=basket_intent_ids(conn, station_id=station_id, event_date=event_date),
        )
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
    receipts: list[dict[str, Any]] = []
    submitted = 0
    # Charter 197 amendment (user ruling 2026-09-22): no new order while the
    # egress reading is red; held before minting, each plan named on the
    # receipt.  The lane re-plans from the book every cycle.
    egress_gate = egress_send_gate()
    if egress_gate.get("hold"):
        receipts = [
            {"order_intent_id": plan["order_intent_id"], "state": "held",
             "reason": EGRESS_HOLD_REASON, "exchange_post_started": False}
            for plan in planned["plans"]
        ]
    for plan in [] if egress_gate.get("hold") else planned["plans"]:
        intent_id = plan["order_intent_id"]
        if conn.execute(
            "SELECT 1 FROM basket_target_order_intents WHERE order_intent_id=?", (intent_id,)
        ).fetchone():
            receipts.append({"order_intent_id": intent_id, "state": "already_minted"})
            continue
        # The expiry counts from the moment this order is sent, not from the
        # cycle's clock: the first live batch stamped eleven sells with one
        # expiry and the last two were refused ``gtd_expiration_too_soon``.
        expiration = int(_utc(wall_clock_utc()).timestamp()) + GTD_HORIZON_SECONDS
        entry = send_exit_sell(
            conn, lane=INTENT_PREFIX, intent_id=intent_id,
            station_id=plan["station_id"], event_date=plan["event_date"],
            settlement_timezone=plan["settlement_timezone"], bucket=plan["bucket"],
            token_id=plan["token_id"], price=plan["limit_price"], shares=plan["shares"],
            order_type="GTD", post_only=False, expiration=expiration,
            intent_body={"schema": "mis_bought_leg_exit_intent_v1", "rule": RULE_VERSION,
                         "execution_candidate": "EXIT_GTD", "plan": plan},
            transport=transport, live_submit_authorized=live_submit_authorized,
        )
        if entry.get("exchange_post_started"):
            submitted += 1
        receipts.append(entry)
    return {
        "schema": "mis_bought_leg_exit_receipt_v1",
        "rule": RULE_VERSION,
        "mode": MIS_BOUGHT_LEG_EXIT,
        "planned": len(planned["plans"]),
        "submitted": submitted,
        "live_intent_armed": bool(live_intent_armed()),
        "egress_gate": dict(egress_gate),
        "receipts": receipts,
        "readings": planned["readings"],
    }


__all__ = [
    "MIS_BOUGHT_LEG_EXIT",
    "RULE_VERSION",
    "TRANSPORT_TIMEOUT_SECONDS",
    "default_transport",
    "wall_clock_utc",
    "mis_bought_legs",
    "plan_mis_bought_leg_exits",
    "execute_mis_bought_leg_exits",
]
