"""Charter 131: the seat is two to three rungs, by ruling, at every stage.

Charter 50's three-to-four and the ``PM_SUPPORT_WIDTH_MIN/MAX`` research
arms (2026-09-12 morning) were retired the same evening by charter 131.  What
is left to pin: one helper hands the bounds to both books, nothing else
does, and the environment no longer moves them.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def test_the_bounds_are_the_rulings_and_the_environment_cannot_move_them() -> None:
    import bayes_basket_target.delta_v2_runtime as runtime  # noqa: PLC0415

    assert (runtime.SEAT_MIN_BUCKETS, runtime.SEAT_MAX_BUCKETS) == (2, 3)
    assert runtime.support_width_bounds_v1() == (2, 3)
    assert runtime.CHAIN_MAX_BUCKETS == 4
    # The retired knobs are read at import, so proving they no longer move
    # anything means importing with them set -- in a FRESH interpreter, not
    # by reloading this one.  ``importlib.reload`` re-executes the module in
    # place: every module that already did ``from .delta_v2_runtime import
    # bid_anchored_ladder`` keeps the old function object while anything
    # imported afterwards gets the new one, and the charter-110 binding test
    # ("production and the ledger read ONE ladder") then fails on identity
    # depending only on which tests ran first.
    probe = subprocess.run(
        [
            sys.executable, "-c",
            "from bayes_basket_target import delta_v2_runtime as r;"
            "print(r.support_width_bounds_v1());"
            "assert not hasattr(r, 'SUPPORT_WIDTH_MIN_OVERRIDE');"
            "assert not hasattr(r, 'OPENING_FALLBACK_MIN_BUCKETS')",
        ],
        env={**os.environ, "PM_SUPPORT_WIDTH_MIN": "1", "PM_SUPPORT_WIDTH_MAX": "6"},
        cwd=str(ROOT), capture_output=True, text=True, check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "(2, 3)"
    assert not hasattr(runtime, "SUPPORT_WIDTH_MIN_OVERRIDE")
    assert not hasattr(runtime, "OPENING_FALLBACK_MIN_BUCKETS")


def test_every_hand_off_of_the_bounds_reads_the_helper() -> None:
    """Production must not come to be running two different pairs."""

    for module in (
        ROOT / "bayes_basket_target" / "delta_v2_runtime.py",
    ):
        source = module.read_text(encoding="utf-8")
        hand_offs = [
            ast.unparse(node) for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and "minimum_support_buckets=" in ast.unparse(node)
            and not ast.unparse(node.func).endswith("_support_windows")
            or (
                isinstance(node, ast.Call)
                and ast.unparse(node.func).endswith("_support_windows")
            )
        ]
        assert hand_offs, module.name
        for call in hand_offs:
            if "minimum_support_buckets=" not in call:
                continue
            assert "support_width_bounds_v1()" in call, (
                "%s hands the enumerator bounds without the helper: %s"
                % (module.name, call[:160])
            )
            assert "policy_minimum=" not in call and "basket_policy.minimum_support_buckets" not in call


