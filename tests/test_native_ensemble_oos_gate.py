"""The cross-system OOS gate and its Brier tolerance channel.

Brier only scores the bucket that settled, so a candidate that is sharper
across the whole distribution can lose it by a rounding-scale margin while
winning CRPS and NLL outright.  The tolerance path admits exactly that shape
and nothing else: over the rejected history it lets through about 6% of
candidates, and every candidate that already passed strictly still does.
"""

from __future__ import annotations

from bayes_basket_target.native_ensemble_oos import (
    BRIER_TOLERANCE_MAX_RELATIVE_LOSS,
    BRIER_TOLERANCE_MIN_RELATIVE_GAIN,
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


def test_a_hair_of_brier_loss_passes_when_crps_and_nll_win_clearly() -> None:
    # Brier loses 1.25%, CRPS wins 12.5%, NLL wins 10%: the KDAL/KHOU shape.
    gate = _cross_oos_gate(
        _cross(brier=0.81, crps=0.70, nll=1.80),
        CLIMATE,
    )

    assert gate["pass"] is True
    assert gate["path"] == "CRPS_NLL_TOLERANCE"
    assert gate["brier_relative_loss"] <= BRIER_TOLERANCE_MAX_RELATIVE_LOSS
    assert gate["crps_relative_gain"] >= BRIER_TOLERANCE_MIN_RELATIVE_GAIN
    assert gate["nll_relative_gain"] >= BRIER_TOLERANCE_MIN_RELATIVE_GAIN


def test_losing_brier_by_more_than_the_tolerance_stays_rejected() -> None:
    # Brier loses 5%, well past the 2% the channel allows, even though both
    # other rules win by a wide margin.
    gate = _cross_oos_gate(
        _cross(brier=0.84, crps=0.60, nll=1.50),
        CLIMATE,
    )

    assert gate["pass"] is False
    assert gate["path"] == "REJECTED"


def test_a_thin_crps_win_does_not_buy_a_brier_loss() -> None:
    # CRPS wins only 1.25%, short of the 5% the channel demands.
    gate = _cross_oos_gate(
        _cross(brier=0.81, crps=0.79, nll=1.80),
        CLIMATE,
    )

    assert gate["pass"] is False
    assert gate["path"] == "REJECTED"


def test_losing_nll_keeps_a_brier_loss_rejected() -> None:
    # NLL is the discriminator that matters here: across the real rejected
    # history it alone rejects half of what Brier-plus-CRPS would admit.
    gate = _cross_oos_gate(
        _cross(brier=0.81, crps=0.70, nll=2.10),
        CLIMATE,
    )

    assert gate["pass"] is False
    assert gate["path"] == "REJECTED"


def test_tolerance_is_unavailable_against_a_zero_baseline() -> None:
    gate = _cross_oos_gate(
        _cross(brier=0.10, crps=0.10, nll=0.10),
        {"brier": 0.0, "crps": 0.0, "nll": 0.0},
    )

    assert gate["pass"] is False
    assert gate["tolerance_available"] is False


def test_a_strict_pass_is_never_reclassified_by_the_tolerance_channel() -> None:
    # Winning Brier while losing CRPS is not a strict pass, and the channel
    # must not rescue it either -- it only ever forgives Brier.
    gate = _cross_oos_gate(
        _cross(brier=0.70, crps=0.85, nll=1.50),
        CLIMATE,
    )

    assert gate["pass"] is False
    assert gate["path"] == "REJECTED"
