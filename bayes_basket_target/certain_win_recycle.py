"""Offer held YES and NO shares at 0.999 when the venue tick permits it.

The official WS book must provide a fresh tick that quotes 0.999 exactly.
Older offers under the price (the 0.99 GTCs, then the 0.998 ones) are
cancelled and reconciled before their shares can be reoffered. An unknown
cancellation or fill remains reserved in the ledger. The historic
``recycle998`` intent prefix is retained for the same lineage.

Charter 264 (user ruling 2026-10-09 「把我的卖出车道0.998也改为0.999」): the
price moved from 0.998 to 0.999 -- the price the benchmark cluster recycles
its own winners at.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import exit_orders
from .contracts import content_sha256
from .exit_orders import TRANSPORT_TIMEOUT_SECONDS, default_position_reader, send_exit_sell
from .supply_watch import EGRESS_HOLD_REASON, egress_held_sql, egress_send_gate

RECYCLE_PRICE = 0.999
STRATEGY_ID = "bayes_basket_target_v1"
INTENT_PREFIX = "recycle998"
SCHEMA = "certain_win_recycle_plan_v1"
RULE_VERSION = "certain_win_recycle_resting_0999_v6"
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
# Sends per cycle: no cap (user ruling 2026-10-11 「单轮不设置上限」; it was 8,
# and the first pass after the 10-11 fix left 19 legs for later cycles of
# 8-15 minutes).  Each send goes through the gateway on the writer's turn
# (2-7 s an order on 10-11), the legs the market bids highest first;
# ``max_sends`` stays for a caller that wants a bound.
MAX_SENDS_PER_CYCLE: int | None = None
# A dead leg: the market offers its own side at a cent or less (the venue
# shows it "<1%") -- a YES leg's lowest YES ask, a NO leg's 1 - the highest
# YES bid.  It is not offered at 0.999, and a 0.999 offer still resting on it
# is cancelled (user ruling 2026-10-11 「死腿就没必要卖了吧」, on Cape Town
# 10-11 23°C and 24°C YES resting at 0.999 while the market priced both <1%).
DEAD_LEG_MAX_ASK = 0.01
# A book older than this gives no bid to order the sends by.
MAX_BOOK_AGE_SECONDS = 3600.0
MAX_TICK_AGE_SECONDS = 120.0
# The venue's refusal when the account does not hold the shares.
BALANCE_REFUSAL_MARK = "not enough balance"
# The account is traded outside this system too (measured 2026-09-18: every
# one of eleven ledger-versus-venue differences since 09-01 was a trade no
# lane of this system made -- winners sold by hand at 0.999, Chengdu bought
# and sold by hand).  The ledger cannot see those, so each sell is sized to
# what the account holds beyond this lane's own resting offers, read from
# the venue just before it is sent; a leg the account holds too little of is
# not asked again for this long.
ACCOUNT_SHORT_RECHECK_SECONDS = 1200.0
_ACCOUNT_SHORT: dict[str, float] = {}
_MIGRATION_CURSOR = 0
# Charter 262 (user ruling 2026-10-09「0.998卖出车道是对所有策略和手动买单都要
# 生效」): the held legs come from the account itself, not only from the two
# ledgers this lane read before -- the basket's confirmed YES inventory (the
# basket closed under charter 231, so it no longer saw the Proper line's YES)
# and the execution-cycle NO fills (which the Proper and copy-follow lanes do
# not write).  Measured 2026-10-09 06:20Z: 66 of 68 weather positions in the
# account carried no 0.998 offer -- Proper YES/NO, copy-follow YES/NO, and the
# board's manual buys, which no ledger records at all.  The account list is
# the venue's public data API (minutes behind at most); each send is still
# sized on the gateway's own balance read just before it goes.
ACCOUNT_POSITIONS_TTL_SECONDS = 300.0
ACCOUNT_POSITIONS_TIMEOUT_SECONDS = 8.0
TOKEN_LEGS_TTL_SECONDS = 600.0
_ACCOUNT_POSITIONS: dict[str, Any] = {"at": -1e18, "positions": None, "error": None}
_TOKEN_LEGS: dict[str, Any] = {"at": -1e18, "oldest": None, "legs": {}}


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
    """Return the exact requested price, or refuse a tick that cannot quote it."""

    try:
        tick = float(tick_size)
    except (TypeError, ValueError):
        raise ValueError("recycle_tick_missing") from None
    if not math.isfinite(tick) or tick <= 0.0:
        raise ValueError("recycle_tick_invalid")
    if abs(RECYCLE_PRICE / tick - round(RECYCLE_PRICE / tick)) > 1e-7:
        raise ValueError("recycle_price_not_on_tick")
    return RECYCLE_PRICE


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _floor_cents(shares: float) -> float:
    """Down to the venue's 0.01 share, without binary float taking a cent
    off a size that is already whole cents (12.46 * 100 is 1245.999...;
    WMKK 2026-09-18 would have left 0.01 of 12.46 behind)."""

    return math.floor(float(shares) * 100.0 + 1e-6) / 100.0


def _latest_book(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, now_utc: str,
    ws_root: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The station-day's book: its settlement timezone off the newest
    committed snapshot (any age), and per bucket the best bid and the tick
    when the book is young enough to order the sends by.

    The ladder is the WS publisher's own when ``ws_root`` carries the scope
    (``p1_tail_lane.live_ws_snapshot``, the read the Proper and copy-follow
    lanes price on), else the newest committed snapshot.  The cycle commits
    books for one event day per ingress, so at the end of a cycle the other
    days' snapshots are minutes old and their tick was withheld here: on
    2026-10-11 the day's own books stopped at 12:52Z while the cycle served
    10-12/10-13, and 48 of 48 held legs still trading -- EDDM 14°C bid 0.931,
    FACT 25°C 0.963, EFHK 7°C 0.95 -- read no tick and were not offered."""

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
    live = None
    if ws_root is not None:
        from .p1_tail_lane import live_ws_snapshot  # noqa: PLC0415 -- p1_tail_lane imports this module

        live = live_ws_snapshot(conn, ws_root, station_id=station_id, event_date=event_date,
                                now_utc=now_utc, include_listing_days=True)
    try:
        if live is not None:
            age = (_utc(now_utc) - _utc(str(live["observed_at"]))).total_seconds()
            buckets = list(live["buckets"] or [])
            out["book_source"] = "ws_publisher_manifest"
        else:
            age = (_utc(now_utc) - _utc(str(row[0]))).total_seconds()
            buckets = json.loads(row[1] or "[]") or []
            out["book_source"] = "committed_snapshot"
    except (TypeError, ValueError):
        return out
    if age < 0.0 or age > MAX_BOOK_AGE_SECONDS:
        return out
    for item in buckets:
        if not isinstance(item, Mapping):
            continue
        best_bid = 0.0
        best_ask = 1.0
        for level in item.get("bid_levels") or ():
            try:
                best_bid = max(best_bid, float(level["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        for level in item.get("ask_levels") or ():
            try:
                best_ask = min(best_ask, float(level["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        out["legs"][str(item.get("bucket"))] = {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "tick_size": item.get("price_tick_size") if age <= MAX_TICK_AGE_SECONDS else None,
            "min_size": item.get("min_order_size"),
            "token_id": str(item.get("token_id") or ""),
        }
    return out


def _dead_leg(leg: Mapping[str, Any], side: str) -> bool:
    """True when the book offers the leg's own side at DEAD_LEG_MAX_ASK or
    less.  A side nobody offers (no YES ask; for NO, no YES bid) is not dead."""

    try:
        if str(side) == "NO":
            bid = float(leg.get("best_bid") or 0.0)
            own_ask = 1.0 - bid if bid > 0.0 else None
        else:
            ask = leg.get("best_ask")
            own_ask = float(ask) if ask is not None and float(ask) < 1.0 else None
    except (TypeError, ValueError):
        return False
    return own_ask is not None and own_ask <= DEAD_LEG_MAX_ASK + 1e-9


# The legs the last plan left unoffered as dead, named on the receipt.
_DEAD_LEGS: list[dict[str, Any]] = []


def _note_dead(station_id: str, event_date: str, bucket: str, side: str) -> None:
    entry = {"station_id": station_id, "event_date": event_date, "bucket": bucket, "side": side}
    if entry not in _DEAD_LEGS:  # a ledger leg and its account row are one leg
        _DEAD_LEGS.append(entry)


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
    conn: sqlite3.Connection, *, station_id: str, event_date: str, bucket: str,
    counted: set[str], side: str = "YES",
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
        " WHERE i.order_action='SELL' AND i.side=? AND i.bucket=? AND i.decision_id=?"
        # Charter 197 amendment: a sell the egress hold kept in never left --
        # neither a send nor a failure against the leg's caps.
        f" AND NOT {egress_held_sql('i')}",
        (side, bucket, f"{INTENT_PREFIX}_decision_{station_id}_{event_date}"),
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
        if str(intent_id) not in counted and (
            side == "YES" or str(reconciliation_state) not in {"resolved_filled", "resolved_partial"}
        ):
            sold_unseen += filled
    return {"offered": offered, "sold_unseen": sold_unseen, "failed": failed, "sends": sends,
            "no_balance": no_balance}


def _intent_id(*, station_id: str, event_date: str, bucket: str, token_id: str, now_utc: str,
               side: str = "YES") -> str:
    minute = _utc(now_utc).replace(second=0, microsecond=0)
    return INTENT_PREFIX + "_" + content_sha256({
        "schema": SCHEMA, "rule": RULE_VERSION, "station_id": station_id, "event_date": event_date,
        "bucket": bucket, "token_id": token_id,
        "side": side,
        "minute": minute.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    })[:40]


# The NO legs the ledger read refused on the last plan, named on the receipt.
_NO_LEDGER_CONFLICTS: list[dict[str, Any]] = []


def _held_no_legs(conn: sqlite3.Connection, oldest_date: str) -> list[tuple[str, str, str, str, str, float]]:
    """Settled NO token inventory, including sells, from the order ledger.

    The canonical inventory snapshot is deliberately YES-only.  A NO share
    cannot be read from its ``filled_by_bucket`` or mapped to a YES token.

    A sell planned from the account's own position list (charter 262,
    ``plan.source = account_positions``) sold shares no ledger BUY recorded,
    so it is not this ledger's sale: counting it drove ZUUU 2026-10-10 24°C NO
    to -25.95 (5.2 + 20.75 sold of a leg the ledger never bought), and the
    conflict, raised for the whole read, stopped every 0.999 offer of every
    leg from 2026-10-10 03:51Z until the day would have aged out (10-14).  A
    leg whose ledger still conflicts is refused on its own -- not planned,
    named in ``_NO_LEDGER_CONFLICTS`` -- and the other legs go on.
    """

    rows = conn.execute(
        "SELECT c.station_id,c.event_date,i.bucket,i.token_id,"
        " MIN(c.settlement_timezone),MAX(c.settlement_timezone),"
        " SUM(CASE i.order_action WHEN 'BUY' THEN r.filled_size"
        " WHEN 'SELL' THEN -r.filled_size END)"
        " FROM basket_target_execution_cycles c"
        " JOIN basket_target_order_intents i ON i.cycle_id=c.cycle_id"
        " JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE c.event_date>=? AND i.side='NO' AND i.live_submit_authorized=1"
        " AND i.order_action IN ('BUY','SELL')"
        " AND NOT (i.order_action='SELL'"
        "          AND COALESCE(json_extract(i.intent_json,'$.plan.source'),'')='account_positions')"
        " AND r.state IN ('resolved_filled','resolved_partial') AND r.filled_size>0"
        " GROUP BY c.station_id,c.event_date,i.bucket,i.token_id",
        (oldest_date,),
    ).fetchall()
    result = []
    conflicts = []
    for station, day, bucket, token, first_tz, last_tz, shares in rows:
        if not first_tz or first_tz != last_tz or float(shares or 0.0) < -1e-6:
            conflicts.append({"reason": "recycle_no_inventory_lineage_conflict", "station_id": str(station),
                              "event_date": str(day), "bucket": str(bucket), "token_id": str(token),
                              "net_shares": None if shares is None else round(float(shares), 6),
                              "settlement_timezones": [first_tz, last_tz]})
            continue
        if float(shares or 0.0) > 1e-6:
            result.append((str(station), str(day), str(bucket), str(token),
                           str(first_tz), float(shares)))
    _NO_LEDGER_CONFLICTS[:] = conflicts
    return result


def retire_old_price_orders(
    conn: sqlite3.Connection, *, now_utc: str, client: Any,
    max_attempts: int | None = MAX_SENDS_PER_CYCLE,
) -> dict[str, Any]:
    """Cancel the offers under the current price (0.99, then 0.998) before their
    shares can be reoffered.

    A cancel response alone does not prove that no shares remain at the
    exchange.  Only its explicit terminal order view closes the ledger;
    lookup silence and any fill race leave the old offer counted as open.
    """

    global _MIGRATION_CURSOR
    rows = conn.execute(
        "SELECT i.order_intent_id,i.requested_size,i.limit_price,"
        " r.exchange_order_id,COALESCE(r.filled_size,0),"
        " COALESCE(r.filled_notional_usdc,0)"
        " FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r"
        " ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_action='SELL' AND i.order_intent_id LIKE 'recycle998_%'"
        " AND i.limit_price<? AND i.state IN ('submitted','reconcile_required','submitting')"
        " AND (r.state IS NULL OR r.state NOT LIKE 'resolved_%')"
        " ORDER BY i.created_at,i.order_intent_id",
        (RECYCLE_PRICE - 1e-7,),
    ).fetchall()
    if not rows:
        return {"attempted": 0, "closed": 0, "pending": []}
    start = _MIGRATION_CURSOR % len(rows)
    rows = rows[start:] + rows[:start]
    if max_attempts is not None:
        rows = rows[:max(0, int(max_attempts))]
    _MIGRATION_CURSOR = start + len(rows)
    return _cancel_offers(conn, rows, now_utc=now_utc, client=client, cancel_key="recycle_0999_v6")


def retire_dead_leg_offers(
    conn: sqlite3.Connection, *, now_utc: str, client: Any,
    ws_root: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Cancel the resting offers on legs that have died (``_dead_leg`` on a
    book young enough to carry a tick), through the same terminal-view
    release as the old-price retirement: a cancel alone frees nothing."""

    rows = conn.execute(
        "SELECT i.order_intent_id,i.requested_size,i.limit_price,"
        " r.exchange_order_id,COALESCE(r.filled_size,0),"
        " COALESCE(r.filled_notional_usdc,0),i.decision_id,i.bucket,i.side"
        " FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r"
        " ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_action='SELL' AND i.order_intent_id LIKE 'recycle998_%'"
        " AND i.state IN ('submitted','reconcile_required','submitting')"
        " AND (r.state IS NULL OR r.state NOT LIKE 'resolved_%')"
        " ORDER BY i.created_at,i.order_intent_id",
    ).fetchall()
    prefix = f"{INTENT_PREFIX}_decision_"
    books: dict[tuple[str, str], dict[str, Any]] = {}
    dead = []
    for row in rows:
        decision = str(row[6] or "")
        if not decision.startswith(prefix):
            continue
        station_id, _sep, event_date = decision[len(prefix):].partition("_")
        key = (station_id, event_date)
        if key not in books:
            books[key] = _latest_book(conn, station_id=station_id, event_date=event_date,
                                      now_utc=now_utc, ws_root=ws_root)
        leg = books[key]["legs"].get(str(row[7])) or {}
        if leg.get("tick_size") is not None and _dead_leg(leg, str(row[8] or "YES")):
            dead.append(tuple(row[:6]))
    if not dead:
        return {"attempted": 0, "closed": 0, "pending": []}
    return _cancel_offers(conn, dead, now_utc=now_utc, client=client, cancel_key="dead_leg")


def _cancel_offers(
    conn: sqlite3.Connection, rows: Any, *, now_utc: str, client: Any, cancel_key: str,
) -> dict[str, Any]:
    """Cancel each offer and close it only on the exchange's terminal view."""

    result: dict[str, Any] = {"attempted": 0, "closed": 0, "pending": []}
    from .resting_ladder_lifecycle import _exchange_view, sync_resting_intent

    for intent_id, requested, price, order_id, filled, filled_notional in rows:
        entry: dict[str, Any] = {"order_intent_id": str(intent_id)}
        result["attempted"] += 1
        if not order_id:
            entry["reason"] = "exchange_order_id_unknown"
            result["pending"].append(entry)
            continue
        try:
            cancel = client.cancel_order({
                "order_id": str(order_id),
                "idempotency_key": f"{intent_id}:cancel:{cancel_key}",
            })
            if cancel.get("ok") is not True:
                entry["reason"] = "cancel_unconfirmed"
                result["pending"].append(entry)
                continue
            view = _exchange_view(client.lookup_exchange_order({"order_id": str(order_id)}))
            if view.get("status_inferred_from_not_found"):
                entry["reason"] = "lookup_not_found_after_cancel"
                result["pending"].append(entry)
                continue
            synced = sync_resting_intent(
                conn,
                intent={"order_intent_id": str(intent_id), "requested_size": float(requested),
                        "limit_price": float(price), "exchange_order_id": str(order_id),
                        "filled_size": float(filled), "filled_notional_usdc": float(filled_notional)},
                view=view, as_of_utc=now_utc, write=nullcontext,
            )
            if synced["state"] in {"cancelled", "expired", "filled"}:
                result["closed"] += 1
            else:
                entry["reason"] = str(synced.get("reason") or synced["state"])
                result["pending"].append(entry)
        except Exception as exc:  # The old offer remains reserved on ambiguity.
            entry["reason"] = f"{type(exc).__name__}:{exc}"[:160]
            result["pending"].append(entry)
    return result


def _read_account_positions() -> Mapping[str, Any]:
    """The account's whole position list off the venue's public data API
    (the same reader the board's holdings use)."""

    from .manual_basket_order import _data_api_positions, funder_address  # noqa: PLC0415

    user = funder_address()
    if not user:
        raise ValueError("funder_address_unknown")
    return _data_api_positions(user, timeout=ACCOUNT_POSITIONS_TIMEOUT_SECONDS)


def _account_positions(
    reader: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, float] | None:
    """token -> shares the account holds, read at most every few minutes.

    None when the list cannot be read: the ledger sources still plan, and
    nothing is inferred from a missing answer."""

    now = time.monotonic()
    if reader is None and now - _ACCOUNT_POSITIONS["at"] < ACCOUNT_POSITIONS_TTL_SECONDS:
        return _ACCOUNT_POSITIONS["positions"]
    try:
        rows = _read_account_positions() if reader is None else reader()
        positions = {}
        for token, row in (rows or {}).items():
            size = float((row or {}).get("size") or 0.0) if isinstance(row, Mapping) else float(row or 0.0)
            if str(token) and math.isfinite(size) and size > 1e-9:
                positions[str(token)] = size
        error = None
    except Exception as exc:  # noqa: BLE001 -- read again next window
        positions, error = None, f"{type(exc).__name__}:{exc}"[:160]
    if reader is None:
        _ACCOUNT_POSITIONS.update(at=now, positions=positions, error=error)
    return positions


def _token_legs(
    canonical_conn: sqlite3.Connection | None, oldest_date: str,
) -> dict[str, tuple[str, str, str, str]]:
    """token -> (station, event date, bucket, side) for the markets of the
    days this lane still sells, off the canonical registry (one row per
    outcome).  Only the columns stored before the registry JSON are read, so
    no overflow page is touched; cached for a few minutes."""

    now = time.monotonic()
    key = (oldest_date, id(canonical_conn))
    if now - _TOKEN_LEGS["at"] < TOKEN_LEGS_TTL_SECONDS and _TOKEN_LEGS["oldest"] == key:
        return _TOKEN_LEGS["legs"]
    legs: dict[str, tuple[str, str, str, str]] = {}
    if canonical_conn is not None and _has_table(canonical_conn, "reported_trajectory_market_registry"):
        for station, day, outcome, yes_token, no_token in canonical_conn.execute(
            "SELECT station_id, event_date, outcome, yes_token, no_token"
            " FROM reported_trajectory_market_registry WHERE event_date>=?",
            (oldest_date,),
        ):
            for token, side in ((yes_token, "YES"), (no_token, "NO")):
                if token:
                    legs[str(token)] = (str(station), str(day), str(outcome), side)
    _TOKEN_LEGS.update(at=now, oldest=key, legs=legs)
    return legs


def _account_plans(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    now_utc: str,
    oldest_date: str,
    planned_tokens: set[str],
    positions: Mapping[str, float],
    ws_root: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Charter 262: every held leg of the account no ledger plan covers --
    whatever lane, or no lane, bought it -- under the same venue rules."""

    plans: list[dict[str, Any]] = []
    legs = _token_legs(canonical_conn, oldest_date)
    books: dict[tuple[str, str], dict[str, Any]] = {}
    for token, account_shares in sorted(positions.items()):
        if token in planned_tokens or token not in legs:
            continue
        station_id, event_date, bucket, side = legs[token]
        if event_date < oldest_date:
            continue
        registry = _registry_rows(
            target_conn, canonical_conn, station_id=station_id, event_date=event_date, outcome=bucket,
        ).get(bucket) or {}
        if not registry or (registry.get("accepting_orders") is not None
                            and int(registry["accepting_orders"] or 0) != 1):
            continue
        # The token must still be this outcome's token on the newest row.
        if side == "YES" and registry.get("yes_token") != token:
            continue
        if side == "NO":
            try:
                if str((json.loads(registry["registry_json"]) or {}).get("no_token") or "") != token:
                    continue
            except (TypeError, ValueError):
                continue
        own = _own_sells(
            target_conn, station_id=station_id, event_date=event_date, bucket=bucket,
            counted=set(), side=side,
        )
        if own["failed"] >= MAX_FAILED_SENDS_PER_LEG or own["sends"] >= MAX_SENDS_PER_LEG or own["no_balance"]:
            continue
        # The account's own count already nets every sale; only the shares
        # this lane still offers are taken off.
        uncovered = _floor_cents(float(account_shares) - own["offered"])
        try:
            min_size = float(registry.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
        except (TypeError, ValueError):
            continue
        if uncovered + 1e-9 < max(min_size, 1e-6):
            continue
        key = (station_id, event_date)
        if key not in books:
            books[key] = _latest_book(target_conn, station_id=station_id, event_date=event_date, now_utc=now_utc,
                                      ws_root=ws_root)
        book = books[key]
        leg = book["legs"].get(bucket) or {}
        try:
            tick_size = float(leg.get("tick_size"))
            limit_price = recycle_price(tick_size)
        except (TypeError, ValueError):
            continue
        if _dead_leg(leg, side):
            _note_dead(station_id, event_date, bucket, side)
            continue
        timezone_name = _settlement_timezone(
            canonical_conn, station_id=station_id, event_date=event_date,
            book_timezone=book["settlement_timezone"],
        )
        if not timezone_name:
            continue
        best_bid = (float(leg.get("best_bid") or 0.0) if side == "YES"
                    else max(0.0, 1.0 - float(leg.get("best_ask") or 1.0)))
        plans.append({
            "schema": SCHEMA, "rule": RULE_VERSION, "source": "account_positions",
            "order_intent_id": _intent_id(
                station_id=station_id, event_date=event_date, bucket=bucket,
                token_id=token, now_utc=now_utc, side=side,
            ),
            "station_id": station_id, "event_date": event_date,
            "settlement_timezone": timezone_name, "bucket": bucket,
            "side": side, "token_id": token, "shares": uncovered,
            "limit_price": limit_price, "tick_size": tick_size,
            "min_size": min_size, "best_bid": best_bid,
            "held_shares": round(float(account_shares), 6),
            "offered_before_shares": round(own["offered"], 6),
            "sold_before_shares": 0.0,
        })
    return plans


def plan_certain_win_recycles(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    now_utc: str,
    account_reader: Callable[[], Mapping[str, Any]] | None = None,
    ws_root: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The 0.999 sells to place: for every held leg of a market still taking
    orders, the shares no open sell of this lane already offers and the
    inventory has not seen sold, when that is at least the venue minimum.
    Held legs come from the two ledgers and, under charter 262, from the
    account's own position list for everything the ledgers do not cover.

    Reads only.  Ordered by the leg's best bid, highest first, so the sends
    a cycle can make go to the legs closest to paying.
    """

    plans: list[dict[str, Any]] = []
    _DEAD_LEGS.clear()
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
        book = _latest_book(target_conn, station_id=station_id, event_date=event_date, now_utc=now_utc,
                            ws_root=ws_root)
        timezone_name = ""
        for bucket in sorted(held):
            own = _own_sells(
                target_conn, station_id=station_id, event_date=event_date, bucket=bucket, counted=counted,
            )
            if own["failed"] >= MAX_FAILED_SENDS_PER_LEG or own["sends"] >= MAX_SENDS_PER_LEG:
                continue
            if own["no_balance"]:
                # The venue refused a sale for want of shares: they left the
                # account outside this system (RKPK 27C 2026-09-18: bought
                # 34 at 0.03 on 09-16, sold by hand at 0.999 at 08:51Z, so
                # the ledger's 34 were not there to sell at 09:27Z).
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
            # 0.999 is legal only on a current fine-tick official WS book.
            # Registry metadata can lag a tick_size_change; never guess.
            tick = leg.get("tick_size")
            try:
                tick_size = float(tick)
                limit_price = recycle_price(tick_size)
            except (TypeError, ValueError):
                continue
            if _dead_leg(leg, "YES"):
                _note_dead(station_id, event_date, bucket, "YES")
                continue
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
                "side": "YES",
                "token_id": str(token_id),
                "shares": uncovered,
                "limit_price": limit_price,
                "tick_size": tick_size,
                "min_size": min_size,
                "best_bid": float(leg.get("best_bid") or 0.0),
                "held_shares": round(held[bucket], 6),
                "offered_before_shares": round(own["offered"], 6),
                "sold_before_shares": round(own["sold_unseen"], 6),
            })
    for station_id, event_date, bucket, token_id, timezone_name, held_shares in _held_no_legs(
        target_conn, oldest_date
    ):
        registry = _registry_rows(
            target_conn, canonical_conn, station_id=station_id, event_date=event_date,
            outcome=bucket,
        ).get(bucket) or {}
        if not registry or (registry.get("accepting_orders") is not None
                            and int(registry["accepting_orders"] or 0) != 1):
            continue
        try:
            market_no_token = str((json.loads(registry["registry_json"]) or {}).get("no_token") or "")
        except (TypeError, ValueError):
            market_no_token = ""
        if not market_no_token or market_no_token != token_id:
            continue
        # This ledger total already subtracts every resolved recycle sale.
        # Only unresolved offers need to be subtracted again.
        own = _own_sells(
            target_conn, station_id=station_id, event_date=event_date,
            bucket=bucket, counted=set(), side="NO",
        )
        if own["failed"] >= MAX_FAILED_SENDS_PER_LEG or own["sends"] >= MAX_SENDS_PER_LEG or own["no_balance"]:
            continue
        uncovered = _floor_cents(held_shares - own["offered"] - own["sold_unseen"])
        try:
            min_size = float(registry.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
        except (TypeError, ValueError):
            continue
        if uncovered + 1e-9 < max(min_size, 1e-6):
            continue
        book = _latest_book(target_conn, station_id=station_id, event_date=event_date, now_utc=now_utc,
                            ws_root=ws_root)
        leg = book["legs"].get(bucket) or {}
        try:
            tick_size = float(leg.get("tick_size"))
            limit_price = recycle_price(tick_size)
        except (TypeError, ValueError):
            continue
        if _dead_leg(leg, "NO"):
            _note_dead(station_id, event_date, bucket, "NO")
            continue
        yes_ask = float(leg.get("best_ask") or 1.0)
        plans.append({
            "schema": SCHEMA, "rule": RULE_VERSION,
            "order_intent_id": _intent_id(
                station_id=station_id, event_date=event_date, bucket=bucket,
                token_id=token_id, now_utc=now_utc, side="NO",
            ),
            "station_id": station_id, "event_date": event_date,
            "settlement_timezone": timezone_name, "bucket": bucket,
            "side": "NO", "token_id": token_id, "shares": uncovered,
            "limit_price": limit_price, "tick_size": tick_size,
            "min_size": min_size, "best_bid": max(0.0, 1.0 - yes_ask),
            "held_shares": round(held_shares, 6),
            "offered_before_shares": round(own["offered"], 6),
            "sold_before_shares": round(own["sold_unseen"], 6),
        })
    positions = _account_positions(account_reader)
    if positions:
        plans.extend(_account_plans(
            target_conn, canonical_conn, now_utc=now_utc, oldest_date=oldest_date,
            planned_tokens={str(plan["token_id"]) for plan in plans}, positions=positions,
            ws_root=ws_root,
        ))
    plans.sort(key=lambda plan: (-plan["best_bid"], plan["station_id"], plan["event_date"], plan["bucket"]))
    return plans


def execute_certain_win_recycles(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    now_utc: str,
    live_submit_authorized: bool,
    max_sends: int | None = MAX_SENDS_PER_CYCLE,
    position_reader: Callable[[str], Mapping[str, Any]] | None = None,
    cancellation_client: Any | None = None,
    account_reader: Callable[[], Mapping[str, Any]] | None = None,
    ws_root: Mapping[str, Any] | None | bool = True,
) -> dict[str, Any]:
    """Mint the intents, place the resting sells, record what came back --
    through the same sender and ledger path every other order uses.

    ``ws_root`` True reads the WS publisher's manifest once for the pass
    (None when unreadable: every leg then prices on its committed book)."""

    if ws_root is True:
        from .p1_tail_lane import _ws_manifest_root  # noqa: PLC0415 -- p1_tail_lane imports this module

        ws_root = _ws_manifest_root(None)
    elif ws_root is False:
        ws_root = None

    if (not live_submit_authorized or not exit_orders.live_intent_armed()
            or (Path(__file__).resolve().parents[1] / "data/live_paused.flag").exists()):
        return {"schema": "certain_win_recycle_receipt_v1", "rule": RULE_VERSION,
                "state": "live_execution_paused", "planned": 0, "sent": 0,
                "submitted": 0, "receipts": []}
    # Charter 197 amendment (user ruling 2026-09-22): no new order while the
    # egress reading is red.  Held before minting, and before the 0.99
    # offers are cancelled: this lane re-plans every cycle from what is
    # still unsold, so nothing is lost by waiting, and a minted-and-held
    # intent per leg per cycle through a fifteen-hour outage would be
    # thousands of rows saying the same thing.  Every plan it held is named
    # on the receipt with the reason.  Resting sells are untouched.
    egress_gate = egress_send_gate()
    if egress_gate.get("hold"):
        plans = plan_certain_win_recycles(target_conn, canonical_conn, now_utc=now_utc,
                                          account_reader=account_reader, ws_root=ws_root)
        return {
            "schema": "certain_win_recycle_receipt_v1",
            "rule": RULE_VERSION,
            "state": "egress_held",
            "planned": len(plans),
            "sent": 0,
            "deferred_to_next_cycle": len(plans),
            "submitted": 0,
            "egress_gate": dict(egress_gate),
            "receipts": [
                {"order_intent_id": plan["order_intent_id"], "state": "held",
                 "reason": EGRESS_HOLD_REASON, "station_id": plan["station_id"],
                 "event_date": plan["event_date"], "bucket": plan["bucket"],
                 "exchange_post_started": False}
                for plan in plans
            ],
        }
    if cancellation_client is None:
        from clob_order_service_client import ClobOrderServiceClient
        cancellation_client = ClobOrderServiceClient(timeout_seconds=TRANSPORT_TIMEOUT_SECONDS)
    migration = retire_old_price_orders(
        target_conn, now_utc=now_utc, client=cancellation_client,
    )
    dead_retire = retire_dead_leg_offers(
        target_conn, now_utc=now_utc, client=cancellation_client, ws_root=ws_root,
    )
    plans = plan_certain_win_recycles(target_conn, canonical_conn, now_utc=now_utc,
                                      account_reader=account_reader, ws_root=ws_root)
    read_position = position_reader or default_position_reader
    receipts: list[dict[str, Any]] = []
    submitted = 0
    sent = 0
    for plan in plans:
        if max_sends is not None and sent >= int(max_sends):
            break
        intent_id = plan["order_intent_id"]
        if target_conn.execute(
            "SELECT 1 FROM basket_target_order_intents WHERE order_intent_id=?", (intent_id,)
        ).fetchone():
            receipts.append({"order_intent_id": intent_id, "state": "already_minted"})
            continue
        token = plan["token_id"]
        if time.monotonic() - _ACCOUNT_SHORT.get(token, -1e18) < ACCOUNT_SHORT_RECHECK_SECONDS:
            receipts.append({"order_intent_id": intent_id, "state": "skipped",
                             "reason": "account_held_less_than_the_ledger_recently"})
            continue
        try:
            position = read_position(token)
        except Exception as exc:  # noqa: BLE001 -- the venue still checks the balance itself
            position = {"ok": False, "error": f"{type(exc).__name__}:{exc}"[:120]}
        if isinstance(position, Mapping) and position.get("ok") is True:
            available = _floor_cents(float(position.get("shares") or 0.0) - plan["offered_before_shares"])
            if available + 1e-9 < max(plan["min_size"], 1e-6):
                _ACCOUNT_SHORT[token] = time.monotonic()
                receipts.append({"order_intent_id": intent_id, "state": "skipped",
                                 "reason": "account_holds_less_than_the_ledger",
                                 "account_shares": position.get("shares"), "ledger_shares": plan["shares"]})
                continue
            if available + 1e-9 < plan["shares"]:
                plan = {**plan, "shares": available, "ledger_shares": plan["shares"],
                        "capped_by_account_shares": position.get("shares")}
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
            outcome_side=plan.get("side", "YES"),
        )
        if entry.get("exchange_post_started"):
            submitted += 1
        receipts.append(entry)
    return {
        "schema": "certain_win_recycle_receipt_v1",
        "rule": RULE_VERSION,
        "old_price_migration": migration,
        "dead_leg_retire": dead_retire,
        "dead_legs_not_offered": len(_DEAD_LEGS),
        "planned": len(plans),
        "sent": sent,
        "deferred_to_next_cycle": max(0, len(plans) - sent),
        "submitted": submitted,
        "egress_gate": dict(egress_gate),
        "no_ledger_conflicts": list(_NO_LEDGER_CONFLICTS),
        "ws_publisher_books": ws_root is not None,
        "account_positions": {
            "read": _ACCOUNT_POSITIONS["positions"] is not None,
            "held_tokens": len(_ACCOUNT_POSITIONS["positions"] or {}),
            "error": _ACCOUNT_POSITIONS["error"],
            "planned_from_account": sum(1 for plan in plans if plan.get("source") == "account_positions"),
        },
        "receipts": receipts,
    }


__all__ = [
    "RECYCLE_PRICE",
    "RULE_VERSION",
    "recycle_price",
    "plan_certain_win_recycles",
    "execute_certain_win_recycles",
]
