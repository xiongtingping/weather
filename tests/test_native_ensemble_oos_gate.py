"""The cross-system OOS gate under charter 240's ranked verdict.

The model's target is the bucket distribution P(Y=k|X).  The log score is the
primary judge and must beat climatology; the ladder CRPS (structure) and the
bucket Brier (guardrail) may each lose to it by at most
``GUARDRAIL_MAX_RELATIVE_LOSS``.  No score holds an equal veto, and a
guardrail does not have to be paid for with a margin on the primary.
"""
from __future__ import annotations

import math

from bayes_basket_target.native_ensemble_oos import (
    GATE_SCORING_RULE,
    GUARDRAIL_MAX_RELATIVE_LOSS,
    _cross_oos_gate,
)

CLIMATE = {"brier": 0.80, "crps": 0.80, "nll": 2.00}


def _cross(*, brier: float, crps: float, nll: float) -> dict[str, float]:
    return {"brier": brier, "crps": crps, "nll": nll}


def test_beating_climatology_on_the_primary_with_guardrails_inside_passes() -> None:
    gate = _cross_oos_gate(_cross(brier=0.70, crps=0.70, nll=1.90), CLIMATE)
    assert gate["pass"] is True and gate["path"] == "LOGSCORE_PRIMARY"
    assert gate["scoring"] == GATE_SCORING_RULE == "LOGSCORE_PRIMARY_CRPS_BRIER_GUARD_V3"
    assert GUARDRAIL_MAX_RELATIVE_LOSS == 0.02
    assert gate["primary_pass"] and gate["crps_guard_pass"] and gate["brier_guard_pass"]


def test_a_thin_log_score_win_passes_when_the_guardrails_hold() -> None:
    # Charter 228/211 demanded a 5% NLL margin to excuse a Brier loss; under
    # charter 240 any primary win passes if the guardrails are inside 2%.
    gate = _cross_oos_gate(_cross(brier=0.81, crps=0.80, nll=1.98), CLIMATE)
    assert gate["pass"] is True
    assert gate["brier_relative_loss"] <= GUARDRAIL_MAX_RELATIVE_LOSS
    assert math.isclose(gate["nll_relative_gain"], 0.01)


def test_losing_the_primary_is_rejected_whatever_the_guardrails_say() -> None:
    gate = _cross_oos_gate(_cross(brier=0.50, crps=0.50, nll=2.01), CLIMATE)
    assert gate["pass"] is False and gate["path"] == "REJECTED_PRIMARY"


def test_a_crps_loss_beyond_the_margin_is_rejected_on_structure() -> None:
    gate = _cross_oos_gate(_cross(brier=0.70, crps=0.85, nll=1.50), CLIMATE)
    assert gate["pass"] is False and gate["path"] == "REJECTED_CRPS_GUARD"
    assert gate["crps_relative_loss"] > GUARDRAIL_MAX_RELATIVE_LOSS


def test_a_brier_loss_beyond_the_margin_is_rejected_on_the_guardrail() -> None:
    gate = _cross_oos_gate(_cross(brier=0.84, crps=0.60, nll=1.50), CLIMATE)
    assert gate["pass"] is False and gate["path"] == "REJECTED_BRIER_GUARD"


def test_against_a_zero_baseline_the_guardrails_cannot_exceed_it() -> None:
    gate = _cross_oos_gate(
        _cross(brier=0.10, crps=0.10, nll=0.10),
        {"brier": 0.0, "crps": 0.0, "nll": 0.0},
    )
    assert gate["pass"] is False and gate["path"] == "REJECTED_PRIMARY"
    assert gate["nll_relative_gain"] is None
    assert gate["crps_relative_loss"] is None and gate["brier_relative_loss"] is None
