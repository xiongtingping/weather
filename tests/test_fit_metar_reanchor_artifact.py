"""The layer that carries today's observed forecast error to today's maximum.

This is the only layer permitted to read the model's level, and the tests that
matter are the ones that keep it honest about using it once: no intercept, a
bounded shift, and a slope that is zero when the reports and the curve agree.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.fit_metar_reanchor_artifact import (  # noqa: E402
    HOURS_EDGES,
    MAX_SHIFT_C,
    fit_slope,
    hours_bin,
)


def _row(bias: float, error: float, *, weight: float = 0.05) -> dict:
    """One sample: the curve ran ``bias`` cold, the root ``error`` cold."""

    return {
        "station_id": "TEST",
        "event_date": "2026-08-20",
        "root_bias_recent_c": bias,
        "root_center_c": 30.0,
        "truth_tmax_c": 30.0 + error,
        "station_day_weight": weight,
    }


def test_a_day_the_curve_got_right_moves_nothing() -> None:
    """No intercept: agreement is evidence of no bias, not evidence of a shift.

    A fitted constant would translate every root on every such day in whatever
    direction the training window happened to lean.
    """

    rows = [_row(0.0, error) for error in (-1.0, -0.5, 0.5, 1.5, 2.0)]
    fitted = fit_slope(rows)
    assert fitted["slope"] == 0.0
    assert fitted["residual_reduction"] == pytest.approx(0.0, abs=1e-9)


def test_the_slope_is_what_the_error_actually_tracks() -> None:
    """Half the observed bias carried forward is recovered as one half."""

    rows = [_row(bias, 0.5 * bias) for bias in (-2.0, -1.0, 1.0, 2.0, 3.0)]
    fitted = fit_slope(rows)
    assert fitted["slope"] == pytest.approx(0.5, abs=1e-9)
    assert fitted["residual_reduction"] == pytest.approx(1.0, abs=1e-9)
    assert fitted["root_rmse_after_c"] < 1e-6


def test_a_slope_that_buys_nothing_reports_that_it_buys_nothing() -> None:
    """The reduction is on the artifact so a useless slope is visible there.

    A bias uncorrelated with the root's error yields a slope near zero and a
    reduction near zero; the acceptance run reads this rather than the slope.
    """

    rows = [
        _row(bias, error)
        for bias, error in ((2.0, -1.0), (-2.0, 1.0), (2.0, 1.0), (-2.0, -1.0))
    ]
    fitted = fit_slope(rows)
    assert abs(fitted["slope"]) < 1e-9
    assert abs(fitted["residual_reduction"]) < 1e-9


def test_the_shift_is_capped_before_it_is_scored() -> None:
    """One bad report must not translate a whole prior.

    A twenty-degree disagreement is a station whose curve came from the wrong
    run or a report not yet quality-flagged, not an air mass.  The cap is
    applied inside the scoring too, so the published reduction is the one the
    capped shift actually earns.
    """

    rows = [_row(20.0, 20.0)] * 4
    fitted = fit_slope(rows)
    residual = 20.0 - MAX_SHIFT_C
    assert fitted["root_rmse_after_c"] == pytest.approx(residual, abs=1e-6)
    assert fitted["root_rmse_after_c"] > 0.0


def test_hours_bins_cover_the_whole_day_without_a_gap() -> None:
    assert hours_bin(0.0) == 0
    assert hours_bin(23.9) == len(HOURS_EDGES) - 2
    assert hours_bin(1e6) == len(HOURS_EDGES) - 2
    seen = {hours_bin(hour * 0.25) for hour in range(0, 4 * 24)}
    assert seen == set(range(len(HOURS_EDGES) - 1))
