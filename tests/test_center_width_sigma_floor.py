"""The width floor: a served claim narrower than the floor is disbelieved.

Measured 2026-09-05 on P0's own serving cells (9 days, 3,589 cells, anchored
at the served-arithmetic boundary): where the served distribution calls itself
narrower than 1.44 rungs the standardised error's standard deviation is 0.95,
and where it calls itself wider it is 0.67.  One pooled lambda cannot serve
both -- 0.75 shrinks a group that is already too narrow and under-shrinks the
rest -- so the claim is floored and the pooled pair refitted underneath it.

Two arms were measured on the same forward window.  Flooring then refitting
one lambda gained +0.0132 nats against the incumbent; fitting a *second*
lambda per tier gained only +0.0101, because the heterogeneity lives inside
the narrow tier rather than between the tiers.  These tests pin the arm that
won, its entitlement bounds, and the cross-unit reading charter 53(b) needs.
"""

from __future__ import annotations

import math

import pytest

from bayes_basket_target.center_width_calibration import (
    MAX_SIGMA_FLOOR_LIFT,
    MAX_SIGMA_FLOOR_RUNGS,
    apply_center_width_calibration,
    bucket_rung_width_c,
    sigma_floor_lift_v1,
)
from bayes_basket_target.center_width_store import (
    SIGMA_FLOOR_RUNGS,
    _mean_nll,
    _search,
)

# A Celsius market: open-ended edges placed half of EDGE_WIDTH_C beyond their
# finite side, so the first and last gaps are 1.5 and the rung is 1.0.
KEYS_C = ["23C or below", "24C", "25C", "26C", "27C", "28C", "29C or higher"]
CENTERS_C = [22.5, 24.0, 25.0, 26.0, 27.0, 28.0, 29.5]
# The same shape on a Fahrenheit market, whose rung is 2F = 10/9 C.
RUNG_F = 10.0 / 9.0
KEYS_F = [f"f{index}" for index in range(7)]
CENTERS_F = [10.0 + RUNG_F * index for index in range(7)]


def _sigma(dist: dict[str, float], centers: list[float]) -> float:
    total = sum(dist.values())
    mean = sum(v * c for v, c in zip(dist.values(), centers)) / total
    return math.sqrt(
        sum(v / total * (c - mean) ** 2 for v, c in zip(dist.values(), centers))
    )


def _dist(keys: list[str], weights: list[float]) -> dict[str, float]:
    return dict(zip(keys, weights))


# sigma 0.69 rungs: short of the floor by more than the lift may claim.
NARROW = [0.0, 0.02, 0.16, 0.64, 0.16, 0.02, 0.0]
# sigma 1.04 rungs: short of the floor, and reachable inside the bound.
MILD = [0.0, 0.08, 0.22, 0.40, 0.22, 0.08, 0.0]
# sigma 1.97 rungs: already past the floor.
WIDE = [0.10, 0.14, 0.16, 0.20, 0.16, 0.14, 0.10]


def test_floor_is_identity_when_it_is_not_asked_for() -> None:
    """The whole history of this layer is floor zero, byte for byte."""

    probability = _dist(KEYS_C, NARROW)
    lone, _ = apply_center_width_calibration(
        probability, delta_c=0.1, width_lambda=0.75,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
    )
    zeroed, receipt = apply_center_width_calibration(
        probability, delta_c=0.1, width_lambda=0.75,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        sigma_floor_rungs=0.0,
    )
    assert zeroed == lone
    assert receipt["sigma_floor_lift"] == 1.0
    assert receipt["sigma_floor_rungs"] == 0.0


def test_a_claim_at_or_above_the_floor_is_left_alone() -> None:
    """The wide tier is not a second case; it is where the lift is one."""

    probability = _dist(KEYS_C, WIDE)
    assert _sigma(probability, CENTERS_C) > SIGMA_FLOOR_RUNGS
    lift, sigma_c, rung_c = sigma_floor_lift_v1(
        probability, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert lift == 1.0
    assert rung_c == 1.0
    assert sigma_c == pytest.approx(_sigma(probability, CENTERS_C))
    floored, _ = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.75,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    plain, _ = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.75,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
    )
    assert floored == plain


def test_a_narrow_claim_is_lifted_exactly_to_the_floor() -> None:
    """The lift is what it takes to reach the floor, and no more."""

    probability = _dist(KEYS_C, MILD)
    sigma_c = _sigma(probability, CENTERS_C)
    assert sigma_c < SIGMA_FLOOR_RUNGS
    lift, reported, rung_c = sigma_floor_lift_v1(
        probability, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert reported == pytest.approx(sigma_c)
    assert lift == pytest.approx(SIGMA_FLOOR_RUNGS * rung_c / sigma_c)
    # Serving at lambda 1 under the floor lands the spread on the floor,
    # up to the histogram's own re-splitting, which only ever widens.
    floored, receipt = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=1.0,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert _sigma(floored, CENTERS_C) >= SIGMA_FLOOR_RUNGS * rung_c - 1e-9
    assert receipt["width_lambda"] == pytest.approx(lift)
    assert receipt["distribution_sigma_c"] == pytest.approx(sigma_c)
    assert receipt["bucket_rung_c"] == rung_c


def test_the_floor_is_read_in_rungs_not_degrees() -> None:
    """Charter 53(b): a rule counted in rungs is synonymous across units.

    The same shape on a Fahrenheit ladder must be lifted by the same factor,
    not by the ratio of the two units -- the defect this repository has now
    written down three times.
    """

    celsius = _dist(KEYS_C, NARROW)
    fahrenheit = _dist(KEYS_F, NARROW)
    lift_c, _, rung_c = sigma_floor_lift_v1(
        celsius, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    lift_f, _, rung_f = sigma_floor_lift_v1(
        fahrenheit, ordered_keys=KEYS_F, key_centers_c=CENTERS_F,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert rung_c == 1.0
    assert rung_f == pytest.approx(RUNG_F)
    assert lift_f == pytest.approx(lift_c)


def test_the_rung_is_the_smallest_gap_so_open_edges_cannot_set_it() -> None:
    assert bucket_rung_width_c(CENTERS_C) == 1.0
    # A five-bucket ladder has four gaps, two of them edges; the median of an
    # even count lands on an edge gap and reads the rung half again too wide.
    assert bucket_rung_width_c([0.0, 1.5, 2.5, 3.5, 5.0]) == 1.0


def test_the_lift_carries_its_own_entitlement_bound() -> None:
    """Doubling is the mirror of the halving the shrink may already claim."""

    point_mass = {key: 0.0 for key in KEYS_C}
    point_mass[KEYS_C[3]] = 1.0
    lift, sigma_c, _ = sigma_floor_lift_v1(
        point_mass, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert sigma_c == 0.0
    assert lift == MAX_SIGMA_FLOOR_LIFT
    # A floor wider than this is describing something structural, not a
    # correction this layer is entitled to make.
    with pytest.raises(ValueError):
        sigma_floor_lift_v1(
            _dist(KEYS_C, NARROW), ordered_keys=KEYS_C,
            key_centers_c=CENTERS_C,
            floor_rungs=MAX_SIGMA_FLOOR_RUNGS + 0.1,
        )
    with pytest.raises(ValueError):
        apply_center_width_calibration(
            _dist(KEYS_C, NARROW), delta_c=0.0, width_lambda=1.0,
            ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
            sigma_floor_rungs=-1.0,
        )


def test_the_lift_composes_onto_the_lambda_like_the_station_scale() -> None:
    """Two scalings about the same mean multiply; each keeps its own bounds.

    The product may leave the lambda's own range, which is exactly why the
    floor is a separate argument rather than something a caller folds in.
    """

    probability = _dist(KEYS_C, MILD)
    lift, _, _ = sigma_floor_lift_v1(
        probability, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    floored, receipt = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.725,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert receipt["width_lambda"] == pytest.approx(0.725 * lift)
    equivalent, _ = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.725 * lift,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
    )
    assert floored == equivalent


def test_the_search_scores_the_floor_it_would_serve() -> None:
    """The grid searched under a floor must score under that same floor.

    A search that floored the candidate but scored the unfloored claim would
    be the serve/train split the spread cap taught this codebase once.
    """

    cells = [
        {
            "probabilities": _dist(KEYS_C, NARROW),
            "ordered_keys": KEYS_C,
            "key_centers_c": CENTERS_C,
            "excluded_buckets": (),
            "settled_bucket": KEYS_C[5],
        },
        {
            "probabilities": _dist(KEYS_C, WIDE),
            "ordered_keys": KEYS_C,
            "key_centers_c": CENTERS_C,
            "excluded_buckets": (),
            "settled_bucket": KEYS_C[2],
        },
    ]
    delta, lam, score = _search(cells, sigma_floor_rungs=SIGMA_FLOOR_RUNGS)
    assert score == pytest.approx(
        _mean_nll(
            cells, delta_c=delta, width_lambda=lam,
            sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
        )
    )
    # And the floored score beats the unfloored one on a cell whose truth
    # settled two rungs out from a claim that called itself certain.
    unfloored = _mean_nll(cells, delta_c=delta, width_lambda=lam)
    assert score < unfloored


def test_the_shadow_reports_but_serves_nothing() -> None:
    """Fit-only, by the user's ruling of 2026-09-05.

    The shadow may say ADMITTED without a single served number moving; what
    it must never do is reach into the scalars the stage is serving.
    """

    from bayes_basket_target.center_width_store import _sigma_floor_shadow

    def cell(weights: list[float], settled: str) -> dict[str, object]:
        return {
            "probabilities": _dist(KEYS_C, weights),
            "ordered_keys": KEYS_C,
            "key_centers_c": CENTERS_C,
            "excluded_buckets": (),
            "settled_bucket": settled,
        }

    train = [cell(NARROW, KEYS_C[5]), cell(WIDE, KEYS_C[2])]
    forward = [cell(NARROW, KEYS_C[1]), cell(WIDE, KEYS_C[4])]
    out = _sigma_floor_shadow(
        train=train, forward=forward, incumbent=(0.0, 0.75),
        pooled=(0.0, 0.75), pooled_admitted=False,
    )
    assert out["served"] is False
    assert out["sigma_floor_rungs"] == SIGMA_FLOOR_RUNGS
    assert out["state"] in {"ADMITTED", "REFUSED_FORWARD"}
    assert out["forward_lifted_count"] == 1
    assert out["forward_cell_count"] == 2
    # Empty windows say so rather than inventing a verdict.
    assert _sigma_floor_shadow(
        train=[], forward=forward, incumbent=(0.0, 1.0),
        pooled=(0.0, 1.0), pooled_admitted=False,
    )["state"] == "NOT_MEASURED"
