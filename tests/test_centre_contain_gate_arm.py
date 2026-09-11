"""Charter 49's centre gate, as a switch that prices instead of refusing.

The gate is what narrows the domain: on the settled enumerations 92.5% of
station-days leave it with a single admissible window, and its refusals
outnumber everything else together.  So it is the thing to ablate, and the
seat-head rule people reach for first binds on 14 of 470 seats.

``deflate_only`` keeps the off-centre deflation and drops only the refusal,
because dropping both would be two changes at once and the deflation has its
own measurements.  There is no ``off``.
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _runtime(mode: str | None):
    previous = os.environ.get("PM_CENTRE_CONTAIN_GATE")
    if mode is None:
        os.environ.pop("PM_CENTRE_CONTAIN_GATE", None)
    else:
        os.environ["PM_CENTRE_CONTAIN_GATE"] = mode
    try:
        import bayes_basket_target.delta_v2_runtime as runtime  # noqa: PLC0415

        return importlib.reload(runtime)
    finally:
        if previous is None:
            os.environ.pop("PM_CENTRE_CONTAIN_GATE", None)
        else:
            os.environ["PM_CENTRE_CONTAIN_GATE"] = previous


def test_the_default_is_the_charter_and_production_is_untouched() -> None:
    assert _runtime(None).CENTRE_CONTAIN_GATE == "on"


def test_an_unknown_mode_is_refused_and_there_is_no_off() -> None:
    import pytest  # noqa: PLC0415

    for mode in ("off", "anything"):
        with pytest.raises(RuntimeError):
            _runtime(mode)
    _runtime(None)


def test_the_refusal_is_gated_and_the_deflation_is_not() -> None:
    """Read off the enumerator: one branch moves, the pricing does not.

    Asserted structurally because the alternative is a fixture that rebuilds
    a snapshot, a core, a ladder centre and a book -- and a fixture that
    large tends to pass for the wrong reason.
    """

    source = (
        ROOT / "bayes_basket_target" / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    windows = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_support_windows"
    )
    body = ast.unparse(windows)
    # The refusal now stands behind the switch.
    assert "if CENTRE_CONTAIN_GATE == 'on':" in body
    # The counter still counts every miss, switch or no switch, or the audit
    # would stop being able to say how often the gate was the binding rule.
    assert "center_refused += 1" in body
    # And the claim is still deflated for the distance to the centre.
    assert "window_claim_factor" in body
    assert "center_gap" in body
    # The deflation is not inside any centre-gate branch.
    for node in ast.walk(windows):
        if (
            isinstance(node, ast.If)
            and "CENTRE_CONTAIN_GATE" in ast.unparse(node.test)
        ):
            assert "window_claim_factor" not in ast.unparse(node)


def test_the_ledger_hashes_the_arm_and_not_the_default() -> None:
    import paper_trading_ledger  # noqa: PLC0415

    def _sha(mode: str | None) -> str:
        previous = os.environ.get("PM_CENTRE_CONTAIN_GATE")
        if mode is None:
            os.environ.pop("PM_CENTRE_CONTAIN_GATE", None)
        else:
            os.environ["PM_CENTRE_CONTAIN_GATE"] = mode
        try:
            _runtime(mode)
            return importlib.reload(paper_trading_ledger).SELECTION_RULE_SHA256
        finally:
            if previous is None:
                os.environ.pop("PM_CENTRE_CONTAIN_GATE", None)
            else:
                os.environ["PM_CENTRE_CONTAIN_GATE"] = previous

    default = _sha(None)
    assert default != _sha("deflate_only")
    assert _sha(None) == default
    _runtime(None)
    importlib.reload(paper_trading_ledger)
