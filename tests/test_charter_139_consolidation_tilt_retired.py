"""Charter 139: the consolidation tilt is retired, and the arm still runs.

The tilt cut a chain's capital to a twentieth when the P0 opener's ``stable``
lamp was lit.  Re-read on 2026-08-31..09-15 production decisions it cut the
half of the book that loses less per dollar (lit -3.1% against unlit -10.3%),
and the venue-minimum floor had already eaten it in production (48 of 428 lit
P0 trades lost any capital).  The default is now the identity on both lanes;
the lamps are still computed and recorded, and 0.05 reproduces the rule.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bayes_basket_target import delta_v2_runtime as R
from bayes_basket_target.delta_v2_runtime import compile_delta_v2_runtime_plan
from tests.test_bayes_basket_target_delta_v2_runtime import (
    _basket_policy,
    _delta_policy,
    _fill_resolver,
    _snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import paper_trading_ledger as L  # noqa: E402


def test_both_lanes_default_to_the_identity() -> None:
    assert R.CONSOLIDATION_TILT_FACTOR == 1.0
    assert L.CONSOLIDATION_TILT_FACTOR == R.CONSOLIDATION_TILT_FACTOR


def test_a_lit_lamp_no_longer_moves_the_capital(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixture's two-bucket book is sharp by construction; with both
    lamps counted one is lit, and the capital must come through untouched."""

    monkeypatch.setattr(R, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp"))
    plan = compile_delta_v2_runtime_plan(
        snapshot=_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )
    audit = plan["delta_v2_audit"]["consolidation_tilt"]
    # The lamps are still read and recorded -- annotation, not sizing.
    assert audit["signals_lit"]
    assert audit["basis"] == "p0_own_lights"
    assert audit["factor"] == 1.0
    assert audit["capital_before_usdc"] == 5.0
    assert audit["capital_after_usdc"] == 5.0
    assert audit["floor_usdc"] is None


def test_the_ledger_fingerprint_names_the_retirement() -> None:
    assert "固化减仓已退役(恒等)" in L.SELECTION_RULE_LABEL
    assert "固化≥" not in L.SELECTION_RULE_LABEL


def test_the_research_arm_reproduces_the_retired_rule() -> None:
    """0.05 on both switches is the rule as it ran until 2026-09-16."""

    code = (
        "import json, sys; sys.path.insert(0, 'scripts');"
        "import bayes_basket_target.delta_v2_runtime as R;"
        "import paper_trading_ledger as L;"
        "print(json.dumps([R.CONSOLIDATION_TILT_FACTOR,"
        " L.CONSOLIDATION_TILT_FACTOR, L.SELECTION_RULE_LABEL]))"
    )
    env = {
        **os.environ,
        "PM_CONSOLIDATION_TILT": "0.05",
        "PAPER_LEDGER_CONSOLIDATION_TILT": "0.05",
    }
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1]
    runtime_factor, ledger_factor, label = json.loads(out)
    assert runtime_factor == ledger_factor == 0.05
    assert "固化≥1减仓0.05·灯stable·钳交易所最小单×3腿" in label
    assert "固化减仓已退役" not in label
