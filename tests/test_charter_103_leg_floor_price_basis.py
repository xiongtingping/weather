"""Charter 103: the leg floor is read off the mid, not the offer.

User ruling 2026-09-07 ("地板改读中间价").  The floor asks what the market
thinks a bucket is worth; the ask is that question's upper bound, so on a
0.01/0.03 book a bucket worth two cents cleared a three-cent floor.
"""
from __future__ import annotations

from pathlib import Path

from bayes_basket_target.basket_kernel import delta_v2 as K


def test_the_two_sided_book_is_measured_on_its_mid() -> None:
    assert K.leg_floor_reference_price(0.03, 0.01) == 0.02
    assert K.leg_floor_reference_price(0.04, 0.01) == 0.025


def test_the_one_sided_book_keeps_the_ask() -> None:
    # 174 of the 850 quoted legs on 2026-09-07 had no bid at all.  Reading a
    # missing bid as zero would bar every one of them, which is a wider rule
    # than the one that was given.
    assert K.leg_floor_reference_price(0.03, 0.0) == 0.03
    assert K.leg_floor_reference_price(0.03, None) == 0.03


def test_the_old_basis_is_still_reachable_and_is_the_old_answer() -> None:
    assert K.leg_floor_reference_price(0.03, 0.01, basis="ask") == 0.03


def test_the_egll_rung_that_prompted_the_ruling_is_now_refused() -> None:
    # EGLC 2026-09-07 22C: bid 0.01, ask 0.03, belief 1.57%, floor 0.03.
    # The ask cleared the floor exactly; the mid does not.
    assert not K.leg_below_floor(0.03, 0.0157, 0.03, bid=0.01, basis="ask")
    assert K.leg_below_floor(0.03, 0.0157, 0.03, bid=0.01)


def test_belief_no_longer_lowers_the_floor_charter_131() -> None:
    # Charter 131 (Q8, 2026-09-12) retired charter 91's belief scaling: a
    # 61.5% claim faces the same three cents as a tail claim, and a
    # 0.01/0.03 book's mid (0.02) is under it.
    assert K.leg_below_floor(0.03, 0.615, 0.03, bid=0.01)
    assert not K.leg_below_floor(0.05, 0.615, 0.03, bid=0.03)


def test_the_plan_receipt_carries_the_floor_it_screened_with() -> None:
    """A rule nobody can read off a receipt is a rule nobody can audit.

    Measured 2026-09-07: 0 of the 400 most recent preselection plans carried
    ``leg_floor_rule``, and ``window_gate_counts`` was null in all 253 rows
    that had the key -- so charter 91's belief scaling and charter 103's
    price basis were both unprovable on the production side while the
    ledger's fingerprint recorded them.
    """

    root = Path(__file__).resolve().parents[1]
    runtime = (root / "bayes_basket_target" / "delta_v2_runtime.py").read_text(
        encoding="utf-8"
    )
    # The persisted audit -- the one the plan stores -- names the floor.
    marker = '"schema": "delta_basket_score_authorization_v2",'
    assert marker in runtime
    persisted = runtime[runtime.index(marker):]
    persisted = persisted[: persisted.index('delta_v2_audit["content_sha256"]')]
    assert '"price_basis": LEG_FLOOR_PRICE_BASIS,' in persisted
    assert '"rule": LEG_FLOOR_RULE_VERSION,' in persisted
    assert '"refused_window_count"' in persisted
    assert '"window_gate_counts": window_gate_counts_v1(' in persisted


def test_a_support_empty_hold_records_which_gate_emptied_the_list() -> None:
    """The funnel reaches the stored plan on the path that needs it most.

    ``window_gate_counts_v1`` was added 2026-09-05 so a blocked compile
    would record more than the error's name.  Measured 2026-09-07 it never
    had: all 253 rows carrying the key had it null, a support_empty scope
    wrote no decision row at all, and three of the eight compiles in the
    03:46Z batch blocked this way with nothing recorded about why.
    """

    root = Path(__file__).resolve().parents[1]
    runtime = (root / "bayes_basket_target" / "runtime.py").read_text(
        encoding="utf-8"
    )
    assert 'window_gate_counts=getattr(exc, "window_gate_counts", None),' in runtime
    assert 'delta_audit["window_gate_counts"] = dict(window_gate_counts)' in runtime
