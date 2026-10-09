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
  never under the MINIMUM the venue allows for the whole position -- the
  smallest leg at the venue's minimum order (five shares), every other leg
  in exact ratio -- and raised until the position costs ``stake_usdc``, the
  board's per-trade dial (charter 252, user ruling 2026-10-04 「还有Proper
  下注线的单笔预算调整按钮」).  When the free cash cannot carry that, the stake
  is cut to the cash; when it cannot carry the minimum, the legs are taken
  in order of ``|s|`` and the longest prefix whose own minimum the cash can
  carry is kept (``budget_capped_by_cash`` names the shortfall).
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
* Sides (charter 235, user ruling 2026-09-28 「Proper 线的 YES 腿停掉」): the
  ``sides`` setting names the directions a leg may take, ``["NO"]`` as
  shipped.  A leg on a stopped side is still quoted -- its rung and the
  rule's verdict stay on the record -- and never posted or sized; an active
  plan stops re-posting it.

* Charter 249 (user rulings 2026-10-03 「重开Proper 线并放开 YES，只开YES，市场
  众数桶，挂单」 and 「限p0」), rule v3: the lane reopens on ONE leg a
  station-day -- YES on the market's own modal bucket (the largest normalised
  mid, the lower rung on a tie), rested at the best bid plus one tick, only
  under ``p``.  ``buckets: "market_mode"`` names the bucket scope
  (``"all"`` is charter 231's whole ladder); a leg off the mode is still
  quoted and recorded ``not_market_mode``, never posted.
  ``entry: "latest_decision"`` judges the station-day's NEWEST P0 decision on
  every pass until a plan is made, instead of the first decision inside its
  first hour: over 2026-09-26..30 the first-decision entry planned 3
  station-days and refused 170 ``first_decision_stale``, because a ladder
  more than two days out is rarely quoted whole in that hour.  While a plan
  is active each re-post reads the newest P0 envelope's ``p`` and the
  standing book: the leg waits while its bucket is not the market's mode or
  the rung would not be under ``p``.  A rung under the venue's one-dollar
  minimum is sized up to it (charter 83).  Measured before the ruling
  (docs/research/proper_conditional_edge_20261003, section 14): this leg is
  positive under every fill assumption tried -- +1.7c a share when only a
  trade-through fills, +2.8c when a lifted level fills -- and its intervals
  reach zero on the served record; the lane is the live measurement.

v1 (FOK at the worst in-band ask level, 2026-09-26 11:52Z-) never sent an
order: every first decision it saw was stale, closed or barred.  v2's 468
intents of 2026-09-28 were 464 gateway refusals (156 on the venue's GTD floor
while this host's clock ran 107 s behind, 308 on prices under one cent), three
transport errors and ONE order that rested -- and filled.

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
from collections import Counter
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
from .production import GTD_SIGNING_MARGIN_SECONDS, VENUE_GTD_MINIMUM_HORIZON_SECONDS
from .delta_v2_runtime import (
    CHARTER_83_MIN_NOTIONAL,
    EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC,
    bid_anchored_ladder,
)
from . import benchmark_cluster
from .execution_reconciliation import can_submit_next_order
from .exit_orders import Transport, default_position_reader, send_exit_sell
from .lane_orders import send_lane_buy
from .live_station_scope import admission as live_station_admission
from .materialization import floor_probabilities
from . import realisation_store
from .p1_tail_lane import (
    DEPTH_HEADROOM_FRACTION, _latest_snapshot, _sendable_shares, _ws_manifest_root, live_ws_snapshot,
)
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
RULE_MAKER_MODE_YES_V3 = "log_proper_maker_market_mode_yes_v3"
# Charter 265 (user ruling 2026-10-10 「改」): back to charter 231's standard
# rule -- every bucket, both sides, x_k = lambda * |s_k| -- with s read off the
# realisation layer's belief b (its three latest-run change columns), not the
# weather vector's level.
RULE_MAKER_BELIEF_B_V4 = "log_proper_maker_realisation_belief_v4"
RULE_VERSIONS = {RULE_MAKER_V2: "proper_bet_lane_v2", RULE_MAKER_MODE_YES_V3: "proper_bet_lane_v3",
                 RULE_MAKER_BELIEF_B_V4: "proper_bet_lane_v4"}
DEFAULT_RULE = RULE_MAKER_BELIEF_B_V4
EXECUTION_CANDIDATE = "MAKER"
EVALUATION_TABLE = "proper_bet_lane_evaluation"
INTENT_PREFIX = "proper"
DEFAULT_MIN_ORDER_SHARES = 5.0
DEFAULT_LOOKBACK_DAYS = 3
SHARE_PRECISION = 2
OPEN_STATES = ("prepared", "submitting", "submitted", "reconcile_required")

#: Charter 258 (user rulings 2026-10-09 「proper线增加一条规则：不做对标账号的对手盘」,
#: 「买no线交给跟单线」 and 「proper线以对标账号为筛子，不做对标账号的对手盘！」): the
#: ``benchmark_counterparty`` setting.  ``seal`` uses the cluster as the
#: lane's filter -- no leg, no rung, a resting rung cancelled, on a bucket it
#: has traded against; ``sell`` also sells a YES the lane holds there (the
#: earlier 「先卖出」); ``off`` is the lane before the ruling.  Buying the NO
#: is the copy-follow lane's, never this lane's.
BENCHMARK_SELL = "sell"
BENCHMARK_SEAL = "seal"
BENCHMARK_OFF = "off"
BENCHMARK_MODES = (BENCHMARK_SELL, BENCHMARK_SEAL, BENCHMARK_OFF)
BENCHMARK_OPPOSITE = "benchmark_opposite"
BENCHMARK_UNREADABLE = "benchmark_unreadable"
SEAL_TABLE = "proper_bet_benchmark_seals"
SELL_PREFIX = f"{INTENT_PREFIX}_flipsell"
#: The YES bought back after a sealed NO is sold (user ruling 2026-10-09
#: 「并反向买入yes」), a taker FOK at the YES ask.
REVERSE_YES_PREFIX = f"{INTENT_PREFIX}_revyes"
REVERSE_EXECUTION_CANDIDATE = "TAKER_FOK"
#: Polymarket's weather taker fee: shares x rate x p x (1 - p) (charter 234).
TAKER_FEE_RATE = 0.05
SELL_SCHEMA = "proper_bet_benchmark_sell_v1"
#: How far back the sell pass looks for held legs; a settled day drops out on
#: its own clock (``settlement_day_closed``).
SELL_LOOKBACK_DAYS = 3

#: The two directions a leg can take, as the ledger spells the outcome leg.
SIDE_YES = "YES"
SIDE_NO = "NO"

BOTH_SIDES = frozenset({SIDE_YES, SIDE_NO})
SIDE_STOPPED = "side_stopped"

#: Which buckets a leg may sit on (charter 249): the whole ladder, or only the
#: market's own modal bucket.
BUCKETS_ALL = "all"
BUCKETS_MARKET_MODE = "market_mode"
NOT_MARKET_MODE = "not_market_mode"
#: Which decision opens a station-day: its first P0 decision (charter 231), or
#: its newest one on every pass until a plan is made (charter 249).
ENTRY_FIRST_DECISION = "first_decision"
ENTRY_LATEST_DECISION = "latest_decision"
#: Refusals under the latest-decision entry that no later pass can lift.
TERMINAL_REFUSAL_PREFIXES = ("inside_", "settlement_day_closed")

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
#: Charter 258: one row per bucket the cluster has stood against a leg of
#: this lane on -- its position (YES and NO shares apart, the stance and since
#: when) and the latest fill that moved it there are the evidence, and a row of
#: the current ``criterion`` is the seal.  Every read judges the bucket again
#: on the position as it stands (user ruling 「不做对手盘：要区分yes和no的份额和时间
#: 轴」): a seal whose stance has turned is marked ``SEAL_LIFTED`` with when,
#: and sealed again if it turns back; a row of an earlier criterion is
#: rewritten either way.  The sell step's progress is kept on
#: it (the ``flip_*`` columns); what it still needs is always recomputed from
#: the order store.
SEAL_DDL = (
    f"CREATE TABLE IF NOT EXISTS {SEAL_TABLE} ("
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " bucket TEXT NOT NULL,"
    " side TEXT NOT NULL,"
    " yes_token TEXT NOT NULL,"
    " no_token TEXT NOT NULL,"
    " condition_id TEXT NOT NULL,"
    " sealed_at_utc TEXT NOT NULL,"
    " sealed_by TEXT NOT NULL,"
    " wallet TEXT NOT NULL,"
    " fill_ts INTEGER NOT NULL,"
    " fill_tx TEXT,"
    " fill_token_side TEXT NOT NULL,"
    " fill_action TEXT NOT NULL,"
    " fill_price REAL NOT NULL,"
    " fill_size REAL NOT NULL,"
    " implied_price REAL NOT NULL,"
    " opposite_fills INTEGER NOT NULL,"
    " flip_state TEXT,"
    " flip_json TEXT,"
    " flip_updated_at_utc TEXT,"
    " PRIMARY KEY (station_id, event_date, bucket, side)"
    ")"
)
#: Added with the net-side criterion (user ruling 2026-10-09 「改封桶口径」).
#: ``opposite_shares``/``same_side_shares`` are the v2 criterion's flows and
#: stay on its rows only; v3 (「要区分yes和no的份额和时间轴」) records the
#: cluster's YES and NO shares apart, the locked pairs, the stance and since
#: when, and when a seal lifted.
SEAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("criterion", "TEXT"),
    ("opposite_shares", "REAL"),
    ("same_side_shares", "REAL"),
    ("yes_shares", "REAL"),
    ("no_shares", "REAL"),
    ("paired_shares", "REAL"),
    ("net_yes", "REAL"),
    ("since_ts", "INTEGER"),
    ("lifted_at_utc", "TEXT"),
)
SEAL_LIFTED = f"lifted:{benchmark_cluster.SEAL_CRITERION}"

Admission = Callable[[str, sqlite3.Connection], Mapping[str, Any]]


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    conn.execute(EVALUATION_DDL)
    conn.execute(SEAL_DDL)
    have_seal = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({SEAL_TABLE})")}
    for name, kind in SEAL_COLUMNS:
        if name not in have_seal:
            conn.execute(f"ALTER TABLE {SEAL_TABLE} ADD COLUMN {name} {kind}")
    have = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({EVALUATION_TABLE})")}
    for name, kind in PLAN_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE {EVALUATION_TABLE} ADD COLUMN {name} {kind}")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_plan_state_idx"
        f" ON {EVALUATION_TABLE}(plan_state, event_date)"
    )
    realisation_store.ensure_shadow_schema(conn)


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


def allowed_sides(settings: Mapping[str, Any]) -> frozenset[str]:
    """The sides a leg may take (charter 235).

    A list of ``YES``/``NO``, possibly one of them.  Anything else raises, as
    ``opening_stages`` does: a lane that guessed which side to trade would be
    deciding where to spend on its own.
    """

    declared = settings.get("sides")
    if not isinstance(declared, (list, tuple)):
        raise ValueError(f"proper_bet_sides_not_a_list:{declared!r}")
    sides = frozenset(str(side).strip().upper() for side in declared)
    unknown = sorted(sides - BOTH_SIDES)
    if unknown:
        raise ValueError(f"proper_bet_sides_unknown:{','.join(unknown)}")
    return sides


def bucket_scope(settings: Mapping[str, Any]) -> str:
    """``all`` or ``market_mode`` (charter 249); anything else raises."""

    declared = str(settings.get("buckets", BUCKETS_ALL) or "").strip().lower()
    if declared not in {BUCKETS_ALL, BUCKETS_MARKET_MODE}:
        raise ValueError(f"proper_bet_buckets_unknown:{settings.get('buckets')!r}")
    return declared


def rung_horizon_seconds(settings: Mapping[str, Any]) -> float:
    """How long a rung rests before it expires and is re-posted (charter 249).

    Zero is the venue's minimum horizon, as charter 231 ruled it (「挂单周期最
    短」); a positive number is that many seconds.  Anything else raises.
    """

    declared = settings.get("rung_horizon_seconds", 0.0)
    try:
        value = float(declared)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"proper_bet_rung_horizon_not_a_number:{declared!r}") from exc
    if not (math.isfinite(value) and value >= 0.0):
        raise ValueError(f"proper_bet_rung_horizon_invalid:{declared!r}")
    return value


def position_stake(settings: Mapping[str, Any]) -> float:
    """The position's budget in USDC (charter 252): the board dial
    ``stake_usdc``, read off the lane file on every pass.  A positive finite
    number; anything else raises."""

    declared = settings.get("stake_usdc")
    try:
        value = float(declared)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"proper_bet_stake_not_a_number:{declared!r}") from exc
    if not (math.isfinite(value) and value > 0.0):
        raise ValueError(f"proper_bet_stake_invalid:{declared!r}")
    return value


def entry_rule(settings: Mapping[str, Any]) -> str:
    """``first_decision`` or ``latest_decision`` (charter 249); anything else raises."""

    declared = str(settings.get("entry", ENTRY_FIRST_DECISION) or "").strip().lower()
    if declared not in {ENTRY_FIRST_DECISION, ENTRY_LATEST_DECISION}:
        raise ValueError(f"proper_bet_entry_unknown:{settings.get('entry')!r}")
    return declared


def benchmark_mode(settings: Mapping[str, Any]) -> str:
    """``flip``, ``seal`` or ``off`` (charter 258); anything else raises."""

    declared = str(settings.get("benchmark_counterparty", BENCHMARK_OFF) or "").strip().lower()
    if declared not in BENCHMARK_MODES:
        raise ValueError(f"proper_bet_benchmark_counterparty_unknown:{settings.get('benchmark_counterparty')!r}")
    return declared


BELIEF_WEATHER_P = "weather_p"
BELIEF_REALISATION_B = "realisation_b"


def belief_source(settings: Mapping[str, Any]) -> str:
    """``weather_p`` (charters 231/249: the served P0 vector) or
    ``realisation_b`` (charter 265: the realisation layer's belief with the
    latest-run change); anything else raises."""

    declared = str(settings.get("belief", BELIEF_WEATHER_P) or "").strip().lower()
    if declared not in {BELIEF_WEATHER_P, BELIEF_REALISATION_B}:
        raise ValueError(f"proper_bet_belief_unknown:{settings.get('belief')!r}")
    return declared


FRESH_BY_DECISION_AGE = "decision_age"
FRESH_BY_ENVELOPE_VALIDITY = "envelope_validity"
ENVELOPE_EXPIRED = "envelope_expired"


def freshness_rule(settings: Mapping[str, Any]) -> str:
    """What makes the decision a plan is made from fresh enough.

    ``decision_age`` (charter 231): made within ``first_decision_fresh_seconds``.
    ``envelope_validity`` (charter 249 (十), user ruling 2026-10-03 「按信封有效
    期」): the P0 envelope that decision read is still inside its own
    ``fresh_until_utc``.  P0 decisions more than a day out arrive in bursts
    with the model runs -- on 2026-10-03 the newest one was 3.7 h old on 35 of
    36 station-days of 10-05 while every envelope behind them had over an
    hour of validity left -- so the one-hour age let a plan be made only in
    the hour after a burst.  The same rule holds a re-post: a leg waits
    (``envelope_expired``) while the newest P0 envelope has run out.
    Anything else raises.
    """

    declared = str(settings.get("freshness", FRESH_BY_DECISION_AGE) or "").strip().lower()
    if declared not in {FRESH_BY_DECISION_AGE, FRESH_BY_ENVELOPE_VALIDITY}:
        raise ValueError(f"proper_bet_freshness_unknown:{settings.get('freshness')!r}")
    return declared


def _envelope_valid(envelope: Mapping[str, Any], now: datetime) -> bool:
    try:
        return _utc(str(envelope.get("fresh_until_utc") or "")) > now
    except (TypeError, ValueError):
        return False


def market_mode_bucket(q: Mapping[str, float], order: list[str]) -> str | None:
    """The market's modal bucket: the largest normalised mid, the lower rung on
    a tie -- the mode the far-tail lane measures its rungs from (charter 234)."""

    ladder = [bucket for bucket in order if bucket in q]
    if not ladder:
        return None
    return ladder[max(range(len(ladder)), key=lambda i: (float(q[ladder[i]]), -i))]


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
    *, sides: frozenset[str] = BOTH_SIDES, only_bucket: str | None = None,
) -> dict[str, Any]:
    """One bucket's leg: its side from ``s``, its rung, and the rule's verdict.

    The rule: a YES rung only under ``p``, a NO rung only under ``1 - p``.
    A leg that obeys it on a side not in ``sides`` reads ``side_stopped``
    (charter 235), and one off ``only_bucket`` reads ``not_market_mode``
    (charter 249); either way its rung is kept on the record.
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
    elif only_bucket is not None and bucket != only_bucket:
        out["reason"] = NOT_MARKET_MODE
    elif side not in sides:
        out["reason"] = f"{SIDE_STOPPED}:{side}"
    return out


def size_position(
    legs: list[dict[str, Any]],
    *,
    cash_usdc: float | None,
    min_shares: Mapping[str, float],
    stake_usdc: float | None = None,
) -> dict[str, Any]:
    """``x_k = lambda * |s_k|``, the whole position filled up to ``stake_usdc``.

    The smallest ``lambda`` the venue allows is ``max_k m_k / |s_k|`` (``m_k``
    the leg's venue minimum): the smallest leg sits exactly at its minimum,
    every other in ratio.  Charter 252 raises ``lambda`` until the position
    costs ``stake_usdc`` at the legs' prices (shares floored, so never more),
    and never lowers it under that minimum; no stake is the minimum itself
    (charter 231).  The whole position is taken when the cash carries it;
    otherwise the stake is cut to the cash, and when even that does not
    carry the minimum, the longest ``|s|``-ordered prefix whose own minimum
    the cash carries (adding a smaller ``|s|`` only raises ``lambda`` and the
    cost, so the affordable prefixes are an initial run).
    """

    ranked = sorted(
        (leg for leg in legs if leg.get("side") and leg.get("price") and not leg.get("reason")),
        key=lambda leg: (-abs(float(leg["s"])), str(leg["bucket"])),
    )

    def sized(
        prefix: list[dict[str, Any]], budget: float | None,
    ) -> tuple[float, list[dict[str, Any]], float]:
        lam = max(float(min_shares.get(leg["bucket"], DEFAULT_MIN_ORDER_SHARES)) / abs(float(leg["s"]))
                  for leg in prefix)
        if budget is not None:
            per_lambda = sum(float(leg["price"]) * abs(float(leg["s"])) for leg in prefix)
            if per_lambda > 0.0:
                lam = max(lam, float(budget) / per_lambda)
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
                "need_min_usdc": None, "budget_usdc": 0.0, "capped_by_cash": False}
    need_min = sized(ranked, None)[2]
    lam_full, full, need_full = sized(ranked, stake_usdc)
    if cash_usdc is None or need_full <= float(cash_usdc) + 1e-9:
        return {"lambda_shares": lam_full, "legs": full, "dropped": {}, "need_full_usdc": need_full,
                "need_min_usdc": need_min, "budget_usdc": need_full, "capped_by_cash": False}
    budget = float(cash_usdc) if stake_usdc is not None else None
    best: tuple[float, list[dict[str, Any]], float] | None = None
    for n in range(1, len(ranked) + 1):
        candidate = sized(ranked[:n], budget)
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
        "need_min_usdc": need_min,
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
    sides: frozenset[str] = BOTH_SIDES,
    buckets: str = BUCKETS_ALL,
    stake_usdc: float | None = None,
) -> dict[str, Any]:
    """The whole rule on one ladder: ``p``, ``q``, ``s``, ``D``, the rungs, lambda.

    ``buckets="market_mode"`` keeps only the leg on the market's own modal
    bucket (charter 249); every other bucket is still quoted, for the record.
    ``stake_usdc`` is the position's budget (charter 252, ``size_position``).
    """

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
    mode = market_mode_bucket(q, order)
    quotes = [
        leg_quote(b, p[b], s[b], rows.get(b), float(ticks.get(b) or rows.get(b, {}).get("price_tick_size") or 0.01),
                  sides=sides, only_bucket=mode if buckets == BUCKETS_MARKET_MODE else None)
        for b in order
    ]
    sizing = size_position(quotes, cash_usdc=cash_usdc, min_shares=min_shares, stake_usdc=stake_usdc)
    out = {
        "refusal": None,
        "p": p, "q": q, "s": s, "divergence_kl": divergence,
        "p_floored_buckets": floored,
        "mid_sum": market.get("mid_sum"),
        "market_mode_bucket": mode,
        "quotes": quotes,
        **sizing,
    }
    if not any(leg.get("price") and not leg.get("reason") for leg in quotes):
        # Legs that obeyed the rule on a stopped side say so, rather than
        # reading as a ladder the rule found nothing on.
        stopped = sorted({str(leg["side"]) for leg in quotes
                          if str(leg.get("reason") or "").startswith(SIDE_STOPPED + ":")})
        out["refusal"] = f"{SIDE_STOPPED}:{','.join(stopped)}" if stopped else "no_leg_obeys_rule"
    elif not sizing["legs"]:
        out["refusal"] = f"insufficient_cash:{float(cash_usdc or 0.0):.2f}<{sizing['need_min_usdc']:.2f}"
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
        "SELECT probabilities_json, content_sha256, decision_at_utc, stage,"
        " fresh_until_utc"
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
        "fresh_until_utc": str(row[4] or ""),
    }


def latest_stage_decisions(
    conn: sqlite3.Connection, *, now_utc: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """The NEWEST fact of every station-day on recent event days (charter 249).

    Its stage is the stage the station-day is in now, so a day that has left
    P0 is not offered.  The far-tail lane reads its window the same way.
    """

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='basket_policy_training_fact'"
    ).fetchone() is None:
        return []
    floor = (_utc(now_utc) - timedelta(days=int(lookback_days))).date().isoformat()
    rows = conn.execute(
        "SELECT f.station_id, f.event_date, f.stage, f.decision_at_utc"
        " FROM basket_policy_training_fact f"
        " JOIN (SELECT station_id, event_date, MAX(decision_at_utc) AS newest"
        "       FROM basket_policy_training_fact WHERE event_date >= ?"
        "       GROUP BY station_id, event_date) n"
        "   ON n.station_id = f.station_id AND n.event_date = f.event_date"
        "  AND n.newest = f.decision_at_utc"
        # ``rowid`` alone breaks a tie on the timestamp (insertion order): it is
        # in the index, where ``created_at_utc`` sits behind the fact's large
        # JSON columns and would read their overflow pages on every pass.
        " ORDER BY f.station_id, f.event_date, f.rowid DESC",
        (floor,),
    ).fetchall()
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for station, day, stage, decided in rows:
        seen.setdefault((str(station), str(day)), {
            "station_id": str(station), "event_date": str(day), "stage": str(stage),
            "decision_at_utc": str(decided),
        })
    return list(seen.values())


def served_p0_vector(
    conn: sqlite3.Connection, station_id: str, event_date: str,
    *, valid_at: datetime | None = None,
) -> dict[str, Any]:
    """The station-day's newest P0 envelope, normalised and floored as
    ``evaluate_ladder`` floors it.  ``state`` is ``ok``, ``no_p0``,
    ``envelope_unreadable`` or -- with ``valid_at``, past the envelope's own
    validity at that instant (charter 249 (十)) -- ``ENVELOPE_EXPIRED``.  The
    far-tail lane's model filter (charter 251) reads the same vector."""

    out: dict[str, Any] = {"state": "no_p0", "envelope_id": None, "content_sha256": None,
                           "decision_at_utc": None, "probabilities": {}}
    row = conn.execute(
        "SELECT probability_envelope_id FROM basket_policy_training_fact"
        " WHERE station_id=? AND event_date=? AND stage='P0'"
        " ORDER BY decision_at_utc DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        return out
    out["envelope_id"] = str(row[0])
    envelope = _envelope(conn, str(row[0]))
    if envelope is None:
        return {**out, "state": "envelope_unreadable"}
    out.update(content_sha256=envelope["content_sha256"], decision_at_utc=envelope["decision_at_utc"])
    if valid_at is not None and not _envelope_valid(envelope, valid_at):
        return {**out, "state": ENVELOPE_EXPIRED}
    try:
        raw = {str(k): float(v) for k, v in envelope["probabilities"].items()}
    except (TypeError, ValueError):
        return {**out, "state": "envelope_unreadable"}
    total = sum(raw.values())
    if not (total > 0.0 and math.isfinite(total)):
        return {**out, "state": "envelope_unreadable"}
    floored, _lifted = floor_probabilities({b: v / total for b, v in raw.items()})
    return {**out, "state": "ok", "probabilities": {b: float(v) for b, v in floored.items()}}


def _latest_p0_probability(
    conn: sqlite3.Connection, station_id: str, event_date: str, bucket: str,
    *, valid_at: datetime | None = None,
) -> tuple[float | str | None, dict[str, Any]]:
    """``p`` of one bucket in the station-day's newest P0 envelope, floored as
    ``evaluate_ladder`` floors it, and that envelope's identity; the number is
    None when there is none to read.  With ``valid_at`` (charter 249 (十)) an
    envelope past its own validity at that instant answers
    ``ENVELOPE_EXPIRED`` instead of a number."""

    served = served_p0_vector(conn, station_id, event_date, valid_at=valid_at)
    read = _envelope_read(served.get("envelope_id"), served.get("content_sha256"),
                          served.get("decision_at_utc"))
    if served["state"] == ENVELOPE_EXPIRED:
        return ENVELOPE_EXPIRED, read
    if served["state"] != "ok" or bucket not in served["probabilities"]:
        return None, read
    value = float(served["probabilities"][bucket])
    return (value if 0.0 < value < 1.0 else None), read


def _envelope_read(envelope_id: Any, sha256: Any, decision_at_utc: Any) -> dict[str, Any]:
    """Which envelope a rung's ``p`` was read from, as the rung records it."""

    return {"p_envelope_id": envelope_id, "p_envelope_sha256": sha256,
            "p_envelope_decision_at_utc": decision_at_utc}


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

    Read with the listing days (charter 48): this lane only bets more than
    36 hours out, so the day it prices has never started, and the started-
    day view gave it no live book at all -- every v2 first decision on
    2026-09-26 waited on a committed snapshot 130-140 s old against the
    60-second bound until it was an hour old (``first_decision_stale``).
    """

    candidates: list[dict[str, Any]] = []
    if live:
        book = live_ws_snapshot(
            conn, root, station_id=station_id, event_date=event_date, now_utc=now_utc,
            include_listing_days=True,
        )
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
    if envelope is not None and book is not None and reading:
        # Charter 254: the realisation layer's reading of this decision, in
        # shadow -- the lane's own leg (or, refused, the leg the rule looked
        # at) priced on the belief b.  Nothing here changes what the lane does.
        legs = [{"bucket": leg["bucket"], "side": leg["side"], "price": leg.get("planned_price"),
                 "fee_per_share": 0.0, "outcome": "planned"} for leg in ((plan or {}).get("legs") or [])]
        if not legs:
            mode = reading.get("market_mode_bucket")
            looked = next((x for x in reading.get("quotes") or []
                           if mode is not None and str(x.get("bucket")) == str(mode)), None)
            if looked is not None and looked.get("side"):
                legs = [{"bucket": looked["bucket"], "side": looked["side"], "price": looked.get("price"),
                         "fee_per_share": 0.0, "outcome": refusal}]
        realisation_store.record_legs(
            conn, lane=LANE, kind="plan", station_id=str(key["station_id"]), event_date=str(key["event_date"]),
            judged_at_utc=now_utc, hours_to_settlement=hours, book=book,
            p_raw=envelope.get("probabilities"), envelope_id=(fact or {}).get("envelope_id"), legs=legs,
        )
    return row


def _leg_key(leg: Mapping[str, Any]) -> str:
    return f"{leg['side']}:{leg['bucket']}"


# ---------------------------------------------------------------------------
# Charter 258: the benchmark cluster on the lane's buckets.


def market_ids(
    conn: sqlite3.Connection, station_id: str, event_date: str, bucket: str,
) -> dict[str, str]:
    """The bucket's YES token, NO token, condition id, tick and minimum off its newest
    registry row (the station-day index walked backwards, as
    ``certain_win_recycle._registry_rows`` reads it); the NO token and the
    condition id from the row's own record, as the plan reads them.  An
    empty string where the row does not say."""

    row = conn.execute(
        "SELECT yes_token, registry_json FROM basket_target_market_registry"
        " WHERE station_id=? AND event_date=? AND outcome=? ORDER BY rowid DESC LIMIT 1",
        (station_id, event_date, bucket),
    ).fetchone()
    try:
        raw = json.loads((row[1] if row else None) or "{}") or {}
    except ValueError:
        raw = {}
    return {"yes_token": str((row[0] if row else None) or ""), "no_token": str(raw.get("no_token") or ""),
            "condition_id": str(raw.get("condition_id") or ""),
            "tick_size": raw.get("order_price_min_tick_size"),
            "min_size": float(raw.get("order_min_size") or DEFAULT_MIN_ORDER_SHARES)}


def _seal_row(conn: sqlite3.Connection, station_id: str, event_date: str, bucket: str,
              side: str) -> dict[str, Any] | None:
    """The bucket's seal under the current criterion, or None."""

    cursor = conn.execute(
        f"SELECT * FROM {SEAL_TABLE} WHERE station_id=? AND event_date=? AND bucket=? AND side=?"
        " AND criterion=?",
        (station_id, event_date, bucket, side, benchmark_cluster.SEAL_CRITERION),
    )
    row = cursor.fetchone()
    return None if row is None else dict(zip([c[0] for c in cursor.description], row))


class BenchmarkView:
    """One pass's reading of the benchmark cluster (charter 258).

    ``verdict`` answers a leg -- ``{station_id, event_date, bucket, side,
    yes_token, no_token, condition_id}`` -- with ``clear``, ``sealed`` (and the
    seal row, written the first time) or ``unreadable``.  A sealed bucket is
    never read again; every other market is read at most once a pass, the
    ones ``prefetch`` is handed in a single read of all eight wallets."""

    def __init__(self, conn: sqlite3.Connection, *, now_utc: str, mode: str,
                 fetch: benchmark_cluster.Fetch | None = None) -> None:
        self.conn = conn
        self.now_utc = now_utc
        self.mode = mode
        self.fetch = fetch
        self.fills: dict[str, list[dict[str, Any]]] = {}
        self.unreadable: set[str] = set()
        self.reads: list[dict[str, Any]] = []
        self.sealed_new: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self.mode != BENCHMARK_OFF

    def prefetch(self, targets: list[Mapping[str, Any]]) -> None:
        if not self.enabled:
            return
        wanted = sorted({
            str(t.get("condition_id") or "") for t in targets
            if t.get("condition_id") and str(t["condition_id"]) not in self.fills
            and str(t["condition_id"]) not in self.unreadable
        })
        if not wanted:
            return
        read = benchmark_cluster.read_cluster(wanted, fetch=self.fetch)
        self.reads.append({"markets": len(wanted), "seconds": read["seconds"],
                           "unreadable": len(read["unreadable"]), "errors": read["errors"][:4]})
        for cond in wanted:
            if cond in read["unreadable"]:
                self.unreadable.add(cond)
            else:
                self.fills[cond] = list(read["fills"].get(cond) or [])

    def verdict(self, target: Mapping[str, Any], *, sealed_by: str) -> tuple[str, dict[str, Any] | None]:
        if not self.enabled:
            return "clear", None
        where = (str(target["station_id"]), str(target["event_date"]), str(target["bucket"]), str(target["side"]))
        seal = _seal_row(self.conn, *where)
        cond, no_token = str(target.get("condition_id") or ""), str(target.get("no_token") or "")
        if not cond or not no_token or not target.get("yes_token"):
            return "unreadable", None
        if cond not in self.fills and cond not in self.unreadable:
            self.prefetch([target])
        if cond in self.unreadable or cond not in self.fills:
            return "unreadable", None
        evidence = benchmark_cluster.opposite_evidence(
            self.fills[cond], side=where[3], yes_token=str(target["yes_token"]), no_token=no_token,
        )
        if evidence is None:
            # Not against the leg now: a seal of this criterion whose stance
            # has turned, or one an earlier criterion made, is lifted.
            position = benchmark_cluster.cluster_position(
                self.fills[cond], yes_token=str(target["yes_token"]), no_token=no_token)
            self.conn.execute(
                f"UPDATE {SEAL_TABLE} SET criterion=?, yes_shares=?, no_shares=?, paired_shares=?, net_yes=?,"
                " since_ts=?, lifted_at_utc=?"
                " WHERE station_id=? AND event_date=? AND bucket=? AND side=? AND COALESCE(criterion, '')<>?"
                " AND COALESCE(criterion, '') NOT LIKE 'lifted:%'",
                (SEAL_LIFTED, position["yes_shares"], position["no_shares"], position["paired_shares"],
                 position["net_yes"], position["since_ts"], self.now_utc, *where, SEAL_LIFTED),
            )
            return "clear", None
        if seal is not None and all(
            seal.get(k) == evidence[k] for k in ("yes_shares", "no_shares", "fill_ts", "since_ts")
        ):
            return "sealed", seal
        row = {
            "station_id": where[0], "event_date": where[1], "bucket": where[2], "side": where[3],
            "yes_token": str(target["yes_token"]), "no_token": no_token, "condition_id": cond,
            "sealed_at_utc": self.now_utc, "sealed_by": sealed_by,
            "wallet": evidence["wallet"], "fill_ts": evidence["fill_ts"], "fill_tx": evidence["fill_tx"],
            "fill_token_side": evidence["fill_token_side"], "fill_action": evidence["fill_action"],
            "fill_price": evidence["fill_price"], "fill_size": evidence["fill_size"],
            "implied_price": evidence["implied_price"], "opposite_fills": evidence["opposite_fills"],
            "criterion": evidence["criterion"], "yes_shares": evidence["yes_shares"],
            "no_shares": evidence["no_shares"], "paired_shares": evidence["paired_shares"],
            "net_yes": evidence["net_yes"], "since_ts": evidence["since_ts"], "lifted_at_utc": None,
        }
        if seal is not None:
            # Still sealed, the position moved: keep when it was sealed.
            row["sealed_at_utc"], row["sealed_by"] = seal["sealed_at_utc"], seal["sealed_by"]
        # An earlier criterion's row for the bucket is rewritten; the sell
        # step's own columns are left as they stand.
        keys = ("station_id", "event_date", "bucket", "side")
        self.conn.execute(
            f"INSERT INTO {SEAL_TABLE}({','.join(row)}) VALUES({','.join('?' for _ in row)})"
            f" ON CONFLICT({','.join(keys)}) DO UPDATE SET "
            + ",".join(f"{k}=excluded.{k}" for k in row if k not in keys),
            tuple(row.values()),
        )
        if seal is None:
            self.sealed_new.append({k: row[k] for k in (
                "station_id", "event_date", "bucket", "side", "sealed_by", "wallet", "fill_action",
                "fill_token_side", "fill_price", "fill_size", "implied_price", "opposite_fills",
                "yes_shares", "no_shares", "paired_shares", "net_yes", "since_ts")})
        return "sealed", row


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
    view: BenchmarkView | None = None,
) -> dict[str, Any]:
    rule_version = RULE_VERSIONS[str(cfg["rule"])]
    fresh_seconds = float(cfg["first_decision_fresh_seconds"])
    book_fresh = float(cfg["send_book_fresh_seconds"])
    min_hours = float(cfg["min_hours_to_settlement"])
    live = cfg.get("live_ws_book", True) is True
    stages = opening_stages(cfg)
    sides = allowed_sides(cfg)
    buckets = bucket_scope(cfg)
    stake = position_stake(cfg)
    entry = entry_rule(cfg)
    latest = entry == ENTRY_LATEST_DECISION
    by_envelope = freshness_rule(cfg) == FRESH_BY_ENVELOPE_VALIDITY
    belief = belief_source(cfg)
    now = _utc(now_utc)
    done = _evaluated(
        target_conn,
        since_event_date=(now - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date().isoformat(),
    )
    evaluations: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    decisions = (
        latest_stage_decisions(target_conn, now_utc=now_utc, lookback_days=DEFAULT_LOOKBACK_DAYS)
        if latest else
        stage_first_decisions(target_conn, now_utc=now_utc, lookback_days=DEFAULT_LOOKBACK_DAYS)
    )
    for key in decisions:
        if str(key["stage"]) not in stages:
            continue
        if (key["station_id"], key["event_date"], key["stage"]) in done:
            continue
        fact = _fact(target_conn, key)

        def refuse(reason: str, key: Mapping[str, Any] = key, fact: Mapping[str, Any] | None = fact,
                   **record: Any) -> None:
            # Charter 249: under the latest-decision entry a station-day is
            # judged again on the next pass, so only a refusal no later pass
            # can lift is written down (the row is the station-day's one
            # verdict); everything else waits and says what on.
            if latest and not reason.startswith(TERMINAL_REFUSAL_PREFIXES):
                waiting.append({**key, "reason": reason})
                return
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal=reason, **record))

        if fact is None:
            refuse("fact_missing")
            continue
        refusal, hours = _window_refusal(
            target_conn, station_id=key["station_id"], event_date=key["event_date"],
            settlement_timezone=fact["settlement_timezone"], now_utc=now_utc,
            min_hours=min_hours, gate=gate,
        )
        if refusal:
            refuse(refusal, hours=hours)
            continue
        if by_envelope:
            # Charter 249 (十): fresh while the envelope the decision read is
            # still valid, however long ago the decision was made.
            envelope = _envelope(target_conn, fact["envelope_id"])
            if envelope is None:
                refuse("envelope_missing", hours=hours)
                continue
            if not _envelope_valid(envelope, now):
                refuse(ENVELOPE_EXPIRED, hours=hours)
                continue
        else:
            young = (now - _utc(fact["decision_at_utc"])).total_seconds() <= fresh_seconds
            if not young:
                refuse("decision_stale" if latest else "first_decision_stale", hours=hours)
                continue
            envelope = _envelope(target_conn, fact["envelope_id"])
            if envelope is None:
                refuse("envelope_missing", hours=hours)
                continue
        book, book_refusal = _book(
            target_conn, manifest_root(), station_id=key["station_id"], event_date=key["event_date"],
            now_utc=now_utc, fresh_seconds=book_fresh, live=live,
        )
        if book is None:
            waiting.append({**key, "reason": book_refusal})
            continue
        vector = envelope["probabilities"]
        belief_read: dict[str, Any] = {"belief": belief}
        if belief == BELIEF_REALISATION_B:
            # Charter 265: s is read off b -- the market, the P0 vector this
            # decision read, and the latest-run change in hand now -- under a
            # fit the realisation gate admitted; no such b, no plan this pass.
            read = realisation_store.belief_vector(
                target_conn, station_id=key["station_id"], event_date=key["event_date"], as_of_utc=now_utc,
                p_raw=envelope["probabilities"], book_buckets=list(book["buckets"]),
            )
            if read.get("state") != "ok":
                waiting.append({**key, "reason": f"belief:{read.get('state')}"})
                continue
            vector = read["b"]
            belief_read.update(belief_fit_id=read.get("fit_id"), belief_change=read.get("change"),
                               belief_change_rule=read.get("change_rule"))
        if latest:
            # Most station-days fire nothing on most passes: read the rule
            # off the book before paying for the registry.
            glance = evaluate_ladder(vector, list(book["buckets"]), cash_usdc=None,
                                     min_shares={}, ticks={}, sides=sides, buckets=buckets)
            if glance.get("refusal"):
                detail = f":{glance.get('market_reason')}" if glance["refusal"] == "market_incomplete" else ""
                waiting.append({**key, "reason": f"{glance['refusal']}{detail}"})
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
                "condition_id": str(raw.get("condition_id") or ""),
                "accepting_orders": bool(row.get("accepting_orders")),
            }
        if "cash" not in cash:
            cash["cash"], cash["refusal"] = _free_cash(target_conn, now_utc=now_utc)
        if cash.get("refusal"):
            refuse(cash["refusal"], envelope=envelope, book=book, hours=hours)
            continue
        reading = evaluate_ladder(vector, list(book["buckets"]),
                                  cash_usdc=cash.get("cash"), min_shares=min_shares, ticks=ticks,
                                  sides=sides, buckets=buckets, stake_usdc=stake)
        if reading.get("refusal") == "market_incomplete":
            waiting.append({**key, "reason": f"market_incomplete:{reading.get('market_reason')}"})
            continue
        if reading.get("refusal"):
            refuse(reading["refusal"], envelope=envelope, book=book, reading=reading, hours=hours)
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
        benchmark: dict[str, str] = {}
        if view is not None and view.enabled:
            # Charter 258: no leg on a bucket the benchmark cluster has traded
            # the other side of, and none on one whose cluster read failed.
            kept: list[dict[str, Any]] = []
            for leg in legs:
                token = tokens.get(str(leg["bucket"])) or {}
                verdict, _seal = view.verdict({
                    "station_id": key["station_id"], "event_date": key["event_date"],
                    "bucket": leg["bucket"], "side": leg["side"], "yes_token": token.get(SIDE_YES),
                    "no_token": token.get(SIDE_NO), "condition_id": token.get("condition_id"),
                }, sealed_by="plan")
                if verdict == "clear":
                    kept.append(leg)
                else:
                    benchmark[str(leg["bucket"])] = (
                        BENCHMARK_OPPOSITE if verdict == "sealed" else BENCHMARK_UNREADABLE)
            legs = kept
            unsendable.update(benchmark)
        reading = {**reading, "legs": [l for l in reading["legs"] if str(l["bucket"]) not in unsendable],
                   "dropped": {**reading["dropped"], **unsendable}}
        if not legs and benchmark:
            # Not terminal: the market's mode can move to a bucket the cluster
            # has not traded against, and an unreadable read is retried.
            reason = BENCHMARK_OPPOSITE if BENCHMARK_OPPOSITE in benchmark.values() else BENCHMARK_UNREADABLE
            refuse(reason, envelope=envelope, book=book, reading=reading, hours=hours)
            continue
        if not legs:
            refuse("no_sendable_leg", envelope=envelope, book=book, reading=reading, hours=hours)
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
            "stake_usdc": stake,
            "budget_usdc": reading["budget_usdc"], "capped_by_cash": reading["capped_by_cash"],
            "divergence_kl": reading["divergence_kl"], "min_hours_to_settlement": min_hours,
            "sides": sorted(sides), "buckets": buckets, "entry": entry,
            "market_mode_bucket": reading.get("market_mode_bucket"), "legs": legs,
            **belief_read,
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
    view: BenchmarkView | None = None,
) -> dict[str, Any]:
    """Every active plan: close it if its window has, else price the rungs it
    still needs on the book that stands, each under the rule."""

    book_fresh = float(cfg["send_book_fresh_seconds"])
    min_hours = float(cfg["min_hours_to_settlement"])
    live = cfg.get("live_ws_book", True) is True
    sides = allowed_sides(cfg)
    horizon = rung_horizon_seconds(cfg)
    by_envelope = freshness_rule(cfg) == FRESH_BY_ENVELOPE_VALIDITY
    belief_reads: dict[tuple[str, str], dict[str, Any]] = {}
    rungs: list[dict[str, Any]] = []
    passes: list[dict[str, Any]] = []
    rows = target_conn.execute(
        f"SELECT station_id, event_date, stage, plan_json, legs_state_json, COALESCE(passes,0)"
        f" FROM {EVALUATION_TABLE} WHERE plan_state=?",
        (PLAN_ACTIVE,),
    ).fetchall()
    targets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    if view is not None and view.enabled:
        # Charter 258: every active leg's market, read in one pass of the
        # cluster -- a resting rung included, so its seal reaches the
        # resting-ladder pass that cancels it.
        for station, day, _stage, plan_json, _legs, _n in rows:
            for leg in json.loads(plan_json).get("legs") or []:
                targets[(str(station), str(day), str(leg["bucket"]), str(leg["side"]))] = {
                    "station_id": str(station), "event_date": str(day), "bucket": str(leg["bucket"]),
                    "side": str(leg["side"]), **market_ids(target_conn, str(station), str(day), str(leg["bucket"])),
                }
        view.prefetch(list(targets.values()))
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
        stopped: set[str] = set()
        sealed = False
        for leg in plan["legs"]:
            leg_state = state.setdefault(_leg_key(leg), {"intents": []})
            filled, open_rung = _leg_progress(target_conn, list(leg_state["intents"]))
            leg_state["filled_shares"] = filled
            remaining = float(leg["target_shares"]) - filled
            if remaining < float(leg["min_size"]) - 1e-9:
                leg_state["status"] = "filled" if filled > 0 else "done"
                continue
            if str(leg["side"]) not in sides:
                # Charter 235: a plan made before its side was stopped posts
                # no further rung on it; a rung already resting ends at its
                # own (minimum) horizon, and what filled is held.
                leg_state["status"] = f"{SIDE_STOPPED}:{leg['side']}"
                stopped.add(str(leg["side"]))
                continue
            target = targets.get((str(station), str(day), str(leg["bucket"]), str(leg["side"])))
            if target is not None and view is not None:
                verdict, _seal = view.verdict(target, sealed_by="rung")
                if verdict == "sealed":
                    # Charter 258: no further rung; a resting one is cancelled
                    # by the resting-ladder pass, what filled is the flip's.
                    leg_state["status"] = f"sealed:{BENCHMARK_OPPOSITE}"
                    sealed = True
                    continue
                if verdict == "unreadable" and not open_rung:
                    outstanding += 1
                    leg_state["status"] = f"waiting:{BENCHMARK_UNREADABLE}"
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
            p_now, s_now = float(leg["p"]), float(leg["s"])
            # The envelope this rung's ``p`` is read from: the plan's own,
            # until the re-post below takes its number from a newer one.
            p_read = _envelope_read(plan.get("envelope_id"), plan.get("envelope_sha256"),
                                    plan.get("envelope_decision_at_utc"))
            if plan.get("buckets") == BUCKETS_MARKET_MODE:
                # Charter 249: the leg is "YES on the market's modal bucket",
                # so every re-post asks the standing book which bucket that
                # is, and the newest P0 envelope what p is.
                market = market_reference_v1(book["buckets"])
                if market.get("state") != "complete":
                    leg_state["status"] = f"waiting:market_incomplete:{market.get('reason')}"
                    continue
                q_now = {str(k): float(v) for k, v in market["vector"].items()}
                by_bucket = {str(r.get("bucket")): r for r in book["buckets"]}
                mode = market_mode_bucket(
                    q_now, sorted(q_now, key=lambda b: by_bucket.get(b, {}).get("ordinal", 0)))
                if mode != str(leg["bucket"]):
                    leg_state["status"] = f"waiting:{NOT_MARKET_MODE}:{mode}"
                    continue
                newest, newest_read = _latest_p0_probability(
                    target_conn, str(station), str(day), str(leg["bucket"]),
                    valid_at=_utc(now_utc) if by_envelope else None,
                )
                if newest == ENVELOPE_EXPIRED:
                    leg_state["status"] = f"waiting:{ENVELOPE_EXPIRED}"
                    continue
                if newest is not None:
                    p_now = float(newest)
                    p_read = newest_read
                s_now = math.log(p_now / q_now[str(leg["bucket"])])
            elif plan.get("belief") == BELIEF_REALISATION_B:
                # Charter 265: every re-post reads b again -- the newest P0
                # envelope still valid, the standing book, the change in hand
                # now -- and the rung waits while b does not carry it.
                if (str(station), str(day)) not in belief_reads:
                    served = served_p0_vector(target_conn, str(station), str(day),
                                              valid_at=_utc(now_utc) if by_envelope else None)
                    if served["state"] != "ok":
                        belief_reads[(str(station), str(day))] = {"state": str(served["state"])}
                    else:
                        read = realisation_store.belief_vector(
                            target_conn, station_id=str(station), event_date=str(day), as_of_utc=now_utc,
                            p_raw=served["probabilities"], book_buckets=list(book["buckets"]),
                        )
                        market = market_reference_v1(book["buckets"])
                        read["q"] = ({str(k): float(v) for k, v in market["vector"].items()}
                                     if market.get("state") == "complete" else None)
                        read["p_read"] = _envelope_read(served.get("envelope_id"), served.get("content_sha256"),
                                                        served.get("decision_at_utc"))
                        belief_reads[(str(station), str(day))] = read
                read = belief_reads[(str(station), str(day))]
                if read.get("state") != "ok" or not read.get("q") or str(leg["bucket"]) not in read["b"]:
                    leg_state["status"] = f"waiting:belief:{read.get('state') if read.get('state') != 'ok' else 'market_incomplete'}"
                    continue
                p_now = float(read["b"][str(leg["bucket"])])
                s_now = math.log(p_now / read["q"][str(leg["bucket"])])
                p_read = read["p_read"]
            quote = leg_quote(str(leg["bucket"]), p_now, s_now, row, tick)
            if quote["reason"] or quote["side"] != leg["side"]:
                leg_state["status"] = f"waiting:{quote['reason'] or 'side_changed'}"
                leg_state["last_quote"] = {"price": quote["price"], "bid": quote["bid"], "ask": quote["ask"],
                                           "p": p_now}
                continue
            scale = 10 ** SHARE_PRECISION
            shares = math.floor(remaining * scale + 1e-6) / scale
            floor_usdc = EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC if CHARTER_83_MIN_NOTIONAL == "on" else 0.0
            if filled <= 0.0 and shares * float(quote["price"]) < floor_usdc - 1e-9:
                # The venue takes no order under a dollar (charter 83): a
                # five-share rung on a bucket priced under 0.20 is sized up
                # to the dollar rather than refused on every pass.
                shares = math.ceil(floor_usdc / float(quote["price"]) * scale - 1e-9) / scale
            cost = round(shares * float(quote["price"]), 6)
            if cash.get("cash") is not None and float(cash["cash"]) < cost - 1e-9:
                leg_state["status"] = f"waiting:insufficient_cash:{float(cash['cash']):.2f}<{cost:.2f}"
                continue
            if cash.get("cash") is not None:
                cash["cash"] = float(cash["cash"]) - cost
            until = now_utc
            if horizon > 0.0:
                # Charter 249 (九): the rung rests ``horizon`` seconds, so it is
                # on the book through a production cycle rather than for the
                # venue's minimum -- but never past the 36-hour line, and not
                # posted at all once less than the venue's own floor is left.
                room = (float(hours) - min_hours) * 3600.0 if hours is not None else horizon
                rest = min(horizon, room)
                if rest < VENUE_GTD_MINIMUM_HORIZON_SECONDS + GTD_SIGNING_MARGIN_SECONDS:
                    leg_state["status"] = "waiting:window_closing"
                    continue
                until = (_utc(now_utc) + timedelta(seconds=rest)).isoformat().replace("+00:00", "Z")
            venue = resting_order_fields(now_utc=now_utc, limit_price=float(quote["price"]),
                                         belief_fresh_until_utc=until)
            if horizon > 0.0:
                venue = {**venue, "expiration_basis": "rung_horizon_seconds"}
            n = len(leg_state["intents"])
            intent_id = f"{INTENT_PREFIX}_" + content_sha256({
                "plan_id": plan["plan_id"], "bucket": leg["bucket"], "side": leg["side"], "n": n,
            })[:32]
            leg_state["status"] = "posting"
            # Charter 254: the rung's reading by the realisation layer, in shadow.
            realisation_store.record_legs(
                target_conn, lane=LANE, kind="rung", station_id=str(station), event_date=str(day),
                judged_at_utc=now_utc, hours_to_settlement=hours, book=book,
                p_raw=realisation_store.envelope_probabilities(target_conn, p_read.get("p_envelope_id")),
                envelope_id=p_read.get("p_envelope_id"),
                legs=[{"bucket": leg["bucket"], "side": leg["side"], "price": float(quote["price"]),
                       "fee_per_share": 0.0, "outcome": "posting", "order_intent_id": intent_id}],
            )
            rungs.append({
                "where": where, "plan": plan, "leg": leg, "intent_id": intent_id,
                "price": float(quote["price"]), "shares": shares, "cost_usdc": cost,
                "bid": quote["bid"], "ask": quote["ask"], "tick": tick, "p": p_now, **p_read,
                "book_id": book.get("snapshot_id"), "book_source": book.get("book_source"),
                "hours_to_settlement": hours, **venue,
            })
        if outstanding == 0:
            target_conn.execute(
                f"UPDATE {EVALUATION_TABLE} SET legs_state_json=? WHERE station_id=? AND event_date=? AND stage=?",
                (json.dumps(state, ensure_ascii=False, sort_keys=True), *where),
            )
            if sealed:
                close(BENCHMARK_OPPOSITE)
            elif stopped:
                close(f"{SIDE_STOPPED}:{','.join(sorted(stopped))}")
            else:
                close("all_legs_filled", final=PLAN_DONE)
            continue
        target_conn.execute(
            f"UPDATE {EVALUATION_TABLE} SET legs_state_json=?, passes=?, last_pass_at_utc=?"
            " WHERE station_id=? AND event_date=? AND stage=?",
            (json.dumps(state, ensure_ascii=False, sort_keys=True), int(n_passes) + 1, now_utc, *where),
        )
        passes.append({"station_id": station, "event_date": day, "outstanding_legs": outstanding})
    return {"rungs": rungs, "passes": passes}


# ---------------------------------------------------------------------------
# Charter 258: what the lane holds on the cluster's other side.


def _lane_positions(conn: sqlite3.Connection, *, now_utc: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Every bucket the lane holds or has an order out on, on station-days
    not yet closed, from the order store: per side the shares bought and
    sold and whether an order is still open, and the YES bought by the NO
    reversal of (五)."""

    floor = (_utc(now_utc) - timedelta(days=SELL_LOOKBACK_DAYS)).date().isoformat()
    rows = conn.execute(
        "SELECT c.station_id, c.event_date, c.settlement_timezone, i.order_intent_id, i.bucket, i.side,"
        " i.order_action, i.state, COALESCE(r.state, ''), COALESCE(r.filled_size, 0)"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_execution_cycles c ON c.cycle_id=i.cycle_id"
        " LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE i.decision_id >= ? AND i.decision_id < ? AND c.event_date >= ?",
        (f"{LANE}_decision_", f"{LANE}_decision`", floor),
    ).fetchall()
    held: dict[tuple[str, str, str], dict[str, Any]] = {}
    closed: dict[tuple[str, str], bool] = {}
    for station, day, tz, intent_id, bucket, side, action, state, rec_state, filled in rows:
        if (station, day) not in closed:
            try:
                closed[(station, day)] = settlement_day_state(
                    str(day), as_of_utc=now_utc, settlement_timezone=str(tz)).get("state") == "closed"
            except Exception:  # noqa: BLE001 -- the step's own window check reports it
                closed[(station, day)] = False
        if closed[(station, day)]:
            continue
        entry = held.setdefault((str(station), str(day), str(bucket)), {
            "station_id": str(station), "event_date": str(day), "bucket": str(bucket),
            "settlement_timezone": str(tz),
            SIDE_YES: {"bought": 0.0, "sold": 0.0, "open": False},
            SIDE_NO: {"bought": 0.0, "sold": 0.0, "open": False},
            "reversal_yes": 0.0,
        })
        leg = entry[SIDE_NO if str(side).upper() == SIDE_NO else SIDE_YES]
        leg["bought" if str(action).upper() == "BUY" else "sold"] += float(filled or 0.0)
        if str(state) in OPEN_STATES and not str(rec_state).startswith("resolved_"):
            leg["open"] = True
        if str(intent_id).startswith(REVERSE_YES_PREFIX):
            entry["reversal_yes"] += float(filled or 0.0)
    return {k: v for k, v in held.items()
            if any(v[s]["bought"] > 0.0 or v[s]["open"] for s in (SIDE_YES, SIDE_NO))}


def _best(levels: Any, *, highest: bool) -> tuple[float | None, float]:
    ladder = sorted(_levels(levels), reverse=highest)
    return (ladder[0][0], ladder[0][1]) if ladder else (None, 0.0)


def plan_sells(
    target_conn: sqlite3.Connection,
    *,
    now_utc: str,
    cfg: Mapping[str, Any],
    gate: Admission,
    manifest_root: Callable[[], Mapping[str, Any] | None],
    view: BenchmarkView,
    position_reader: Callable[[str], Mapping[str, Any]],
    cash: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every bucket the lane holds (or has an order out on), read against the
    cluster on each side it holds -- so a seal reaches a resting rung,
    whatever the mode.  Under ``sell``, one order a bucket a pass, never while
    one of the lane's own is still out on it:

    * a sealed YES -- the cluster net on NO -- is sold: a GTD limit at the
      YES bid while a lot the venue takes (five shares and a dollar) is held;
    * a sealed NO -- the cluster net on YES; only the NO the 01:54Z flip left
      (the lane buys no NO) -- is sold the same way at the NO bid, then the
      position is mirrored into YES by a FOK at the YES ask, until the YES
      bought this way covers the NO bought plus any NO too small to sell
      (user ruling 2026-10-09 「EHAM 和 WMKK卖掉」「并反向买入yes」)."""

    out: dict[str, Any] = {"orders": [], "sells": []}
    if not view.enabled:
        return out
    cash = {} if cash is None else cash
    held = _lane_positions(target_conn, now_utc=now_utc)
    targets = {key: {**entry, **market_ids(target_conn, *key)} for key, entry in held.items()}
    view.prefetch([{**t, "side": s} for t in targets.values() for s in (SIDE_YES, SIDE_NO)
                   if t[s]["bought"] > 0.0 or t[s]["open"]])
    book_fresh = float(cfg["send_book_fresh_seconds"])
    live = cfg.get("live_ws_book", True) is True
    books: dict[tuple[str, str], tuple[dict[str, Any] | None, str | None]] = {}
    for key in sorted(targets):
        target = targets[key]
        for side in (SIDE_YES, SIDE_NO):
            if not (target[side]["bought"] > 0.0 or target[side]["open"]):
                continue
            verdict, seal = view.verdict({**target, "side": side}, sealed_by="holding")
            if verdict != "sealed" or view.mode != BENCHMARK_SELL:
                continue
            order = _next_step(target_conn, target, side, seal, now_utc=now_utc, gate=gate,
                               manifest_root=manifest_root, books=books, book_fresh=book_fresh, live=live,
                               position_reader=position_reader, cash=cash, out=out,
                               reverse_no=str(cfg.get("rule")) != RULE_MAKER_BELIEF_B_V4)
            if order is not None:
                out["orders"].append(order)
    return out


def _next_step(
    conn: sqlite3.Connection, target: Mapping[str, Any], side: str, seal: Mapping[str, Any] | None, *,
    now_utc: str, gate: Admission, manifest_root: Callable[[], Mapping[str, Any] | None],
    books: dict[tuple[str, str], tuple[dict[str, Any] | None, str | None]], book_fresh: float, live: bool,
    position_reader: Callable[[str], Mapping[str, Any]], cash: dict[str, Any], out: dict[str, Any],
    reverse_no: bool = True,
) -> dict[str, Any] | None:
    """The one order this pass sends on a sealed side of a held bucket, or None
    (the reason is written on the seal row either way).

    ``reverse_no`` (charter 265): the YES mirror of a sealed NO belongs to the
    NO the 01:54Z flip left, when the lane bought no NO; under the v4 rule the
    lane buys NO on its own belief, and a sealed NO is sold like a sealed YES
    and nothing is bought back."""

    station, day, bucket = str(target["station_id"]), str(target["event_date"]), str(target["bucket"])
    own = target[side]
    held = max(float(own["bought"]) - float(own["sold"]), 0.0)
    progress: dict[str, Any] = {"side": side, "bought": round(float(own["bought"]), 4),
                                "sold": round(float(own["sold"]), 4), "held": round(held, 4)}
    if side == SIDE_NO and reverse_no:
        progress["reversal_yes"] = round(float(target["reversal_yes"]), 4)
    order: dict[str, Any] | None = None

    def settle(state: str) -> None:
        conn.execute(
            f"UPDATE {SEAL_TABLE} SET flip_state=?, flip_json=?, flip_updated_at_utc=?"
            " WHERE station_id=? AND event_date=? AND bucket=? AND side=?",
            (state, json.dumps({**progress, **({"order": order} if order else {})}, ensure_ascii=False,
                               sort_keys=True), now_utc, station, day, bucket, side),
        )
        out["sells"].append({"station_id": station, "event_date": day, "bucket": bucket, "state": state,
                             **progress})

    if target[SIDE_YES]["open"] or target[SIDE_NO]["open"]:
        settle("waiting:order_open")
        return None
    reversal_need = ((float(own["bought"]) + held - float(target["reversal_yes"]))
                     if side == SIDE_NO and reverse_no else 0.0)
    if held <= 0.0 and reversal_need < 1.0:
        settle("done" if float(own["bought"]) > 0.0 else "no_position")
        return None
    refusal, hours = _window_refusal(
        conn, station_id=station, event_date=day, settlement_timezone=str(target["settlement_timezone"]),
        now_utc=now_utc, min_hours=-math.inf, gate=gate,
    )
    if refusal:
        settle(f"closed:{refusal}" if refusal == "settlement_day_closed" else f"waiting:{refusal}")
        return None
    if (station, day) not in books:
        books[(station, day)] = _book(conn, manifest_root(), station_id=station, event_date=day,
                                      now_utc=now_utc, fresh_seconds=book_fresh, live=live)
    book, book_refusal = books[(station, day)]
    if book is None:
        settle(f"waiting:{book_refusal}")
        return None
    row = next((r for r in book["buckets"] if str(r.get("bucket")) == bucket), None)
    tick = max(float(target.get("tick_size") or (row or {}).get("price_tick_size") or 0.01), 0.01)
    digits = _tick_decimals(tick)
    yes_bid, yes_bid_size = _best((row or {}).get("bid_levels"), highest=True)
    yes_ask, yes_ask_size = (_best((row or {}).get("ask_levels"), highest=False)
                             if bool((row or {}).get("ask_available", True)) else (None, 0.0))
    # The side's own bid: the YES bid, or the NO bid the YES ask stands for.
    bid, bid_size = (yes_bid, yes_bid_size) if side == SIDE_YES else (
        (round(1.0 - yes_ask, digits), yes_ask_size) if yes_ask is not None else (None, 0.0))
    implied = yes_bid if side == SIDE_YES else yes_ask
    if implied is None:
        settle(f"waiting:no_{side.lower()}_bid")
        return None
    if not benchmark_cluster.OPPOSITE_IMPLIED_MIN <= implied <= benchmark_cluster.OPPOSITE_IMPLIED_MAX:
        # The bucket is all but decided: trading a winner at 0.99 or a loser
        # at 0.01 is nobody's view, the cluster's neither.
        settle(f"waiting:bucket_decided:{implied:.3f}")
        return None
    token = str(target["yes_token"] if side == SIDE_YES else target["no_token"])
    try:
        position = position_reader(token)
    except Exception as exc:  # noqa: BLE001 -- no wallet reading, no order
        position = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    if position.get("ok") is not True:
        settle(f"waiting:position_unreadable:{str(position.get('error') or '')[:60]}")
        return None
    wallet = float(position.get("shares") or 0.0)
    now_held = max(min(held, wallet), 0.0)
    progress.update(wallet=round(wallet, 4), held_now=round(now_held, 4), bid=bid, bid_size=bid_size,
                    yes_ask=yes_ask, yes_ask_size=yes_ask_size, hours_to_settlement=hours)
    min_size = float(target.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
    shares = math.floor(now_held * 100 + 1e-6) / 100
    base = {"station_id": station, "event_date": day, "bucket": bucket,
            "settlement_timezone": str(target["settlement_timezone"]), "tick": tick,
            "seal": {k: (seal or {}).get(k) for k in (
                "criterion", "yes_shares", "no_shares", "paired_shares", "net_yes", "since_ts", "wallet",
                "fill_ts", "fill_tx",
                "fill_action", "fill_token_side", "fill_price", "implied_price", "sealed_at_utc")}}
    if bid is not None and shares >= min_size and shares * bid >= EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC - 1e-9:
        if can_submit_next_order(conn, bucket=bucket, token_id=token).get("allowed") is not True:
            settle("waiting:same_bucket_pending")
            return None
        order = {"kind": f"sell_{side.lower()}", "side": side, "token_id": token, "price": bid, "shares": shares}
        settle(f"selling_{side.lower()}")
        return {**base, **order, "progress": dict(progress)}
    if side == SIDE_YES or not reverse_no:
        # What is left is under the venue's minimum: held to settlement.
        settle("done:below_venue_minimum" if now_held > 0.0 else "done")
        return None
    # 「并反向买入yes」: mirror the NO into YES, pairing any NO too small to sell.
    need = float(own["bought"]) + now_held - float(target["reversal_yes"])
    progress["reversal_need"] = round(need, 4)
    if need < 1.0:
        settle("done")
        return None
    if yes_ask is None:
        settle("waiting:no_yes_ask")
        return None
    usable = max(min(yes_ask_size * DEPTH_HEADROOM_FRACTION, yes_ask_size - 1.0), 0.0)
    want = max(math.ceil(need - 1e-9), min_size, math.ceil(EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC / yes_ask - 1e-9))
    lot = _sendable_shares(yes_ask, min(float(want), usable))
    if lot is None or lot < min_size:
        settle(f"waiting:thin_ask:{yes_ask_size:.2f}")
        return None
    cost = round(lot * yes_ask, 6)
    fee = round(lot * TAKER_FEE_RATE * yes_ask * (1.0 - yes_ask), 6)
    if cash.get("cash") is not None and float(cash["cash"]) < cost + fee - 1e-9:
        settle(f"waiting:insufficient_cash:{float(cash['cash']):.2f}<{cost + fee:.2f}")
        return None
    yes_token = str(target["yes_token"])
    if can_submit_next_order(conn, bucket=bucket, token_id=yes_token).get("allowed") is not True:
        settle("waiting:same_bucket_pending")
        return None
    if cash.get("cash") is not None:
        cash["cash"] = float(cash["cash"]) - cost - fee
    order = {"kind": "buy_yes", "side": SIDE_YES, "token_id": yes_token, "price": yes_ask, "shares": lot,
             "cost_usdc": cost, "fee_usdc_est": fee}
    settle("buying_yes")
    return {**base, **order, "progress": dict(progress)}


def send_sells(
    target_conn: sqlite3.Connection,
    orders: list[Mapping[str, Any]],
    *,
    now_utc: str,
    transport: Transport,
    live_submit_authorized: bool,
) -> list[dict[str, Any]]:
    """Send each order the pass planned and keep its answer on the seal."""

    receipts: list[dict[str, Any]] = []
    for order in orders:
        station, day, bucket = str(order["station_id"]), str(order["event_date"]), str(order["bucket"])
        seal_side = SIDE_NO if order["kind"] in ("sell_no", "buy_yes") else SIDE_YES
        body = {"schema": SELL_SCHEMA, "rule": "charter_258_benchmark_sell", "strategy_id": STRATEGY_ID,
                "kind": order["kind"], "seal": order["seal"], "progress": order["progress"]}
        stamp = content_sha256({"bucket": [station, day, bucket], "kind": order["kind"], "at": now_utc})[:32]
        if order["kind"] == "buy_yes":
            intent_id = f"{REVERSE_YES_PREFIX}_{stamp}"
            stage = _latest_stage(target_conn, station, day) or "P0"
            entry = send_lane_buy(
                target_conn, lane=LANE, intent_id=intent_id, station_id=station, event_date=day,
                settlement_timezone=str(order["settlement_timezone"]), bucket=bucket,
                token_id=str(order["token_id"]), price=float(order["price"]), shares=float(order["shares"]),
                side=SIDE_YES, execution_candidate=REVERSE_EXECUTION_CANDIDATE,
                fok_price_cap=float(order["price"]), tick_size=float(order["tick"]),
                intent_body={**body, "order_type": "FOK", "post_only": False},
                transport=transport, live_submit_authorized=live_submit_authorized,
                decision_details={
                    "schema": "proper_bet_lane_decision_v2", "lane": LANE, "title": LANE_TITLE,
                    "rule": body["rule"], "decision_signature": {"stage": stage, "lane": LANE, "rule": body["rule"]},
                    "forward_evidence_binding": {"decision_at_utc": now_utc, "probability_stage": stage},
                    "seal": order["seal"],
                },
            )
        else:
            intent_id = f"{SELL_PREFIX}_{stamp}"
            expiry = resting_order_fields(now_utc=now_utc, limit_price=float(order["price"]),
                                          belief_fresh_until_utc=now_utc)["expiration"]
            entry = send_exit_sell(
                target_conn, lane=LANE, intent_id=intent_id, station_id=station, event_date=day,
                settlement_timezone=str(order["settlement_timezone"]), bucket=bucket,
                token_id=str(order["token_id"]), price=float(order["price"]), shares=float(order["shares"]),
                order_type="GTD", post_only=False, expiration=int(expiry), tick_size=float(order["tick"]),
                outcome_side=str(order["side"]), transport=transport,
                live_submit_authorized=live_submit_authorized, intent_body=body,
            )
        send = {"order_intent_id": intent_id, "kind": order["kind"], "price": order["price"],
                "shares": order["shares"], "state": entry.get("state"), "event_type": entry.get("event_type"),
                "reason": entry.get("reason"), "matched_size": entry.get("matched_size"), "at": now_utc}
        row = target_conn.execute(
            f"SELECT flip_json FROM {SEAL_TABLE} WHERE station_id=? AND event_date=? AND bucket=? AND side=?",
            (station, day, bucket, seal_side),
        ).fetchone()
        state = json.loads((row[0] if row else None) or "{}")
        state["last_send"] = send
        target_conn.execute(
            f"UPDATE {SEAL_TABLE} SET flip_json=? WHERE station_id=? AND event_date=? AND bucket=? AND side=?",
            (json.dumps(state, ensure_ascii=False, sort_keys=True), station, day, bucket, seal_side),
        )
        receipts.append({"station_id": station, "event_date": day, "bucket": bucket, **send})
    return receipts


def benchmark_sealed_rung(conn: sqlite3.Connection, intent: Mapping[str, Any]) -> dict[str, Any] | None:
    """Charter 258, for the resting-ladder pass: the seal a resting rung of
    this lane sits on, or None.  Only the lane's own BUY rungs; the flip's
    orders are its own, and another lane's rung on the same bucket is not
    this ruling's."""

    intent_id = str(intent.get("order_intent_id") or "")
    if not intent_id.startswith(f"{INTENT_PREFIX}_") or intent_id.startswith((SELL_PREFIX, REVERSE_YES_PREFIX)):
        return None
    if str(intent.get("order_action") or "BUY").upper() != "BUY":
        return None
    try:
        mode = benchmark_mode({**PROPER_BET_DEFAULT_SETTINGS, **lane_settings(LANE)})
    except ValueError:
        mode = BENCHMARK_SEAL
    if mode == BENCHMARK_OFF or conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SEAL_TABLE,)
    ).fetchone() is None:
        return None
    return _seal_row(conn, str(intent.get("station_id") or ""), str(intent.get("event_date") or ""),
                     str(intent.get("bucket") or ""), str(intent.get("side") or SIDE_YES).upper())


def execute_benchmark_sells(
    target_conn: sqlite3.Connection,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Any = None,
    benchmark_fetch: benchmark_cluster.Fetch | None = None,
    position_reader: Callable[[str], Mapping[str, Any]] = default_position_reader,
) -> dict[str, Any]:
    """Charter 258's sell step alone, for a cycle in which the lane is paused
    (user ruling 2026-10-09 「是对标账号的对手盘的yes全部卖掉」): pausing the lane
    stops its plans and rungs, not the exit of a YES it holds on the cluster's
    other side.  ``benchmark_counterparty`` still decides: only ``sell`` sells."""

    ensure_lane_schema(target_conn)
    cfg = {**PROPER_BET_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    view = BenchmarkView(target_conn, now_utc=now_utc, mode=benchmark_mode(cfg), fetch=benchmark_fetch)
    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    live = cfg.get("live_ws_book", True) is True
    manifest: dict[str, Any] = {}

    def manifest_root() -> Mapping[str, Any] | None:
        if "root" not in manifest:
            manifest["root"] = _ws_manifest_root(ws_manifest_path) if live else None
        return manifest["root"]

    cash: dict[str, Any] = {}
    cash["cash"], cash["refusal"] = _free_cash(target_conn, now_utc=now_utc)
    if cash.get("refusal"):
        cash["cash"] = 0.0
    sells = plan_sells(target_conn, now_utc=now_utc, cfg=cfg, gate=gate, manifest_root=manifest_root,
                       view=view, position_reader=position_reader, cash=cash)
    sell_receipts = send_sells(target_conn, sells["orders"], now_utc=now_utc, transport=transport,
                               live_submit_authorized=live_submit_authorized)
    return {
        "schema": "proper_bet_benchmark_sell_receipt_v1",
        "lane": LANE,
        "state": "lane_paused_exits_only",
        "benchmark": {"mode": view.mode, "reads": view.reads, "sealed_new": view.sealed_new,
                      "sells": sells["sells"], "sell_orders": sell_receipts},
    }


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
    benchmark_fetch: benchmark_cluster.Fetch | None = None,
    position_reader: Callable[[str], Mapping[str, Any]] = default_position_reader,
) -> dict[str, Any]:
    """Judge new first decisions, then post every rung an active plan needs,
    then sell what charter 258 says the lane no longer holds."""

    ensure_lane_schema(target_conn)
    cfg = {**PROPER_BET_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    if str(cfg.get("rule")) not in RULE_VERSIONS:
        raise ValueError(f"proper_bet_rule_unknown:{cfg.get('rule')}")
    allowed_sides(cfg)
    bucket_scope(cfg)
    entry_rule(cfg)
    rung_horizon_seconds(cfg)
    freshness_rule(cfg)
    view = BenchmarkView(target_conn, now_utc=now_utc, mode=benchmark_mode(cfg), fetch=benchmark_fetch)
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
        manifest_root=manifest_root, cash=cash, view=view,
    )
    # A fresh cash read for the rungs: planning only reserved on paper.
    cash = {}
    cash["cash"], cash["refusal"] = _free_cash(target_conn, now_utc=now_utc)
    if cash.get("refusal"):
        cash["cash"] = 0.0
    pending = plan_rungs(target_conn, now_utc=now_utc, cfg=cfg, gate=gate,
                         manifest_root=manifest_root, cash=cash, view=view)
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
                "leg": leg, "rung": {k: rung[k] for k in ("price", "shares", "bid", "ask", "tick", "p",
                                                           "p_envelope_id", "p_envelope_sha256",
                                                           "p_envelope_decision_at_utc",
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
    # Charter 258: what the lane holds where the cluster is on the other side.
    sells = plan_sells(target_conn, now_utc=now_utc, cfg=cfg, gate=gate, manifest_root=manifest_root,
                       view=view, position_reader=position_reader, cash=cash)
    sell_receipts = send_sells(target_conn, sells["orders"], now_utc=now_utc, transport=transport,
                               live_submit_authorized=live_submit_authorized)
    return {
        "schema": "proper_bet_lane_receipt_v2",
        "lane": LANE,
        "title": LANE_TITLE,
        "rule": RULE_VERSIONS[str(cfg["rule"])],
        "settings": cfg,
        "evaluated": len(judged["evaluations"]),
        "planned": sum(1 for e in judged["evaluations"] if e.get("plan_json")),
        "waiting": len(judged["waiting"]),
        # What each waiting first decision waits on.  A decision waits
        # unrecorded (no book fresh enough, a ladder the market has not
        # completed) until it is judged or turns stale, so this count is the
        # only place the reason shows before the refusal does.
        "waiting_reasons": dict(sorted(Counter(
            str(item.get("reason") or "") for item in judged["waiting"]
        ).items())),
        "active_plans": sum(1 for p in pending["passes"] if "outstanding_legs" in p),
        "closed_plans": [p for p in pending["passes"] if "closed" in p],
        "rungs": len(pending["rungs"]),
        "submitted": submitted,
        "filled": filled,
        "refusals": sorted({str(e["refusal"]) for e in judged["evaluations"] if e.get("refusal")}),
        "receipts": receipts,
        "benchmark": {
            "mode": view.mode, "reads": view.reads, "sealed_new": view.sealed_new,
            "sells": sells["sells"], "sell_orders": sell_receipts,
        },
    }


__all__ = [
    "BENCHMARK_SELL",
    "BENCHMARK_OFF",
    "BENCHMARK_OPPOSITE",
    "BENCHMARK_SEAL",
    "BENCHMARK_UNREADABLE",
    "BenchmarkView",
    "SEAL_LIFTED",
    "SEAL_TABLE",
    "benchmark_mode",
    "benchmark_sealed_rung",
    "execute_benchmark_sells",
    "market_ids",
    "plan_sells",
    "send_sells",
    "BUCKETS_ALL",
    "BUCKETS_MARKET_MODE",
    "DEFAULT_RULE",
    "ENTRY_FIRST_DECISION",
    "ENTRY_LATEST_DECISION",
    "EVALUATION_TABLE",
    "EXECUTION_CANDIDATE",
    "LANE",
    "LANE_TITLE",
    "PLAN_ACTIVE",
    "PLAN_CLOSED",
    "PLAN_DONE",
    "RULE_MAKER_MODE_YES_V3",
    "RULE_MAKER_V2",
    "RULE_VERSIONS",
    "SIDE_NO",
    "SIDE_STOPPED",
    "SIDE_YES",
    "allowed_sides",
    "bucket_scope",
    "divergence_kl",
    "entry_rule",
    "latest_stage_decisions",
    "market_mode_bucket",
    "ensure_lane_schema",
    "evaluate_ladder",
    "execute_proper_bets",
    "leg_quote",
    "plan_new_first_decisions",
    "plan_rungs",
    "proper_position",
    "rung_horizon_seconds",
    "rung_price",
    "side_book",
    "size_position",
    "position_stake",
]
