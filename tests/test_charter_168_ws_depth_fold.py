"""Charter 168: the order book's tail is folded, not dropped.

User ruling 2026-09-20 「盘口截断（只对新写入）」.  What these bind is the
reason the tail is folded rather than truncated: the sizing layer reads
``sum(level.shares)`` as the liquidity at a price, and five levels hold a
median 23.8% of a side's depth.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.market_ingress import (  # noqa: E402
    DEPTH_HEAD_LEVELS,
    DEPTH_HEAD_LEVELS_ENV,
    depth_head_levels,
    fold_depth_tail,
)


def _asks(n: int) -> list[dict[str, float]]:
    return [{"price": 0.40 + i * 0.01, "shares": 10.0 * (i + 1)} for i in range(n)]


def _bids(n: int) -> list[dict[str, float]]:
    return [{"price": 0.39 - i * 0.01, "shares": 7.0 * (i + 1)} for i in range(n)]


@pytest.mark.parametrize("make,ascending", [(_asks, True), (_bids, False)])
def test_the_folded_side_keeps_the_depth_the_sizing_layer_reads(make, ascending) -> None:
    """The whole reason for folding instead of truncating.  ``sum(shares)``
    is what ``delta_v2_runtime._maker_resting_band`` calls the liquidity at
    a price, and it comes back to the float."""

    levels = make(20)
    folded = fold_depth_tail(levels, ascending=ascending)
    assert len(folded) == DEPTH_HEAD_LEVELS + 1
    assert sum(x["shares"] for x in folded) == pytest.approx(
        sum(x["shares"] for x in levels), abs=1e-9,
    )


@pytest.mark.parametrize("make,ascending", [(_asks, True), (_bids, False)])
def test_the_touch_and_the_head_survive_verbatim(make, ascending) -> None:
    """Everything that prices an order reads the first levels: the best
    quote, the one-tick rung, the walk for an order this system's size.
    Those must be the exchange's own numbers, not an average."""

    levels = make(20)
    folded = fold_depth_tail(levels, ascending=ascending)
    assert folded[:DEPTH_HEAD_LEVELS] == levels[:DEPTH_HEAD_LEVELS]


@pytest.mark.parametrize("make,ascending", [(_asks, True), (_bids, False)])
def test_monotonicity_holds_because_contracts_enforces_it(make, ascending) -> None:
    """``contracts`` refuses a side that is not strictly monotone.  The
    folded price is a weighted mean of levels beyond the head, so it sits
    past the head on the correct side -- asserted, not assumed."""

    folded = fold_depth_tail(make(20), ascending=ascending)
    prices = [x["price"] for x in folded]
    pairs = list(zip(prices, prices[1:]))
    assert all((b > a) if ascending else (b < a) for a, b in pairs)
    assert all(0.0 < p <= 1.0 for p in prices)


@pytest.mark.parametrize("n", [0, 1, 4, 5, 6])
def test_a_side_with_nothing_to_gain_is_returned_unchanged(n: int) -> None:
    """A tail of one folds to one: no saving, and it would replace a real
    quote with an average for nothing."""

    levels = _asks(n)
    assert fold_depth_tail(levels, ascending=True) == levels


def test_the_switch_turns_it_off_entirely(monkeypatch) -> None:
    """``PM_WS_DEPTH_HEAD_LEVELS=0`` stores every level, which is what the
    store did before this charter."""

    levels = _asks(20)
    monkeypatch.setenv(DEPTH_HEAD_LEVELS_ENV, "0")
    assert depth_head_levels() == 0
    assert fold_depth_tail(levels, ascending=True) == levels
    monkeypatch.setenv(DEPTH_HEAD_LEVELS_ENV, "3")
    assert len(fold_depth_tail(levels, ascending=True)) == 4
    monkeypatch.setenv(DEPTH_HEAD_LEVELS_ENV, "not a number")
    assert depth_head_levels() == DEPTH_HEAD_LEVELS


def test_a_fold_that_would_break_the_invariant_is_refused() -> None:
    """The fail-safe.  A side whose tail cannot produce an ordered price --
    zero shares throughout -- comes back whole rather than malformed."""

    levels = _asks(20)
    for level in levels[DEPTH_HEAD_LEVELS:]:
        level["shares"] = 0.0
    assert fold_depth_tail(levels, ascending=True) == levels


def test_the_ingress_folds_after_the_tick_is_derived() -> None:
    """A share-weighted price is not tick aligned.  Folding before the tick
    derivation would put a wrong tick on the bucket, so the call sits after
    it -- asserted on the order of the two statements, which is the thing
    that can silently regress."""

    import ast

    source = (ROOT / "bayes_basket_target" / "market_ingress.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fold_lines = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "fold_depth_tail"
    ]
    tick_lines = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "derive_official_ws_price_tick_size"
    ]
    assert fold_lines and tick_lines
    assert min(fold_lines) > max(tick_lines)
