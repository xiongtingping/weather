"""远尾做空线 -- the market's own far tails, bought NO as a taker (charter 234).

User rulings 2026-09-28: 「上线：只按市场自己的价格结构做空远尾桶」 and
「上线：H1 的纯市场版。并测试持续监控，直至下单成功！」.  The model plays no
part: this lane reads the book and the settlement clock, nothing else.

Measured before the ruling (``docs/research/p0_differential_edge_20260927``,
H1's market-only arm -- a report-only arm of a pre-registered study, both
halves positive): on P0 decisions more than 36 hours out, event days
08-15..09-25, a bucket the market itself priced at or under 5%, three or more
rungs from its own modal bucket, with a YES bid of at least 2 cents, settled
1.35% of the time against a mean bid of about 2.5 cents.  NO bought at the NO
ask (1 - YES bid) returned +1.18% [+0.56%, +1.72%] before fees.  Polymarket
charges weather markets a taker fee of 0.05 x p x (1 - p) per share, about
0.12 cents at a NO price of 0.975, which leaves about +1.0%.  The same study's
model-picked tails (weather p <= 1%) made +0.55% with the halves disagreeing:
where the model and a 5-cent bid disagree the bid was right; the overpricing
lives in the far tails the market itself puts there.

* **window**: the station-day's newest decision is P0 and more than
  ``min_hours_to_settlement`` hours remain on the settlement clock;
* **book**: the freshest ladder -- the WS publisher's own, else the
  committed snapshot -- at most ``send_book_fresh_seconds`` old, whose
  ``market_reference_v1`` vector is complete;
* **leg**: a bucket with q <= ``max_market_price``, at least
  ``min_rungs_from_mode`` rungs from the market's modal bucket in ladder
  order, and a YES best bid >= ``min_yes_bid``;
* **order**: FOK buy of the NO token at the NO ask, 1 - YES best bid, sized
  up to ``stake_usdc`` and capped by the depth resting at that bid (the P1
  tail lane's headroom), at least the venue minimum;
* **limits**: a bucket is done once any send fills, and gets at most
  ``max_attempts_per_bucket`` sends that reached the venue and
  ``max_orders_per_bucket`` orders minted in all (a send that never reached
  the venue waits ``unsent_retry_seconds`` before the next); at most
  ``max_legs_per_station_day`` buckets a station-day; this lane's fills --
  and sends whose outcome is not known -- cost at most
  ``max_daily_cost_usdc`` a UTC day.

Nothing is read or written while ``data/live_paused.flag`` is up or the
arming flag is down: the gateway would refuse every send, and a refusal
minted each cycle is a row each cycle.

Held to settlement.  Every bucket the rule fires on is recorded -- sent,
filled, killed or refused -- in ``tail_short_lane_evaluation``; a repeated
refusal for the same reason is written once.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .basket_kernel.valuation_v1 import market_reference_v1
from .certain_win_recycle import _registry_rows
from .contracts import content_sha256
from . import lane_orders
from .exit_orders import Transport
from .lane_orders import send_lane_buy
from .live_station_scope import admission as live_station_admission
from .p1_tail_lane import (
    DEPTH_HEADROOM_FRACTION,
    _latest_snapshot,
    _sendable_shares,
    _ws_manifest_root,
    live_ws_snapshot,
    open_buy_orders_on_station_day,
)
from .settlement_time import hours_to_settlement, settlement_day_state
from .strategy_lanes import LANE_TAIL_SHORT, TAIL_SHORT_DEFAULT_SETTINGS, lane_settings, opening_stages
from .tiebreak_lane import _cash_refusal, settle_stuck_lane_cycles

LANE = LANE_TAIL_SHORT
LANE_TITLE = "远尾做空线"
STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "tail_short_lane_plan_v1"
RULE_V1 = "market_far_tail_no_taker_v1"
RULE_VERSIONS = {RULE_V1: "tail_short_lane_v1"}
DEFAULT_RULE = RULE_V1
EXECUTION_CANDIDATE = "TAKER_FOK"
SIDE = "NO"
EVALUATION_TABLE = "tail_short_lane_evaluation"
DEFAULT_LOOKBACK_DAYS = 1
DEFAULT_MIN_ORDER_SHARES = 5.0
INTENT_PREFIX = "tailshort"
#: The operator's stop; the gateway reads it at every submission.
PAUSE_FLAG_PATH = Path(__file__).resolve().parents[1] / "data" / "live_paused.flag"
#: The grid the gateway signs on.  The payload carries no tick, so the
#: gateway signs at its default (``PM_CLOB_DEFAULT_TICK_SIZE``, 0.01), and the
#: venue moves a market to a 0.001 tick once its price is under 0.04 or over
#: 0.96 -- exactly where these legs are.  A NO price off the cent grid is one
#: the gateway would round to a price nobody quoted.
SIGNING_TICK = 0.01

Admission = Callable[[str, sqlite3.Connection], Mapping[str, Any]]

EVALUATION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {EVALUATION_TABLE} ("
    " evaluation_id TEXT PRIMARY KEY,"
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " bucket TEXT NOT NULL,"
    " evaluated_at_utc TEXT NOT NULL,"
    " rule_version TEXT NOT NULL,"
    " settlement_timezone TEXT,"
    " hours_to_settlement REAL,"
    " snapshot_id TEXT,"
    " book_source TEXT,"
    " book_observed_at TEXT,"
    " book_age_seconds REAL,"
    " market_q REAL,"
    " market_mode_bucket TEXT,"
    " rungs_from_mode INTEGER,"
    " yes_bid REAL,"
    " yes_bid_size REAL,"
    " yes_ask REAL,"
    " no_price REAL,"
    " shares REAL,"
    " cost_usdc REAL,"
    " fee_usdc_est REAL,"
    " refusal TEXT,"
    " order_intent_id TEXT,"
    " sent INTEGER NOT NULL DEFAULT 0,"
    " filled_shares REAL NOT NULL DEFAULT 0,"
    " send_json TEXT"
    ")"
)
EVALUATION_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_scope_idx"
    f" ON {EVALUATION_TABLE}(station_id, event_date, bucket)"
)


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    conn.execute(EVALUATION_DDL)
    conn.execute(EVALUATION_INDEX_DDL)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def taker_fee_usdc(price: float, shares: float, rate: float) -> float:
    """Polymarket's taker fee: shares x rate x p x (1 - p), in USDC."""

    return round(float(shares) * float(rate) * float(price) * (1.0 - float(price)), 6)


def _levels(raw: Any) -> list[tuple[float, float]]:
    out = []
    for level in raw or ():
        try:
            price = float(level.get("price"))
            shares = float(level.get("shares") or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(price) and 0.0 < price < 1.0 and shares > 0.0:
            out.append((price, shares))
    return out


def rests_on_another_bucket(order: Mapping[str, Any], bucket: str) -> bool:
    """Whether an open BUY is another lane's acknowledged resting rung on a
    different bucket -- the one open order that does not hold a take back.

    Charter 166's rule is "never queued behind a resting BUY of any lane": a
    take on the token our own rung rests on queues behind that rung.  Charter
    249 rests a YES rung on the market's modal bucket through the very window
    this lane takes NO on the far tails -- another token, three or more rungs
    away -- and read station-day-wide the rule would have stopped this lane
    on every station-day the Proper lane is posting.  An order of this lane,
    one whose outcome is not known (``prepared`` / ``submitting`` /
    ``reconcile_required``), a taker order and anything on the same bucket
    hold the take back as before.
    """

    return (
        order.get("post_only") is True
        and str(order.get("state")) == "submitted"
        and str(order.get("lane")) != LANE
        and str(order.get("bucket")) != str(bucket)
    )


def yes_quote(book_row: Mapping[str, Any] | None) -> dict[str, Any]:
    """The YES book's best bid with the shares resting there, and the best ask."""

    bids = _levels((book_row or {}).get("bid_levels"))
    asks = _levels((book_row or {}).get("ask_levels"))
    best_bid = max((p for p, _s in bids), default=None)
    return {
        "bid": best_bid,
        "bid_size": (sum(s for p, s in bids if abs(p - best_bid) < 1e-9) if best_bid is not None else None),
        "ask": (min(p for p, _s in asks) if asks and (book_row or {}).get("ask_available", True) else None),
    }


def far_tail_legs(
    book_buckets: Sequence[Mapping[str, Any]],
    *,
    max_market_price: float,
    min_rungs_from_mode: int,
    min_yes_bid: float,
) -> dict[str, Any]:
    """The rule on one ladder: the market's vector, its mode, the legs it fires."""

    market = market_reference_v1(book_buckets)
    if market.get("state") != "complete":
        return {"state": "market_incomplete", "reason": market.get("reason"), "legs": []}
    q = {str(k): float(v) for k, v in market["vector"].items()}
    rows = {str(r.get("bucket")): r for r in book_buckets}
    order = [str(r.get("bucket")) for r in sorted(
        book_buckets, key=lambda r: (r.get("ordinal") is None, r.get("ordinal") or 0))]
    order = [b for b in order if b in q]
    if not order:
        return {"state": "market_incomplete", "reason": "no_buckets", "legs": []}
    mode_index = max(range(len(order)), key=lambda i: (q[order[i]], -i))
    legs = []
    for index, bucket in enumerate(order):
        rungs = abs(index - mode_index)
        if q[bucket] > max_market_price or rungs < int(min_rungs_from_mode):
            continue
        quote = yes_quote(rows.get(bucket))
        if quote["bid"] is None or quote["bid"] < float(min_yes_bid) - 1e-12:
            continue
        legs.append({"bucket": bucket, "q": q[bucket], "rungs_from_mode": rungs, **quote})
    return {"state": "complete", "q": q, "mode_bucket": order[mode_index], "order": order, "legs": legs}


def no_take(
    yes_bid: float,
    yes_bid_size: float | None,
    stake_usdc: float,
    *,
    tick_size: Any,
    min_shares: float,
) -> dict[str, Any]:
    """The NO order a stake buys at the NO ask, within the depth at the bid.

    Buying NO takes the resting YES bid: the NO ask is 1 - bid and the shares
    on offer are the bid's.  The P1 tail lane's headroom applies -- the book
    is always a little older than the send, and a FOK the level cannot fill
    is killed whole.
    """

    out: dict[str, Any] = {"price": None, "shares": None, "cost_usdc": None, "refusal": None}
    try:
        tick = float(tick_size) if tick_size not in (None, "") else SIGNING_TICK
    except (TypeError, ValueError):
        tick = SIGNING_TICK
    tick = max(tick, SIGNING_TICK)
    price = round(1.0 - float(yes_bid), 6)
    if abs(price / tick - round(price / tick)) > 1e-6:
        # A bid off the signing grid (0.025 on a 0.001-tick market) has a NO
        # ask we cannot send; rounding either way is a price nobody quoted.
        out["refusal"] = "no_price_off_tick"
        return out
    if not 0.0 < price < 1.0:
        out["refusal"] = "no_price_invalid"
        return out
    affordable = float(stake_usdc) / price
    if yes_bid_size is not None:
        usable = min(float(yes_bid_size) * DEPTH_HEADROOM_FRACTION, float(yes_bid_size) - 1.0)
        affordable = min(affordable, max(usable, 0.0))
    shares = _sendable_shares(price, affordable)
    if shares is None or shares < float(min_shares):
        thin = yes_bid_size is not None and float(yes_bid_size) < float(stake_usdc) / price
        out["refusal"] = (f"thin_bid:{float(yes_bid_size):.2f}" if thin
                          else f"below_min_size:{(shares or 0.0):.2f}<{float(min_shares):.2f}")
        return out
    out.update(price=price, shares=shares, cost_usdc=round(price * shares, 6))
    return out


# ---------------------------------------------------------------------------
# Reading the store.


def station_days_in_window(
    conn: sqlite3.Connection, *, now_utc: str, lookback_days: int, stages: frozenset[str],
    only_station_day: tuple[str, str] | None = None,
) -> list[dict[str, str]]:
    """Station-days whose newest decision is in an opening stage."""

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='basket_policy_training_fact'"
    ).fetchone() is None:
        return []
    floor = (_utc(now_utc) - timedelta(days=int(lookback_days))).date().isoformat()
    restriction = " AND station_id=? AND event_date=?" if only_station_day else ""
    rows = conn.execute(
        "SELECT f.station_id, f.event_date, f.stage, f.settlement_timezone"
        " FROM basket_policy_training_fact f"
        " JOIN (SELECT station_id, event_date, MAX(decision_at_utc) AS newest"
        "       FROM basket_policy_training_fact WHERE event_date >= ?" + restriction +
        "       GROUP BY station_id, event_date) n"
        "   ON n.station_id = f.station_id AND n.event_date = f.event_date"
        "  AND n.newest = f.decision_at_utc"
        " ORDER BY f.decision_at_utc DESC, f.created_at_utc DESC, f.rowid DESC",
        (floor, *only_station_day) if only_station_day else (floor,),
    ).fetchall()
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for station, day, stage, tz in rows:
        seen.setdefault((str(station), str(day)), {
            "station_id": str(station), "event_date": str(day), "stage": str(stage),
            "settlement_timezone": str(tz),
        })
    return [v for v in seen.values() if v["stage"] in stages]


def _book(
    conn: sqlite3.Connection,
    root: Mapping[str, Any] | None,
    *,
    station_id: str,
    event_date: str,
    now_utc: str,
    fresh_seconds: float,
    live: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """The freshest ladder: the WS publisher's own, else the committed one.

    Read in that order and no further than needed: this lane looks at every
    P0 station-day each cycle, and the committed snapshot is the slower read.
    """

    now = _utc(now_utc)

    def fresh(book: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            age = (now - _utc(book["observed_at"])).total_seconds()
        except (TypeError, ValueError, KeyError):
            return None
        return {**book, "age_seconds": round(age, 1)} if age <= float(fresh_seconds) else None

    seen = False
    if live:
        # H1 trades future listing days (>36 h to settlement).  The default
        # started-day view excludes exactly those scopes, leaving this lane
        # waiting for the slower committed snapshot even with a fresh WS
        # publisher ladder in hand.
        book = live_ws_snapshot(
            conn, root, station_id=station_id, event_date=event_date,
            now_utc=now_utc, include_listing_days=True,
        )
        if book is not None:
            seen = True
            taken = fresh(book)
            if taken is not None:
                return taken, None
    committed = _latest_snapshot(conn, station_id, event_date)
    if committed is not None:
        seen = True
        taken = fresh({**committed, "book_source": "committed_snapshot"})
        if taken is not None:
            return taken, None
    return None, ("book_stale" if seen else "book_unavailable")


def _new_history() -> dict[str, Any]:
    return {"filled": 0.0, "ledger_filled": False, "sends": 0, "orders": 0, "unsent_at": None,
            "refusals": set()}


def _held(entry: Mapping[str, Any]) -> bool:
    return float(entry["filled"]) > 0.0 or bool(entry["ledger_filled"])


def bucket_history(conn: sqlite3.Connection, station_id: str, event_date: str) -> dict[str, dict[str, Any]]:
    """Per bucket: shares filled, sends that reached the venue, orders minted,
    the newest order that never reached it, refusals written."""

    out: dict[str, dict[str, Any]] = {}
    for bucket, filled, sent, refusal, intent, at in conn.execute(
        f"SELECT bucket, filled_shares, sent, refusal, order_intent_id, evaluated_at_utc"
        f" FROM {EVALUATION_TABLE} WHERE station_id=? AND event_date=?",
        (station_id, event_date),
    ):
        entry = out.setdefault(str(bucket), _new_history())
        entry["filled"] += float(filled or 0.0)
        entry["sends"] += int(sent or 0)
        if intent:
            entry["orders"] += 1
            if not sent and not float(filled or 0.0) > 0.0 and str(at) > str(entry["unsent_at"] or ""):
                entry["unsent_at"] = str(at)
        if refusal:
            entry["refusals"].add(str(refusal))
    # A send answered late -- reconciled to filled after this table recorded
    # it unmatched -- holds its bucket all the same.
    for (bucket,) in conn.execute(
        f"SELECT DISTINCT e.bucket FROM {EVALUATION_TABLE} AS e"
        " JOIN basket_target_order_intents AS i ON i.order_intent_id = e.order_intent_id"
        " WHERE e.station_id=? AND e.event_date=? AND i.state='filled'",
        (station_id, event_date),
    ):
        out.setdefault(str(bucket), _new_history())["ledger_filled"] = True
    return out


def lane_cost_today(conn: sqlite3.Connection, *, now_utc: str) -> float:
    """USDC this lane spent, or may have spent, on the UTC day: a fill at what
    it filled, a killed FOK at nothing, a send whose outcome is unknown at its
    full cost."""

    day = _utc(now_utc).date().isoformat()
    row = conn.execute(
        f"SELECT COALESCE(SUM(CASE"
        f"  WHEN filled_shares > 0 THEN filled_shares * no_price + COALESCE(fee_usdc_est, 0)"
        f"  WHEN json_extract(send_json, '$.event_type') = 'EXPIRED' THEN 0"
        f"  ELSE COALESCE(cost_usdc, 0) + COALESCE(fee_usdc_est, 0) END), 0)"
        f" FROM {EVALUATION_TABLE} WHERE sent=1 AND substr(evaluated_at_utc,1,10)=?",
        (day,),
    ).fetchone()
    return float(row[0] or 0.0)


def _record(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(row)
    body.setdefault("sent", 0)
    body.setdefault("filled_shares", 0.0)
    body["evaluation_id"] = f"{INTENT_PREFIX}_eval_" + content_sha256({
        k: body.get(k) for k in ("station_id", "event_date", "bucket", "evaluated_at_utc", "refusal", "order_intent_id")
    })[:32]
    columns = ",".join(body)
    conn.execute(
        f"INSERT OR IGNORE INTO {EVALUATION_TABLE}({columns}) VALUES({','.join('?' for _ in body)})",
        tuple(body.values()),
    )
    return body


# ---------------------------------------------------------------------------
# Planning and sending.


def plan_tail_short(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    now_utc: str,
    cfg: Mapping[str, Any],
    gate: Admission,
    manifest_root: Callable[[], Mapping[str, Any] | None],
    only_station_day: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Every station-day in the window: read the book, fire the rule, plan the takes."""

    rule_version = RULE_VERSIONS[str(cfg["rule"])]
    stages = opening_stages(cfg)
    now = _utc(now_utc)
    min_hours = float(cfg["min_hours_to_settlement"])
    stake = float(cfg["stake_usdc"])
    fee_rate = float(cfg.get("taker_fee_rate", 0.0))
    budget_left = float(cfg["max_daily_cost_usdc"]) - lane_cost_today(target_conn, now_utc=now_utc)
    plans: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for scope in station_days_in_window(
        target_conn, now_utc=now_utc, lookback_days=int(cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS)),
        stages=stages, only_station_day=only_station_day,
    ):
        station, day, tz = scope["station_id"], scope["event_date"], scope["settlement_timezone"]
        try:
            if settlement_day_state(day, as_of_utc=now_utc, settlement_timezone=tz).get("state") == "closed":
                skip("settlement_day_closed")
                continue
            hours = float(hours_to_settlement(day, as_of_utc=now_utc, settlement_timezone=tz))
        except Exception:  # noqa: BLE001 -- a day whose clock cannot be read is not traded
            skip("settlement_day_unknown")
            continue
        if not hours > min_hours:
            skip("inside_window")
            continue
        book, book_refusal = _book(
            target_conn, manifest_root(), station_id=station, event_date=day, now_utc=now_utc,
            fresh_seconds=float(cfg["send_book_fresh_seconds"]), live=cfg.get("live_ws_book", True) is True,
        )
        if book is None:
            skip(str(book_refusal))
            continue
        reading = far_tail_legs(
            list(book["buckets"]), max_market_price=float(cfg["max_market_price"]),
            min_rungs_from_mode=int(cfg["min_rungs_from_mode"]), min_yes_bid=float(cfg["min_yes_bid"]),
        )
        if reading["state"] != "complete":
            skip(f"market_incomplete:{reading.get('reason')}")
            continue
        if not reading["legs"]:
            skip("no_leg")
            continue
        history = bucket_history(target_conn, station, day)
        held = sum(1 for h in history.values() if _held(h))
        verdict = gate(station, target_conn)
        station_refusal = None
        open_orders: list[dict[str, Any]] = []
        if verdict.get("admitted") is not True:
            station_refusal = f"station_not_admitted:{verdict.get('reason') or 'refused'}"
        else:
            open_orders = open_buy_orders_on_station_day(target_conn, station, day)
        registry = None
        for leg in reading["legs"]:
            bucket = leg["bucket"]
            past = history.get(bucket) or _new_history()
            base = {
                "station_id": station, "event_date": day, "bucket": bucket, "evaluated_at_utc": now_utc,
                "rule_version": rule_version, "settlement_timezone": tz, "hours_to_settlement": round(hours, 3),
                "snapshot_id": book.get("snapshot_id"), "book_source": book.get("book_source"),
                "book_observed_at": book.get("observed_at"), "book_age_seconds": book.get("age_seconds"),
                "market_q": round(leg["q"], 6), "market_mode_bucket": reading["mode_bucket"],
                "rungs_from_mode": leg["rungs_from_mode"], "yes_bid": leg["bid"], "yes_bid_size": leg["bid_size"],
                "yes_ask": leg["ask"],
            }

            def refuse(reason: str, **extra: Any) -> None:
                if reason in past["refusals"]:
                    return
                past["refusals"].add(reason)
                evaluations.append(_record(target_conn, {**base, **extra, "refusal": reason}))

            if _held(past):
                continue
            if past["sends"] >= int(cfg["max_attempts_per_bucket"]):
                refuse("max_attempts_reached")
                continue
            if past["orders"] >= int(cfg["max_orders_per_bucket"]):
                refuse("max_orders_reached")
                continue
            if past["unsent_at"] and (now - _utc(past["unsent_at"])).total_seconds() < float(
                cfg["unsent_retry_seconds"]
            ):
                skip("unsent_retry_wait")
                continue
            if station_refusal:
                refuse(station_refusal)
                continue
            blocker = next((o for o in open_orders if not rests_on_another_bucket(o, bucket)), None)
            if blocker is not None:
                refuse(f"open_order:{blocker['lane'] or 'unknown'}:{blocker['state']}")
                continue
            if held >= int(cfg["max_legs_per_station_day"]):
                refuse("max_legs_per_station_day")
                continue
            if registry is None:
                registry = _registry_rows(target_conn, canonical_conn, station_id=station, event_date=day)
            market_row = registry.get(bucket) or {}
            try:
                no_token = str((json.loads(market_row.get("registry_json") or "{}") or {}).get("no_token") or "")
            except ValueError:
                no_token = ""
            if not no_token:
                refuse("registry_no_token_missing")
                continue
            if not bool(market_row.get("accepting_orders")):
                refuse("market_not_accepting_orders")
                continue
            take = no_take(
                leg["bid"], leg["bid_size"], stake, tick_size=market_row.get("tick_size"),
                min_shares=float(market_row.get("min_size") or DEFAULT_MIN_ORDER_SHARES),
            )
            if take["refusal"]:
                refuse(take["refusal"])
                continue
            fee = taker_fee_usdc(take["price"], take["shares"], fee_rate)
            if take["cost_usdc"] + fee > budget_left + 1e-9:
                refuse(f"daily_budget:{max(budget_left, 0.0):.2f}")
                continue
            cash_refusal = _cash_refusal(target_conn, now_utc=now_utc, needed=float(take["cost_usdc"]) + fee)
            if cash_refusal:
                refuse(cash_refusal)
                continue
            plan = {
                "schema": SCHEMA, "rule": rule_version, "rule_name": str(cfg["rule"]), "lane": LANE,
                "title": LANE_TITLE, "station_id": station, "event_date": day, "settlement_timezone": tz,
                "stage": scope["stage"], "bucket": bucket, "side": SIDE, "token_id": no_token,
                "execution_candidate": EXECUTION_CANDIDATE, "order_type": "FOK", "post_only": False,
                "limit_price": take["price"], "fok_price_cap": take["price"], "shares": take["shares"],
                "cost_usdc": take["cost_usdc"], "fee_usdc_est": fee, "taker_fee_rate": fee_rate,
                "tick_size": market_row.get("tick_size"), "min_size": market_row.get("min_size"),
                "stake_usdc": stake, "attempt": past["sends"] + 1, **{k: base[k] for k in (
                    "hours_to_settlement", "snapshot_id", "book_source", "book_observed_at",
                    "book_age_seconds", "market_q", "market_mode_bucket", "rungs_from_mode",
                    "yes_bid", "yes_bid_size", "yes_ask")},
            }
            plan["order_intent_id"] = f"{INTENT_PREFIX}_{content_sha256({'plan': plan, 'at': now_utc})[:32]}"
            evaluations.append(_record(target_conn, {
                **base, "no_price": take["price"], "shares": take["shares"], "cost_usdc": take["cost_usdc"],
                "fee_usdc_est": fee, "refusal": None, "order_intent_id": plan["order_intent_id"],
            }))
            budget_left -= take["cost_usdc"] + fee
            held += 1
            plans.append(plan)
    return {"plans": plans, "evaluations": evaluations, "skipped": skipped, "rule_version": rule_version}


def execute_tail_short(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Any = None,
    only_station_day: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Fire the rule on every station-day in the window and send the takes."""

    stopped = ("live_paused" if PAUSE_FLAG_PATH.exists()
               else "live_intent_disarmed" if not lane_orders.live_intent_armed() else None)
    if stopped:
        return {"schema": "tail_short_lane_receipt_v1", "lane": LANE, "title": LANE_TITLE,
                "state": stopped, "evaluated": 0, "planned": 0, "submitted": 0, "filled": 0}
    ensure_lane_schema(target_conn)
    cfg = {**TAIL_SHORT_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    if str(cfg.get("rule")) not in RULE_VERSIONS:
        raise ValueError(f"tail_short_rule_unknown:{cfg.get('rule')}")
    # A cycle this lane left open (a send that crashed mid-way) holds the
    # station-day's quotes shut; settle it before anything else is read.
    cycles_settled = settle_stuck_lane_cycles(target_conn)
    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    live = cfg.get("live_ws_book", True) is True
    manifest: dict[str, Any] = {}

    def manifest_root() -> Mapping[str, Any] | None:
        if "root" not in manifest:
            manifest["root"] = _ws_manifest_root(ws_manifest_path) if live else None
        return manifest["root"]

    planned = plan_tail_short(target_conn, canonical_conn, now_utc=now_utc, cfg=cfg, gate=gate,
                              manifest_root=manifest_root, only_station_day=only_station_day)
    receipts: list[dict[str, Any]] = []
    submitted = filled = 0
    for plan in planned["plans"]:
        details = {
            "schema": "tail_short_lane_decision_v1", "lane": LANE, "title": LANE_TITLE, "rule": plan["rule"],
            "decision_signature": {"stage": plan["stage"], "lane": LANE, "rule": plan["rule"]},
            "forward_evidence_binding": {
                "decision_at_utc": now_utc, "probability_stage": plan["stage"],
                "probability_decision_at_utc": None, "probability_artifact_sha256": None,
                "probability_envelope_id": None, "ws_snapshot_id": plan["snapshot_id"],
            },
            "plan": plan,
        }
        entry = send_lane_buy(
            target_conn, lane=LANE, intent_id=plan["order_intent_id"],
            station_id=plan["station_id"], event_date=plan["event_date"],
            settlement_timezone=plan["settlement_timezone"], bucket=plan["bucket"],
            token_id=plan["token_id"], price=plan["limit_price"], shares=plan["shares"],
            side=SIDE, execution_candidate=EXECUTION_CANDIDATE,
            fok_price_cap=float(plan["fok_price_cap"]),
            tick_size=(float(plan["tick_size"]) if plan.get("tick_size") not in (None, "") else None),
            intent_body={"schema": SCHEMA, "rule": plan["rule"], "strategy_id": STRATEGY_ID,
                         "order_type": plan["order_type"], "post_only": plan["post_only"], "plan": plan},
            transport=transport, live_submit_authorized=live_submit_authorized,
            decision_details=details,
        )
        sent = bool(entry.get("exchange_post_started"))
        matched = float(entry.get("matched_size") or 0.0)
        submitted += int(sent)
        filled += int(matched > 0.0)
        outcome = None if matched > 0.0 else (
            f"send:{entry.get('reason') or entry.get('state')}" if not sent
            else f"send:{entry.get('event_type') or 'killed'}:{entry.get('reason') or ''}".rstrip(":")
        )
        send = {"state": entry.get("state"), "event_type": entry.get("event_type"), "reason": entry.get("reason"),
                "matched_size": matched, "exchange_post_started": sent, "at": now_utc}
        target_conn.execute(
            f"UPDATE {EVALUATION_TABLE} SET sent=?, filled_shares=?, refusal=?, send_json=?"
            " WHERE order_intent_id=?",
            (int(sent), matched, outcome, json.dumps(send, ensure_ascii=False, sort_keys=True),
             plan["order_intent_id"]),
        )
        receipts.append({"station_id": plan["station_id"], "event_date": plan["event_date"],
                         "bucket": plan["bucket"], "price": plan["limit_price"], "shares": plan["shares"],
                         "order_intent_id": plan["order_intent_id"], **send})
    return {
        "schema": "tail_short_lane_receipt_v1",
        "lane": LANE,
        "title": LANE_TITLE,
        "rule": planned["rule_version"],
        "settings": cfg,
        "evaluated": len(planned["evaluations"]),
        "planned": len(planned["plans"]),
        "submitted": submitted,
        "filled": filled,
        "skipped": planned["skipped"],
        "refusals": sorted({str(e["refusal"]) for e in planned["evaluations"] if e.get("refusal")}),
        "cycles_settled": cycles_settled,
        "receipts": receipts,
    }


__all__ = [
    "DEFAULT_RULE",
    "EVALUATION_TABLE",
    "EXECUTION_CANDIDATE",
    "LANE",
    "LANE_TITLE",
    "RULE_V1",
    "RULE_VERSIONS",
    "SIDE",
    "bucket_history",
    "ensure_lane_schema",
    "execute_tail_short",
    "far_tail_legs",
    "lane_cost_today",
    "no_take",
    "plan_tail_short",
    "rests_on_another_bucket",
    "station_days_in_window",
    "taker_fee_usdc",
    "yes_quote",
]
