"""Proper 下注线 -- log-proper betting on the whole ladder (charter 231).

User rulings 2026-09-26: 「调整下注规则」 (arXiv 2607.06166, LogScore), then
「实盘上线，p＝天气模型」; v2 the same day: 「吃单改为挂单，post-only，best
bid+1，挂单周期最短，重新挂单，直至成交，但是挂单价格要符合下注规则」,
「proper线还是按照>36h，仅p0阶段下单」 and 「预算提高到最低要求」.  Not a
basket, not Kelly, not a value clamp: the pair of probability vectors
``(p, q)`` generates the position vector directly.

* ``p`` -- the weather model: the served envelope the station-day's P0
  FIRST decision read (the calibrated NWP ensemble vector, charter 224's
  floor included).  Not the L4 posterior (charter 230): the ruling names the
  weather model.
* ``q`` -- the market: the normalised mid ladder of one fresh book
  (``market_reference_v1``).
* LogScore, so the proper position is ``s_k = log(p_k / q_k)`` on every
  bucket.  ``s > 0`` is the YES direction, ``s < 0`` the NO direction; both
  are traded.
* A leg is a post-only GTD rung at the best bid plus one tick of its own
  side's book (charter 94's ``bid_anchored_ladder``, clamped under the ask).
  The NO book is the YES book mirrored: NO bid ``1 - YES ask``, NO ask
  ``1 - YES bid``.  The rung must obey the betting rule every time it is
  priced: YES at a price under ``p_k``, NO at a price under ``1 - p_k``
  (the pasted ``p_k < 1 - bid`` for NO was corrected before the ruling).
* Size: ``x_k = lambda * |s_k|`` shares, one ``lambda`` for the station-day,
  set at the MINIMUM the venue allows for the whole position -- the smallest
  leg at the venue's minimum order (five shares), every other leg in exact
  ratio.  When the free cash cannot carry that, the legs are taken in order
  of ``|s|`` and the longest prefix whose own minimum the cash can carry is
  kept (``budget_capped_by_cash`` names the shortfall).
* Window: P0 first decisions only, with more than 36 hours to the
  settlement clock (``settlement_time.hours_to_settlement``, the axis the
  L4 verdict found P0 ``>=36h`` on).  Each leg is posted at the venue's
  minimum horizon and, while it is unfilled, re-posted on the book that
  stands -- while the station-day is still P0, still more than 36 hours out
  and the rung's price still obeys the rule -- until the leg's target is
  filled.  Held to settlement.
* Attribution: the evaluation row keeps ``p``, ``q``, ``s`` and
  ``D = KL(q || p)``; ``scripts/proper_bet_attribution.py`` closes
  ``Pi = Delta S + D - L`` on settlement.

v1 (FOK at the worst in-band ask level, 2026-09-26 11:52Z-) never sent an
order: every first decision it saw was stale, closed or barred.

Measured before the ruling (docs/research/log_proper_betting_20260926,
sections 8, 9, 12, 13): two-sided Log shares at the ask ROI -3.0%
[-4.4, -1.7]; resting at the bid, the fills that come carry adverse
selection (a filled YES rung's bucket hit 3.0% against 10.1% for the
unfilled).  The lane goes live on the ruling; every first decision is
recorded, fired or not.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any

from .account_equity import (
    capture_account_equity,
    load_account_equity,
    table_present as account_equity_table_present,
)
from .basket_kernel.valuation_v1 import market_reference_v1
from .certain_win_recycle import _registry_rows
from .contracts import content_sha256
from .delta_v2_runtime import bid_anchored_ladder
from .exit_orders import Transport
from .lane_orders import send_lane_buy
from .live_station_scope import admission as live_station_admission
from .materialization import floor_probabilities
from .p1_tail_lane import _latest_snapshot, _ws_manifest_root, live_ws_snapshot
from .settlement_time import hours_to_settlement, settlement_day_state
from .strategy_lanes import (
    LANE_PROPER_BET,
    PROPER_BET_DEFAULT_SETTINGS,
    lane_settings,
    opening_stages,
)
from .tiebreak_lane import _budget_dial, resting_order_fields, stage_first_decisions

LANE = LANE_PROPER_BET
LANE_TITLE = "Proper 下注线"
STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "proper_bet_lane_plan_v2"
RULE_MAKER_V2 = "log_proper_maker_bid_plus_one_min_horizon_v2"
RULE_VERSIONS = {RULE_MAKER_V2: "proper_bet_lane_v2"}
DEFAULT_RULE = RULE_MAKER_V2
EXECUTION_CANDIDATE = "MAKER"
EVALUATION_TABLE = "proper_bet_lane_evaluation"
INTENT_PREFIX = "proper"
DEFAULT_MIN_ORDER_SHARES = 5.0
DEFAULT_LOOKBACK_DAYS = 3
SHARE_PRECISION = 2
OPEN_STATES = ("prepared", "submitting", "submitted", "reconcile_required")

#: The two directions a leg can take, as the ledger spells the outcome leg.
SIDE_YES = "YES"
SIDE_NO = "NO"

PLAN_ACTIVE = "active"
PLAN_DONE = "done"
PLAN_CLOSED = "closed"

EVALUATION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {EVALUATION_TABLE} ("
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " stage TEXT NOT NULL,"
    " decision_at_utc TEXT NOT NULL,"
    " settlement_timezone TEXT,"
    " fact_id TEXT,"
    " envelope_id TEXT,"
    " envelope_sha256 TEXT,"
    " snapshot_id TEXT,"
    " book_observed_at TEXT,"
    " book_source TEXT,"
    " rule_version TEXT NOT NULL,"
    " p_json TEXT,"
    " q_json TEXT,"
    " s_json TEXT,"
    " divergence_kl REAL,"
    " stake_usdc REAL,"
    " budget_usdc REAL,"
    " lambda_shares REAL,"
    " buckets_json TEXT,"
    " planned_legs INTEGER NOT NULL DEFAULT 0,"
    " planned_cost_usdc REAL NOT NULL DEFAULT 0,"
    " refusal TEXT,"
    " sends_json TEXT,"
    " evaluated_at_utc TEXT NOT NULL,"
    " PRIMARY KEY (station_id, event_date, stage)"
    ")"
)
#: Added with v2: the plan the rung pass keeps posting, and where it stands.
PLAN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("hours_to_settlement", "REAL"),
    ("plan_json", "TEXT"),
    ("plan_state", "TEXT"),
    ("plan_state_reason", "TEXT"),
    ("legs_state_json", "TEXT"),
    ("passes", "INTEGER"),
    ("last_pass_at_utc", "TEXT"),
)

Admission = Callable[[str, sqlite3.Connection], Mapping[str, Any]]


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    conn.execute(EVALUATION_DDL)
    have = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({EVALUATION_TABLE})")}
    for name, kind in PLAN_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE {EVALUATION_TABLE} ADD COLUMN {name} {kind}")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_plan_state_idx"
        f" ON {EVALUATION_TABLE}(plan_state, event_date)"
    )


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _levels(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for level in raw or ():
        try:
            price = float(level.get("price"))
            size = float(level.get("shares") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if size > 0.0 and math.isfinite(price) and 0.0 < price < 1.0:
            out.append((price, size))
    return out


def _tick_decimals(tick: float) -> int:
    return max(0, int(round(-math.log10(tick)))) if tick > 0 else 6


# ---------------------------------------------------------------------------
# The rule: (p, q) -> s -> the rung each leg rests at -> lambda.  Pure.


def proper_position(
    p: Mapping[str, float], q: Mapping[str, float]
) -> dict[str, float]:
    """``s_k = log(p_k / q_k)`` -- the LogScore proper position."""

    return {bucket: math.log(float(p[bucket]) / float(q[bucket])) for bucket in q}


def divergence_kl(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """``D = KL(q || p) = sum q_k log(q_k / p_k)``, the Log rule's divergence."""

    return sum(
        float(q[b]) * math.log(float(q[b]) / float(p[b]))
        for b in q if float(q[b]) > 0.0
    )


def side_book(book_row: Mapping[str, Any] | None, side: str, tick: float) -> tuple[float | None, float | None]:
    """The best bid and ask of the leg's own token.

    YES is the book as quoted.  NO is the same book mirrored -- the CLOB
    matches a NO bid against a YES bid (and a NO ask against a YES ask)
    through its complement -- so NO bid ``1 - YES ask``, NO ask
    ``1 - YES bid``, rounded to the tick.
    """

    row = book_row or {}
    asks = sorted(_levels(row.get("ask_levels"))) if bool(row.get("ask_available", True)) else []
    bids = sorted(_levels(row.get("bid_levels")), reverse=True)
    if not bids:
        try:
            raw = float(row.get("bid_price") or 0.0)
        except (TypeError, ValueError):
            raw = 0.0
        if raw > 0.0:
            bids = [(raw, 0.0)]
    yes_bid = bids[0][0] if bids else None
    yes_ask = asks[0][0] if asks else None
    if side == SIDE_YES:
        return yes_bid, yes_ask
    digits = _tick_decimals(tick)
    no_bid = round(1.0 - yes_ask, digits) if yes_ask is not None else None
    no_ask = round(1.0 - yes_bid, digits) if yes_bid is not None else None
    return no_bid, no_ask


def rung_price(bid: float | None, ask: float | None, tick: float) -> float | None:
    """Best bid plus one tick, clamped under the ask -- the production rung."""

    if bid is None or ask is None or not (tick > 0.0):
        return None
    rungs = bid_anchored_ladder(bid=float(bid), ask=float(ask), tick=float(tick))
    if not rungs:
        return None
    price = float(rungs[-1])
    return price if 0.0 < price < 1.0 else None


def leg_quote(
    bucket: str, p: float, s: float, book_row: Mapping[str, Any] | None, tick: float,
) -> dict[str, Any]:
    """One bucket's leg: its side from ``s``, its rung, and the rule's verdict.

    The rule: a YES rung only under ``p``, a NO rung only under ``1 - p``.
    """

    out: dict[str, Any] = {"bucket": bucket, "p": p, "s": s, "side": None, "price": None,
                           "bid": None, "ask": None, "reason": None}
    if s > 0.0:
        side, claim = SIDE_YES, p
    elif s < 0.0:
        side, claim = SIDE_NO, 1.0 - p
    else:
        out["reason"] = "no_position"
        return out
    out["side"] = side
    bid, ask = side_book(book_row, side, tick)
    out["bid"], out["ask"] = bid, ask
    price = rung_price(bid, ask, tick)
    if price is None:
        out["reason"] = "maker_anchor_unavailable"
        return out
    out["price"] = price
    if not price < claim - 1e-12:
        out["reason"] = f"price_breaks_rule:{price:.4f}>={claim:.4f}"
    return out


def size_minimum(
    legs: list[dict[str, Any]],
    *,
    cash_usdc: float | None,
    min_shares: Mapping[str, float],
) -> dict[str, Any]:
    """``x_k = lambda * |s_k|`` at the smallest ``lambda`` the venue allows.

    For a set of legs the minimum is ``lambda = max_k m_k / |s_k|`` (``m_k``
    the leg's venue minimum): the smallest leg sits exactly at its minimum,
    every other in ratio.  The whole position is taken when the cash carries
    it; otherwise the longest ``|s|``-ordered prefix whose own minimum the
    cash carries (adding a smaller ``|s|`` only raises ``lambda`` and the
    cost, so the affordable prefixes are an initial run).
    """

    ranked = sorted(
        (leg for leg in legs if leg.get("side") and leg.get("price") and not leg.get("reason")),
        key=lambda leg: (-abs(float(leg["s"])), str(leg["bucket"])),
    )

    def sized(prefix: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]], float]:
        lam = max(float(min_shares.get(leg["bucket"], DEFAULT_MIN_ORDER_SHARES)) / abs(float(leg["s"]))
                  for leg in prefix)
        out = []
        scale = 10 ** SHARE_PRECISION
        for leg in prefix:
            floor = float(min_shares.get(leg["bucket"], DEFAULT_MIN_ORDER_SHARES))
            shares = max(floor, math.floor(lam * abs(float(leg["s"])) * scale + 1e-6) / scale)
            out.append({**leg, "target_shares": shares,
                        "cost_usdc": round(shares * float(leg["price"]), 6)})
        return lam, out, sum(leg["cost_usdc"] for leg in out)

    if not ranked:
        return {"lambda_shares": None, "legs": [], "dropped": {}, "need_full_usdc": None,
                "budget_usdc": 0.0, "capped_by_cash": False}
    lam_full, full, need_full = sized(ranked)
    if cash_usdc is None or need_full <= float(cash_usdc) + 1e-9:
        return {"lambda_shares": lam_full, "legs": full, "dropped": {}, "need_full_usdc": need_full,
                "budget_usdc": need_full, "capped_by_cash": False}
    best: tuple[float, list[dict[str, Any]], float] | None = None
    for n in range(1, len(ranked) + 1):
        candidate = sized(ranked[:n])
        if candidate[2] <= float(cash_usdc) + 1e-9:
            best = candidate
        else:
            break
    kept = {leg["bucket"] for leg in (best[1] if best else [])}
    dropped = {str(leg["bucket"]): "budget_capped_by_cash" for leg in ranked if leg["bucket"] not in kept}
    return {
        "lambda_shares": best[0] if best else None,
        "legs": best[1] if best else [],
        "dropped": dropped,
        "need_full_usdc": need_full,
        "budget_usdc": best[2] if best else 0.0,
        "capped_by_cash": True,
    }


def evaluate_ladder(
    probabilities: Mapping[str, float],
    book_buckets: list[Mapping[str, Any]],
    *,
    cash_usdc: float | None,
    min_shares: Mapping[str, float],
    ticks: Mapping[str, float],
) -> dict[str, Any]:
    """The whole rule on one ladder: ``p``, ``q``, ``s``, ``D``, the rungs, lambda."""

    market = market_reference_v1(book_buckets)
    if market.get("state") != "complete":
        return {"refusal": "market_incomplete", "market_reason": market.get("reason")}
    q = {str(k): float(v) for k, v in market["vector"].items()}
    raw_p = {str(k): float(v) for k, v in (probabilities or {}).items()}
    if set(raw_p) != set(q):
        return {
            "refusal": "ladder_mismatch",
            "model_only": sorted(set(raw_p) - set(q)),
            "market_only": sorted(set(q) - set(raw_p)),
        }
    total = sum(raw_p.values())
    if not (total > 0.0 and math.isfinite(total)):
        return {"refusal": "model_vector_invalid"}
    p, floored = floor_probabilities({b: v / total for b, v in raw_p.items()})
    s = proper_position(p, q)
    divergence = divergence_kl(p, q)
    rows = {str(r.get("bucket")): r for r in book_buckets}
    order = sorted(q, key=lambda b: rows.get(b, {}).get("ordinal", 0))
    quotes = [
        leg_quote(b, p[b], s[b], rows.get(b), float(ticks.get(b) or rows.get(b, {}).get("price_tick_size") or 0.01))
        for b in order
    ]
    sizing = size_minimum(quotes, cash_usdc=cash_usdc, min_shares=min_shares)
    out = {
        "refusal": None,
        "p": p, "q": q, "s": s, "divergence_kl": divergence,
        "p_floored_buckets": floored,
        "mid_sum": market.get("mid_sum"),
        "quotes": quotes,
        **sizing,
    }
    if not any(leg.get("price") and not leg.get("reason") for leg in quotes):
        out["refusal"] = "no_leg_obeys_rule"
    elif not sizing["legs"]:
        out["refusal"] = f"insufficient_cash:{float(cash_usdc or 0.0):.2f}<{sizing['need_full_usdc']:.2f}"
    return out


# ---------------------------------------------------------------------------
# Reading the store.


def _fact(conn: sqlite3.Connection, key: Mapping[str, Any]) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT fact_id, settlement_timezone, probability_envelope_id, ws_snapshot_id, decision_at_utc"
        " FROM basket_policy_training_fact"
        " WHERE station_id=? AND event_date=? AND stage=? AND decision_at_utc=?"
        " ORDER BY created_at_utc, fact_id LIMIT 1",
        (key["station_id"], key["event_date"], key["stage"], key["decision_at_utc"]),
    ).fetchone()
    if row is None:
        return None
    return {
        "fact_id": str(row[0]), "settlement_timezone": str(row[1]),
        "envelope_id": str(row[2]), "snapshot_id": str(row[3]), "decision_at_utc": str(row[4]),
    }


def _latest_stage(conn: sqlite3.Connection, station_id: str, event_date: str) -> str | None:
    row = conn.execute(
        "SELECT stage FROM basket_policy_training_fact WHERE station_id=? AND event_date=?"
        " ORDER BY decision_at_utc DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    return str(row[0]) if row else None


def _envelope(conn: sqlite3.Connection, envelope_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT probabilities_json, content_sha256, decision_at_utc, stage"
        " FROM basket_probability_envelope WHERE envelope_id=?",
        (envelope_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        probabilities = json.loads(row[0] or "{}") or {}
    except ValueError:
        return None
    return {
        "probabilities": probabilities, "content_sha256": str(row[1]),
        "decision_at_utc": str(row[2]), "stage": str(row[3]),
    }


def _evaluated(conn: sqlite3.Connection, *, since_event_date: str) -> set[tuple[str, str, str]]:
    return {
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            f"SELECT station_id, event_date, stage FROM {EVALUATION_TABLE} WHERE event_date >= ?",
            (since_event_date,),
        )
    }


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

    The publisher's comes first for a second reason besides speed: a rung
    resting on the station-day holds its market ingress shut, so the
    committed snapshot stops advancing while this lane is posting.
    """

    candidates: list[dict[str, Any]] = []
    if live:
        book = live_ws_snapshot(conn, root, station_id=station_id, event_date=event_date, now_utc=now_utc)
        if book is not None:
            candidates.append(book)
    committed = _latest_snapshot(conn, station_id, event_date)
    if committed is not None:
        candidates.append({**committed, "book_source": "committed_snapshot"})
    if not candidates:
        return None, "book_unavailable"
    now = _utc(now_utc)
    for book in candidates:
        try:
            age = (now - _utc(book["observed_at"])).total_seconds()
        except (TypeError, ValueError, KeyError):
            continue
        if age <= float(fresh_seconds):
            return {**book, "age_seconds": round(age, 1)}, None
    return None, "book_stale"


def _free_cash(conn: sqlite3.Connection, *, now_utc: str) -> tuple[float | None, str | None]:
    """The account's cash, read as the tiebreak lane reads it (charter 141)."""

    if not account_equity_table_present(conn):
        return None, None
    row = load_account_equity(conn, as_of_utc=now_utc)
    if row is None:
        try:
            capture_account_equity(conn, as_of_utc=now_utc, write=nullcontext, budget_usdc=_budget_dial())
        except Exception:  # noqa: BLE001 -- a failed read leaves the stale row
            pass
        row = load_account_equity(conn, as_of_utc=now_utc)
    if row is None:
        return None, "account_equity_stale"
    return float(row.get("cash_usdc") or 0.0), None


def _window_refusal(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, settlement_timezone: str,
    now_utc: str, min_hours: float, gate: Admission,
) -> tuple[str | None, float | None]:
    """Why this station-day may not be bet on now, and its hours to settlement."""

    try:
        verdict = gate(station_id, conn)
    except Exception as exc:  # noqa: BLE001 -- an unreadable gate admits nothing
        return f"station_admission_unreadable:{type(exc).__name__}", None
    if verdict.get("admitted") is not True:
        return f"station_not_admitted:{verdict.get('reason') or 'refused'}", None
    try:
        day = settlement_day_state(event_date, as_of_utc=now_utc, settlement_timezone=settlement_timezone)
        hours = hours_to_settlement(event_date, as_of_utc=now_utc, settlement_timezone=settlement_timezone)
    except Exception as exc:  # noqa: BLE001
        return f"settlement_day_unknown:{type(exc).__name__}", None
    if day.get("state") == "closed":
        return "settlement_day_closed", hours
    if not hours > float(min_hours):
        return f"inside_{float(min_hours):g}h:{hours:.1f}h", hours
    return None, hours


def _record(
    conn: sqlite3.Connection,
    key: Mapping[str, Any],
    fact: Mapping[str, Any] | None,
    *,
    rule_version: str,
    now_utc: str,
    refusal: str | None,
    envelope: Mapping[str, Any] | None = None,
    book: Mapping[str, Any] | None = None,
    reading: Mapping[str, Any] | None = None,
    hours: float | None = None,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reading = dict(reading or {})
    legs = list(reading.get("legs") or [])
    row = {
        "station_id": key["station_id"], "event_date": key["event_date"], "stage": key["stage"],
        "decision_at_utc": key["decision_at_utc"],
        "settlement_timezone": (fact or {}).get("settlement_timezone"),
        "fact_id": (fact or {}).get("fact_id"),
        "envelope_id": (fact or {}).get("envelope_id"),
        "envelope_sha256": (envelope or {}).get("content_sha256"),
        "snapshot_id": (book or {}).get("snapshot_id"),
        "book_observed_at": (book or {}).get("observed_at"),
        "book_source": (book or {}).get("book_source"),
        "rule_version": rule_version,
        "p_json": json.dumps(reading.get("p"), sort_keys=True) if reading.get("p") else None,
        "q_json": json.dumps(reading.get("q"), sort_keys=True) if reading.get("q") else None,
        "s_json": json.dumps(reading.get("s"), sort_keys=True) if reading.get("s") else None,
        "divergence_kl": reading.get("divergence_kl"),
        "stake_usdc": reading.get("need_full_usdc"),
        "budget_usdc": reading.get("budget_usdc"),
        "lambda_shares": reading.get("lambda_shares"),
        "buckets_json": json.dumps(
            {"quotes": reading.get("quotes"), "dropped": reading.get("dropped"),
             "capped_by_cash": reading.get("capped_by_cash"),
             **({k: reading[k] for k in ("market_reason", "model_only", "market_only") if k in reading})},
            ensure_ascii=False, sort_keys=True,
        ) if reading else None,
        "planned_legs": len(legs),
        "planned_cost_usdc": round(sum(float(leg["cost_usdc"]) for leg in legs), 6),
        "refusal": refusal,
        "sends_json": None,
        "evaluated_at_utc": now_utc,
        "hours_to_settlement": hours,
        "plan_json": json.dumps(plan, ensure_ascii=False, sort_keys=True) if plan else None,
        "plan_state": PLAN_ACTIVE if plan else None,
        "plan_state_reason": None,
        "legs_state_json": json.dumps({}) if plan else None,
        "passes": 0 if plan else None,
        "last_pass_at_utc": None,
    }
    columns = ",".join(row)
    conn.execute(
        f"INSERT OR IGNORE INTO {EVALUATION_TABLE}({columns}) VALUES({','.join('?' for _ in row)})",
        tuple(row.values()),
    )
    return row


def _leg_key(leg: Mapping[str, Any]) -> str:
    return f"{leg['side']}:{leg['bucket']}"


# ---------------------------------------------------------------------------
# Plan: judge each new first decision once.


def plan_new_first_decisions(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    now_utc: str,
    cfg: Mapping[str, Any],
    gate: Admission,
    manifest_root: Callable[[], Mapping[str, Any] | None],
    cash: dict[str, Any],
) -> dict[str, Any]:
    rule_version = RULE_VERSIONS[str(cfg["rule"])]
    fresh_seconds = float(cfg["first_decision_fresh_seconds"])
    book_fresh = float(cfg["send_book_fresh_seconds"])
    min_hours = float(cfg["min_hours_to_settlement"])
    live = cfg.get("live_ws_book", True) is True
    stages = opening_stages(cfg)
    now = _utc(now_utc)
    done = _evaluated(
        target_conn,
        since_event_date=(now - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date().isoformat(),
    )
    evaluations: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for key in stage_first_decisions(target_conn, now_utc=now_utc, lookback_days=DEFAULT_LOOKBACK_DAYS):
        if str(key["stage"]) not in stages:
            continue
        if (key["station_id"], key["event_date"], key["stage"]) in done:
            continue
        fact = _fact(target_conn, key)
        if fact is None:
            evaluations.append(_record(target_conn, key, None, rule_version=rule_version, now_utc=now_utc,
                                       refusal="fact_missing"))
            continue
        refusal, hours = _window_refusal(
            target_conn, station_id=key["station_id"], event_date=key["event_date"],
            settlement_timezone=fact["settlement_timezone"], now_utc=now_utc,
            min_hours=min_hours, gate=gate,
        )
        if refusal:
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal=refusal, hours=hours))
            continue
        young = (now - _utc(fact["decision_at_utc"])).total_seconds() <= fresh_seconds
        if not young:
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal="first_decision_stale", hours=hours))
            continue
        envelope = _envelope(target_conn, fact["envelope_id"])
        if envelope is None:
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal="envelope_missing", hours=hours))
            continue
        book, book_refusal = _book(
            target_conn, manifest_root(), station_id=key["station_id"], event_date=key["event_date"],
            now_utc=now_utc, fresh_seconds=book_fresh, live=live,
        )
        if book is None:
            waiting.append({**key, "reason": book_refusal})
            continue
        registry = _registry_rows(target_conn, canonical_conn, station_id=key["station_id"], event_date=key["event_date"])
        min_shares: dict[str, float] = {}
        ticks: dict[str, float] = {}
        tokens: dict[str, dict[str, Any]] = {}
        for bucket, row in registry.items():
            try:
                raw = json.loads(row.get("registry_json") or "{}")
            except ValueError:
                raw = {}
            min_shares[bucket] = float(row.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
            if row.get("tick_size") not in (None, ""):
                ticks[bucket] = float(row["tick_size"])
            tokens[bucket] = {
                SIDE_YES: str(row.get("yes_token") or ""),
                SIDE_NO: str(raw.get("no_token") or ""),
                "accepting_orders": bool(row.get("accepting_orders")),
            }
        if "cash" not in cash:
            cash["cash"], cash["refusal"] = _free_cash(target_conn, now_utc=now_utc)
        if cash.get("refusal"):
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal=cash["refusal"], envelope=envelope, book=book, hours=hours))
            continue
        reading = evaluate_ladder(envelope["probabilities"], list(book["buckets"]),
                                  cash_usdc=cash.get("cash"), min_shares=min_shares, ticks=ticks)
        if reading.get("refusal") == "market_incomplete":
            waiting.append({**key, "reason": f"market_incomplete:{reading.get('market_reason')}"})
            continue
        if reading.get("refusal"):
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal=reading["refusal"], envelope=envelope, book=book,
                                       reading=reading, hours=hours))
            continue
        legs: list[dict[str, Any]] = []
        unsendable: dict[str, str] = {}
        for leg in reading["legs"]:
            token = tokens.get(str(leg["bucket"])) or {}
            token_id = str(token.get(leg["side"]) or "")
            if not token_id:
                unsendable[str(leg["bucket"])] = f"registry_{leg['side'].lower()}_token_missing"
                continue
            if not token.get("accepting_orders"):
                unsendable[str(leg["bucket"])] = "market_not_accepting_orders"
                continue
            legs.append({
                "bucket": leg["bucket"], "side": leg["side"], "p": leg["p"], "s": leg["s"],
                "token_id": token_id, "tick_size": ticks.get(str(leg["bucket"])),
                "min_size": min_shares.get(str(leg["bucket"]), DEFAULT_MIN_ORDER_SHARES),
                "target_shares": leg["target_shares"], "planned_price": leg["price"],
                "planned_cost_usdc": leg["cost_usdc"],
            })
        reading = {**reading, "legs": [l for l in reading["legs"] if str(l["bucket"]) not in unsendable],
                   "dropped": {**reading["dropped"], **unsendable}}
        if not legs:
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal="no_sendable_leg", envelope=envelope, book=book,
                                       reading=reading, hours=hours))
            continue
        if cash.get("cash") is not None:
            cash["cash"] = float(cash["cash"]) - sum(float(leg["planned_cost_usdc"]) for leg in legs)
        plan = {
            "schema": SCHEMA, "rule": rule_version, "rule_name": str(cfg["rule"]), "lane": LANE,
            "title": LANE_TITLE,
            "station_id": key["station_id"], "event_date": key["event_date"], "stage": key["stage"],
            "settlement_timezone": fact["settlement_timezone"], "fact_id": fact["fact_id"],
            "decision_at_utc": fact["decision_at_utc"], "envelope_id": fact["envelope_id"],
            "envelope_sha256": envelope["content_sha256"], "envelope_decision_at_utc": envelope["decision_at_utc"],
            "snapshot_id": book["snapshot_id"], "book_observed_at": book["observed_at"],
            "book_source": book.get("book_source"), "execution_candidate": EXECUTION_CANDIDATE,
            "lambda_shares": reading["lambda_shares"], "need_full_usdc": reading["need_full_usdc"],
            "budget_usdc": reading["budget_usdc"], "capped_by_cash": reading["capped_by_cash"],
            "divergence_kl": reading["divergence_kl"], "min_hours_to_settlement": min_hours,
            "legs": legs,
        }
        plan["plan_id"] = f"{INTENT_PREFIX}_{content_sha256({'plan': plan})[:32]}"
        evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                   refusal=None, envelope=envelope, book=book, reading=reading,
                                   hours=hours, plan=plan))
    return {"evaluations": evaluations, "waiting": waiting}


# ---------------------------------------------------------------------------
# Rungs: 「重新挂单，直至成交」 on every active plan.


def _leg_progress(conn: sqlite3.Connection, intent_ids: list[str]) -> tuple[float, bool]:
    """What the leg's rungs have filled, and whether one is still at the venue."""

    if not intent_ids:
        return 0.0, False
    marks = ",".join("?" for _ in intent_ids)
    filled = conn.execute(
        f"SELECT COALESCE(SUM(filled_size),0) FROM basket_target_order_reconciliations"
        f" WHERE order_intent_id IN ({marks})",
        tuple(intent_ids),
    ).fetchone()[0]
    open_row = conn.execute(
        f"SELECT 1 FROM basket_target_order_intents WHERE order_intent_id IN ({marks})"
        f" AND state IN ({','.join('?' for _ in OPEN_STATES)}) LIMIT 1",
        (*intent_ids, *OPEN_STATES),
    ).fetchone()
    return float(filled or 0.0), open_row is not None


def plan_rungs(
    target_conn: sqlite3.Connection,
    *,
    now_utc: str,
    cfg: Mapping[str, Any],
    gate: Admission,
    manifest_root: Callable[[], Mapping[str, Any] | None],
    cash: dict[str, Any],
) -> dict[str, Any]:
    """Every active plan: close it if its window has, else price the rungs it
    still needs on the book that stands, each under the rule."""

    book_fresh = float(cfg["send_book_fresh_seconds"])
    min_hours = float(cfg["min_hours_to_settlement"])
    live = cfg.get("live_ws_book", True) is True
    rungs: list[dict[str, Any]] = []
    passes: list[dict[str, Any]] = []
    rows = target_conn.execute(
        f"SELECT station_id, event_date, stage, plan_json, legs_state_json, COALESCE(passes,0)"
        f" FROM {EVALUATION_TABLE} WHERE plan_state=?",
        (PLAN_ACTIVE,),
    ).fetchall()
    for station, day, stage, plan_json, legs_json, n_passes in rows:
        plan = json.loads(plan_json)
        state = json.loads(legs_json or "{}")
        where = (str(station), str(day), str(stage))

        def close(reason: str, *, final: str = PLAN_CLOSED) -> None:
            target_conn.execute(
                f"UPDATE {EVALUATION_TABLE} SET plan_state=?, plan_state_reason=?, last_pass_at_utc=?"
                " WHERE station_id=? AND event_date=? AND stage=?",
                (final, reason, now_utc, *where),
            )
            passes.append({"station_id": station, "event_date": day, "closed": final, "reason": reason})

        refusal, hours = _window_refusal(
            target_conn, station_id=str(station), event_date=str(day),
            settlement_timezone=str(plan["settlement_timezone"]), now_utc=now_utc,
            min_hours=min_hours, gate=gate,
        )
        if refusal:
            close(refusal)
            continue
        latest_stage = _latest_stage(target_conn, str(station), str(day))
        if latest_stage and latest_stage != "P0":
            close(f"stage_left_p0:{latest_stage}")
            continue
        book = None
        book_refusal = None
        outstanding = 0
        for leg in plan["legs"]:
            leg_state = state.setdefault(_leg_key(leg), {"intents": []})
            filled, open_rung = _leg_progress(target_conn, list(leg_state["intents"]))
            leg_state["filled_shares"] = filled
            remaining = float(leg["target_shares"]) - filled
            if remaining < float(leg["min_size"]) - 1e-9:
                leg_state["status"] = "filled" if filled > 0 else "done"
                continue
            outstanding += 1
            if open_rung:
                leg_state["status"] = "resting"
                continue
            if book is None and book_refusal is None:
                book, book_refusal = _book(
                    target_conn, manifest_root(), station_id=str(station), event_date=str(day),
                    now_utc=now_utc, fresh_seconds=book_fresh, live=live,
                )
            if book is None:
                leg_state["status"] = f"waiting:{book_refusal}"
                continue
            row = next((r for r in book["buckets"] if str(r.get("bucket")) == str(leg["bucket"])), None)
            tick = float(leg.get("tick_size") or (row or {}).get("price_tick_size") or 0.01)
            quote = leg_quote(str(leg["bucket"]), float(leg["p"]), float(leg["s"]), row, tick)
            if quote["reason"] or quote["side"] != leg["side"]:
                leg_state["status"] = f"waiting:{quote['reason'] or 'side_changed'}"
                leg_state["last_quote"] = {"price": quote["price"], "bid": quote["bid"], "ask": quote["ask"]}
                continue
            scale = 10 ** SHARE_PRECISION
            shares = math.floor(remaining * scale + 1e-6) / scale
            cost = round(shares * float(quote["price"]), 6)
            if cash.get("cash") is not None and float(cash["cash"]) < cost - 1e-9:
                leg_state["status"] = f"waiting:insufficient_cash:{float(cash['cash']):.2f}<{cost:.2f}"
                continue
            if cash.get("cash") is not None:
                cash["cash"] = float(cash["cash"]) - cost
            venue = resting_order_fields(now_utc=now_utc, limit_price=float(quote["price"]),
                                         belief_fresh_until_utc=now_utc)
            n = len(leg_state["intents"])
            intent_id = f"{INTENT_PREFIX}_" + content_sha256({
                "plan_id": plan["plan_id"], "bucket": leg["bucket"], "side": leg["side"], "n": n,
            })[:32]
            leg_state["status"] = "posting"
            rungs.append({
                "where": where, "plan": plan, "leg": leg, "intent_id": intent_id,
                "price": float(quote["price"]), "shares": shares, "cost_usdc": cost,
                "bid": quote["bid"], "ask": quote["ask"], "tick": tick,
                "book_id": book.get("snapshot_id"), "book_source": book.get("book_source"),
                "hours_to_settlement": hours, **venue,
            })
        if outstanding == 0:
            target_conn.execute(
                f"UPDATE {EVALUATION_TABLE} SET legs_state_json=? WHERE station_id=? AND event_date=? AND stage=?",
                (json.dumps(state, ensure_ascii=False, sort_keys=True), *where),
            )
            close("all_legs_filled", final=PLAN_DONE)
            continue
        target_conn.execute(
            f"UPDATE {EVALUATION_TABLE} SET legs_state_json=?, passes=?, last_pass_at_utc=?"
            " WHERE station_id=? AND event_date=? AND stage=?",
            (json.dumps(state, ensure_ascii=False, sort_keys=True), int(n_passes) + 1, now_utc, *where),
        )
        passes.append({"station_id": station, "event_date": day, "outstanding_legs": outstanding})
    return {"rungs": rungs, "passes": passes}


def _append_intent(conn: sqlite3.Connection, where: tuple[str, str, str], leg: Mapping[str, Any],
                   intent_id: str, send: Mapping[str, Any]) -> None:
    row = conn.execute(
        f"SELECT legs_state_json FROM {EVALUATION_TABLE} WHERE station_id=? AND event_date=? AND stage=?",
        where,
    ).fetchone()
    state = json.loads((row[0] if row else None) or "{}")
    leg_state = state.setdefault(_leg_key(leg), {"intents": []})
    if intent_id not in leg_state["intents"]:
        leg_state["intents"].append(intent_id)
    leg_state["last_send"] = dict(send)
    leg_state["status"] = "resting" if send.get("event_type") == "POST_ACK" else str(
        send.get("event_type") or send.get("state") or ""
    )
    conn.execute(
        f"UPDATE {EVALUATION_TABLE} SET legs_state_json=? WHERE station_id=? AND event_date=? AND stage=?",
        (json.dumps(state, ensure_ascii=False, sort_keys=True), *where),
    )


def execute_proper_bets(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Any = None,
) -> dict[str, Any]:
    """Judge new first decisions, then post every rung an active plan needs."""

    ensure_lane_schema(target_conn)
    cfg = {**PROPER_BET_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    if str(cfg.get("rule")) not in RULE_VERSIONS:
        raise ValueError(f"proper_bet_rule_unknown:{cfg.get('rule')}")
    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    live = cfg.get("live_ws_book", True) is True
    manifest: dict[str, Any] = {}

    def manifest_root() -> Mapping[str, Any] | None:
        if "root" not in manifest:
            manifest["root"] = _ws_manifest_root(ws_manifest_path) if live else None
        return manifest["root"]

    cash: dict[str, Any] = {}
    judged = plan_new_first_decisions(
        target_conn, canonical_conn, now_utc=now_utc, cfg=cfg, gate=gate,
        manifest_root=manifest_root, cash=cash,
    )
    # A fresh cash read for the rungs: planning only reserved on paper.
    cash = {}
    cash["cash"], cash["refusal"] = _free_cash(target_conn, now_utc=now_utc)
    if cash.get("refusal"):
        cash["cash"] = 0.0
    pending = plan_rungs(target_conn, now_utc=now_utc, cfg=cfg, gate=gate,
                         manifest_root=manifest_root, cash=cash)
    receipts: list[dict[str, Any]] = []
    submitted = 0
    filled = 0
    for rung in pending["rungs"]:
        plan, leg = rung["plan"], rung["leg"]
        details = {
            "schema": "proper_bet_lane_decision_v2", "lane": LANE, "title": LANE_TITLE, "rule": plan["rule"],
            "decision_signature": {"stage": plan["stage"], "lane": LANE, "rule": plan["rule"]},
            "forward_evidence_binding": {
                "decision_at_utc": now_utc,
                "probability_decision_at_utc": plan["envelope_decision_at_utc"],
                "probability_artifact_sha256": plan["envelope_sha256"],
                "probability_stage": plan["stage"],
                "probability_envelope_id": plan["envelope_id"],
                "ws_snapshot_id": plan["snapshot_id"],
            },
            "plan": plan,
        }
        entry = send_lane_buy(
            target_conn, lane=LANE, intent_id=rung["intent_id"],
            station_id=plan["station_id"], event_date=plan["event_date"],
            settlement_timezone=plan["settlement_timezone"], bucket=leg["bucket"],
            token_id=leg["token_id"], price=rung["price"], shares=rung["shares"],
            side=leg["side"], execution_candidate=EXECUTION_CANDIDATE,
            expiration=int(rung["expiration"]),
            tick_size=float(rung["tick"]),
            intent_body={
                "schema": SCHEMA, "rule": plan["rule"], "strategy_id": STRATEGY_ID,
                "order_type": rung["order_type"], "post_only": rung["post_only"],
                "expiration_basis": rung.get("expiration_basis"), "plan_id": plan["plan_id"],
                "leg": leg, "rung": {k: rung[k] for k in ("price", "shares", "bid", "ask", "tick",
                                                           "book_id", "book_source", "hours_to_settlement")},
            },
            transport=transport, live_submit_authorized=live_submit_authorized,
            decision_details=details,
        )
        if entry.get("exchange_post_started"):
            submitted += 1
        if float(entry.get("matched_size") or 0.0) > 0.0:
            filled += 1
        send = {
            "order_intent_id": rung["intent_id"], "price": rung["price"], "shares": rung["shares"],
            "state": entry.get("state"), "event_type": entry.get("event_type"), "reason": entry.get("reason"),
            "matched_size": entry.get("matched_size"), "at": now_utc,
        }
        _append_intent(target_conn, rung["where"], leg, rung["intent_id"], send)
        receipts.append({"station_id": plan["station_id"], "event_date": plan["event_date"],
                         "bucket": leg["bucket"], "side": leg["side"], **send})
    return {
        "schema": "proper_bet_lane_receipt_v2",
        "lane": LANE,
        "title": LANE_TITLE,
        "rule": RULE_VERSIONS[str(cfg["rule"])],
        "settings": cfg,
        "evaluated": len(judged["evaluations"]),
        "planned": sum(1 for e in judged["evaluations"] if e.get("plan_json")),
        "waiting": len(judged["waiting"]),
        "active_plans": sum(1 for p in pending["passes"] if "outstanding_legs" in p),
        "closed_plans": [p for p in pending["passes"] if "closed" in p],
        "rungs": len(pending["rungs"]),
        "submitted": submitted,
        "filled": filled,
        "refusals": sorted({str(e["refusal"]) for e in judged["evaluations"] if e.get("refusal")}),
        "receipts": receipts,
    }


__all__ = [
    "DEFAULT_RULE",
    "EVALUATION_TABLE",
    "EXECUTION_CANDIDATE",
    "LANE",
    "LANE_TITLE",
    "PLAN_ACTIVE",
    "PLAN_CLOSED",
    "PLAN_DONE",
    "RULE_MAKER_V2",
    "RULE_VERSIONS",
    "SIDE_NO",
    "SIDE_YES",
    "divergence_kl",
    "ensure_lane_schema",
    "evaluate_ladder",
    "execute_proper_bets",
    "leg_quote",
    "plan_new_first_decisions",
    "plan_rungs",
    "proper_position",
    "rung_price",
    "side_book",
    "size_minimum",
]
