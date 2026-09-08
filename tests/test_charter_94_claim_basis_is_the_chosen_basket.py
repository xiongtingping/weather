"""Charter 94 split the basket in two, and the claims layer must read the
half it was asked about.

Until charter 65 the execution family was a taker FOK: a basket filled whole
or was refused whole, so "the basket we chose" and "the basket we hold" were
one object and every consumer could read either column.  Charter 94 made the
order a resting ladder on the bid.  Now a seat posts three legs and keeps
two, and the two it keeps are not a random two -- measured on the 97 settled
seats of 2026-09-07, the legs that went on to WIN filled 18% of the time and
the legs that went on to LOSE filled 88%, because a resting bid is lifted
exactly when the market has decided that bucket is not happening.

So the post-execution columns (``win_probability``, ``all_in_cost``, ``hit``)
became a measurement of maker adverse selection.  Anything fitted for a
decision that has not executed yet has to read the chosen basket instead --
lambda(a) above all, because it is consumed by a gate that has authorised
nothing at the moment it asks.  Read the wrong pair and the 2026-09-06T23:02Z
P0 fit is what comes out: forty seats with zero winning fills swept into the
a<=0.50 band, lambda clamped to its 0.05 floor, and every other band stripped
of four fifths of its evidence.

These tests pin the split at all three places it can be undone: the score
that writes it, the migration that backfills it, and the queries that read
it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import paper_trading_ledger as ledger  # noqa: E402
from bayes_basket_target import effective_edge_store as store  # noqa: E402

# (bucket, ordinal, lower_c, upper_c, probability)
LADDER = [
    ("30°C", 0, 29.5, 30.5, 0.20),
    ("31°C", 1, 30.5, 31.5, 0.45),
    ("32°C", 2, 31.5, 32.5, 0.30),
]


def test_the_winning_leg_that_never_filled_is_a_miss_and_a_chosen_hit():
    """The one shape charter 94 introduced, scored by the production rule.

    Posted three legs, kept the two that lost, and the truth landed on the
    one the market would not sell.  ``hit`` must be 0 -- nothing paid, and
    that column is the money answer.  ``intended_hit`` must be 1 -- the
    claim named the right bucket, and that is the calibration answer.  A
    single column cannot be both, which is why there are two.
    """

    scored = ledger._score_settlement(
        ladder=LADDER,
        window=["30°C", "32°C"],          # what filled
        stake=2.0,
        shares=4.0,
        per_leg={"30°C": 2.0, "32°C": 2.0},
        center_c=31.0,
        truth_c=31.0,                      # landed on the unfilled leg
        intended_window=["30°C", "31°C", "32°C"],
    )
    assert scored is not None
    assert scored.settled_bucket == "31°C"
    assert scored.hit == 0
    assert scored.intended_hit == 1
    assert scored.pnl_usdc == pytest.approx(-2.0)


def test_a_seat_with_no_recorded_intent_scores_none_not_a_borrowed_window():
    """Absent evidence is not evidence of a whole fill.

    Defaulting ``intended_window`` to the filled window would make every
    pre-charter-94 row look like a basket that filled perfectly, which is
    the reading that has to be excluded and counted rather than assumed.
    """

    scored = ledger._score_settlement(
        ladder=LADDER,
        window=["30°C"],
        stake=1.0,
        shares=1.0,
        per_leg={"30°C": 1.0},
        center_c=31.0,
        truth_c=31.0,
    )
    assert scored is not None
    assert scored.hit == 0
    assert scored.intended_hit is None


def _book(path: Path) -> sqlite3.Connection:
    """A ledger schema built by the ledger, with two hand-written seats."""

    conn = sqlite3.connect(path)
    ledger.ensure_schema(conn)
    probabilities = json.dumps({b: p for b, _o, _l, _u, p in LADDER})
    prices = json.dumps({"30°C": 0.18, "31°C": 0.40, "32°C": 0.28})
    rows = [
        # Posted three, kept the two losers; truth is the middle leg.
        (
            "chosen", "2026-09-06", ["30°C", "32°C"],
            {"30°C": 2.0, "31°C": 2.0, "32°C": 2.0},
        ),
        # Never recorded what it posted: no basis, and it must say so.
        ("legacy", "2026-09-05", ["30°C", "32°C"], None),
    ]
    for position_id, event_date, kept, intended in rows:
        conn.execute(
            "INSERT INTO paper_trade_position("
            " position_id, station_id, event_date, stage, decision_at_utc,"
            " quote_observed_at_utc, ladder_center_bucket,"
            " window_buckets_json, window_size, win_probability,"
            " all_in_cost, edge, kelly_fraction, stake_usdc, shares,"
            " probabilities_json, prices_json, refused_window_count,"
            " content_sha256, intended_legs_json, chain_shares_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                position_id, "ZSQD", event_date, "P0",
                "2026-09-05T00:00:00Z", "2026-09-05T00:00:00Z", "31°C",
                json.dumps(kept), len(kept), 0.50, 0.46, 0.04, 0.02, 2.0,
                4.0, probabilities, prices, 0, position_id,
                json.dumps(intended) if intended else "",
                json.dumps({b: 2.0 for b in kept}),
            ),
        )
        conn.execute(
            "INSERT INTO paper_trade_settlement("
            " settlement_id, position_id, station_id, event_date, stage,"
            " settled_tmax_c, settled_bucket, hit, stake_usdc, payout_usdc,"
            " pnl_usdc, claimed_win_probability, settled_at_utc"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "s-" + position_id, position_id, "ZSQD", event_date, "P0",
                31.0, "31°C", 0, 2.0, 0.0, -2.0, 0.50,
                "2026-09-06T20:00:00Z",
            ),
        )
    conn.commit()
    return conn


def test_the_backfill_sums_the_recorded_ladders_over_the_posted_legs(tmp_path):
    """The chosen claim and price are the served vector and the ask ladder
    summed over the legs the seat posted -- the same arithmetic the entry
    walk did to build the candidate, over the same two recorded dicts.
    """

    conn = _book(tmp_path / "book.db")
    try:
        ledger._backfill_intended_basis(conn)
        claim, price, chosen_hit = conn.execute(
            "SELECT p.intended_win_probability, p.intended_all_in_cost,"
            " s.intended_hit FROM paper_trade_position p"
            " JOIN paper_trade_settlement s USING(position_id)"
            " WHERE p.position_id='chosen'"
        ).fetchone()
        assert claim == pytest.approx(0.20 + 0.45 + 0.30)
        assert price == pytest.approx(0.18 + 0.40 + 0.28)
        # The truth is the leg that was posted and not kept.
        assert chosen_hit == 1
        # ...and the money column is untouched by any of this.
        assert conn.execute(
            "SELECT hit FROM paper_trade_settlement"
            " WHERE position_id='chosen'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_a_row_without_recorded_intent_stays_null_rather_than_guessed(
    tmp_path,
):
    """No basis is not zero basis.

    A seat that cannot say what it posted gets NULL, not a value borrowed
    off the legs that happened to fill -- which would read as a basket that
    filled perfectly, the single most flattering wrong answer available
    here.
    """

    conn = _book(tmp_path / "book.db")
    try:
        ledger._backfill_intended_basis(conn)
        claim, cost = conn.execute(
            "SELECT intended_win_probability, intended_all_in_cost"
            " FROM paper_trade_position WHERE position_id='legacy'"
        ).fetchone()
        assert claim is None and cost is None
        assert conn.execute(
            "SELECT intended_hit FROM paper_trade_settlement"
            " WHERE position_id='legacy'"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_no_claims_layer_query_reads_the_post_execution_columns():
    """The regression guard.

    Both readings are one word apart and both compile, so the only thing
    standing between this fix and its own undoing is a test that says which
    word.  Scoped to the SQL these three functions issue: the ledger is
    free to read the filled columns anywhere else, because settlement,
    P&L and the equity curve are all questions about what was held.
    """

    sources = {
        "effective_edge_store": (
            ROOT / "bayes_basket_target" / "effective_edge_store.py"
        ).read_text(encoding="utf-8"),
        "calibration_dashboard": (
            SCRIPTS / "calibration_dashboard.py"
        ).read_text(encoding="utf-8"),
    }
    for name, text in sources.items():
        assert "p.win_probability" not in text, name
        assert "p.all_in_cost" not in text, name
        assert "s.hit," not in text, name
    # The claims layer moved on again a day later (charter 87, second
    # reading): its population is now every enumerated window, and the
    # ledger columns above are what each seat recorded about its own
    # choice.  Both readings are still one word from the filled basket.
    edge = sources["effective_edge_store"]
    # The replay lane (2026-09-08) shares the prefix, so it is counted out
    # rather than counted in: six reads of the served table, one of its
    # replay sibling, and that one is in the fit -- never in the guard.
    assert edge.count("FROM paper_trade_candidate_window_replay") == 1
    assert edge.count("FROM paper_trade_candidate_window") - 1 == 6, (
        "band-partition lookup, served fit, the fit's excluded-axis tally,"
        " its excluded-claim-vintage tally (charter 110 extended,"
        " 2026-09-08), activation guard, shadow fit"
    )
    # The thermometer's unit is the posted leg, with the fill an
    # observation about it -- not a precondition for counting it.
    dashboard = sources["calibration_dashboard"]
    assert "intended_legs_json" in dashboard


def test_the_gates_own_numbers_come_from_the_lane_that_gated():
    """Third instance of one disease, found by chasing the other two.

    The ledger runs its own enumeration and pricing and then, since
    2026-09-03, hands the seat to production's judge.  When that judge
    decides, this lane's claim and price describe a decision nobody made:
    it prices the same window off its own snapshot and without the charter
    81 lift the gate divides by.

    EFHK 2026-08-16 is the whole argument.  This lane recorded claim 0.6982
    against price 0.7000 -- a negative edge, which the D-rule audit reads as
    "the entry gate cannot have run".  Production's own row for the same
    three buckets, now recorded beside it, reads a LIFTED claim of 0.7540
    against a price of 0.7500: agreement 0.9946, admitted, correctly.
    Neither number was wrong.  The pairing was.
    """

    source = (SCRIPTS / "paper_trading_ledger.py").read_text(encoding="utf-8")
    site = source[source.index("intended_win_probability = float("):][:2400]
    # The judged row is preferred, matched by its own bucket tuple.
    assert 'production_verdict.get("enumerated_windows")' in site
    assert 'intended_win_probability = float(' in site
    assert '_row["win_probability"]' in site
    assert '_row["all_in_price"]' in site
    # And it is matched, not taken positionally: the judge's shortlist is
    # ordered by its own ranking, not by anything this lane knows.
    assert 'tuple(\n                                str(b) for b in (_row.get("buckets") or ())' in site
