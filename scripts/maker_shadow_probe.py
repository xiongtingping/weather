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
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
# The rules a resting order could follow, priced off the book as it stands
# when the intent is written.  ``ask`` is not a maker order at all -- it is
# the crossing trade, carried as the control so every comparison is between
# two orders written at the same instant against the same book.
PRICE_RULES = ("bid", "bid+1t", "bid+2t", "mid", "ask")
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
PLACEMENT_INTERVAL_MINUTES = 15

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
    row = conn.execute(
        "SELECT envelope_json FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=?"
        " ORDER BY decision_at_utc DESC LIMIT 1",
        (station, day),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return {
        str(k): float(v)
        for k, v in (payload.get("probabilities") or {}).items()
    }


def _limit_price(rule: str, quote: dict[str, Any]) -> float | None:
    bid, ask, tick = quote["bid"], quote["ask"], quote["tick"]
    if ask <= 0.0 or ask >= 1.0:
        return None
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


def place(conn: sqlite3.Connection) -> dict[str, int]:
    """Write one round of intents against the freshest book."""

    ensure_schema(conn)
    counts: dict[str, int] = defaultdict(int)
    books = _latest_books(conn)
    if not books:
        return {"scopes_with_fresh_book": 0}
    cutoff = _stamp(_now() - timedelta(minutes=PLACEMENT_INTERVAL_MINUTES))
    recent = {
        (str(station), str(day))
        for station, day in conn.execute(
            "SELECT DISTINCT station_id, event_date FROM maker_shadow_order"
            " WHERE placed_at_utc >= ?",
            (cutoff,),
        )
    }
    pending: list[tuple] = []
    for (station, day), (moment, book) in books.items():
        counts["scopes_with_fresh_book"] += 1
        if (station, day) in recent:
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
        placed_at = _stamp(_now())
        book_at = _stamp(moment)
        for bucket, quote in book.items():
            if bucket not in probabilities:
                continue
            for rule in PRICE_RULES:
                price = _limit_price(rule, quote)
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
                        _stamp(_now() + timedelta(minutes=horizon)),
                        price, quote["bid"], quote["ask"], quote["tick"],
                        (
                            float(quote["queue_ahead"])
                            if quote["queue_ahead"] is not None else None
                        ),
                        1 if quote["queue_known"] else 0,
                        float(probabilities[bucket]), "OPEN", None, None,
                        digest,
                    ))
    if pending:
        conn.executemany(
            "INSERT OR IGNORE INTO maker_shadow_order ("
            " order_id, station_id, event_date, bucket, price_rule,"
            " horizon_minutes, placed_at_utc, book_observed_at_utc,"
            " expires_at_utc, limit_price,"
            " reference_bid, reference_ask, reference_tick,"
            " queue_ahead_shares, queue_known, model_probability, state,"
            " filled_at_utc, fill_observed_ask, content_sha256"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            pending,
        )
        conn.commit()
        counts["intents_written"] = len(pending)
    return dict(counts)


def resolve(conn: sqlite3.Connection) -> dict[str, int]:
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
        " placed_at_utc, expires_at_utc FROM maker_shadow_order"
        " WHERE state='OPEN'"
    ).fetchall()
    if not open_rows:
        return {"open": 0}
    by_scope: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for row in open_rows:
        by_scope[(str(row[1]), str(row[2]))].append(row)
    filled: list[tuple] = []
    expired: list[tuple] = []
    for (station, day), rows in by_scope.items():
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
            snapshot: dict[str, float] = {}
            for entry in entries:
                bucket = str(entry.get("bucket") or "")
                levels = entry.get("ask_levels") or []
                ask = entry.get("best_ask")
                if ask is None and levels:
                    ask = levels[0].get("price")
                if bucket and ask is not None:
                    snapshot[bucket] = float(ask)
            if snapshot:
                book_path.append((str(observed), snapshot))
        for order_id, _s, _d, bucket, limit, placed, expires in rows:
            hit_at = None
            observed_ask = None
            for observed, snapshot in book_path:
                if observed <= placed:
                    continue
                if observed > expires:
                    break
                ask = snapshot.get(str(bucket))
                if ask is not None and ask <= float(limit) + 1e-9:
                    hit_at, observed_ask = observed, ask
                    break
            if hit_at is not None:
                filled.append((hit_at, observed_ask, order_id))
            elif str(expires) < now:
                expired.append((order_id,))
    if filled:
        conn.executemany(
            "UPDATE maker_shadow_order SET state='FILLED',"
            " filled_at_utc=?, fill_observed_ask=? WHERE order_id=?",
            filled,
        )
        counts["filled"] = len(filled)
    if expired:
        conn.executemany(
            "UPDATE maker_shadow_order SET state='EXPIRED' WHERE order_id=?",
            expired,
        )
        counts["expired"] = len(expired)
    if filled or expired:
        conn.commit()
    counts["still_open"] = len(open_rows) - len(filled) - len(expired)
    return dict(counts)


def settle(conn: sqlite3.Connection) -> dict[str, int]:
    """Score filled intents whose station-day has a settled truth."""

    ensure_schema(conn)
    counts: dict[str, int] = defaultdict(int)
    rows = conn.execute(
        "SELECT o.order_id, o.station_id, o.event_date, o.bucket,"
        " o.limit_price FROM maker_shadow_order o"
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
    now = _stamp(_now())
    for order_id, station, day, bucket, limit in rows:
        actual = truth.get((str(station), str(day)))
        if actual is None:
            counts["awaiting_truth"] += 1
            continue
        key = (str(station), str(day))
        if key not in intervals_cache:
            row = conn.execute(
                "SELECT envelope_json FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? LIMIT 1",
                (station, day),
            ).fetchone()
            try:
                lineage = json.loads(row[0])["lineage"] if row else {}
                intervals_cache[key] = json.loads(
                    str(lineage.get("bucket_intervals_c_json") or "{}")
                )
            except (TypeError, ValueError, json.JSONDecodeError, KeyError):
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
    if pending:
        conn.executemany(
            "INSERT OR IGNORE INTO maker_shadow_settlement ("
            " order_id, station_id, event_date, settled_bucket, hit,"
            " limit_price, pnl_per_share, settled_at_utc"
            ") VALUES (?,?,?,?,?,?,?,?)",
            pending,
        )
        conn.commit()
        counts["settled"] = len(pending)
    return dict(counts)


def report(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_schema(conn)
    cells: list[dict[str, Any]] = []
    for rule, horizon, placed, filled in conn.execute(
        "SELECT price_rule, horizon_minutes, COUNT(*),"
        " SUM(CASE WHEN state='FILLED' THEN 1 ELSE 0 END)"
        " FROM maker_shadow_order"
        " WHERE state<>'OPEN' GROUP BY price_rule, horizon_minutes"
    ):
        scored = conn.execute(
            "SELECT COUNT(*), SUM(s.hit), AVG(s.limit_price),"
            " AVG(s.pnl_per_share), AVG(o.reference_ask)"
            " FROM maker_shadow_settlement s"
            " JOIN maker_shadow_order o USING(order_id)"
            " WHERE o.price_rule=? AND o.horizon_minutes=?",
            (rule, horizon),
        ).fetchone()
        count = int(scored[0] or 0)
        cells.append({
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
            key=lambda cell: (cell["horizon_minutes"], cell["price_rule"]),
        ),
    }


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
    print(json.dumps({
        "schema": "maker_shadow_pass_v1",
        "place": place(conn),
        "resolve": resolve(conn),
        "settle": settle(conn),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
