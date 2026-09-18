"""Sell a won position at 0.998 instead of waiting for settlement.

Operator ruling 2026-08-31: 确定赢的仓位提前 0.2% 折价回笼资金再周转 -- a
position that has already won pays 1.000 at settlement, hours or days from
now; a resting sell at 0.998 pays 0.2% less, now, and the freed capital
re-enters the budget the same cycle.  The 0.2% is the price of not waiting,
and it also covers the residual risk that a truth revision moves the high by
a fraction of a degree.

"Won" is measured, not judged: the station's local settlement day is closed,
and the settled truth landed inside a bucket this book holds.  Mid-day highs
do not qualify no matter how safe they look: an afternoon record can leave
the bucket, a closed day cannot.

This is a charter exception granted by the same ruling: the basket doctrine
forbids selling the M/A/F buckets, and this lane sells whichever bucket won.
The exception is bounded by the price: only at 0.998 (the "only after close"
of 2026-08-31 was dropped on 2026-09-18, below).  The limit is the bound, not post-only: in the minutes
between the day closing and the venue closing the market the bid already
stands at 0.999 (ZGGG 2026-09-16: 2,553 shares bid at 0.999 with the day
still open), so a post-only 0.998 would be refused for crossing, while a
plain limit at 0.998 fills against that bid and pays 0.1% instead of 0.2%.

Rewritten 2026-09-17 (user ruling: "打通0.998卖单").  From its first day the
lane had never placed an order, for three reasons stacked: it read the
inventory's holdings under ``filled_shares_by_bucket`` while every snapshot
writes ``filled_by_bucket``, so it never saw a position; the target
registry keeps only a few days of markets, so a day closed longer than that
had no token to sell; and its executor wrote the intent row and nothing
else -- no event, no reconciliation -- so a fill would never have reached
the inventory.  It now reads the holdings the snapshot writes, takes the won
bucket from the settlement label when one is written, else places the
settled truth on the station-day's ladder by the settler's rule, finds the
token and the
tick in the newest registry row (target or canonical), else the token from
the inventory's own token map, and sends through ``exit_orders.send_exit_sell``
like the other exit lane.

The window is short and measured: the truth lane labels the day 1-5 minutes
after it ends, the venue stops accepting orders 18-75 minutes after (37
station-days, 2026-09-14..16), so the lane has two or three cycles.  On
2026-09-16 the label came 63 minutes late for the Chinese stations and the
window was missed outright; the lane cannot fix that, only report it.

Widened 2026-09-18 (user ruling: 「直接挂单卖出啊，和收盘没关系吧」): the
close is not the condition, the price is.  A held leg the market already
bids at the recycle price is sold into that bid whether or not its day has
closed -- measured over 09-10..17, all 11 held legs that went on to win were
bid at >= 0.998 before their day ended (median 6.5 h before), and none of
the 50 that lost ever was.  A sale at 0.998 of a bucket that later loses is
0.998 received for a token that pays nothing, so the price bounds the risk
from both sides.  That sell is a GTD of a few minutes sized to the standing
depth, not an order left resting all day: a resting order keeps its
station-day's order state non-quiescent, and the market evidence pass then
stops renewing that day's quotes (RKSI 2026-09-18, a charter 144 sell).  The
close-and-truth path stays as the fallback for a leg the bid path did not
sell, resting (GTC) because a closed day's quotes serve nothing any more.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .candidate_window_evidence import _settled_bucket
from .contracts import content_sha256
from .exit_orders import send_exit_sell, wall_clock_utc
from .settlement_time import settlement_day_state

RECYCLE_PRICE = 0.998
STRATEGY_ID = "bayes_basket_target_v1"
INTENT_PREFIX = "recycle998"
SCHEMA = "certain_win_recycle_plan_v1"
RULE_VERSION = "certain_win_recycle_bid_or_settled_v3"
DEFAULT_MIN_ORDER_SHARES = 5.0
DEFAULT_TICK_SIZE = 0.01
# A day closed longer ago than this has settled on the venue or is about to;
# the tokens redeem at 1.000 and there is nothing to recycle.
MAX_DAYS_SINCE_CLOSE = 7.0
# Attempts per leg, per trigger.  The settled path retries a refusal over
# the minutes before the venue closes the market; the bid path over the
# hours a won bucket is bid at the recycle price before its day ends.
MAX_ATTEMPTS_PER_LEG = 3
MAX_BID_ATTEMPTS_PER_LEG = 6
# The two triggers (user ruling 2026-09-18, 「直接挂单卖出啊，和收盘没关系吧」).
TRIGGER_BID = "bid_at_recycle_price"
TRIGGER_SETTLED = "settled_truth_after_close"
# A bid older than this is not a price anyone will pay (the charter 144
# exit's bound): the book is renewed about every seven minutes a station.
MAX_BID_AGE_SECONDS = 900.0
# The bid path's order lives this long: it meets the standing bid at once,
# and if the book moved under it the venue retires it minutes later instead
# of it resting all day -- a resting order holds its station-day's order
# state non-quiescent, which stops that day's quotes from renewing (RKSI
# 2026-09-18, a charter 144 sell).  The venue's floor on a GTD is 180 s.
GTD_HORIZON_SECONDS = 300


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
    """0.998 on a 0.001 tick; the highest tick under it otherwise (0.99)."""

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
) -> dict[str, Any] | None:
    """The station-day's newest committed book, if young enough to sell on:
    per bucket the bid ladder (price desc), token, tick and minimum size."""

    if not _has_table(conn, "basket_official_ws_snapshot"):
        return None
    row = conn.execute(
        "SELECT observed_at, buckets_json, settlement_timezone FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=?"
        " ORDER BY observed_at DESC, sequence DESC LIMIT 1",
        (station_id, event_date, now_utc),
    ).fetchone()
    if row is None:
        return None
    try:
        age = (_utc(now_utc) - _utc(str(row[0]))).total_seconds()
        buckets = json.loads(row[1] or "[]") or []
    except (TypeError, ValueError):
        return None
    if age < 0.0 or age > MAX_BID_AGE_SECONDS:
        return None
    legs: dict[str, dict[str, Any]] = {}
    for item in buckets:
        if not isinstance(item, Mapping) or not item.get("token_id"):
            continue
        bids: list[tuple[float, float]] = []
        for level in item.get("bid_levels") or ():
            try:
                price, shares = float(level["price"]), float(level["shares"])
            except (KeyError, TypeError, ValueError):
                continue
            if price > 0.0 and shares > 0.0:
                bids.append((price, shares))
        bids.sort(key=lambda level: -level[0])
        legs[str(item.get("bucket"))] = {
            "token_id": str(item["token_id"]),
            "bids": bids,
            "tick_size": item.get("price_tick_size"),
            "min_size": item.get("min_order_size"),
        }
    return {
        "observed_at": str(row[0]),
        "age_seconds": round(age, 3),
        "settlement_timezone": str(row[2] or ""),
        "legs": legs,
    }


def _own_sells(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, bucket: str, counted: set[str],
) -> dict[str, Any]:
    """This lane's sells on the leg: whether one is still open, the shares
    sold that the inventory has not netted yet, and the attempts per trigger."""

    open_order = False
    sold_unseen = 0.0
    attempts = {TRIGGER_BID: 0, TRIGGER_SETTLED: 0}
    for intent_id, intent_state, intent_json, reconciliation_state, filled in conn.execute(
        "SELECT i.order_intent_id, i.state, i.intent_json, COALESCE(r.state, ''), COALESCE(r.filled_size, 0)"
        " FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_action='SELL' AND i.bucket=? AND i.decision_id=?",
        (bucket, f"{INTENT_PREFIX}_decision_{station_id}_{event_date}"),
    ):
        if str(intent_state) in {"prepared", "submitting", "submitted", "reconcile_required"} and not str(
            reconciliation_state
        ).startswith("resolved_"):
            open_order = True
        if str(intent_id) not in counted:
            sold_unseen += float(filled or 0.0)
        try:
            trigger = str(((json.loads(intent_json or "{}") or {}).get("plan") or {}).get("trigger") or "")
        except (TypeError, ValueError):
            trigger = ""
        attempts[TRIGGER_BID if trigger == TRIGGER_BID else TRIGGER_SETTLED] += 1
    return {"open": open_order, "sold_unseen": sold_unseen, "attempts": attempts}


def _intent_id(*, station_id: str, event_date: str, bucket: str, token_id: str, trigger: str, now_utc: str) -> str:
    minute = _utc(now_utc).replace(second=0, microsecond=0)
    return INTENT_PREFIX + "_" + content_sha256({
        "schema": SCHEMA, "station_id": station_id, "event_date": event_date,
        "bucket": bucket, "token_id": token_id, "trigger": trigger,
        "minute": minute.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    })[:40]


def _bid_plan(
    *, station_id: str, event_date: str, bucket: str, leg: Mapping[str, Any], book: Mapping[str, Any],
    remaining: float, sold_unseen: float, now_utc: str,
) -> dict[str, Any] | None:
    """A sell into the standing bid, when the market already pays the
    recycle price for this bucket -- whether or not its day has closed.

    Measured 2026-09-18 over 09-10..17: all 11 held legs that went on to win
    were bid at >= 0.998 before their day ended, a median 6.5 h before it
    (ZUCK the latest, 0.38 h); none of the 50 held legs that lost ever was.
    The bid is the market saying the bucket has won; waiting for the day to
    close and the truth to land spent those hours holding capital, and on
    09-16 missed the venue's close outright.  Sized to the depth standing at
    or above the price, so the order meets the bid whole rather than resting
    its remainder."""

    if not book.get("settlement_timezone"):
        return None
    tick = leg.get("tick_size") or DEFAULT_TICK_SIZE
    price = recycle_price(tick)
    depth = sum(shares for level_price, shares in leg["bids"] if level_price >= price - 1e-9)
    if depth <= 0.0:
        return None
    try:
        min_size = float(leg.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
    except (TypeError, ValueError):
        min_size = DEFAULT_MIN_ORDER_SHARES
    size = _floor_cents(min(remaining, depth))
    if size + 1e-9 < max(min_size, 1e-6):
        return None
    try:
        tick_size = float(tick)
    except (TypeError, ValueError):
        tick_size = DEFAULT_TICK_SIZE
    return {
        "schema": SCHEMA,
        "rule": RULE_VERSION,
        "trigger": TRIGGER_BID,
        "order_intent_id": _intent_id(
            station_id=station_id, event_date=event_date, bucket=bucket,
            token_id=str(leg["token_id"]), trigger=TRIGGER_BID, now_utc=now_utc,
        ),
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": str(book["settlement_timezone"]),
        "bucket": bucket,
        "token_id": str(leg["token_id"]),
        "shares": size,
        "limit_price": price,
        "tick_size": tick_size,
        "best_bid": leg["bids"][0][0],
        "depth_at_price": round(depth, 6),
        "book_observed_at": str(book["observed_at"]),
        "book_age_seconds": book["age_seconds"],
        "sold_before_shares": round(sold_unseen, 6),
    }


def _settled_win(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    station_id: str,
    event_date: str,
    now_utc: str,
) -> dict[str, Any] | None:
    """The bucket the station-day's closed day settled in, or None while the
    day is open, the truth has not landed, or it closed over a week ago."""

    # Charter 100: the settled truth, never the trajectory lane's running
    # bound.  The label table names the bucket outright but is written
    # 25-235 minutes after the truth (measured 2026-09-16), later than
    # the venue closes the market; so the truth row is placed on the
    # station-day's ladder by the settler's own rule when no label has
    # been written yet -- the same computation the label lane runs.
    label = None
    if _has_table(target_conn, "basket_station_day_settlement_label"):
        label = target_conn.execute(
            "SELECT settled_bucket FROM basket_station_day_settlement_label"
            " WHERE station_id=? AND event_date=?"
            " ORDER BY truth_revision_no DESC LIMIT 1",
            (station_id, event_date),
        ).fetchone()
    truth = target_conn.execute(
        "SELECT t.official_tmax_c, t.settlement_timezone, t.observation_ids_json"
        " FROM calibration_station_daily_high_truth AS t"
        " WHERE t.station_id=? AND t.local_date=? AND t.source_lane='AWC'"
        " AND t.qc_status='ACCEPTED'"
        " AND t.authority_scope IN"
        " ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')"
        " ORDER BY t.revision_no DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if truth is None or truth[0] is None:
        return None
    high_c = float(truth[0])
    timezone_name = str(truth[1] or "")
    if not timezone_name and canonical_conn is not None:
        bound = canonical_conn.execute(
            "SELECT settlement_timezone"
            " FROM reported_trajectory_canonical_station_day_bounds"
            " WHERE station_id=? AND event_date=?",
            (station_id, event_date),
        ).fetchone()
        timezone_name = str((bound or [""])[0] or "")
    if not timezone_name:
        return None
    try:
        day = settlement_day_state(
            event_date, as_of_utc=now_utc, settlement_timezone=timezone_name,
        )
    except Exception:  # noqa: BLE001
        return None
    if day["state"] != "closed":
        return None
    if (_utc(now_utc) - _utc(day["target_day_end_utc"])).total_seconds() > MAX_DAYS_SINCE_CLOSE * 86400.0:
        return None
    won: str | None = str(label[0]) if label and label[0] else None
    won_by = "settlement_label"
    if won is None and _has_table(target_conn, "basket_probability_bridge_history"):
        head = target_conn.execute(
            "SELECT history_id FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " ORDER BY decision_at_utc DESC LIMIT 1",
            (station_id, event_date),
        ).fetchone()
        if head:
            ladder = target_conn.execute(
                "SELECT bucket, ordinal, lower_c, upper_c"
                " FROM basket_probability_bridge_bucket_probability"
                " WHERE history_id=? ORDER BY ordinal",
                (head[0],),
            ).fetchall()
            won = _settled_bucket(ladder, high_c) if ladder else None
            won_by = "truth_on_ladder"
    if won is None:
        return None
    return {
        "won": won, "won_by": won_by, "high_c": high_c,
        "settlement_timezone": timezone_name,
        "truth_observation_ids_json": str(truth[2] or ""),
    }


def _settled_plan(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    inventory: Mapping[str, Any],
    station_id: str,
    event_date: str,
    bucket: str,
    settled: Mapping[str, Any],
    remaining: float,
    sold_unseen: float,
    now_utc: str,
) -> dict[str, Any] | None:
    """A resting sell of a leg its closed day settled in, for whatever the
    bid path did not sell before the close.  Resting costs nothing here: a
    closed day's quotes are not renewed for anything any more."""

    # Only now the registry, and only the won bucket: it is the one read
    # here whose cost grows with every refresh the day was ever given.
    registry = _registry_rows(
        target_conn, canonical_conn, station_id=station_id, event_date=event_date, outcome=bucket,
    )
    row = registry.get(bucket) or {}
    token_id = row.get("yes_token") or str(
        (inventory.get("token_ids_by_bucket") or {}).get(bucket) or ""
    )
    if not token_id:
        return None
    if row.get("accepting_orders") is not None and int(row["accepting_orders"] or 0) != 1:
        # The venue has closed the market: the tokens redeem, nothing sells.
        return None
    try:
        min_size = float(row.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
    except (TypeError, ValueError):
        min_size = DEFAULT_MIN_ORDER_SHARES
    if remaining + 1e-9 < max(min_size, 1e-6):
        return None
    tick = row.get("tick_size") or DEFAULT_TICK_SIZE
    return {
        "schema": SCHEMA,
        "rule": RULE_VERSION,
        "trigger": TRIGGER_SETTLED,
        "order_intent_id": _intent_id(
            station_id=station_id, event_date=event_date, bucket=bucket,
            token_id=str(token_id), trigger=TRIGGER_SETTLED, now_utc=now_utc,
        ),
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": str(settled["settlement_timezone"]),
        "bucket": bucket,
        "token_id": str(token_id),
        "shares": remaining,
        "limit_price": recycle_price(tick),
        "tick_size": float(tick) if isinstance(tick, (int, float)) else DEFAULT_TICK_SIZE,
        "observed_max_c": settled["high_c"],
        "won_by": settled["won_by"],
        "sold_before_shares": round(sold_unseen, 6),
        "truth_observation_ids_json": settled["truth_observation_ids_json"],
    }


def plan_certain_win_recycles(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    now_utc: str,
) -> list[dict[str, Any]]:
    """Every held bucket whose day has closed on a settled truth inside it.

    Reads only; returns the sells the executor should place, each with the
    reasons a leg was or was not one under ``readings`` of the executor's
    receipt.  A sell already open on the leg, or shares this lane already
    sold, are read off its own intents.
    """

    plans: list[dict[str, Any]] = []
    # The newest complete inventory of each station-day, off the covering
    # index (see the 2026-09-09/14 measurements in git history: a bare
    # column beside MAX() re-read every row).  Only the station-days that
    # can still have closed inside MAX_DAYS_SINCE_CLOSE (a day ends at most
    # about a day and a half after its date, UTC), and CROSS JOIN so the
    # planner seeks each newest row from g instead of scanning every
    # complete snapshot to meet g (measured 2026-09-18: 1,952 station-days,
    # 3.6 s -> 495 station-days, 0.07-0.3 s, the same rows).
    oldest_date = (
        _utc(now_utc) - timedelta(days=MAX_DAYS_SINCE_CLOSE + 2.0)
    ).date().isoformat()
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
        # The snapshot writes ``filled_by_bucket``; ``filled_shares_by_bucket``
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
        # The inventory already nets every sale it has seen (SELL fills count
        # negative); only this lane's fills it has not seen yet are taken off
        # again, by intent id against its lineage.  Taking every sale off
        # twice left a partly sold leg reading zero once the inventory caught
        # up, and its remainder was never offered again.
        counted = {
            str(item.get("order_intent_id") or "")
            for item in inventory.get("lineage") or ()
            if isinstance(item, Mapping)
        }
        book = _latest_book(target_conn, station_id=station_id, event_date=event_date, now_utc=now_utc)
        settled: dict[str, Any] | None = None
        settled_read = False
        for bucket in sorted(held):
            own = _own_sells(
                target_conn, station_id=station_id, event_date=event_date, bucket=bucket, counted=counted,
            )
            if own["open"]:
                continue
            remaining = _floor_cents(held[bucket] - own["sold_unseen"])
            if remaining <= 0.0:
                continue
            leg = (book or {}).get("legs", {}).get(bucket)
            if leg is not None and own["attempts"][TRIGGER_BID] < MAX_BID_ATTEMPTS_PER_LEG:
                plan = _bid_plan(
                    station_id=station_id, event_date=event_date, bucket=bucket, leg=leg, book=book,
                    remaining=remaining, sold_unseen=own["sold_unseen"], now_utc=now_utc,
                )
                if plan is not None:
                    plans.append(plan)
                    continue
            if not settled_read:
                settled = _settled_win(target_conn, canonical_conn, station_id=station_id,
                                       event_date=event_date, now_utc=now_utc)
                settled_read = True
            if settled is None or settled["won"] != bucket:
                continue
            if own["attempts"][TRIGGER_SETTLED] >= MAX_ATTEMPTS_PER_LEG:
                # The venue closes the market 18-75 minutes after the day ends
                # (measured 2026-09-14..16); a send it refused is retried on the
                # next cycles, not every cycle for a week.
                continue
            plan = _settled_plan(
                target_conn, canonical_conn, inventory=inventory, station_id=station_id,
                event_date=event_date, bucket=bucket, settled=settled, remaining=remaining,
                sold_unseen=own["sold_unseen"], now_utc=now_utc,
            )
            if plan is not None:
                plans.append(plan)
    return plans


def execute_certain_win_recycles(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    now_utc: str,
    live_submit_authorized: bool,
) -> dict[str, Any]:
    """Mint the intents, send the sells, record what came back -- through
    the same sender and ledger path every other order uses.  A sell into the
    bid is a GTD that expires minutes after it is sent; a sell after the
    close rests (GTC) until the venue closes the market."""

    plans = plan_certain_win_recycles(target_conn, canonical_conn, now_utc=now_utc)
    receipts: list[dict[str, Any]] = []
    submitted = 0
    for plan in plans:
        intent_id = plan["order_intent_id"]
        if target_conn.execute(
            "SELECT 1 FROM basket_target_order_intents WHERE order_intent_id=?", (intent_id,)
        ).fetchone():
            receipts.append({"order_intent_id": intent_id, "state": "already_minted"})
            continue
        into_bid = plan.get("trigger") == TRIGGER_BID
        # The expiry counts from the moment this order is sent (the charter
        # 144 exit's lesson: one stamp per batch ran into the venue's floor).
        expiration = (
            int(_utc(wall_clock_utc()).timestamp()) + GTD_HORIZON_SECONDS if into_bid else None
        )
        entry = send_exit_sell(
            target_conn, lane=INTENT_PREFIX, intent_id=intent_id,
            station_id=plan["station_id"], event_date=plan["event_date"],
            settlement_timezone=plan["settlement_timezone"], bucket=plan["bucket"],
            token_id=plan["token_id"], price=plan["limit_price"], shares=plan["shares"],
            order_type="GTD" if into_bid else "GTC", post_only=False, tick_size=plan["tick_size"],
            expiration=expiration,
            intent_body={"schema": SCHEMA, "rule": RULE_VERSION, "strategy_id": STRATEGY_ID,
                         "execution_candidate": "EXIT_GTD_LIMIT" if into_bid else "EXIT_GTC_LIMIT",
                         "plan": plan},
            transport=transport, live_submit_authorized=live_submit_authorized,
        )
        if entry.get("exchange_post_started"):
            submitted += 1
        receipts.append(entry)
    return {
        "schema": "certain_win_recycle_receipt_v1",
        "rule": RULE_VERSION,
        "planned": len(plans),
        "planned_into_bid": sum(1 for plan in plans if plan.get("trigger") == TRIGGER_BID),
        "submitted": submitted,
        "receipts": receipts,
    }


__all__ = [
    "RECYCLE_PRICE",
    "RULE_VERSION",
    "TRIGGER_BID",
    "TRIGGER_SETTLED",
    "recycle_price",
    "plan_certain_win_recycles",
    "execute_certain_win_recycles",
]
