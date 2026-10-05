"""The board's one-station panel: shape statistics on a ladder (user 2026-10-05)."""

from __future__ import annotations

import math

from bayes_basket_target.station_day_board import bucket_of, ladder_axis, vector_stats


def _axis():
    ordinals = {"20°C or below": 0, "21°C": 1, "22°C": 2, "23°C or higher": 3}
    intervals = {"20°C or below": [None, 20.5], "21°C": [20.5, 21.5],
                 "22°C": [21.5, 22.5], "23°C or higher": [22.5, None]}
    return ladder_axis(ordinals, intervals)


def test_open_edges_sit_half_a_rung_inside() -> None:
    axis = _axis()
    assert [r["centre_c"] for r in axis] == [20.0, 21.0, 22.0, 23.0]
    assert bucket_of(axis, 21.49) == "21°C"
    assert bucket_of(axis, 30.0) == "23°C or higher"


def test_mean_variance_mode_and_spread() -> None:
    axis = _axis()
    s = vector_stats({"20°C or below": 0.1, "21°C": 0.4, "22°C": 0.4, "23°C or higher": 0.1}, axis)
    assert math.isclose(s["mean_c"], 21.5)
    assert math.isclose(s["var_c2"], 0.65, abs_tol=1e-9)
    # A tie goes to the lower rung, as the market mode does (charter 249).
    assert s["mode"] == "21°C" and s["mean_bucket"] == "22°C"
    assert s["interval80"] == ["20°C or below", "22°C"]
    assert math.isclose(s["effective_rungs"], 2 ** s["entropy_bits"], rel_tol=1e-2)


def test_unnormalised_vectors_are_normalised() -> None:
    s = vector_stats({"21°C": 2.0, "22°C": 2.0}, _axis())
    assert math.isclose(s["mode_p"], 0.5)
