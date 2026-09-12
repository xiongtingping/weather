"""Charter 131: the seat is two to three rungs, by ruling, at every stage.

Charter 50's three-to-four and the ``PM_SUPPORT_WIDTH_MIN/MAX`` research
arms (2026-09-12 morning) were retired the same evening by charter 131.  What
is left to pin: one helper hands the bounds to both books, nothing else
does, and the environment no longer moves them.
"""
from __future__ import annotations

import ast
import importlib
import os
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
    previous = {k: os.environ.get(k) for k in ("PM_SUPPORT_WIDTH_MIN", "PM_SUPPORT_WIDTH_MAX")}
    os.environ.update({"PM_SUPPORT_WIDTH_MIN": "1", "PM_SUPPORT_WIDTH_MAX": "6"})
    try:
        assert importlib.reload(runtime).support_width_bounds_v1() == (2, 3)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(runtime)
    assert not hasattr(runtime, "SUPPORT_WIDTH_MIN_OVERRIDE")
    assert not hasattr(runtime, "OPENING_FALLBACK_MIN_BUCKETS")


def test_every_hand_off_of_the_bounds_reads_the_helper() -> None:
    """Both books must not come to be running two different pairs."""

    for module in (
        ROOT / "bayes_basket_target" / "delta_v2_runtime.py",
        ROOT / "scripts" / "paper_trading_ledger.py",
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


def test_the_ledger_hashes_the_shape_and_the_width_unconditionally() -> None:
    import paper_trading_ledger as L  # noqa: PLC0415

    identity = L._SELECTION_RULE_IDENTITY
    assert identity["seat_shape"] == "seat_shape_mean_anchored_v1"
    assert identity["seat_width"] == "2..3"
    assert identity["market_lean_gate"] == "shadow"
    assert "support_width_min_override" not in identity
    assert "symmetric_seat" not in identity
