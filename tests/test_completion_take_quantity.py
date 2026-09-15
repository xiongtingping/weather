"""A completion FOK asks for the gap in shares, whatever levels it walks.

Audit 2026-09-14: the charter-127 completion descriptor carried the walk's
cash and the walk's deepest price, and every reader downstream turns a
candidate back into shares as ``cash / limit``.  On a book that steps up --
5 @ 0.20 then 5 @ 0.30 for a ten-share gap -- that is 2.50 / 0.30 = 8.33, and
the sizing step floored it to an eight-share FOK: the certified completion
that buys short, the defect charter 120 removed from the search.  The depth
term did the same thing a second time (level cash over the limit).

On a single level the walk's cash IS ``gap * limit``, so nothing that takes
one price moves; these tests pin the stepped book.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from bayes_basket_target import delta_v2_runtime
from bayes_basket_target.basket_kernel.contracts import AskLevel
from bayes_basket_target.basket_kernel.delta_v2 import ask_depth_walk_v1
from bayes_basket_target.cash_phase import OrderSizingDecision
from bayes_basket_target.runtime import _executable_depth_cash

from tests import test_charter_84_ladder_balance as fixture


STEPPED = (AskLevel(price=0.20, shares=5.0), AskLevel(price=0.30, shares=5.0))


class _Phase(SimpleNamespace):
    """The sizing step reads the phase template only when it caps the target."""

    def __getattr__(self, name):  # noqa: D401 -- audit fields, not read here
        return "phase"

    def to_dict(self):
        return {"phase": "stub"}


def test_a_stepped_book_is_depth_for_every_share_under_the_limit() -> None:
    assert _executable_depth_cash(
        ask_levels=STEPPED, execution_candidate="TAKER_FOK",
        limit_price=0.30, cash_clip=0.0,
    ) == pytest.approx(3.00)
    # Only the shares at or under the limit count.
    assert _executable_depth_cash(
        ask_levels=STEPPED, execution_candidate="TAKER_FOK",
        limit_price=0.20, cash_clip=0.0,
    ) == pytest.approx(1.00)


def test_the_sized_fok_is_the_whole_gap_on_a_stepped_book() -> None:
    walk = ask_depth_walk_v1(levels=STEPPED, shares=10.0)
    assert walk == {"cash": 2.5, "marginal_price": 0.3, "levels_consumed": 2.0}
    limit = float(walk["marginal_price"])

    sizing = OrderSizingDecision.compute(
        bucket="29°C",
        phase_decision=_Phase(cash_template_usdc=15.0),
        delta_shares=10.0,
        r_limit=limit,
        remaining_budget=50.0,
        executable_depth_cash=_executable_depth_cash(
            ask_levels=STEPPED, execution_candidate="TAKER_FOK",
            limit_price=limit, cash_clip=10.0 * limit,
        ),
        depth_is_market_bound=True,
        candidate_present=True,
        target_clip_notional_usdc=10.0 * limit,
        minimum_lot_shares=5.0,
        cash_template_caps_target=False,
        share_quantum=0.01,
    )

    assert sizing.eligible is True
    assert sizing.q_order == pytest.approx(10.0)
    # Reserved at what the FOK may pay, not at what the book charges now.
    assert sizing.c_order == pytest.approx(3.00)


def test_the_compiled_completion_candidate_is_the_gap_at_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the compile, on the charter-84 fixture.

    The dollar lift is switched off so the short rung's gap is the held level
    (five shares) rather than a lot raise priced at the top of book, which
    refuses a stepped walk by name before it reaches the descriptor.  The
    reservation verdict is set to "no grid price" -- the joint path, where the
    whole basket authorizes the rung -- so what is under test is the
    descriptor's quantity and nothing about the reservation grid.
    """

    monkeypatch.setattr(
        delta_v2_runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_83_MIN_NOTIONAL", "off")
    solve = delta_v2_runtime.solve_reservation_price_v2
    monkeypatch.setattr(
        delta_v2_runtime,
        "solve_reservation_price_v2",
        lambda **kwargs: {**solve(**kwargs), "reservation_price": None},
    )
    snapshot = fixture._held_snapshot()
    rows = tuple(
        replace(
            row,
            ask_levels=(
                AskLevel(price=0.15, shares=3.0),
                AskLevel(price=0.16, shares=100.0),
            ),
        )
        if row.bucket == "32" else row
        for row in snapshot.buckets
    )

    result = fixture.compile_delta_v2_runtime_plan(
        snapshot=replace(snapshot, buckets=rows),
        basket_policy=fixture._basket_policy(),
        delta_policy=fixture._delta_policy(),
        fill_resolver=fixture._fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )

    assert result["target_solution"]["winner_candidate_origin"] == (
        fixture.LADDER_BALANCE_CANDIDATE_ORIGIN
    )
    short = [
        row for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
    ]
    assert [row["bucket"] for row in short] == ["32"]
    leg = short[0]
    assert leg["execution_candidate"] == "TAKER_FOK"
    assert leg["candidate_price"] == pytest.approx(0.16)
    # Was 4.8125 shares on 0.77 of cash: the walk's cash over its deepest price.
    assert leg["clip_shares"] == pytest.approx(leg["delta_shares"]) == pytest.approx(5.0)
    assert leg["clip_notional_usdc"] == pytest.approx(5.0 * 0.16)


def _stepped_compile(monkeypatch: pytest.MonkeyPatch, levels):
    monkeypatch.setattr(
        delta_v2_runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )
    snapshot = fixture._held_snapshot()
    rows = tuple(
        replace(
            row,
            ask_levels=tuple(
                AskLevel(price=price, shares=shares) for price, shares in levels
            ),
        )
        if row.bucket == "32" else row
        for row in snapshot.buckets
    )
    return fixture.compile_delta_v2_runtime_plan(
        snapshot=replace(snapshot, buckets=rows),
        basket_policy=fixture._basket_policy(),
        delta_policy=fixture._delta_policy(),
        fill_resolver=fixture._fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )


def test_a_lot_lifted_completion_on_a_stepped_book_is_sent_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit 2026-09-14, the wrong refusal, both halves.

    32C is short five shares and the dollar floor lifts it to seven.  On
    3 @ 0.15 + 100 @ 0.16 the take walks 1.09.  The lift was priced at the
    top of book (authorizing 1.07), so the leg was refused
    ``completion_take_above_authorized_cost``; with that fixed, the taker
    reservation grid held only the top of book, found a reservation a tick
    under the 0.16 limit and dropped the leg as
    ``leg_candidate_rejected_by_reservation_or_size``.  Nothing rested in its
    place either way, and the basket stayed one rung short.
    """

    result = _stepped_compile(monkeypatch, ((0.15, 3.0), (0.16, 100.0)))

    assert result["target_solution"]["winner_candidate_origin"] == (
        fixture.LADDER_BALANCE_CANDIDATE_ORIGIN
    )
    assert result["maker_candidate_diagnostics"] == {}
    short = [
        row for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
    ]
    assert [row["bucket"] for row in short] == ["32"]
    leg = short[0]
    assert leg["execution_candidate"] == "TAKER_FOK"
    assert leg["delta_shares"] == pytest.approx(7.0)
    assert leg["clip_shares"] == pytest.approx(7.0)
    assert leg["candidate_price"] == pytest.approx(0.16)


def test_a_stepped_book_too_dear_is_refused_by_the_gates_that_price_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pricing the lift on the walk does not buy whatever the book charges:
    the post-lift floor and edge read the walked cost and refuse by name."""

    result = _stepped_compile(monkeypatch, ((0.15, 1.0), (0.90, 100.0)))

    assert not [
        row for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
        and str(row.get("execution_candidate") or "") != "NONE"
    ]
    codes = set(result.get("reason_codes") or ())
    assert codes & {"support_floor_after_lift", "support_edge_after_lift"}, codes
