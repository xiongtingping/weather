"""The nine numbers a bet is made of, and the three ways they are averaged.

The section exists because each of these was already on the board without
the number that gives it a meaning beside it: a 41.4% hit rate is a rout
against a 41.6-cent basket and a licence to print against a 30-cent one.
Putting them together makes two things breakable, so both are pinned here.

**The pair must measure the same set of buckets.**  ``claimed_win_
probability`` is the served probability summed over the buckets the chain
ended up HOLDING, so it divides by ``hit`` and by nothing else.  The
ledger also carries ``intended_hit`` -- the truth landing in what the
objective asked for, including legs the market never sold -- and it is the
larger number (60.7% against 39.8% measured on 2026-09-08).  Swapping it
in would lift the realisation ratio to about 1.0 and read as a model that
is exactly calibrated, when what changed was the denominator's set.

**The share column has to close.**  Weighting each chain by the basket
shares it bought makes cost-per-share equal total money over total shares
and realised-EV-per-share equal total P&L over total shares, so their
ratio IS the book's return.  A rounding shortcut or a dropped row shows up
here as a column that no longer reconciles with the equity curve.

Plus the convexity trap the section warns about in prose: the plain
per-chain mean of (1-c)/c is dominated by a handful of few-cent baskets
(14.7x against a book whose money bought 1.53x), so the tile must print
the odds implied by the average cost, never that mean.
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

DDL = (
    "CREATE TABLE paper_trade_settlement ("
    " settlement_id TEXT PRIMARY KEY, position_id TEXT NOT NULL,"
    " station_id TEXT NOT NULL, event_date TEXT NOT NULL, stage TEXT NOT NULL,"
    " settled_tmax_c REAL NOT NULL, settled_bucket TEXT NOT NULL,"
    " hit INTEGER NOT NULL, stake_usdc REAL NOT NULL,"
    " payout_usdc REAL NOT NULL, pnl_usdc REAL NOT NULL,"
    " claimed_win_probability REAL NOT NULL, center_error_c REAL,"
    " bucket_distance INTEGER, settled_at_utc TEXT NOT NULL,"
    " truth_revision_no INTEGER, truth_content_sha256 TEXT NOT NULL DEFAULT '',"
    " basket_all_in_cost REAL, intended_hit INTEGER)"
)


def _book(rows: list[tuple]) -> sqlite3.Connection:
    """A book of (stage, claim, hit, intended_hit, cost, shares).

    Stake is derived from cost and shares rather than given, so every row
    is internally consistent: a hit pays one per share, a miss pays zero.
    """

    conn = sqlite3.connect(":memory:")
    conn.execute(DDL)
    for i, (stage, claim, hit, intended, cost, shares) in enumerate(rows):
        stake = cost * shares
        payout = shares if hit else 0.0
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"s{i}", f"p{i}", f"ST{i}", f"2026-09-{i + 1:02d}", stage,
             30.0, "30°C", int(hit), stake, payout, payout - stake,
             claim, 0.0, 0, "2026-09-10T00:00:00Z", 1, "", cost,
             int(intended)),
        )
    conn.commit()
    return conn


def test_claim_pairs_with_the_held_basket_not_the_chosen_one():
    # Every chain claims 50% and holds a basket that hits half the time,
    # while the objective's chosen basket would have hit every time.  The
    # realisation ratio is 1.0 only if the denominator is the held set.
    conn = _book([
        ("P0", 0.5, 1, 1, 0.4, 10.0),
        ("P0", 0.5, 0, 1, 0.4, 10.0),
        ("P0", 0.5, 1, 1, 0.4, 10.0),
        ("P0", 0.5, 0, 1, 0.4, 10.0),
    ])
    got = C.collect_entry_economics(conn)
    chain = got["weightings"]["chain"]
    assert chain["claimed_coverage"] == pytest.approx(0.5)
    assert chain["realised_coverage"] == pytest.approx(0.5)
    assert chain["realisation_ratio"] == pytest.approx(1.0)
    # The chosen-basket number is published, and it is a different set.
    assert got["intended_coverage"] == pytest.approx(1.0)
    assert got["intended_coverage"] != chain["realised_coverage"]


def test_share_column_reconciles_with_the_book_return():
    # Uneven sizes on purpose: a big cheap winner and a small dear loser
    # pull the three weightings apart, which is the case the column has to
    # survive.
    conn = _book([
        ("P0", 0.30, 1, 1, 0.20, 100.0),
        ("P0", 0.80, 0, 0, 0.75, 4.0),
        ("P1", 0.55, 0, 1, 0.50, 20.0),
        ("P1", 0.60, 1, 1, 0.55, 8.0),
    ])
    got = C.collect_entry_economics(conn)
    share = got["weightings"]["share"]
    assert share["cost_per_share"] == pytest.approx(
        got["stake_usdc"] / got["shares"]
    )
    assert share["ev_per_share_realised"] == pytest.approx(
        got["pnl_usdc"] / got["shares"]
    )
    # The closure the section leads with: EV per share over cost per share
    # is the realised return on the money, to the last place.
    assert (
        share["ev_per_share_realised"] / share["cost_per_share"]
        == pytest.approx(got["roi"])
    )
    # And the three weightings really do disagree here, so the test is not
    # passing on a book where they collapse to one number.
    claims = {
        round(got["weightings"][key]["claimed_coverage"], 6)
        for key in ("chain", "stake", "share")
    }
    assert len(claims) == 3


def test_break_even_rate_is_the_cost_under_every_weighting():
    conn = _book([
        ("P0", 0.45, 1, 1, 0.35, 12.0),
        ("P0", 0.45, 0, 0, 0.62, 3.0),
        ("P1", 0.70, 0, 1, 0.51, 40.0),
    ])
    got = C.collect_entry_economics(conn)
    for key in ("chain", "stake", "share"):
        block = got["weightings"][key]
        assert block["breakeven_hit_rate"] == pytest.approx(
            block["cost_per_share"]
        )
        assert block["net_odds"] == pytest.approx(
            (1.0 - block["cost_per_share"]) / block["cost_per_share"]
        )
        assert block["margin_over_breakeven"] == pytest.approx(
            block["realised_coverage"] - block["breakeven_hit_rate"]
        )


def test_published_odds_are_not_the_mean_of_per_chain_odds():
    # One three-cent basket among four.  Its own odds are 32x and the
    # plain mean of the four is over eight, while the money in the book
    # bought about 1.2x.  The tile must print the second kind.
    conn = _book([
        ("P0", 0.05, 0, 0, 0.03, 10.0),
        ("P0", 0.60, 1, 1, 0.55, 10.0),
        ("P0", 0.60, 0, 0, 0.55, 10.0),
        ("P0", 0.60, 1, 1, 0.55, 10.0),
    ])
    got = C.collect_entry_economics(conn)
    chain = got["weightings"]["chain"]
    assert got["net_odds_chain_naive"] > 8.0
    assert chain["net_odds"] < 2.0
    assert got["net_odds_max"] == pytest.approx((1 - 0.03) / 0.03)
    assert got["cheap_chains"] == 1


def test_hit_and_profit_are_counted_apart():
    # A basket that cost more than the 1.00 it can win.  It hits and still
    # loses, which is the day the two rates come apart -- the section
    # prints both precisely so that day is visible.
    conn = _book([
        ("P0", 0.99, 1, 1, 1.10, 5.0),
        ("P0", 0.40, 1, 1, 0.30, 5.0),
        ("P0", 0.40, 0, 0, 0.30, 5.0),
    ])
    got = C.collect_entry_economics(conn)
    chain = got["weightings"]["chain"]
    assert chain["realised_coverage"] == pytest.approx(2 / 3)
    assert chain["profitable_rate"] == pytest.approx(1 / 3)
    assert got["hit_not_profitable"] == 1
    assert got["profitable_not_hit"] == 0


def test_rows_without_a_basket_cost_are_counted_not_dropped_silently():
    conn = _book([("P0", 0.5, 1, 1, 0.4, 10.0)])
    conn.execute(
        "INSERT INTO paper_trade_settlement VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sx", "px", "STX", "2026-09-20", "P0", 30.0, "30°C", 1,
         4.0, 10.0, 6.0, 0.5, 0.0, 0, "2026-09-10T00:00:00Z", 1, "",
         None, 1),
    )
    conn.commit()
    got = C.collect_entry_economics(conn)
    assert got["settled_rows"] == 2
    assert got["chains"] == 1
    assert got["cost_missing"] == 1


def test_empty_book_says_so_instead_of_publishing_zeroes():
    # The ledger is rebuilt in place -- cleared, then re-walked for about
    # forty minutes -- and a render landing inside that window used to
    # publish a page of zeroes as if measured (2026-09-08).
    conn = sqlite3.connect(":memory:")
    conn.execute(DDL)
    got = C.collect_entry_economics(conn)
    assert got["available"] is False
    assert got["chains"] == 0
    assert "重建" in got["reason"]


def test_stake_weighted_column_agrees_with_the_portfolio_tiles():
    """One quantity, one value, however many sections print it.

    ``collect_risk_metrics`` already publishes the stake-weighted cost,
    odds, break-even rate and realised win rate.  This section computes
    them again from the same table, so the two are pinned together: a
    change to either definition has to move both or fail here.
    """

    conn = _book([
        ("P0", 0.30, 1, 1, 0.20, 100.0),
        ("P0", 0.80, 0, 0, 0.75, 4.0),
        ("P1", 0.55, 0, 1, 0.50, 20.0),
        ("P1", 0.60, 1, 1, 0.55, 8.0),
        ("P0", 0.44, 1, 1, 0.41, 15.0),
    ])
    entry = C.collect_entry_economics(conn)["weightings"]["stake"]
    risk = C.collect_risk_metrics(conn)
    assert risk["available"] is True
    assert entry["cost_per_share"] == pytest.approx(risk["cost_mean"])
    assert entry["breakeven_hit_rate"] == pytest.approx(
        risk["breakeven_win_rate"]
    )
    assert entry["realised_coverage"] == pytest.approx(
        risk["realised_win_rate_weighted"]
    )
    # The odds are the one pair that is allowed to differ, and the reason
    # is the convexity above: the portfolio tile averages each chain's own
    # (1-c)/c weighted by stake, this section inverts the average cost.
    # Pinned as an inequality so nobody "reconciles" them by accident.
    assert risk["odds_mean"] != pytest.approx(entry["net_odds"])


def test_section_is_rendered_and_reachable():
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert 'sec(entryEconomics, "入场的九个数", "sec-entry")' in source
    assert '"entry_economics": _cached_section(' in source
