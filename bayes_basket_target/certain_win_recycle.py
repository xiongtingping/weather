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
The exception is bounded by the price: only at 0.998, only after close, only
the full held size.  The limit is the bound, not post-only: in the minutes
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
from .exit_orders import lane_sold_and_open, send_exit_sell, wall_clock_utc
from .settlement_time import settlement_day_state

RECYCLE_PRICE = 0.998
STRATEGY_ID = "bayes_basket_target_v1"
INTENT_PREFIX = "recycle998"
SCHEMA = "certain_win_recycle_plan_v1"
RULE_VERSION = "certain_win_recycle_settled_label_v2"
DEFAULT_MIN_ORDER_SHARES = 5.0
DEFAULT_TICK_SIZE = 0.01
# A day closed longer ago than this has settled on the venue or is about to;
# the tokens redeem at 1.000 and there is nothing to recycle.
MAX_DAYS_SINCE_CLOSE = 7.0
MAX_ATTEMPTS_PER_LEG = 3


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
            continue
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
            continue
        try:
            day = settlement_day_state(
                event_date, as_of_utc=now_utc, settlement_timezone=timezone_name,
            )
        except Exception:  # noqa: BLE001
            continue
        if day["state"] != "closed":
            continue
        if (_utc(now_utc) - _utc(day["target_day_end_utc"])).total_seconds() > MAX_DAYS_SINCE_CLOSE * 86400.0:
            continue
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
        if won is None or won not in held:
            continue
        # Only now the registry, and only the won bucket: it is the one read
        # here whose cost grows with every refresh the day was ever given.
        registry = _registry_rows(
            target_conn, canonical_conn, station_id=station_id, event_date=event_date, outcome=won,
        )
        row = registry.get(won) or {}
        token_id = row.get("yes_token") or str(
            (inventory.get("token_ids_by_bucket") or {}).get(won) or ""
        )
        if not token_id:
            continue
        if row.get("accepting_orders") is not None and int(row["accepting_orders"] or 0) != 1:
            # The venue has closed the market: the tokens redeem, nothing sells.
            continue
        sold, open_order = lane_sold_and_open(
            target_conn, lane=INTENT_PREFIX, station_id=station_id, event_date=event_date, bucket=won,
        )
        if open_order:
            continue
        attempts = int(target_conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
            " WHERE order_action='SELL' AND bucket=? AND decision_id=?",
            (won, f"{INTENT_PREFIX}_decision_{station_id}_{event_date}"),
        ).fetchone()[0])
        if attempts >= MAX_ATTEMPTS_PER_LEG:
            # The venue closes the market 18-75 minutes after the day ends
            # (measured 2026-09-14..16); a send it refused is retried on the
            # next cycles, not every cycle for a week.
            continue
        try:
            min_size = float(row.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
        except (TypeError, ValueError):
            min_size = DEFAULT_MIN_ORDER_SHARES
        remaining = math.floor((float(held[won]) - sold) * 100.0) / 100.0
        if remaining + 1e-9 < max(min_size, 1e-6):
            continue
        tick = row.get("tick_size") or DEFAULT_TICK_SIZE
        minute = _utc(now_utc).replace(second=0, microsecond=0)
        intent_id = INTENT_PREFIX + "_" + content_sha256({
            "schema": SCHEMA, "station_id": station_id, "event_date": event_date,
            "bucket": won, "token_id": token_id,
            "minute": minute.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        })[:40]
        plans.append({
            "schema": SCHEMA,
            "rule": RULE_VERSION,
            "order_intent_id": intent_id,
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "bucket": won,
            "token_id": str(token_id),
            "shares": remaining,
            "limit_price": recycle_price(tick),
            "tick_size": float(tick) if isinstance(tick, (int, float)) else DEFAULT_TICK_SIZE,
            "observed_max_c": high_c,
            "won_by": won_by,
            "sold_before_shares": round(sold, 6),
            "truth_observation_ids_json": str(truth[2] or ""),
        })
    return plans


def execute_certain_win_recycles(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    now_utc: str,
    live_submit_authorized: bool,
) -> dict[str, Any]:
    """Mint the intents, place the resting sells, record what came back --
    through the same sender and ledger path every other order uses."""

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
