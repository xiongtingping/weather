"""A resting leg posts across a band, not at a point.

The shadow probe measured the two ends of that band 14x apart on fill rate
-- joining the bid filled 5.8% of orders in five minutes, a tick under the
ask filled 82.8%.  A single price has to pick one of those and live with it;
a ladder takes both ends and pays the average of whatever the market
actually came down to.
"""

import sqlite3

from scripts.paper_trading_ledger import (
    MAKER_RESTING_LADDER_STEPS,
    _resting_ladder,
    _resting_ladder_fill,
)


def _row(ask: float, bid: float, tick: float = 0.01) -> object:
    return type(
        "Row",
        (),
        {
            "ask_available": True,
            "ask_price": ask,
            "bid_price": bid,
            "price_tick_size": tick,
        },
    )()


def test_the_band_spans_the_spread_without_ever_crossing_it() -> None:
    """Cheapest rung above the bid, dearest a tick under the ask.

    At the ask the order is not resting, it is crossing -- and paying the
    spread is the one thing this mode exists to avoid.
    """

    rungs = _resting_ladder(_row(0.30, 0.24))
    assert len(rungs) == MAKER_RESTING_LADDER_STEPS
    assert rungs == sorted(rungs)
    assert rungs[0] > 0.24
    assert rungs[-1] < 0.30


def test_a_one_tick_spread_collapses_the_band_onto_the_bid() -> None:
    """There is nowhere to stand between two adjacent prices.

    The ladder degenerates rather than silently posting at the ask, which
    is what a naive ``min(bid + tick, ask)`` does -- a crossing trade
    wearing a maker order's name.
    """

    assert _resting_ladder(_row(0.06, 0.05)) == [0.05]


def _book(rows: tuple[tuple[str, str, float], ...]) -> sqlite3.Connection:
    """(observed_at, bucket, best_ask) -> a committed snapshot table."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot ("
        " station_id TEXT, event_date TEXT, state TEXT,"
        " observed_at TEXT, buckets_json TEXT)"
    )
    by_time: dict[str, list[dict[str, object]]] = {}
    for observed, bucket, ask in rows:
        by_time.setdefault(observed, []).append(
            {"bucket": bucket, "best_ask": ask}
        )
    import json

    for observed, quotes in by_time.items():
        conn.execute(
            "INSERT INTO basket_official_ws_snapshot VALUES (?,?,?,?,?)",
            ("ZUUU", "2026-08-17", "COMMITTED", observed, json.dumps(quotes)),
        )
    return conn


def test_only_the_rungs_the_book_traded_through_are_filled() -> None:
    """A ladder is a set of independent orders, decided one at a time."""

    conn = _book((
        ("2026-08-17T01:00:00Z", "32C", 0.29),   # dearest rung only
        ("2026-08-17T02:00:00Z", "32C", 0.25),   # all three
        ("2026-08-17T01:00:00Z", "33C", 0.29),
    ))
    got, missing = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C", "33C"),
        ladders=([0.25, 0.27, 0.29], [0.25, 0.27, 0.29]),
        placed_at_utc="2026-08-17T00:00:00Z",
    )
    assert sorted(got["32C"]) == [0.25, 0.27, 0.29]
    assert got["33C"] == [0.29]
    assert missing is None


def test_a_leg_with_no_rung_filled_is_still_a_missing_leg() -> None:
    """The basket stays atomic: a ladder changes how much, not whether.

    Partial fills are now the normal case, so the refusal has to key on a
    leg that got nothing at all rather than on a leg that got less than it
    asked for -- otherwise the ladder's whole benefit is refused away.
    """

    conn = _book((
        ("2026-08-17T01:00:00Z", "32C", 0.25),
        ("2026-08-17T01:00:00Z", "33C", 0.40),
    ))
    got, missing = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C", "33C"),
        ladders=([0.25, 0.27, 0.29], [0.25, 0.27, 0.29]),
        placed_at_utc="2026-08-17T00:00:00Z",
    )
    assert got["32C"]
    assert missing == "33C"


def test_the_ladder_average_beats_the_dearest_rung_it_needed() -> None:
    """The reason to post a band at all, stated as an assertion.

    A point order at the price that filled would have paid that price for
    the whole clip; the ladder pays it for one third.
    """

    conn = _book((("2026-08-17T01:00:00Z", "32C", 0.25),))
    got, _ = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C",),
        ladders=([0.25, 0.27, 0.29],),
        placed_at_utc="2026-08-17T00:00:00Z",
    )
    average = sum(got["32C"]) / len(got["32C"])
    assert average == 0.27
    assert average < 0.29
