"""A resting leg posts across a band, not at a point.

The shadow probe measured the two ends of that band 14x apart on fill rate
-- joining the bid filled 5.8% of orders in five minutes, a tick under the
ask filled 82.8%.  A single price has to pick one of those and live with it;
a ladder takes both ends and pays the average of whatever the market
actually came down to.
"""

import sqlite3

import pytest

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
    got, missing, _withdrew = _resting_ladder_fill(
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


def test_a_leg_with_no_rung_filled_is_reported_as_such() -> None:
    """Reported, not refused: the basket is built up, not switched on."""

    conn = _book((
        ("2026-08-17T01:00:00Z", "32C", 0.25),
        ("2026-08-17T01:00:00Z", "33C", 0.40),
    ))
    got, missing, _withdrew = _resting_ladder_fill(
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
    got, _, _withdrew = _resting_ladder_fill(
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


def test_a_leg_that_filled_nothing_leaves_the_basket_it_was_chosen_for(
) -> None:
    """Progressive entry keeps what filled and drops what did not.

    The earlier rule sized the whole basket down to its least-filled leg to
    preserve the shape.  That is a bet, not a description of what resting
    orders do -- each rung is its own order, decided on its own.
    """

    from scripts.paper_trading_ledger import _progressive_entry

    entered = _progressive_entry(
        buckets=("31C", "32C", "33C"),
        leg_shares=(100.0, 100.0, 100.0),
        filled_fraction={"31C": 1.0, "32C": 0.0, "33C": 0.4},
        prices={"31C": 0.20, "33C": 0.10},
        probabilities={"31C": 0.30, "32C": 0.25, "33C": 0.15},
    )
    assert entered is not None
    buckets, shares, stake, win = entered
    assert buckets == ["31C", "33C"]
    assert shares == [100.0, 40.0]
    assert stake == pytest.approx(100.0 * 0.20 + 40.0 * 0.10)
    # The claim shrinks with the position: 32C was never bought, so its
    # probability is not part of what this basket can win.
    assert win == pytest.approx(0.45)


def test_nothing_filled_anywhere_is_still_the_one_refusal() -> None:
    from scripts.paper_trading_ledger import _progressive_entry

    assert _progressive_entry(
        buckets=("31C", "32C"),
        leg_shares=(100.0, 100.0),
        filled_fraction={"31C": 0.0, "32C": 0.0},
        prices={},
        probabilities={"31C": 0.3, "32C": 0.25},
    ) is None


def test_escalation_lifts_the_floor_and_the_ceiling_caps_the_chase() -> None:
    """Late in the window the band stands where the book trades.

    Halfway through escalation the floor has climbed halfway; at 1.0 every
    remaining rung sits a tick under the ask.  And a book that has trended
    past the entry ask is not followed: the cap holds the band at the price
    the entry gate was struck against.
    """

    from scripts.paper_trading_ledger import _ladder_from_quote

    quote = {"best_ask": 0.30, "bid_price": 0.24, "price_tick_size": 0.01}
    flat = _ladder_from_quote(quote, 3)
    assert flat[0] == 0.25 and flat[-1] == 0.29
    lifted = _ladder_from_quote(quote, 3, floor_fraction=0.5)
    assert lifted[0] == 0.27 and lifted[-1] == 0.29
    # Fully escalated, the whole remaining size stands a tick under the
    # ask: three rungs at one price are one order for the combined size.
    top = _ladder_from_quote(quote, 3, floor_fraction=1.0)
    assert top == [0.29, 0.29, 0.29]
    capped = _ladder_from_quote(quote, 3, floor_fraction=1.0, ceiling=0.27)
    assert capped == [0.26, 0.26, 0.26]


def test_a_bought_out_band_is_replenished_up_to_its_posted_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One rung bought out of a collapsed band is not a finished leg.

    Mid-day the spread pinches, the re-derived band collapses to a single
    rung, and that rung fills -- the pending list is empty while the leg
    still owes one rung of its posted depth.  The next re-pricing round
    must post that rung again rather than reading the empty list as
    completion; under the old rule this leg stalled at two fills of three.
    """

    import scripts.paper_trading_ledger as ledger

    monkeypatch.setattr(ledger, "MAKER_RESTING_VALUE_LAMBDA", 1.0)

    import json as _json

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot ("
        " station_id TEXT, event_date TEXT, state TEXT,"
        " observed_at TEXT, buckets_json TEXT)"
    )

    def snap(observed: str, ask: float, bid: float) -> None:
        conn.execute(
            "INSERT INTO basket_official_ws_snapshot VALUES (?,?,?,?,?)",
            ("ZUUU", "2026-08-17", "COMMITTED", observed, _json.dumps([
                {"bucket": "32C", "best_ask": ask, "bid_price": bid,
                 "price_tick_size": 0.01},
            ])),
        )

    snap("2026-08-17T00:30:00Z", 0.26, 0.25)  # dearest opening rung fills
    snap("2026-08-17T01:30:00Z", 0.27, 0.26)  # reprice: band pinches to one
    snap("2026-08-17T01:45:00Z", 0.26, 0.25)  # that one rung fills too
    snap("2026-08-17T02:30:00Z", 0.26, 0.22)  # reprice: one rung still owed
    snap("2026-08-17T02:50:00Z", 0.22, 0.20)  # and it fills

    got, missing, _withdrew = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C",),
        ladders=([0.24, 0.25, 0.26],),
        placed_at_utc="2026-08-17T00:00:00Z",
        probabilities={"32C": 0.27},
    )
    assert missing is None
    assert len(got["32C"]) == 3


def _bridge_book():
    """A book plus the probability bridge the belief timeline reads.

    Returns ``(conn, snap, believe)``: the connection cannot carry helper
    attributes, so the two writers ride beside it.
    """

    import json as _json

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot ("
        " station_id TEXT, event_date TEXT, state TEXT,"
        " observed_at TEXT, buckets_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history ("
        " history_id TEXT, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_bucket_probability ("
        " history_id TEXT, bucket TEXT, probability REAL)"
    )

    def snap(observed: str, quotes: dict[str, tuple[float, float]]) -> None:
        conn.execute(
            "INSERT INTO basket_official_ws_snapshot VALUES (?,?,?,?,?)",
            ("ZUUU", "2026-08-17", "COMMITTED", observed, _json.dumps([
                {"bucket": bucket, "best_ask": ask, "bid_price": bid,
                 "price_tick_size": 0.01}
                for bucket, (ask, bid) in quotes.items()
            ])),
        )

    def believe(decided: str, probabilities: dict[str, float]) -> None:
        hid = "h-" + decided
        conn.execute(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?)", (hid, "ZUUU", "2026-08-17", "P1", decided),
        )
        for bucket, p in probabilities.items():
            conn.execute(
                "INSERT INTO basket_probability_bridge_bucket_probability"
                " VALUES (?,?,?)", (hid, bucket, p),
            )

    return conn, snap, believe


def test_a_collapsed_belief_withdraws_the_rungs_above_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our own probability, not the market, pulls the cap down.

    The belief falls from 0.30 to 0.12 mid-window; at the next re-pricing
    round every rung above the new value is withdrawn, and the later ask
    collapse through those prices buys nothing.  The market alone never
    triggers anything: its own slide is, by our valuation, a discount --
    but only the rungs at or under the value may take it.
    """

    import scripts.paper_trading_ledger as ledger

    monkeypatch.setattr(ledger, "MAKER_RESTING_VALUE_LAMBDA", 1.0)

    conn, snap, believe = _bridge_book()
    snap("2026-08-17T00:30:00Z", {"32C": (0.30, 0.24)})
    believe("2026-08-17T00:45:00Z", {"32C": 0.12})
    snap("2026-08-17T01:30:00Z", {"32C": (0.30, 0.24)})  # reprice round
    snap("2026-08-17T02:00:00Z", {"32C": (0.20, 0.15)})  # slide begins
    got, missing, _withdrew = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C",),
        ladders=([0.25, 0.27, 0.29],),
        placed_at_utc="2026-08-17T00:00:00Z",
        probabilities={"32C": 0.30},
    )
    # 0.27 and 0.29 stood above the collapsed value (0.12) and were
    # withdrawn at the 01:00 round; the 02:00 slide to ask 0.20 would have
    # filled both had they stayed.  Nothing filled at all: the re-posted
    # band tops out a tick under the value, and the ask never came down
    # that far.
    assert got["32C"] == []
    assert missing == "32C"


def test_a_negative_basket_expectation_withdraws_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole-order gate: our claim can no longer pay for the order."""

    import scripts.paper_trading_ledger as ledger

    monkeypatch.setattr(ledger, "MAKER_RESTING_BELIEF_GATE", 1)

    conn, snap, believe = _bridge_book()
    snap("2026-08-17T00:30:00Z", {"32C": (0.30, 0.24), "33C": (0.30, 0.24)})
    # The claim collapses on both legs: expectation of the posted order
    # goes negative and every open rung is withdrawn for good.
    believe(
        "2026-08-17T00:45:00Z", {"32C": 0.05, "33C": 0.05}
    )
    snap("2026-08-17T01:30:00Z", {"32C": (0.30, 0.24), "33C": (0.30, 0.24)})
    # Both books later trade through the whole original band; a live order
    # would have filled everything.
    snap("2026-08-17T02:00:00Z", {"32C": (0.05, 0.03), "33C": (0.05, 0.03)})
    got, missing, _withdrew = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C", "33C"),
        ladders=([0.25, 0.27, 0.29], [0.25, 0.27, 0.29]),
        placed_at_utc="2026-08-17T00:00:00Z",
        probabilities={"32C": 0.30, "33C": 0.30},
    )
    assert got["32C"] == [] and got["33C"] == []
    assert missing is not None


def test_the_peak_cutoff_ends_the_order_before_settlement() -> None:
    """Past the station's peak-heat hours nothing fills, however cheap."""

    conn, snap, believe = _bridge_book()
    snap("2026-08-17T10:00:00Z", {"32C": (0.40, 0.30)})   # inside window
    snap("2026-08-17T15:00:00Z", {"32C": (0.20, 0.10)})   # past cutoff
    got, missing, _withdrew = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C",),
        ladders=([0.25, 0.27, 0.29],),
        placed_at_utc="2026-08-17T00:00:00Z",
        probabilities={"32C": 0.60},
        # 14:00 UTC wall clock on the event day (settlement tz is UTC).
        cutoff_local_minutes=14 * 60,
    )
    # The 15:00 collapse to ask 0.20 would have filled the whole ladder;
    # the order was already dead.
    assert got["32C"] == []
    assert missing == "32C"


def test_value_headroom_escalates_without_waiting_for_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leg worth more than the band top posts at the top immediately."""

    import scripts.paper_trading_ledger as ledger

    monkeypatch.setattr(ledger, "MAKER_RESTING_VALUE_LAMBDA", 1.0)

    conn, snap, believe = _bridge_book()
    # First reprice round: value 0.60 far above the band; the re-post
    # should stand a tick under the ask rather than fish the bottom.
    snap("2026-08-17T01:30:00Z", {"32C": (0.30, 0.20)})
    # One tick down fills the entire remainder in one stroke -- before the
    # next re-pricing round, which would move the band under the new ask.
    snap("2026-08-17T01:45:00Z", {"32C": (0.29, 0.20)})
    got, missing, _withdrew = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C",),
        ladders=([0.21, 0.25, 0.29],),
        placed_at_utc="2026-08-17T00:00:00Z",
        probabilities={"32C": 0.60},
    )
    assert missing is None
    assert got["32C"] == [0.29, 0.29, 0.29]


def test_the_centre_bucket_is_exempt_from_the_value_clamp() -> None:
    """The basket's base holding stands on the book, whatever its claim.

    A shoulder leg with the same low claim is clamped to nothing above
    its value; the centre leg posts the full book band regardless -- a
    basket without its centre is a shape the charter does not allow.
    """

    from scripts.paper_trading_ledger import _leg_value_cap

    probabilities = {"32C": 0.10, "33C": 0.10}
    shoulder = _leg_value_cap(
        "32C", probabilities=probabilities, centre_bucket="33C"
    )
    centre = _leg_value_cap(
        "33C", probabilities=probabilities, centre_bucket="33C"
    )
    assert shoulder is not None and shoulder <= 0.10
    assert centre is None
