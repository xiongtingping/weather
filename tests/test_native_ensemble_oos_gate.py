"""The cross-system OOS gate and its Brier tolerance channel.

Charter 211: the gate reads the two proper scores of the discrete bucket
distribution -- bucket Brier and the log score (NLL).  Brier scores only
the bucket that settled, so a candidate that is sharper across the whole
distribution can lose it by a rounding-scale margin while winning the log
score outright.  The tolerance path admits exactly that shape and nothing
looser; CRPS is recorded as a diagnostic and decides nothing.
"""
from __future__ import annotations

from bayes_basket_target.native_ensemble_oos import (
    BRIER_TOLERANCE_MAX_RELATIVE_LOSS,
    BRIER_TOLERANCE_MIN_RELATIVE_GAIN,
    GATE_SCORING_RULE,
    _cross_oos_gate,
)

CLIMATE = {"brier": 0.80, "crps": 0.80, "nll": 2.00}


def _cross(*, brier: float, crps: float, nll: float) -> dict[str, float]:
    return {"brier": brier, "crps": crps, "nll": nll}


def test_beating_climatology_on_both_rules_passes_strictly() -> None:
    gate = _cross_oos_gate(
        _cross(brier=0.70, crps=0.70, nll=1.90),
        CLIMATE,
    )
    assert gate["pass"] is True
    assert gate["path"] == "STRICT"
    assert gate["scoring"] == GATE_SCORING_RULE == "BRIER_LOGSCORE_V2"


def test_a_hair_of_brier_loss_passes_when_the_log_score_wins_clearly() -> None:
    # Brier loses 1.25%, NLL wins 10%: the KDAL/KHOU shape.
    gate = _cross_oos_gate(
        _cross(brier=0.81, crps=0.70, nll=1.80),
        CLIMATE,
    )
    assert gate["pass"] is True
    assert gate["path"] == "NLL_TOLERANCE"
    assert gate["brier_relative_loss"] <= BRIER_TOLERANCE_MAX_RELATIVE_LOSS
    assert gate["nll_relative_gain"] >= BRIER_TOLERANCE_MIN_RELATIVE_GAIN


def test_losing_brier_by_more_than_the_tolerance_stays_rejected() -> None:
    # Brier loses 5%: however good the log score, the channel is shut.
    gate = _cross_oos_gate(
        _cross(brier=0.84, crps=0.60, nll=1.50),
        CLIMATE,
    )
    assert gate["pass"] is False
    assert gate["path"] == "REJECTED"


def test_a_thin_log_score_win_does_not_buy_a_brier_loss() -> None:
    # NLL wins only 1%, short of the 5% the channel demands.
    gate = _cross_oos_gate(
        _cross(brier=0.81, crps=0.70, nll=1.98),
        CLIMATE,
    )
    assert gate["pass"] is False
    assert gate["path"] == "REJECTED"


def test_crps_decides_nothing_either_way() -> None:
    """Charter 211: a CRPS win cannot admit, a CRPS loss cannot reject."""

    # Same Brier loss, big CRPS win, log score short of the margin: rejected.
    lost = _cross_oos_gate(_cross(brier=0.81, crps=0.40, nll=1.98), CLIMATE)
    assert lost["pass"] is False
    # Same Brier loss, CRPS worse than climatology, log score wins: admitted.
    won = _cross_oos_gate(_cross(brier=0.81, crps=0.95, nll=1.80), CLIMATE)
    assert won["pass"] is True and won["path"] == "NLL_TOLERANCE"
    # Strict: Brier and NLL win while CRPS loses.
    strict = _cross_oos_gate(_cross(brier=0.70, crps=0.95, nll=1.50), CLIMATE)
    assert strict["path"] == "STRICT"
    # ... and the diagnostic is still on the record, with its sign.
    assert won["crps_relative_gain"] < 0 < lost["crps_relative_gain"]


def test_tolerance_is_unavailable_against_a_zero_baseline() -> None:
    gate = _cross_oos_gate(
        _cross(brier=0.10, crps=0.10, nll=0.10),
        {"brier": 0.0, "crps": 0.0, "nll": 0.0},
    )
    assert gate["pass"] is False
    assert gate["tolerance_available"] is False
    assert gate["crps_relative_gain"] is None


def test_a_strict_pass_is_never_reclassified_by_the_tolerance_channel() -> None:
    # Winning Brier while losing the log score is not a strict pass, and the
    # channel only ever opens for a Brier loss.
    gate = _cross_oos_gate(
        _cross(brier=0.70, crps=0.60, nll=2.10),
        CLIMATE,
    )
    assert gate["pass"] is False
    assert gate["path"] == "REJECTED"
