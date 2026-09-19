from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.p1_compiler import (
    _bucket_geometry_c,
    _hard_exclusion_threshold_c,
)


F_BUCKETS = (
    "59°F or below", "60-61°F", "62-63°F", "64-65°F", "66-67°F",
    "68-69°F", "70-71°F", "72-73°F", "74-75°F", "76-77°F", "78°F or higher",
)
# The market's own half-open edges, in Celsius: 67.5F is 19.7222C.
F_INTERVALS = {
    "59°F or below": [None, 15.277777777777779],
    "60-61°F": [15.277777777777779, 16.38888888888889],
    "62-63°F": [16.38888888888889, 17.5],
    "64-65°F": [17.5, 18.61111111111111],
    "66-67°F": [18.61111111111111, 19.72222222222222],
    "68-69°F": [19.72222222222222, 20.833333333333332],
    "70-71°F": [20.833333333333332, 21.944444444444443],
    "72-73°F": [21.944444444444443, 23.055555555555557],
    "74-75°F": [23.055555555555557, 24.166666666666668],
    "76-77°F": [24.166666666666668, 25.27777777777778],
    "78°F or higher": [25.27777777777778, None],
}
F_UPPER_LABELS = {
    "59°F or below": 59.0, "60-61°F": 61.0, "62-63°F": 63.0,
    "64-65°F": 65.0, "66-67°F": 67.0, "68-69°F": 69.0, "70-71°F": 71.0,
    "72-73°F": 73.0, "74-75°F": 75.0, "76-77°F": 77.0,
    "78°F or higher": None,
}


def _root(lineage: dict) -> ProbabilityEnvelope:
    share = 1.0 / len(F_BUCKETS)
    return ProbabilityEnvelope.from_mapping({
        "schema": "bayes_basket_probability_envelope_v1",
        "stage": "P0",
        "checkpoint_id": "p0-native-test",
        "station_id": "KSFO",
        "event_date": "2026-08-20",
        "settlement_timezone": "America/Los_Angeles",
        "decision_at_utc": "2026-08-20T18:00:00.000Z",
        "fresh_until_utc": "2026-08-21T18:00:00.000Z",
        "probabilities": {bucket: share for bucket in F_BUCKETS},
        "bucket_ordinals": {
            bucket: index for index, bucket in enumerate(F_BUCKETS)
        },
        "posterior_version": "test",
        "bridge_sha256": "0" * 64,
        "posterior_sha256": "1" * 64,
        "lineage": lineage,
    })


def test_f_threshold_is_the_first_killing_report_not_a_celsius_margin() -> None:
    """66-67F dies at the first 68F report, which is exactly 20.0C.

    The margin has to be taken on the reporting grid.  A whole Celsius degree
    added to an F market's edge lands at 20.22C, which no 68F report reaches,
    so the bucket survives a rung past its own death.
    """

    assert _hard_exclusion_threshold_c(67.0, "F") == pytest.approx(20.0)
    assert _hard_exclusion_threshold_c(65.0, "F") == pytest.approx(
        (66.0 - 32.0) / 1.8
    )
    # C markets report whole degrees C, so their margin stays a whole degree.
    assert _hard_exclusion_threshold_c(33.0, "C") == pytest.approx(34.0)


def test_intervals_alone_are_enough_to_use_the_exact_geometry() -> None:
    """A root stating its intervals must not have them re-derived.

    Roots carry ``bucket_intervals_c_json`` without ``bucket_centers_c_json``;
    requiring both sent every one of them down the formula path and replaced
    exact edges with approximated ones.
    """

    centers, uppers = _bucket_geometry_c(
        _root({"bucket_intervals_c_json": __import__("json").dumps(F_INTERVALS)}),
        unit="F",
        upper_by_bucket=F_UPPER_LABELS,
    )
    for bucket, bounds in F_INTERVALS.items():
        if bounds[1] is not None:
            assert uppers[bucket] == pytest.approx(bounds[1]), bucket
    # Centres are the interval midpoints, and the open-ended top bucket takes
    # the ladder's own rung rather than a hardcoded half degree.
    assert centers["66-67°F"] == pytest.approx((18.61111111111111 + 19.72222222222222) / 2)
    assert centers["78°F or higher"] == pytest.approx(25.27777777777778 + 5.0 / 9.0)
    # ...but no upper edge: no report can rule an open top out (review
    # 2026-09-19 -- a finite stand-in exhausted the support).
    assert uppers["78°F or higher"] is None


def test_ksfo_20260820_dead_bucket_is_excluded() -> None:
    """Regression: KSFO reached 68F and 66-67F kept 27.2% of the posterior.

    The bucket is dead above 67.5F; at a running high of 20.0C (68.0F) its
    upper bound must compare as reached.
    """

    _, uppers = _bucket_geometry_c(
        _root({"bucket_intervals_c_json": __import__("json").dumps(F_INTERVALS)}),
        unit="F",
        upper_by_bucket=F_UPPER_LABELS,
    )
    running_high = 20.0
    assert uppers["66-67°F"] <= running_high
    assert uppers["68-69°F"] > running_high
