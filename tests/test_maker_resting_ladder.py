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


@pytest.fixture(autouse=True)
def _band_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about the band arm (charter 63 era: a five-rung
    band between bid+1t and ask-1t with re-posts, escalation, the value
    clamp and the belief gate).  Charter 94 (2026-09-06) made the
    bid-anchored, single-shot ladder the family both lanes form and switches
    those arms off at import; put the arm back for the duration of a test."""

    import scripts.paper_trading_ledger as ledger
    from bayes_basket_target import delta_v2_runtime as runtime

    monkeypatch.setattr(runtime, "MAKER_LADDER_SHAPE", "band")
    monkeypatch.setattr(ledger, "MAKER_LADDER_SHAPE", "band")
    monkeypatch.setattr(ledger, "MAKER_RESTING_SINGLE_SHOT", False)
    for name, value in ledger.MAKER_RESTING_BAND_ARM_DEFAULTS.items():
        monkeypatch.setattr(ledger, name, value)


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

    The rung count is bounded by the band, not only by the step count: a
    0.24/0.30 book on a one-cent tick has five legal prices between its two
    sides and cannot carry eight distinct orders.  This used to assert
    eight, which passed only because the ladder was rounding to six
    decimals and posting 0.2586 -- a price the exchange does not accept.
    """

    rungs = _resting_ladder(_row(0.30, 0.24))
    legal = [0.25, 0.26, 0.27, 0.28, 0.29]
    assert rungs == legal
    assert len(rungs) <= MAKER_RESTING_LADDER_STEPS
    assert rungs == sorted(rungs)
    assert rungs[0] > 0.24
    assert rungs[-1] < 0.30


def test_every_rung_sits_on_the_exchange_tick_grid() -> None:
    """A price off the grid is not an order, so it must not be a fill.

    Before 2026-08-26 this lane's ladder was its own copy of production's
    arithmetic and rounded to six decimals where production floors to the
    tick.  It priced -- and filled -- rungs like 0.2864 on a tick-0.001
    book, so every ROI figure it produced was built on orders that could
    not have been placed.
    """

    for ask, bid, tick in (
        (0.30, 0.24, 0.01), (0.32, 0.28, 0.001), (0.90, 0.33, 0.005),
        (0.18, 0.12, 0.001), (0.55, 0.45, 0.01),
    ):
        for rung in _resting_ladder(_row(ask, bid, tick)):
            steps = rung / tick
            assert abs(steps - round(steps)) < 1e-6, (ask, bid, tick, rung)


def test_the_ledger_posts_productions_ladder_not_a_copy_of_it() -> None:
    """One band, one implementation, or they drift and nobody notices.

    Measured on 2026-08-26 across 64 (bid, ask, tick, cap) combinations,
    the two copies disagreed on 37.  This asserts the binding rather than
    the agreement: a future edit to production's ladder must reach this
    lane, and an edit that forks it must fail here.
    """

    from bayes_basket_target.delta_v2_runtime import _maker_ladder

    for tick in (0.001, 0.005, 0.01):
        for ask, bid in (
            (0.23, 0.20), (0.37, 0.36), (0.32, 0.28), (0.18, 0.12),
            (0.05, 0.02), (0.02, 0.001), (0.55, 0.45), (0.30, 0.05),
        ):
            for cap in (None, 0.0795, 0.212, 0.005, 0.0106):
                # Production's ladder no longer takes a cap (charter 63
                # retired, 2026-09-03); the ledger folds its research-arm
                # cap into the ceiling before the call.
                assert _resting_ladder(_row(ask, bid, tick), value_cap=cap) \
                    == _maker_ladder(
                        bid=bid,
                        ask=ask if cap is None else min(ask, cap),
                        tick=tick,
                    ), (tick, ask, bid, cap)


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
    buckets, shares, stake, win, leg_stake = entered
    assert buckets == ["31C", "33C"]
    assert shares == [100.0, 40.0]
    assert stake == pytest.approx(100.0 * 0.20 + 40.0 * 0.10)
    # The cash follows the legs that survived, and the legs still add up to
    # the stake the position is recorded at.
    assert leg_stake == pytest.approx([100.0 * 0.20, 40.0 * 0.10])
    assert sum(leg_stake) == pytest.approx(stake)
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


def test_late_phase_crosses_the_spread_inside_the_value_ceiling() -> None:
    """Completion beats the spread once the window is two thirds gone.

    The measured reason: legs that never filled realised 115% of their
    claimed probability against 36% for the ones that did, so an open rung
    late in the window is, in expectation, the expensive part of the day.
    A core leg still open past GAP_LATE takes the ask -- the remaining
    saving is exactly the sacrifice the redesign budgets for.
    """

    conn = _book((
        ("2026-08-17T01:00:00Z", "32C", 0.40),   # never touches the rung
        ("2026-08-17T17:00:00Z", "32C", 0.40),   # past 2/3 of the window
    ))
    got, missing, withdrew = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C",),
        ladders=([0.25],),
        placed_at_utc="2026-08-17T00:00:00Z",
        probabilities={"32C": 0.5},
        centre_bucket="32C",
    )
    assert withdrew is False
    # Crossed at the ask, not at the resting rung -- the price says taker.
    assert got["32C"] == [0.40]
    assert missing is None


def test_the_value_red_line_holds_at_any_completion_gap() -> None:
    """Gate 1 is the one thing execution pressure can never move.

    A hedge leg's ceiling is its clamp: lambda times its claim.  A market
    asking three times that number stays unbought to the very end of the
    window -- completion priority operates inside the value band, and a
    leg whose band the market never enters is incomplete by design.
    """

    conn = _book((
        ("2026-08-17T01:00:00Z", "32C", 0.25),   # core fills normally
        ("2026-08-17T01:00:00Z", "33C", 0.30),
        ("2026-08-17T17:00:00Z", "32C", 0.25),
        ("2026-08-17T17:00:00Z", "33C", 0.30),   # still triple the clamp
        ("2026-08-17T23:00:00Z", "33C", 0.30),
    ))
    got, missing, withdrew = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C", "33C"),
        ladders=([0.25], [0.05]),
        placed_at_utc="2026-08-17T00:00:00Z",
        # 33C claims 0.2 -> clamp 0.53 x 0.2 = 0.106, far under the 0.30 ask.
        probabilities={"32C": 0.5, "33C": 0.2},
        centre_bucket="32C",
    )
    assert withdrew is False
    assert got["32C"] == [0.25]
    assert got["33C"] == []
    assert missing == "33C"


def test_crossing_never_reaches_for_a_collapsed_ask() -> None:
    """A sub-two-cent ask is a tail offer, not a price -- even late.

    The first run of the completion redesign crossed "inside value" into
    0.001 asks on collapsed buckets and manufactured +260% ROI out of
    lottery tickets.  The selection layer's leg floor applies to every
    quote this book acts on: below it we neither seat nor reach.
    """

    conn = _book((
        ("2026-08-17T01:00:00Z", "32C", 0.40),
        ("2026-08-17T17:00:00Z", "32C", 0.001),   # collapse, late phase
    ))
    got, missing, withdrew = _resting_ladder_fill(
        conn,
        station_id="ZUUU",
        event_date="2026-08-17",
        buckets=("32C",),
        ladders=([0.25],),
        placed_at_utc="2026-08-17T00:00:00Z",
        probabilities={"32C": 0.5},
        centre_bucket="32C",
    )
    assert withdrew is False
    # The resting rung at 0.25 IS traded through by the collapse -- a
    # standing order fills where it stands -- but no crossing tops it up
    # at the phantom price.
    assert got["32C"] == [0.25]
