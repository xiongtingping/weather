#!/usr/bin/env python3
"""Paper-trade every station daily and score it after settlement.

What this answers: if the seat the strategy actually picks were bought with
real money, what would the book look like?  It is a measurement lane, not a
trading lane -- it authorizes nothing, submits nothing, and writes only its
own two tables.

Faithfulness is the whole point, so the selection is not re-implemented
here.  Each scope is priced from the same ``basket_official_ws_snapshot``
rows production reads, run through the same ``_support_windows`` ladder
enumeration, and seated on the same ordering -- whatever that ordering
currently is.  It was the KL divergence between belief and the market's
implied odds until 2026-08-17, when the measured cost of ranking on any
belief-minus-price quantity retired it in favour of probability mass; the
head of the list is the seat either way, and this module deliberately does
not know which key produced it.

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
import os
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
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
    CENTRE_ANCHOR_DISTANCE_DECAY,
    CENTRE_ANCHOR_EDGE_TILT,
    CENTRE_ANCHOR_MIN_RUNG_SHARE,
    MINIMUM_LEG_ALL_IN_ASK,
    P1_MINIMUM_SUPPORT_BUCKETS,
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
# How many distinct books one stage may be offered before the pass gives up on
# it for this sweep.  Production re-decides on every model update and opens the
# moment its objective says yes, so the ledger walks the same sequence; the cap
# only bounds the read for a scope that will refuse all day, and the next sweep
# starts the walk again from the top.
MAX_ENTRY_CYCLES_PER_STAGE = 240
# Which bucket-selection rule this sweep runs.  ``live`` is production's own
# ``_support_windows`` ordering; the rest exist so a strategy question can be
# answered against real ask depth and the kernel's own fill bound instead of
# a notebook.  Four separate simulations of the live strategy returned
# -17.2%, +24.3%, +73.5% and +146% against this ledger's -0.2% -- all wrong
# the same way, because a penny leg's payout dominates any P&L it enters.
WINDOW_STRATEGY = (
    os.environ.get("PAPER_LEDGER_WINDOW_STRATEGY", "live").strip() or "live"
)
# How a chosen basket is assumed to be filled.  ``taker`` crosses the spread
# and is the only one that is not a hypothesis: it is what the live path
# does and what every number before 2026-08-17 was measured under.
#
# The maker modes answer "what if we posted instead", and they are an upper
# bound, not a forecast.  A resting bid fills only when someone chooses to
# sell into it, which is adverse by construction -- the fills you get are
# the ones the other side wanted to give you.  Nothing in a depth snapshot
# says which of our orders would have filled, so these runs assume all of
# them do and should be read as "the best this could possibly be worth".
# They are worth measuring because the spread is large: 27.3% of ask at the
# median, and the whole of the book's 8.6% vig.
EXECUTION_MODE = (
    os.environ.get("PAPER_LEDGER_EXECUTION", "maker_resting").strip()
    or "maker_resting"
)
EXECUTION_MODES = (
    "taker", "maker_bid", "maker_improve", "mid", "maker_resting",
)
# How long a resting order is left on the book before the basket gives up
# on it.  The probe's four horizons top out at an hour because it is
# measuring how fill rate grows; a basket entering at listing has until the
# day resolves, and the ruling is to let it wait (user ruling, 2026-08-17).
MAKER_RESTING_HORIZON_MINUTES = float(
    os.environ.get("PAPER_LEDGER_MAKER_HORIZON_MIN", "360") or 360.0
)
# A resting leg posts across a band, not at a point (user ruling,
# 2026-08-17).  The probe measured the two ends 14x apart on fill rate --
# joining the bid filled 5.8% in five minutes, a tick under the ask filled
# 82.8% -- and a single price has to pick one of those and live with it.
# A ladder takes both: the cheap rungs save the spread when the market
# comes all the way down, the dear rungs make sure the leg fills at all.
#
# The band runs from one tick above the bid (the best price worth waiting
# for) to one tick below the ask (the dearest price that is still a maker
# order rather than a crossing trade).  Size is split evenly, because
# weighting it toward the cheap end reintroduces the same "hope the market
# collapses" bet the shape work just removed.
MAKER_RESTING_IMPROVE_TICKS = 1
MAKER_RESTING_LADDER_STEPS = int(
    os.environ.get("PAPER_LEDGER_MAKER_LADDER_STEPS", "5") or 5
)
# How the stake is split across the basket's rungs.  ``equal_shares`` is
# what the ledger has always done -- one share count for the whole basket,
# which is what atomicity means for a complete basket and is why allocation
# shape never reached these numbers.  ``centre_tilt`` reproduces
# production's ``_centre_anchored_candidates``: a base holding on the centre
# rung, leaned toward positive edge, with the far rungs floored as hedges.
ALLOCATION_SHAPE = (
    os.environ.get("PAPER_LEDGER_ALLOCATION", "centre_tilt").strip()
    or "centre_tilt"
)
ALLOCATION_SHAPES = ("equal_shares", "centre_tilt")
# Every structural rule a recorded seat depends on.  Positions are derived rows,
# so a change to any of these makes the ones already in the table describe a
# strategy that no longer runs -- the ladder floor landed on 2026-08-15 and the
# five two-bucket P0 seats written before it stayed on the page for a day,
# because the pass skips a stage it has already recorded.  Rows stamped with a
# different rule set are dropped and re-derived instead.
# The fingerprint identifies a rule set exactly; this names it in a way a
# reader can hold.  The page shows one per basket, so a day compiled under a
# retired rule is visibly different from a day compiled under the current
# one rather than silently comparable to it.  Kept short and mechanical --
# it is derived from the same constants the fingerprint hashes, so the two
# cannot drift.
SELECTION_RULE_LABEL = "·".join(filter(None, (
    {"probability_mass": "D-质量", "kl": "K-散度"}.get(
        "probability_mass", "probability_mass"
    ),
    "整篮EV",
    "中心底仓",
    f"腿≥{MINIMUM_LEG_ALL_IN_ASK:g}",
    None if WINDOW_STRATEGY == "live" else WINDOW_STRATEGY,
    None if EXECUTION_MODE == "taker" else EXECUTION_MODE,
    None
    if EXECUTION_MODE != "maker_resting"
    else f"阶梯{MAKER_RESTING_LADDER_STEPS}档",
)))
SELECTION_RULE_SHA256 = content_sha256({
    "schema": "paper_trade_selection_rule_v4",
    "compounding": "credible_fills_only_from_settlements",
    "dust_cost_floor": DUST_COST_FLOOR,
    "entry": "first_cycle_whose_objective_seats",
    "pre_listing_decision": "first_book_after_listing",
    "fee_rate": FEE_RATE,
    # The ledger replays production's own ``_support_windows``, so a change
    # to how that ranks or filters is a change to what the ledger records --
    # and rows written under the old one describe a strategy that no longer
    # runs.  These two travel with the fingerprint for the same reason the
    # ladder floor does.
    "window_ranking": "probability_mass",
    # The label rides in the fingerprint after all.  Left out of it, a rule
    # change that only renames itself leaves old rows in place and the
    # equity curve compounds two rule sets together -- measured: 2026-08-17
    # seated LEMD at 16.88 (2% of 844) beside RKSI at 14.52 (2% of 726) on
    # the same day, because half the day was re-derived and half was not.
    "selection_rule_label": SELECTION_RULE_LABEL,
    "allocation_shape": ALLOCATION_SHAPE,
    "allocation_basis": "share_count",
    # The divisor of ``all_in_cost`` under weighted allocation.  It is a
    # recorded quantity, so changing how it is computed changes what every
    # row means -- and leaving it out of the fingerprint let a corrected
    # ledger keep serving rows derived under the old arithmetic, which is
    # how a "verification" ended up reading the numbers it was meant to
    # check.
    "cost_basis": "expected_payout_equivalent_shares",
    "window_strategy": WINDOW_STRATEGY,
    "execution_mode": EXECUTION_MODE,
    "maker_ladder_steps": MAKER_RESTING_LADDER_STEPS,
    "minimum_leg_all_in_ask": MINIMUM_LEG_ALL_IN_ASK,
    "initial_capital_usdc": INITIAL_CAPITAL_USDC,
    "kelly_fraction": KELLY_FRACTION,
    "max_quote_staleness_minutes": MAX_QUOTE_STALENESS_MINUTES,
    "max_stake_fraction_per_scope": MAX_STAKE_FRACTION_PER_SCOPE,
    "maximum_support_buckets": MAXIMUM_SUPPORT_BUCKETS,
    "minimum_support_buckets": MINIMUM_SUPPORT_BUCKETS,
    "p1_minimum_support_buckets": P1_MINIMUM_SUPPORT_BUCKETS,
    "stage_floor": "per_stage",
})
# Why a station-day has no seat, in the ledger's own words.  The page used to
# infer this from the absence of a row and could only ever say "the objective
# looked and did not sit" -- true of one of these and wrong about the other
# four, which are four different problems with four different owners.
REFUSAL_REASONS = (
    "no_book_at_decision",
    "incomplete_book",
    "selection_failed",
    "no_priced_window",
    # Our own floor, not the market's doing.  Folding it into
    # ``no_priced_window`` said "the market is dearer than the model" about
    # scopes where the market never got a say -- every window held a leg
    # quoted under a cent and we declined to price it.  The two have
    # different owners and different fixes, so they are different reasons.
    "leg_below_floor",
    # A resting basket that never filled whole.  Distinct from "no depth":
    # the size was there, the price simply never came to us.
    "resting_unfilled",
    "no_positive_kelly",
    "no_depth",
)

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
  selection_rule_label TEXT NOT NULL DEFAULT '',
  leg_shares_json TEXT NOT NULL DEFAULT '',
  resting_fill_json TEXT NOT NULL DEFAULT '',
  UNIQUE(station_id, event_date, stage)
)
"""
_REFUSAL_DDL = """
CREATE TABLE IF NOT EXISTS paper_trade_refusal (
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  reason TEXT NOT NULL CHECK(reason IN (%s)),
  detail TEXT NOT NULL,
  cycles_examined INTEGER NOT NULL,
  last_decision_at_utc TEXT NOT NULL,
  recorded_at_utc TEXT NOT NULL,
  PRIMARY KEY(station_id, event_date, stage)
)
""" % ",".join("'%s'" % reason for reason in REFUSAL_REASONS)
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
    conn.execute(_REFUSAL_DDL)
    # A new reason cannot be recorded through an existing CHECK, and
    # ``CREATE TABLE IF NOT EXISTS`` will not widen one.  Rebuild in place
    # when the live constraint is missing a reason this code can emit;
    # refusals are re-derived every sweep, so nothing is lost.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='paper_trade_refusal'"
    ).fetchone()
    live = str((row or [""])[0] or "")
    if live and any(f"'{name}'" not in live for name in REFUSAL_REASONS):
        conn.execute("DROP TABLE paper_trade_refusal")
        conn.execute(_REFUSAL_DDL)
        conn.commit()
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(paper_trade_position)")
    }
    if "leg_shares_json" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN leg_shares_json TEXT NOT NULL DEFAULT ''"
        )
    if "resting_fill_json" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN resting_fill_json TEXT NOT NULL DEFAULT ''"
        )
    if "selection_rule_label" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN selection_rule_label TEXT NOT NULL DEFAULT ''"
        )
    # The label is derived from the same constants the fingerprint hashes,
    # so a row already stamped with the current fingerprint was chosen under
    # the current label by definition -- backfill it rather than forcing a
    # re-derivation of every position to carry a descriptive string.  Rows
    # under any other fingerprint keep whatever they were written with, which
    # is exactly the distinction the page needs to draw.
    if "selection_rule_label" in columns:
        conn.execute(
            "UPDATE paper_trade_position SET selection_rule_label=?"
            " WHERE selection_rule_sha256=? AND"
            " (selection_rule_label IS NULL OR selection_rule_label='')",
            (SELECTION_RULE_LABEL, SELECTION_RULE_SHA256),
        )
        conn.commit()
    if "selection_rule_sha256" not in columns:
        # Rows written before the stamp existed were written under a rule set
        # nothing records, so they carry the empty string and the sweep below
        # re-derives them.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN selection_rule_sha256 TEXT NOT NULL DEFAULT ''"
        )


def _drop_superseded_positions(
    conn: sqlite3.Connection, *, recent_days: int | None
) -> int:
    """Discard seats recorded under a rule set that no longer runs.

    Only within the window this pass can re-derive: a row deleted outside it
    would leave the page emptier than the ledger, which is the opposite of what
    the stamp is for.
    """

    earliest = ""
    if recent_days is not None:
        floor = conn.execute(
            "SELECT date((SELECT max(event_date)"
            " FROM basket_probability_bridge_history), '-' || ?1 || ' days')",
            (recent_days,),
        ).fetchone()
        earliest = str((floor or [""])[0] or "")
    conn.execute(
        "DELETE FROM paper_trade_settlement WHERE position_id IN ("
        "  SELECT position_id FROM paper_trade_position"
        "  WHERE selection_rule_sha256<>? AND event_date>=?)",
        (SELECTION_RULE_SHA256, earliest),
    )
    cursor = conn.execute(
        "DELETE FROM paper_trade_position"
        " WHERE selection_rule_sha256<>? AND event_date>=?",
        (SELECTION_RULE_SHA256, earliest),
    )
    dropped = int(cursor.rowcount or 0)
    if dropped:
        conn.commit()
    return dropped


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


def _stage_entry_candidates(
    conn: sqlite3.Connection,
    station_id: str,
    event_date: str,
    stage: str,
):
    """Every book this stage was offered, oldest first.

    A stage is entered the first moment it *can* be, and the earliest decision
    with a book is only the first thing that could count as that moment -- not
    the only one.  Production re-decides on every model update (charter 46) and
    opens whenever its objective seats a window, so a listing-window book too
    wide to trade is a cycle it declines, not a day it forfeits.  Stopping at
    the first priced decision made the ledger forfeit: measured on 2026-08-17 it
    seated 12 of 43 stations, while walking the same sequence seats 23, and
    every one of the eleven it had been dropping was a later cycle of a station
    whose listing book alone was unaffordable -- ZGGG's whole book asked 2.09
    for a partition worth 1.00 at 21:15, and 0.50 for the seated four rungs
    nineteen minutes later.

    There is no hindsight in that: each cycle is priced against the book as it
    stood at that cycle, and the walk stops at the first one that seats rather
    than choosing among them.  Taking the *last* decision would be the cheat --
    WMKK's P0 ran 205 times on 2026-08-15 and the last landed 23:36 local,
    against a book that had already resolved to 34C.

    Consecutive decisions that resolve to the same book and the same
    distribution cannot answer differently, so they are yielded once.
    """

    seen: set[tuple] = set()
    for history_id, decided, envelope_json in conn.execute(
        "SELECT history_id, decision_at_utc, envelope_json"
        " FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND stage=?"
        " ORDER BY decision_at_utc ASC",
        (station_id, event_date, stage),
    ).fetchall():
        priced = _quotes_at(conn, station_id, event_date, str(decided))
        if priced is None:
            continue
        quotes, observed_at = priced
        buckets = conn.execute(
            "SELECT bucket, ordinal, lower_c, upper_c, probability"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=? ORDER BY ordinal",
            (history_id,),
        ).fetchall()
        if not buckets:
            continue
        signature = (
            observed_at,
            tuple(
                (str(row[0]), round(float(row[4]), 6)) for row in buckets
            ),
        )
        if signature in seen:
            continue
        seen.add(signature)
        lineage = (json.loads(envelope_json) or {}).get("lineage") or {}
        yield {
            "decision_at_utc": str(decided),
            "quotes": quotes,
            "observed_at": observed_at,
            "buckets": buckets,
            "center_bucket": str(lineage.get("model_center_bucket") or ""),
            "center_c": lineage.get("model_center_c"),
        }
        if len(seen) >= MAX_ENTRY_CYCLES_PER_STAGE:
            return


def _decode_quotes(payload: Any) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    try:
        rows = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return quotes
    for item in rows:
        if not isinstance(item, dict):
            continue
        bucket = str(item.get("bucket") or "")
        if bucket:
            quotes[bucket] = item
    return quotes


def _quotes_at(
    conn: sqlite3.Connection,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
) -> tuple[dict[str, dict[str, Any]], str] | None:
    """The last market snapshot at or before the decision.

    Pricing a decision on a book that did not exist yet is the one way a
    paper ledger flatters itself, so the cutoff is strict.

    With one exception, and it is the opening stage's whole point.  P0
    decides before the market lists -- that is what an opening stage is --
    and charter 48 seats it at listing.  Requiring a book that predates the
    decision made those scopes permanently unseatable: of 227 P0 refusals
    for "no book at decision", 153 had a book, just minutes later.  EPWA
    2026-07-31 decided at 22:24 against a market that opened at 22:26 and
    was recorded as having no market at all.

    So a decision with nothing behind it may use the *first* book that
    appears after it.  Not any later book -- the first, and only when
    nothing earlier exists.  Waiting for a better price is the cheat this
    guard exists to stop; waiting for the market to open is not, and the
    entry walk still has to find a cycle whose objective actually seats.
    """

    row = conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND observed_at <= ?"
        " ORDER BY observed_at DESC LIMIT 1",
        (station_id, event_date, decision_at_utc),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT observed_at, buckets_json"
            " FROM basket_official_ws_snapshot"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            "   AND observed_at > ?"
            " ORDER BY observed_at ASC LIMIT 1",
            (station_id, event_date, decision_at_utc),
        ).fetchone()
        if row is None:
            return None
        quotes = _decode_quotes(row[1])
        return (quotes, str(row[0])) if quotes else None
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
    quotes = _decode_quotes(row[1])
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


# The strategies the comparison sweep can put in production's place.  Each
# returns the same shape ``_support_windows`` does, so everything downstream
# -- the positive-EV gate, Kelly, the depth walk, settlement -- is unchanged
# and the only thing varying between runs is which buckets were chosen.
#
# Why these run through the ledger rather than a notebook: four separate
# simulations of the live strategy returned -17.2%, +24.3%, +73.5% and
# +146%, against the ledger's -0.2%.  Every one of them was wrong in the
# same direction, because a penny leg's payout dominates any P&L it enters
# and only the ledger walks real ask depth with the "average price under the
# win probability" bound the kernel enforces.  Strategy questions are
# answered here or not at all.
WINDOW_STRATEGIES = (
    "live",             # whatever _support_windows currently ranks first
    "mass4",            # widest window holding the centre, most mass (D)
    "mass3",
    "peak4",            # most-mass contiguous run, centre not required (C)
    "mu4",              # centred on the distribution mean (B)
    "centre1",          # the ladder centre alone (E)
    "mu1",              # the mean bucket alone (F)
    "leg_positive_ev",  # every bucket whose own p exceeds its own ask (G)
    "mass3_best_leg",   # take mass3's basket, then buy only its best p/c leg
    "mass4_best_leg",
)


def _resting_ladder(
    row: Any, steps: int = MAKER_RESTING_LADDER_STEPS
) -> list[float]:
    """The price band one leg rests across, cheapest first.

    Collapses to a single price when the spread has no room for a ladder --
    one tick of spread leaves nowhere to stand between the two sides.
    """

    if not row.ask_available:
        return []
    ask = float(row.ask_price)
    bid = float(getattr(row, "bid_price", 0.0) or 0.0)
    tick = float(getattr(row, "price_tick_size", 0.001) or 0.001)
    if bid <= 0.0 or ask <= 0.0:
        return []
    # A tick under the ask is the top of the band and also its hard ceiling:
    # at the ask the order is not resting, it is crossing, and the whole
    # point of this mode is to be paid the spread rather than pay it.  A
    # one-tick spread therefore collapses the band onto the bid.
    high = ask - tick
    low = min(bid + MAKER_RESTING_IMPROVE_TICKS * tick, high)
    if high < bid - 1e-9:
        return []
    if high <= low + tick / 2 or steps <= 1:
        return [round(low, 6)]
    span = high - low
    prices = [
        round(low + span * index / (steps - 1), 6) for index in range(steps)
    ]
    # Distinct rungs only: a band narrower than the tick grid collapses to
    # its own endpoints, and duplicated prices would double-count a fill.
    out: list[float] = []
    for price in prices:
        if not out or price > out[-1] + tick / 2:
            out.append(price)
    return out


def _execution_price(row: Any) -> float | None:
    """The per-share price this run assumes for one leg."""

    if not row.ask_available:
        return None
    ask = float(row.ask_price) * (1.0 + FEE_RATE)
    if EXECUTION_MODE == "taker":
        return ask
    bid = float(getattr(row, "bid_price", 0.0) or 0.0)
    if bid <= 0.0:
        # No resting bid to join or improve: a maker order here would be
        # alone on its side, which is the one-sided quote this ledger
        # already refuses to treat as a price.
        return None
    tick = float(getattr(row, "price_tick_size", 0.001) or 0.001)
    if EXECUTION_MODE == "maker_resting":
        # The band's own mean: what the leg pays if the whole ladder fills.
        # The realised price replaces this once the book has been walked --
        # this is the number the gate and the Kelly sizing see beforehand.
        rungs = _resting_ladder(row)
        if not rungs:
            return None
        price = sum(rungs) / len(rungs)
    elif EXECUTION_MODE == "maker_bid":
        price = bid
    elif EXECUTION_MODE == "maker_improve":
        price = min(bid + tick, ask)
    elif EXECUTION_MODE == "mid":
        price = (bid + ask) / 2.0
    else:
        raise ValueError(f"unknown execution mode: {EXECUTION_MODE}")
    return price * (1.0 + FEE_RATE)


def _maker_leg_prices(snapshot: Any, buckets: Sequence[str]) -> list[float]:
    """The assumed resting price for each leg, in the seat's own order."""

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    prices = []
    for bucket in buckets:
        row = by_bucket.get(str(bucket))
        price = None if row is None else _execution_price(row)
        # A leg with no price here cannot have been seated: the strategy
        # builder refuses those.  Falling back to the ask keeps the walk
        # well-formed if a caller ever gets here another way.
        prices.append(
            float(price)
            if price is not None
            else float(row.ask_price) * (1.0 + FEE_RATE)
        )
    return prices


def _window_row(
    buckets: Sequence[str],
    *,
    snapshot: Any,
    probabilities: Mapping[str, float],
) -> dict[str, Any] | None:
    """Price a chosen bucket set the way ``_support_windows`` prices one."""

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    ordered = [b for b in buckets if b in by_bucket]
    if not ordered:
        return None
    quotes = []
    for bucket in ordered:
        quote = _execution_price(by_bucket[bucket])
        if quote is None or quote < MINIMUM_LEG_ALL_IN_ASK:
            return None
        quotes.append(quote)
    win = sum(float(probabilities.get(b, 0.0)) for b in ordered)
    price = sum(quotes)
    return {
        "buckets": tuple(
            sorted(ordered, key=lambda b: by_bucket[b].ordinal)
        ),
        "win_probability": win,
        "all_in_price": price,
        "edge": win - price,
        "log_growth_rank": None,
        "is_incumbent": False,
    }


def _strategy_windows(
    strategy: str,
    *,
    snapshot: Any,
    probabilities: Mapping[str, float],
    center: str,
    ordinals: Mapping[str, int],
) -> list[dict[str, Any]]:
    """One candidate seat under the named strategy, priced and gated.

    Returned as a list so the caller's ``priced_windows[0]`` and its
    "nothing priced" refusal both keep working unchanged.
    """

    by_ordinal = sorted(probabilities, key=lambda b: ordinals[b])
    count = len(by_ordinal)
    if not count:
        return []
    index = {bucket: position for position, bucket in enumerate(by_ordinal)}
    mass = lambda seg: sum(  # noqa: E731
        float(probabilities.get(b, 0.0)) for b in seg
    )
    centre_at = index.get(center)
    if centre_at is None:
        centre_at = max(
            range(count), key=lambda j: probabilities.get(by_ordinal[j], 0.0)
        )
    mean_at = sum(
        j * float(probabilities.get(by_ordinal[j], 0.0)) for j in range(count)
    ) / max(sum(probabilities.values()), 1e-12)

    def contiguous(width: int, *, hold_centre: bool) -> list[str] | None:
        width = min(width, count)
        starts = range(0, count - width + 1)
        if hold_centre:
            starts = [
                a for a in starts if a <= centre_at < a + width
            ]
        best = None
        for start in starts:
            seg = by_ordinal[start:start + width]
            value = mass(seg)
            if best is None or value > best[0]:
                best = (value, seg)
        return None if best is None else best[1]

    if strategy == "mass4":
        chosen = contiguous(4, hold_centre=True)
    elif strategy == "mass3":
        chosen = contiguous(3, hold_centre=True)
    elif strategy == "peak4":
        chosen = contiguous(4, hold_centre=False)
    elif strategy == "mu4":
        low = max(0, min(int(round(mean_at - 1.5)), count - 4))
        chosen = by_ordinal[low:low + 4]
    elif strategy == "centre1":
        chosen = [by_ordinal[centre_at]]
    elif strategy == "mu1":
        chosen = [by_ordinal[max(0, min(int(round(mean_at)), count - 1))]]
    elif strategy in ("mass3_best_leg", "mass4_best_leg"):
        # The basket is chosen exactly as mass3/mass4 would, and has to clear
        # the whole-basket gate downstream -- but the money goes to one leg.
        # This separates "is this window the right region" from "is spreading
        # across it worth what it costs".
        width = 3 if strategy == "mass3_best_leg" else 4
        seg = contiguous(width, hold_centre=True)
        chosen = None
        if seg:
            by_bucket = {row.bucket: row for row in snapshot.buckets}
            ranked = []
            for bucket in seg:
                row = by_bucket.get(bucket)
                if row is None:
                    continue
                price = _execution_price(row)
                if price is None or price < MINIMUM_LEG_ALL_IN_ASK:
                    continue
                ranked.append(
                    (float(probabilities.get(bucket, 0.0)) / price, bucket)
                )
            if ranked:
                chosen = [max(ranked)[1]]
    elif strategy == "leg_positive_ev":
        # Every bucket the model prices above the market, contiguity not
        # required.  This is the one shape the live contract forbids, and
        # the reason to measure it here rather than argue about it.
        by_bucket = {row.bucket: row for row in snapshot.buckets}
        chosen = [
            bucket for bucket in by_ordinal
            if bucket in by_bucket
            and by_bucket[bucket].ask_available
            and (_execution_price(by_bucket[bucket]) or 9.9)
            >= MINIMUM_LEG_ALL_IN_ASK
            and float(probabilities.get(bucket, 0.0))
            > (_execution_price(by_bucket[bucket]) or 9.9)
        ]
    else:
        raise ValueError(f"unknown window strategy: {strategy}")
    if not chosen:
        return []
    row = _window_row(
        chosen, snapshot=snapshot, probabilities=probabilities
    )
    return [] if row is None else [row]




def _resting_ladder_fill(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    buckets: Sequence[str],
    ladders: Sequence[Sequence[float]],
    placed_at_utc: str,
) -> tuple[dict[str, list[float]], str | None]:
    """Which rungs of each leg's ladder the book traded through.

    A leg counts as filled when any rung fills; a basket needs every leg,
    because a complete basket is atomic.  Returns the filled prices per
    bucket so the cost is the ladder's own average rather than an assumed
    one.
    """

    deadline = (
        datetime.fromisoformat(str(placed_at_utc).replace("Z", "+00:00"))
        + timedelta(minutes=MAKER_RESTING_HORIZON_MINUTES)
    ).isoformat().replace("+00:00", "Z")
    pending = {
        str(bucket): sorted(prices, reverse=True)
        for bucket, prices in zip(buckets, ladders)
    }
    filled: dict[str, list[float]] = {b: [] for b in pending}
    for observed, payload in conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND observed_at > ? AND observed_at <= ?"
        " ORDER BY observed_at",
        (station_id, event_date, placed_at_utc, deadline),
    ):
        if not any(pending.values()):
            break
        quotes = _decode_quotes(payload)
        for bucket, rungs in pending.items():
            if not rungs:
                continue
            item = quotes.get(bucket)
            if not isinstance(item, Mapping):
                continue
            ask = item.get("best_ask")
            levels = item.get("ask_levels") or []
            if ask is None and levels:
                ask = levels[0].get("price")
            if ask is None:
                continue
            price = float(ask)
            # Every rung at or above where the book traded is filled.
            while rungs and price <= rungs[0] + 1e-9:
                filled[bucket].append(rungs.pop(0))
    missing = next(
        (bucket for bucket, prices in filled.items() if not prices), None
    )
    return filled, missing


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



def _centre_tilt_weights(
    buckets: Sequence[str],
    *,
    centre_bucket: str,
    probabilities: Mapping[str, float],
    prices: Sequence[float],
    ordinals: Mapping[str, int],
) -> list[float] | None:
    """Production's rung weights, recomputed here from the same inputs.

    Imported rather than duplicated would be better, but the runtime builds
    them inside a candidate generator that needs a live snapshot; the shape
    is three lines and the constants come from the module that owns them, so
    a drift shows up as an import error rather than as a silent divergence.
    """

    if centre_bucket not in ordinals:
        return None
    centre = ordinals[centre_bucket]
    edges = [
        float(probabilities.get(b, 0.0)) - price
        for b, price in zip(buckets, prices)
    ]
    best_edge = max(edges) if edges else 0.0
    weights = []
    for bucket, edge in zip(buckets, edges):
        decay = CENTRE_ANCHOR_DISTANCE_DECAY ** abs(ordinals[bucket] - centre)
        lean = 1.0 + CENTRE_ANCHOR_EDGE_TILT * (
            max(edge, 0.0) / best_edge if best_edge > 1e-12 else 0.0
        )
        weights.append(max(decay * lean, CENTRE_ANCHOR_MIN_RUNG_SHARE))
    if centre_bucket in buckets:
        index = list(buckets).index(centre_bucket)
        weights[index] = max(weights[index], 1.0)
    total = sum(weights)
    return None if total <= 0 else [w / total for w in weights]


def _fillable_weighted(
    *,
    legs: list[list[dict[str, float]]],
    weights: Sequence[float],
    probabilities: Sequence[float],
    budget: float,
    fee_rate: float,
) -> tuple[list[float], float] | None:
    """Largest weighted clip inside budget whose expected payout beats it.

    The equal-share path bounds itself with "average price under the win
    probability", which is the right test when every rung pays the same.
    Once rungs differ, the payout depends on which one lands, so the bound
    becomes the expectation itself: the basket must expect to return more
    than it costs, summed over the outcomes it holds.
    """

    # The weights size the *share count*, not the cash.  Weighting cash puts
    # the largest share count on the cheapest rung -- shares are cash over
    # price -- so a basket meant to hold its centre ends up holding its tail:
    # measured on ZGSZ 2026-08-17, 4.14 USDC on 30C bought 207 shares while
    # 6.51 on the centre bought 50, and the payout curve peaked two rungs out.
    # Sizing shares directly makes the centre the position and lets price
    # decide what that costs.
    def attempt(scale: float) -> tuple[list[float], float, float] | None:
        shares: list[float] = []
        spend = 0.0
        for leg, weight in zip(legs, weights):
            want = scale * weight
            got, paid = _walk_ask_shares(leg, want)
            if got <= 0.0 or got + 1e-9 < want:
                return None
            shares.append(got)
            spend += paid
        spend *= 1.0 + fee_rate
        if spend > budget + 1e-9:
            return None
        expected = sum(p * q for p, q in zip(probabilities, shares))
        return shares, spend, expected

    best: tuple[list[float], float] | None = None
    # ``scale`` is now a share multiplier, so its ceiling comes from the
    # thinnest leg rather than from the budget being a fraction of itself.
    ceiling = min(
        (sum(float(level["shares"]) for level in leg) / max(weight, 1e-9)
         for leg, weight in zip(legs, weights)),
        default=0.0,
    )
    if ceiling <= 0.0:
        return None
    low, high = 0.0, ceiling
    probe = attempt(min(ceiling, 1e-3))
    if probe is None or probe[2] <= probe[1]:
        return None
    for _ in range(60):
        mid = 0.5 * (low + high)
        got = attempt(mid)
        if got is not None and got[2] > got[1]:
            best = (got[0], got[1])
            low = mid
        else:
            high = mid
    return best


def _walk_ask_shares(
    levels: Sequence[Mapping[str, float]], want: float
) -> tuple[float, float]:
    """Shares bought and cash spent walking one leg for a share target."""

    shares = spent = 0.0
    for level in levels:
        if shares + 1e-12 >= want:
            break
        take = min(float(level["shares"]), want - shares)
        if take <= 0.0:
            break
        shares += take
        spent += take * float(level["price"])
    return shares, spent


def _walk_ask_cash(
    levels: Sequence[Mapping[str, float]], cash: float
) -> tuple[float, float]:
    """Shares bought and cash spent walking one leg with a cash limit."""

    shares = spent = 0.0
    for level in levels:
        price = float(level["price"])
        if price <= 0.0 or cash - spent <= 1e-12:
            break
        take = min(float(level["shares"]), (cash - spent) / price)
        if take <= 0.0:
            break
        shares += take
        spent += take * price
    return shares, spent


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
    superseded = _drop_superseded_positions(conn, recent_days=recent_days)
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
    if superseded:
        counts["superseded_rule_dropped"] = superseded
    # Equity advances a day at a time, and every position opened on a day is
    # sized off the equity the book held when that day opened.  Compounding
    # within the day would let a station-day that settles first bankroll the
    # ones beside it, which no book can do -- they are all opened before any
    # of them resolve.
    #
    # Only credible fills compound.  The report has always split the book at
    # ``DUST_COST_FLOOR`` because a basket bought under a cent is a one-sided
    # tail quote rather than a price anyone would fill, and a 0.001 leg that
    # lands returns a thousand times its stake.  Sizing was still reading the
    # unsplit number, so those phantom wins bankrolled the real ones: on
    # 2026-08-13 dust manufactured +3713 (P0) and +3733 (P1), lifting equity
    # from about a thousand to 4615/4792, and the 2% per-scope cap then sized
    # 2026-08-15's seats at 73 USDC apiece instead of 20.  Dust seats are
    # still recorded and still reported -- charter 35 buys the negative-EV leg
    # and refusing one-sided quotes outright leaves a single tradable basket
    # -- they just no longer set the size of anything.
    equity = {stage: INITIAL_CAPITAL_USDC for stage in STAGES}
    pending_positions: list[tuple] = []
    pending_settlements: list[tuple] = []
    pending_refusals: list[tuple] = []
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
            for stage in (s for s in STAGES if s in present):
                if stage in seated:
                    counts["already_recorded"] += 1
                    continue
                # Why this stage has no seat, kept as the walk goes so the
                # last cycle's answer is the one the page gets told.  It starts
                # at "no book" because a stage with no priced decision yields
                # nothing below and would otherwise be reported as nothing at
                # all.
                refusal = ("no_book_at_decision", "", 0, "")
                opened = False
                for payload in _stage_entry_candidates(
                    conn, station_id, event_date, stage
                ):
                    cycles = refusal[2] + 1
                    quotes = payload["quotes"]
                    observed_at = payload["observed_at"]
                    built = _build_snapshot(
                        station_id=station_id,
                        event_date=event_date,
                        observed_at=observed_at,
                        buckets=payload["buckets"],
                        quotes=quotes,
                    )
                    if built is None:
                        missing = [
                            str(row[0]) for row in payload["buckets"]
                            if str(row[0]) not in quotes
                        ]
                        refusal = (
                            "incomplete_book", ",".join(missing[:4]),
                            cycles, payload["decision_at_utc"],
                        )
                        continue
                    snapshot, probabilities, prices, depth = built
                    center = payload["center_bucket"] or max(
                        probabilities, key=lambda b: probabilities[b]
                    )
                    if center not in probabilities:
                        center = max(
                            probabilities, key=lambda b: probabilities[b]
                        )
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
                                max(
                                    probabilities,
                                    key=lambda b: probabilities[b],
                                )
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
                            # There is no incumbent here -- the ledger opens
                            # each station-day fresh -- so the evidence support
                            # is the mass-selected run the continuous-core
                            # resolver would produce, which is what
                            # probability_core computes.  A bare centre made a
                            # one-bucket incumbent, and the incumbent is exempt
                            # from every floor, so the ledger seated windows
                            # the live rule refuses.
                            desired=set(core) or {center},
                            probabilities=probabilities,
                            fee_rate=FEE_RATE,
                            maximum_support_buckets=MAXIMUM_SUPPORT_BUCKETS,
                            minimum_support_buckets=MINIMUM_SUPPORT_BUCKETS,
                            center_bucket=center,
                            # P1 converges rather than opens, so charter 50a
                            # gives it a floor of two while the opening stages
                            # keep three.  Left unset the enumeration held P1
                            # to the opening floor, which is a rule production
                            # does not run -- the ledger would report a stage
                            # refusing seats the live system takes.
                            probability_stage=stage,
                        )
                    except Exception as exc:  # noqa: BLE001 -- scope skipped
                        refusal = (
                            "selection_failed", f"{type(exc).__name__}",
                            cycles, payload["decision_at_utc"],
                        )
                        continue
                    priced_windows = [
                        row for row in windows
                        if row.get("all_in_price") is not None
                        and row.get("edge") is not None
                    ]
                    if WINDOW_STRATEGY != "live":
                        priced_windows = _strategy_windows(
                            WINDOW_STRATEGY,
                            snapshot=snapshot,
                            probabilities=probabilities,
                            center=center,
                            ordinals=ordinals,
                        )
                    if not priced_windows:
                        # Name every floor that turned windows away, or the
                        # page reports a refusal whose stated causes do not
                        # add up to it.  The leg floor landed on 2026-08-17
                        # and was not listed here, so 112 P0 refusals were
                        # attributed to a centre-bucket count that could not
                        # explain them.
                        centre_out = int(audit.get(
                            "center_refused_window_count") or 0)
                        short_out = int(audit.get(
                            "short_refused_window_count") or 0)
                        leg_out = int(audit.get(
                            "leg_floor_refused_window_count") or 0)
                        refusal = (
                            (
                                "leg_below_floor"
                                if leg_out > centre_out + short_out
                                else "no_priced_window"
                            ),
                            "中心桶拒绝 %d · 桶数不足拒绝 %d"
                            " · 单腿低于 %.2f 拒绝 %d" % (
                                centre_out, short_out,
                                MINIMUM_LEG_ALL_IN_ASK, leg_out,
                            ),
                            cycles, payload["decision_at_utc"],
                        )
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
                        refusal = (
                            "no_positive_kelly",
                            "概率 %.3f · 成本 %.3f" % (
                                float(seat["win_probability"]),
                                float(seat["all_in_price"]),
                            ),
                            cycles, payload["decision_at_utc"],
                        )
                        continue
                    # A complete basket buys the same share count on every leg
                    # (atomicity), so the size is set by the thinnest ladder --
                    # and the price by how deep into each the clip has to
                    # reach.  Charging the whole clip the top-of-book price is
                    # what let a 30-share offer at one tick "fill" 24,213
                    # shares at 0.004 when walking the ladders for that size
                    # costs 3.06 a set, three times anything the basket can pay
                    # out.
                    legs = [
                        depth.get(bucket) or [] for bucket in seat["buckets"]
                    ]
                    if EXECUTION_MODE != "taker":
                        # A resting order fills at its own price, not by
                        # walking the offer ladder, so the ladder is replaced
                        # by a single level at the assumed maker price.  Its
                        # size is still bounded by the visible offer depth --
                        # not because a maker consumes it, but because it is
                        # the only measure of how much trades here at all,
                        # and letting the clip run past it would reproduce
                        # the 24,213-shares-at-0.004 fiction in a new place.
                        legs = [
                            [{
                                "price": float(price),
                                "shares": sum(
                                    level["shares"]
                                    for level in (depth.get(bucket) or [])
                                ),
                            }]
                            for bucket, price in zip(
                                seat["buckets"],
                                _maker_leg_prices(snapshot, seat["buckets"]),
                            )
                        ]
                    capacity = min(
                        (
                            sum(level["shares"] for level in leg)
                            for leg in legs
                        ),
                        default=0.0,
                    )
                    per_leg: dict[str, float] = {}
                    weights = None
                    if ALLOCATION_SHAPE == "centre_tilt":
                        weights = _centre_tilt_weights(
                            seat["buckets"],
                            centre_bucket=str(
                                audit.get("ladder_center_bucket") or center
                            ),
                            probabilities=probabilities,
                            prices=[
                                float(quotes[b]["best_ask"] or 0.0)
                                if quotes.get(b, {}).get("best_ask")
                                is not None
                                else float(
                                    (quotes[b]["ask_levels"] or [{}])[0]
                                    .get("price") or 0.0
                                )
                                for b in seat["buckets"]
                            ],
                            ordinals={
                                str(row[0]): int(row[1])
                                for row in payload["buckets"]
                            },
                        )
                    resting_fills: dict[str, str] = {}
                    if EXECUTION_MODE == "maker_resting":
                        by_row = {r.bucket: r for r in snapshot.buckets}
                        ladders = [
                            _resting_ladder(by_row[str(b)])
                            if str(b) in by_row else []
                            for b in seat["buckets"]
                        ]
                        got, missing = _resting_ladder_fill(
                            conn,
                            station_id=station_id,
                            event_date=event_date,
                            buckets=seat["buckets"],
                            ladders=ladders,
                            placed_at_utc=payload["decision_at_utc"],
                        )
                        if missing is not None:
                            refusal = (
                                "resting_unfilled",
                                "挂 %d 腿 · %s 全档未成交（%.0f 分钟内）" % (
                                    len(seat["buckets"]),
                                    missing,
                                    MAKER_RESTING_HORIZON_MINUTES,
                                ),
                                cycles, payload["decision_at_utc"],
                            )
                            continue
                        # The basket keeps its shape, so it is only as large
                        # as its least-filled leg -- a ladder that filled
                        # three rungs on one leg and one on another bought a
                        # third of the intended basket, not a lopsided whole
                        # one.  Each leg pays the average of what it actually
                        # got, which is the point of resting across a band:
                        # the cheap rungs pull that average below the single
                        # price a point order would have had to pick.
                        resting_fraction = min(
                            (
                                len(got[str(b)]) / len(ladder)
                                for b, ladder in zip(seat["buckets"], ladders)
                                if ladder
                            ),
                            default=1.0,
                        )
                        resting_prices = {
                            str(b): sum(got[str(b)]) / len(got[str(b)])
                            * (1.0 + FEE_RATE)
                            for b in seat["buckets"]
                            if got.get(str(b))
                        }
                        stake *= resting_fraction
                        resting_fills = {
                            str(b): "%d/%d@%.4f" % (
                                len(got[str(b)]), len(ladder),
                                sum(got[str(b)]) / len(got[str(b)]),
                            )
                            for b, ladder in zip(seat["buckets"], ladders)
                            if ladder and got.get(str(b))
                        }
                        legs = [
                            [{
                                "price": resting_prices[str(bucket)],
                                "shares": sum(
                                    level["shares"]
                                    for level in (depth.get(bucket) or [])
                                ),
                            }]
                            for bucket in seat["buckets"]
                        ]
                        capacity = min(
                            (
                                sum(level["shares"] for level in leg)
                                for leg in legs
                            ),
                            default=0.0,
                        )
                    if weights is None:
                        filled = _fillable_size(
                            legs=legs,
                            win_probability=float(
                                seat["win_probability"]
                            ),
                            budget=stake,
                            fee_rate=FEE_RATE,
                            capacity=capacity,
                        )
                        if filled is None:
                            refusal = (
                                "no_depth", "最薄一档 %.0f 份" % capacity,
                                cycles, payload["decision_at_utc"],
                            )
                            continue
                        shares, stake = filled
                    else:
                        weighted = _fillable_weighted(
                            legs=legs,
                            weights=weights,
                            probabilities=[
                                float(probabilities.get(b, 0.0))
                                for b in seat["buckets"]
                            ],
                            budget=stake,
                            fee_rate=FEE_RATE,
                        )
                        if weighted is None:
                            refusal = (
                                "no_depth",
                                "加权分配无可行规模 · 最薄一档 %.0f 份"
                                % capacity,
                                cycles, payload["decision_at_utc"],
                            )
                            continue
                        leg_shares, stake = weighted
                        # The basket's own payout per unit spent.  Under
                        # equal shares this was cost over share count;
                        # weighted, the honest analogue is what the position
                        # expects to return, so ``shares`` becomes the
                        # expectation-equivalent count the settlement math
                        # already assumes.
                        # Two different questions, two different numbers.
                        #
                        # Settlement pays the rung that landed, so it reads
                        # the per-leg counts -- a single basket-level count
                        # would credit a payout no outcome produces.
                        #
                        # ``shares`` is the divisor of ``all_in_cost``, which
                        # the positive-EV gate, the dust floor and the whole
                        # page are built on, and it has to mean "cost per
                        # unit of payout".  Taking the thinnest leg made that
                        # a number no outcome produces either, in the other
                        # direction: FACT 2026-07-31 recorded 0.3400 against
                        # a 0.2800 top-of-book total -- a resting basket
                        # costing *more* than crossing, which cannot happen.
                        # The payout-weighted count is the honest analogue
                        # and degenerates to the equal-share case exactly:
                        # with q_i all equal, sum(p_i q_i)/p_win = q.
                        per_leg = {
                            str(b): float(q)
                            for b, q in zip(seat["buckets"], leg_shares)
                        }
                        expected_payout = sum(
                            float(probabilities.get(b, 0.0)) * q
                            for b, q in zip(seat["buckets"], leg_shares)
                        )
                        shares = expected_payout / max(
                            float(seat["win_probability"]), 1e-9
                        )
                    opened = True
                    # What the basket actually cost per set, which is the
                    # number every downstream P&L and edge reading has to be
                    # built on.
                    all_in_cost = stake / shares
                    if all_in_cost > float(seat["all_in_price"]) + 1e-9:
                        counts["walked_book"] += 1
                    # One book, one rule: a seat that does not compound does
                    # not consume the day's budget either.  Charging dust
                    # against a bankroll its own wins are barred from feeding
                    # would let the phantom side crowd out the real one.
                    credible = all_in_cost >= DUST_COST_FLOOR
                    if credible:
                        day_staked[stage] += stake
                    else:
                        counts["dust_fill_excluded_from_equity"] += 1
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
                            json.dumps(
                                list(seat["buckets"]), ensure_ascii=False
                            ),
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
                            SELECTION_RULE_SHA256,
                            SELECTION_RULE_LABEL,
                            json.dumps(per_leg, sort_keys=True),
                            json.dumps(resting_fills, sort_keys=True),
                        )
                    )
                    counts["positions"] += 1
                    counts["entry_cycle_%s" % (
                        "first" if cycles == 1 else "later"
                    )] += 1
                    actual = truth.get((station_id, event_date))
                    if actual is None:
                        counts["open_unsettled"] += 1
                        break
                    landed = _settled_bucket(payload["buckets"], actual)
                    if landed is None:
                        counts["truth_outside_book"] += 1
                        break
                    settled_bucket, settled_ordinal = landed
                    hit = 1 if settled_bucket in set(seat["buckets"]) else 0
                    # The rung that landed is the one that pays.  With one
                    # share count for the whole basket these are the same
                    # number; with a weighted basket they are not, and using
                    # the basket-level count credits a payout no outcome
                    # produced.
                    payout = (
                        per_leg.get(str(settled_bucket), 0.0)
                        if (hit and per_leg)
                        else (shares if hit else 0.0)
                    )
                    pnl = payout - stake
                    settled_ordinals = {
                        str(b): int(o)
                        for b, o, _l, _u, _p in payload["buckets"]
                    }
                    distance = min(
                        abs(settled_ordinals[b] - settled_ordinal)
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
                    if credible:
                        day_pnl[stage] += pnl
                    break
                if not opened:
                    counts[refusal[0]] += 1
                    pending_refusals.append(
                        (
                            station_id, event_date, stage, refusal[0],
                            refusal[1], refusal[2], refusal[3],
                            datetime.now(timezone.utc)
                            .isoformat().replace("+00:00", "Z"),
                        )
                    )
        # The day is over: realise it, then let the next day size off the
        # book it actually leaves behind.
        #
        # Read the day's realised P&L back out of the settlements rather than
        # accumulating what this pass happened to write.  A scope already
        # recorded is skipped above and contributes nothing to ``day_pnl``,
        # so on any incremental sweep the equity curve saw only the seats
        # this run created -- which on a steady-state pass is none at all,
        # leaving equity pinned at the opening capital.  That is how a P1
        # seat came to be staked at exactly 20.00 (2% of 1000) on a day the
        # book had drawn down to 726.  Same filter as the report: only fills
        # at or above the dust floor compound.
        realised = {
            str(row[0]): float(row[1] or 0.0)
            for row in conn.execute(
                "SELECT s.stage, SUM(s.pnl_usdc)"
                " FROM paper_trade_settlement s"
                " JOIN paper_trade_position p USING(position_id)"
                " WHERE s.event_date=? AND p.all_in_cost>=?"
                " GROUP BY s.stage",
                (event_date, DUST_COST_FLOOR),
            )
        }
        # Settlements this pass created are still buffered for the final
        # write burst, so they are absent from the query above and are added
        # on top rather than replacing it.  The two sets are disjoint by
        # construction: a scope is either already recorded (in the table) or
        # newly seated (in ``day_pnl``), never both.
        for stage in STAGES:
            equity[stage] = max(
                0.0,
                equity[stage] + realised.get(stage, 0.0) + day_pnl[stage],
            )
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
            " refused_window_count, content_sha256, selection_rule_sha256,"
            " selection_rule_label, leg_shares_json, resting_fill_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
    # A refusal is the current answer for a scope, not an event log: the next
    # sweep re-walks it and may seat where this one did not, so the row is
    # replaced rather than appended.
    if pending_refusals:
        conn.executemany(
            "INSERT INTO paper_trade_refusal ("
            " station_id, event_date, stage, reason, detail,"
            " cycles_examined, last_decision_at_utc, recorded_at_utc"
            ") VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(station_id,event_date,stage) DO UPDATE SET"
            "  reason=excluded.reason, detail=excluded.detail,"
            "  cycles_examined=excluded.cycles_examined,"
            "  last_decision_at_utc=excluded.last_decision_at_utc,"
            "  recorded_at_utc=excluded.recorded_at_utc",
            pending_refusals,
        )
    # A scope that seated has no refusal to explain any more.
    if pending_positions:
        conn.executemany(
            "DELETE FROM paper_trade_refusal"
            " WHERE station_id=? AND event_date=? AND stage=?",
            [(row[1], row[2], row[3]) for row in pending_positions],
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
            conn.execute("DELETE FROM paper_trade_refusal")
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
