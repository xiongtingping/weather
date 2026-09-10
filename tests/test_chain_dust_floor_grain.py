"""The dust floor is a fact about the basket, so it is read off the chain.

A basket costing under a cent is a minimum-tick offer with nothing bid
against it, and the ledger drops such chains from its own accounting
(``chain_credible = chain_cost >= DUST_COST_FLOOR``).  The board applied
the same floor -- to the wrong column.  A position is topped up across
stages, so the row a settlement points at is whichever stage last touched
it, and that stage can be a third-of-a-cent add-on to a basket that cost
thirty-five cents.  Reading the floor off that row dropped the whole chain.

Measured against the live book on 2026-09-10, three chains -- VILK 08-25,
VILK 09-08, ZSQD 09-08, priced 0.35, 0.24 and 0.46 -- were being dropped
from the daily table, the coverage strip and the basket-hit table.  They
carried 15.07 USDC of stake and -10.06 of P&L, which is why the page
showed the book at -84.77 in three places and -95.08 in the portfolio card
that reads the settlement table directly.  A reader had no way to tell
which was the book.

The odds column had the matching defect for the matching reason: it took
the tail stage's own price, so 2026-08-25 -- one real basket beside one
0.3-cent add-on -- priced out at 3.5 cents and printed 27.17x.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402
import selection_market_dashboard as SM  # noqa: E402


def _row(
    station: str,
    day: str,
    stage: str,
    cost: float,
    chain_cost: float,
    tail: bool,
    stake: float,
    pnl: float,
    hit: int,
) -> dict:
    """One selection row, carrying both prices the page can filter on."""

    return {
        "station_id": station, "event_date": day, "stage": stage,
        "decision_at_utc": f"{day}T00:00:00Z",
        "all_in_cost": cost, "chain_all_in_cost": chain_cost,
        "dust_offer": cost < SM.DUST_COST_FLOOR,
        "chain_dust_offer": chain_cost < SM.DUST_COST_FLOOR,
        "chain_tail": tail, "settled": True, "hit": hit, "pnl_usdc": pnl,
        "chain_stake_usdc": stake, "stake_usdc": stake,
        "win_probability": 0.5, "seat_ask_total": cost, "edge": 0.0,
        "entry": {"posted_leg_count": 3, "filled_leg_count": 2},
        "side": "aligned", "ladder_side": "aligned", "skew_sign": 0,
        "odds": (1.0 - cost) / cost, "ev_usdc": 0.0, "ev_per_usdc": 0.0,
        "selection_rule_label": "r", "window": ["30C"], "window_size": 1,
        "net_profit_probability": None, "consolidation": {},
        "lean_buckets": [], "ladder_lean_buckets": [],
        "centre_bucket": "30C", "settled_bucket": "30C",
        "settled_tmax_c": 30.0, "chain_shares": {}, "ask_total": cost,
    }


# The live case, reduced: a 35-cent chain whose last top-up was 0.3 of a
# cent, a genuinely sub-cent chain, and an ordinary cheap one.
LIVE_SHAPE = {
    "rows": [
        _row("VILK", "2026-08-25", "P0", 0.414, 0.3488, False, 5.811,
             -5.811, 0),
        _row("VILK", "2026-08-25", "P1", 0.003, 0.3488, True, 5.811,
             -5.811, 0),
        _row("ZGSZ", "2026-08-25", "P0", 0.003, 0.003, True, 0.045,
             -0.045, 0),
        _row("KMIA", "2026-08-25", "P0", 0.021, 0.021, True, 0.231,
             -0.231, 0),
    ],
    "station_roster": [],
}


def test_a_topped_up_chain_is_not_dust_because_its_last_leg_was():
    table = C._daily_table(LIVE_SHAPE, {})
    assert len(table) == 1
    day = table[0]
    # VILK and KMIA settle; only the genuinely sub-cent ZGSZ chain drops.
    assert day["settled_count"] == 2
    assert day["pnl_usdc"] == pytest.approx(-6.042)
    assert day["staked_usdc"] == pytest.approx(6.042)


def test_the_days_odds_come_from_the_chain_price_not_the_last_leg():
    day = C._daily_table(LIVE_SHAPE, {})[0]
    # Mean chain price over the two credible chains, inverted once.
    mean_cost = (0.3488 + 0.021) / 2
    assert day["odds_mean"] == pytest.approx((1 - mean_cost) / mean_cost)
    # Read off the tail stage instead, this day priced at 1.2 cents and
    # printed odds in the eighties.
    tail_mean = (0.003 + 0.021) / 2
    assert day["odds_mean"] < (1 - tail_mean) / tail_mean / 10


def test_basket_hit_table_gates_on_the_chain_price():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trade_settlement (settlement_id TEXT PRIMARY KEY,"
        " position_id TEXT, station_id TEXT, event_date TEXT, stage TEXT,"
        " hit INTEGER, stake_usdc REAL, pnl_usdc REAL,"
        " claimed_win_probability REAL, basket_all_in_cost REAL)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_position (position_id TEXT PRIMARY KEY,"
        " window_size INTEGER, all_in_cost REAL)"
    )
    # Topped-up chain: tail stage 0.3 cents, basket 35 cents.
    conn.execute("INSERT INTO paper_trade_position VALUES ('p1', 2, 0.003)")
    conn.execute(
        "INSERT INTO paper_trade_settlement VALUES"
        " ('s1','p1','VILK','2026-08-25','P1',0,5.811,-5.811,0.42,0.3488)"
    )
    # Genuinely sub-cent basket.
    conn.execute("INSERT INTO paper_trade_position VALUES ('p2', 2, 0.003)")
    conn.execute(
        "INSERT INTO paper_trade_settlement VALUES"
        " ('s2','p2','ZGSZ','2026-08-25','P0',0,0.045,-0.045,0.25,0.003)"
    )
    conn.commit()
    rows = SM._basket_hit_by_size(conn)
    assert sum(r["settled_count"] for r in rows) == 1
    assert sum(r["staked_usdc"] for r in rows) == pytest.approx(5.811)


def test_the_stage_flag_survives_for_the_readings_that_are_stage_grained():
    """Both flags exist, and neither is a rename of the other.

    The per-leg display marks an individual sub-cent offer 虚价, and the
    stage-grained means (EV per USDC, ask totals) still exclude it: a
    0.3-cent leg really does have an EV per USDC in the hundreds and would
    set every average by itself.  What must not use it is any total that
    counts a POSITION.
    """

    source = (ROOT / "scripts" / "selection_market_dashboard.py").read_text()
    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert '"chain_dust_offer": (' in source
    assert 'credible_rows = [r for r in rows if not r["dust_offer"]]' in source
    assert 'and not r["chain_dust_offer"]' in source
    assert 'row.get("chain_dust_offer", row.get("dust_offer"))' in page
