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
    tick_outward,
)

TICK = 0.01


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
    folded = fold_depth_tail(levels, ascending=ascending, tick=TICK)
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
    folded = fold_depth_tail(levels, ascending=ascending, tick=TICK)
    assert folded[:DEPTH_HEAD_LEVELS] == levels[:DEPTH_HEAD_LEVELS]


@pytest.mark.parametrize("make,ascending", [(_asks, True), (_bids, False)])
def test_monotonicity_holds_because_contracts_enforces_it(make, ascending) -> None:
    """``contracts`` refuses a side that is not strictly monotone.  The
    folded price is a weighted mean of levels beyond the head, so it sits
    past the head on the correct side -- asserted, not assumed."""

    folded = fold_depth_tail(make(20), ascending=ascending, tick=TICK)
    prices = [x["price"] for x in folded]
    pairs = list(zip(prices, prices[1:]))
    assert all((b > a) if ascending else (b < a) for a, b in pairs)
    assert all(0.0 < p <= 1.0 for p in prices)


@pytest.mark.parametrize("n", [0, 1, 4, 5, 6])
def test_a_side_with_nothing_to_gain_is_returned_unchanged(n: int) -> None:
    """A tail of one folds to one: no saving, and it would replace a real
    quote with an average for nothing."""

    levels = _asks(n)
    assert fold_depth_tail(levels, ascending=True, tick=TICK) == levels


def test_the_switch_turns_it_off_entirely(monkeypatch) -> None:
    """``PM_WS_DEPTH_HEAD_LEVELS=0`` stores every level, which is what the
    store did before this charter."""

    levels = _asks(20)
    monkeypatch.setenv(DEPTH_HEAD_LEVELS_ENV, "0")
    assert depth_head_levels() == 0
    assert fold_depth_tail(levels, ascending=True, tick=TICK) == levels
    monkeypatch.setenv(DEPTH_HEAD_LEVELS_ENV, "3")
    assert len(fold_depth_tail(levels, ascending=True, tick=TICK)) == 4
    monkeypatch.setenv(DEPTH_HEAD_LEVELS_ENV, "not a number")
    assert depth_head_levels() == DEPTH_HEAD_LEVELS


def test_a_fold_that_would_break_the_invariant_is_refused() -> None:
    """The fail-safe.  A side whose tail cannot produce an ordered price --
    zero shares throughout -- comes back whole rather than malformed."""

    levels = _asks(20)
    for level in levels[DEPTH_HEAD_LEVELS:]:
        level["shares"] = 0.0
    assert fold_depth_tail(levels, ascending=True, tick=TICK) == levels


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


# --- The kernel reads every rung against the tick (09-21) -------------------
#
# The first cut of this charter kept monotonicity and left the folded price
# where the share-weighted average fell.  ``contracts`` checks EVERY level
# against the bucket's ``price_tick_size`` and refuses the whole bucket on one
# stray rung (``bucket_price_tick_mismatch``): from the first cycle after it
# shipped, every deep same-day book was refused and P1 fell from 31-55
# decisions an hour to 1-5.  None of the tests above parsed a folded bucket.


def _bucket(asks, bids, tick=TICK) -> dict:
    return {
        "bucket": "20-21", "ordinal": 3, "probability": 0.2,
        "ask_levels": asks, "ask_available": bool(asks),
        "bid_levels": bids, "bid_price": bids[0]["price"] if bids else 0.0,
        "price_tick_size": tick,
    }


def _ladder(start: float, n: int, step: float, sign: int, shares=lambda i: 3.0 + 7.0 * i):
    return [{"price": round(start + sign * i * step, 6), "shares": float(shares(i))} for i in range(n)]


@pytest.mark.parametrize("n_ask,n_bid", [(7, 3), (8, 8), (16, 16), (40, 25), (70, 29)])
def test_a_folded_bucket_is_one_the_kernel_accepts(n_ask: int, n_bid: int) -> None:
    """The test the first cut lacked: the production parser, on the output."""

    from bayes_basket_target.basket_kernel.contracts import BucketObservation

    asks = _ladder(0.30, n_ask, TICK, +1)
    bids = _ladder(0.29, n_bid, TICK, -1)
    BucketObservation.from_mapping(_bucket(asks, bids))       # the raw book parses
    folded = _bucket(
        fold_depth_tail(asks, ascending=True, tick=TICK),
        fold_depth_tail(bids, ascending=False, tick=TICK),
    )
    BucketObservation.from_mapping(folded)                    # and so must the fold


@pytest.mark.parametrize("tick", [0.01, 0.001])
def test_the_folded_rung_moves_outward_onto_the_grid(tick: float) -> None:
    """Up on an ask side, down on a bid side: never a better price than the
    book offered, and always on the grid."""

    asks = _ladder(0.50, 20, tick, +1)
    bids = _ladder(0.49, 20, tick, -1)
    for levels, ascending in ((asks, True), (bids, False)):
        tail = levels[DEPTH_HEAD_LEVELS:]
        mean = sum(x["price"] * x["shares"] for x in tail) / sum(x["shares"] for x in tail)
        rung = fold_depth_tail(levels, ascending=ascending, tick=tick)[-1]["price"]
        assert abs(rung / tick - round(rung / tick)) <= 1e-7
        assert (rung >= mean - 1e-12) if ascending else (rung <= mean + 1e-12)
        assert abs(rung - mean) < tick + 1e-12


@pytest.mark.parametrize("tick", [None, 0.0, -0.01, float("nan"), "x"])
def test_without_a_usable_tick_nothing_is_folded(tick) -> None:
    """No grid, no fold: an unaligned rung is worse than a long ladder."""

    levels = _asks(20)
    assert fold_depth_tail(levels, ascending=True, tick=tick) == levels


def test_tick_outward_is_exact_on_the_grid_and_directional_off_it() -> None:
    assert tick_outward(0.35, 0.01, ascending=True) == 0.35
    assert tick_outward(0.35, 0.01, ascending=False) == 0.35
    assert tick_outward(0.355283, 0.01, ascending=True) == 0.36
    assert tick_outward(0.219444, 0.01, ascending=False) == 0.21
    assert tick_outward(0.0405, 0.001, ascending=True) == 0.041


def test_every_ingress_fold_is_given_the_bucket_tick() -> None:
    """The call sites, not just the function: a fold without ``tick`` is a
    silent no-op, and one with the wrong tick is the defect again."""

    import ast

    source = (ROOT / "bayes_basket_target" / "market_ingress.py").read_text(encoding="utf-8")
    calls = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "fold_depth_tail"
    ]
    assert calls
    for call in calls:
        tick = [kw for kw in call.keywords if kw.arg == "tick"]
        assert tick and isinstance(tick[0].value, ast.Name) and tick[0].value.id == "price_tick_size"


def test_the_rebuild_repairs_rungs_the_first_cut_stored_off_grid() -> None:
    """Rows production folded between the charter shipping and the fix carry
    an off-grid sixth rung.  The rebuild moves exactly that rung, outward,
    and leaves an aligned stored row byte for byte."""

    import importlib.util
    import json

    from bayes_basket_target.basket_kernel.contracts import BucketObservation, ContractError

    spec = importlib.util.spec_from_file_location(
        "rebuild_ssot_compact", ROOT / "scripts" / "rebuild_ssot_compact.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    head_a = _ladder(0.30, DEPTH_HEAD_LEVELS, TICK, +1)
    head_b = _ladder(0.29, DEPTH_HEAD_LEVELS, TICK, -1)
    stored = [_bucket(head_a + [{"price": 0.355283, "shares": 90.0}],
                      head_b + [{"price": 0.219444, "shares": 80.0}])]
    with pytest.raises(ContractError, match="bucket_price_tick_mismatch"):
        BucketObservation.from_mapping(stored[0])
    raw = json.dumps(stored, sort_keys=True)
    repaired = json.loads(module._fold_payload(raw))[0]
    assert repaired["ask_levels"][-1] == {"price": 0.36, "shares": 90.0}
    assert repaired["bid_levels"][-1] == {"price": 0.21, "shares": 80.0}
    assert repaired["ask_levels"][:-1] == head_a and repaired["bid_levels"][:-1] == head_b
    BucketObservation.from_mapping(repaired)

    aligned = json.dumps([_bucket(head_a + [{"price": 0.36, "shares": 90.0}], head_b)], sort_keys=True)
    assert module._fold_payload(aligned) is aligned
