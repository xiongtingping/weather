"""The centre/width layer: what it must do, and what it must refuse to do."""

import math

import pytest

from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
    bucket_centers_from_intervals,
)


KEYS = ("29C or below", "30C", "31C", "32C", "33C or higher")
CENTERS = (29.0, 30.0, 31.0, 32.0, 33.0)


def _uniformish() -> dict[str, float]:
    return {
        "29C or below": 0.05,
        "30C": 0.20,
        "31C": 0.50,
        "32C": 0.20,
        "33C or higher": 0.05,
    }


def _mean(probability, keys=KEYS, centers=CENTERS) -> float:
    total = sum(probability.values())
    index = {key: position for position, key in enumerate(keys)}
    return sum(
        centers[index[key]] * value / total
        for key, value in probability.items()
    )


def _sigma(probability, keys=KEYS, centers=CENTERS) -> float:
    mean = _mean(probability, keys, centers)
    total = sum(probability.values())
    index = {key: position for position, key in enumerate(keys)}
    return math.sqrt(
        sum(
            value / total * (centers[index[key]] - mean) ** 2
            for key, value in probability.items()
        )
    )


def test_identity_leaves_the_distribution_alone() -> None:
    """Serving before a table is fitted must be byte-identical.

    The same property the band table has: a stage with no admitted
    correction has to produce exactly what it produced before this layer
    existed, or every content hash downstream moves for no reason.
    """

    source = _uniformish()
    adjusted, receipt = apply_center_width_calibration(
        source,
        delta_c=0.0,
        width_lambda=1.0,
        ordered_keys=KEYS,
        key_centers_c=CENTERS,
    )
    assert receipt["state"] == "IDENTITY"
    for key, value in source.items():
        assert adjusted[key] == pytest.approx(value, abs=1e-12)


def test_shift_moves_the_mean_by_that_many_degrees() -> None:
    """Exact on mass that stays inside the axis."""

    source = {
        "29C or below": 0.0,
        "30C": 0.25,
        "31C": 0.50,
        "32C": 0.25,
        "33C or higher": 0.0,
    }
    adjusted, receipt = apply_center_width_calibration(
        source,
        delta_c=0.30,
        width_lambda=1.0,
        ordered_keys=KEYS,
        key_centers_c=CENTERS,
    )
    assert receipt["state"] == "APPLIED"
    assert _mean(adjusted) == pytest.approx(_mean(source) + 0.30, abs=1e-9)


def test_edge_mass_is_absorbed_rather_than_leaving_the_axis() -> None:
    """A shift past the last centre piles up there, so the mean moves less.

    Not a defect: the edge buckets are open-ended, so "hotter than 33" has
    nowhere further to go and the histogram cannot represent it moving.  The
    fit measured this same behaviour, which is why the scalars it chose are
    the ones this function must reproduce.
    """

    source = _uniformish()
    adjusted, _ = apply_center_width_calibration(
        source,
        delta_c=0.30,
        width_lambda=1.0,
        ordered_keys=KEYS,
        key_centers_c=CENTERS,
    )
    moved = _mean(adjusted) - _mean(source)
    assert 0.0 < moved < 0.30
    # Exactly the mass the top bucket could not carry any further.
    assert moved == pytest.approx(0.30 - 0.30 * source["33C or higher"], abs=1e-9)


def test_scale_narrows_without_moving_the_mean() -> None:
    """Lambda is a width knob, not a second centre knob.

    Scaling about the distribution's own mean is what makes the two scalars
    separable -- if it moved the centre too, the shift fitted beside it
    would be absorbing part of the width correction.

    The realised narrowing is weaker than lambda: splitting each point mass
    between two centres adds s(1-s)*span^2 of variance back.  Lambda is
    therefore nominal, and the number that matters is the one the fit chose
    on this same function.
    """

    source = _uniformish()
    adjusted, _ = apply_center_width_calibration(
        source,
        delta_c=0.0,
        width_lambda=0.80,
        ordered_keys=KEYS,
        key_centers_c=CENTERS,
    )
    assert _mean(adjusted) == pytest.approx(_mean(source), abs=1e-9)
    narrowed = _sigma(adjusted) / _sigma(source)
    assert 0.80 < narrowed < 1.0
    # Monotone in lambda, which is what the fit's grid search relies on.
    tighter, _ = apply_center_width_calibration(
        source,
        delta_c=0.0,
        width_lambda=0.60,
        ordered_keys=KEYS,
        key_centers_c=CENTERS,
    )
    assert _sigma(tighter) < _sigma(adjusted)


def test_hard_bound_is_not_undone_by_the_correction() -> None:
    """A shift may not put mass back on an outcome the day has ruled out.

    The hard exclusion is an observation -- the day is already hotter than
    those buckets -- and a scalar fitted on fortnight averages does not get
    to overrule it.  Without the re-zero a negative shift walks mass
    straight back onto the excluded rungs.
    """

    source = {
        "29C or below": 0.0,
        "30C": 0.0,
        "31C": 0.50,
        "32C": 0.35,
        "33C or higher": 0.15,
    }
    adjusted, receipt = apply_center_width_calibration(
        source,
        delta_c=-0.60,
        width_lambda=1.0,
        ordered_keys=KEYS,
        key_centers_c=CENTERS,
        excluded_buckets=("29C or below", "30C"),
    )
    assert adjusted["29C or below"] == 0.0
    assert adjusted["30C"] == 0.0
    assert receipt["mass_returned_to_hard_bound"] > 0.0
    assert sum(adjusted.values()) == pytest.approx(1.0, abs=1e-9)


def test_refuses_when_everything_lands_on_ruled_out_buckets() -> None:
    """No mass left is a failure, not a correction -- serve the input."""

    source = {
        "29C or below": 0.0,
        "30C": 1.0,
        "31C": 0.0,
        "32C": 0.0,
        "33C or higher": 0.0,
    }
    adjusted, receipt = apply_center_width_calibration(
        source,
        delta_c=-1.4,
        width_lambda=1.0,
        ordered_keys=KEYS,
        key_centers_c=CENTERS,
        excluded_buckets=("29C or below",),
    )
    assert receipt["state"] == "REFUSED_EMPTY_AFTER_HARD_BOUND"
    assert adjusted == source


def test_out_of_range_scalars_are_refused() -> None:
    """Beyond these the fit is reporting a fault, not a bias.

    Bending the distribution onto a 3-degree shift or a half-width would
    hide whatever produced that number -- a broken bucket axis, a stage
    whose centre is not what it says -- behind a plausible-looking curve.
    """

    for kwargs in (
        {"delta_c": 3.0, "width_lambda": 1.0},
        {"delta_c": 0.0, "width_lambda": 0.2},
        {"delta_c": 0.0, "width_lambda": 2.5},
        {"delta_c": float("nan"), "width_lambda": 1.0},
    ):
        with pytest.raises(ValueError):
            apply_center_width_calibration(
                _uniformish(),
                ordered_keys=KEYS,
                key_centers_c=CENTERS,
                **kwargs,
            )


def test_mass_is_conserved_and_edges_absorb() -> None:
    source = _uniformish()
    adjusted, _ = apply_center_width_calibration(
        source,
        delta_c=1.4,
        width_lambda=1.0,
        ordered_keys=KEYS,
        key_centers_c=CENTERS,
    )
    assert sum(adjusted.values()) == pytest.approx(1.0, abs=1e-9)
    assert adjusted["33C or higher"] > source["33C or higher"]


def test_edge_bucket_centres_use_the_fitted_convention() -> None:
    """Serving and fitting must place the open-ended buckets identically.

    The edge buckets have no far side, so their centre is a convention.  If
    the two sides pick different ones the correction lands somewhere the
    measurement never looked -- the same serve/train split that took the
    spread cap down.
    """

    intervals = {
        "29C or below": [None, 29.5],
        "30C": [29.5, 30.5],
        "31C": [30.5, 31.5],
        "32C": [31.5, 32.5],
        "33C or higher": [32.5, None],
    }
    centers = bucket_centers_from_intervals(
        intervals, ordered_keys=KEYS, edge_width_c=2.0
    )
    assert centers == [28.5, 30.0, 31.0, 32.0, 33.5]


def test_only_p0_serves_the_layer_and_materialization_applies_none() -> None:
    """Charter 150 (2026-09-19): P0 is the only stage with a centre/width
    correction, and it applies it inside its own compile (``served_shape``,
    geometry before claim).  P0-M and P1 are one likelihood update each on
    P0's root, so the materializer carries neither the call nor the band
    table the ordering rule used to guard.
    """

    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "materialization.py"
    ).read_text(encoding="utf-8")
    assert "apply_center_width_calibration(" not in source
    assert "band_table" not in source
