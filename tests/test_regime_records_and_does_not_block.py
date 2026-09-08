"""Charter 52c: the regime gate records and does not enforce -- anywhere.

The 2026-08-16 ruling took direction away from this layer: weather state is
already inside p(t), and a regime layer voting on it a second time is the
double counting clause 43 forbids.  Worse, it read the direction it was
judging out of the plan it then overruled.

The ruling changed the call site but left two traces behind, both found on
2026-09-08:

* ``regime_direction_allowed`` survived as a parameter defaulting to True
  with two branches reading it, and nothing had passed it since 2026-08-16 --
  998 refusals before that date, zero in the 23 days after;
* the receipt was appended to ``blockers``, which made a thing that blocks
  nothing the second most common blocker in the book (4664 rows), so every
  count of "why did this day not trade" was wrong by that much.

Both are gone.  The disagreement is still recorded, in the audit that
already carried it.
"""

from __future__ import annotations

import inspect

from bayes_basket_target import delta_v2_runtime as P
from bayes_basket_target import runtime as R
from bayes_basket_target import regime_materialization as M


def test_nothing_can_pass_a_regime_direction_veto_any_more() -> None:
    compile_source = inspect.getsource(P.compile_delta_v2_runtime_plan)
    assert "regime_direction_allowed" not in compile_source
    assert "regime_direction_not_allowed" not in compile_source
    # And the parameter itself is gone from the signature.
    assert "regime_direction_allowed" not in inspect.signature(
        P.compile_delta_v2_runtime_plan
    ).parameters


def test_the_receipt_is_recorded_and_is_not_a_blocker() -> None:
    source = inspect.getsource(R._compile_target_plan)
    assert "regime_direction_recorded_not_enforced" not in source
    # The gate is still evaluated and still reported.
    assert "evaluate_regime_gate(" in source
    assert "regime_audit" in source


def test_the_audit_still_carries_the_disagreement_it_used_to_block_on() -> None:
    """Removing the blocker must not remove the fact."""

    audit_source = inspect.getsource(M)
    assert '"direction_allowed": allowed' in audit_source
    assert '"reasons": list(reasons)' in audit_source
    assert '"schema": "bayes_basket_regime_gate_v1"' in audit_source
