"""Charter 94 (一)(二): a leg posts one order per rung it can afford.

Ruled 2026-09-06 and generated in the optimizer from that day, but never
posted: every structure below the selection was keyed one-per-bucket, so of
the rungs offered only one reached the exchange.  Measured then: 1668
(cycle, bucket) pairs in the whole ledger, every one a single order.
"""
from __future__ import annotations

import json

import sqlite3

from bayes_basket_target.production import _ladder_rung_prices
from bayes_basket_target.execution_reconciliation import can_submit_next_order
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema


def _quote(bid: float, ask: float, tick: float = 0.001) -> dict:
    return {
        "bid_price": bid,
        "ask_levels": [{"price": ask, "shares": 500.0}],
        "price_tick_size": tick,
        "min_order_size": 0.0,
    }


def test_a_gap_that_used_to_split_now_goes_whole_onto_the_anchor() -> None:
    """EGLC 20C on the 2026-09-08 book: 8 shares, bid 0.310, ask 0.330.

    A lot at the cheapest rung is 1.00/0.309 = 3.24 shares, so charter 94 (二)
    split this gap across two rungs.  Charter 110 leaves one rung, so the
    whole gap rests on it: the split was routing the second half into the
    population an ask has to fall to the bid to reach, measured at -0.0234
    a share against -0.0067 for the rung inside the bid.
    """
    rungs = _ladder_rung_prices(
        execution_candidate="MAKER", quote=_quote(0.310, 0.330),
        q_order=8.0, r_limit=0.311, share_quantum=0.01,
    )
    assert rungs == [0.311], rungs
    # The plumbing this module was written for is untouched: the count comes
    # from the ladder, not from one-order-per-bucket, and it still clears
    # the venue dollar.
    assert 8.0 * rungs[0] >= 1.0


def test_gap_holding_one_lot_posts_the_single_order_unchanged() -> None:
    """EGLC 18C: 25 shares at 0.041.  A lot at the cheapest rung (0.039) is
    25.6 shares, so the gap holds one -- and the one it posts must be the
    order this replaced, at the same price, or a leg too small to split would
    change behaviour for no reason."""
    rungs = _ladder_rung_prices(
        execution_candidate="MAKER", quote=_quote(0.040, 0.060),
        q_order=25.0, r_limit=0.041, share_quantum=0.01,
    )
    assert rungs == [0.041]


def test_split_that_would_break_the_dollar_floor_falls_back_to_one() -> None:
    """The count is read at the cheapest rung, so only the share grid can
    push a rung under the dollar.  When it does, post one."""
    rungs = _ladder_rung_prices(
        execution_candidate="MAKER", quote=_quote(0.500, 0.520),
        q_order=4.0, r_limit=0.501, share_quantum=1.0,
    )
    assert len(rungs) == 1


def test_taker_has_no_ladder() -> None:
    assert _ladder_rung_prices(
        execution_candidate="TAKER_FOK", quote=_quote(0.310, 0.330),
        q_order=8.0, r_limit=0.311, share_quantum=0.01,
    ) == [0.311]


def test_unanchorable_book_falls_back_rather_than_inventing_a_price() -> None:
    for quote in (_quote(0.0, 0.330), _quote(0.310, 0.0), _quote(0.310, 0.330, 0.0)):
        assert _ladder_rung_prices(
            execution_candidate="MAKER", quote=quote,
            q_order=8.0, r_limit=0.311, share_quantum=0.01,
        ) == [0.311]


def _seed(conn: sqlite3.Connection, *, intent_id: str, cycle_id: str, state: str) -> None:
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,cycle_id,bucket,"
        "token_id,side,order_action,limit_price,requested_size,requested_notional_usdc,state,"
        "live_submit_authorized,idempotency_key,intent_json,intent_sha256,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (intent_id, "d", cycle_id, "20C", "tok", "YES", "BUY", 0.311, 4.0, 1.24,
         state, 1, "idem_" + intent_id, "{}", "a" * 64, "2026-09-06T15:00:00Z"),
    )


def test_same_cycle_rungs_do_not_block_each_other(tmp_path) -> None:
    """The gate stops a later compile posting into a bucket that already
    holds an order.  Under one order per leg those were the same thing; with
    a ladder they are not, and read as duplicates the rungs of one leg would
    block each other -- the first posts, and the second sees it submitted."""
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    _seed(conn, intent_id="rung_a", cycle_id="cycle_1", state="submitted")

    same = can_submit_next_order(
        conn, bucket="20C", token_id="tok",
        exclude_order_intent_id="rung_b", same_cycle_id="cycle_1",
    )
    assert same["allowed"] is True, same

    other = can_submit_next_order(
        conn, bucket="20C", token_id="tok",
        exclude_order_intent_id="rung_b", same_cycle_id="cycle_2",
    )
    assert other["allowed"] is False, other

    unscoped = can_submit_next_order(
        conn, bucket="20C", token_id="tok", exclude_order_intent_id="rung_b",
    )
    assert unscoped["allowed"] is False, "no cycle named still blocks"


def test_an_ambiguous_sibling_blocks_even_inside_one_ladder(tmp_path) -> None:
    """The exemption is for rungs that say plainly whether they reached the
    exchange.  A reconciliation that is pending or unknown says nobody knows
    whether money moved, and posting another rung against that is the double
    the gate exists to prevent -- sibling or not."""
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    _seed(conn, intent_id="rung_a", cycle_id="cycle_1", state="submitted")
    conn.execute(
        "INSERT INTO basket_target_order_reconciliations(order_intent_id,state,"
        "filled_size,filled_notional_usdc,updated_at) VALUES(?,?,?,?,?)",
        ("rung_a", "unknown", 0.0, 0.0, "2026-09-06T15:00:00Z"),
    )
    gate = can_submit_next_order(
        conn, bucket="20C", token_id="tok",
        exclude_order_intent_id="rung_b", same_cycle_id="cycle_1",
    )
    assert gate["allowed"] is False, gate


def test_quiescence_probes_ask_about_the_scope_they_protect() -> None:
    """The zero-pending snapshot is produced per (station, event day), and the
    probes that gate it must ask about that scope.

    Measured 2026-09-06 23:50Z: ten resting orders for 2026-09-07 and
    2026-09-08 refused the market ingress for ten 2026-09-06 scopes holding no
    order of their own -- CYYZ, KATL, KAUS, KBKF, KDAL, KHOU, KLAX, KLGA,
    KMIA, KORD -- so quotes stopped refreshing and every scope died on
    production_quote_expired, while the charter-94 lifecycle that follows the
    bid read a book 281 minutes old.  Fleet-wide probes could not bite under
    the taker family, where an intent was ``submitted`` only between the POST
    and its fill and nothing rested.
    """
    from bayes_basket_target.order_state_producer import (
        _SCOPE_PREDICATES, TARGET_INTENT_BLOCKING_STATES,
    )

    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,plan_sha256,requested_reserve_usdc,state,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("cyc_kmia", "plan_kmia", "KMIA", "2026-09-08", "America/New_York", "basket",
         "b" * 64, 1.0, "prepared", "2026-09-06T20:00:00Z", "2026-09-06T20:00:00Z"),
    )
    _seed(conn, intent_id="resting_kmia", cycle_id="cyc_kmia", state="submitted")

    states = sorted(TARGET_INTENT_BLOCKING_STATES)
    sql = _SCOPE_PREDICATES["basket_target_order_intents"].format(
        states=",".join("?" for _ in states)
    )

    def found(station: str, day: str) -> bool:
        return conn.execute(sql, (*states, station, station, day, day)).fetchone() is not None

    assert found("KMIA", "2026-09-08") is True, "its own scope must still be refused"
    assert found("CYYZ", "2026-09-06") is False, "another scope must not be blinded by it"
    assert found("KMIA", "2026-09-07") is False, "same station, other day, is another scope"
    assert found("", "") is True, "the fleet-wide question keeps the fleet answer"


def test_a_row_whose_scope_is_unknown_blocks_every_scope() -> None:
    """An inner join would drop an intent with no cycle row, turning "I cannot
    tell whose this is" into "it is nobody's" -- the one answer this guard must
    never give.  Left join, and the orphan blocks everyone."""
    from bayes_basket_target.order_state_producer import (
        _SCOPE_PREDICATES, TARGET_INTENT_BLOCKING_STATES,
    )

    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    _seed(conn, intent_id="orphan", cycle_id="cycle_that_does_not_exist", state="submitted")
    states = sorted(TARGET_INTENT_BLOCKING_STATES)
    sql = _SCOPE_PREDICATES["basket_target_order_intents"].format(
        states=",".join("?" for _ in states)
    )
    for station, day in (("CYYZ", "2026-09-06"), ("KMIA", "2026-09-08"), ("", "")):
        assert conn.execute(sql, (*states, station, station, day, day)).fetchone() is not None


def test_a_repost_may_only_replenish_the_rungs_the_leg_still_owes():
    """Charter 94's anchored re-post is capped by the gap, like the open.

    The anchored branch returned before the ``steps`` cap, so a leg whose
    ladder had degenerated to one rung -- charter 94(二), which is most legs
    -- was re-posted three rungs wide every five minutes and could buy three
    times the position it was sized for.  Measured on the rebuilt book,
    218 of 273 positions had a leg that filled past its intended shares.
    Production never had it: its re-mint sizes the ladder from the gap and
    the sequential gate holds the bucket until the open order resolves, and
    0 of 262 authorised live legs ever filled past their requested size.
    """

    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(root / "scripts"))
    import paper_trading_ledger as ledger

    quote = {
        "bid_price": 0.20,
        "best_ask": 0.26,
        "price_tick_size": 0.01,
        "ask_levels": [{"price": 0.26, "shares": 100.0}],
    }
    assert ledger.MAKER_RESTING_ANCHORED, "this is the anchored branch"
    owed_one = ledger._ladder_from_quote(quote, 1)
    owed_two = ledger._ladder_from_quote(quote, 2)
    owed_three = ledger._ladder_from_quote(quote, 3)
    # Charter 110 leaves one rung, so the cap can never bind -- which is the
    # strongest possible form of the property this test was written for: the
    # re-post cannot exceed what the leg owes because there is only ever one
    # rung to re-post.  The cap stays in the code for the band arm, which
    # still has a multi-rung ladder.
    assert len(owed_one) == 1
    assert owed_one == owed_two == owed_three
    assert owed_three == sorted(owed_three, reverse=True)
    assert owed_one == ledger.bid_anchored_ladder(bid=0.20, ask=0.26, tick=0.01)
