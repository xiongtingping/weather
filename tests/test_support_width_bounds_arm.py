"""Charter 50's width bounds, as the arm the centre-gate ablation pointed at.

An enumeration produces a median of one priced window and touches a median of
three buckets; widths are almost entirely three or four against bounds of
three and four.  So the bounds are not a filter on a wide domain, they are
most of the domain, and the centre gate had nothing left to refuse.

Unset the overrides are absent and the station policy's own pair is served,
which is what production runs.
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


def _runtime(**env):
    previous = {
        key: os.environ.get(key)
        for key in ("PM_SUPPORT_WIDTH_MIN", "PM_SUPPORT_WIDTH_MAX")
    }
    for key in previous:
        os.environ.pop(key, None)
    os.environ.update({k: str(v) for k, v in env.items() if v is not None})
    try:
        import bayes_basket_target.delta_v2_runtime as runtime  # noqa: PLC0415

        return importlib.reload(runtime)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_unset_serves_the_station_policys_own_pair() -> None:
    runtime = _runtime()
    assert runtime.SUPPORT_WIDTH_MIN_OVERRIDE is None
    assert runtime.SUPPORT_WIDTH_MAX_OVERRIDE is None
    assert runtime.support_width_bounds_v1(
        policy_minimum=3, policy_maximum=4
    ) == (3, 4)
    # A different policy is followed, not overridden by a remembered value.
    assert runtime.support_width_bounds_v1(
        policy_minimum=2, policy_maximum=5
    ) == (2, 5)
    _runtime()


def test_each_bound_can_be_moved_alone() -> None:
    assert _runtime(PM_SUPPORT_WIDTH_MIN=2).support_width_bounds_v1(
        policy_minimum=3, policy_maximum=4
    ) == (2, 4)
    assert _runtime(PM_SUPPORT_WIDTH_MAX=6).support_width_bounds_v1(
        policy_minimum=3, policy_maximum=4
    ) == (3, 6)
    _runtime()


def test_an_inverted_or_absurd_pair_is_refused() -> None:
    import pytest  # noqa: PLC0415

    with pytest.raises(RuntimeError):
        _runtime(PM_SUPPORT_WIDTH_MIN=5, PM_SUPPORT_WIDTH_MAX=3)
    for bad in (0, 9):
        with pytest.raises(RuntimeError):
            _runtime(PM_SUPPORT_WIDTH_MIN=bad)
    # And an override that inverts a policy pair is refused at the call,
    # not silently swapped.
    runtime = _runtime(PM_SUPPORT_WIDTH_MIN=4)
    with pytest.raises(RuntimeError):
        runtime.support_width_bounds_v1(policy_minimum=3, policy_maximum=3)
    _runtime()


def test_the_enumerator_is_handed_the_bounds_from_one_function() -> None:
    """Both sides must not come to be running two different pairs."""

    source = (
        ROOT / "bayes_basket_target" / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("_support_windows")
    ]
    assert calls, "the enumerator is called somewhere"
    # The production-judge call reads the helper rather than the policy
    # fields directly.
    judged = [c for c in calls if "basket_policy.fee_rate" in c]
    assert judged, calls
    for call in judged:
        assert "support_width_bounds_v1" in call
        assert "basket_policy.maximum_support_buckets" not in call.replace(
            "policy_maximum=basket_policy.maximum_support_buckets", ""
        )


def test_the_ledger_hashes_an_arm_and_not_the_default() -> None:
    import paper_trading_ledger  # noqa: PLC0415

    def _sha(**env) -> str:
        _runtime(**env)
        previous = {
            key: os.environ.get(key)
            for key in ("PM_SUPPORT_WIDTH_MIN", "PM_SUPPORT_WIDTH_MAX")
        }
        for key in previous:
            os.environ.pop(key, None)
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            return importlib.reload(
                paper_trading_ledger
            ).SELECTION_RULE_SHA256
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    default = _sha()
    assert default != _sha(PM_SUPPORT_WIDTH_MIN=2)
    assert default != _sha(PM_SUPPORT_WIDTH_MAX=6)
    assert _sha() == default
    _runtime()
    importlib.reload(paper_trading_ledger)
