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
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _probe(mode: str | None) -> subprocess.CompletedProcess[str]:
    """Import the runtime with this mode set, in a FRESH interpreter.

    The mode is read at import, so it can only be exercised at import -- and
    ``importlib.reload`` is not that: it re-executes the module in place, so
    every module that already took a reference to one of its functions keeps
    the old object while anything imported afterwards gets the new one.  The
    charter-110 binding test ("production and the ledger read ONE ladder")
    then fails on identity for no reason but the order the suite ran in.
    """

    env = {**os.environ}
    if mode is None:
        env.pop("PM_CENTRE_CONTAIN_GATE", None)
    else:
        env["PM_CENTRE_CONTAIN_GATE"] = mode
    return subprocess.run(
        [
            sys.executable, "-c",
            "from bayes_basket_target import delta_v2_runtime as r;"
            "print(r.CENTRE_CONTAIN_GATE)",
        ],
        env=env, cwd=str(ROOT), capture_output=True, text=True, check=False,
    )


def test_the_default_is_the_charter_and_production_is_untouched() -> None:
    probe = _probe(None)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "on"


def test_an_unknown_mode_is_refused_and_there_is_no_off() -> None:
    for mode in ("off", "anything"):
        probe = _probe(mode)
        assert probe.returncode != 0, f"{mode} was admitted"
        assert "RuntimeError" in probe.stderr


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
    """The arm must move the ledger's rule hash, or a replay under it would
    be filed as if it had run the served rule."""

    def _sha(mode: str | None) -> str:
        env = {**os.environ}
        if mode is None:
            env.pop("PM_CENTRE_CONTAIN_GATE", None)
        else:
            env["PM_CENTRE_CONTAIN_GATE"] = mode
        # The ledger lives in scripts/, which only this suite puts on the
        # path; the child interpreter needs it too.
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), str(ROOT / "scripts"), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        probe = subprocess.run(
            [
                sys.executable, "-c",
                "import paper_trading_ledger as l; print(l.SELECTION_RULE_SHA256)",
            ],
            env=env, cwd=str(ROOT), capture_output=True, text=True, check=False,
        )
        assert probe.returncode == 0, probe.stderr
        return probe.stdout.strip()

    default = _sha(None)
    assert default != _sha("deflate_only")
    assert _sha(None) == default
