"""The shadow probe grows the arm charter 108 said was missing.

Every rule the probe had rests at the price it was written at.  Charter
94(六) does not: production cancels a rung that has fallen out of the
bid-anchored ladder and re-mints it at the current bid, every few minutes,
all day.  Replayed in the ledger that one difference is enormous -- charter
108's experiment took the winning leg's fill rate from 74.6% to 20.7% and the
winner/loser asymmetry from 0.76 to 0.23 by flipping the re-post cadence, and
0.23 is 2.7x outside anything the fixed-limit rules have ever measured on a
real book.

Whether a real chased order behaves like the ledger's model of one had never
been measured, because no rule here chased.  ``bid+1t@chase`` is that
measurement: same placement instant, same fill test, same settlement, and a
limit that re-derives from the current bid on production's own cadence.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "maker_shadow_probe", ROOT / "scripts" / "maker_shadow_probe.py"
)
probe = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(probe)


def _book(conn, observed, *, bid, ask, tick=0.01, bucket="30°C"):
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot"
        " (station_id, event_date, quote_observed_at, state, buckets_json)"
        " VALUES('ZSQD','2026-09-01',?,'COMMITTED',?)",
        (observed, json.dumps([{
            "bucket": bucket, "best_ask": ask, "bid_price": bid,
            "price_tick_size": tick,
            "ask_levels": [{"price": ask, "shares": 50.0}],
        }])),
    )


def _db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "probe.db")
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot("
        " station_id TEXT, event_date TEXT, quote_observed_at TEXT,"
        " state TEXT, buckets_json TEXT)"
    )
    probe.ensure_schema(conn)
    return conn


def _order(conn, rule, limit, *, placed, expires):
    conn.execute(
        "INSERT INTO maker_shadow_order("
        " order_id, station_id, event_date, bucket, price_rule,"
        " horizon_minutes, placed_at_utc, book_observed_at_utc,"
        " expires_at_utc, lead_hours, limit_price, reference_bid,"
        " reference_ask, reference_tick, queue_known, state, content_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"mso-{rule}", "ZSQD", "2026-09-01", "30°C", rule, 60,
            placed, placed, expires, 6.0, limit, 0.30, 0.40, 0.01, 0,
            "OPEN", f"sha-{rule}",
        ),
    )


def test_the_chasing_rule_prices_off_its_base_rule_at_placement():
    """It is written at the same instant, off the same book, at the same
    price as the rung it is the chasing twin of -- otherwise the pair is
    not a comparison."""

    quote = {"bid": 0.30, "ask": 0.40, "tick": 0.01}
    assert probe._limit_price("bid+1t@chase", quote) == probe._limit_price(
        "bid+1t", quote
    )
    assert "bid+1t@chase" in probe.PRICE_RULES
    assert "bid+1t" in probe.PRICE_RULES, "the fixed twin stays"


def test_a_dying_bucket_strands_the_chase_and_the_ask_sweeps_it(tmp_path):
    """The mechanism, once the fixture is honest about how a bucket dies.

    The chase does not walk down onto the ask -- it re-anchors one tick over
    the bid, which stays under the ask, so while both sides are quoted it is
    never reached.  What kills it is the bid VANISHING: with no bid there is
    no anchor, the re-post produces nothing, and the last limit it posted
    stays live far above a collapsing ask until the ask sweeps through it.
    That is the ledger's behaviour too -- ``_ladder_from_quote`` returns an
    empty ladder on a one-sided book and the caller keeps the standing rungs
    -- and it is why the ledger's chased order fills on the buckets that die
    while a fixed one at the same price may not.
    """

    conn = _db(tmp_path)
    placed, expires = "2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z"
    _order(conn, "bid+1t", 0.11, placed=placed, expires=expires)
    _order(conn, "bid+1t@chase", 0.31, placed=placed, expires=expires)
    # Both sides quoted and falling: the chase re-anchors one tick over the
    # bid, which stays under the ask, so it is not reached here.  It ends
    # this stretch anchored at 0.10 + one tick.
    _book(conn, "2026-09-01T00:06:00Z", bid=0.20, ask=0.25)
    _book(conn, "2026-09-01T00:12:00Z", bid=0.10, ask=0.14)
    # Then the bid goes.  With no anchor the re-post produces nothing and
    # the last limit stays live, far above a collapsing ask.
    _book(conn, "2026-09-01T00:20:00Z", bid=0.0, ask=0.09)
    _book(conn, "2026-09-01T00:26:00Z", bid=0.0, ask=0.05)
    conn.commit()
    probe.resolve(conn)
    rows = {
        str(rule): row for rule, *row in conn.execute(
            "SELECT price_rule, state, fill_observed_ask, fill_limit_price"
            " FROM maker_shadow_order"
        )
    }
    chased = rows["bid+1t@chase"]
    assert chased[0] == "FILLED"
    assert chased[1] == pytest.approx(0.09), "swept at the first ask under it"
    assert chased[2] == pytest.approx(0.11), (
        "the stranded limit, not the 0.31 it was written at"
    )
    # A fixed order written at that same stranded price fills at the same
    # tick -- the chase's loss here is not that it filled, it is that it
    # walked its limit down to 0.11 and then could not walk any further.
    assert rows["bid+1t"][0] == "FILLED"
    assert rows["bid+1t"][2] is None
    conn.close()


def test_a_rising_market_never_reaches_either_order(tmp_path):
    """The other half of the asymmetry: on a bucket the market is coming to
    believe, the ask stays above the bid, so neither the fixed order nor the
    chase is ever reached -- and the chase spends the day raising what it
    would have paid."""

    conn = _db(tmp_path)
    placed, expires = "2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z"
    _order(conn, "bid+1t", 0.31, placed=placed, expires=expires)
    _order(conn, "bid+1t@chase", 0.31, placed=placed, expires=expires)
    for minute, (bid, ask) in enumerate(
        [(0.40, 0.50), (0.55, 0.65), (0.70, 0.80)], start=1
    ):
        _book(conn, f"2026-09-01T00:{6*minute:02d}:00Z", bid=bid, ask=ask)
    conn.commit()
    probe.resolve(conn)
    states = dict(conn.execute(
        "SELECT price_rule, state FROM maker_shadow_order"
    ).fetchall())
    # Past its expiry by wall clock, so the sweep retires both -- what
    # matters is that neither filled.
    assert states["bid+1t"] != "FILLED"
    assert states["bid+1t@chase"] != "FILLED"
    conn.close()


def test_settlement_scores_a_chased_order_at_the_price_it_filled_at(tmp_path):
    """A chasing order that re-priced does not settle at the price it was
    written at.  Scoring it there would book a trade nobody made."""

    source = (ROOT / "scripts" / "maker_shadow_probe.py").read_text(
        encoding="utf-8"
    )
    assert "COALESCE(o.fill_limit_price, o.limit_price)" in source
    conn = _db(tmp_path)
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(maker_shadow_order)")
    }
    assert "fill_limit_price" in columns
    conn.close()


def test_the_reprice_cadence_is_the_one_production_re_mints_at():
    """Five minutes, which is charter 94(六)'s dwell.  A different cadence
    would measure a rule nobody runs."""

    assert probe.CHASE_REPRICE_MINUTES == 5.0
    from bayes_basket_target.resting_ladder_lifecycle import (
        RESTING_REPRICE_DWELL_SECONDS,
    )
    assert probe.CHASE_REPRICE_MINUTES * 60 == RESTING_REPRICE_DWELL_SECONDS
