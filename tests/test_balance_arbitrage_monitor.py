from __future__ import annotations

import math

import pytest

from balance_arbitrage_monitor import (
    FeeSpec,
    MarketBinding,
    build_complete_sets,
    propose_maker_leg,
    validate_exhaustive_partition,
)
from scripts.monitor_zuuu_balance_arbitrage import BookState


def _market(outcome, condition, yes, no, kind, lower, upper):
    return MarketBinding(
        slug="highest-temperature-in-chengdu-on-july-19-2026",
        event_id="event-1",
        event_date="2026-07-19",
        outcome=outcome,
        condition_id=condition,
        yes_token=yes,
        no_token=no,
        kind=kind,
        lower=lower,
        upper=upper,
    )


def _partition():
    return [
        _market("33°C or below", "c0", "y0", "n0", "lower_tail", None, 33.0),
        _market("34°C", "c1", "y1", "n1", "single", 34.0, 34.0),
        _market("35°C or higher", "c2", "y2", "n2", "upper_tail", 35.0, None),
    ]


def _fee():
    return FeeSpec(rate=0.05, exponent=1.0, taker_only=True)


def _book(bid, ask, bid_size=10):
    return {
        "bids": [] if bid is None else [{"price": bid, "size": bid_size}],
        "asks": [] if ask is None else [{"price": ask, "size": 10}],
    }


def test_taker_only_evidence_makes_maker_fee_zero():
    assert _fee().maker_fee_per_share(0.4) == 0.0
    with pytest.raises(ValueError, match="maker_fee_zero_not_proven"):
        FeeSpec(rate=0.05, exponent=1.0, taker_only=False).maker_fee_per_share(0.4)


def test_maker_quote_improves_best_bid_one_tick_without_crossing():
    leg = propose_maker_leg(
        outcome="34°C",
        side="YES",
        token_id="y",
        book=_book(0.40, 0.42),
        fee=_fee(),
        tick_size=0.001,
    )
    assert leg is not None
    assert leg.order_price == 0.401
    assert leg.quote_mode == "improve_best_bid_one_tick"
    assert leg.queue_ahead_shares == 0.0


def test_maker_quote_joins_best_bid_when_next_tick_would_cross():
    leg = propose_maker_leg(
        outcome="34°C",
        side="NO",
        token_id="n",
        book=_book(0.999, None, bid_size=77),
        fee=_fee(),
        tick_size=0.001,
    )
    assert leg is not None
    assert leg.order_price == 0.999
    assert leg.quote_mode == "join_best_bid"
    assert leg.queue_ahead_shares == 77


def test_pair_signal_uses_resting_bid_prices_not_asks_or_taker_fee():
    market = _market("34°C", "c", "y", "n", "single", 34.0, 34.0)
    books = {"y": _book(0.40, 0.45), "n": _book(0.55, 0.60)}
    result = build_complete_sets(
        [market],
        books,
        {"y": _fee(), "n": _fee()},
        {"y": 0.001, "n": 0.001},
        target_shares=5,
        minimum_edge=0.01,
        minimum_order_shares=5,
    )["pairs"][0]
    assert math.isclose(result["maker_price_sum"], 0.952)
    assert result["maker_fee_per_set_usdc"] == 0.0
    assert math.isclose(result["net_edge_per_set_usdc"], 0.048)
    assert math.isclose(result["net_profit_if_all_filled_usdc"], 0.24)
    assert result["signal"] is True


def test_partition_and_all_bucket_maker_payout_invariants():
    markets = _partition()
    assert validate_exhaustive_partition(markets) == (True, "ok")
    books = {
        "y0": _book(0.10, 0.20),
        "y1": _book(0.20, 0.30),
        "y2": _book(0.30, 0.40),
        "n0": _book(0.70, 0.80),
        "n1": _book(0.60, 0.70),
        "n2": _book(0.50, 0.60),
    }
    result = build_complete_sets(
        markets,
        books,
        {token: _fee() for token in books},
        {token: 0.001 for token in books},
        target_shares=5,
        minimum_edge=0.01,
        minimum_order_shares=5,
    )
    assert result["all_yes"]["payout_per_complete_set_usdc"] == 1.0
    assert math.isclose(result["all_yes"]["net_edge_per_set_usdc"], 0.397)
    assert result["all_yes"]["signal"] is True
    assert result["all_no"]["payout_per_complete_set_usdc"] == 2.0
    assert math.isclose(result["all_no"]["net_edge_per_set_usdc"], 0.197)
    assert result["all_no"]["signal"] is True


def test_partition_gap_blocks_cross_bucket_sets_but_not_pairs():
    markets = [_partition()[0], _partition()[2]]
    books = {
        token: _book(0.40, 0.50)
        for row in markets
        for token in (row.yes_token, row.no_token)
    }
    result = build_complete_sets(
        markets,
        books,
        {token: _fee() for token in books},
        {token: 0.001 for token in books},
        target_shares=5,
        minimum_edge=0.01,
        minimum_order_shares=5,
    )
    assert result["partition_ok"] is False
    assert result["partition_reason"] == "partition_has_gap_or_overlap"
    assert result["all_yes"] is None
    assert len(result["pairs"]) == 2


def test_book_state_requires_full_books_and_applies_bid_and_ask_deltas():
    state = BookState(["a", "b"])
    state.apply(
        {
            "event_type": "book",
            "asset_id": "a",
            "bids": [{"price": "0.4", "size": "5"}],
            "asks": [{"price": "0.5", "size": "6"}],
        }
    )
    state.apply({"event_type": "book", "asset_id": "b", "bids": [], "asks": []})
    assert state.ready is True
    assert state.snapshot()["a"] == {
        "bids": [{"price": 0.4, "size": 5.0}],
        "asks": [{"price": 0.5, "size": 6.0}],
    }
    state.apply(
        {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "a", "side": "BUY", "price": "0.4", "size": "0"},
                {"asset_id": "a", "side": "SELL", "price": "0.49", "size": "7"},
            ],
        }
    )
    assert state.snapshot()["a"]["bids"] == []
    assert state.snapshot()["a"]["asks"][0] == {"price": 0.49, "size": 7.0}
