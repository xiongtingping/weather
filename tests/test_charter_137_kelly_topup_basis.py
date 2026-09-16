"""Charter 137: which budget the Kelly target LEVEL is read off.

The clause exists because production read a stock (the level a leg should
hold) off a flow (the cash still unspent this compile) and then compared it
with the shares already held.  These tests hold three things: the shipped
``net`` basis reproduces the pre-clause arithmetic exactly, the ``gross``
arm actually opens the valve on a chain that holds a full-size P0 seat, and
both lanes read one definition.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from bayes_basket_target import delta_v2_runtime as PROD
from bayes_basket_target.basket_kernel import delta_v2 as K

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class _Level:
    price: float
    shares: float


def _kelly_candidate(*, basis: str, held: float, capital: float, stage: str):
    """Run production's candidate generator under one basis.

    ``monkeypatch`` cannot reach the kernel's module constant through the
    runtime's ``from ... import``, so the basis is threaded the way the
    generator reads it: through the kernel helper the generator calls.
    """

    ordered = ("20°C", "21°C")
    levels = {bucket: [_Level(0.25, 500.0)] for bucket in ordered}
    prices = {bucket: 0.25 for bucket in ordered}
    zero = {bucket: 0.0 for bucket in ordered}
    original = K.KELLY_TOPUP_BASIS
    prod_original = PROD.KELLY_TOPUP_BASIS
    K.KELLY_TOPUP_BASIS = basis
    PROD.KELLY_TOPUP_BASIS = basis
    # ``kelly_target_budget_usdc_v1``'s default argument was bound at
    # definition time, so the basis has to arrive as the caller's value.
    default = K.kelly_target_budget_usdc_v1.__kwdefaults__
    K.kelly_target_budget_usdc_v1.__kwdefaults__ = dict(default, basis=basis)
    try:
        return PROD._whole_basket_kelly_candidate(
            ordered=ordered,
            lane_levels=levels,
            lane_prices=prices,
            pricing_basis="test",
            current_shares={bucket: held for bucket in ordered},
            current_cost={bucket: held * 0.25 for bucket in ordered},
            pending_shares=dict(zero),
            pending_cost=dict(zero),
            capital_available_usdc=capital,
            probabilities_by_bucket={"20°C": 0.55, "21°C": 0.35},
            book_equity_usdc=300.0,
            quote_version="q1",
            centre_bucket="20°C",
            ordinals={"20°C": 1, "21°C": 2},
            stage=stage,
        )
    finally:
        K.kelly_target_budget_usdc_v1.__kwdefaults__ = default
        K.KELLY_TOPUP_BASIS = original
        PROD.KELLY_TOPUP_BASIS = prod_original


def test_the_shipped_basis_is_the_pre_clause_one() -> None:
    """``net`` is charter 72 as written; the arm has to be asked for."""

    assert K.KELLY_TOPUP_BASIS_MODES == ("net", "gross")
    assert "PM_KELLY_TOPUP_BASIS" in (
        (ROOT / "bayes_basket_target" / "basket_kernel" / "delta_v2.py")
        .read_text(encoding="utf-8")
    )
    # Absent an override this process is on the shipped basis, which is what
    # every unqualified reading in this repo assumes.
    if not os.environ.get("PM_KELLY_TOPUP_BASIS"):
        assert K.KELLY_TOPUP_BASIS == "net"


def test_net_returns_the_room_and_gross_returns_the_stage_allowance() -> None:
    room = 1.20
    net = K.kelly_target_budget_usdc_v1(
        book_equity_usdc=300.0, stage="P0-M",
        capital_available_usdc=room, basis="net",
    )
    gross = K.kelly_target_budget_usdc_v1(
        book_equity_usdc=300.0, stage="P0-M",
        capital_available_usdc=room, basis="gross",
    )
    assert net == pytest.approx(room)
    # 2% of the book, times P0-M's 0.8 share, with nothing committed.
    assert gross == pytest.approx(300.0 * 0.02 * 0.8)
    # The arm may only widen a target, never narrow one.
    assert gross >= net


def test_gross_never_narrows_and_refuses_an_unnamed_stage() -> None:
    wide_room = 99.0
    assert K.kelly_target_budget_usdc_v1(
        book_equity_usdc=300.0, stage="P1",
        capital_available_usdc=wide_room, basis="gross",
    ) == pytest.approx(wide_room)
    # A stage with no share of its own keeps the room it was handed rather
    # than helping itself to the whole 2%.
    assert K.kelly_target_budget_usdc_v1(
        book_equity_usdc=300.0, stage="",
        capital_available_usdc=2.0, basis="gross",
    ) == pytest.approx(2.0)


def test_a_full_size_p0_seat_stands_the_later_stages_down_under_net() -> None:
    """The defect the clause was written for, as an executable statement.

    P0 buys its 0.6 share; at P0-M the room converts to fewer shares than
    are already held on every leg, so the candidate that would add to the
    basket is never offered.
    """

    p0 = _kelly_candidate(basis="net", held=0.0, capital=3.60, stage="P0")
    assert p0 is not None
    opened = p0["shares_by_bucket"]["20°C"]
    assert opened > 0.0

    # Same chain, next stage: 2% x 0.8 minus what P0 spent.
    room = 300.0 * 0.02 * 0.8 - sum(p0["cost_by_bucket"].values())
    assert 0.0 < room < sum(p0["cost_by_bucket"].values())
    assert _kelly_candidate(
        basis="net", held=opened, capital=room, stage="P0-M",
    ) is None


def test_gross_opens_the_valve_on_that_same_chain() -> None:
    p0 = _kelly_candidate(basis="net", held=0.0, capital=3.60, stage="P0")
    opened = p0["shares_by_bucket"]["20°C"]
    spent = sum(p0["cost_by_bucket"].values())
    room = 300.0 * 0.02 * 0.8 - spent

    topped = _kelly_candidate(
        basis="gross", held=opened, capital=room, stage="P0-M",
    )
    assert topped is not None, "charter 137's whole point"
    # It adds to every leg -- the basket stays complete (charter 72's
    # atomicity, which this clause does not touch).
    for bucket in ("20°C", "21°C"):
        assert topped["shares_by_bucket"][bucket] > opened
    # And it does not spend past the room: the chain's total outlay stays
    # inside 2% x stage share, which is the bound the clause promised not
    # to move.
    new_cash = sum(topped["cost_by_bucket"].values()) - spent
    assert new_cash <= room + 1e-9


def test_the_cash_bound_binds_when_the_level_wants_more_than_the_room(
) -> None:
    """A room far under the gross allowance scales the whole basket, and
    scales it rather than dropping a leg."""

    p0 = _kelly_candidate(basis="net", held=0.0, capital=3.60, stage="P0")
    opened = p0["shares_by_bucket"]["20°C"]
    spent = sum(p0["cost_by_bucket"].values())

    pinched = _kelly_candidate(
        basis="gross", held=opened, capital=0.20, stage="P1",
    )
    assert pinched is not None
    new_cash = sum(pinched["cost_by_bucket"].values()) - spent
    assert new_cash <= 0.20 + 1e-9
    for bucket in ("20°C", "21°C"):
        assert pinched["shares_by_bucket"][bucket] > opened


def test_both_lanes_read_one_definition() -> None:
    """Production and the ledger import the same function object."""

    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "_ledger_charter_137", ROOT / "scripts" / "paper_trading_ledger.py"
    )
    ledger = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ledger
    try:
        spec.loader.exec_module(ledger)
        assert ledger.kelly_target_budget_usdc_v1 is (
            K.kelly_target_budget_usdc_v1
        )
        assert PROD.kelly_target_budget_usdc_v1 is (
            K.kelly_target_budget_usdc_v1
        )
        assert ledger.KELLY_TOPUP_BASIS == K.KELLY_TOPUP_BASIS
        assert PROD.KELLY_TOPUP_BASIS == K.KELLY_TOPUP_BASIS
        # The arm is hashed only away from the pre-clause book, so the
        # standing digest is untouched while it is off.
        identity = ledger._SELECTION_RULE_IDENTITY
        assert ("kelly_topup_basis" in identity) == (
            ledger.KELLY_TOPUP_BASIS != "net"
        )
    finally:
        sys.modules.pop(spec.name, None)


def test_the_arm_gets_its_own_ledger_fingerprint() -> None:
    """Turning the arm on must re-derive the book, not share its digest.

    Measured by importing the ledger twice in two processes rather than by
    reading the source: the condition that matters is what the digest does,
    not how the dict is spelled.
    """

    read = (
        "import json,sys;"
        "sys.path.insert(0,%r);"
        "import importlib.util as u;"
        "s=u.spec_from_file_location('lg',%r);"
        "m=u.module_from_spec(s);s.loader.exec_module(m);"
        "print(m.SELECTION_RULE_SHA256)"
        % (str(ROOT), str(ROOT / "scripts" / "paper_trading_ledger.py"))
    )
    env = dict(os.environ)
    env.pop("PM_KELLY_TOPUP_BASIS", None)
    shipped = subprocess.run(
        [sys.executable, "-c", read], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=600,
    )
    assert shipped.returncode == 0, shipped.stderr[-2000:]
    armed = subprocess.run(
        [sys.executable, "-c", read], cwd=ROOT,
        env=dict(env, PM_KELLY_TOPUP_BASIS="gross"),
        capture_output=True, text=True, timeout=600,
    )
    assert armed.returncode == 0, armed.stderr[-2000:]
    assert shipped.stdout.strip() != armed.stdout.strip(), (
        "the gross arm shares the standing book's fingerprint, so the "
        "charter-129 re-import would never fire for it"
    )
