"""The kernel admits the bookless bucket its own producers emit.

Charter 48: a listing-day bucket nobody has quoted on either side is a legal
market state when it names itself.  The materializer and the production
reader admitted that shape; the kernel did not, so every producer that had to
hand it a bookless bucket invented a ceiling ask (max(tick, 1-tick) on a
tick-sized level) instead.  Unfillable, which was the intent -- but summed
into basket costs, one of those placeholders reached the page as a basket
"costing" 2.997 for three mutually exclusive outcomes, which no book asked.

KLAX 2026-08-18 is the measured case: nine of eleven buckets carried
ask_levels=[] and ask_available=False in the raw quote.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.contracts import (  # noqa: E402
    BucketObservation,
    ContractError,
)


def _raw(**overrides: object) -> dict[str, object]:
    # Exactly the mapping p0_preselection builds for a declared-unmaterialized
    # token, zero tick and all (tick_size_source UNMATERIALIZED_BOOK).
    base: dict[str, object] = {
        "bucket": "71F or below",
        "ordinal": 0,
        "probability": 0.05,
        "unmaterialized_book": True,
        "bid_price": 0.0,
        "ask_levels": [],
        "ask_available": False,
        "bid_levels": [],
        "price_tick_size": 0.0,
        "tick_size_source": "UNMATERIALIZED_BOOK",
        "maker_queue_ahead_shares": 0.0,
        "maker_queue_known": False,
        "filled_shares": 0.0,
        "filled_cost_usdc": 0.0,
        "pending_shares": 0.0,
        "pending_notional_usdc": 0.0,
        "active_before": False,
        "token_id": "t",
    }
    base.update(overrides)
    return base


def test_the_producers_own_bookless_shape_is_admitted() -> None:
    value = BucketObservation.from_mapping(_raw())
    assert value.unmaterialized_book is True
    assert value.ask_available is False
    assert value.ask_levels == ()
    assert value.bid_levels == ()
    # The unknown grid takes the kernel's own absent-key default rather than
    # failing the whole snapshot; nothing can price against this bucket
    # while both sides are empty.
    assert value.price_tick_size == 0.01
    # Unbuyable stays unbuyable: the price accessor refuses, it does not
    # invent a ceiling.
    with pytest.raises(ContractError, match="bucket_ask_unavailable"):
        _ = value.ask_price


def test_an_unnamed_empty_book_is_still_a_defect() -> None:
    with pytest.raises(ContractError, match="bucket_book_empty"):
        BucketObservation.from_mapping(
            _raw(unmaterialized_book=False, price_tick_size=0.01)
        )


@pytest.mark.parametrize(
    "quoted",
    [
        {"ask_levels": [{"price": 0.06, "shares": 5.0}], "ask_available": True},
        {
            "bid_price": 0.04,
            "bid_levels": [{"price": 0.04, "shares": 13.0}],
        },
        {"bid_price": 0.04},
    ],
)
def test_a_quoted_book_may_not_call_itself_unmaterialized(
    quoted: dict[str, object],
) -> None:
    # "Unbuyable" must never be a label a priced bucket can hide behind.
    with pytest.raises(
        ContractError, match="bucket_unmaterialized_book_quoted"
    ):
        BucketObservation.from_mapping(_raw(price_tick_size=0.01, **quoted))


def test_a_bid_only_bucket_needs_no_flag_and_gains_none() -> None:
    value = BucketObservation.from_mapping(
        _raw(
            unmaterialized_book=False,
            price_tick_size=0.01,
            bid_price=0.04,
            bid_levels=[{"price": 0.04, "shares": 13.0}],
        )
    )
    assert value.ask_available is False
    assert value.unmaterialized_book is False


def test_every_book_empty_gate_admits_the_named_shape() -> None:
    """One admission, three readers: materializer, production reader, kernel.

    The materializer had the escape hatch from the start; the production
    reader and the kernel each carried an unconditional rejection, which is
    what forced the ceiling-ask placeholder into every producer that could
    meet a bookless bucket.
    """

    for path, gate in (
        ("bayes_basket_target/market_materialization.py", "official_ws_book_empty"),
        ("bayes_basket_target/production_io.py", "production_ws_book_empty"),
        ("bayes_basket_target/basket_kernel/contracts.py", "bucket_book_empty"),
    ):
        source = (ROOT / path).read_text(encoding="utf-8")
        gate_at = source.index(f'"{gate}"')
        window = source[max(0, gate_at - 600):gate_at]
        assert "unmaterialized_book" in window, path

    # And the ledger no longer dresses an empty book as a ceiling ask.
    ledger = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "unfillable_ask" not in ledger
    assert '"unmaterialized_book": empty_book' in ledger
