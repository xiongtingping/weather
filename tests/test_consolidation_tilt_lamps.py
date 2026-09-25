"""Which lamp cuts the stake, and the evidence it was decided on.

The consolidation tilt cuts the stake to a twentieth when the market has
finished pricing the day.  Two lamps could trigger it, and until 2026-09-08
either one did.  Read on the new production ledger, 264 settled positions
split by lamp: ``sharp`` lit returned +8.7% -- the only positive group in the
book -- and ``stable`` lit returned -8.6%.  Cutting the stake on the one
group that made money is self-harm, so the trigger is ``stable`` alone.

This file pins the decision AND its evidence grade, because the grade is
weak and a later reader must not mistake it for a measured result.

The tilt itself was retired as the default on 2026-09-16 (charter 139); the
lamp ruling here still decides which lamp the research arm reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

import bayes_basket_target.delta_v2_runtime as R

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def test_only_stable_cuts_the_stake() -> None:
    assert R.CONSOLIDATION_TILT_SIGNALS == ("stable",)
    assert "sharp" not in R.CONSOLIDATION_TILT_SIGNALS


def test_sharp_remains_reachable_as_a_research_arm() -> None:
    """The old trigger is not deleted; it is no longer the default."""

    source = (
        ROOT / "bayes_basket_target" / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")
    assert "PM_CONSOLIDATION_TILT_SIGNALS" in source
    assert 'name.strip() in ("stable", "sharp")' in source


def test_the_evidence_grade_is_recorded_next_to_the_default() -> None:
    """The ruling was made on an observational split, not on an A/B.

    Lamp-lit days are days the market had already priced, which are easier
    days regardless of the tilt, and the arithmetic counterfactual could not
    settle it either -- the un-tilted arms deploy 3815-7717 USDC against a
    399 USDC book.  Only the sign survives.  Whoever revisits this must find
    that written beside the constant, not have to rediscover it.
    """

    source = (
        ROOT / "bayes_basket_target" / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")
    block = source[source.index("两盏灯改为只看 stable"):]
    block = block[:block.index("CONSOLIDATION_TILT_SIGNALS")]
    assert "观察性" in block
    assert "不是 A/B" in block
    assert "399" in block
    assert "证据不足" in block
