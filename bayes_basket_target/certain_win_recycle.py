"""Keep a 0.99 sell resting on every held leg.

Operator ruling 2026-08-31: 确定赢的仓位提前折价回笼资金再周转 -- a position
that has won pays 1.000 at settlement, hours or days later; a sell a little
under that pays now, and the freed capital re-enters the budget.

What the lane does has been ruled three times since, all on 2026-09-18:
「直接挂单卖出啊，和收盘没关系吧」 (the close is not the condition),
「一直挂」 (the sell rests from the moment the leg is held, not only once the
market bids for it), and 「改为0.99」 (the price).  So: every held leg of a
market still taking orders carries a GTC limit sell at 0.99 for the shares
not already offered or sold, not post-only.  A leg the market already bids
at 0.99 or more fills at once, at the bid (0.999 on a won bucket); a leg it
does not rests at 0.99 until a buyer takes it or the venue closes the
market.  A leg that loses never meets a buyer at 0.99, and one sold at 0.99
that later loses was sold at 0.99 -- the price bounds both sides.

Why 0.99 and not 0.998: the venue prices a token between 0.04 and 0.96 on a
0.01 tick (measured 2026-09-18: 665 of 671 mid-priced book rows), where the
highest sell it accepts is 0.99; 0.998 exists only once the bucket is priced
past 0.96.  A sell that must rest from the moment the leg is held has to be
a price the venue takes at every price the leg can have.

Two gates had to let a resting recycle sell through, and do only for it
(RESTING_RECYCLE_SELL_PREFIX):

* the order-state quiescence predicates (order_state_producer), which
  refuse a station-day a zero-pending order snapshot -- and so stop its
  quotes renewing -- while any of its orders is open.  A resting sell of a
  held leg spends no cash and reserves nothing a decision reads; held all
  day it would have frozen every held station-day's quotes (RKSI
  2026-09-18, a charter 144 sell, froze RKSI's for a day);
* the same-bucket sequential gate (can_submit_next_order), which holds a
  token shut while another order on it is open: a plainly resting recycle
  sell no longer blocks a buy (the tiebreak lane's, a basket top-up) or the
  next recycle sell on that token.  One whose state is unknown still does.

History: from 08-31 to 09-17 the lane never placed an order (it read the
holdings under a key no snapshot wrote, lost the token when the target
registry pruned the market, and wrote no event or reconciliation); repaired
09-17, it was then paused by charter 146 with the other lanes until
「继续，打开车道」 reopened it 09-18 08:50Z.  The intent prefix keeps the
lane's first name, recycle998, so its decisions and every reader of them
keep one key.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import content_sha256
from .exit_orders import send_exit_sell

RECYCLE_PRICE = 0.99
STRATEGY_ID = "bayes_basket_target_v1"
INTENT_PREFIX = "recycle998"
SCHEMA = "certain_win_recycle_plan_v1"
RULE_VERSION = "certain_win_recycle_resting_099_v4"
DEFAULT_MIN_ORDER_SHARES = 5.0
DEFAULT_TICK_SIZE = 0.01
# A market takes orders until a few hours after its day ends (18-300 min,
# measured 2026-09-14..16), and a day ends at most about a day and a half
# after its date in UTC; older station-days hold nothing left to sell.
MAX_EVENT_AGE_DAYS = 3.0
# Sends the venue refused, per leg, before the lane leaves it to settle; and
# every send of the leg, so a sell the venue keeps retiring is not re-posted
# every cycle for the life of the market.
MAX_FAILED_SENDS_PER_LEG = 6
MAX_SENDS_PER_LEG = 12
# Sends per cycle.  Each goes through the gateway on the writer's turn (the
# charter 144 batch took ~15 s an order); the first pass after this rule
# offers every held leg at once, so it is spread over cycles, the legs the
# market bids highest first.
MAX_SENDS_PER_CYCLE = 8
# A book older than this gives no bid to order the sends by.
MAX_BOOK_AGE_SECONDS = 3600.0
# The venue's refusal when the account does not hold the shares.
BALANCE_REFUSAL_MARK = "not enough balance"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _registry_rows(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    station_id: str,
    event_date: str,
    outcome: str | None = None,
) -> dict[str, dict[str, Any]]:
    """The market rows for a station-day by outcome: the target registry's
    (a few days deep), then the canonical registry's (kept since July).

    ``outcome`` narrows the read to that bucket's newest row.  The target
    registry keeps every refresh (RJTT 2026-09-17: 3,201 rows, 4.3 MB of
    registry JSON) and is append-only, so its newest rowid is its newest
    refresh (3,146 station-day-outcomes since 2026-09-10, no exception) and
    the station-day index walked backwards finds it in one row; the canonical
    registry holds one row per outcome.  Measured 2026-09-18: the recycle
    planner took 20-40 s a cycle reading every held station-day's full
    history."""

    rows: dict[str, dict[str, Any]] = {}
    newest: dict[str, str] = {}
    narrow = "" if outcome is None else " AND outcome=? ORDER BY rowid DESC LIMIT 1"
    params: tuple[str, ...] = (
        (station_id, event_date) if outcome is None else (station_id, event_date, str(outcome))
    )
    for conn, table in (
        (target_conn, "basket_target_market_registry"),
        (canonical_conn, "reported_trajectory_market_registry"),
    ):
        if conn is None:
            continue
        if not _has_table(conn, table):
            continue
        for outcome_name, yes_token, registry_json, accepting, refreshed_at in conn.execute(
            f"SELECT outcome, yes_token, registry_json, market_accepting_orders, refreshed_at"
            f" FROM {table} WHERE station_id=? AND event_date=?{narrow}",
            params,
        ):
            key = str(outcome_name)
            # The newest refresh wins wherever it lives: the target registry
            # is refreshed while the day trades, the canonical one is what
            # recorded the venue closing the market afterwards.
            if not yes_token or str(refreshed_at or "") <= newest.get(key, ""):
                continue
            try:
                raw = json.loads(registry_json or "{}")
            except ValueError:
                raw = {}
            newest[key] = str(refreshed_at or "")
            rows[key] = {
                "yes_token": str(yes_token),
                "registry_json": str(registry_json or ""),
                "min_size": raw.get("order_min_size"),
                "tick_size": raw.get("order_price_min_tick_size"),
                "accepting_orders": accepting,
                "refreshed_at": str(refreshed_at or ""),
            }
    return rows


def recycle_price(tick_size: Any) -> float:
    """0.99 on the venue's 0.01 and 0.001 ticks alike; floored to a coarser one."""

    try:
        tick = float(tick_size)
    except (TypeError, ValueError):
        tick = DEFAULT_TICK_SIZE
    if not math.isfinite(tick) or tick <= 0.0:
        tick = DEFAULT_TICK_SIZE
    price = math.floor(RECYCLE_PRICE / tick + 1e-9) * tick
    return round(min(RECYCLE_PRICE, price), 6)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _floor_cents(shares: float) -> float:
    """Down to the venue's 0.01 share, without binary float taking a cent
    off a size that is already whole cents (12.46 * 100 is 1245.999...;
    WMKK 2026-09-18 would have left 0.01 of 12.46 behind)."""

    return math.floor(float(shares) * 100.0 + 1e-6) / 100.0


def _latest_book(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, now_utc: str,
) -> dict[str, Any]:
    """The station-day's newest committed book at or before now_utc: its
    settlement timezone (any age), and per bucket the best bid and the tick
    when the book is young enough to order the sends by."""

    out: dict[str, Any] = {"settlement_timezone": "", "legs": {}}
    if not _has_table(conn, "basket_official_ws_snapshot"):
        return out
    row = conn.execute(
        "SELECT observed_at, buckets_json, settlement_timezone FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=?"
        " ORDER BY observed_at DESC, sequence DESC LIMIT 1",
        (station_id, event_date, now_utc),
    ).fetchone()
    if row is None:
        return out
    out["settlement_timezone"] = str(row[2] or "")
    try:
        age = (_utc(now_utc) - _utc(str(row[0]))).total_seconds()
        buckets = json.loads(row[1] or "[]") or []
    except (TypeError, ValueError):
        return out
    if age < 0.0 or age > MAX_BOOK_AGE_SECONDS:
        return out
    for item in buckets:
        if not isinstance(item, Mapping):
            continue
        best_bid = 0.0
        for level in item.get("bid_levels") or ():
            try:
                best_bid = max(best_bid, float(level["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        out["legs"][str(item.get("bucket"))] = {
            "best_bid": best_bid,
            "tick_size": item.get("price_tick_size"),
            "min_size": item.get("min_order_size"),
            "token_id": str(item.get("token_id") or ""),
        }
    return out


def _settlement_timezone(
    canonical_conn: sqlite3.Connection | None, *, station_id: str, event_date: str, book_timezone: str,
) -> str:
    if book_timezone or canonical_conn is None:
        return book_timezone
    if not _has_table(canonical_conn, "reported_trajectory_canonical_station_day_bounds"):
        return ""
    bound = canonical_conn.execute(
        "SELECT settlement_timezone FROM reported_trajectory_canonical_station_day_bounds"
        " WHERE station_id=? AND event_date=?",
        (station_id, event_date),
    ).fetchone()
    return str((bound or [""])[0] or "")


def _own_sells(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, bucket: str, counted: set[str],
) -> dict[str, Any]:
    """This lane's sells on the leg: the shares still offered by its open
    orders, the shares it sold that the inventory has not netted yet, and
    how many of its sends the venue refused and how many it made."""

    offered = 0.0
    sold_unseen = 0.0
    failed = 0
    sends = 0
    no_balance = False
    for intent_id, intent_state, requested, reconciliation_state, filled, refusal in conn.execute(
        "SELECT i.order_intent_id, i.state, i.requested_size, COALESCE(r.state, ''), COALESCE(r.filled_size, 0),"
        " (SELECT e.event_json FROM basket_target_order_events e WHERE e.order_intent_id=i.order_intent_id"
        "   AND e.event_type='SUBMIT_REFUSED' ORDER BY e.rowid DESC LIMIT 1)"
        " FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_action='SELL' AND i.bucket=? AND i.decision_id=?",
        (bucket, f"{INTENT_PREFIX}_decision_{station_id}_{event_date}"),
    ):
        sends += 1
        if refusal and BALANCE_REFUSAL_MARK in str(refusal):
            no_balance = True
        filled = float(filled or 0.0)
        if str(intent_state) in {"prepared", "submitting", "submitted", "reconcile_required"} and not str(
            reconciliation_state
        ).startswith("resolved_"):
            # Still at the venue, or not known not to be: all of it that has
            # not filled is offered.
            offered += max(0.0, float(requested or 0.0) - filled)
        if str(intent_state) == "failed":
            failed += 1
        # The inventory already nets every sale it has seen (SELL fills count
        # negative); only this lane's fills missing from its lineage are
        # taken off again.  Taking every sale off twice left a partly sold
        # leg reading zero once the inventory caught up.
        if str(intent_id) not in counted:
            sold_unseen += filled
    return {"offered": offered, "sold_unseen": sold_unseen, "failed": failed, "sends": sends,
            "no_balance": no_balance}


def _intent_id(*, station_id: str, event_date: str, bucket: str, token_id: str, now_utc: str) -> str:
    minute = _utc(now_utc).replace(second=0, microsecond=0)
    return INTENT_PREFIX + "_" + content_sha256({
        "schema": SCHEMA, "rule": RULE_VERSION, "station_id": station_id, "event_date": event_date,
        "bucket": bucket, "token_id": token_id,
        "minute": minute.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    })[:40]


def plan_certain_win_recycles(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    now_utc: str,
) -> list[dict[str, Any]]:
    """The 0.99 sells to place: for every held leg of a market still taking
    orders, the shares no open sell of this lane already offers and the
    inventory has not seen sold, when that is at least the venue minimum.

    Reads only.  Ordered by the leg's best bid, highest first, so the sends
    a cycle can make go to the legs closest to paying.
    """

    plans: list[dict[str, Any]] = []
    # The newest complete inventory of each station-day, off the covering
    # index (see the 2026-09-09/14 measurements in git history: a bare
    # column beside MAX() re-read every row), only for the days whose
    # markets can still take orders, and CROSS JOIN so each newest row is
    # one seek from g rather than a scan of every complete snapshot to meet
    # g (measured 2026-09-18: 1,952 station-days, 3.6 s -> 0.07-0.3 s).
    oldest_date = (_utc(now_utc) - timedelta(days=MAX_EVENT_AGE_DAYS)).date().isoformat()
    inventories = target_conn.execute(
        "SELECT s.station_id, s.event_date, s.inventory_json, s.observed_at,"
        "       MAX(s.rowid)"
        " FROM (SELECT station_id, event_date, MAX(observed_at) AS newest"
        "       FROM basket_confirmed_inventory_snapshot"
        "       WHERE complete=1 AND event_date>=?"
        "       GROUP BY station_id, event_date) g"
        " CROSS JOIN basket_confirmed_inventory_snapshot s"
        "   ON s.complete=1 AND s.station_id=g.station_id"
        "  AND s.event_date=g.event_date AND s.observed_at=g.newest"
        " GROUP BY s.station_id, s.event_date",
        (oldest_date,),
    ).fetchall()
    for station_id, event_date, inventory_json, _observed_at, _row in inventories:
        station_id, event_date = str(station_id), str(event_date)
        try:
            inventory = json.loads(inventory_json or "{}")
        except ValueError:
            continue
        # The snapshot writes filled_by_bucket; filled_shares_by_bucket
        # was the name this lane read for its whole first month, and no
        # snapshot ever carried it.
        held = {
            str(bucket): float(shares)
            for bucket, shares in (
                inventory.get("filled_by_bucket")
                or inventory.get("filled_shares_by_bucket")
                or {}
            ).items()
            if float(shares or 0.0) > 1e-9
        }
        if not held:
            continue
        counted = {
            str(item.get("order_intent_id") or "")
            for item in inventory.get("lineage") or ()
            if isinstance(item, Mapping)
        }
        book = _latest_book(target_conn, station_id=station_id, event_date=event_date, now_utc=now_utc)
        timezone_name = ""
        for bucket in sorted(held):
            own = _own_sells(
                target_conn, station_id=station_id, event_date=event_date, bucket=bucket, counted=counted,
            )
            if own["failed"] >= MAX_FAILED_SENDS_PER_LEG or own["sends"] >= MAX_SENDS_PER_LEG:
                continue
            if own["no_balance"]:
                # The venue holds none of what the ledger says is held: the
                # position is the ledger's error, not a sale to retry (RKPK
                # 27C 2026-09-18: 34 shares on the books, 0 in the account).
                continue
            uncovered = _floor_cents(held[bucket] - own["sold_unseen"] - own["offered"])
            if uncovered <= 0.0:
                continue
            leg = book["legs"].get(bucket) or {}
            registry = _registry_rows(
                target_conn, canonical_conn, station_id=station_id, event_date=event_date, outcome=bucket,
            )
            row = registry.get(bucket) or {}
            if row.get("accepting_orders") is not None and int(row["accepting_orders"] or 0) != 1:
                # The venue has closed the market: the tokens redeem, nothing sells.
                continue
            token_id = row.get("yes_token") or leg.get("token_id") or str(
                (inventory.get("token_ids_by_bucket") or {}).get(bucket) or ""
            )
            if not token_id:
                continue
            try:
                min_size = float(row.get("min_size") or leg.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
            except (TypeError, ValueError):
                min_size = DEFAULT_MIN_ORDER_SHARES
            if uncovered + 1e-9 < max(min_size, 1e-6):
                continue
            if not timezone_name:
                timezone_name = _settlement_timezone(
                    canonical_conn, station_id=station_id, event_date=event_date,
                    book_timezone=book["settlement_timezone"],
                )
            if not timezone_name:
                continue
            # The book's tick is the venue's current one (the registry's can
            # lag a switch to 0.001); 0.99 is on both.
            tick = leg.get("tick_size") or row.get("tick_size") or DEFAULT_TICK_SIZE
            try:
                tick_size = float(tick)
            except (TypeError, ValueError):
                tick_size = DEFAULT_TICK_SIZE
            plans.append({
                "schema": SCHEMA,
                "rule": RULE_VERSION,
                "order_intent_id": _intent_id(
                    station_id=station_id, event_date=event_date, bucket=bucket,
                    token_id=str(token_id), now_utc=now_utc,
                ),
                "station_id": station_id,
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "bucket": bucket,
                "token_id": str(token_id),
                "shares": uncovered,
                "limit_price": recycle_price(tick_size),
                "tick_size": tick_size,
                "best_bid": float(leg.get("best_bid") or 0.0),
                "held_shares": round(held[bucket], 6),
                "offered_before_shares": round(own["offered"], 6),
                "sold_before_shares": round(own["sold_unseen"], 6),
            })
    plans.sort(key=lambda plan: (-plan["best_bid"], plan["station_id"], plan["event_date"], plan["bucket"]))
    return plans


def execute_certain_win_recycles(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    now_utc: str,
    live_submit_authorized: bool,
    max_sends: int = MAX_SENDS_PER_CYCLE,
) -> dict[str, Any]:
    """Mint the intents, place the resting sells, record what came back --
    through the same sender and ledger path every other order uses."""

    plans = plan_certain_win_recycles(target_conn, canonical_conn, now_utc=now_utc)
    receipts: list[dict[str, Any]] = []
    submitted = 0
    sent = 0
    for plan in plans:
        if sent >= int(max_sends):
            break
        intent_id = plan["order_intent_id"]
        if target_conn.execute(
            "SELECT 1 FROM basket_target_order_intents WHERE order_intent_id=?", (intent_id,)
        ).fetchone():
            receipts.append({"order_intent_id": intent_id, "state": "already_minted"})
            continue
        sent += 1
        entry = send_exit_sell(
            target_conn, lane=INTENT_PREFIX, intent_id=intent_id,
            station_id=plan["station_id"], event_date=plan["event_date"],
            settlement_timezone=plan["settlement_timezone"], bucket=plan["bucket"],
            token_id=plan["token_id"], price=plan["limit_price"], shares=plan["shares"],
            order_type="GTC", post_only=False, tick_size=plan["tick_size"],
            intent_body={"schema": SCHEMA, "rule": RULE_VERSION, "strategy_id": STRATEGY_ID,
                         "execution_candidate": "EXIT_GTC_LIMIT", "plan": plan},
            transport=transport, live_submit_authorized=live_submit_authorized,
        )
        if entry.get("exchange_post_started"):
            submitted += 1
        receipts.append(entry)
    return {
        "schema": "certain_win_recycle_receipt_v1",
        "rule": RULE_VERSION,
        "planned": len(plans),
        "sent": sent,
        "deferred_to_next_cycle": max(0, len(plans) - sent),
        "submitted": submitted,
        "receipts": receipts,
    }


__all__ = [
    "RECYCLE_PRICE",
    "RULE_VERSION",
    "recycle_price",
    "plan_certain_win_recycles",
    "execute_certain_win_recycles",
]
