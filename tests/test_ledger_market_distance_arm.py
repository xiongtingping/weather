"""Arm G: refuse a leg the book's own centre has ruled out.

The rule under test is one line -- how many rungs from the highest-mid
bucket a leg may sit -- but it is the line that separates two populations
the earlier reading had mixed together.  Measured over 45,393 two-sided
quotes and 625,246 shadow orders (see
docs/research/VENUE_PRICE_CALIBRATION_2026-09-08.md), a cheap leg adjacent
to the book's centre returns -21.7% bought at the ask and the same price
three rungs out returns -66.7%.

The first test is the one that matters most: with the arm off, which is the
default, nothing this file adds may change a recorded row.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as ledger  # noqa: E402


def _row(bucket, ordinal, bid, ask, *, ask_available=True):
    return SimpleNamespace(
        bucket=bucket, ordinal=ordinal, bid_price=bid, ask_price=ask,
        ask_available=ask_available, price_tick_size=0.01,
    )


def _snapshot(*rows):
    return SimpleNamespace(buckets=list(rows))


@pytest.fixture
def armed(monkeypatch):
    """Turn the arm on at N rungs for one test, module constant and all."""

    def _arm(rungs: int):
        monkeypatch.setattr(ledger, "MARKET_DISTANCE_MAX", int(rungs))
    return _arm


def test_default_is_off_and_changes_nothing():
    """The shipped default has to leave every recorded row alone."""

    assert ledger.MARKET_DISTANCE_MAX == 0
    # Off, the predicate is False for every reading including absurd ones,
    # so no call site can behave differently from before it existed.
    for ordinal, centre in ((0, 0), (0, 99), (99, 0), (None, 3), (3, None)):
        assert ledger._beyond_market_distance(ordinal, centre) is False


def test_centre_is_the_books_own_highest_mid():
    snapshot = _snapshot(
        _row("a", 0, 0.01, 0.03),
        _row("b", 1, 0.20, 0.24),
        _row("c", 2, 0.48, 0.52),   # highest mid
        _row("d", 3, 0.30, 0.34),
    )
    assert ledger._market_centre_ordinal(snapshot) == 2


def test_one_sided_rungs_cannot_be_the_centre():
    """A rung with no bid has no mid, and an unquoted side is not an opinion.

    Left in, the deepest out-of-the-money rung -- the one quoted 0.001/0.90
    by nobody in particular -- would win the max and drag the reference to
    the far end of the ladder, which is the exact failure this gate exists
    to prevent.
    """

    snapshot = _snapshot(
        _row("a", 0, 0.0, 0.90),                      # no bid
        _row("b", 1, 0.48, 0.52),                     # the real centre
        _row("c", 2, 0.60, 0.64, ask_available=False),  # no ask
        _row("d", 3, 0.30, 0.30),                     # crossed/degenerate
    )
    assert ledger._market_centre_ordinal(snapshot) == 1


def test_no_two_sided_quote_leaves_no_centre():
    snapshot = _snapshot(_row("a", 0, 0.0, 0.90), _row("b", 1, 0.4, 0.4))
    assert ledger._market_centre_ordinal(snapshot) is None


def test_gate_admits_inside_and_refuses_outside(armed):
    armed(2)
    for distance in (0, 1, 2):
        assert ledger._beyond_market_distance(5 + distance, 5) is False
        assert ledger._beyond_market_distance(5 - distance, 5) is False
    for distance in (3, 4, 7):
        assert ledger._beyond_market_distance(5 + distance, 5) is True
        assert ledger._beyond_market_distance(5 - distance, 5) is True


def test_gate_fails_open_when_the_reading_is_missing(armed):
    """A book with no centre is a liquidity fact, not a distance fact.

    Refusing there would make the arm screen out thin books under a name
    that promises to screen out far rungs, and the two would then be
    impossible to tell apart in the result.
    """

    armed(2)
    assert ledger._beyond_market_distance(None, 5) is False
    assert ledger._beyond_market_distance(9, None) is False


def test_leg_floor_refuses_a_far_leg_that_price_alone_would_admit(armed):
    """The gate has to bind at the ledger's single leg-screen read point.

    Same price, same belief, same book -- only the distance differs, and
    that is what decides.  A leg priced well inside every other floor is
    still refused three rungs out.
    """

    kwargs = dict(ask=0.45, bid=0.43, probability=0.5, fee_rate=0.0)
    assert ledger._leg_below_floor(**kwargs) is False

    armed(2)
    assert ledger._leg_below_floor(
        **kwargs, ordinal=7, market_centre_ordinal=5) is False
    assert ledger._leg_below_floor(
        **kwargs, ordinal=8, market_centre_ordinal=5) is True


def test_the_arm_is_in_the_rule_fingerprint():
    """An arm that does not change the fingerprint corrupts the book.

    Rows recorded under a different gate describe a different strategy; if
    they share a fingerprint with production's rows they get pooled, and no
    later reader can separate them.
    """

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text()
    assert '"market_distance_max": MARKET_DISTANCE_MAX,' in source


def test_reimport_reads_the_environment(monkeypatch):
    monkeypatch.setenv("PAPER_LEDGER_MARKET_DISTANCE_MAX", "2")
    reloaded = importlib.reload(ledger)
    try:
        assert reloaded.MARKET_DISTANCE_MAX == 2
        assert reloaded._beyond_market_distance(8, 5) is True
        assert reloaded._beyond_market_distance(7, 5) is False
    finally:
        monkeypatch.delenv("PAPER_LEDGER_MARKET_DISTANCE_MAX", raising=False)
        importlib.reload(ledger)
