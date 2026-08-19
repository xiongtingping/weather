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
from typing import Any, NamedTuple

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
from bayes_basket_target.settlement_time import (  # noqa: E402
    settlement_day_window,
)
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
#
# The scope is the station-day, not the station-day-stage.  One book means
# one position per station-day, so the cap binds the whole chain: whatever
# P0 spent is spent, and a top-up at P0-M or P1 draws on what is left of the
# same 2%.
MAX_STAKE_FRACTION_PER_SCOPE = 0.02
# One book for the whole network and one position per station-day (user
# ruling, 2026-08-18).  P0 opens it, P0-M and P1 top it up, and it settles
# once.
#
# Read as three books -- a bankroll each, a settlement each -- the ledger
# answered "which moment's evidence would have entered best", which is a
# real question and not this one.  It also charged the same event two and
# three times: of 152 settled station-days, 40 were scored more than once
# against a single truth, and the three books together staked 2220 USDC out
# of a nominal 1000.
#
# Top-ups only add (user ruling, 2026-08-18).  A later stage may widen the
# ladder or deepen a rung it already holds; it may not sell one back.  What
# that costs is honesty in the other direction: when P1's ladder converges
# from four rungs to two, the book keeps carrying the rungs the hard bound
# has since ruled out, all the way to settlement.  What it buys is that no
# fill is ever priced against a bid this lane cannot measure.
BOOK_MODEL = "one_book_per_station_day_top_up_only"
# How much of the station-day's cap each stage may have spent by the time it
# is done (user ruling, 2026-08-19).
#
# One cap for the chain is right -- three stages each taking 2% is a 6%
# position in one outcome -- but a single running total lets the opening
# spend all of it, and it does: ZUUU 2026-08-19 opened at 10.63 against a
# 10.6 cap and P1 was refused for having no room left, before its own
# numbers were reached at all.  That is the opening deciding there will be
# no adjustment, which is not a decision it is entitled to make.
#
# So the cap is staged.  P0 may commit most of it and no more; each later
# stage unlocks the rest, and the chain total is unchanged -- what changes
# is that a later stage cannot be starved by the earlier one.  Room a stage
# does not use is left for the next.
STAGE_BUDGET_SHARE = {"P0": 0.6, "P0-M": 0.8, "P1": 1.0}
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
# 0 means "until the day resolves", which is the standing ruling (user
# ruling, 2026-08-17, restated 2026-08-18) and is now what the code does.
# Six hours was a leftover from the probe's measurement horizons, and it is
# not a horizon a basket entering at listing has any reason to accept: it
# gives up in the middle of the night before the event and books the miss.
MAKER_RESTING_HORIZON_MINUTES = float(
    os.environ.get("PAPER_LEDGER_MAKER_HORIZON_MIN", "0") or 0.0
)
# How often an unfilled leg is re-posted at the prices then on the book.
#
# A resting order that never moves is not what a maker does and not what
# this lane can honestly claim: the band is derived from the spread, and a
# spread from twenty hours ago is not a price anyone was offering.  Left
# static, a ladder posted at listing spends the day a long way from a market
# that has since moved, and the leg records a miss that says more about the
# staleness of the quote than about the book's willingness to sell.
#
# Each round re-derives the band for the legs still open, from the book at
# that moment, and leaves the rungs already bought alone.  0 disables
# re-pricing and reproduces the single-shot behaviour.
MAKER_RESTING_REPRICE_MINUTES = float(
    os.environ.get("PAPER_LEDGER_MAKER_REPRICE_MIN", "60") or 0.0
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
    else f"阶梯{MAKER_RESTING_LADDER_STEPS}档·逐步建仓",
    None
    if EXECUTION_MODE != "maker_resting"
    else (
        "挂到结算" if MAKER_RESTING_HORIZON_MINUTES <= 0
        else f"挂{MAKER_RESTING_HORIZON_MINUTES:g}分钟"
    ),
    None
    if EXECUTION_MODE != "maker_resting" or MAKER_RESTING_REPRICE_MINUTES <= 0
    else f"每{MAKER_RESTING_REPRICE_MINUTES:g}分钟重挂",
    "一本账·只加不减",
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
    # How long the order rests and how often it is re-posted.  Both change
    # which fills a recorded row is built on, so rows written under the old
    # values describe a different execution.
    "maker_horizon_minutes": MAKER_RESTING_HORIZON_MINUTES,
    "maker_reprice_minutes": MAKER_RESTING_REPRICE_MINUTES,
    # Whether a partly-filled resting basket is a position or a refusal.
    # It changes what every resting row *is*, not merely its price.
    "resting_entry": "progressive_per_leg",
    "minimum_leg_all_in_ask": MINIMUM_LEG_ALL_IN_ASK,
    "initial_capital_usdc": INITIAL_CAPITAL_USDC,
    "kelly_fraction": KELLY_FRACTION,
    "max_quote_staleness_minutes": MAX_QUOTE_STALENESS_MINUTES,
    "max_stake_fraction_per_scope": MAX_STAKE_FRACTION_PER_SCOPE,
    # Which share of that cap each stage may have committed by its
    # end.  It changes what every seat after the first may be.
    "stage_budget_share": STAGE_BUDGET_SHARE,
    "maximum_support_buckets": MAXIMUM_SUPPORT_BUCKETS,
    "minimum_support_buckets": MINIMUM_SUPPORT_BUCKETS,
    "p1_minimum_support_buckets": P1_MINIMUM_SUPPORT_BUCKETS,
    "stage_floor": "per_stage",
    # One book, one position per station-day, settled once.  This changes
    # what every recorded row *is* -- a stage row is now a top-up of a
    # standing position rather than a bet of its own -- so rows written
    # under the three-book reading describe a strategy that no longer runs.
    "book_model": BOOK_MODEL,
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
    # A later stage that wanted nothing this position does not already hold.
    # Under one book a stage is a top-up, so "the ladder it chose is inside
    # what P0 already bought" is not a refusal to trade -- it is the chain
    # standing pat, and it has to read as that rather than as no seat.
    "no_top_up_needed",
    # A later stage whose station-day never opened.  Only P0 opens a
    # position; a top-up with nothing to top up is not a position of its own.
    "chain_never_opened",
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
  posted_buckets_json TEXT NOT NULL DEFAULT '',
  intended_legs_json TEXT NOT NULL DEFAULT '',
  intended_stake_usdc REAL,
  held_before_json TEXT NOT NULL DEFAULT '',
  chain_shares_json TEXT NOT NULL DEFAULT '',
  chain_stake_usdc REAL,
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
  -- Which truth this score was struck against.  Settled truth is revisable
  -- by design: the table is append-only under ``revision_no``, guarded by
  -- the ``calibration_truth_no_update``/``calibration_truth_no_delete``
  -- triggers, so a correction can only ever arrive as a *new* revision.  A
  -- settlement that does not say which one it read cannot know it has gone
  -- stale, and this one could not: on 2026-08-18 192 revisions landed at
  -- once -- 156 changing the value, 35 moving the winning bucket -- and the
  -- incremental sweep kept scoring the superseded ones because a recorded
  -- row is a recorded row.  The sha is the staleness key; the revision
  -- number rides along so a reader can see how far the row has moved
  -- without joining back to the truth table.
  truth_revision_no INTEGER,
  truth_content_sha256 TEXT NOT NULL DEFAULT '',
  -- What the whole position cost per unit of payout, across every stage that
  -- topped it up.  The dust floor is a statement about the basket that
  -- settled, and no stage row carries that number: each holds the cost of
  -- its own top-up.
  basket_all_in_cost REAL,
  -- Kept keyed by stage rather than migrated to the station-day: one chain
  -- writes one settlement now, and the stage it carries is whichever one
  -- last topped it up, so the wider key never binds.  Deletes key on the
  -- station-day alone so a chain whose last stage moved still replaces its
  -- own row.
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
    if "posted_buckets_json" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN posted_buckets_json TEXT NOT NULL DEFAULT ''"
        )
    if "resting_fill_json" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN resting_fill_json TEXT NOT NULL DEFAULT ''"
        )
    if "intended_legs_json" not in columns:
        # The order as it was posted, per leg, before the book answered.
        # ``leg_shares_json`` holds what came back, so without this the
        # shortfall on a leg that filled nothing is unrecoverable -- its
        # kept share is zero and the weighted allocation that sized it
        # cannot be inverted from the survivors.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN intended_legs_json TEXT NOT NULL DEFAULT ''"
        )
    if "intended_stake_usdc" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN intended_stake_usdc REAL"
        )
    if "held_before_json" not in columns:
        # What the chain already held when this stage looked.  A top-up buys
        # the difference, so without this a one-leg P1 row is unreadable:
        # it looks like a one-rung basket rather than the two rungs the
        # position already had plus the one it added.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN held_before_json TEXT NOT NULL DEFAULT ''"
        )
    if "chain_shares_json" not in columns:
        # The whole position after this stage acted, which is what settles.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN chain_shares_json TEXT NOT NULL DEFAULT ''"
        )
    if "chain_stake_usdc" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN chain_stake_usdc REAL"
        )
    if "selection_rule_label" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN selection_rule_label TEXT NOT NULL DEFAULT ''"
        )
    if "selection_rule_sha256" not in columns:
        # Rows written before the stamp existed were written under a rule set
        # nothing records, so they carry the empty string and the sweep below
        # re-derives them.
        #
        # Added before the label backfill below, which reads this column.  The
        # other order only works on a database that already has it: on a fresh
        # one ``_POSITION_DDL`` creates ``selection_rule_label`` but not this,
        # so the backfill ran first and ``ensure_schema`` could not initialise
        # an empty database at all.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN selection_rule_sha256 TEXT NOT NULL DEFAULT ''"
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
    settlement_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(paper_trade_settlement)")
    }
    # Settlements written before the binding existed carry the empty sha,
    # which no standing truth can equal, so ``_rescore_settlements`` picks
    # every one of them up once.  It re-binds rather than re-scores the ones
    # whose recorded value still matches, so the migration costs an UPDATE of
    # two columns per row and leaves the numbers on the page untouched.
    if "basket_all_in_cost" not in settlement_columns:
        conn.execute(
            "ALTER TABLE paper_trade_settlement"
            " ADD COLUMN basket_all_in_cost REAL"
        )
    if "truth_revision_no" not in settlement_columns:
        conn.execute(
            "ALTER TABLE paper_trade_settlement"
            " ADD COLUMN truth_revision_no INTEGER"
        )
    if "truth_content_sha256" not in settlement_columns:
        conn.execute(
            "ALTER TABLE paper_trade_settlement"
            " ADD COLUMN truth_content_sha256 TEXT NOT NULL DEFAULT ''"
        )


def _window_floor(
    conn: sqlite3.Connection, *, recent_days: int | None
) -> str:
    """The earliest event day an incremental pass re-derives.

    Empty string for a rebuild, which has no floor.  One definition, used by
    everything that has to agree on where the window starts -- the walk, the
    superseded-row sweep and the opening equity.
    """

    if recent_days is None:
        return ""
    floor = conn.execute(
        "SELECT date((SELECT max(event_date)"
        " FROM basket_probability_bridge_history), '-' || ?1 || ' days')",
        (recent_days,),
    ).fetchone()
    return str((floor or [""])[0] or "")


def _opening_equity(conn: sqlite3.Connection, *, earliest: str) -> float:
    """The book an incremental window opens with, not the book it started life
    with.

    An incremental pass walks only the last few days, and the equity it
    compounds used to start at the initial capital on every run.  So a seat
    created by a sweep was sized against a thousand USDC whatever the book had
    actually done -- 2026-08-16 through 08-18 seated 24 positions over the 2%
    per-scope cap that way, sized off 1000 while the P1 book stood at 724.
    The rebuild that walks everything got this right and hid it: only the
    incremental path was wrong, and the incremental path is the one that runs
    every fifteen minutes.

    Same filter as the day-by-day advance below, for the same reason: a
    basket bought under the dust floor is a one-sided quote, and its win
    bankrolls nothing.  Rule fingerprints are deliberately *not* filtered
    here -- rows outside the window are never re-derived, so excluding them
    would open the window on a book the page does not show.

    One number, not one per stage: the three stages are one position and
    draw on one bankroll (see ``BOOK_MODEL``).
    """

    if not earliest:
        return INITIAL_CAPITAL_USDC
    realised = conn.execute(
        "SELECT SUM(s.pnl_usdc)"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p USING(position_id)"
        " WHERE s.event_date < ? AND p.all_in_cost>=?",
        (earliest, DUST_COST_FLOOR),
    ).fetchone()
    return max(
        0.0, INITIAL_CAPITAL_USDC + float((realised or [0.0])[0] or 0.0)
    )


def _drop_superseded_positions(
    conn: sqlite3.Connection, *, recent_days: int | None
) -> int:
    """Discard seats recorded under a rule set that no longer runs.

    Only within the window this pass can re-derive: a row deleted outside it
    would leave the page emptier than the ledger, which is the opposite of what
    the stamp is for.
    """

    earliest = _window_floor(conn, recent_days=recent_days)
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


class _Truth(NamedTuple):
    """The settled value that stands, and which revision said so."""

    tmax_c: float
    revision_no: int
    content_sha256: str


def _truth(conn: sqlite3.Connection) -> dict[tuple[str, str], _Truth]:
    """Latest revision per station-day (charter 44).

    The revision's identity travels with its value.  A settlement scored
    against a number alone cannot tell a corrected day from an unchanged one,
    which is the whole of why an incremental pass used to need a rebuild.
    """

    out: dict[tuple[str, str], _Truth] = {}
    for station, day, value, revision, sha in conn.execute(
        "SELECT station_id, local_date, official_tmax_c, revision_no,"
        " content_sha256"
        " FROM calibration_station_daily_high_truth t"
        " WHERE revision_no = ("
        "   SELECT MAX(revision_no)"
        "   FROM calibration_station_daily_high_truth x"
        "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date)"
    ):
        out[(str(station), str(day))] = _Truth(
            float(value), int(revision), str(sha)
        )
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




def _ladder_from_quote(
    quote: Mapping[str, Any], steps: int = MAKER_RESTING_LADDER_STEPS
) -> list[float]:
    """The same band as ``_resting_ladder``, from a raw snapshot quote.

    Re-pricing happens against the book as it stood mid-day, where there is
    no compiled snapshot row to read -- only the quote the socket recorded.
    The arithmetic is deliberately the same one, so a re-posted ladder is
    the ladder the opening would have posted at that moment.
    """

    levels = [
        level for level in (quote.get("ask_levels") or [])
        if level.get("price") is not None
    ]
    ask = quote.get("best_ask")
    if ask is None and levels:
        ask = levels[0].get("price")
    bid = quote.get("bid_price", quote.get("best_bid"))
    if ask is None or bid is None:
        return []
    ask, bid = float(ask), float(bid)
    tick = float(quote.get("price_tick_size") or 0.001)
    if bid <= 0.0 or ask <= 0.0:
        return []
    high = ask - tick
    low = min(bid + MAKER_RESTING_IMPROVE_TICKS * tick, high)
    if high < bid - 1e-9:
        return []
    if high <= low + tick / 2 or steps <= 1:
        return [round(low, 6)]
    span = high - low
    out: list[float] = []
    for index in range(steps):
        price = round(low + span * index / (steps - 1), 6)
        if not out or price > out[-1] + tick / 2:
            out.append(price)
    return out


def _resting_deadline(
    *, placed_at_utc: str, settlement_timezone: str, event_date: str
) -> str:
    """When the order gives up.

    The day itself, unless a horizon is configured.  A basket posted at
    listing has until its market resolves and no reason to withdraw before
    then; the fixed six hours this used to carry came from the fill probe's
    measurement horizons, and it booked a miss in the middle of the night
    before the event.
    """

    placed = datetime.fromisoformat(str(placed_at_utc).replace("Z", "+00:00"))
    ends = placed
    if MAKER_RESTING_HORIZON_MINUTES > 0:
        ends = placed + timedelta(minutes=MAKER_RESTING_HORIZON_MINUTES)
    try:
        day_end = settlement_day_window(
            event_date, settlement_timezone
        )["target_day_end_utc"]
    except Exception:
        day_end = ""
    if MAKER_RESTING_HORIZON_MINUTES <= 0:
        return day_end or (
            placed + timedelta(days=1)
        ).isoformat().replace("+00:00", "Z")
    # A configured horizon still cannot outlive the market it is posted in.
    horizon = ends.isoformat().replace("+00:00", "Z")
    return min(horizon, day_end) if day_end else horizon


def _resting_ladder_fill(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    buckets: Sequence[str],
    ladders: Sequence[Sequence[float]],
    placed_at_utc: str,
    settlement_timezone: str = "UTC",
) -> tuple[dict[str, list[float]], str | None]:
    """Which rungs of each leg's ladder the book traded through.

    A leg counts as filled when any rung fills.  Returns the filled prices
    per bucket so the cost is the ladder's own average rather than an
    assumed one.

    Two things the order does while it waits.  It waits until the day
    resolves rather than for a fixed six hours, and every
    ``MAKER_RESTING_REPRICE_MINUTES`` it re-posts whatever is still open at
    the prices then on the book.  A band derived from a spread twenty hours
    stale is not a price anyone was offering, and a leg that misses against
    it has recorded the staleness of its own quote rather than the book's
    unwillingness to sell.  Rungs already bought are never re-posted.
    """

    deadline = _resting_deadline(
        placed_at_utc=placed_at_utc,
        settlement_timezone=settlement_timezone,
        event_date=event_date,
    )
    pending = {
        str(bucket): sorted(prices, reverse=True)
        for bucket, prices in zip(buckets, ladders)
    }
    # How many rungs each leg posted, so a re-post keeps the ladder the same
    # depth rather than growing it every round.
    posted_depth = {
        bucket: len(prices) for bucket, prices in pending.items()
    }
    filled: dict[str, list[float]] = {b: [] for b in pending}
    next_reprice = (
        None if MAKER_RESTING_REPRICE_MINUTES <= 0
        else datetime.fromisoformat(
            str(placed_at_utc).replace("Z", "+00:00")
        ) + timedelta(minutes=MAKER_RESTING_REPRICE_MINUTES)
    )
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
        # Re-post first, then see what the same tick trades through: an
        # order moved to a new price is live at that price from the moment
        # it is moved, and checking the old band one last time would credit
        # a fill to a price that had already been cancelled.
        if next_reprice is not None:
            at = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            while at >= next_reprice:
                for bucket, rungs in pending.items():
                    if not rungs:
                        # Nothing open on this leg -- either fully bought or
                        # the book stopped quoting it.  Re-posting the rungs
                        # it already owns would buy them twice.
                        continue
                    item = quotes.get(bucket)
                    if not isinstance(item, Mapping):
                        continue
                    fresh = _ladder_from_quote(
                        item,
                        max(
                            1,
                            posted_depth.get(bucket, 0) - len(filled[bucket]),
                        ),
                    )
                    if fresh:
                        pending[bucket] = sorted(fresh, reverse=True)
                next_reprice = next_reprice + timedelta(
                    minutes=MAKER_RESTING_REPRICE_MINUTES
                )
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


def _progressive_entry(
    *,
    buckets: Sequence[str],
    leg_shares: Sequence[float],
    filled_fraction: Mapping[str, float],
    prices: Mapping[str, float],
    probabilities: Mapping[str, float],
) -> tuple[list[str], list[float], float, float] | None:
    """Keep the part of each leg that filled, and only that part.

    A resting basket is built up, not switched on (user ruling,
    2026-08-17).  Each rung of each leg is its own order and is decided on
    its own, so the position that exists at the end of the horizon is
    whatever the book came down to -- a leg that filled four rungs of five
    is four fifths of a leg, and a leg that filled none is simply absent.

    The earlier rule sized the whole basket down to its least-filled leg to
    keep the shape intact.  That is a defensible *bet*, but it is not what
    resting orders do, and it threw away fills that had already happened.

    What this costs is honesty about the result: the basket that settles can
    be smaller than the one that was chosen, can be missing a rung in the
    middle, and its win probability is the sum over what was actually
    bought.  All three are recorded rather than assumed away.

    Returns ``None`` when nothing filled anywhere -- there is no position to
    record, which is the one case that is still a refusal.
    """

    kept_buckets: list[str] = []
    kept_shares: list[float] = []
    stake = 0.0
    for bucket, target in zip(buckets, leg_shares):
        quantity = float(target) * float(filled_fraction.get(str(bucket), 0.0))
        price = float(prices.get(str(bucket), 0.0))
        if quantity <= 0.0 or price <= 0.0:
            continue
        kept_buckets.append(str(bucket))
        kept_shares.append(quantity)
        stake += quantity * price
    if not kept_buckets:
        return None
    win_probability = sum(
        float(probabilities.get(bucket, 0.0)) for bucket in kept_buckets
    )
    return kept_buckets, kept_shares, stake, win_probability


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


class _Score(NamedTuple):
    """What a settled value does to a seat, once the value is known."""

    settled_bucket: str
    hit: int
    payout_usdc: float
    pnl_usdc: float
    center_error_c: float | None
    bucket_distance: int


def _score_settlement(
    *,
    ladder: Sequence[tuple],
    window: Sequence[str],
    stake: float,
    shares: float,
    per_leg: Mapping[str, float],
    center_c: float | None,
    truth_c: float,
) -> _Score | None:
    """Score one seat against one settled value.

    Two callers reach this: the entry walk, which knows the seat because it
    just chose it, and ``_rescore_settlements``, which reads the seat back off
    the recorded row.  They have to agree exactly -- a settlement re-derived
    under a corrected truth that also silently re-derived its *payout* rule
    would move numbers the correction never touched -- so the arithmetic lives
    in one place rather than twice.

    ``None`` means the settled value fell outside the ladder the seat was
    priced against: nothing to score, not a loss.
    """

    landed = _settled_bucket(list(ladder), truth_c)
    if landed is None:
        return None
    settled_bucket, settled_ordinal = landed
    seated = {str(bucket) for bucket in window}
    hit = 1 if settled_bucket in seated else 0
    # The rung that landed is the one that pays.  With one share count for
    # the whole basket these are the same number; with a weighted basket they
    # are not, and using the basket-level count credits a payout no outcome
    # produced.
    payout = (
        float(per_leg.get(settled_bucket, 0.0))
        if (hit and per_leg)
        else (shares if hit else 0.0)
    )
    ordinals = {str(b): int(o) for b, o, _l, _u, _p in ladder}
    distance = min(abs(ordinals[b] - settled_ordinal) for b in seated)
    return _Score(
        settled_bucket=settled_bucket,
        hit=hit,
        payout_usdc=payout,
        pnl_usdc=payout - stake,
        center_error_c=(
            None if center_c is None else float(center_c) - truth_c
        ),
        bucket_distance=distance,
    )


def _settlement_ladder(
    conn: sqlite3.Connection,
    station_id: str,
    event_date: str,
    stage: str,
    decision_at_utc: str,
) -> list[tuple]:
    """The bucket ladder the recorded seat was priced against.

    One indexed lookup: ``decision_at_utc`` is unique within a stage scope, so
    this recovers exactly the ``buckets`` the entry walk held when it opened
    the seat -- edges included, which ``probabilities_json`` does not carry.
    That is why re-scoring needs no history walk and no quote replay.
    """

    return conn.execute(
        "SELECT b.bucket, b.ordinal, b.lower_c, b.upper_c, b.probability"
        " FROM basket_probability_bridge_history h"
        " JOIN basket_probability_bridge_bucket_probability b"
        "   ON b.history_id = h.history_id"
        " WHERE h.station_id=? AND h.event_date=? AND h.stage=?"
        "   AND h.decision_at_utc=?"
        " ORDER BY b.ordinal",
        (station_id, event_date, stage, decision_at_utc),
    ).fetchall()


def _rescore_settlements(
    conn: sqlite3.Connection, truth: dict[tuple[str, str], _Truth]
) -> dict[str, int]:
    """Score every recorded seat against the truth that stands *now*.

    A seat is chosen before the day resolves, so settled truth is no part of
    it -- but the settlement is nothing else, and both were written by the
    same branch of the entry walk.  That walk skips a stage it has already
    seated, so once the position row existed its settlement was frozen in two
    ways.  A seat opened before its truth landed never got one at all: the
    next sweep saw the stage as recorded and walked past it, and on 2026-08-18
    that left 86 of 262 positions open with only a rebuild able to close any
    of them -- two already had their truth in hand.  And a settlement whose
    truth was later *revised* kept scoring the superseded value, because the
    write is ``INSERT OR IGNORE`` on ``UNIQUE(station_id,event_date,stage)``
    and the only thing that deletes a settlement keys on the selection rule,
    which a truth correction does not touch.

    Truth is revisable by design -- append-only under ``revision_no``, guarded
    by the ``calibration_truth_no_update``/``calibration_truth_no_delete``
    triggers, so a correction can only arrive as a new revision -- and on
    2026-08-18 the sub-degree Fahrenheit re-derivation appended 192 of them at
    once, 156 changing the value and 35 moving the winning bucket.  The
    quarter-hourly ``--backfill`` would have scored the old buckets
    indefinitely; a full ``--rebuild``, which discards the whole derived book,
    was the only way to move them.

    So each settlement records the truth it read, and is re-derived here when
    that binding is no longer the standing one.  Three answers per seat:
    nothing to do when the binding still stands; a two-column re-bind when the
    revision moved but the value did not, which leaves the page's numbers and
    ``settled_at_utc`` untouched; and a full re-score otherwise.

    Deliberately *not* bounded by ``recent_days`` the way
    ``_drop_superseded_positions`` is.  That bound exists because re-deriving
    a seat means walking a day's whole decision history and quote book, which
    is most of the pass; re-scoring means one indexed ladder lookup, and only
    for the rows whose truth actually moved.  A correction that reaches back a
    month has to reach that month's rows, and on the days where nothing moved
    this costs a single scan of a table with a few hundred rows in it.
    """

    counts: dict[str, int] = defaultdict(int)
    # One row per chain, not per stage: the stages of a station-day are one
    # position and settle once, so this re-scores the position as the last
    # stage left it.  Reading every stage row here would write a settlement
    # per stage and rebuild the three-book account this ledger no longer
    # keeps -- against a single truth, which is how the same event came to
    # be charged three times.
    rows = conn.execute(
        "SELECT p.position_id, p.station_id, p.event_date, p.stage,"
        " p.decision_at_utc, p.chain_shares_json, p.leg_shares_json,"
        " p.chain_stake_usdc, p.stake_usdc, p.shares, p.probabilities_json,"
        " p.win_probability, p.center_c,"
        " s.settlement_id, s.truth_content_sha256, s.settled_tmax_c"
        " FROM paper_trade_position p"
        " JOIN (SELECT station_id, event_date, MAX(decision_at_utc) AS last"
        "       FROM paper_trade_position GROUP BY station_id, event_date) t"
        "   ON t.station_id = p.station_id AND t.event_date = p.event_date"
        "  AND t.last = p.decision_at_utc"
        " LEFT JOIN paper_trade_settlement s"
        "   ON s.position_id = p.position_id"
    ).fetchall()
    rebind: list[tuple] = []
    drop: list[tuple] = []
    write: list[tuple] = []
    for row in rows:
        (
            position_id, station_id, event_date, stage, decision_at_utc,
            chain_shares_json, leg_shares_json, chain_stake, stage_stake,
            stage_shares, probabilities_json, stage_win_probability,
            center_c, settlement_id, bound_sha, settled_tmax_c,
        ) = row
        standing = truth.get((str(station_id), str(event_date)))
        if standing is None:
            # No settled value yet.  An open seat, not a stale one -- and
            # truth is never deleted, so a settled row cannot lose its truth.
            counts["open_unsettled"] += 1
            continue
        if settlement_id is not None:
            if str(bound_sha or "") == standing.content_sha256:
                counts["settlement_current"] += 1
                continue
            if settled_tmax_c is not None and (
                float(settled_tmax_c) == standing.tmax_c
            ):
                # The revision moved but the value did not, so every number
                # derived from it is unchanged by construction.  Re-binding
                # rather than re-writing is what keeps a revision that only
                # restates the day from touching the page -- and it is how
                # the rows written before this column existed, which carry
                # the empty sha, settle down after one pass instead of
                # looking stale forever.
                rebind.append(
                    (standing.revision_no, standing.content_sha256,
                     position_id)
                )
                counts["settlement_rebound"] += 1
                continue
        ladder = _settlement_ladder(
            conn, str(station_id), str(event_date), str(stage),
            str(decision_at_utc),
        )
        if not ladder:
            counts["settlement_ladder_missing"] += 1
            continue
        # The accumulated position, falling back to this stage's own legs for
        # a row written before the chain columns existed -- a one-stage chain
        # is the same thing either way.
        per_leg = {
            str(bucket): float(value)
            for bucket, value in (
                json.loads(chain_shares_json or leg_shares_json or "{}") or {}
            ).items()
        }
        if not per_leg:
            counts["settlement_position_empty"] += 1
            continue
        stake = float(
            chain_stake if chain_stake is not None else stage_stake
        )
        ordinals = {str(b): int(o) for b, o, _l, _u, _p in ladder}
        window = sorted(per_leg, key=lambda b: ordinals.get(b, 0))
        probabilities = json.loads(probabilities_json or "{}") or {}
        win_probability = sum(
            float(probabilities.get(bucket, 0.0)) for bucket in per_leg
        ) or float(stage_win_probability)
        expected_payout = sum(
            float(probabilities.get(bucket, 0.0)) * quantity
            for bucket, quantity in per_leg.items()
        )
        shares = (
            expected_payout / max(win_probability, 1e-9)
            if expected_payout > 0
            else float(stage_shares)
        )
        scored = _score_settlement(
            ladder=ladder,
            window=window,
            stake=stake,
            shares=shares,
            per_leg=per_leg,
            center_c=center_c,
            truth_c=standing.tmax_c,
        )
        if scored is None:
            # The corrected value left the ladder the seat was priced
            # against.  The entry walk records no settlement in that case, so
            # neither does a re-score -- and a row scored against a value
            # that is no longer the truth must not survive as if it were.
            counts["truth_outside_book"] += 1
            if settlement_id is not None:
                drop.append((station_id, event_date))
                counts["settlement_dropped"] += 1
            continue
        identity = {
            "station_id": str(station_id),
            "event_date": str(event_date),
            "stage": str(stage),
            "decision_at_utc": str(decision_at_utc),
            "window": window,
            "stake_usdc": round(float(stake), 6),
        }
        write.append((
            "paper-set-" + content_sha256(
                {**identity, "truth": standing.tmax_c}
            )[:24],
            position_id, station_id, event_date, stage,
            standing.tmax_c, scored.settled_bucket, scored.hit,
            float(stake), scored.payout_usdc, scored.pnl_usdc,
            float(win_probability), scored.center_error_c,
            scored.bucket_distance,
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            standing.revision_no, standing.content_sha256,
            stake / shares if shares > 0 else None,
        ))
        counts[
            "settlement_rescored" if settlement_id is not None
            else "settlement_settled_late"
        ] += 1
    if not (rebind or drop or write):
        return dict(counts)
    # Same short write burst as the entry walk, and for the same reason: the
    # scan above is all reads, and the resident gives up on a busy database
    # after five seconds.
    if rebind:
        conn.executemany(
            "UPDATE paper_trade_settlement"
            " SET truth_revision_no=?, truth_content_sha256=?"
            " WHERE position_id=?",
            rebind,
        )
    # Delete before insert rather than upsert: a re-scored row gets a new
    # ``settlement_id`` whenever the value moved (the id hashes the truth), so
    # the incoming row can collide on the primary key or on the station-day
    # uniqueness, and only one of those can be an upsert target.  Keyed on the
    # station-day alone -- one chain settles once, and the stage it carries is
    # whichever one last topped the position up, which can move between passes
    # without the day's account changing hands.
    if drop or write:
        conn.executemany(
            "DELETE FROM paper_trade_settlement"
            " WHERE station_id=? AND event_date=?",
            drop + [(row[2], row[3]) for row in write],
        )
    if write:
        conn.executemany(
            "INSERT INTO paper_trade_settlement ("
            " settlement_id, position_id, station_id, event_date, stage,"
            " settled_tmax_c, settled_bucket, hit, stake_usdc,"
            " payout_usdc, pnl_usdc, claimed_win_probability,"
            " center_error_c, bucket_distance, settled_at_utc,"
            " truth_revision_no, truth_content_sha256,"
            " basket_all_in_cost"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            write,
        )
    conn.commit()
    return dict(counts)


def backfill(
    conn: sqlite3.Connection,
    *,
    verbose: bool = False,
    recent_days: int | None = None,
) -> dict:
    ensure_schema(conn)
    superseded = _drop_superseded_positions(conn, recent_days=recent_days)
    truth = _truth(conn)
    # The market day a resting order lives inside.  Charter 44 binds the
    # settlement timezone per station in its own table rather than the
    # registry, whose raw column is wrong for 33 of 45 stations.
    timezones = {
        str(station): str(zone)
        for station, zone in conn.execute(
            "SELECT station_id, settlement_timezone"
            " FROM calibration_station_market_timezone_binding"
        )
    }
    # Before the walk, not after.  The equity curve below reads each day's
    # realised P&L back out of the committed settlements, so a correction that
    # landed after this pass would size the following days off numbers it had
    # already superseded.  The walk itself only ever adds seats this pass
    # created, which this cannot have touched -- they do not exist yet.
    rescored = _rescore_settlements(conn, truth)
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
    for key, value in rescored.items():
        if value:
            counts[key] += value
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
    #
    # An incremental pass inherits the book the days before its window left
    # behind; a rebuild walks from the first day and starts at the initial
    # capital.  Both read the same settlements through the same dust filter,
    # so the two paths size a seat identically.
    equity = _opening_equity(
        conn, earliest=_window_floor(conn, recent_days=recent_days)
    )
    pending_positions: list[tuple] = []
    pending_settlements: list[tuple] = []
    pending_refusals: list[tuple] = []
    pending_chain_resets: list[tuple[str, str]] = []
    by_day: dict[str, list[str]] = defaultdict(list)
    for station_id, event_date in scopes:
        by_day[str(event_date)].append(str(station_id))
    for event_date in sorted(by_day):
        day_pnl = 0.0
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
            # A chain is walked whole or not at all.  A top-up is priced
            # against what the position already holds, so replaying P1 on
            # its own -- with P0's rows left in place and its shares unknown
            # to this pass -- would size the top-up as if the chain were
            # empty.  Rows for the day are dropped in the write burst and
            # rewritten from P0 forward.
            if seated:
                pending_chain_resets.append((station_id, event_date))
                counts["chain_replayed"] += 1
            # The position this station-day holds, built up stage by stage.
            # Shares per bucket, what they cost, and the ladder the last
            # stage to act was working from -- settlement reads all three.
            held: dict[str, float] = {}
            chain_stake = 0.0
            chain_probabilities: dict[str, float] = {}
            chain_ladder: list = []
            chain_center_c: float | None = None
            chain_win_probability = 0.0
            chain_credible = False
            chain_opened_at = ""
            chain_position_id = ""
            chain_last_stage = ""
            for stage in (s for s in STAGES if s in present):
                # Only P0 opens.  A later stage on a day the position was
                # never opened is not a bet this strategy would have held --
                # under three books it was, and that is where a third of the
                # recorded P1 rows came from.
                if stage != "P0" and not held:
                    counts["chain_never_opened"] += 1
                    pending_refusals.append((
                        station_id, event_date, stage, "chain_never_opened",
                        "P0 未建仓，本阶段无仓可加", 0, "",
                        datetime.now(timezone.utc)
                        .isoformat().replace("+00:00", "Z"),
                    ))
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
                    # never beyond what the per-scope cap leaves this chain.
                    # The cap is the station-day's, so a top-up spends what
                    # the opening left rather than a fresh allowance of its
                    # own -- three stages each taking 2% is a 6% position in
                    # one outcome, which is the three-book reading wearing
                    # the cap's clothes.
                    equity_at_open = equity
                    fraction, stake = _kelly_stake(
                        win_probability=float(seat["win_probability"]),
                        cost=float(seat["all_in_price"]),
                        equity=equity_at_open,
                    )
                    stage_room = (
                        equity_at_open
                        * MAX_STAKE_FRACTION_PER_SCOPE
                        * STAGE_BUDGET_SHARE.get(stage, 1.0)
                        - chain_stake
                    )
                    stake = min(stake, max(0.0, stage_room))
                    if stake <= 0.0:
                        refusal = (
                            "no_positive_kelly",
                            "概率 %.3f · 成本 %.3f%s" % (
                                float(seat["win_probability"]),
                                float(seat["all_in_price"]),
                                ""
                                if not held
                                else " · %s 段额度已用尽（本链已投 %.2f）"
                                % (stage, chain_stake),
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
                            settlement_timezone=timezones.get(
                                station_id, "UTC"
                            ),
                            station_id=station_id,
                            event_date=event_date,
                            buckets=seat["buckets"],
                            ladders=ladders,
                            placed_at_utc=payload["decision_at_utc"],
                        )
                        if not any(got.values()):
                            refusal = (
                                "resting_unfilled",
                                "挂 %d 腿 · 全程一档未成交（%s）" % (
                                    len(seat["buckets"]),
                                    "挂到结算"
                                    if MAKER_RESTING_HORIZON_MINUTES <= 0
                                    else "%.0f 分钟"
                                    % MAKER_RESTING_HORIZON_MINUTES,
                                ),
                                cycles, payload["decision_at_utc"],
                            )
                            continue
                        # Each leg pays the average of the rungs it got.
                        # That average is the point of resting across a band
                        # rather than at a price: the cheap rungs pull it
                        # below the single price a point order would have
                        # had to pick, and no quote in the book equals it.
                        resting_prices = {
                            str(b): sum(got[str(b)]) / len(got[str(b)])
                            * (1.0 + FEE_RATE)
                            for b in seat["buckets"]
                            if got.get(str(b))
                        }
                        resting_fraction = {
                            str(b): (
                                len(got[str(b)]) / len(ladder)
                                if ladder else 0.0
                            )
                            for b, ladder in zip(seat["buckets"], ladders)
                        }
                        # Every intended leg is recorded, including the ones
                        # that bought nothing -- "which rung never filled" is
                        # the number the page needs, and it is invisible if
                        # the empty legs are dropped here.
                        resting_fills = {
                            str(b): "%d/%d@%.4f" % (
                                len(got.get(str(b)) or []), len(ladder),
                                (
                                    sum(got[str(b)]) / len(got[str(b)])
                                    if got.get(str(b)) else 0.0
                                ),
                            )
                            for b, ladder in zip(seat["buckets"], ladders)
                            if ladder
                        }
                        # Sizing is done on the order that was *posted*, so
                        # a leg that filled nothing is still priced at its
                        # ladder's mean here -- what it would have paid.  The
                        # realised price only replaces it for the legs that
                        # bought something, and the rest drop out below.
                        posted = _maker_leg_prices(snapshot, seat["buckets"])
                        legs = [
                            [{
                                "price": resting_prices.get(
                                    str(bucket), float(price)
                                ),
                                "shares": sum(
                                    level["shares"]
                                    for level in (depth.get(bucket) or [])
                                ),
                            }]
                            for bucket, price in zip(seat["buckets"], posted)
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
                        leg_shares = [shares] * len(seat["buckets"])
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
                    # What was chosen and posted, before the book had a say.
                    # The charter's minimum support is a rule about
                    # selection, so it has to stay auditable even though the
                    # recorded window is now whatever actually filled.
                    posted_buckets = list(seat["buckets"])
                    # Top-ups only add.  The stage chose a target position;
                    # what it may buy is the part of that target the chain
                    # does not already hold, and a rung it wants less of
                    # than it has is simply left alone (user ruling,
                    # 2026-08-18).  P0 opens with an empty book, so this is
                    # the identity there and binds only on the later stages.
                    held_before = {
                        bucket: held[bucket]
                        for bucket in seat["buckets"] if bucket in held
                    }
                    if held:
                        topped: list[float] = []
                        spend = 0.0
                        for leg, bucket, want in zip(
                            legs, seat["buckets"], leg_shares
                        ):
                            delta = max(
                                0.0, float(want) - held.get(bucket, 0.0)
                            )
                            if delta <= 0.0:
                                topped.append(0.0)
                                continue
                            got, paid = _walk_ask_shares(leg, delta)
                            topped.append(got)
                            spend += paid
                        if sum(topped) <= 0.0:
                            refusal = (
                                "no_top_up_needed",
                                "本阶段目标 %s 已在仓内" % "+".join(
                                    seat["buckets"]
                                ),
                                cycles, payload["decision_at_utc"],
                            )
                            continue
                        leg_shares = topped
                        stake = spend * (1.0 + FEE_RATE)
                    # The order as posted, leg by leg, kept beside the one
                    # that came back.  Progressive entry overwrites
                    # ``leg_shares`` and ``stake`` with the filled part a
                    # few lines below, and the shortfall on a leg that
                    # bought nothing cannot be recovered afterwards: its
                    # kept share is zero, and the centre-tilt weights that
                    # sized it are not invertible from the legs that
                    # survived.  Recorded here so "posted 4 legs, got 1" is
                    # a readable number rather than an inference.
                    #
                    # The stake is priced at the ladder means the sizing
                    # itself used, so it answers "what the basket would have
                    # cost had every rung filled" -- the denominator the
                    # completion rate needs.
                    intended_legs = {
                        str(bucket): float(quantity)
                        for bucket, quantity in zip(
                            seat["buckets"], leg_shares
                        )
                    }
                    intended_stake = float(stake)
                    if EXECUTION_MODE == "maker_resting":
                        # The sizing above is the order that was posted.
                        # This is the position that came back.
                        entered = _progressive_entry(
                            buckets=seat["buckets"],
                            leg_shares=leg_shares,
                            filled_fraction=resting_fraction,
                            prices=resting_prices,
                            probabilities=probabilities,
                        )
                        if entered is None:
                            refusal = (
                                "resting_unfilled",
                                "挂 %d 腿 · 成交量为零" % len(seat["buckets"]),
                                cycles, payload["decision_at_utc"],
                            )
                            continue
                        kept, leg_shares, stake, win_probability = entered
                        if len(kept) < len(seat["buckets"]):
                            counts["resting_partial_basket"] += 1
                            ordinals = {
                                str(row[0]): int(row[1])
                                for row in payload["buckets"]
                            }
                            span = (
                                ordinals.get(kept[-1], 0)
                                - ordinals.get(kept[0], 0)
                            )
                            if span != len(kept) - 1:
                                counts["resting_gapped_basket"] += 1
                        seat = dict(seat)
                        seat["buckets"] = kept
                        seat["win_probability"] = win_probability
                    # The basket's own payout per unit spent.  Under equal
                    # shares this was cost over share count; weighted, the
                    # honest analogue is what the position expects to
                    # return, so ``shares`` becomes the expectation-
                    # equivalent count the settlement math already assumes.
                    # Two different questions, two different numbers.
                    #
                    # Settlement pays the rung that landed, so it reads the
                    # per-leg counts -- a single basket-level count would
                    # credit a payout no outcome produces.
                    #
                    # ``shares`` is the divisor of ``all_in_cost``, which the
                    # positive-EV gate, the dust floor and the whole page are
                    # built on, and it has to mean "cost per unit of payout".
                    # Taking the thinnest leg made that a number no outcome
                    # produces either, in the other direction: FACT
                    # 2026-07-31 recorded 0.3400 against a 0.2800
                    # top-of-book total -- a resting basket costing *more*
                    # than crossing, which cannot happen.  The
                    # payout-weighted count is the honest analogue and
                    # degenerates to the equal-share case exactly: with q_i
                    # all equal, sum(p_i q_i)/p_win = q.
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
                    if not credible:
                        counts["dust_fill_excluded_from_equity"] += 1
                    # The position this station-day now holds.  A stage adds
                    # to it; nothing here ever takes away.  Everything the
                    # chain settles on is read off these, not off the stage
                    # that happened to act last.
                    for bucket, quantity in per_leg.items():
                        held[bucket] = held.get(bucket, 0.0) + float(quantity)
                    chain_stake += stake
                    chain_probabilities = probabilities
                    chain_ladder = payload["buckets"]
                    chain_center_c = payload.get("center_c")
                    chain_last_stage = stage
                    if not chain_opened_at:
                        chain_opened_at = payload["decision_at_utc"]
                    identity = {
                        "station_id": station_id,
                        "event_date": event_date,
                        "stage": stage,
                        "decision_at_utc": payload["decision_at_utc"],
                        "window": list(seat["buckets"]),
                        "stake_usdc": round(stake, 6),
                    }
                    position_id = "paper-pos-" + content_sha256(identity)[:24]
                    chain_position_id = position_id
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
                            json.dumps(posted_buckets, ensure_ascii=False),
                            json.dumps(intended_legs, sort_keys=True),
                            intended_stake,
                            json.dumps(held_before, sort_keys=True),
                            json.dumps(held, sort_keys=True),
                            chain_stake,
                        )
                    )
                    counts["positions"] += 1
                    counts["entry_cycle_%s" % (
                        "first" if cycles == 1 else "later"
                    )] += 1
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
            # The chain is done adding.  One position, one settlement: the
            # truth lands once, and scoring each stage against it separately
            # is what charged 40 of 152 settled station-days two and three
            # times over.  What settles is the accumulated position, not the
            # window the last stage happened to choose.
            if not held:
                continue
            chain_win_probability = sum(
                float(chain_probabilities.get(bucket, 0.0)) for bucket in held
            )
            chain_expected_payout = sum(
                float(chain_probabilities.get(bucket, 0.0)) * quantity
                for bucket, quantity in held.items()
            )
            chain_shares = chain_expected_payout / max(
                chain_win_probability, 1e-9
            )
            chain_cost = (
                chain_stake / chain_shares if chain_shares > 0 else 0.0
            )
            chain_credible = chain_cost >= DUST_COST_FLOOR
            standing = truth.get((station_id, event_date))
            if standing is None:
                counts["open_unsettled"] += 1
                continue
            chain_ordinals = {
                str(row[0]): int(row[1]) for row in chain_ladder
            }
            chain_window = sorted(
                held, key=lambda bucket: chain_ordinals.get(bucket, 0)
            )
            scored = _score_settlement(
                ladder=chain_ladder,
                window=chain_window,
                stake=chain_stake,
                shares=chain_shares,
                per_leg=held,
                center_c=chain_center_c,
                truth_c=standing.tmax_c,
            )
            if scored is None:
                counts["truth_outside_book"] += 1
                continue
            pending_settlements.append(
                (
                    "paper-set-" + content_sha256({
                        "station_id": station_id,
                        "event_date": event_date,
                        "chain_opened_at": chain_opened_at,
                        "window": chain_window,
                        "stake_usdc": round(chain_stake, 6),
                        "truth": standing.tmax_c,
                    })[:24],
                    chain_position_id, station_id, event_date,
                    # The stage that last added to the position.  A chain is
                    # one bet, so this says how far it had been adjusted when
                    # the day resolved, not which of three books it belonged
                    # to.
                    chain_last_stage or "P0",
                    standing.tmax_c, scored.settled_bucket,
                    scored.hit, chain_stake, scored.payout_usdc,
                    scored.pnl_usdc,
                    chain_win_probability,
                    scored.center_error_c, scored.bucket_distance,
                    datetime.now(timezone.utc)
                    .isoformat().replace("+00:00", "Z"),
                    # The revision this score was struck against.  Without it
                    # the next sweep cannot tell a seat it scored correctly
                    # from one whose truth has since been corrected under it.
                    standing.revision_no, standing.content_sha256,
                    chain_cost,
                )
            )
            counts["settlements"] += 1
            if chain_credible:
                day_pnl += scored.pnl_usdc
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
        #
        # Chains this pass replayed are the one case where the two sets are
        # not disjoint: their old settlement is still in the table and their
        # new one is in ``day_pnl``.  Counting the recorded side of a chain
        # that has just been rewritten would compound a P&L this pass has
        # already superseded, so those stations are read out of the query
        # and taken from ``day_pnl`` alone.
        replayed_today = {
            station for station, day in pending_chain_resets
            if day == event_date
        }
        realised = 0.0
        for station, pnl in conn.execute(
            "SELECT s.station_id, SUM(s.pnl_usdc)"
            " FROM paper_trade_settlement s"
            " JOIN paper_trade_position p USING(position_id)"
            " WHERE s.event_date=? AND p.all_in_cost>=?"
            " GROUP BY s.station_id",
            (event_date, DUST_COST_FLOOR),
        ):
            if str(station) in replayed_today:
                continue
            realised += float(pnl or 0.0)
        # Settlements this pass created are still buffered for the final
        # write burst, so they are absent from the query above and are added
        # on top rather than replacing it.
        equity = max(0.0, equity + realised + day_pnl)
    # Every write in one short burst at the end.  The scan above reads most of
    # a ten-gigabyte database and takes about half a minute; holding the write
    # lock across it is what cost the resident whole cycles to
    # ``database is locked``, and the resident is right to give up fast --
    # "a concurrent maintenance writer must not consume a whole production
    # cadence" is its own stated rule.  Reads do not block it, so the only
    # thing that has to be brief is this, and a few dozen inserts are.
    # Replayed chains first: their rows were written stage by stage under a
    # reading where each stage was its own bet, and the rewrite below is one
    # position built up across them.  ``INSERT OR IGNORE`` would keep the old
    # rows and silently drop the new ones.
    if pending_chain_resets:
        conn.executemany(
            "DELETE FROM paper_trade_settlement WHERE position_id IN ("
            "  SELECT position_id FROM paper_trade_position"
            "  WHERE station_id=? AND event_date=?)",
            pending_chain_resets,
        )
        conn.executemany(
            "DELETE FROM paper_trade_position"
            " WHERE station_id=? AND event_date=?",
            pending_chain_resets,
        )
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
            " selection_rule_label, leg_shares_json, resting_fill_json,"
            " posted_buckets_json, intended_legs_json, intended_stake_usdc,"
            " held_before_json, chain_shares_json, chain_stake_usdc"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?)",
            pending_positions,
        )
    if pending_settlements:
        conn.executemany(
            "INSERT OR IGNORE INTO paper_trade_settlement ("
            " settlement_id, position_id, station_id, event_date, stage,"
            " settled_tmax_c, settled_bucket, hit, stake_usdc,"
            " payout_usdc, pnl_usdc, claimed_win_probability,"
            " center_error_c, bucket_distance, settled_at_utc,"
            " truth_revision_no, truth_content_sha256,"
            " basket_all_in_cost"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
    # Grouped by the stage that last topped the position up, which is not the
    # same question the three-book ledger answered.  These slices partition
    # one book: a chain appears in exactly one of them, and they sum to the
    # book below.  "How far had this position been adjusted when the day
    # resolved" is what a stage means now.
    for stage in STAGES:
        rows = conn.execute(
            "SELECT s.hit, s.stake_usdc, s.payout_usdc, s.pnl_usdc,"
            " s.claimed_win_probability, s.center_error_c,"
            " s.bucket_distance,"
            " COALESCE(s.basket_all_in_cost, p.all_in_cost)"
            " FROM paper_trade_settlement s"
            " JOIN paper_trade_position p ON p.position_id=s.position_id"
            " WHERE s.stage=? ORDER BY s.event_date",
            (stage,),
        ).fetchall()
        # A chain is open when its station-day has no settlement at all, and
        # it belongs to the slice of the stage that last topped it up -- the
        # same partition the settled counts use, so the three slices sum to
        # the book's open count instead of counting one position three times.
        open_positions = conn.execute(
            "SELECT COUNT(*) FROM paper_trade_position p"
            " JOIN (SELECT station_id, event_date,"
            "         MAX(decision_at_utc) AS last"
            "       FROM paper_trade_position GROUP BY 1, 2) t"
            "   ON t.station_id=p.station_id AND t.event_date=p.event_date"
            "  AND t.last=p.decision_at_utc"
            " WHERE p.stage=? AND NOT EXISTS ("
            "   SELECT 1 FROM paper_trade_settlement s"
            "   WHERE s.station_id=p.station_id"
            "     AND s.event_date=p.event_date)",
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
            # No equity of its own.  A slice is part of one book, and giving
            # each an initial capital is exactly the three-book reading this
            # ledger was corrected out of.
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
    # The book itself: one bankroll, one row per station-day.
    book_rows = conn.execute(
        "SELECT s.hit, s.stake_usdc, s.pnl_usdc, s.claimed_win_probability,"
        " COALESCE(s.basket_all_in_cost, p.all_in_cost)"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p ON p.position_id=s.position_id"
        " ORDER BY s.event_date"
    ).fetchall()
    credible_rows = [
        row for row in book_rows
        if row[4] is not None and float(row[4]) >= DUST_COST_FLOOR
    ]
    book_pnl = sum(float(r[2]) for r in credible_rows)
    book_staked = sum(float(r[1]) for r in credible_rows)
    book_hits = sum(int(r[0]) for r in credible_rows)
    open_chains = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT station_id, event_date FROM paper_trade_position p"
        "  WHERE NOT EXISTS (SELECT 1 FROM paper_trade_settlement s"
        "    WHERE s.station_id=p.station_id AND s.event_date=p.event_date)"
        "  GROUP BY station_id, event_date)"
    ).fetchone()[0]
    return {
        "schema": "paper_trading_report_v1",
        "initial_capital_usdc": INITIAL_CAPITAL_USDC,
        "kelly_fraction": KELLY_FRACTION,
        "max_stake_fraction_per_scope": MAX_STAKE_FRACTION_PER_SCOPE,
        "book_model": BOOK_MODEL,
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "book": {
            "settled_chains": len(credible_rows),
            "open_chains": open_chains,
            "hit_count": book_hits,
            "hit_rate": (
                book_hits / len(credible_rows) if credible_rows else None
            ),
            "staked_usdc": book_staked,
            "pnl_usdc": book_pnl,
            "return_on_stake": (
                book_pnl / book_staked if book_staked > 0 else None
            ),
            "equity_usdc": INITIAL_CAPITAL_USDC + book_pnl,
            "return_on_capital": book_pnl / INITIAL_CAPITAL_USDC,
            "dust_chains_excluded": len(book_rows) - len(credible_rows),
        },
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
