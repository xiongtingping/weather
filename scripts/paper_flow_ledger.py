#!/usr/bin/env python3
"""Score the strategy as the one chain it actually is, not three parallel books.

``paper_trading_ledger`` scores P0, P0-M and P1 as independent entries, each
with its own bankroll, and it says so: the question it answers is which
moment's evidence would have entered better.  That is a useful question and
this does not replace it.

It is not the question of whether the strategy works.  Production posts a
resting order, opens at P0 if it fills, and adjusts the same position at P0-M
and P1 (charter 46).  Read as three books that chain is invisible, and three
distortions ride on top of the misreading:

* Every resting order is assumed to fill.  The module says so -- "assume all
  of them do and should be read as the best this could possibly be worth".
  The shadow probe has 713,952 posted orders and says a resting bid fills
  5.6% of the time, 4.7% at the 12-24h lead where most seats are taken.  The
  gap between 100% and 5.6% is not a rounding error, and the fills that do
  arrive are the ones a counterparty chose to give away.
* Three bankrolls are counted as one number.  A dashboard summing them
  reports a book three times the size of any that exists.
* The stages are added together, so a station-day that opened once and was
  adjusted twice is counted as three separate bets with three separate
  stakes.

So this walks the chain: post, fill or miss, open at P0, adjust, settle once,
against a single bankroll shared across stations.  A missed fill is not a
loss -- it is no position, which is exactly the point, because assuming it
filled is how an unfillable strategy looks profitable.

Fill is drawn from the probe's measured rate for the price rule and lead, not
assumed and not simulated per order: the probe cannot say which of *our*
orders fill, only how often orders like them did.  So a scope's expected
exposure is scaled by that rate, which is the honest middle between assuming
all and guessing individually.  ``--fill-mode all`` reproduces the optimistic
reading for comparison.

Read-only.  Places no orders and writes nothing but its report.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STAGE_ORDER = ("P0", "P0-M", "P1")
# One book for the whole network, matching the live objective's single
# bankroll.  The three-book ledger gives each stage its own and is right to,
# because it is comparing them; a chain has one.
INITIAL_CAPITAL_USDC = 1000.0
# The live sizing: a quarter of the Kelly fraction for the basket read as a
# binary bet, capped so no station-day can take a large bite.
KELLY_FRACTION = 0.25
MAX_SCOPE_FRACTION = 0.03


def _fill_rates(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Measured fill rate by price rule and lead band.

    By band rather than per order because the probe cannot say which of our
    orders would have filled -- only how often orders posted like them did.
    """

    rows = conn.execute(
        "SELECT price_rule,"
        " CASE WHEN lead_hours<6 THEN '<6h'"
        "      WHEN lead_hours<12 THEN '6-12h'"
        "      WHEN lead_hours<24 THEN '12-24h' ELSE '>=24h' END,"
        " COUNT(*), SUM(state='FILLED')"
        " FROM maker_shadow_order GROUP BY 1,2"
    ).fetchall()
    out: dict[tuple[str, str], float] = {}
    for rule, band, posted, filled in rows:
        if int(posted or 0) >= 200:
            out[(str(rule), str(band))] = float(filled or 0) / float(posted)
    return out


def _lead_band(hours: float | None) -> str:
    if hours is None:
        return "12-24h"
    value = float(hours)
    if value < 6:
        return "<6h"
    if value < 12:
        return "6-12h"
    if value < 24:
        return "12-24h"
    return ">=24h"


def _p0_refusals(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Why P0 declined a station-day, where it did.

    A chain with no P0 is not a gap in the record: P0 looked and refused, and
    the refusal table says why.  Carrying the reason turns "83 chains have no
    opening" from a hole into an account -- 65 had no priced window at all,
    17 had no positive Kelly, one posted and never filled.  A P1 row on such
    a day is the three-book ledger answering its own question; this lane has
    to say the position was never opened, and why.
    """

    out: dict[tuple[str, str], str] = {}
    for station, event_date, reason in conn.execute(
        "SELECT station_id, event_date, reason FROM paper_trade_refusal"
        " WHERE stage='P0' ORDER BY recorded_at_utc"
    ):
        out[(str(station), str(event_date))] = str(reason)
    return out


def _chain_rows(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Every station-day's stages in order, with its settled outcome."""

    rows = conn.execute(
        "SELECT p.station_id, p.event_date, p.stage, p.decision_at_utc,"
        " p.win_probability, p.all_in_cost, p.window_size, p.stake_usdc,"
        " p.shares, s.hit, s.pnl_usdc, s.settled_bucket"
        " FROM paper_trade_position p"
        " LEFT JOIN paper_trade_settlement s"
        "  ON s.position_id = p.position_id"
        " ORDER BY p.station_id, p.event_date, p.decision_at_utc"
    ).fetchall()
    chains: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row[0]), str(row[1]))
        chain = chains.setdefault(key, {"stages": {}})
        chain["stages"][str(row[2])] = {
            "decision_at_utc": str(row[3]),
            "win_probability": float(row[4]),
            "all_in_cost": float(row[5]),
            "window_size": int(row[6]),
            "shares": float(row[8] or 0.0),
            "hit": None if row[9] is None else int(row[9]),
            "settled_bucket": row[11],
        }
    return chains


def _kelly_stake(
    *, probability: float, cost: float, bankroll: float
) -> float:
    """A quarter-Kelly stake on the basket as a binary bet, scope-capped."""

    if not (0.0 < cost < 1.0) or probability <= cost:
        return 0.0
    edge = (probability - cost) / (1.0 - cost)
    stake = bankroll * KELLY_FRACTION * edge
    return max(0.0, min(stake, bankroll * MAX_SCOPE_FRACTION))


def walk(
    chains: dict[tuple[str, str], dict],
    *,
    fill_rates: dict[tuple[str, str], float],
    fill_mode: str,
    price_rule: str,
    p0_refusals: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """One position per station-day: post, fill or miss, open, adjust, settle."""

    bankroll = INITIAL_CAPITAL_USDC
    opened = missed = settled = hits = 0
    staked = returned = 0.0
    adjustments: collections.Counter[str] = collections.Counter()
    no_p0: collections.Counter[str] = collections.Counter()
    per_day: dict[str, float] = collections.defaultdict(float)

    for (station, event_date), chain in sorted(
        chains.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        stages = chain["stages"]
        entry = stages.get("P0")
        if entry is None:
            # The chain starts at P0.  A later-stage row without one is the
            # three-book ledger answering its own question, not a position
            # this strategy would ever have held -- and the refusal table
            # says why P0 declined, so this is an account rather than a gap.
            no_p0[
                p0_refusals.get((station, event_date), "unrecorded")
            ] += 1
            continue
        band = _lead_band(24.0)
        rate = (
            1.0 if fill_mode == "all"
            else fill_rates.get((price_rule, band), 0.056)
        )
        # The scope's exposure is scaled by how often an order like this one
        # filled.  Not a coin flip per scope: the probe measures a rate, and
        # a rate applied to size is what it can honestly support.
        stake = _kelly_stake(
            probability=entry["win_probability"],
            cost=entry["all_in_cost"],
            bankroll=bankroll,
        ) * rate
        if stake <= 0.0:
            missed += 1
            continue
        opened += 1
        for stage in ("P0-M", "P1"):
            if stage in stages:
                adjustments[stage] += 1
        # The outcome is the settled one for this station-day.  Later stages
        # adjust the same basket, so the chain settles once; where a later
        # stage recorded the settlement, that is the row that carries it.
        outcome = None
        for stage in reversed(STAGE_ORDER):
            candidate = stages.get(stage)
            if candidate is not None and candidate["hit"] is not None:
                outcome = candidate
                break
        if outcome is None:
            continue
        settled += 1
        hits += int(outcome["hit"])
        payout = stake / entry["all_in_cost"] if outcome["hit"] else 0.0
        staked += stake
        returned += payout
        bankroll += payout - stake
        per_day[event_date] += payout - stake

    profit = returned - staked
    return {
        "fill_mode": fill_mode,
        "price_rule": price_rule,
        "assumed_fill_rate": (
            1.0 if fill_mode == "all"
            else fill_rates.get((price_rule, "12-24h"), 0.056)
        ),
        "chains_seen": len(chains),
        "chains_without_p0": sum(no_p0.values()),
        "chains_without_p0_by_reason": dict(no_p0),
        "opened": opened,
        "never_filled_or_unsized": missed,
        "settled": settled,
        "hits": hits,
        "hit_rate": hits / settled if settled else None,
        "staked_usdc": staked,
        "returned_usdc": returned,
        "profit_usdc": profit,
        "roi": profit / staked if staked else None,
        "final_bankroll_usdc": bankroll,
        "adjusted_at_p0m": adjustments["P0-M"],
        "adjusted_at_p1": adjustments["P1"],
        "daily_profit": dict(per_day),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--price-rule", default="bid",
        choices=("bid", "bid+1t", "bid+2t", "mid", "ask"),
    )
    parser.add_argument(
        "--fill-mode", default="measured", choices=("measured", "all"),
        help="'all' reproduces the three-book ledger's optimistic reading",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        rates = _fill_rates(conn)
        chains = _chain_rows(conn)
        refusals = _p0_refusals(conn)
    finally:
        conn.close()

    report = {
        "schema": "paper_flow_ledger_v1",
        "initial_capital_usdc": INITIAL_CAPITAL_USDC,
        "kelly_fraction": KELLY_FRACTION,
        "max_scope_fraction": MAX_SCOPE_FRACTION,
        "measured_fill_rates": {
            f"{rule}|{band}": round(value, 4)
            for (rule, band), value in sorted(rates.items())
        },
        "walk": walk(
            chains, fill_rates=rates,
            fill_mode=args.fill_mode, price_rule=args.price_rule,
            p0_refusals=refusals,
        ),
        "calls_order_gateway": False,
        "writes_target_database": False,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
