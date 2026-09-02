"""The hazard chain the phase and remaining-warming layers are fitted as.

Two of these pin bugs this fit actually shipped with before it was read
against the artifact it has to reproduce.  Both were silent: one published a
flat rate for the whole clock, the other published one half for every bin and
reported success.  A fit that fails loudly costs an afternoon; a fit that
fails quietly costs whatever the book does next.
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

from scripts.fit_metar_phase_hazard_artifact import (  # noqa: E402
    FULL_FEATURES,
    PHASE_FEATURES,
    fit_continuation,
    fit_logistic,
    fit_phase_rung,
    fleet_means,
    hours_bin,
    isotonic_increasing,
)


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
    fitted = fit_phase_rung(samples, fallbacks)
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


def test_the_phase_rung_reads_no_model_quantity() -> None:
    """Rung one is model-free by contract, so the curve cannot reach it.

    Two sample sets identical but for the model's shape must fit the same
    rung: if the shape ever leaks into this layer, the split it drives would
    carry the root's own belief and the ceiling's replacement would stop
    being an independent statement about the clock.
    """

    base = [
        _sample(remaining_hours=hours, delta_c=delta, shape=0.2)
        for hours, delta in ((1.0, 0), (9.0, 1), (12.0, 4)) * 40
    ]
    shifted = [dict(row, curve_shape_realized=0.95) for row in base]
    left = fit_phase_rung(base, fleet_means(base, FULL_FEATURES))
    right = fit_phase_rung(shifted, fleet_means(shifted, FULL_FEATURES))
    assert left["coefficients"] == right["coefficients"]
    assert left["bin_intercepts"] == right["bin_intercepts"]
    assert "curve_shape_realized" not in left["coefficients"]


def test_the_two_continuation_fits_differ_only_by_the_extra_evidence() -> None:
    """The likelihood ratio is an increment, so its denominator is a subset.

    The phase-only twin carries exactly the covariates rung one is allowed;
    the full fit adds the physical state and the model's shape.  A denominator
    fitted flat instead would price the clock twice -- once in the ratio and
    once in the split applied over it.
    """

    samples = [
        _sample(
            remaining_hours=6.0 + (index % 7),
            delta_c=1 + (index % 5),
            shape=0.1 * (index % 10),
            cloud=0.1 * (index % 9),
        )
        for index in range(400)
    ]
    fallbacks = fleet_means(samples, FULL_FEATURES)
    full = fit_continuation(samples, FULL_FEATURES, fallbacks)
    phase_only = fit_continuation(samples, PHASE_FEATURES, fallbacks)
    assert set(phase_only["coefficients"]) < set(full["coefficients"])
    assert set(phase_only["coefficients"]) == set(PHASE_FEATURES)
    assert "curve_shape_realized" in full["coefficients"]
    assert full["row_n"] == phase_only["row_n"]
    assert full["rungs"] == phase_only["rungs"]
