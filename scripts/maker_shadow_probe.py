#!/usr/bin/env python3
"""Record what a resting bid would have been, then watch the live book fill it.

Why this exists.  Every maker number so far came from replaying stored
snapshots, and each replay was wrong in a way only visible afterwards: the
first used "any later moment" as the fill window, so a losing bucket's price
decaying toward zero counted as a fill and read as adverse selection; the
second compared the subset that filled against the whole population, so a
selection effect read as a price effect.  Both are artefacts of choosing the
placement time after knowing the outcome.  This probe cannot make either
mistake, because it writes the order before the market moves and never
edits it.

It places nothing.  Each row is an intent -- station, bucket, limit price,
expiry -- stamped at a moment that had no knowledge of the future, and the
fill is decided later by whether the live book actually traded through it.
That is the closest thing to a live maker test that does not risk money,
and it is the measurement a real one would have to beat.

    python3 scripts/maker_shadow_probe.py            # one pass: place, fill, settle
    python3 scripts/maker_shadow_probe.py --report   # what the book has learned
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.pass_deadline import (  # noqa: E402
    HOSTED_SWEEP_CONTINUE_MARKER,
    writer_lease_seconds,
)
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    effective_edge_lambda_from_tables,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.effective_edge_store import (  # noqa: E402
    load_effective_edge_tables,
)
from bayes_basket_target.probability_bridge import (  # noqa: E402
    history_envelope_payload,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
# The rules a resting order could follow, priced off the book as it stands
# when the intent is written.  ``ask`` is not a maker order at all -- it is
# the crossing trade, carried as the control so every comparison is between
# two orders written at the same instant against the same book.
PRICE_RULES = (
    "bid", "bid+1t", "bid+2t", "mid", "ask",
    "bid+1t@chase", "bid+1t@then_take",
    "claim_cap", "claim_cap@0.8",
)
# The arms that price off the claim instead of off the book (2026-09-09).
#
# Every rule above answers "where is the other side resting?".  None answers
# "what is this bucket worth?", and production does not either: the gate
# knows a ceiling -- charter 68's ``claim x lambda(a)``, which is the claim
# after the fraction history says actually arrives -- and that ceiling has
# never reached the price.  ``delta_v2_runtime`` says so in as many words at
# the maker branch: "the rung is the price.  The reservation solve rides
# along for the audit."  So the book decides what we pay and the ceiling
# only checks afterwards that we did not overpay.
#
# The floor under that check is zero: ``minimum_expected_edge_per_share`` is
# 0.0, so a leg priced one thousandth under its own ceiling passes.  Lambda
# is fitted on tens to hundreds of seats per band, so a thousandth is inside
# its own error bar.
#
# These two arms invert the order: take the ceiling first, and rest at
# whichever is cheaper, it or the ladder rung.  ``claim_cap`` is the bare
# ceiling; ``claim_cap@0.8`` keeps a fifth of it as margin.  When the rung
# is already under the ceiling the arm posts the rung and is its own
# control -- the difference only appears on the legs production would have
# bought at a price its own gate can barely justify.
CLAIM_CAP_MARGIN = {"claim_cap": 0.0, "claim_cap@0.8": 0.2}
# Which stage's lambda table prices the ceiling.  P0 is the opening stage and
# the one this lane has always been about; a shadow order carries no stage of
# its own, so pinning it is a statement rather than an inference.
CLAIM_CAP_STAGE = "P0"
# The arm charter 108 asked for.  Every rule above rests at the price it was
# written at; charter 94(六) does not -- production cancels a rung that has
# fallen out of the bid-anchored ladder and re-mints it at the current bid,
# every few minutes, all day.  Replayed in the ledger that difference is
# enormous: one switch takes the winning leg's fill rate from 74.6% to 20.7%
# and the winner/loser asymmetry from 0.76 to 0.23, and 0.23 is 2.7x outside
# anything the fixed-limit rules above have ever measured.  Whether a real
# chased order behaves like the ledger's model of one has never been
# measured, and this is the rule that measures it: same placement, same fill
# test, same settlement, and a limit that re-derives from the current bid on
# the same cadence production re-mints at.
CHASING_RULES = frozenset({"bid+1t@chase"})
CHASE_REPRICE_MINUTES = 5.0
# The arm the paired analysis needed and could not fake.  Measured on the
# fixed rules, posting strictly dominates crossing on the orders it gets --
# same bucket, same day, same outcome, 1.86 cents per share cheaper -- and
# the orders it misses turned out to be no better than the ones it got
# (24.0% hit against 23.3%).  That says "post, and cross if it does not
# fill" beats crossing outright, but only if crossing LATER costs no more
# than crossing now, and the taker's own average fill price moves from
# 0.238 beyond twelve hours to 0.315 inside the last one.  Nothing measured
# that, so this rule does: it rests at bid+1t for its horizon and, if the
# book never came to it, pays whatever the ask is when the horizon ends.
TAKE_AFTER_RULES = frozenset({"bid+1t@then_take"})
BASE_RULE = {
    "bid+1t@chase": "bid+1t",
    "bid+1t@then_take": "bid+1t",
}
# How long the order rests.  Measured on stored books the spread saved grows
# with the horizon and so does the adverse selection, and the two cross
# somewhere in here; a live probe should not assume where.
HORIZON_MINUTES = (5, 15, 30, 60)
# A quote below this is the one-sided tail offer the ledger already refuses
# to treat as a price.
MINIMUM_LIMIT_PRICE = 0.01
# Only write a new intent for a scope this often.  The book updates about
# 144 times a minute; writing an intent per update would measure the
# sampler, not the strategy.
# ...but the opening window is sampled harder.  P0 is the entry stage
# (charter 46), and its book is the one no replay can reach: settled
# station-days have their snapshots overwhelmingly on the event day itself,
# so the >24h band had under 60 fills in the whole stored history while the
# event day had 4869.  That band is also where posting looks most feasible
# -- 89% of buckets carry a two-sided quote against 31% on the event day,
# and the relative spread is 50% against 33%.  It is the least measured and
# most promising part of the day, so it gets the tighter cadence.
PLACEMENT_INTERVAL_MINUTES = 15
OPENING_PLACEMENT_INTERVAL_MINUTES = 5
OPENING_LEAD_HOURS = 12.0

_ORDER_DDL = """
CREATE TABLE IF NOT EXISTS maker_shadow_order (
  order_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  bucket TEXT NOT NULL,
  price_rule TEXT NOT NULL,
  horizon_minutes INTEGER NOT NULL,
  placed_at_utc TEXT NOT NULL,
  book_observed_at_utc TEXT NOT NULL,
  expires_at_utc TEXT NOT NULL,
  lead_hours REAL NOT NULL,
  limit_price REAL NOT NULL,
  reference_bid REAL NOT NULL,
  reference_ask REAL NOT NULL,
  reference_tick REAL NOT NULL,
  queue_ahead_shares REAL,
  queue_known INTEGER NOT NULL,
  model_probability REAL,
  state TEXT NOT NULL CHECK(state IN ('OPEN','FILLED','EXPIRED')),
  filled_at_utc TEXT,
  fill_observed_ask REAL,
  -- What a chasing order's limit had become by the time it filled.  A
  -- fixed-limit order fills at ``limit_price`` and this stays NULL; a
  -- chasing one re-derives its limit all day, so settling it at the price
  -- it was WRITTEN at would score a trade nobody made.
  fill_limit_price REAL,
  -- ``maker`` when the book came to the resting price, ``crossed`` when the
  -- order gave up and paid the ask.  Inferring it from whether
  -- ``fill_limit_price`` is set would work today and stop working the next
  -- time a rule sets that column for its own reasons.
  fill_kind TEXT,
  content_sha256 TEXT NOT NULL,
  UNIQUE(station_id, event_date, bucket, price_rule,
         horizon_minutes, placed_at_utc)
)
"""
_SETTLEMENT_DDL = """
CREATE TABLE IF NOT EXISTS maker_shadow_settlement (
  order_id TEXT PRIMARY KEY
    REFERENCES maker_shadow_order(order_id),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settled_bucket TEXT NOT NULL,
  hit INTEGER NOT NULL CHECK(hit IN (0,1)),
  limit_price REAL NOT NULL,
  pnl_per_share REAL NOT NULL,
  settled_at_utc TEXT NOT NULL
)
"""
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_maker_shadow_open"
    " ON maker_shadow_order(state, expires_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_maker_shadow_scope"
    " ON maker_shadow_order(station_id, event_date)",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_ORDER_DDL)
    conn.execute(_SETTLEMENT_DDL)
    for statement in _INDEXES:
        conn.execute(statement)
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(maker_shadow_order)")
    }
    if "fill_kind" not in columns:
        conn.execute(
            "ALTER TABLE maker_shadow_order ADD COLUMN fill_kind TEXT"
        )
    if "fill_limit_price" not in columns:
        # NULL on every row written before the chasing arm existed, and on
        # every fixed-limit row after it: those fill at the price they were
        # written at, which ``limit_price`` already says.
        conn.execute(
            "ALTER TABLE maker_shadow_order ADD COLUMN fill_limit_price REAL"
        )


def _latest_books(
    conn: sqlite3.Connection, *, within_minutes: int = 20
) -> dict[tuple[str, str], tuple[datetime, dict[str, dict[str, Any]]]]:
    """The freshest committed book per scope, if it is recent enough."""

    floor = _stamp(_now() - timedelta(minutes=within_minutes))
    out: dict[tuple[str, str], tuple[datetime, dict[str, dict[str, Any]]]] = {}
    for station, day, observed, payload in conn.execute(
        "SELECT station_id, event_date, quote_observed_at, buckets_json"
        " FROM basket_official_ws_snapshot"
        " WHERE state='COMMITTED' AND quote_observed_at >= ?"
        " ORDER BY quote_observed_at",
        (floor,),
    ):
        try:
            rows = json.loads(payload)
            moment = _parse(observed)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        book: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = str(row.get("bucket") or "")
            levels = row.get("ask_levels") or []
            ask = row.get("best_ask")
            if ask is None and levels:
                ask = levels[0].get("price")
            if not bucket or ask is None:
                continue
            book[bucket] = {
                "ask": float(ask),
                "bid": float(row.get("bid_price") or 0.0),
                "tick": float(row.get("price_tick_size") or 0.001),
                "queue_ahead": row.get("maker_queue_ahead_shares"),
                "queue_known": bool(row.get("maker_queue_known")),
                "depth": sum(
                    float(level.get("shares") or 0.0) for level in levels
                ),
            }
        if book:
            out[(str(station), str(day))] = (moment, book)
    return out


def _model_probabilities(
    conn: sqlite3.Connection, station: str, day: str
) -> dict[str, float]:
    # Freshest by observation stage first, clock second.  P0 recomputes all
    # day (charter 46) but its chain carries no observation, so after dark a
    # P0 row is a forecast written after the day's high was already measured
    # and would override the P1 belief that saw it.  Measured on the settled
    # book: such rows held 46.9% of mass on buckets the reported high had
    # excluded, against 3.5% for the same station-days' last P1 row.
    row = conn.execute(
        "SELECT envelope_json, checkpoint_id, envelope_content_sha256"
        " FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=?"
        " ORDER BY (stage='P1') DESC, decision_at_utc DESC LIMIT 1",
        (station, day),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = history_envelope_payload(
            conn,
            checkpoint_id=str(row[1]),
            envelope_content_sha256=str(row[2]),
            stored=str(row[0]) if row[0] else None,
        )
    except (ContractError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return {
        str(k): float(v)
        for k, v in (payload.get("probabilities") or {}).items()
    }


def _floor_to_tick(price: float, tick: float) -> float:
    if tick <= 0.0:
        return price
    return math.floor(price / tick + 1e-9) * tick


def _limit_price(
    rule: str,
    quote: dict[str, Any],
    *,
    probability: float | None = None,
    lambda_tables: Any = None,
) -> float | None:
    rule = BASE_RULE.get(rule, rule)
    bid, ask, tick = quote["bid"], quote["ask"], quote["tick"]
    if ask <= 0.0 or ask >= 1.0:
        return None
    if rule in CLAIM_CAP_MARGIN:
        # The ladder rung this arm is measured against, then the ceiling.
        if bid <= 0.0 or probability is None or probability <= 0.0:
            return None
        rung = min(bid + tick, ask)
        if rung <= 0.0:
            return None
        # ``a`` is read at the rung: what the market's agreement would be if
        # this leg were posted the way production posts it.  Lambda is a
        # step function of a, so reading it at the price we would otherwise
        # pay is the honest lookup -- and it makes the ceiling a property of
        # this book rather than of the price we are about to choose.
        lam = float(effective_edge_lambda_from_tables(
            CLAIM_CAP_STAGE, rung / float(probability), lambda_tables
        ))
        ceiling = float(probability) * lam * (1.0 - CLAIM_CAP_MARGIN[rule])
        price = min(rung, _floor_to_tick(ceiling, tick))
        if price <= 0.0:
            return None
        return round(price, 6)
    if rule == "ask":
        return ask
    if bid <= 0.0:
        # Nothing resting on the bid: a maker order would be alone on its
        # side, which is the one-sided quote this system does not price.
        return None
    if rule == "bid":
        price = bid
    elif rule == "bid+1t":
        price = min(bid + tick, ask)
    elif rule == "bid+2t":
        price = min(bid + 2 * tick, ask)
    elif rule == "mid":
        price = (bid + ask) / 2.0
    else:
        raise ValueError(f"unknown price rule: {rule}")
    return round(price, 6)


# One pass of this probe is three loops over every open scope, and on the
# 2026-09-14 book it ran seven minutes (once, past its 900s timeout) while
# holding the resident's SSOT writer -- production waited 422s behind it at
# 06:57Z on 2026-09-15.  The pass is bounded by the writer lease the resident
# grants (``pass_deadline.writer_lease_seconds``): each loop stops between
# scopes once the clock is spent, commits what it has, and the pass asks for
# the next one with the continuation marker.  Every row it leaves behind is
# picked up where it stopped -- an intent not placed this pass is placed on
# the next book, an OPEN row is resolved later, a fill is settled later.
PASS_BUDGET_SEC = writer_lease_seconds(600.0)


def _spent(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _left(deadline: float | None) -> float:
    """Seconds of pass budget remaining; unbounded when there is no clock."""

    return float("inf") if deadline is None else deadline - time.monotonic()


# Rows written per commit, inside every loop rather than once after it.
#
# The loops were bounded and their writes were not.  Each phase gathered its
# rows while the clock ran and wrote them all in one executemany after the
# loop had stopped -- and on this cadence a phase gathers a lot: the sweep
# runs every three hours against a fifteen-minute placement throttle, so
# every pass writes a full round.  Measured 2026-09-16T07:34Z, the first pass
# after the scan was indexed: place 14,028 inserts and resolve 16,080 updates
# into a 4.7M-row table carrying five indexes, all of it after the deadline,
# and the pass ran 337s against a 240s lease.  Flushing every FLUSH_ROWS
# bounds what a spent clock can still owe to one chunk.  Every write here is
# idempotent -- INSERT OR IGNORE on a content hash, or an UPDATE that takes a
# row out of the set the next pass selects -- so a pass that stops between
# chunks leaves nothing the next one cannot finish.
FLUSH_ROWS = 2000

PLACE_INSERT_SQL = (
    "INSERT OR IGNORE INTO maker_shadow_order ("
    " order_id, station_id, event_date, bucket, price_rule,"
    " horizon_minutes, placed_at_utc, book_observed_at_utc,"
    " expires_at_utc, lead_hours, limit_price,"
    " reference_bid, reference_ask, reference_tick,"
    " queue_ahead_shares, queue_known, model_probability, state,"
    " filled_at_utc, fill_observed_ask, content_sha256"
    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
RESOLVE_FILL_SQL = (
    "UPDATE maker_shadow_order SET state='FILLED',"
    " filled_at_utc=?, fill_observed_ask=?, fill_limit_price=?,"
    " fill_kind=? WHERE order_id=?"
)
RESOLVE_EXPIRE_SQL = (
    "UPDATE maker_shadow_order SET state='EXPIRED' WHERE order_id=?"
)
SETTLE_INSERT_SQL = (
    "INSERT OR IGNORE INTO maker_shadow_settlement ("
    " order_id, station_id, event_date, settled_bucket, hit,"
    " limit_price, pnl_per_share, settled_at_utc"
    ") VALUES (?,?,?,?,?,?,?,?)"
)


def _flush(conn: sqlite3.Connection, sql: str, rows: list[tuple]) -> int:
    """Write and commit ``rows``; return how many, and empty the buffer."""

    if not rows:
        return 0
    conn.executemany(sql, rows)
    conn.commit()
    written = len(rows)
    rows.clear()
    return written


# What the claim_cap reading needs before it is allowed to start.  It is two
# self-joins over 4.7M orders -- 21.0s and 26.4s measured warm on 2026-09-16
# -- and it cannot stop half way, so the pass reserves room for it rather
# than merely checking that the clock has not already run out.
CLAIM_CAP_READING_RESERVE_SEC = 90.0


def place(
    conn: sqlite3.Connection, *, deadline: float | None = None
) -> dict[str, int]:
    """Write one round of intents against the freshest book."""

    ensure_schema(conn)
    counts: dict[str, int] = defaultdict(int)
    books = _latest_books(conn)
    if not books:
        return {"scopes_with_fresh_book": 0}
    now = _now()
    # One load per round: the served tables, exactly as the gate reads them.
    try:
        lambda_tables = load_effective_edge_tables(conn)
    except Exception:  # noqa: BLE001 -- no store, no ceiling, other arms run
        lambda_tables = None
    widest = _stamp(now - timedelta(minutes=PLACEMENT_INTERVAL_MINUTES))
    last_placed: dict[tuple[str, str], str] = {}
    for station, day, when in conn.execute(
        "SELECT station_id, event_date, MAX(placed_at_utc)"
        " FROM maker_shadow_order WHERE placed_at_utc >= ?"
        " GROUP BY station_id, event_date",
        (widest,),
    ):
        last_placed[(str(station), str(day))] = str(when)
    pending: list[tuple] = []
    written = 0
    for (station, day), (moment, book) in books.items():
        if len(pending) >= FLUSH_ROWS:
            written += _flush(conn, PLACE_INSERT_SQL, pending)
        if _spent(deadline):
            counts["stopped_for_time"] += 1
            break
        counts["scopes_with_fresh_book"] += 1
        try:
            lead_hours = (
                datetime.fromisoformat(f"{day}T00:00:00+00:00") - now
            ).total_seconds() / 3600.0
        except ValueError:
            continue
        opening = lead_hours >= OPENING_LEAD_HOURS
        counts["opening_window" if opening else "late_window"] += 1
        interval = (
            OPENING_PLACEMENT_INTERVAL_MINUTES
            if opening else PLACEMENT_INTERVAL_MINUTES
        )
        previous = last_placed.get((station, day))
        if previous and _parse(previous) > now - timedelta(minutes=interval):
            counts["scope_throttled"] += 1
            continue
        probabilities = _model_probabilities(conn, station, day)
        if not probabilities:
            counts["scope_without_model"] += 1
            continue
        # The intent is stamped now, not when the book was observed.  A
        # snapshot even a minute old already has successor rows in the
        # table, and resolving against those would be backtesting again --
        # the exact failure this probe exists to avoid.  The price still
        # comes from that book, because that is what a trader placing an
        # order at this instant would be looking at.
        placed_at = _stamp(now)
        book_at = _stamp(moment)
        for bucket, quote in book.items():
            if bucket not in probabilities:
                continue
            for rule in PRICE_RULES:
                price = _limit_price(
                    rule, quote,
                    probability=float(probabilities[bucket]),
                    lambda_tables=lambda_tables,
                )
                if price is None or price < MINIMUM_LIMIT_PRICE:
                    counts["leg_below_floor_or_one_sided"] += 1
                    continue
                for horizon in HORIZON_MINUTES:
                    identity = {
                        "station_id": station,
                        "event_date": day,
                        "bucket": bucket,
                        "price_rule": rule,
                        "horizon_minutes": horizon,
                        "placed_at_utc": placed_at,
                        "book_observed_at_utc": book_at,
                        "limit_price": price,
                    }
                    digest = content_sha256(identity)
                    pending.append((
                        "mso-" + digest[:24], station, day, bucket, rule,
                        horizon, placed_at, book_at,
                        _stamp(now + timedelta(minutes=horizon)),
                        round(lead_hours, 4),
                        price, quote["bid"], quote["ask"], quote["tick"],
                        (
                            float(quote["queue_ahead"])
                            if quote["queue_ahead"] is not None else None
                        ),
                        1 if quote["queue_known"] else 0,
                        float(probabilities[bucket]), "OPEN", None, None,
                        digest,
                    ))
    written += _flush(conn, PLACE_INSERT_SQL, pending)
    if written:
        counts["intents_written"] = written
    return dict(counts)


def resolve(
    conn: sqlite3.Connection, *, deadline: float | None = None
) -> dict[str, int]:
    """Decide open intents against books that arrived after they were written.

    Fill rule: the live book traded at or through the limit at some moment
    inside the order's life.  This ignores queue position, so it is the
    optimistic end -- a real order behind size at the same price might not
    have filled.  ``queue_ahead_shares`` is stored on every intent so that
    correction can be measured later rather than assumed now.
    """

    ensure_schema(conn)
    counts: dict[str, int] = defaultdict(int)
    now = _stamp(_now())
    open_rows = conn.execute(
        "SELECT order_id, station_id, event_date, bucket, limit_price,"
        " placed_at_utc, expires_at_utc, price_rule"
        " FROM maker_shadow_order"
        " WHERE state='OPEN'"
    ).fetchall()
    if not open_rows:
        return {"open": 0}
    by_scope: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for row in open_rows:
        by_scope[(str(row[1]), str(row[2]))].append(row)
    filled: list[tuple] = []
    expired: list[tuple] = []
    n_filled = 0
    n_expired = 0
    for (station, day), rows in by_scope.items():
        if len(filled) >= FLUSH_ROWS:
            n_filled += _flush(conn, RESOLVE_FILL_SQL, filled)
        if len(expired) >= FLUSH_ROWS:
            n_expired += _flush(conn, RESOLVE_EXPIRE_SQL, expired)
        if _spent(deadline):
            counts["stopped_for_time"] += 1
            break
        earliest = min(str(row[5]) for row in rows)
        book_path: list[tuple[str, dict[str, float]]] = []
        for observed, payload in conn.execute(
            "SELECT quote_observed_at, buckets_json"
            " FROM basket_official_ws_snapshot"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            "   AND quote_observed_at > ?"
            " ORDER BY quote_observed_at",
            (station, day, earliest),
        ):
            try:
                entries = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            snapshot: dict[str, dict[str, Any]] = {}
            for entry in entries:
                bucket = str(entry.get("bucket") or "")
                levels = entry.get("ask_levels") or []
                ask = entry.get("best_ask")
                if ask is None and levels:
                    ask = levels[0].get("price")
                if not bucket or ask is None:
                    continue
                # The bid and tick ride along for the chasing arm only; the
                # fixed rules read ``ask`` and nothing else, exactly as
                # before.
                snapshot[bucket] = {
                    "ask": float(ask),
                    "bid": float(entry.get("bid_price") or 0.0),
                    "tick": float(entry.get("price_tick_size") or 0.001),
                }
            if snapshot:
                book_path.append((str(observed), snapshot))
        for (order_id, _s, _d, bucket, limit, placed, expires,
             rule) in rows:
            hit_at = None
            observed_ask = None
            fill_limit = None
            chasing = str(rule) in CHASING_RULES
            live_limit = float(limit)
            next_reprice = (
                _parse(str(placed))
                + timedelta(minutes=CHASE_REPRICE_MINUTES)
                if chasing else None
            )
            for observed, snapshot in book_path:
                if observed <= placed:
                    continue
                if observed > expires:
                    break
                quote = snapshot.get(str(bucket))
                if quote is None:
                    continue
                if chasing and next_reprice is not None:
                    at = _parse(str(observed))
                    while at >= next_reprice:
                        # Re-anchor before testing this tick, for the same
                        # reason the ledger does: an order moved to a new
                        # price is live at that price from the moment it is
                        # moved, and testing the old one first would credit
                        # a fill to a price already cancelled.
                        # Only the ladder rules chase, and BASE_RULE maps
                        # them to a book-priced rule, so no claim is needed
                        # here.  A claim-capped rule never enters this path
                        # (it is not in CHASING_RULES).
                        repriced = _limit_price(str(rule), quote)
                        if (
                            repriced is not None
                            and repriced >= MINIMUM_LIMIT_PRICE
                        ):
                            live_limit = repriced
                        next_reprice = next_reprice + timedelta(
                            minutes=CHASE_REPRICE_MINUTES
                        )
                if quote["ask"] <= live_limit + 1e-9:
                    hit_at, observed_ask = observed, quote["ask"]
                    fill_limit = live_limit if chasing else None
                    break
            if hit_at is not None:
                filled.append((
                    hit_at, observed_ask, fill_limit, "maker", order_id,
                ))
                continue
            if str(rule) in TAKE_AFTER_RULES:
                # The horizon ran out and the book never came.  This rule
                # does not walk away -- it pays the ask at that moment, and
                # what it pays is the number the whole arm exists to
                # measure.  Still OPEN while no book has arrived after the
                # expiry: the crossing price is a fact about a book, and
                # waiting for one is not the same as guessing it.
                crossing = next(
                    (
                        snapshot[str(bucket)]["ask"]
                        for observed, snapshot in book_path
                        if observed >= expires and str(bucket) in snapshot
                    ),
                    None,
                )
                if crossing is not None:
                    filled.append((
                        str(expires), crossing, crossing, "crossed", order_id,
                    ))
                continue
            if str(expires) < now:
                expired.append((order_id,))
    n_filled += _flush(conn, RESOLVE_FILL_SQL, filled)
    n_expired += _flush(conn, RESOLVE_EXPIRE_SQL, expired)
    if n_filled:
        counts["filled"] = n_filled
    if n_expired:
        counts["expired"] = n_expired
    counts["still_open"] = len(open_rows) - n_filled - n_expired
    return dict(counts)


def settle(
    conn: sqlite3.Connection, *, deadline: float | None = None
) -> dict[str, int]:
    """Score filled intents whose station-day has a settled truth."""

    ensure_schema(conn)
    counts: dict[str, int] = defaultdict(int)
    rows = conn.execute(
        "SELECT o.order_id, o.station_id, o.event_date, o.bucket,"
        " COALESCE(o.fill_limit_price, o.limit_price)"
        " FROM maker_shadow_order o"
        " LEFT JOIN maker_shadow_settlement s USING(order_id)"
        " WHERE o.state='FILLED' AND s.order_id IS NULL"
    ).fetchall()
    if not rows:
        return {"unsettled_fills": 0}
    truth: dict[tuple[str, str], float] = {}
    for station, day, value in conn.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth t"
        " WHERE revision_no = (SELECT MAX(revision_no)"
        "   FROM calibration_station_daily_high_truth x"
        "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date)"
    ):
        truth[(str(station), str(day))] = float(value)
    intervals_cache: dict[tuple[str, str], dict[str, Any]] = {}
    pending: list[tuple] = []
    settled = 0
    now = _stamp(_now())
    for order_id, station, day, bucket, limit in rows:
        if len(pending) >= FLUSH_ROWS:
            settled += _flush(conn, SETTLE_INSERT_SQL, pending)
        if _spent(deadline):
            counts["stopped_for_time"] += 1
            break
        actual = truth.get((str(station), str(day)))
        if actual is None:
            counts["awaiting_truth"] += 1
            continue
        key = (str(station), str(day))
        if key not in intervals_cache:
            row = conn.execute(
                "SELECT envelope_json, checkpoint_id, envelope_content_sha256"
                " FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? LIMIT 1",
                (station, day),
            ).fetchone()
            try:
                lineage = history_envelope_payload(
                    conn,
                    checkpoint_id=str(row[1]),
                    envelope_content_sha256=str(row[2]),
                    stored=str(row[0]) if row[0] else None,
                )["lineage"] if row else {}
                intervals_cache[key] = json.loads(
                    str(lineage.get("bucket_intervals_c_json") or "{}")
                )
            except (ContractError, TypeError, ValueError,
                    json.JSONDecodeError, KeyError):
                intervals_cache[key] = {}
        intervals = intervals_cache[key]
        landed = None
        for name, bounds in intervals.items():
            lower, upper = bounds[0], bounds[1]
            if (lower is None or actual >= float(lower)) and (
                upper is None or actual < float(upper)
            ):
                landed = str(name)
                break
        if landed is None:
            counts["truth_outside_book"] += 1
            continue
        hit = 1 if landed == str(bucket) else 0
        price = float(limit)
        pending.append((
            order_id, station, day, landed, hit, price,
            (1.0 - price) if hit else -price, now,
        ))
    settled += _flush(conn, SETTLE_INSERT_SQL, pending)
    if settled:
        counts["settled"] = settled
    return dict(counts)


def report(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_schema(conn)
    cells: list[dict[str, Any]] = []
    # Lead band matters more than either knob: the opening book carries a
    # two-sided quote on 89% of buckets against 31% on the event day, so a
    # resting order is a different instrument in the two.
    band = (
        "CASE WHEN lead_hours >= 24 THEN 'open_gt24h'"
        " WHEN lead_hours >= 12 THEN 'open_12_24h'"
        " WHEN lead_hours >= 0 THEN 'pre_event_0_12h'"
        " ELSE 'event_day' END"
    )
    for rule, horizon, lead_band, placed, filled in conn.execute(
        f"SELECT price_rule, horizon_minutes, {band}, COUNT(*),"
        " SUM(CASE WHEN state='FILLED' THEN 1 ELSE 0 END)"
        " FROM maker_shadow_order"
        f" WHERE state<>'OPEN' GROUP BY price_rule, horizon_minutes, {band}"
    ):
        scored = conn.execute(
            "SELECT COUNT(*), SUM(s.hit), AVG(s.limit_price),"
            " AVG(s.pnl_per_share), AVG(o.reference_ask)"
            " FROM maker_shadow_settlement s"
            " JOIN maker_shadow_order o USING(order_id)"
            f" WHERE o.price_rule=? AND o.horizon_minutes=? AND {band}=?",
            (rule, horizon, lead_band),
        ).fetchone()
        count = int(scored[0] or 0)
        cells.append({
            "lead_band": str(lead_band),
            "price_rule": str(rule),
            "horizon_minutes": int(horizon),
            "placed": int(placed or 0),
            "filled": int(filled or 0),
            "fill_rate": (filled or 0) / max(placed or 1, 1),
            "settled": count,
            "hit_rate": (scored[1] or 0) / count if count else None,
            "mean_limit_price": scored[2],
            "mean_pnl_per_share": scored[3],
            "mean_reference_ask": scored[4],
        })
    return {
        "schema": "maker_shadow_report_v1",
        "generated_at_utc": _stamp(_now()),
        "note": (
            "Fill assumes queue priority at the limit; queue_ahead_shares is"
            " recorded per intent so that assumption can be corrected."
        ),
        "cells": sorted(
            cells,
            key=lambda cell: (
                cell["lead_band"],
                cell["horizon_minutes"],
                cell["price_rule"],
            ),
        ),
    }


def claim_cap_reading(conn: sqlite3.Connection) -> str:
    """One paired line: what pricing off the claim did to fills and to pnl.

    Paired, never pooled.  The arm drops a leg whose ceiling falls under the
    one-cent floor, so its surviving population runs at a higher claim than
    bid+1t's (0.258 against 0.195 on the first round) -- comparing the two
    means would read that selection as a result.  The five columns the two
    rules share (station, date, bucket, horizon, placed_at) are a unique key
    by construction, so the join is the comparison.

    Two numbers decide this arm and they have to be read together: the fill
    rate says whether the ceiling priced us out of the market, and the pnl
    per filled share says whether the legs it did buy were worth more.  A
    fill rate that halves while pnl stands still is a dead arm; a fill rate
    that gives up a fifth while pnl doubles is the point.
    """

    try:
        row = conn.execute(
            "SELECT COUNT(*),"
            " SUM(CASE WHEN cap.limit_price < rung.limit_price THEN 1 ELSE 0 END),"
            " AVG(rung.limit_price - cap.limit_price),"
            " SUM(CASE WHEN cap.state='FILLED' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN rung.state='FILLED' THEN 1 ELSE 0 END)"
            " FROM maker_shadow_order cap"
            " JOIN maker_shadow_order rung"
            "   ON rung.station_id=cap.station_id"
            "  AND rung.event_date=cap.event_date"
            "  AND rung.bucket=cap.bucket"
            "  AND rung.horizon_minutes=cap.horizon_minutes"
            "  AND rung.placed_at_utc=cap.placed_at_utc"
            "  AND rung.price_rule='bid+1t'"
            " WHERE cap.price_rule='claim_cap'"
        ).fetchone()
        scored = conn.execute(
            "SELECT COUNT(*), AVG(scap.pnl_per_share), AVG(srung.pnl_per_share)"
            " FROM maker_shadow_order cap"
            " JOIN maker_shadow_order rung"
            "   ON rung.station_id=cap.station_id"
            "  AND rung.event_date=cap.event_date"
            "  AND rung.bucket=cap.bucket"
            "  AND rung.horizon_minutes=cap.horizon_minutes"
            "  AND rung.placed_at_utc=cap.placed_at_utc"
            "  AND rung.price_rule='bid+1t'"
            " JOIN maker_shadow_settlement scap ON scap.order_id=cap.order_id"
            " JOIN maker_shadow_settlement srung ON srung.order_id=rung.order_id"
            " WHERE cap.price_rule='claim_cap'"
        ).fetchone()
    except sqlite3.Error:
        return "  claim_cap: lane not present"
    paired = int(row[0] or 0)
    if not paired:
        return "  claim_cap: no paired legs yet"
    capped = int(row[1] or 0)
    cut = float(row[2] or 0.0)
    cap_filled, rung_filled = int(row[3] or 0), int(row[4] or 0)
    line = (
        f"  claim_cap vs bid+1t: paired {paired}, ceiling bound {capped}"
        f" ({capped / paired * 100:.0f}%), mean cut {cut:+.4f}"
        f"  |  filled {cap_filled} vs {rung_filled}"
    )
    both = int(scored[0] or 0)
    if both:
        line += (
            f"  |  settled both {both}: pnl/share"
            f" {float(scored[1] or 0.0):+.4f} vs {float(scored[2] or 0.0):+.4f}"
        )
    else:
        line += "  |  no leg settled on both rules yet"
    return line


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.report:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True,
                               timeout=60.0)
        print(json.dumps(report(conn), ensure_ascii=False, sort_keys=True))
        return 0
    conn = sqlite3.connect(str(database), timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    # Never checkpoint from here: this database's WAL sits far past the
    # autocheckpoint threshold, and folding it while the resident writes is
    # how a maintenance pass takes production down.
    conn.execute("PRAGMA wal_autocheckpoint=0")
    deadline = time.monotonic() + PASS_BUDGET_SEC
    passes = {
        "schema": "maker_shadow_pass_v1",
        "pass_budget_sec": PASS_BUDGET_SEC,
        "place": place(conn, deadline=deadline),
        "resolve": resolve(conn, deadline=deadline),
        "settle": settle(conn, deadline=deadline),
    }
    # The daily line a person reads, ahead of the JSON -- but only on a pass
    # that still has clock.  It is two self-joins over 4.7M orders, measured
    # 2026-09-16 at 21.0s and 26.4s, and it sat outside the lease entirely:
    # the three loops stop when the budget is spent and then this ran anyway,
    # for 47 seconds of exclusive SSOT writer that nothing was bounding.
    #
    # Skipped rather than sliced because it is a reading, not work: it
    # answers the same question on the next pass, and a pass that ran out of
    # clock has already asked for one with the continuation marker.  Said out
    # loud, because a number that silently stops appearing reads as an arm
    # that stopped rather than a line that was not affordable.
    #
    # "Not spent" is not the test.  The reading is a single uninterruptible
    # statement pair, so a pass with one second left would pass that check
    # and then run for forty-seven -- the same "the gate between units does
    # not bound one unit" that this whole pass was killed for.  It runs only
    # with room for it: measured 21.0s + 26.4s warm, and the reserve is
    # roughly double so a cold cache still lands inside the lease.
    if _spent(deadline) or _left(deadline) < CLAIM_CAP_READING_RESERVE_SEC:
        print("  claim_cap: 本轮预算不够读这两条自连接，跳过（下一轮再读）")
    else:
        print(claim_cap_reading(conn))
    print(json.dumps(passes, ensure_ascii=False, sort_keys=True))
    if any(
        isinstance(phase, Mapping) and phase.get("stopped_for_time")
        for phase in (passes["place"], passes["resolve"], passes["settle"])
    ):
        print(HOSTED_SWEEP_CONTINUE_MARKER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
