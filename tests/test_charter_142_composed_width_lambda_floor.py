"""Charter 142: the composed width lambda has a floor of 0.5.

The pooled lambda, the station width scale and the sigma-floor lift each pass
their own bounds, and until 2026-09-17 their product was allowed to reach
0.25 on that argument.  The station width table v1 then served 1,611 P0 rows
at 0.75 x 0.5 = 0.375 (2026-09-06..10): settled truth on an exact-zero
bucket 4.59% of the time against 0.13% under the pooled lambda alone, and
+0.57 nat against identity, until the incumbent veto retired it.  The user
ruled the composed product floored at 0.5.

The fit and the serving path share ``apply_center_width_calibration``, so
the floor is a property of the function, not of any caller.
"""
from __future__ import annotations

import math

import pytest

from bayes_basket_target import center_width_calibration as cw
from bayes_basket_target.center_width_calibration import (
    MIN_COMPOSED_WIDTH_LAMBDA,
    MIN_STATION_WIDTH_SCALE,
    MIN_WIDTH_LAMBDA,
    apply_center_width_calibration,
)

KEYS = ["23°C or below", "24°C", "25°C", "26°C", "27°C", "28°C or higher"]
CENTERS = [22.5, 24.0, 25.0, 26.0, 27.0, 28.5]
PEAKED = dict(zip(KEYS, [0.05, 0.15, 0.3, 0.3, 0.15, 0.05]))


def _sd(dist: dict[str, float]) -> float:
    mean = sum(v * c for v, c in zip(dist.values(), CENTERS))
    return math.sqrt(sum(v * (c - mean) ** 2 for v, c in zip(dist.values(), CENTERS)))


def test_the_floor_is_half() -> None:
    assert MIN_COMPOSED_WIDTH_LAMBDA == 0.5
    # The floor is what the pooled lambda alone may already claim; it only
    # ever binds when a station scale is composed onto it.
    assert MIN_COMPOSED_WIDTH_LAMBDA == MIN_WIDTH_LAMBDA
    assert MIN_WIDTH_LAMBDA * MIN_STATION_WIDTH_SCALE < MIN_COMPOSED_WIDTH_LAMBDA


def test_the_v1_composition_is_served_at_the_floor() -> None:
    """0.75 x 0.5 = 0.375 -- what the retired table served -- now serves 0.5,
    byte for byte what the pooled lambda 0.5 alone would serve."""

    floored, receipt = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=0.75, station_width_scale=0.5,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    at_floor, _ = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=0.5,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert floored == at_floor
    assert receipt["width_lambda"] == 0.5
    assert receipt["composed_width_lambda_unfloored"] == pytest.approx(0.375)
    assert receipt["composed_width_lambda_floor"] == 0.5
    assert receipt["composed_width_lambda_floored"] is True
    assert receipt["station_width_scale"] == 0.5
    assert receipt["pooled_width_lambda"] == 0.75
    # And it is wider than what 0.375 would have served: the floor keeps
    # mass in the shoulders that the unfloored contraction emptied.
    unfloored, _ = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=0.5, station_width_scale=0.75,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert _sd(floored) > 0.0
    assert receipt["composed_width_lambda_unfloored"] < receipt["width_lambda"]
    # 0.5 x 0.75 composes to 0.375 too, and lands on the same served vector.
    assert unfloored == floored


def test_a_composition_above_the_floor_is_untouched() -> None:
    composed, receipt = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=0.8, station_width_scale=0.7,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    product, _ = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=0.56,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert composed == product
    assert receipt["width_lambda"] == pytest.approx(0.56)
    assert receipt["composed_width_lambda_unfloored"] == pytest.approx(0.56)
    assert receipt["composed_width_lambda_floored"] is False


def test_exactly_the_floor_is_not_floored() -> None:
    _, receipt = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=0.5, station_width_scale=1.0,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert receipt["width_lambda"] == 0.5
    assert receipt["composed_width_lambda_floored"] is False
    # Identity is still identity, byte for byte.
    same, receipt = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=1.0, station_width_scale=1.0,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert same == PEAKED
    assert receipt["state"] == "IDENTITY"
    assert receipt["composed_width_lambda_floored"] is False


def test_a_lane_cannot_buy_a_width_below_the_floor() -> None:
    """Every station scale that composes below the floor scores -- and would
    serve -- the same vector, so a forward search over scales has no gradient
    below 0.5 / pooled and cannot select one on its own merits."""

    vectors = []
    for scale in (0.5, 0.55, 0.6, 0.65, 2.0 / 3.0):
        served, receipt = apply_center_width_calibration(
            PEAKED, delta_c=0.0, width_lambda=0.75, station_width_scale=scale,
            ordered_keys=KEYS, key_centers_c=CENTERS,
        )
        assert receipt["width_lambda"] == pytest.approx(0.5)
        vectors.append(tuple(round(v, 12) for v in served.values()))
    assert len(set(vectors)) == 1
    # Just above the floor the width moves again.
    above, receipt = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=0.75, station_width_scale=0.7,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert receipt["composed_width_lambda_floored"] is False
    assert tuple(round(v, 12) for v in above.values()) not in set(vectors)


def test_the_floor_is_the_module_constant_not_a_local_number(monkeypatch) -> None:
    """Reverse check: lowering the constant must lower what is served."""

    monkeypatch.setattr(cw, "MIN_COMPOSED_WIDTH_LAMBDA", 0.25)
    served, receipt = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=0.75, station_width_scale=0.5,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert receipt["width_lambda"] == pytest.approx(0.375)
    assert receipt["composed_width_lambda_floored"] is False
    at_half, _ = apply_center_width_calibration(
        PEAKED, delta_c=0.0, width_lambda=0.5,
        ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    assert served != at_half
