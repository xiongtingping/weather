"""A gate that only counts its refusals cannot be priced afterwards.

market_lean was promoted on 2026-09-05 on the strength of a ledger replay,
and in its first four days refused 60 windows in production.  Every one of
those was a counter increment and nothing else: which windows, and whether
the truth landed inside them, was not recoverable.  Asked on 2026-09-08 what
effect the gate had had since promotion, the honest answer was "not
measurable" -- of 60 refusals only 3 could be reconstructed from what the
ledger happened to keep, and 3 is not a sample.

So the refusal is written down now.  The count stays exact; the list is
bounded, because a wide day enumerates thousands of windows.
"""

from __future__ import annotations

import inspect

import bayes_basket_target.delta_v2_runtime as R


def test_the_gate_records_the_window_it_refuses_not_only_the_count() -> None:
    source = inspect.getsource(R._support_windows)
    gate = source[source.index("The market-lean gate:"):]
    gate = gate[:gate.index("market_lean_shadow_refused += 1") + 40]
    # The four things needed to price a refusal after the fact: which rungs,
    # where the window sat, where the model sat, and what the book said.
    for field in (
        '"buckets": list(window)',
        '"window_mid_ordinal"',
        '"model_mean_ordinal"',
        '"lean"',
    ):
        assert field in gate, field
    # Shadow refusals are recorded too, flagged by whether they bound.  A
    # measurement arm that leaves no record measures nothing.
    assert '"enforced"' in gate


def test_the_record_is_bounded_and_the_count_is_not() -> None:
    """A truncated list must never be readable as the whole."""

    assert R.MARKET_LEAN_REFUSAL_RECORD_MAX >= 1
    source = inspect.getsource(R._support_windows)
    assert "len(market_lean_refused_windows) < MARKET_LEAN_REFUSAL_RECORD_MAX" in source
    # The counter is incremented outside the bound, so it stays exact.
    after = source[source.index("market_lean_refused_windows.append("):]
    assert "market_lean_refused += 1" in after


def test_the_refusals_reach_the_audit() -> None:
    source = inspect.getsource(R._support_windows)
    assert '"market_lean_refused_windows": market_lean_refused_windows' in source
