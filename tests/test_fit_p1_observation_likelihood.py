"""The estimator behind P1's joint observation likelihood, and its alpha choice.

The ordered hazard chain moved here from the retired four-layer fitter
(charter 150).  Two of these pin bugs that chain actually shipped with, both
silent: one published a flat rate for the whole clock, the other published
one half for every bin and reported success.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import p1_observation_likelihood as OL  # noqa: E402
from scripts.fit_p1_observation_likelihood import (  # noqa: E402
    ALPHA_GRID,
    _log_score,
    fit_logistic,
    fit_phase_rung,
    fleet_means,
    hours_bin,
    isotonic_increasing,
)

FULL_FEATURES = OL.FULL_FEATURES
PHASE_FEATURES = OL.PHASE_FEATURES


def _sample(
    *,
    remaining_hours: float,
    delta_c: int,
    drawdown: float = 0.0,
    cloud: float = 0.3,
    momentum: float = 0.0,
    precipitation: int = 0,
    weight: float = 0.05,
    shape: float | None = 0.5,
) -> dict:
    return {
        "station_id": "TEST",
        "event_date": "2026-08-20",
        "remaining_hours": remaining_hours,
        "hours_since_high_refresh": 1.0,
        "drawdown_from_high_c": drawdown,
        "momentum_c_per_hour": momentum,
        "cloud_cover_fraction": cloud,
        "precipitation_present": precipitation,
        "dewpoint_depression_c": 8.0,
        "wind_speed_mps": 3.0,
        "warming_so_far_c": 4.0,
        "curve_shape_realized": shape,
        "root_bias_recent_c": 0.0,
        "regional_headroom_c": None,
        "regional_live_gap_c": None,
        "delta_c": delta_c,
        "station_day_weight": weight,
    }


def test_the_hours_projection_rises_with_the_hours_the_day_has_left() -> None:
    """The bins run from least hours left to most, so the rate rises with them.

    Written the other way round -- which reads correctly in English as "the
    rate falls as the day runs out" -- pool-adjacent-violators collapses every
    bin into a single block, and the fit publishes one rate for the whole
    clock.  That is what the first version did: a flat 0.648 where the ceiling
    artifact publishes 0.002 through 0.929.
    """

    assert isotonic_increasing([1.0, 2.0, 3.0], [1.0, 1.0, 1.0]) == [
        1.0, 2.0, 3.0
    ]
    assert isotonic_increasing([1.0, 3.0, 2.0], [1.0, 1.0, 1.0]) == [
        1.0, 2.5, 2.5
    ]
    # A fully reversed input pools to its own mean rather than to its first
    # value, and keeps its length.
    projected = isotonic_increasing([3.0, 2.0, 1.0], [1.0, 1.0, 1.0])
    assert projected == [2.0, 2.0, 2.0]
    assert len(isotonic_increasing([5.0, 1.0, 4.0, 2.0], [1.0] * 4)) == 4


def test_a_singular_design_is_refused_rather_than_returned_as_zeros() -> None:
    """An unoccupied bin makes an all-zero column; the solve must not swallow it.

    Returning the zero vector on a failed Newton step publishes an artifact
    whose every rate is one half, with a success line on stdout.
    """

    design = np.array([[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]])
    outcome = np.array([1.0, 0.0, 1.0])
    weight = np.array([1.0, 1.0, 1.0])
    with pytest.raises(SystemExit, match="design_singular"):
        fit_logistic(
            design, outcome, weight, penalise=np.array([0.0, 0.0])
        )


def test_the_phase_rung_recovers_a_rate_it_was_shown() -> None:
    """Given a clean separation by hours left, the fitted bins reproduce it."""

    samples = []
    for _ in range(60):
        samples.append(_sample(remaining_hours=1.0, delta_c=0))
        samples.append(_sample(remaining_hours=12.0, delta_c=3))
    for _ in range(15):
        samples.append(_sample(remaining_hours=7.0, delta_c=2))
    for _ in range(45):
        samples.append(_sample(remaining_hours=7.0, delta_c=0))
    fallbacks = fleet_means(samples, FULL_FEATURES)
    fitted = fit_phase_rung(samples, fallbacks, PHASE_FEATURES)
    shift = sum(
        fitted["coefficients"][name] * fallbacks[name]
        for name in fitted["coefficients"]
    )

    def rate(hours: float) -> float:
        intercept = fitted["bin_intercepts"][hours_bin(hours)]
        return 1.0 / (1.0 + math.exp(-(intercept + shift)))

    assert rate(1.0) < 0.1
    assert 0.1 < rate(7.0) < 0.5
    assert rate(12.0) > 0.9
    # And the published vector is monotone in the hours the day has left.
    assert fitted["bin_intercepts"] == sorted(fitted["bin_intercepts"])


def test_the_alpha_grid_reaches_zero_and_an_alpha_of_zero_is_the_root() -> None:
    """Zero is on the grid, so the refit can always choose to say nothing,
    and at zero the tempered likelihood is exactly the root under the mask."""

    assert ALPHA_GRID[0] == 0.0
    prior = {"a": 0.2, "b": 0.5, "c": 0.3}
    evidence = {"a": 0.9, "b": 0.1, "c": 0.01}
    assert _log_score(prior, evidence, {"a"}, "b", 0.0) == pytest.approx(
        _log_score(prior, None, {"a"}, "b", 1.0)
    )
    # And a likelihood that points away from the truth costs more the more
    # it is believed.
    assert _log_score(prior, evidence, set(), "c", 2.0) < _log_score(
        prior, evidence, set(), "c", 0.5
    )
