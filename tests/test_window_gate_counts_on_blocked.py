"""A blocked compile names the gate that emptied the window list.

Until 2026-09-05 ``delta_v2_support_empty`` reached the ledger and the
production decision table as a bare name: 28 P0 station-days in
2026-09-02..06 and every blocked support_empty scope in production could
not be told apart from a solver fault, when what had happened was that a
structural gate threw out the seed window and the price gate refused every
other shape.  The solver now attaches the funnel to the error, both
recorders write it, and the ledger prints it in the gate vocabulary the
page already uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

AUDIT = {
    "enumerated_window_count": 9,
    "shortlisted_window_count": 0,
    "gapped_window_refused_count": 0,
    "center_refused_window_count": 2,
    "short_refused_window_count": 1,
    "shape_refused_window_count": 3,
    "market_lean_refused_window_count": 0,
    "leg_floor_refused_window_count": 0,
    "unpriced_refused_window_count": 0,
    "edge_refused_window_count": 3,
    "last_refusing_gate": "edge",
    "edge_best_refused_window": {
        "buckets": ["74-75°F", "76-77°F", "78-79°F"],
        "edge": -0.254,
        "win_probability": 0.506,
        "all_in_cost": 0.76,
        "friction": 0.0,
    },
}


def test_the_empty_list_error_carries_the_funnel() -> None:
    from bayes_basket_target.basket_kernel.contracts import ContractError
    from bayes_basket_target.delta_v2_runtime import (
        WINDOW_REFUSAL_FUNNEL,
        _solve_over_support_windows,
        window_gate_counts_v1,
    )

    counts = window_gate_counts_v1(AUDIT)
    assert set(counts["refused"]) == set(WINDOW_REFUSAL_FUNNEL)
    assert counts["refused"]["shape"] == 3
    assert counts["refused"]["edge"] == 3
    assert counts["enumerated"] == 9 and counts["shortlisted"] == 0
    assert counts["last_refusing_gate"] == "edge"
    assert counts["edge_best_refused_window"]["edge"] == pytest.approx(-0.254)
    # A missing or malformed audit still yields the full shape.
    empty = window_gate_counts_v1({})
    assert set(empty["refused"]) == set(WINDOW_REFUSAL_FUNNEL)
    assert empty["last_refusing_gate"] is None

    # The solver raises before it touches anything but the list and the
    # audit, and the error keeps its bare name for every string consumer.
    with pytest.raises(ContractError) as caught:
        _solve_over_support_windows(
            windows=[],
            window_audit=AUDIT,
            live_buckets=(),
            snapshot=None,
            probabilities={},
            shortfall=set(),
            breakthrough=set(),
            delta_policy=None,
            basket_policy=None,
            capital_available_usdc=0.0,
            objective_form="whole_basket_kelly_v1",
            bankroll_usdc=1.0,
        )
    assert str(caught.value) == "delta_v2_support_empty"
    assert caught.value.window_gate_counts == counts
    assert not hasattr(caught.value, "window_failures")


def test_the_unsolvable_list_error_carries_each_windows_failure() -> None:
    from bayes_basket_target.delta_v2_runtime import _support_window_error

    error = _support_window_error(
        "delta_v2_no_solvable_support_window",
        window_audit=AUDIT,
        failures=[{"buckets": ["a", "b"], "reason": "ContractError:x"}],
    )
    assert str(error) == "delta_v2_no_solvable_support_window"
    assert error.window_failures == [
        {"buckets": ["a", "b"], "reason": "ContractError:x"}
    ]
    assert error.window_gate_counts["refused"]["center"] == 2


def test_the_worker_records_the_funnel() -> None:
    # The worker used to read the funnel straight off the exception here.
    # That line was correct and the funnel still never arrived: the blocked
    # plan is re-raised as a fresh ContractError one hop earlier, so there
    # was nothing on the exception to read (2026-09-09: 0 of 1,808
    # blocked-scope rows carried a funnel).  Asserting the source text of
    # this end is what let that stand for four days, so the guarantee is
    # now asserted where it can actually fail -- across the hop, in
    # ``tests/test_scope_serving_and_blocked_funnel.py`` -- and this only
    # pins that the recorder still reads through the resolver.
    worker = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(
        encoding="utf-8"
    )
    assert '"window_gate_counts": _blocked_funnel[0],' in worker
    assert '"window_failures": _blocked_funnel[1],' in worker
    assert "_window_funnel_from(\n                        exc, candidate=candidate\n                    )" in worker

    runtime = (ROOT / "bayes_basket_target" / "delta_v2_runtime.py").read_text(
        encoding="utf-8"
    )
    # Neither empty-list raise is a bare ContractError any more.
    assert 'raise ContractError("delta_v2_no_solvable_support_window")' not in runtime
    assert runtime.count("raise _support_window_error(") == 2


