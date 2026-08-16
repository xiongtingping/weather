#!/usr/bin/env python3
"""Paper-trade every station daily and score it after settlement.

What this answers: if the seat the strategy actually picks were bought with
real money, what would the book look like?  It is a measurement lane, not a
trading lane -- it authorizes nothing, submits nothing, and writes only its
own two tables.

Faithfulness is the whole point, so the selection is not re-implemented
here.  Each scope is priced from the same ``basket_official_ws_snapshot``
rows production reads, run through the same ``_support_windows`` ladder
enumeration, and seated on the same ordering: charter 35 establishes that
the curve score at optimal stake is the KL divergence between belief and
the market's implied odds, which is exactly the key the enumeration sorts
by, so the head of that list is the seat.

Each stage is scored as an independent entry rather than as one position
adjusted three times.  Production opens at P0 and adjusts at P0-M and P1
(charter 46), so a single running position would mix "was the P0 decision
good" with "was the adjustment good".  Scoring them separately answers both:
the P0 lane is the headline account, and the P0-M/P1 lanes say whether the
later evidence would have entered better.  Probability drift across the
stages is recorded on every row so the movement itself can be read.

Sizing is fractional Kelly on the basket as a binary bet.  A basket costing
C all-in and paying 1 when the truth lands inside it has Kelly fraction
(p - C)/(1 - C); the served fraction is a quarter of that (the same f the
live objective uses), capped per scope so no single station-day can take a
large bite of the book.

    python3 scripts/paper_trading_ledger.py --backfill   # score settled days
    python3 scripts/paper_trading_ledger.py --report     # print the book
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.contracts import (  # noqa: E402
    TargetBasketSnapshot,
)
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    probability_core,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.delta_v2_runtime import (  # noqa: E402
    _support_windows,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STAGES = ("P0", "P0-M", "P1")
INITIAL_CAPITAL_USDC = 1000.0
# The live objective's fractional Kelly.  A quarter of full Kelly is the
# stake that made the curve score concave enough to price variance without
# refusing bets that cannot hurt the book.
KELLY_FRACTION = 0.25
# No single station-day may take more than this share of the book, however
# large its Kelly number: 45 stations trading daily means a full-Kelly
# outlier would otherwise dominate the curve.
MAX_STAKE_FRACTION_PER_SCOPE = 0.02
MAXIMUM_SUPPORT_BUCKETS = 4
# The ladder floor (charter 50).  The ledger exists to reproduce what
# production seats, so it must carry production's structural rules; left at
# the function default of 1 it recorded single-bucket seats the live rule
# forbids.
MINIMUM_SUPPORT_BUCKETS = 3
FEE_RATE = 0.0  # all-in asks in the snapshot already carry the fee
# A fill must be priced against a book that existed around the decision.
# Without this the ledger reaches back to the hours just after listing, when
# a market is quoted at a cent with no size behind it, and manufactures an
# edge no live book ever offered: measured on the first run, quotes averaged
# 6.1 hours stale (max 17.8) and produced a 0.158 average cost against a
# 0.539 claimed win -- a 3.4x mispricing that does not exist.
MAX_QUOTE_STALENESS_MINUTES = 30.0
# Below this all-in cost a basket is almost certainly a dust offer on an
# empty side rather than a price anyone would fill.
DUST_COST_FLOOR = 0.01

_POSITION_DDL = """
CREATE TABLE IF NOT EXISTS paper_trade_position (
  position_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  decision_at_utc TEXT NOT NULL,
  quote_observed_at_utc TEXT NOT NULL,
  ladder_center_bucket TEXT NOT NULL,
  window_buckets_json TEXT NOT NULL,
  window_size INTEGER NOT NULL,
  win_probability REAL NOT NULL,
  all_in_cost REAL NOT NULL,
  edge REAL NOT NULL,
  curve_rank REAL,
  kelly_fraction REAL NOT NULL,
  stake_usdc REAL NOT NULL,
  shares REAL NOT NULL,
  probabilities_json TEXT NOT NULL,
  prices_json TEXT NOT NULL,
  center_c REAL,
  refused_window_count INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL,
  UNIQUE(station_id, event_date, stage)
)
"""
_SETTLEMENT_DDL = """
CREATE TABLE IF NOT EXISTS paper_trade_settlement (
  settlement_id TEXT PRIMARY KEY,
  position_id TEXT NOT NULL REFERENCES paper_trade_position(position_id),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL,
  settled_tmax_c REAL NOT NULL,
  settled_bucket TEXT NOT NULL,
  hit INTEGER NOT NULL CHECK(hit IN (0,1)),
  stake_usdc REAL NOT NULL,
  payout_usdc REAL NOT NULL,
  pnl_usdc REAL NOT NULL,
  claimed_win_probability REAL NOT NULL,
  center_error_c REAL,
  bucket_distance INTEGER,
  settled_at_utc TEXT NOT NULL,
  UNIQUE(station_id, event_date, stage)
)
"""


def _connect(db: Path, *, writable: bool) -> sqlite3.Connection:
    if writable:
        conn = sqlite3.connect(str(db), timeout=60.0)
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    if writable:
        # Never checkpoint from here.  The WAL on this database sits past the
        # 1000-page autocheckpoint threshold essentially always, so with the
        # default every one of this pass's commits would try to fold a
        # fifty-megabyte WAL into a ten-gigabyte database -- while the
        # resident, whose own lock patience is five seconds, is trying to
        # write.  That is how a read-only scorer took the P1 stage down with
        # ``database is locked``.  Appending to the WAL and letting the owner
        # of the database checkpoint on its own schedule costs nothing here.
        conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_POSITION_DDL)
    conn.execute(_SETTLEMENT_DDL)


def _truth(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Latest revision per station-day (charter 44)."""

    out: dict[tuple[str, str], float] = {}
    for station, day, value in conn.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth t"
        " WHERE revision_no = ("
        "   SELECT MAX(revision_no)"
        "   FROM calibration_station_daily_high_truth x"
        "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date)"
    ):
        out[(str(station), str(day))] = float(value)
    return out


def _stage_distributions(
    conn: sqlite3.Connection,
    station_id: str,
    event_date: str,
) -> dict[str, dict[str, Any]]:
    """Opening snapshot per stage, with its buckets and decision time.

    A stage is entered the first moment it can be: the earliest decision
    that has a book to trade against.  Taking the *last* decision instead
    dated every P0 entry to the close of its own event day -- WMKK's P0
    ran 205 times on 2026-08-15 and the last landed 23:36 local, against a
    book that had already resolved to 34C with every other bucket dumped
    at one tick.  Buying those is not an edge, it is reading the answer
    off the market and then claiming to have forecast it.
    """

    stages: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        row = None
        for candidate in conn.execute(
            "SELECT history_id, decision_at_utc, envelope_json"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND stage=?"
            " ORDER BY decision_at_utc ASC",
            (station_id, event_date, stage),
        ):
            if _quotes_at(
                conn, station_id, event_date, str(candidate[1])
            ) is not None:
                row = candidate
                break
        if row is None:
            continue
        buckets = conn.execute(
            "SELECT bucket, ordinal, lower_c, upper_c, probability"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=? ORDER BY ordinal",
            (row[0],),
        ).fetchall()
        if not buckets:
            continue
        lineage = (json.loads(row[2]) or {}).get("lineage") or {}
        stages[stage] = {
            "decision_at_utc": str(row[1]),
            "buckets": buckets,
            "center_bucket": str(lineage.get("model_center_bucket") or ""),
            "center_c": lineage.get("model_center_c"),
        }
    return stages


def _quotes_at(
    conn: sqlite3.Connection,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
) -> tuple[dict[str, dict[str, Any]], str] | None:
    """The last market snapshot at or before the decision.

    Pricing a decision on a book that did not exist yet is the one way a
    paper ledger flatters itself, so the cutoff is strict.
    """

    row = conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND observed_at <= ?"
        " ORDER BY observed_at DESC LIMIT 1",
        (station_id, event_date, decision_at_utc),
    ).fetchone()
    if row is None:
        return None
    try:
        quoted = datetime.fromisoformat(
            str(row[0]).replace("Z", "+00:00")
        )
        decided = datetime.fromisoformat(
            str(decision_at_utc).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    age_minutes = (decided - quoted).total_seconds() / 60.0
    if age_minutes > MAX_QUOTE_STALENESS_MINUTES:
        return None
    quotes: dict[str, dict[str, Any]] = {}
    for item in json.loads(row[1]):
        if not isinstance(item, dict):
            continue
        bucket = str(item.get("bucket") or "")
        if bucket:
            quotes[bucket] = item
    return (quotes, str(row[0])) if quotes else None


def _build_snapshot(
    *,
    station_id: str,
    event_date: str,
    observed_at: str,
    buckets: list[tuple],
    quotes: dict[str, dict[str, Any]],
) -> tuple[
    Any, dict[str, float], dict[str, float],
    dict[str, list[dict[str, float]]]
] | None:
    rows = []
    probabilities: dict[str, float] = {}
    prices: dict[str, float] = {}
    depth: dict[str, list[dict[str, float]]] = {}
    for bucket, ordinal, _lower, _upper, probability in buckets:
        quote = quotes.get(str(bucket))
        if quote is None:
            return None
        # The book itself is the only source for the offer.  Reading a
        # scalar ``best_ask`` beside it split the price in two: eight
        # stations never write that key, so every bucket of theirs was
        # scored unpriceable here while the kernel priced them from the
        # levels -- and the capacity guard below, which compares levels
        # against this map, then admitted the entire book at a price only
        # the top level had.  One source, and the guard binds again.
        levels = sorted(
            (
                {"price": float(l["price"]), "shares": float(l["shares"])}
                for l in (quote.get("ask_levels") or [])
                if l.get("price") is not None and l.get("shares") is not None
                and float(l["shares"]) > 0.0
            ),
            key=lambda l: l["price"],
        )
        ask = levels[0]["price"] if levels else None
        bid = quote.get("bid_price", quote.get("best_bid"))
        # A bucket with no offer is unpriceable, not a reason to drop the
        # station: the enumeration already refuses to rank a window that
        # contains one, so the remaining windows stay available.  Refusing
        # the whole scope here is what kept 2026-08-16 off the page after
        # production had opened it -- eleven buckets quoted, one of them
        # empty, and the ledger discarded all eleven.
        priceable = (
            ask is not None and bool(quote.get("ask_available", True))
        )
        # An unpriceable bucket still needs a representable book: the kernel
        # rejects an empty level list outright, so it carries a zero-size
        # level at the far end of the range, sized below any clip the sizer
        # can take -- nothing fills against it, and ask_available says so.
        tick_size = float(quote.get("price_tick_size") or 0.001)
        # Highest price still on this bucket's tick grid: unfillable in
        # practice, and the kernel requires both a positive depth and a
        # grid-aligned price, so it cannot simply be left empty.
        unfillable_ask = max(tick_size, 1.0 - tick_size)
        ask = float(ask) if priceable else unfillable_ask
        # No offer means nothing to buy, so the bid side is dropped with it:
        # the ledger only ever buys, and a standing bid beside a synthetic
        # ceiling ask would violate the kernel's bid<ask invariant.
        if not priceable:
            bid = 0.0

        bid = float(bid or 0.0)
        probabilities[str(bucket)] = float(probability)
        # Recorded for every bucket so downstream lookups are total; an
        # unpriceable one carries the ceiling, which no window can afford.
        prices[str(bucket)] = ask
        # Real depth, not an assumed slab.  A penny bucket whose whole book
        # is twelve shares must not be fillable for hundreds: that fiction
        # is precisely what turns a thin tail into an infinite-return
        # lottery on paper.
        ask_levels = levels or [
            {"price": unfillable_ask, "shares": tick_size}
        ]
        bid_levels = [
            {"price": float(l["price"]), "shares": float(l["shares"])}
            for l in (quote.get("bid_levels") or [])
            if l.get("price") is not None and l.get("shares") is not None
        ] if priceable else []
        depth[str(bucket)] = ask_levels
        rows.append({
            "bucket": str(bucket),
            "ordinal": int(ordinal),
            "probability": float(probability),
            "bid_price": bid,
            "bid_levels": bid_levels,
            "ask_levels": ask_levels,
            "price_tick_size": tick_size,
            "maker_queue_ahead_shares": 2.0,
            "maker_queue_known": True,
        })
    snapshot = TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "paper-" + content_sha256({
            "s": station_id, "e": event_date, "o": observed_at
        })[:24],
        "sequence": 1,
        "observed_at": observed_at,
        "market_id": "paper",
        "station_id": station_id,
        "event_date": event_date,
        "quote_version": "paper",
        "quote_observed_at": observed_at,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "paper",
        "order_state_version": "paper",
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "paper",
            "posterior_sha256": "a" * 64,
        },
        "buckets": rows,
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 1440.0,
    })
    return snapshot, probabilities, prices, depth


def _kelly_stake(
    *, win_probability: float, cost: float, equity: float
) -> tuple[float, float]:
    """Fractional Kelly for a basket paying 1 when the truth lands inside."""

    if cost <= 0.0 or cost >= 1.0:
        return 0.0, 0.0
    full = (win_probability - cost) / (1.0 - cost)
    if full <= 0.0:
        return 0.0, 0.0
    fraction = min(
        KELLY_FRACTION * full, MAX_STAKE_FRACTION_PER_SCOPE
    )
    return fraction, max(0.0, fraction * equity)


def _walk_ask(
    levels: list[dict[str, float]], shares: float
) -> float | None:
    """Cost of lifting ``shares`` off one bucket's offer ladder.

    ``None`` when the ladder does not hold that many.  The top level is a
    price for its own size and nothing more, so anything past it pays the
    next level up -- which is the whole difference between a book and a
    quote.
    """

    remaining = shares
    spent = 0.0
    for level in levels:
        take = min(remaining, level["shares"])
        spent += take * level["price"]
        remaining -= take
        if remaining <= 1e-9:
            return spent
    return None


def _fillable_size(
    *,
    legs: list[list[dict[str, float]]],
    win_probability: float,
    budget: float,
    fee_rate: float,
    capacity: float,
) -> tuple[float, float] | None:
    """Largest clip whose walked cost stays inside budget and edge.

    Both constraints are monotone in size -- spend rises with it, and so
    does the average price -- so the feasible sizes are an interval from
    zero and a bisection finds its end.
    """

    def spend(size: float) -> float | None:
        total = 0.0
        for leg in legs:
            leg_cost = _walk_ask(leg, size)
            if leg_cost is None:
                return None
            total += leg_cost
        return total * (1.0 + fee_rate)

    def feasible(size: float) -> float | None:
        cost = spend(size)
        if cost is None or cost > budget:
            return None
        # An all-in average at or above the payout is a guaranteed loss no
        # matter how the day settles, which is exactly what the top-of-book
        # price hid when the clip outran the level that quoted it.
        if cost / size >= win_probability:
            return None
        return cost

    if capacity <= 0.0 or feasible(min(capacity, 1e-6)) is None:
        return None
    low, high = 0.0, capacity
    for _ in range(60):
        mid = 0.5 * (low + high)
        if mid <= 0.0:
            break
        if feasible(mid) is None:
            high = mid
        else:
            low = mid
    cost = spend(low)
    if low <= 0.0 or cost is None:
        return None
    return low, cost


def _settled_bucket(
    buckets: list[tuple], truth_c: float
) -> tuple[str, int] | None:
    for bucket, ordinal, lower, upper, _probability in buckets:
        low = -1e9 if lower is None else float(lower)
        high = 1e9 if upper is None else float(upper)
        if low <= truth_c < high:
            return str(bucket), int(ordinal)
    return None


def backfill(
    conn: sqlite3.Connection,
    *,
    verbose: bool = False,
    recent_days: int | None = None,
) -> dict:
    ensure_schema(conn)
    truth = _truth(conn)
    # A day whose history and book are both closed cannot grow a new seat, and
    # re-deriving its refusals is what made the pass expensive: roughly two
    # hundred scopes that will answer "no positive kelly" or "no book" forever
    # were re-answered every run.  On a schedule that read is what the
    # resident's WAL checkpoint waits on.  ``recent_days`` bounds an
    # incremental pass to the window that can still change; a rebuild leaves
    # it None and walks everything, because that is the pass whose equity
    # actually compounds day over day.
    scopes = conn.execute(
        "SELECT DISTINCT station_id, event_date"
        " FROM basket_probability_bridge_history"
        " WHERE (?1 IS NULL OR event_date >= date("
        "   (SELECT max(event_date) FROM basket_probability_bridge_history),"
        "   '-' || ?1 || ' days'))"
        " ORDER BY event_date, station_id",
        (recent_days,),
    ).fetchall()
    counts: dict[str, int] = defaultdict(int)
    # Equity advances a day at a time, and every position opened on a day is
    # sized off the equity the book held when that day opened.  Compounding
    # within the day would let a station-day that settles first bankroll the
    # ones beside it, which no book can do -- they are all opened before any
    # of them resolve.
    equity = {stage: INITIAL_CAPITAL_USDC for stage in STAGES}
    pending_positions: list[tuple] = []
    pending_settlements: list[tuple] = []
    by_day: dict[str, list[str]] = defaultdict(list)
    for station_id, event_date in scopes:
        by_day[str(event_date)].append(str(station_id))
    for event_date in sorted(by_day):
        day_pnl: dict[str, float] = defaultdict(float)
        day_staked: dict[str, float] = defaultdict(float)
        for station_id in sorted(by_day[event_date]):
            # Ask the cheap question first.  Resolving a stage's opening
            # distribution walks its whole history looking for the earliest
            # decision that has a book, and doing that for scopes already
            # seated was most of the pass: a steady-state sweep spent the
            # better part of a minute re-deriving 268 answers it was about
            # to throw away.  On a schedule beside the resident that long
            # read is what a WAL checkpoint has to wait on, and the resident
            # gives up after five seconds -- so the wasted work was not just
            # slow, it fail-closed P0 and P1.
            seated = {
                str(row[0])
                for row in conn.execute(
                    "SELECT stage FROM paper_trade_position"
                    " WHERE station_id=? AND event_date=?",
                    (station_id, event_date),
                )
            }
            present = {
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT stage FROM basket_probability_bridge_history"
                    " WHERE station_id=? AND event_date=?",
                    (station_id, event_date),
                )
            }
            if present and present <= seated:
                counts["already_recorded"] += len(present)
                continue
            stages = _stage_distributions(conn, station_id, event_date)
            if not stages:
                continue
            for stage, payload in stages.items():
                if stage in seated:
                    counts["already_recorded"] += 1
                    continue
                priced = _quotes_at(
                    conn, station_id, event_date, payload["decision_at_utc"]
                )
                if priced is None:
                    counts["no_book_at_decision"] += 1
                    continue
                quotes, observed_at = priced
                built = _build_snapshot(
                    station_id=station_id,
                    event_date=event_date,
                    observed_at=observed_at,
                    buckets=payload["buckets"],
                    quotes=quotes,
                )
                if built is None:
                    counts["incomplete_book"] += 1
                    continue
                snapshot, probabilities, prices, depth = built
                center = payload["center_bucket"] or max(
                    probabilities, key=lambda b: probabilities[b]
                )
                if center not in probabilities:
                    center = max(probabilities, key=lambda b: probabilities[b])
                ordinals = {
                    row.bucket: int(row.ordinal)
                    for row in snapshot.buckets
                }
                core = probability_core(
                    probabilities,
                    ordinals=ordinals,
                    center_bucket=center,
                )
                # The production resolver converges its support to the cap
                # before anything downstream sees it; the raw core has no
                # such trim, and an over-wide one entered here as an
                # incumbent -- which is exempt from the cap -- and seated a
                # five-bucket basket the live rule forbids.  Trim from the
                # far end of the centre, which is the order the resolver
                # drops buckets in.
                if len(core) > MAXIMUM_SUPPORT_BUCKETS:
                    centre_ordinal = ordinals.get(center)
                    if centre_ordinal is None:
                        centre_ordinal = ordinals[
                            max(probabilities, key=lambda b: probabilities[b])
                        ]
                    core = frozenset(
                        sorted(
                            core,
                            key=lambda b: (
                                abs(ordinals[b] - centre_ordinal),
                                -probabilities.get(b, 0.0),
                            ),
                        )[:MAXIMUM_SUPPORT_BUCKETS]
                    )
                try:
                    windows, audit = _support_windows(
                        snapshot=snapshot,
                        # There is no incumbent here -- the ledger opens each
                        # station-day fresh -- so the evidence support is the
                        # mass-selected run the continuous-core resolver would
                        # produce, which is what probability_core computes.
                        # A bare centre made a one-bucket incumbent, and the
                        # incumbent is exempt from every floor, so the ledger
                        # seated windows the live rule refuses.
                        desired=set(core) or {center},
                        probabilities=probabilities,
                        fee_rate=FEE_RATE,
                        maximum_support_buckets=MAXIMUM_SUPPORT_BUCKETS,
                        minimum_support_buckets=MINIMUM_SUPPORT_BUCKETS,
                        center_bucket=center,
                    )
                except Exception:  # noqa: BLE001 -- a scope that cannot price is skipped
                    counts["selection_failed"] += 1
                    continue
                priced_windows = [
                    row for row in windows
                    if row.get("all_in_price") is not None
                    and row.get("edge") is not None
                ]
                if not priced_windows:
                    counts["no_priced_window"] += 1
                    continue
                seat = priced_windows[0]
                # Sized off the book as it stood when the day opened, and
                # never beyond the equity this day has left to deploy.
                equity_at_open = equity[stage]
                fraction, stake = _kelly_stake(
                    win_probability=float(seat["win_probability"]),
                    cost=float(seat["all_in_price"]),
                    equity=equity_at_open,
                )
                stake = min(
                    stake, max(0.0, equity_at_open - day_staked[stage])
                )
                if stake <= 0.0:
                    counts["no_positive_kelly"] += 1
                    continue
                # A complete basket buys the same share count on every leg
                # (atomicity), so the size is set by the thinnest ladder --
                # and the price by how deep into each the clip has to reach.
                # Charging the whole clip the top-of-book price is what let a
                # 30-share offer at one tick "fill" 24,213 shares at 0.004
                # when walking the ladders for that size costs 3.06 a set,
                # three times anything the basket can pay out.
                legs = [
                    depth.get(bucket) or [] for bucket in seat["buckets"]
                ]
                capacity = min(
                    (sum(level["shares"] for level in leg) for leg in legs),
                    default=0.0,
                )
                filled = _fillable_size(
                    legs=legs,
                    win_probability=float(seat["win_probability"]),
                    budget=stake,
                    fee_rate=FEE_RATE,
                    capacity=capacity,
                )
                if filled is None:
                    counts["no_depth"] += 1
                    continue
                shares, stake = filled
                # What the basket actually cost per set, which is the number
                # every downstream P&L and edge reading has to be built on.
                all_in_cost = stake / shares
                if all_in_cost > float(seat["all_in_price"]) + 1e-9:
                    counts["walked_book"] += 1
                day_staked[stage] += stake
                identity = {
                    "station_id": station_id,
                    "event_date": event_date,
                    "stage": stage,
                    "decision_at_utc": payload["decision_at_utc"],
                    "window": list(seat["buckets"]),
                    "stake_usdc": round(stake, 6),
                }
                position_id = "paper-pos-" + content_sha256(identity)[:24]
                pending_positions.append(
                    (
                        position_id, station_id, event_date, stage,
                        payload["decision_at_utc"], observed_at,
                        str(audit.get("ladder_center_bucket") or center),
                        json.dumps(list(seat["buckets"]), ensure_ascii=False),
                        len(seat["buckets"]),
                        float(seat["win_probability"]),
                        all_in_cost,
                        float(seat["win_probability"]) - all_in_cost,
                        seat.get("log_growth_rank"),
                        fraction, stake, shares,
                        json.dumps(probabilities, ensure_ascii=False),
                        json.dumps(prices, ensure_ascii=False),
                        payload.get("center_c"),
                        int(audit.get("center_refused_window_count") or 0),
                        content_sha256(identity),
                    )
                )
                counts["positions"] += 1
                actual = truth.get((station_id, event_date))
                if actual is None:
                    counts["open_unsettled"] += 1
                    continue
                landed = _settled_bucket(payload["buckets"], actual)
                if landed is None:
                    counts["truth_outside_book"] += 1
                    continue
                settled_bucket, settled_ordinal = landed
                hit = 1 if settled_bucket in set(seat["buckets"]) else 0
                payout = shares if hit else 0.0
                pnl = payout - stake
                ordinals = {
                    str(b): int(o) for b, o, _l, _u, _p in payload["buckets"]
                }
                distance = min(
                    abs(ordinals[b] - settled_ordinal)
                    for b in seat["buckets"]
                )
                center_error = None
                if payload.get("center_c") is not None:
                    center_error = float(payload["center_c"]) - actual
                pending_settlements.append(
                    (
                        "paper-set-" + content_sha256(
                            {**identity, "truth": actual}
                        )[:24],
                        position_id, station_id, event_date, stage,
                        actual, settled_bucket, hit, stake, payout, pnl,
                        float(seat["win_probability"]), center_error,
                        distance,
                        datetime.now(timezone.utc)
                        .isoformat().replace("+00:00", "Z"),
                    )
                )
                counts["settlements"] += 1
                day_pnl[stage] += pnl
        # The day is over: realise it, then let the next day size off the
        # book it actually leaves behind.
        for stage in STAGES:
            equity[stage] = max(0.0, equity[stage] + day_pnl[stage])
    # Every write in one short burst at the end.  The scan above reads most of
    # a ten-gigabyte database and takes about half a minute; holding the write
    # lock across it is what cost the resident whole cycles to
    # ``database is locked``, and the resident is right to give up fast --
    # "a concurrent maintenance writer must not consume a whole production
    # cadence" is its own stated rule.  Reads do not block it, so the only
    # thing that has to be brief is this, and a few dozen inserts are.
    if pending_positions:
        conn.executemany(
            "INSERT OR IGNORE INTO paper_trade_position ("
            " position_id, station_id, event_date, stage,"
            " decision_at_utc, quote_observed_at_utc,"
            " ladder_center_bucket, window_buckets_json, window_size,"
            " win_probability, all_in_cost, edge, curve_rank,"
            " kelly_fraction, stake_usdc, shares,"
            " probabilities_json, prices_json, center_c,"
            " refused_window_count, content_sha256"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            pending_positions,
        )
    if pending_settlements:
        conn.executemany(
            "INSERT OR IGNORE INTO paper_trade_settlement ("
            " settlement_id, position_id, station_id, event_date, stage,"
            " settled_tmax_c, settled_bucket, hit, stake_usdc,"
            " payout_usdc, pnl_usdc, claimed_win_probability,"
            " center_error_c, bucket_distance, settled_at_utc"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            pending_settlements,
        )
    conn.commit()
    return dict(counts)


def _fill_slice(rows: list) -> dict[str, Any]:
    """Headline numbers for one credibility slice of the settled book."""

    if not rows:
        return {"settled_count": 0}
    hits = sum(int(r[0]) for r in rows)
    staked = sum(float(r[1]) for r in rows)
    pnl = sum(float(r[3]) for r in rows)
    claimed = sum(float(r[4]) for r in rows) / len(rows)
    return {
        "settled_count": len(rows),
        "hit_count": hits,
        "hit_rate": hits / len(rows),
        "claimed_win_probability_mean": claimed,
        "calibration_ratio": (
            (hits / len(rows)) / claimed if claimed > 0 else None
        ),
        "staked_usdc": staked,
        "pnl_usdc": pnl,
        "return_on_stake": pnl / staked if staked > 0 else None,
        "equity_if_only_these_usdc": INITIAL_CAPITAL_USDC + pnl,
    }


def report(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_schema(conn)
    stages: dict[str, Any] = {}
    for stage in STAGES:
        rows = conn.execute(
            "SELECT s.hit, s.stake_usdc, s.payout_usdc, s.pnl_usdc,"
            " s.claimed_win_probability, s.center_error_c,"
            " s.bucket_distance, p.all_in_cost"
            " FROM paper_trade_settlement s"
            " JOIN paper_trade_position p ON p.position_id=s.position_id"
            " WHERE s.stage=? ORDER BY s.event_date",
            (stage,),
        ).fetchall()
        open_positions = conn.execute(
            "SELECT COUNT(*) FROM paper_trade_position p WHERE p.stage=?"
            " AND NOT EXISTS (SELECT 1 FROM paper_trade_settlement s"
            "  WHERE s.position_id=p.position_id)",
            (stage,),
        ).fetchone()[0]
        if not rows:
            stages[stage] = {
                "settled_count": 0, "open_positions": open_positions
            }
            continue
        hits = sum(int(r[0]) for r in rows)
        staked = sum(float(r[1]) for r in rows)
        pnl = sum(float(r[3]) for r in rows)
        claimed = sum(float(r[4]) for r in rows) / len(rows)
        errors = [float(r[5]) for r in rows if r[5] is not None]
        distances = [int(r[6]) for r in rows if r[6] is not None]
        equity = INITIAL_CAPITAL_USDC + pnl
        stages[stage] = {
            "settled_count": len(rows),
            "open_positions": open_positions,
            "hit_count": hits,
            "hit_rate": hits / len(rows),
            "claimed_win_probability_mean": claimed,
            "calibration_ratio": (
                (hits / len(rows)) / claimed if claimed > 0 else None
            ),
            "staked_usdc": staked,
            "pnl_usdc": pnl,
            "return_on_stake": pnl / staked if staked > 0 else None,
            "equity_usdc": equity,
            "return_on_capital": pnl / INITIAL_CAPITAL_USDC,
            "center_bias_c": (
                sum(errors) / len(errors) if errors else None
            ),
            "center_abs_error_c": (
                sum(abs(e) for e in errors) / len(errors)
                if errors else None
            ),
            "miss_distance_buckets_mean": (
                sum(distances) / len(distances) if distances else None
            ),
            # A basket bought for under a cent is almost always a bucket
            # offered at the minimum tick with nothing bid against it -- the
            # known one-sided tail quote, not a tradable price.  Such fills
            # dominate any P&L they appear in (a 0.001 leg returns 1000x), so
            # the book is reported with and without them rather than having
            # them silently set the headline.
            "credible_fill": _fill_slice(
                [r for r in rows if float(r[7]) >= DUST_COST_FLOOR]
            ),
            "dust_offer_fill": _fill_slice(
                [r for r in rows if float(r[7]) < DUST_COST_FLOOR]
            ),
        }
    return {
        "schema": "paper_trading_report_v1",
        "initial_capital_usdc": INITIAL_CAPITAL_USDC,
        "kelly_fraction": KELLY_FRACTION,
        "max_stake_fraction_per_scope": MAX_STAKE_FRACTION_PER_SCOPE,
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "stages": stages,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "discard recorded positions and settlements before backfilling."
            "  These rows are derived, so a structural rule change (the"
            " ladder floor, the dispersion cap) makes the old ones describe"
            " a strategy that no longer runs."
        ),
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=4,
        help=(
            "how far back an incremental --backfill looks, counted from the"
            " newest event day the bridge knows.  Older days have a closed"
            " history and a closed book, so re-reading them only costs the"
            " lock.  0 means no bound.  --rebuild always walks everything."
        ),
    )
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.recent_days is not None and args.recent_days <= 0:
        args.recent_days = None
    database = args.db.expanduser().resolve(strict=True)
    if args.backfill or args.rebuild:
        conn = _connect(database, writable=True)
        if args.rebuild:
            ensure_schema(conn)
            conn.execute("DELETE FROM paper_trade_settlement")
            conn.execute("DELETE FROM paper_trade_position")
            conn.commit()
        counts = backfill(
            conn,
            recent_days=None if args.rebuild else args.recent_days,
        )
        print(json.dumps(
            {"schema": "paper_trading_backfill_v1", "counts": counts},
            ensure_ascii=False, sort_keys=True,
        ))
        if args.report:
            print(json.dumps(report(conn), ensure_ascii=False, sort_keys=True))
        return 0
    conn = _connect(database, writable=False)
    print(json.dumps(report(conn), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
