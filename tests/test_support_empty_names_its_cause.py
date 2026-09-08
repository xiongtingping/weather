"""One verdict per cause: the three ways a support set can be empty.

Until 2026-09-08 all three raised ``delta_v2_support_empty``, and 495
production decisions carried that name without saying which had happened --
a book with no quoted bucket, an enumeration that refused every window, or a
caller that handed over no support at all.  Only the middle one publishes a
funnel audit, so the other two read as "every window lost on the number"
when no window had been enumerated at all.

The names are split; the behaviour is not.  All three are the objective
answering "nothing to buy here", so all three still absorb into a named HOLD
rather than surfacing as a contract breach.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from bayes_basket_target import delta_v2_runtime as P
from bayes_basket_target import runtime as R

ROOT = Path(__file__).resolve().parents[1]

SCOPE_UNQUOTED = "delta_v2_support_scope_unquoted"
DESIRED_EMPTY = "delta_v2_support_desired_empty"
ENUMERATION_EMPTY = "delta_v2_support_empty"


def test_each_cause_raises_under_its_own_name() -> None:
    windows = inspect.getsource(P._support_windows)
    assert 'raise ContractError("%s")' % SCOPE_UNQUOTED in windows
    # The enumeration's own name is not raised from here any more; the
    # docstrings around it still mention it, which is why this asserts on the
    # raise rather than on the text.
    assert 'raise ContractError("%s")' % ENUMERATION_EMPTY not in windows

    compile_source = inspect.getsource(P.compile_delta_v2_runtime_plan)
    assert 'raise ContractError("%s")' % DESIRED_EMPTY in compile_source
    assert 'raise ContractError("%s")' % ENUMERATION_EMPTY not in compile_source

    # The enumeration keeps the original name because it is the one that can
    # explain itself: it publishes the funnel that emptied the shortlist.
    solve = inspect.getsource(P._solve_over_support_windows)
    assert ENUMERATION_EMPTY in solve
    assert "window_audit=window_audit" in solve


def test_all_three_still_absorb_into_a_named_hold() -> None:
    """Splitting the name must not turn two of them into crashes."""

    absorbed = inspect.getsource(R._compile_target_plan)
    for name in (ENUMERATION_EMPTY, SCOPE_UNQUOTED, DESIRED_EMPTY):
        assert name in absorbed, name


def test_the_ledger_reads_all_three_as_a_funnel_refusal() -> None:
    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    branch = source.split('detail = "生产裁判求解失败：%s" % exc')[1][:400]
    for name in (ENUMERATION_EMPTY, SCOPE_UNQUOTED, DESIRED_EMPTY):
        assert name in branch, name


def test_the_three_names_are_distinct_and_none_embeds_a_parameter() -> None:
    """Charter 117(三)④: a blocker code carries no parameter."""

    names = {ENUMERATION_EMPTY, SCOPE_UNQUOTED, DESIRED_EMPTY}
    assert len(names) == 3
    assert all(":" not in name for name in names)
