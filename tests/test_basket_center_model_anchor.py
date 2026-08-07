"""The basket anchor is where the stage's data points, not the argmax bucket.

An open-ended edge bucket ("35°C or higher") spans an unbounded interval, so it
collects the entire upper tail of every ensemble member.  Ranked against 1°C
buckets it is not commensurable, and with wide system disagreement it wins the
probability argmax outright.

Live ZUUU 2026-08-07: ECMWF center 30.87°C, GEFS center 34.39°C, model envelope
31°C..34°C -- and the edge bucket anchored the basket at 0.3344, outside that
envelope.  The weight-averaged center is 32.80°C, i.e. the 33°C bucket.

An edge bucket stays a legitimate anchor when the estimate really falls inside
it; centers of 36/37 land there and should.
"""

import pytest

from bayes_basket_target.basket_kernel.contracts import (
    AskLevel,
    BucketObservation,
    ContractError,
)
from bayes_basket_target.basket_kernel.support import _center
from bayes_basket_target.native_ensemble_p0 import _bucket_for_value


INTERVALS = {
    "25°C or below": (None, 25.5), "26°C": (25.5, 26.5), "27°C": (26.5, 27.5),
    "28°C": (27.5, 28.5), "29°C": (28.5, 29.5), "30°C": (29.5, 30.5),
    "31°C": (30.5, 31.5), "32°C": (31.5, 32.5), "33°C": (32.5, 33.5),
    "34°C": (33.5, 34.5), "35°C or higher": (34.5, None),
}
ORDINALS = {bucket: index for index, bucket in enumerate(INTERVALS)}


def _buckets(probabilities: dict[str, float]) -> tuple[BucketObservation, ...]:
    return tuple(
        BucketObservation(
            bucket=bucket,
            ordinal=ORDINALS[bucket],
            probability=probabilities.get(bucket, 0.0),
            bid_price=0.0,
            ask_levels=(AskLevel(price=0.5, shares=10.0),),
            ask_available=True,
            bid_levels=(),
            price_tick_size=0.01,
            maker_queue_ahead_shares=0.0,
            maker_queue_known=True,
        )
        for bucket in ORDINALS
    )


# The exact live ZUUU distribution.
ZUUU = {
    "27°C": 0.0064, "28°C": 0.0189, "29°C": 0.0567, "30°C": 0.0697,
    "31°C": 0.1452, "32°C": 0.1191, "33°C": 0.1439, "34°C": 0.1057,
    "35°C or higher": 0.3344,
}


def _weighted_center(centers: dict[str, float], weights: dict[str, float]) -> str:
    return _bucket_for_value(
        sum(weights[system] * centers[system] for system in centers), INTERVALS
    )


def test_live_zuuu_edge_bucket_wins_the_probability_argmax() -> None:
    # Establishes the defect this anchor exists to prevent.
    assert _center(_buckets(ZUUU)).bucket == "35°C or higher"


def test_live_zuuu_anchors_on_the_model_center_instead() -> None:
    anchor = _weighted_center(
        {"ECMWF": 30.866209818522137, "GEFS": 34.390733078167},
        {"ECMWF": 0.45, "GEFS": 0.55},
    )
    assert anchor == "33°C"

    center = _center(_buckets(ZUUU), model_center_ordinal=ORDINALS[anchor])
    assert center.bucket == "33°C"
    # The anchor is deliberately not the most probable bucket.
    assert center.probability < ZUUU["35°C or higher"]


def test_anchor_lands_inside_the_model_envelope() -> None:
    # Envelope is the span of the system centers: 31°C..34°C here.
    anchor = _weighted_center(
        {"ECMWF": 30.866209818522137, "GEFS": 34.390733078167},
        {"ECMWF": 0.45, "GEFS": 0.55},
    )
    assert ORDINALS["31°C"] <= ORDINALS[anchor] <= ORDINALS["34°C"]


@pytest.mark.parametrize(
    "centers, expected",
    [
        ({"a": 36.0, "b": 37.0}, "35°C or higher"),
        ({"a": 34.6, "b": 35.2}, "35°C or higher"),
        ({"a": 30.0, "b": 31.0}, "31°C"),
        ({"a": 24.0, "b": 24.5}, "25°C or below"),
    ],
)
def test_edge_buckets_remain_eligible_anchors(centers, expected) -> None:
    # An edge bucket is not banned -- it anchors when the models truly point
    # there, which is exactly what distinguishes this from an argmax rule.
    assert _weighted_center(centers, {"a": 0.45, "b": 0.55}) == expected


def test_anchor_uses_system_weights_not_a_plain_mean() -> None:
    centers = {"a": 30.0, "b": 34.0}
    # Plain mean would be 32.0 -> "32°C"; the 0.55/0.45 tilt moves it to 33°C.
    assert _weighted_center(centers, {"a": 0.45, "b": 0.55}) == "32°C"
    assert _weighted_center(centers, {"a": 0.05, "b": 0.95}) == "34°C"


def test_missing_anchor_falls_back_to_probability_argmax() -> None:
    # Stages that publish no anchor keep the previous behaviour.
    assert _center(_buckets(ZUUU), model_center_ordinal=None).bucket == (
        "35°C or higher"
    )


def test_anchor_outside_the_quoted_bucket_set_fails_closed() -> None:
    with pytest.raises(ContractError, match="support_model_center_bucket_absent"):
        _center(_buckets(ZUUU), model_center_ordinal=99)
